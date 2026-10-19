"""Frozen synthetic episodic tasks: relations, exact details, updates and abstention.

This is a targeted capability suite, not a representative public benchmark.
All controls receive the same histories. Answers never enter indexing/embedding.
"""

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAMILIES = (
    "assistant_relation",
    "exact_identifier",
    "temporal_update",
    "cross_session_join",
    "user_preference",
    "unanswerable",
)


def build(seed=2026101902, repetitions=4):
    rng = random.Random(seed)
    cases = []
    for family in FAMILIES:
        for repetition in range(repetitions):
            identity = hashlib.sha256(
                f"{seed}:{family}:{repetition}".encode()
            ).hexdigest()[:16]
            qid = "e-" + identity + ("_abs" if family == "unanswerable" else "")
            project = (
                "project "
                + rng.choice(["Aster", "Boreal", "Cobalt", "Dahlia"])
                + " "
                + str(rng.randrange(100, 999))
            )
            owner = (
                rng.choice(["Mira", "Nolan", "Priya", "Soren"])
                + " "
                + str(rng.randrange(100, 999))
            )
            code = lambda: (
                "K" + "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=9))
            )
            old, current, alternative = code(), code(), code()
            pairs = []
            topics = [
                (
                    "I like cycling along the river on weekends.",
                    "A flat riverside path suits relaxed cycling.",
                ),
                (
                    "My basil seedlings need a bigger pot.",
                    "Use drainage holes and avoid overwatering.",
                ),
                (
                    "How do I organize workshop receipts?",
                    "Group them by date and supplier.",
                ),
                (
                    "I am comparing quiet keyboards for work.",
                    "Consider switch noise and desk acoustics.",
                ),
                (
                    "I would like to learn watercolor painting.",
                    "Start with simple washes and a small color palette.",
                ),
                (
                    "My walking route crosses a small park.",
                    "A park loop can make the route more pleasant.",
                ),
            ]
            for index in range(48):
                user, assistant = rng.choice(topics)
                pairs.append([user + f" This is note {index + 1}.", assistant, False])
            first, second = rng.randrange(4, 12), rng.randrange(32, 44)
            if family == "assistant_relation":
                pairs[first] = [
                    f"Which adapter should I use for {project}, and which one for its sandbox?",
                    f"For {project}, use adapter {current}. Its sandbox uses {alternative}; these adapters are not interchangeable.",
                    True,
                ]
                question = f"Which exact adapter did you recommend for {project}, rather than its sandbox?"
                answer = [current]
            elif family == "exact_identifier":
                pairs[first] = [
                    f"The deployment label I assigned to {project} is {current}. Preserve its exact spelling.",
                    f"Recorded the deployment label for {project} exactly as supplied.",
                    True,
                ]
                question, answer = (
                    f"What is the exact deployment label I assigned to {project}?",
                    [current],
                )
            elif family == "temporal_update":
                pairs[first] = [
                    f"The deployment label for {project} is {old}.",
                    "Recorded the original deployment label.",
                    True,
                ]
                pairs[second] = [
                    f"Correction: I retired {old}. The current deployment label for {project} is now {current}.",
                    "The corrected label supersedes the retired one; the earlier record remains historical.",
                    True,
                ]
                question, answer = (
                    f"What is the current deployment label for {project} after my correction?",
                    [current],
                )
            elif family == "cross_session_join":
                pairs[first] = [
                    f"I assigned {project} to researcher {owner}.",
                    "Recorded the project assignment.",
                    True,
                ]
                pairs[second] = [
                    f"Researcher {owner} published their artifact under bundle label {current}.",
                    "Recorded the researcher and bundle label.",
                    True,
                ]
                question, answer = (
                    f"Which bundle label belongs to the researcher assigned to {project}?",
                    [current],
                )
            elif family == "user_preference":
                preferred = rng.choice(
                    [
                        "roasted oolong",
                        "unsweetened black tea",
                        "mint infusion",
                        "decaffeinated coffee",
                    ]
                )
                pairs[first] = [
                    f"For long work sessions on {project}, my preferred drink is {preferred}. I avoid sugary drinks.",
                    "I will use your stated drink preference when suggesting refreshments.",
                    True,
                ]
                question, answer = (
                    f"What drink do I prefer for long work sessions on {project}?",
                    [preferred],
                )
            else:
                pairs[first] = [
                    f"I worked on documentation for {project}; we have not assigned it a recovery label.",
                    "The documentation discussion does not establish any recovery label.",
                    False,
                ]
                question, answer = (
                    f"What exact recovery label did we assign to {project}?",
                    None,
                )
            sessions, dates, ids, gold = [], [], [], []
            for session_index in range(12):
                sid = (
                    "s-"
                    + hashlib.sha256(
                        f"{identity}:{session_index}".encode()
                    ).hexdigest()[:16]
                )
                ids.append(sid)
                stamp = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(
                    days=31 * session_index
                )
                dates.append(stamp.isoformat())
                messages = []
                for user, assistant, marked in pairs[
                    4 * session_index : 4 * session_index + 4
                ]:
                    messages.extend(
                        [
                            {"role": "user", "content": user, "has_answer": marked},
                            {
                                "role": "assistant",
                                "content": assistant,
                                "has_answer": marked,
                            },
                        ]
                    )
                    if marked and sid not in gold:
                        gold.append(sid)
                sessions.append(messages)
            cases.append(
                {
                    "question_id": qid,
                    "question_type": {
                        "assistant_relation": "single-session-assistant",
                        "exact_identifier": "single-session-user",
                        "temporal_update": "knowledge-update",
                        "cross_session_join": "multi-session",
                        "user_preference": "single-session-preference",
                        "unanswerable": "single-session-user",
                    }[family],
                    "family": family,
                    "question": question,
                    "answer": json.dumps(answer),
                    "question_date": "2025-01-01T00:00:00+00:00",
                    "expected": answer,
                    "haystack_session_ids": ids,
                    "haystack_dates": dates,
                    "haystack_sessions": sessions,
                    "answer_session_ids": gold,
                }
            )
    return cases


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    data = a.output / "dataset.json"
    data.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
    selection = {
        "case_ids": [row["question_id"] for row in build()],
        "rule": "all 24 prespecified generated cases, 4 per family, seed 2026101902",
    }
    (a.output / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    protocol = {
        "dataset_id": "kirakira-episodic-contract-v1",
        "seed": 2026101902,
        "dataset_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "frozen_before_model_calls": True,
        "families": FAMILIES,
        "metric": "exact JSON answer (case-sensitive strings), all errors count incorrect; no model judge",
        "scope": "targeted synthetic capability suite; not LongMemEval or population accuracy",
        "budget": {
            "max_pairs_or_facts": 10,
            "max_chars": 24000,
            "reader_max_tokens": 8192,
        },
        "controls": [
            "no_memory",
            "recency",
            "dense",
            "bm25",
            "rrf",
            "akasha2",
            "mem0_oss",
        ],
        "prohibition": "no answer-dependent selection, tuning, replacement or best-run score mixing",
    }
    (a.output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    print(json.dumps({"cases": 24, "dataset_sha256": protocol["dataset_sha256"]}))


if __name__ == "__main__":
    main()
