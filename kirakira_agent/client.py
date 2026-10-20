"""Public client names for Kirakira's versioned runtime protocol."""

from pathlib import Path

from akashic_sdk import Akashic as Client
from akashic_sdk import AsyncAkashic, RemoteError

from plugins.akasha.contract import (
    MemoryQuery,
    MemoryReadout,
    MemorySourceQuery,
    MemorySourceReadout,
)


class AsyncClient(AsyncAkashic):
    async def memory_source(self, request: MemorySourceQuery) -> MemorySourceReadout:
        """Read the original text behind a previously presented memory source."""
        import json

        result = await self.request("memory/source", request.model_dump(mode="json"))
        return MemorySourceReadout.model_validate_json(json.dumps(result))

    async def memory_recall(self, request: MemoryQuery) -> MemoryReadout:
        """Read bounded historical evidence with a durable retry identity."""
        import json

        result = await self.request("memory/recall", request.model_dump(mode="json"))
        return MemoryReadout.model_validate_json(json.dumps(result))

    @classmethod
    async def for_workspace(cls, workspace: str | Path):
        """Connect to the workspace's private local control socket."""
        return await cls.connect(str(Path(workspace).expanduser() / "akashic.sock"))


__all__ = ["AsyncClient", "Client", "RemoteError"]
