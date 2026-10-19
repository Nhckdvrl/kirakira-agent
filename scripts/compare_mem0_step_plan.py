"""Real Mem0 OSS comparison: local Qdrant/BGE, Step5 subscription transport.

Mem0 owns extraction, updates and search. Only its HTTP client and embedding
query prefix are adapted. The protocol uses session-batch ingestion and facts,
not Kirakira's original-pair readout; record that distinction when comparing.
"""

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

from agent.model_runtime.types import RetryableModelError
from eval.longmemeval.dataset import load_longmemeval
from eval.longmemeval.protocol import chronological, select_fixed
from infra.providers.step_plan import STEP5_MODEL, STEP_PLAN_BASE_URL, StepPlanClient


def conversation_inputs(batch):
    """Only original text and dates reach extraction; fixture IDs stay outside."""
    inputs = []
    for date, _identity, messages in batch:
        block = [{"role": turn.role, "content": turn.content} for turn in messages]
        if block:
            block[0] = {
                **block[0],
                "content": f"[Conversation date: {date}]\n" + block[0]["content"],
            }
        inputs.extend(block)
    return inputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sessions-per-add", type=int, default=8)
    parser.add_argument("--ingest-max-tokens", type=int, default=32768)
    args = parser.parse_args()
    if min(args.workers, args.sessions_per_add, args.ingest_max_tokens) < 1:
        parser.error("workers, sessions-per-add and ingest-max-tokens must be positive")
    if args.output.exists():
        raise FileExistsError("use a new comparison output")
    args.workspace.mkdir(parents=True, exist_ok=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        MEM0_TELEMETRY="false",
        MEM0_DIR=str(args.workspace / "metadata"),
        FASTEMBED_CACHE_PATH=str(args.cache),
    )
    os.environ.pop("OPENROUTER_API_KEY", None)
    from fastembed import TextEmbedding
    from mem0 import Memory
    from mem0.llms.openai import OpenAILLM
    from openai.types.chat import ChatCompletion

    cases = select_fixed(load_longmemeval(args.dataset), selection=args.selection)
    client = StepPlanClient(api_key=args.key_file.read_text().strip(), timeout=360)

    class Transport:
        def __init__(self, work, blocked_ids):
            self.work = work
            self.blocked_ids = tuple(
                identity for identity in blocked_ids if len(identity) >= 8
            )
            self.requests = []
            self.failed_calls = 0

        def create(self, **params):
            if params.get("model") != STEP5_MODEL:
                raise ValueError("Mem0 attempted another model")
            messages = json.dumps(params["messages"], ensure_ascii=False)
            if any(identity in messages for identity in self.blocked_ids):
                self.failed_calls += 1
                raise ValueError("label-bearing fixture ID reached the model input")
            payload = {
                **params,
                "reasoning_effort": "medium",
                "max_tokens": args.ingest_max_tokens,
            }
            payload.pop("max_completion_tokens", None)
            for attempt in range(5):
                row = {"status": "started", "attempt": attempt + 1}
                self.requests.append(row)
                started = time.monotonic()
                try:
                    with client._open(payload) as response:
                        raw = json.load(response)
                    row.update(status="completed", usage=raw.get("usage", {}))
                    if raw["choices"][0]["finish_reason"] == "length":
                        raise ValueError("Mem0 extraction was truncated")
                    return ChatCompletion.model_validate(raw)
                except RetryableModelError as error:
                    row.update(
                        status=type(error).__name__, http_status=error.status_code
                    )
                    if attempt == 4:
                        self.failed_calls += 1
                        raise
                    time.sleep(
                        min(30.0, max(float(error.retry_after or 0), 2**attempt))
                    )
                except Exception as error:
                    self.failed_calls += 1
                    row.update(status=type(error).__name__)
                    raise
                finally:
                    row["seconds"] = time.monotonic() - started
                    with (self.work / "requests.jsonl").open("a") as journal:
                        journal.write(json.dumps(row) + "\n")

    manifest = {
        "system": "Mem0 OSS",
        "version": importlib.metadata.version("mem0ai"),
        "llm": STEP5_MODEL,
        "endpoint": STEP_PLAN_BASE_URL,
        "billing_surface": "step_plan",
        "reasoning_effort": "medium",
        "max_tokens": args.ingest_max_tokens,
        "telemetry": False,
        "optional_nlp_extra": False,
        "fixture_identifier_guard": True,
        "embedding": "BAAI/bge-small-en-v1.5, 384 dims, 512 truncation, query prefix",
        "vector_store": "local Qdrant",
        "ingest_unit": f"fixed batches of {args.sessions_per_add} complete chronological source sessions per add(infer=True)",
        "readout_unit": "Mem0 extracted facts, top_k=10, threshold=0.1, max 24000 chars",
        "cases": [case.question_id for case in cases],
        "workers": args.workers,
        "selection_sha256": hashlib.sha256(args.selection.read_bytes()).hexdigest(),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "memory_source_sha256": hashlib.sha256(
            Path(inspect.getfile(Memory)).read_bytes()
        ).hexdigest(),
        "llm_source_sha256": hashlib.sha256(
            Path(inspect.getfile(OpenAILLM)).read_bytes()
        ).hexdigest(),
        "limitations": [
            "OSS, not hosted Mem0 Platform; temporal API parameters unavailable",
            "session-batch extraction vs original complete pairs is an architectural difference",
            "small question-ID subset, no superiority claim from a small sample",
        ],
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))

    def case_run(case):
        work = args.workspace / case.question_id
        work.mkdir()
        transport = Transport(work, [case.question_id, *case.haystack_session_ids])
        started = time.monotonic()
        row = {
            "question_id": case.question_id,
            "question_type": case.question_type,
            "status": "error",
        }
        memory = None
        native_embed = None
        try:
            memory = Memory.from_config(
                {
                    "llm": {
                        "provider": "openai",
                        "config": {
                            "model": STEP5_MODEL,
                            "openai_base_url": STEP_PLAN_BASE_URL,
                            "api_key": "credential-owned-by-subscription-transport",
                            "reasoning_effort": "medium",
                            "max_tokens": args.ingest_max_tokens,
                        },
                    },
                    "embedder": {
                        "provider": "fastembed",
                        "config": {
                            "model": "BAAI/bge-small-en-v1.5",
                            "embedding_dims": 384,
                        },
                    },
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": "memories",
                            "embedding_model_dims": 384,
                            "path": str(work / "qdrant"),
                            "on_disk": True,
                        },
                    },
                    "history_db_path": str(work / "history.db"),
                }
            )
            memory.llm.client.close()
            memory.llm.client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=transport.create)
                )
            )
            memory.embedding_model.dense_model = TextEmbedding(
                "BAAI/bge-small-en-v1.5", cache_dir=str(args.cache), threads=1
            )
            native_embed = memory.embedding_model.embed

            def embed(text, memory_action=None):
                if memory_action == "search":
                    text = (
                        "Represent this sentence for searching relevant passages: "
                        + text
                    )
                return native_embed(text, memory_action=memory_action)

            memory.embedding_model.embed = embed
            session_results = []
            history = chronological(case)
            for offset in range(0, len(history), args.sessions_per_add):
                batch = history[offset : offset + args.sessions_per_add]
                inputs = conversation_inputs(batch)
                dates, identities = (
                    [item[0] for item in batch],
                    [item[1] for item in batch],
                )
                result = memory.add(
                    inputs,
                    user_id=case.question_id,
                    metadata={
                        "source_sessions": identities,
                        "source_date": dates[0] + " through " + dates[-1],
                    },
                )
                session_results.append(
                    {
                        "session_ids": identities,
                        "events": Counter(
                            record.get("event", "unknown")
                            for record in result.get("results", [])
                        ),
                    }
                )
                progress = {
                    "question_id": case.question_id,
                    "processed_sessions": offset + len(batch),
                    "total_sessions": len(history),
                    "requests": len(transport.requests),
                }
                (work / "progress.json").write_text(json.dumps(progress))
                print(json.dumps({**progress, "phase": "ingest"}), flush=True)
            index_seconds = time.monotonic() - started
            query_started = time.monotonic()
            result = memory.search(
                case.question,
                top_k=10,
                filters={"user_id": case.question_id},
                threshold=0.1,
            )
            query_seconds = time.monotonic() - query_started
            records, context = [], ""
            for hit in result.get("results", []):
                metadata = hit.get("metadata") or {}
                block = f"[{metadata.get('source_date', hit.get('created_at', ''))}] memory: {hit['memory']}\n"
                if len(context) + len(block) > 24000:
                    break
                context += block
                records.append(
                    {
                        "id": hit["id"],
                        "summary": block,
                        "source_session": metadata.get("source_session"),
                        "source_sessions": metadata.get("source_sessions", []),
                        "score": hit.get("score"),
                    }
                )
            row.update(
                status="error" if transport.failed_calls else "ok",
                unrecovered_model_failures=transport.failed_calls,
                ingest_seconds=index_seconds,
                query_seconds=query_seconds,
                sessions=session_results,
                memory_records=len(records),
                context=context,
                records=records,
            )
        except Exception as error:
            row.update(error=type(error).__name__)
        finally:
            if memory is not None:
                if native_embed is not None:
                    memory.embedding_model.embed = native_embed
                if memory._entity_store is not None:
                    memory._entity_store.client.close()
                memory.vector_store.client.close()
                memory.db.close()
        row.update(seconds=time.monotonic() - started, requests=transport.requests)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for future in as_completed(
            {executor.submit(case_run, case): case for case in cases}
        ):
            row = future.result()
            with args.output.open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                json.dumps(
                    {
                        "question_id": row["question_id"],
                        "status": row["status"],
                        "requests": len(row["requests"]),
                    }
                ),
                flush=True,
            )
    rows = {
        row["question_id"]: row
        for row in map(json.loads, args.output.read_text().splitlines())
    }
    retrieval = []
    for case in cases:
        row = rows[case.question_id]
        retrieval.append(
            {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "question": case.question,
                "question_date": case.question_date,
                "answer": case.answer,
                "abstention": case.question_id.endswith("_abs"),
                "arms": {
                    "mem0_oss": {
                        "context": row.get("context", ""),
                        "records": row.get("records", []),
                        "ingestion_status": row["status"],
                    }
                },
            }
        )
    args.output.with_suffix(".retrieval.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in retrieval)
    )
    if any(row["status"] != "ok" for row in rows.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
