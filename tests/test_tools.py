import tempfile
import unittest
from pathlib import Path

from kirakira_agent.schema import ToolCall, ToolSpec
from kirakira_agent.skills import SkillLoader
from kirakira_agent.tools.builtins import WorkspaceTools, safe_path
from kirakira_agent.tools.registry import ToolRegistry


class ToolTests(unittest.TestCase):
    def test_safe_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                safe_path(Path(tmp), "../outside.txt")

    def test_file_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            tools = WorkspaceTools(workdir, SkillLoader(workdir / "skills"))

            self.assertIn("Wrote", tools.write_file("a.txt", "hello\nworld"))
            self.assertEqual(tools.read_file("a.txt"), "hello\nworld")
            self.assertIn("Edited", tools.edit_file("a.txt", "world", "kirakira"))
            self.assertEqual((workdir / "a.txt").read_text(), "hello\nkirakira")

    def test_registry_executes_and_handles_unknown_tool(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec("echo", "Echo text", {"type": "object", "properties": {}, "required": []}),
            lambda text: text,
        )
        ok = registry.execute(ToolCall("1", "echo", {"text": "hi"}))
        missing = registry.execute(ToolCall("2", "missing", {}))

        self.assertEqual(ok.content, "hi")
        self.assertTrue(missing.is_error)
        self.assertIn("Unknown tool", missing.content)


if __name__ == "__main__":
    unittest.main()
