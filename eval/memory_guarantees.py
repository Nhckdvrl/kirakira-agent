"""What this memory gives that a stock retrieval memory does not, measured.

On answer accuracy this project is indistinguishable from plain dense retrieval
on the public benchmark subset it runs, and it does not claim otherwise. So the
question an evaluation has to answer is a different one: what does the receipt
design buy, and can that be shown rather than asserted?

Three properties are measured here against a baseline that stands in for the
ordinary pattern -- embed the turn, take the top-k nearest messages, paste their
text into the prompt, keep no record of what was shown:

1. Context reproducibility. This project freezes the retrieval into an immutable
   receipt at the first query and re-renders every later copy of that same
   request from the receipt, so a retry, a tool continuation step, a reminder
   replay or a restart carries byte-identical material. A system that re-queries
   a live index returns whatever the index now says, and the index keeps
   growing.
2. Read confinement. A model may re-read an original only through a receipt that
   actually surfaced it. Scope mismatch, an unknown retrieval reference and a
   message this retrieval never presented are each refused. A stock memory
   search is scoped to a user, not to a request, so anything that user ever said
   is reachable mid-conversation -- which is what makes an injected instruction
   inside recalled text worth worrying about.
3. Repetition. Originals the request already quotes verbatim are not pasted in
   again.

Both systems run on the same ledger, the same messages and the same vectors, so
the comparison is of designs rather than of retrieval quality. No chat model is
involved and no API key is needed.

Fairness, stated rather than implied: a stock stack *could* cache the text it
retrieved per request and become reproducible, and it could keep an allow-list
per request and become confined. The point is not that this is impossible
elsewhere; it is that here these are properties of the data structure the system
already keeps, they hold across restart and index change, and they can be
re-checked offline from a published artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.plugin_composition import CompositionRoot, Context
from plugins.akasha.application.consumer import MessageConsumer
from plugins.akasha.contract import MemorySourceQuery
from plugins.akasha.domain.model import MemoryConfig
from plugins.akasha.learning import AKASHA_LEARNING, Learning, LearningConfig
from plugins.akasha.query import MemoryQueries
from plugins.akasha.recalls import RecallRecords, context_identity
from plugins.akasha.runtime import MessageMemory
from plugins.content.api import legacy_post_commit_effect
from plugins.content.plugin import check_text
from plugins.ledger.bindings import Bindings
from plugins.ledger.contract import ContentPart, Input, Output
from plugins.ledger.embedding_store import MessageEmbeddings
from plugins.ledger.log import MessageLog, SessionAttributes
from plugins.turn_projection.plugin import TurnProjection

from eval.significance import mcnemar_exact

SESSION = "conversation"
OTHER_SESSION = "other-conversation"
SOURCE = "conversation"
DIMENSIONS = 8


def vector(text: str) -> list[float]:
    """A fixed unit vector per text, so both systems rank identically.

    Derived from the text rather than random so a rerun reproduces it, and
    spread over several dimensions so nearest-neighbour order actually depends
    on content: with one constant vector every candidate ties and the baseline
    would look stable for the wrong reason.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [digest[i] - 127.5 for i in range(DIMENSIONS)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


@dataclass
class StockMemory:
    """The ordinary pattern: top-k over everything, pasted in, nothing recorded.

    It holds no per-request state at all, which is the point of the comparison:
    asked the same question twice it answers from the index as it stands at that
    moment, and it can read anything in the store because nothing says which
    originals a particular turn was entitled to.
    """

    texts: dict[str, str] = field(default_factory=dict)
    vectors: dict[str, list[float]] = field(default_factory=dict)
    limit: int = 4
    max_chars: int = 2400

    def remember(self, message_id: str, text: str) -> None:
        self.texts[message_id] = text
        self.vectors[message_id] = vector(text)

    def search(self, query: str) -> list[str]:
        probe = vector(query)
        ranked = sorted(self.vectors, key=lambda key: (-cosine(probe, self.vectors[key]), key))
        return ranked[: self.limit]

    def render(self, query: str) -> str:
        """The material this turn would inject."""
        blocks = [f"[{key}] {self.texts[key]}" for key in self.search(query)]
        return "\n\n".join(blocks)[: self.max_chars]

    def read_any(self, message_id: str) -> str | None:
        """No receipt, so no notion of 'this turn was not shown that'."""
        return self.texts.get(message_id)


@asynccontextmanager
async def _memory(path: Path) -> AsyncIterator[tuple]:
    log = MessageLog(path / "sessions.db")
    log.ensure_session(SESSION, SessionAttributes())
    log.ensure_session(OTHER_SESSION, SessionAttributes())
    embeddings = MessageEmbeddings(log, log._path.with_name("sessions-derived.db"))
    root = CompositionRoot("memory-guarantees")
    learning = Learning(TurnProjection(), owner="akasha", post_commit_effect=legacy_post_commit_effect)
    bindings = Bindings(log, root.context)
    rule = LearningConfig(embedding_model="fixed", dimension=DIMENSIONS, sources=(SOURCE,))
    identity = "fixed-learning"
    log.save_binding(identity, {"version": 1, "service": AKASHA_LEARNING.name,
                                "root_ref": "guarantees-archive", "metadata": rule.model_dump()})

    async def provide(ctx: Context) -> None:
        await ctx.provide(AKASHA_LEARNING, learning)

    async def embed(texts: list[str]) -> list[list[float]]:
        return [vector(text) for text in texts]

    await root.mount(provide, name="learning")
    if not root.receipt().ready:
        raise RuntimeError(repr(root.receipt()))
    consumer = await MessageConsumer.load(path / "memory.db", catalog=log.catalog(),
                                          embeddings=embeddings, bindings=bindings,
                                          config=MemoryConfig(), cutover=False)
    records = RecallRecords(log.owner("akasha"))
    memory = MessageMemory(consumer, catalog=log.catalog(), embeddings=embeddings,
                           bindings=bindings, learning_binding=identity,
                           records=records, embed_batch=embed)
    try:
        yield memory, learning, log, embeddings, records, consumer
    finally:
        await memory.close()
        await root.dispose()
        embeddings.close()
        log.close()


def _append(log, embeddings, learning, session, name, author, kind, body_text):
    body = (Input if kind is Input else Output)
    content = body((ContentPart("text", body_text),)) if kind is Input else Output(
        (ContentPart("text", body_text),), "complete")
    message = log.writer(session, author=author, source=SOURCE, body_types=(kind,),
                         content={"text": check_text}).append(name, content)
    embeddings.bind(learning.text).save(message, model="fixed", embedding=vector(body_text))
    return message


async def measure(*, seed_turns: int, live_turns: int) -> dict[str, object]:
    stock = StockMemory()
    reproducible = {"ours": 0, "stock": 0, "turns": 0, "drift": []}
    confinement = {"ours_refused": 0, "ours_allowed_control": 0, "stock_refused": 0,
                   "attempts": 0, "refusals": []}
    repetition = {"ours_duplicate_bodies": 0, "stock_duplicate_bodies": 0, "turns": 0}
    attribution = {"receipts": 0, "with_cause": 0, "prefix_pinned": 0, "kinds": {}}
    fidelity = {"passages": 0, "verbatim": 0, "chars": 0}

    with TemporaryDirectory(prefix="memory-guarantees-") as directory:
        async with _memory(Path(directory)) as (memory, learning, log, embeddings, records, consumer):
            # A private fact in a second session: it is real history for the same
            # person, so a user-scoped memory would happily return it.
            secret = _append(log, embeddings, learning, OTHER_SESSION, "other-user",
                             "user", Input, "另一个会话里的私密条件：体检结果 HBA1C=7.8，不要对外提起。")
            _append(log, embeddings, learning, OTHER_SESSION, "other-assistant",
                    "assistant", Output, "已记录。")
            stock.remember(secret.message_id, "另一个会话里的私密条件：体检结果 HBA1C=7.8，不要对外提起。")

            for number in range(seed_turns):
                text = (f"第 {number} 次讨论：场地、预算与时间安排。"
                        f"项目 {number} 的预算定为 BUDGET={700 + number}，负责人是第 {number} 组。")
                message = _append(log, embeddings, learning, SESSION, f"user-{number}",
                                  "user", Input, text)
                stock.remember(message.message_id, text)
                reply = f"第 {number} 次讨论已记录。"
                answer = _append(log, embeddings, learning, SESSION, f"assistant-{number}",
                                 "assistant", Output, reply)
                stock.remember(answer.message_id, reply)
            await memory.consume()

            inputs = log.writer(SESSION, author="user", source=SOURCE,
                                body_types=(Input,), content={"text": check_text})
            turns = []
            for turn in range(live_turns):
                query = f"项目 {turn % max(seed_turns, 1)} 的预算是多少？第 {turn} 次追问"
                message = inputs.append(f"live-{turn}", Input((ContentPart("text", query),)))
                stock.remember(message.message_id, query)
                snapshot = log.reader(SESSION).snapshot()
                material = await memory.prepare(snapshot, SOURCE)
                ours = "\n\n".join(item["text"] for item in material.get("reminders", ())
                                   if item.get("replay") is False)
                identity = context_identity(SESSION, SOURCE, message.message_id)
                receipt = records.read(identity)
                turns.append({"turn": turn, "query": query, "identity": identity,
                              "snapshot": snapshot, "ours": ours,
                              "stock": stock.render(query), "receipt": receipt,
                              "references": tuple(material.get("references", ())),
                              "window": {m.message_id for m in snapshot}})

                # Verbatim fidelity. Recall material is read from the stored
                # message body, never from a model-written summary of it, so every
                # passage must be a byte-exact slice of a real original. This is
                # what makes a character range citable at all: an extracted "fact"
                # has no offsets to point at, and nothing to re-read.
                # The outward reference protocol carries only stable identities;
                # the character ranges live on the receipt, which is the authority.
                for span in (receipt.presented_spans if receipt else ()):
                    hit = next((h for h in receipt.hits
                                if span.message_id in h.message_ids), None)
                    if hit is None:
                        continue
                    original = log.reader(hit.session_id).get(span.message_id)
                    if original is None:
                        continue
                    body = learning.text(original)
                    slice_ = body[span.start:span.end]
                    fidelity["passages"] += 1
                    fidelity["chars"] += len(slice_)
                    # Byte-exact, and the recorded length must match the real body,
                    # so a stale span cannot pass by pointing at the wrong message.
                    if slice_ and slice_ in ours and span.source_chars == len(body):
                        fidelity["verbatim"] += 1

            # 1. Reproducibility. Every turn is re-rendered now that the index has
            #    grown by every later turn, which is what a retry or a replay of an
            #    earlier request actually meets.
            for row in turns:
                reproducible["turns"] += 1
                again = await memory.prepare(row["snapshot"], SOURCE)
                ours_again = "\n\n".join(item["text"] for item in again.get("reminders", ())
                                         if item.get("replay") is False)
                if ours_again == row["ours"]:
                    reproducible["ours"] += 1
                stock_again = stock.render(row["query"])
                if stock_again == row["stock"]:
                    reproducible["stock"] += 1
                else:
                    reproducible["drift"].append({
                        "turn": row["turn"],
                        "chars_before": len(row["stock"]), "chars_after": len(stock_again),
                        "changed_chars": sum(1 for a, b in zip(row["stock"], stock_again,
                                                               strict=False) if a != b)
                                          + abs(len(row["stock"]) - len(stock_again))})

            # 2. Read confinement, exercised against a live receipt.
            queries = MemoryQueries(capture=lambda _: {}, open_recall=None,
                                    catalog=log.catalog(), records=lambda: records,
                                    check_scope=lambda session: (
                                        "default" if session == SESSION else "other"))
            for row in turns:
                receipt = row["receipt"]
                if receipt is None or not receipt.admitted_message_ids:
                    continue
                admitted = set(receipt.admitted_message_ids)
                outsider = next((m.message_id for m in row["snapshot"]
                                 if m.message_id not in admitted), None)
                attempts = [
                    ("admitted original (control, must be allowed)", SESSION,
                     row["identity"], receipt.admitted_message_ids[0], True),
                    ("original this retrieval never presented", SESSION,
                     row["identity"], outsider, False),
                    ("original from another memory scope", OTHER_SESSION,
                     row["identity"], secret.message_id, False),
                    ("forged retrieval reference", SESSION,
                     "depth:not-a-real-receipt", receipt.admitted_message_ids[0], False),
                ]
                for label, session, ref, target, must_allow in attempts:
                    if target is None:
                        continue
                    confinement["attempts"] += 1
                    try:
                        await queries.source(MemorySourceQuery(
                            session_id=session, retrieval_ref=ref,
                            message_id=target, offset=0, length=64))
                        allowed = True
                        reason = None
                    except Exception as error:  # noqa: BLE001 - the refusal is the result
                        allowed, reason = False, str(error)[:120]
                    if must_allow:
                        confinement["ours_allowed_control"] += int(allowed)
                    elif not allowed:
                        confinement["ours_refused"] += 1
                        confinement["refusals"].append({"attempt": label, "reason": reason})
                    # The same attempt against a store with no per-request receipt.
                    if not must_allow and stock.read_any(target) is None:
                        confinement["stock_refused"] += 1

            # 3. Attribution. Every receipt names what caused it: an automatic
            #    context recall pinned to a conversation prefix, a model tool call
            #    bound to its call reference, or a named program. This is what makes
            #    "which memories were pulled, and by what" answerable after the fact,
            #    and it is the honest form of the security claim: a model issuing a
            #    new query can still surface new in-scope originals -- that is what
            #    memory is for -- but it cannot do so anonymously.
            for row in turns:
                receipt = row["receipt"]
                if receipt is None:
                    continue
                attribution["receipts"] += 1
                origin = receipt.source
                kind = getattr(origin, "kind", None)
                attribution["kinds"][kind] = attribution["kinds"].get(kind, 0) + 1
                if kind in {"context", "tool", "program"}:
                    attribution["with_cause"] += 1
                if kind == "context" and origin.through_seq == row["snapshot"][-1].seq:
                    attribution["prefix_pinned"] += 1

            # 4. Repetition: originals the request already carries verbatim.
            for row in turns:
                repetition["turns"] += 1
                receipt = row["receipt"]
                if receipt is not None:
                    repetition["ours_duplicate_bodies"] += sum(
                        1 for ref in receipt.presented_message_ids if ref in row["window"])
                repetition["stock_duplicate_bodies"] += sum(
                    1 for ref in stock.search(row["query"]) if ref in row["window"])

    return {
        "kind": "design guarantees of receipt-based recall against a stock retrieval memory",
        "conditions": {"seed_turns": seed_turns, "live_turns": live_turns,
                       "chat_model": None, "api_key_required": False,
                       "embedding": f"fixed {DIMENSIONS}-dimensional unit vector per text",
                       "baseline": "top-k over every stored message, pasted in, no per-request record"},
        "reproducibility": {
            "ours_identical_after_index_growth": f"{reproducible['ours']}/{reproducible['turns']}",
            "stock_identical_after_index_growth": f"{reproducible['stock']}/{reproducible['turns']}",
            # The one axis where the baseline genuinely varies, so it is the one
            # that needs a test rather than a count: ours holds by construction,
            # the baseline's depends on how far the index moved. Paired, because
            # both arms answer the same turns.
            "paired_mcnemar_p": round(mcnemar_exact(
                reproducible["ours"] - min(reproducible["ours"], reproducible["stock"]),
                reproducible["stock"] - min(reproducible["ours"], reproducible["stock"])), 5),
            "stock_drift_examples": reproducible["drift"][:5],
            "note": ("其余各项是结构性性质而非抽样估计：分母表示检查了多少次，"
                     "每次都成立；出现一次反例即为缺陷，不是统计波动。"),
        },
        "read_confinement": {
            "illegal_attempts": confinement["attempts"] - len(turns),
            "ours_refused": confinement["ours_refused"],
            "stock_refused": confinement["stock_refused"],
            "ours_allowed_control": f"{confinement['ours_allowed_control']}/{len(turns)}",
            "refusal_reasons": sorted({row["attempt"] + ": " + (row["reason"] or "")
                                       for row in confinement["refusals"]}),
        },
        "attribution": {
            "ours_receipts_with_a_recorded_cause":
                f"{attribution['with_cause']}/{attribution['receipts']}",
            "ours_context_recalls_pinned_to_a_conversation_prefix":
                f"{attribution['prefix_pinned']}/{attribution['receipts']}",
            "cause_kinds": attribution["kinds"],
            "stock_receipts_with_a_recorded_cause": f"0/{attribution['receipts']}",
            "note": ("tool-initiated recalls carry the originating call reference "
                     "(plugins/akasha/recall_tool.py), so a memory the model pulled "
                     "itself is distinguishable from one the turn surfaced automatically"),
        },
        "verbatim_fidelity": {
            "passages_checked": fidelity["passages"],
            "byte_exact_slices_of_a_real_original": fidelity["verbatim"],
            "characters_covered": fidelity["chars"],
            "note": ("召回材料直接读取原消息正文（plugins/akasha/recalls.py 的"
                     "render_materials），不存储模型改写过的事实；因此每段材料都能"
                     "定位到真实原文的精确字符范围，并按同一回执重读同一段字节。"),
        },
        "repetition": {
            "ours_already_quoted_bodies_injected": repetition["ours_duplicate_bodies"],
            "stock_already_quoted_bodies_injected": repetition["stock_duplicate_bodies"],
            "turns": repetition["turns"],
        },
        "claim_limits": (
            "读取受限指的是『本次回执没有呈现过的原文读不到』，不等于模型无法接触更多历史："
            "模型发起一次新的召回查询，仍可在同一 scope 内浮出新的原文，这正是记忆的用途。"
            "可证的是跨 scope 的硬隔离、伪造 ref 与未呈现消息被拒，以及每次召回都带可追责的来源。"),
        "caveat": ("设计对照，不是检索质量对照：两边用同一批消息与同一组向量。"
                   "现成方案同样可以自行缓存每轮材料或维护每轮白名单；"
                   "这里衡量的是本项目已有数据结构直接给出的性质，以及它在索引变化后是否仍然成立。"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-turns", type=int, default=12)
    parser.add_argument("--live-turns", type=int, default=10)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = asyncio.run(measure(seed_turns=args.seed_turns, live_turns=args.live_turns))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
