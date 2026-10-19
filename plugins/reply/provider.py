from agent.composition.lifecycle import on_close, on_ready, on_start, on_stopping

"""Declare this owner's dependencies, capabilities and resource lifetime."""


from bus.contract import BUS
from plugins.conversation.contract import CONVERSATION
from plugins.reply.contract import FOLLOWER
from plugins.reply.follower import ReplyFollower
from plugins.reply_program.contract import PROGRAMS

inject = (BUS, CONVERSATION, PROGRAMS)
provides = (FOLLOWER,)


async def apply(ctx):
    bus = ctx.get(BUS)
    pipeline = ctx.get(CONVERSATION)
    programs = ctx.get(PROGRAMS)
    bus.inbound_acceptor = programs.accept_channel
    loop = ReplyFollower(bus=bus, pipeline=pipeline)
    await on_close(ctx, lambda: loop.shutdown())

    await ctx.provide(FOLLOWER, loop)

    await on_start(ctx, lambda: ctx.spawn(loop.run(), name="agent_loop"))
    await on_stopping(ctx, loop.shutdown)

    async def recover_channel_work():
        for request in tuple(programs.recovery_requests):
            if bus.has_outbound_subscribers(request.channel):
                await bus.publish_inbound(request)
                programs.recovery_requests.remove(request)

    await on_ready(ctx, recover_channel_work)
