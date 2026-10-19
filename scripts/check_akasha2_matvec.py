"""Finite mixed-precision/block-tail checks inspired by Akashic's cost report.

These are our declared public shapes, not the author's unpublished shape set.
The full NumPy operation is the independent reference; no vendor code is edited.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

import numpy as np

from plugins.akasha2.domain.features import _dense_product

DIMENSIONS = (
    1,
    2,
    3,
    7,
    8,
    15,
    16,
    17,
    31,
    32,
    33,
    63,
    64,
    65,
    127,
    128,
    129,
    255,
    256,
    257,
    384,
    768,
    1536,
    3072,
)
ROWS = (
    0,
    1,
    2,
    7,
    8,
    9,
    15,
    16,
    31,
    32,
    33,
    63,
    64,
    65,
    127,
    128,
    129,
    247,
    248,
    249,
    255,
    256,
    257,
    263,
    264,
    511,
    512,
    513,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("choose a new numeric experiment output")
    rng = np.random.default_rng(20261019)
    cases = []
    for dimension in DIMENSIONS:
        for count in ROWS:
            matrix = rng.normal(size=(count, dimension)).astype(np.float32)
            cue = rng.normal(size=dimension).astype(np.float64)
            expected = matrix @ cue
            actual = _dense_product(matrix, cue)
            cases.append(
                {
                    "rows": count,
                    "dimensions": dimension,
                    "bitwise_equal": actual.dtype == expected.dtype
                    and actual.tobytes() == expected.tobytes(),
                    "max_absolute_difference": float(np.max(np.abs(actual - expected)))
                    if count
                    else 0.0,
                }
            )
    source = Path("plugins/akasha2/domain/features.py")
    report = {
        "seed": 20261019,
        "python": sys.version,
        "numpy": np.__version__,
        "blas": np.__config__.show(mode="dicts"),
        "threads": 1,
        "matrix_dtype": "float32",
        "cue_dtype": "float64",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "passed": sum(row["bitwise_equal"] for row in cases),
        "total": len(cases),
        "cases": cases,
        "scope": "finite C-contiguous matrices on this NumPy/BLAS/platform, not universal floating-point equivalence",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "total": report["total"]}))
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
