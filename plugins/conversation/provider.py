from agent.composition.lifecycle import on_stopping

"""Conversation acceptance composes source admission and a replaceable reply program."""

from plugins.reply_program.contract import DEFAULT_REPLY, PROGRAMS
from plugins.sources.contract import ADMISSION
from plugins.jobs.contract import JOBS

from .contract import CONVERSATION
from .service import ConversationService

inject = (ADMISSION, DEFAULT_REPLY, PROGRAMS, JOBS)
provides = (CONVERSATION,)


async def apply(ctx):
    program, state = ctx.get(DEFAULT_REPLY), ctx.get(PROGRAMS)
    admission = ctx.get(ADMISSION)
    conversation = ConversationService(
        program=program, admission=admission, programs=state, jobs=ctx.get(JOBS)
    )
    execute = conversation.run

    async def run_owned(*args, **kwargs):
        task = await ctx.spawn(execute(*args, **kwargs), name="conversation")
        return await task

    conversation.run = run_owned

    state.suspending = False
    state.recover(program._persist_interrupted_turn)

    async def suspend():
        state.suspending = True
        await conversation.close()

    await on_stopping(ctx, suspend)
    await ctx.provide(CONVERSATION, conversation)
