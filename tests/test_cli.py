"""Kirakira Agent learning harness module."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_cli_reports_missing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("MODEL_ID", None)
            env.pop("OPENAI_COMPATIBLE_BASE_URL", None)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            proc = subprocess.run(
                [sys.executable, "-m", "kirakira_agent"],
                input="/exit\n",
                text=True,
                capture_output=True,
                cwd=tmp,
                env=env,
                timeout=10,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MODEL_ID", proc.stderr + proc.stdout)

    def test_cli_tools_command_starts_and_exits(self):
        env = os.environ.copy()
        env["MODEL_ID"] = "fake"
        env["OPENAI_COMPATIBLE_BASE_URL"] = "http://example.test/v1"
        env["OPENAI_COMPATIBLE_API_KEY"] = ""
        proc = subprocess.run(
            [sys.executable, "-m", "kirakira_agent"],
            input="/tools\n/exit\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("bash", proc.stdout)
        self.assertIn("read_file", proc.stdout)


if __name__ == "__main__":
    unittest.main()
