from core.memory.plugin import MemoryPluginRuntime

from .engine import Akasha2MemoryEngine


class MemoryPlugin:
    plugin_id = "akasha2"

    def ensure_workspace_storage(self, *, config, workspace):
        path = workspace / "memory" / "akasha2.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A checkpoint is created only with a bound source stream and embedder.
        return []

    def build(self, deps):
        engine = Akasha2MemoryEngine(
            config=deps.config,
            workspace=deps.workspace,
            http_resources=deps.http_resources,
            event_publisher=deps.event_publisher,
        )
        return MemoryPluginRuntime(
            engine=engine, closeables=[engine], embedding_api=engine.embedding_api
        )
