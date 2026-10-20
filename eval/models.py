"""Explicit model configuration shared by provider-independent evaluations."""

import asyncio
import fcntl
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kirakira_agent.runtime import configure_chat_model


def source_identity():
    """Hash installed implementation files, plus checkout provenance when present.

    Wheels contain runtime modules and license metadata, while the repository's
    root source manifest and patch review files belong to the source archive.
    """
    from kirakira_agent.runtime import ROOT

    names = ["kirakira_agent/runtime.py", "kirakira_agent/setup.py",
             "kirakira_agent/environment.py", "kirakira_agent/client.py",
             "eval/models.py", "plugins/akasha/contract.py", "plugins/akasha/plugin.py",
             "plugins/akasha/query.py", "plugins/akasha/recalls.py", "plugins/akasha/recall_tool.py",
             "plugins/akasha/runtime.py", "plugins/akasha/excerpts.py", "plugins/akasha/source_tool.py"]
    if (ROOT / "UPSTREAM.json").is_file():
        names.append("UPSTREAM.json")
    names.extend(str(path.relative_to(ROOT)) for path in sorted((ROOT / "patches").glob("*.patch")))
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


@asynccontextmanager
async def provider_slot(config, key_file: Path, *, slots: int = 6):
    """Share a provider concurrency budget across local evaluation processes."""
    identity = hashlib.sha256(config.endpoint.encode() + b"\0" + key_file.read_bytes().strip()).hexdigest()
    directory = Path("/var/tmp") / ("kirakira-eval-slots-" + str(os.getuid())) / identity
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    held = None
    try:
        while held is None:
            for number in range(slots):
                stream = (directory / str(number)).open("a")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                else:
                    held = stream
                    break
            if held is None:
                await asyncio.sleep(0.2)
        yield
    finally:
        if held is not None:
            held.close()


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    driver_id: str = "openai-compatible"
    model_id: str = "evaluation"
    context_window: int = Field(default=32768, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    reasoning_effort: str | None = None

    @classmethod
    def read(cls, path: Path | None):
        if path is not None:
            return cls.model_validate_json(path.read_text())
        # Compatibility with existing private subscription test commands.
        from plugins.step_plan.driver import BASE_URL, DRIVER_ID, MODEL
        return cls(model=MODEL, endpoint=BASE_URL, driver_id=DRIVER_ID,
                   reasoning_effort="low")

    async def configure(self, core, key_file):
        await configure_chat_model(core, key_file, **self.model_dump(),
                                   connection_id=self.model_id)

    def description(self):
        return json.loads(self.model_dump_json())
