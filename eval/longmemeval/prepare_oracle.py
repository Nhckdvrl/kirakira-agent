"""Diagnostic answer-span oracle. Uses labels intentionally, never a retrieval arm.

Only original complete user/assistant pairs bearing has_answer are shown to the
reader. Gold answer text is still reserved for the judge, as in normal evaluation.
"""

import argparse
import hashlib
import json
from pathlib import Path

from eval.longmemeval.dataset import load_longmemeval
from eval.longmemeval.offline_ab import _turn_groups
from eval.longmemeval.prepare_retrieval import bounded
from eval.longmemeval.protocol import chronological, select_fixed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--selection", type=Path)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError("oracle output must be new")
    rows = []
    for case in select_fixed(load_longmemeval(a.dataset), selection=a.selection):
        records = []
        for date, sid, messages in chronological(case):
            for group in _turn_groups(messages):
                if (
                    len(group) == 2
                    and [t.role for t in group] == ["user", "assistant"]
                    and any(t.has_answer for t in group)
                ):
                    records.append(
                        {
                            "id": sid + ":" + str(len(records)),
                            "source_session": sid,
                            "summary": f"[{date}] user: {group[0].content}\nassistant: {group[1].content}",
                        }
                    )
        context, kept = bounded(records)
        rows.append(
            {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "question": case.question,
                "question_date": case.question_date,
                "answer": case.answer,
                "abstention": case.question_id.endswith("_abs"),
                "arms": {
                    "oracle": {
                        "context": context,
                        "records": kept,
                        "label_access": "has_answer message annotations; diagnostic only",
                    }
                },
            }
        )
    a.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )
    a.output.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "dataset_sha256": hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "cases": len(rows),
                "context_chars": 24000,
                "max_turns": 10,
                "warning": "label-assisted answer-span oracle, not valid retrieval performance",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
