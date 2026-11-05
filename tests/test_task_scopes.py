import asyncio
import pytest
from agent.plugin_composition import CompositionError, CompositionRoot, Context
from agent.plugin_composition.tasks import Tasks

@pytest.mark.asyncio
async def test_raw_child_does_not_inherit_parent_plugin_scope():
    """A raw child Task inherits values, but cannot capture the parent permit."""

    root = CompositionRoot("task-raw-child-root")
    contexts: list[Context] = []

    async def apply(ctx: Context) -> None:
        contexts.append(ctx)

    fiber = await root.mount(apply, name="task-owner")
    ctx = contexts[0]
    tasks = Tasks()
    errors: list[str | None] = []

    async def operation(_task):
        with pytest.raises(CompositionError) as excinfo:
            ctx.capture_runtime_scope()
        errors.append(excinfo.value.code)

    async def raw_child() -> None:
        task = await tasks.admit("key", lambda slot: slot.start(operation))
        await task.join()

    async with ctx.runtime_scope():
        await asyncio.create_task(raw_child())

    assert errors == ["OWNER_CALL_CONTEXT"]
    await tasks.close()
    await fiber.dispose()
    await root.dispose()


def test_programmatic_scope_governs_the_read_axis_that_persist_memory_cannot(tmp_path):
    """`persist_memory=False` stops writing memory; only a scope stops reading it.

    The two axes come from different places: the write axis from the session's
    learning eligibility, the read axis from the recall policy of every scope
    dimension the session carries. A session with no scope therefore satisfies
    the read condition vacuously and still gets recall injected, which is what
    admitting into a scope is for.
    """
    from types import SimpleNamespace

    from plugins.akasha.scopes import ScopePolicies
    from plugins.ledger.contract import SessionAttributes
    from plugins.ledger.log import MessageLog
    from plugins.programmatic.contract import AdmitParams, parse_scope_pairs

    log = MessageLog(tmp_path / "ledger.db")
    try:
        attributes = {
            # What admission produced before a scope could be given.
            "unscoped": SessionAttributes(visibility="internal", learning="excluded"),
            # What admission produces now, for a scope whose policy denies recall.
            "scoped": SessionAttributes.scoped(
                {"project": "github-review"}, visibility="internal", learning="excluded"),
        }
        # Setting a policy requires that no session routes to the value yet, so
        # the catalog answers both the per-session and the whole-catalog reads.
        catalog = SimpleNamespace(
            attributes=lambda session: attributes[session],
            snapshot_attributes=lambda: {},
        )
        policies = ScopePolicies(log.owner("akasha-scopes"), catalog)
        _ = policies.set("project", "github-review", "isolated", recall=False)

        unscoped = policies.route("unscoped")
        scoped = policies.route("scoped")

        # Both are excluded from learning, so neither writes.
        assert unscoped.write is None and scoped.write is None
        # Only the scoped one is kept out of recall. This is the gap: excluding a
        # session from learning never closed the read path.
        assert unscoped.read is not None
        assert scoped.read is None

        # Admission carries the scope through as an optional parameter, and
        # omitting it stays byte-equivalent to the previous construction.
        assert AdmitParams(session_id="s").scope is None
        assert (SessionAttributes.scoped({}, visibility="internal", learning="excluded")
                == SessionAttributes(visibility="internal", learning="excluded"))
        admitted = AdmitParams(session_id="s", scope={"project": "github-review"})
        assert admitted.scope == {"project": "github-review"}

        # The CLI and any scenario script share one parser.
        assert parse_scope_pairs(["project=a", "topic=b"]) == {"project": "a", "topic": "b"}
        assert parse_scope_pairs([]) == {}
        for bad in (["project"], ["=value"], ["project="], ["a=1", "a=2"]):
            try:
                parse_scope_pairs(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted invalid --scope {bad!r}")
    finally:
        log.close()
