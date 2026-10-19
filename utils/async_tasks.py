"""Physical completion for synchronous calls that cannot be cancelled in a thread."""

import asyncio
import inspect


async def cancel_and_drain(worker):
    """Send cancellation once; further caller cancels cannot interrupt cleanup."""
    if not worker.done() and not worker.cancelling():
        worker.cancel()
    cancellation = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if asyncio.current_task().cancelling():
                cancellation = exc
        except Exception:
            break
    if not worker.cancelled():
        worker.exception()
    if cancellation is not None:
        raise cancellation


async def run_sync_drained(function, /, *args, **kwargs):
    """Hold ownership on cancellation until the thread actually stops.

    This does not kill the thread or claim its external outcome is known. Repeated
    cancellation must not release the session/generation while it can still act.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            error = worker.exception()
            if error is None and inspect.iscoroutine(worker.result()):
                worker.result().close()
        raise
