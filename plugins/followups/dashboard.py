"""Authenticated, generation-bound HTTP facade for the conversation tab."""

from fastapi import FastAPI, HTTPException, Path, Query

from .contract import FOLLOWUPS, Feedback, SetEnabled, WatchId, WatchSpec

inject = (FOLLOWUPS,)


async def checked(action):
    try:
        return await action
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def register(app: FastAPI, context):
    @app.get("/api/dashboard/followups")
    async def overview(session_id: str = Query(min_length=1, max_length=512)):
        return await checked(context.require(FOLLOWUPS).overview(session_id))

    @app.post("/api/dashboard/followups")
    async def create(request: WatchSpec):
        return await checked(context.require(FOLLOWUPS).create(request))

    @app.get("/api/dashboard/followups/{watch_id}")
    async def timeline(watch_id: str = Path(pattern=r"^w_[0-9a-f]{32}$")):
        request = WatchId(watch_id=watch_id)
        return await checked(context.require(FOLLOWUPS).timeline(request.watch_id))

    @app.post("/api/dashboard/followups/poll")
    async def poll(request: WatchId):
        return await checked(context.require(FOLLOWUPS).poll(request.watch_id))

    @app.post("/api/dashboard/followups/enabled")
    async def enabled(request: SetEnabled):
        return await checked(context.require(FOLLOWUPS).set_enabled(request))

    @app.post("/api/dashboard/followups/feedback")
    async def feedback(request: Feedback):
        return await checked(context.require(FOLLOWUPS).feedback(request))
