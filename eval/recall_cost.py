"""Deterministic measurement of what automatic recall injects per turn.

No chat model is involved. Fixed embeddings and real memory components replay
consecutive committed turns through the production `MessageMemory.prepare`
path, then record, for every turn, how many characters the live recall
reminders carry and which original messages they carried.

Three facts are separate here and reported separately: the characters injected
into one request, how often the same original message is injected again in a
later turn while it is still in the window, and how many distinct originals the
material covered. Cost is not quality: this module measures cost only, and a
cheaper injection is not evidence of an equally useful one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.plugin_composition import CompositionRoot, Context
from plugins.akasha.application.consumer import MessageConsumer
from plugins.akasha.domain.model import MemoryConfig
from plugins.akasha.learning import AKASHA_LEARNING, Learning, LearningConfig
from plugins.akasha.recalls import RecallRecords, context_identity
from plugins.akasha.runtime import MessageMemory
from plugins.content.api import legacy_post_commit_effect
from plugins.content.plugin import check_text
from plugins.ledger.bindings import Bindings
from plugins.ledger.contract import ContentPart, Input, Output
from plugins.ledger.embedding_store import MessageEmbeddings
from plugins.ledger.log import MessageLog, SessionAttributes
from plugins.turn_projection.plugin import TurnProjection

SESSION = "conversation"
SOURCE = "conversation"
FILLER = "Background notes about the schedule, the venue and the people involved. "


@dataclass
class TurnCost:
    turn: int
    live_chars: int
    pointer_chars: int
    body_refs: tuple[str, ...]
    index_refs: tuple[str, ...]
    withheld_refs: tuple[str, ...] = ()

    @property
    def refs(self) -> tuple[str, ...]:
        """Every original this turn surfaced, as a body or as an index line."""
        return self.body_refs + self.index_refs


@dataclass
class Measurement:
    seed_turns: int
    live_turns: int
    filler_chars: int
    turns: list[TurnCost] = field(default_factory=list)

    def report(self) -> dict[str, object]:
        injected: Counter[str] = Counter()
        for row in self.turns:
            injected.update(row.refs)
        live = [row.live_chars for row in self.turns]
        total = sum(live)
        repeats = {ref: count for ref, count in injected.items() if count > 1}
        return {
            "conditions": {
                "seed_turns": self.seed_turns,
                "live_turns": self.live_turns,
                "filler_chars_per_seed_message": self.filler_chars,
                "chat_model": None,
                "embedding": "fixed 3-dimensional unit vector",
            },
            "injection": {
                "total_live_chars": total,
                "mean_live_chars_per_turn": round(total / len(live), 1) if live else 0,
                "max_live_chars_in_one_turn": max(live, default=0),
                "mean_pointer_chars_per_turn": (
                    round(sum(row.pointer_chars for row in self.turns) / len(live), 1) if live else 0
                ),
            },
            "repetition": {
                "distinct_messages_injected": len(injected),
                "messages_injected_more_than_once": len(repeats),
                "max_injections_of_one_message": max(injected.values(), default=0),
                "most_repeated": sorted(repeats.items(), key=lambda item: (-item[1], item[0]))[:8],
            },
            "form": {
                "mean_body_sources_per_turn": (
                    round(sum(len(row.body_refs) for row in self.turns) / len(live), 2) if live else 0),
                "mean_index_sources_per_turn": (
                    round(sum(len(row.index_refs) for row in self.turns) / len(live), 2) if live else 0),
                "mean_withheld_sources_per_turn": (
                    round(sum(len(row.withheld_refs) for row in self.turns) / len(live), 2) if live else 0),
            },
            "per_turn": [
                {"turn": row.turn, "live_chars": row.live_chars,
                 "pointer_chars": row.pointer_chars,
                 "body_refs": list(row.body_refs), "index_refs": list(row.index_refs)}
                for row in self.turns
            ],
        }


@asynccontextmanager
async def _memory(path: Path) -> AsyncIterator[tuple[MessageMemory, Learning, MessageLog, MessageEmbeddings, RecallRecords]]:
    """Real log, bindings, learning and on-disk writer; only embeddings are fixed."""
    log = MessageLog(path / "sessions.db")
    log.ensure_session(SESSION, SessionAttributes())
    embeddings = MessageEmbeddings(log, log._path.with_name("sessions-derived.db"))
    root = CompositionRoot("recall-cost")
    learning = Learning(TurnProjection(), owner="akasha", post_commit_effect=legacy_post_commit_effect)
    bindings = Bindings(log, root.context)
    rule = LearningConfig(embedding_model="fixed", dimension=3, sources=(SOURCE,))
    identity = "fixed-learning"
    log.save_binding(identity, {"version": 1, "service": AKASHA_LEARNING.name,
                                "root_ref": "recall-cost-archive", "metadata": rule.model_dump()})

    async def provide(ctx: Context) -> None:
        await ctx.provide(AKASHA_LEARNING, learning)

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

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
        yield memory, learning, log, embeddings, records
    finally:
        await memory.close()
        await root.dispose()
        embeddings.close()
        log.close()


def _seed(log: MessageLog, embeddings: MessageEmbeddings, learning: Learning,
          number: int, filler_chars: int) -> None:
    filler = (FILLER * (filler_chars // len(FILLER) + 1))[:filler_chars]
    for author, kind, body in (
        ("user", Input, Input((ContentPart("text", f"{filler} episode {number} budget BUDGET={700 + number}."),))),
        ("assistant", Output, Output((ContentPart("text", f"episode {number} is settled"),), "complete")),
    ):
        message = log.writer(SESSION, author=author, source=SOURCE, body_types=(kind,),
                             content={"text": check_text}).append(f"{author}-{number}", body)
        embeddings.bind(learning.text).save(message, model="fixed", embedding=[1.0, 0.0, 0.0])


async def measure(*, seed_turns: int, live_turns: int, filler_chars: int) -> Measurement:
    result = Measurement(seed_turns=seed_turns, live_turns=live_turns, filler_chars=filler_chars)
    with TemporaryDirectory(prefix="recall-cost-") as directory:
        async with _memory(Path(directory)) as (memory, learning, log, embeddings, records):
            for number in range(seed_turns):
                _seed(log, embeddings, learning, number, filler_chars)
            await memory.consume()
            inputs = log.writer(SESSION, author="user", source=SOURCE,
                                body_types=(Input,), content={"text": check_text})
            for turn in range(live_turns):
                message = inputs.append(
                    f"live-{turn}",
                    Input((ContentPart("text", f"what was the project budget? ask {turn}"),)))
                snapshot = log.reader(SESSION).snapshot()
                material = await memory.prepare(snapshot, SOURCE)
                reminders = material.get("reminders", ())
                live = [item for item in reminders if item.get("replay") is False]
                pointer = [item for item in reminders if item.get("replay", True)]
                # The receipt, not the rendered text, is the authority on which
                # originals this query surfaced and in which form.
                receipt = records.read(context_identity(SESSION, SOURCE, message.message_id))
                result.turns.append(TurnCost(
                    turn=turn,
                    live_chars=sum(len(item["text"]) for item in live),
                    pointer_chars=sum(len(item["text"]) for item in pointer),
                    body_refs=() if receipt is None else tuple(receipt.presented_message_ids),
                    index_refs=() if receipt is None else tuple(receipt.indexed_message_ids),
                    withheld_refs=() if receipt is None else tuple(receipt.withheld_message_ids),
                ))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-turns", type=int, default=12)
    parser.add_argument("--live-turns", type=int, default=15)
    parser.add_argument("--filler-chars", type=int, default=2000)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = asyncio.run(measure(seed_turns=args.seed_turns, live_turns=args.live_turns,
                                 filler_chars=args.filler_chars)).report()
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
