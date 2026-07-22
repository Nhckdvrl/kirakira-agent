"""Kirakira Agent learning harness module."""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_proactive_target_auto_enables_builtin_channel(self):
        from kirakira_agent.cli import build_runtime

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                config = workdir / "config.toml"
                config.write_text(
                    """
[llm.main]
model = "fake"
base_url = "http://example.test/v1"

[channels.chat]
channel_name = "push-web"

[proactive]
enabled = true

[proactive.target]
channel = "push-web"
chat_id = "u1"
""",
                    encoding="utf-8",
                )
                runtime = await build_runtime(workdir, config_path=config)
                try:
                    self.assertIsNotNone(runtime.channel_host)
                    self.assertIn(
                        "push-web",
                        [channel.name for channel in runtime.channel_host.channels],
                    )
                finally:
                    await runtime.stop_background([])

        asyncio.run(scenario())

    def test_cli_mode_selection(self):
        from kirakira_agent.cli import choose_cli_mode

        self.assertEqual(
            choose_cli_mode(
                stdin_isatty=True,
                stdout_isatty=True,
                textual_available=True,
            ),
            "tui",
        )
        self.assertEqual(
            choose_cli_mode(
                stdin_isatty=False,
                stdout_isatty=True,
                textual_available=True,
            ),
            "plain",
        )
        self.assertEqual(
            choose_cli_mode(
                force_plain=True,
                stdin_isatty=True,
                stdout_isatty=True,
                textual_available=True,
            ),
            "plain",
        )
        self.assertEqual(choose_cli_mode(force_tui=True), "tui")

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
