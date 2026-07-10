"""Session persistence and history boundary tests."""

import tempfile
import unittest
from pathlib import Path

from kirakira_agent.session import Session, SessionManager


class SessionTests(unittest.TestCase):
    def test_sanitized_key_collisions_use_distinct_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(Path(tmp))
            first = manager.get_or_create("qq:a/b")
            second = manager.get_or_create("qq:a:b")
            first.add_message("user", "first")
            second.add_message("user", "second")
            manager.save(first)
            manager.save(second)

            reloaded = SessionManager(Path(tmp))
            self.assertEqual(reloaded.get_or_create("qq:a/b").messages[0]["content"], "first")
            self.assertEqual(reloaded.get_or_create("qq:a:b").messages[0]["content"], "second")
            self.assertEqual(len(list((Path(tmp) / "sessions").glob("*.json"))), 2)

    def test_history_never_starts_from_orphan_assistant(self):
        session = Session("test")
        session.add_message("user", "old user")
        session.add_message("assistant", "orphan at window boundary")
        session.add_message("user", "new user")
        session.add_message("assistant", "new answer")

        history = session.get_history(max_messages=3)

        self.assertEqual(history[0], {"role": "user", "content": "new user"})

    def test_history_preserves_reasoning_for_tool_calls_and_legacy_assistant(self):
        session = Session("reasoning")
        session.add_message("user", "inspect")
        session.add_message(
            "assistant",
            "done",
            thinking="legacy final thought",
            tool_chain=[
                {
                    "text": "",
                    "reasoning_content": "tool thought",
                    "calls": [
                        {
                            "call_id": "c1",
                            "name": "read_file",
                            "arguments": {"path": "README.md"},
                            "result": "ok",
                        }
                    ],
                }
            ],
        )

        history = session.get_history()

        self.assertEqual(history[1]["reasoning_content"], "tool thought")
        self.assertEqual(history[-1]["reasoning_content"], "legacy final thought")

    def test_message_index_rebuilds_and_returns_fetchable_source_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manager = SessionManager(workspace)
            session = manager.get_or_create("web:search")
            session.add_message("user", "alpha searchable marker omega")
            session.add_message("assistant", "ack")
            manager.save(session)
            manager.close()

            reloaded = SessionManager(workspace)
            results = reloaded.search_messages("searchable marker")
            self.assertEqual(results[0]["source_ref"], "web:search:0")
            fetched = reloaded.fetch_messages(results[0]["source_ref"], context=0)
            self.assertEqual(fetched[0]["content"], "alpha searchable marker omega")
            reloaded.close()


if __name__ == "__main__":
    unittest.main()
