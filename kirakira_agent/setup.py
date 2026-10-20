"""Explicit operator configuration through native Models and plugin inputs."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from kirakira_agent.runtime import rpc


def prepare_embedding(cache, model, *, download=False):
    from fastembed import TextEmbedding

    from plugins.local_embedding.driver import sha256

    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    supported = next(
        (row for row in TextEmbedding.list_supported_models() if row["model"] == model),
        None,
    )
    if supported is None:
        raise ValueError("embedding model is not in the pinned FastEmbed registry")
    encoder = TextEmbedding(
        model,
        cache_dir=str(cache),
        threads=2,
        lazy_load=True,
        local_files_only=not download,
        providers=["CPUExecutionProvider"],
    )
    # FastEmbed 0.9 owns download layout. Only setup reads this pinned-version
    # path; the runtime consumes a hash-verified manifest and never downloads.
    directory = Path(encoder.model._model_dir).resolve()
    files = {
        str(path.relative_to(directory)): sha256(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and ".cache" not in path.relative_to(directory).parts
    }
    manifest = cache / ("embedding-" + model.replace("/", "--") + ".json")
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "model": model,
                "dimensions": supported["dim"],
                "directory": str(directory),
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )
    return manifest


async def configure_embedding(core, manifest):
    from agent.plugin_composition.config_input import load_config, save_config
    from plugins.local_embedding.driver import DRIVER_ID, sha256
    from plugins.models.contract import EMBEDDINGS, MODEL_CATALOG

    manifest = Path(manifest).resolve()
    assets = json.loads(manifest.read_text())
    catalog = core.plugin_manager.live_root.context.require(MODEL_CATALOG)
    snapshot = catalog.snapshot()
    if any(model.model_id == "local-memory" for model in snapshot.models):
        existing = snapshot.model("local-memory")
        if (
            existing.model != assets["model"]
            or snapshot.connection(existing.connection_id).driver_id != DRIVER_ID
        ):
            raise ValueError("local-memory already identifies another vector space")
        if snapshot.default_embedding_model_id != "local-memory":
            await rpc(
                core,
                "models/command",
                {
                    "type": "set_default",
                    "role": None,
                    "expected_revision": snapshot.revision,
                    "model_id": "local-memory",
                },
            )
    else:
        await rpc(
            core,
            "models/command",
            {
                "type": "create_connection_with_model",
                "connection": {
                    "expected_revision": snapshot.revision,
                    "connection_id": "local-memory",
                    "name": "Local memory embeddings",
                    "driver_id": DRIVER_ID,
                    "endpoint": manifest.as_uri(),
                    "auth_identity": "local-no-auth",
                    "credential": {"kind": "local"},
                    "driver_config": {"asset_sha256": sha256(manifest), "threads": 2},
                },
                "model": {
                    "expected_revision": snapshot.revision,
                    "model_id": "local-memory",
                    "connection_id": "local-memory",
                    "kind": "embedding",
                    "model": assets["model"],
                    "make_default_embedding": True,
                    "capabilities": {
                        "embedding_dimensions": assets["dimensions"],
                        "embedding_normalization": "l2",
                    },
                    "capability_sources": {
                        "embedding_dimensions": "driver",
                        "embedding_normalization": "driver",
                    },
                },
            },
        )
    async with core.plugin_manager.live_root.context.require(
        EMBEDDINGS
    ).bind() as model:
        await model.embed(["本地记忆向量空间验证"])
    data = core.workspace / "plugin-data/akasha-builtin"
    config, _ = load_config(data)
    if config.get("enabled") is not True:
        save_config(data, {**config, "enabled": True})
        await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"akasha"}))


async def configure_proactive(core, workspace, session, *, timezone="Asia/Shanghai"):
    from agent.plugin_composition.config_input import save_config
    from plugins.wake.api import Config

    if not session.startswith("akashic:") or not session.removeprefix("akashic:"):
        raise ValueError("configure-proactive requires a native local akashic: session")
    config = Config(
        enabled=True,
        timezone=timezone,
        delivery={
            "channel": "akashic",
            "recipient": session.removeprefix("akashic:"),
            "session_id": session,
        },
    )
    save_config(
        Path(workspace) / "plugin-data/wake-builtin", config.model_dump(mode="json")
    )
    await core.plugin_manager.reconcile_changed(plugin_ids=frozenset({"wake"}))


def upgrade_workspace(workspace):
    """Offline native selection upgrade; preserve user choices and installed inputs.

    A running Root owns its committed selection. Updating bundle.patch alone
    cannot add builtins to an existing selection, nor publish current inputs.
    Prepare through the native loader, back up metadata, then CAS one selection.
    """
    from agent.plugins.bundles import plugin_choices, set_plugin_choice
    from agent.plugins.input_preparation import prepare_plugin_input
    from agent.plugins.selection import PluginSelection
    from agent.plugins.source_resolver import resolve_plugin_sources
    from bootstrap.workspace_lock import WorkspaceInstanceLock
    from kirakira_agent.runtime import ROOT

    workspace = Path(workspace).resolve()
    if (
        not (workspace / "sessions.db").is_file()
        or not (workspace / "bundle.patch.toml").is_file()
    ):
        raise ValueError("upgrade requires an initialized native workspace")
    additions = {
        "runtime_diagnostics",
        "plugin_update",
        "local-embedding",
        "event-ingress",
    }
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        selection = PluginSelection(workspace)
        previous = selection.read()
        if previous is None:
            raise ValueError("upgrade requires an already committed native selection")
        refs = selection.components(previous)
        existing = {selection.read_input(ref)["plugin_id"]: ref for ref in refs}
        choices = {**plugin_choices(workspace), **dict.fromkeys(additions, True)}
        refreshed = []
        for source in resolve_plugin_sources((ROOT / "plugins",)):
            name = source.plugin_name
            # Existing disabled providers stay disabled; unrelated sources and
            # marketplace installations retain their exact committed input.
            if not choices.get(name, name in existing):
                existing.pop(name, None)
                continue
            if name not in existing and name not in additions:
                continue
            manifest = source.static_manifest
            prepared = prepare_plugin_input(
                {
                    "name": name,
                    "plugin_root": str(source.plugin_root),
                    "module_path": str(source.plugin_root / "plugin.py"),
                    "manifest_digest": manifest.identity_digest,
                    "source_type": "builtin",
                },
                workspace=workspace,
                selection=selection,
            )
            existing[name] = prepared.input_ref
            refreshed.append(name)
        if not additions.issubset(existing):
            raise ValueError("distribution provider source is missing")
        backup = (
            workspace
            / "operator-backups"
            / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        backup.mkdir(mode=0o700, parents=True)
        for path in (workspace / "bundle.patch.toml", selection.path):
            shutil.copy2(path, backup / path.name)
        for name in sorted(additions):
            set_plugin_choice(workspace, name, enabled=True)
        current = selection.commit(
            tuple(existing[name] for name in sorted(existing)), expected_ref=previous
        )
        (backup / "upgrade.json").write_text(
            json.dumps(
                {
                    "previous_root": previous,
                    "current_root": current,
                    "refreshed_builtins": refreshed,
                    "enabled_additions": sorted(additions),
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        lock.release()
    return backup


async def backfill_embeddings(core):
    """Explicit local backfill into the native Ledger cache, never the graph.

    Native reindex deliberately skips uncached history. Fill eligible, complete
    raw-message segments first; Akasha still owns all learning and replay rules.
    """
    from agent.plugin_composition.config_input import load_config
    from core.common.file_io import run_file_io
    from plugins.content.api import legacy_post_commit_effect
    from plugins.ledger.contract import (
        MESSAGE_CATALOG,
        MESSAGE_EMBEDDINGS,
        ContentPart,
        Input,
        Output,
    )
    from plugins.models.contract import EMBEDDINGS
    from plugins.turn_projection.contract import TURN_PROJECTION

    ctx = core.plugin_manager.live_root.context
    catalog, projection = ctx.require(MESSAGE_CATALOG), ctx.require(TURN_PROJECTION)
    config, _ = load_config(core.workspace / "plugin-data/akasha-builtin")
    sources = set(
        config.get("sources", ("conversation", "programmatic", "legacy-unattributed"))
    )

    def visible(message):
        if not isinstance(message.body, (Input, Output)):
            return ""
        return "".join(
            p.value
            for p in message.body.parts
            if isinstance(p, ContentPart)
            and p.kind == "text"
            and isinstance(p.value, str)
        )

    records = ctx.require(MESSAGE_EMBEDDINGS).bind(visible)
    descriptor = ctx.require(EMBEDDINGS).describe()

    def pending():
        selected = {}
        for sid, head in catalog.snapshot_heads().items():
            if catalog.attributes(sid).learning != "eligible":
                continue
            reader = catalog.reader(sid)
            rows = reader.snapshot(through_seq=head)
            by_id = {m.message_id: m for m in rows}
            for source in sources & set(reader.source_names()):
                for turn in projection.project(rows, source):
                    if turn.status != "complete":
                        continue
                    members = [by_id[identity] for identity in turn.message_ids]
                    observations = [
                        by_id[identity] for _, identity in turn.observations
                    ]
                    if any(
                        legacy_post_commit_effect(m) == "suppress"
                        for m in (*members, *observations)
                    ):
                        continue
                    inputs = [
                        m
                        for m in members
                        if isinstance(m.body, Input) and visible(m).strip()
                    ]
                    ending = by_id[turn.ending_message_id]
                    if not inputs or not visible(ending).strip():
                        continue
                    for message in (*inputs, ending):
                        if (
                            records.read(
                                message,
                                model=descriptor.identity,
                                dimension=descriptor.dimensions,
                            )
                            is None
                        ):
                            selected[message.message_id] = message
        return list(selected.values())

    messages = await run_file_io(pending)
    async with ctx.require(EMBEDDINGS).bind() as model:
        if model.descriptor.identity != descriptor.identity:
            raise ValueError("embedding space changed before history backfill")
        for start in range(0, len(messages), 32):
            batch = messages[start : start + 32]
            result = await model.embed([visible(m) for m in batch])
            if len(result.vectors) != len(batch):
                raise ValueError("backfill embedding count mismatch")

            def save(batch=batch, vectors=result.vectors):
                for message, vector in zip(batch, vectors, strict=True):
                    records.save(message, model=descriptor.identity, embedding=vector)

            await run_file_io(save)
    return {"messages_backfilled": len(messages), "dimensions": descriptor.dimensions}
