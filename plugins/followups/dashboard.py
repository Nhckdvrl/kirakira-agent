"""Authenticated, generation-bound HTTP facade for the conversation tab."""

from fastapi import FastAPI, HTTPException, Path, Query

from plugins.ui.contract import UI

from .contract import FOLLOWUPS, Feedback, Revise, SetEnabled, WatchId, WatchSpec
from .mail_settings import MAIL_SETTINGS

inject = (FOLLOWUPS, MAIL_SETTINGS, UI)


async def checked(action):
    try:
        return await action
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def register(app: FastAPI, context):
    context.require(UI).register_configuration(
        app, context, MAIL_SETTINGS, "/api/dashboard/followups/config"
    )

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

    @app.post("/api/dashboard/followups/revise")
    async def revise(request: Revise):
        return await checked(context.require(FOLLOWUPS).revise(request))

    @app.post("/api/dashboard/followups/reset-mailbox")
    async def reset(request: WatchId):
        return await checked(context.require(FOLLOWUPS).reset_mailbox(request.watch_id))
