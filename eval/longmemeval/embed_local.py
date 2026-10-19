"""Optional CPU embedding preparation. Requires fastembed in a separate env.

python -m eval.longmemeval.embed_local --dataset ... --output ...
No API credentials or hosted embedding service are used.
"""

import argparse
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from fastembed import TextEmbedding
from .dataset import load_longmemeval
from .protocol import select


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--per-type", type=int, default=6)
    p.add_argument("--cache", type=Path, required=True)
    a = p.parse_args()
    cases = select(load_longmemeval(a.dataset), a.per_type)
    texts = {
        turn.content
        for case in cases
        for session in case.haystack_sessions
        for turn in session
        if turn.role in {"user", "assistant"}
    }
    keys = sorted("p:" + t for t in texts) + ["q:" + case.question for case in cases]
    inputs = [
        key[2:]
        if key.startswith("p:")
        else "Represent this sentence for searching relevant passages: " + key[2:]
        for key in keys
    ]
    # Length bucketing reduces padding work without changing truncation or vectors.
    ordered = sorted(
        zip(keys, inputs, strict=True), key=lambda row: (len(row[1]), row[0])
    )
    keys, inputs = map(list, zip(*ordered, strict=True))
    start = time.monotonic()
    model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=str(a.cache), threads=4)
    vectors = []
    for i, vector in enumerate(model.embed(inputs, batch_size=16)):
        vectors.append(vector)
        if (i + 1) % 512 == 0:
            print(json.dumps({"embedded": i + 1, "total": len(keys)}), flush=True)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        a.output, vectors=np.asarray(vectors, dtype=np.float32), keys=np.asarray(keys)
    )
    # Pin downloaded model files as well as dataset and selection.
    hashes = {
        str(f.relative_to(a.cache)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in a.cache.rglob("*")
        if f.is_file() and f.stat().st_size > 0 and "/.cache/" not in str(f)
    }
    a.output.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": "BAAI/bge-small-en-v1.5",
                "runtime": "fastembed/ONNX CPU",
                "max_length": 512,
                "query_prefix": "Represent this sentence for searching relevant passages: ",
                "case_ids": [c.question_id for c in cases],
                "dataset_sha256": hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
                "model_files": hashes,
                "vectors": len(vectors),
                "seconds": time.monotonic() - start,
            },
            indent=2,
        )
    )
    print("complete", len(vectors), flush=True)


if __name__ == "__main__":
    main()
