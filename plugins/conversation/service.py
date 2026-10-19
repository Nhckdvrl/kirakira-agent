"""Accept source work and run an injected reply program under Session admission."""

import asyncio
from contextlib import AsyncExitStack, nullcontext
from dataclasses import asdict

from bus.events import InboundMessage, OutboundMessage


class ConversationService:
    def __init__(self, *, program, admission, programs=None, jobs=None):
        self.program, self.admission, self.programs = program, admission, programs
        self.jobs = jobs
        self._closing, self._callers = False, set()
        self.bus, self.event_bus = program.bus, program.event_bus

    def __getattr__(self, name):
        return getattr(self.program, name)

    async def close(self):
        self._closing = True
        callers = tuple(self._callers)
        for caller in callers:
            caller.cancel()
        await asyncio.gather(*callers, return_exceptions=True)

    async def run(self, *args, **kwargs):
        if self._closing:
            raise RuntimeError("conversation owner is closing")
        task = asyncio.current_task()
        self._callers.add(task)
        try:
            return await self._run(*args, **kwargs)
        finally:
            self._callers.discard(task)

    async def _run(
        self,
        msg: InboundMessage | None,
        key: str,
        *,
        dispatch_outbound: bool = True,
        admitted=None,
        supersede=False,
        job_notice_id=None,
    ) -> OutboundMessage | None:
        identity = None
        resume = msg.metadata.get("resume_program_id") if msg is not None else None
        if msg is not None and self.programs is not None:
            if resume:
                identity = str(resume)
                msg = self.programs.resume_request(identity, key)
            else:
                identity, _ = self.programs.accept(msg, key, supersede=supersede)
        async with AsyncExitStack() as resources:
            await resources.enter_async_context(self.admission.hold(key))
            if msg is None:
                if self.jobs is None or job_notice_id is None:
                    raise ValueError("a pending job notice is required")
                # Decide after admission: the active turn may already have claimed
                # the result at its next model boundary. No empty program is created.
                msg = self.jobs.wake_request(job_notice_id, key)
                if msg is None:
                    return None
                identity, value = self.programs.accept(msg, key)
                if value["status"] in {"paused", "failed"}:
                    # A crash may have accepted the wake before its first material
                    # claim. Reuse that program, including its effects/head guards.
                    msg = self.programs.resume_request(identity, key)
                    resume = identity
            if identity is not None:
                row = self.programs.store.read(identity, key)
                if row["status"] == "completed":
                    result = self.programs.store.outbound(row)
                    if dispatch_outbound:
                        await self.bus.publish_outbound(result)
                    return result
            scope = (
                self.programs.activate(identity, resume=bool(resume))
                if identity is not None
                else nullcontext(None)
            )
            binding = resources.enter_context(scope)
            try:
                self.program._active_sessions.add(key)
                if admitted is not None:
                    admitted()
                if binding is not None:
                    binding.ensure_current()
                result = await self.program.execute(
                    msg, key, dispatch_outbound=dispatch_outbound
                )
                if (
                    binding is not None
                    and self.programs.store.read(identity)["status"] == "running"
                ):
                    result.metadata["program_id"] = identity
                    self.programs.store.settle(
                        identity, binding.lease, "completed", result=asdict(result)
                    )
                return result
            except asyncio.CancelledError:
                superseded = binding is not None and not self.programs.store.is_current(
                    identity
                )
                self.program._persist_interrupted_turn(
                    key,
                    msg,
                    binding=binding,
                    status="superseded" if superseded else "paused",
                )
                if superseded:
                    from bus.events_lifecycle import ProgramSuperseded

                    await self.event_bus.fanout(
                        ProgramSuperseded(
                            key,
                            identity,
                            str(msg.metadata.get("client_request_id") or ""),
                        )
                    )
                raise
            except Exception as exc:
                if (
                    binding is not None
                    and self.programs.store.read(identity)["status"] == "running"
                ):
                    superseded = not self.programs.store.is_current(identity)
                    self.program._persist_interrupted_turn(
                        key,
                        msg,
                        binding=binding,
                        status="superseded" if superseded else "failed",
                        error=str(exc),
                    )
                    if superseded:
                        from bus.events_lifecycle import ProgramSuperseded

                        await self.event_bus.fanout(
                            ProgramSuperseded(
                                key,
                                identity,
                                str(msg.metadata.get("client_request_id") or ""),
                            )
                        )
                        raise asyncio.CancelledError(
                            "superseded by a newer input"
                        ) from exc
                raise
            finally:
                self.program._turn_snapshots.pop(key, None)
                self.program._active_sessions.discard(key)
