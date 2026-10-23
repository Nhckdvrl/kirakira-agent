"""原生上下文压缩：后台整理旧前缀，硬容量不足时由前台缩减。"""
from __future__ import annotations

from importlib import import_module
import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping, Sequence
import logging
import hashlib
import json
from uuid import uuid4

from core.common.file_io import run_file_io

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import (
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    Context,
)
from plugins.models.contract import (
    CHAT_MODELS, BoundChatModel, BoundModelDescriptor, ContextModel, ModelError, ModelRequest,
    ContextLengthError,
    ModelTimeoutError,
    RateLimitError,
    TransportError,
)
from plugins.context.contract import CONTEXT, MATERIALS_V4, MaterialData, ReductionStatus, output_reserve
from plugins.ledger.contract import BINDINGS
from plugins.ledger.contract import MESSAGE_CATALOG, OWNER_STATE
from plugins.ledger.contract import Control, Input, Message, MessageConflict, Output

from .records import ManualResetRecord, StoredSummary, SummaryLookup, SummaryRecord, SummaryRecords
from .message_summary import MAX_OUTPUT_TOKENS, SummaryError, clip_summary, closed_groups, recoverable, source_text, summarize, summary_groups, window_starts
from plugins.compaction.contract import COMPACTION_READER, COMPACTION_SUMMARIES
from plugins.turn_projection.contract import TURN_PROJECTION, TurnProjection
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from agent.plugin_composition.model import ServiceKey
from plugins.ui.contract import Configuration, ModelSettings, mount_settings
from plugins.commands.contract import COMMANDS, CommandDefinition, CommandResult
from plugins.gateway.contract import RpcMethod

logger = logging.getLogger(__name__)

api_version = 3
name = "compaction"
version = "4.1.0"
desc = "按不可变消息前缀发布摘要，并为已使用的摘要保留原始读取口"
inject = (PLUGIN_CONFIG, MATERIALS_V4, CONTEXT, OWNER_STATE, BINDINGS, MESSAGE_CATALOG, CHAT_MODELS, TURN_PROJECTION)


SETTINGS = ServiceKey[Configuration]("compaction.settings.v1")
SETTING_FIELDS = ("keep_recent_tokens",)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_recent_tokens: int = Field(default=20_000, gt=0, strict=True)


class StartNew(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1, max_length=128)
    expected_head: int = Field(ge=0)


@dataclass(frozen=True)
class _FrozenProjection:
    """Only projected data crosses into maintenance; the worker owns its model."""

    model: BoundChatModel
    tail: ModelRequest
    cutoff: int

    @property
    def context_window(self) -> int | None:
        return self.model.descriptor.capabilities.context_window

    @property
    def max_tool_schemas(self) -> int | None:
        return self.model.max_tool_schemas

    def estimate(self, request: ModelRequest) -> int:
        return self.model.estimate_context_tokens(request.messages, request.tools)

    def render(self, messages: Sequence[Message], *, after_seq: int,
               summary_reference: str | None = None, **kwargs: object) -> ModelRequest:
        if after_seq != self.cutoff or summary_reference is None:
            raise ValueError("后台摘要只能使用已冻结的完整尾部投影")
        return self.tail


@dataclass
class _Maintenance:
    source: str
    boundary_seq: int
    parent: str | None
    finish: Callable[[BoundChatModel, ContextModel], Awaitable[MaterialData]]
    descriptor: BoundModelDescriptor
    tail: ModelRequest
    cutoff: int
    result: asyncio.Future[MaterialData | None]
    running: bool = False


class _CompactionReader:
    """Markdown 只取得 compaction 的纯读取算法，不取得发布或模型权限。"""

    def __init__(self, *, settled_prefixes: Callable[[tuple[Message, ...]], tuple[int, ...]]):
        self._settled_prefixes = settled_prefixes

    def source_text(self, messages: Sequence[Message]) -> str:
        return source_text(messages)

    def window_starts(
        self, messages: tuple[Message, ...], projection: TurnProjection,
    ) -> tuple[int, ...]:
        return window_starts(messages, projection, settled_prefixes=self._settled_prefixes)

    def summary_groups(
        self, groups: tuple[tuple[Message, ...], ...], snapshot: tuple[Message, ...],
    ) -> tuple[tuple[Message, ...], ...]:
        return summary_groups(groups, snapshot)


async def apply(ctx: Context) -> None:
    """注册只读材料和归档解析；apply 不打开 writer 或调用模型。"""
    config = Config.model_validate(ctx.config)
    await mount_settings(ctx, SETTINGS, ModelSettings(ctx, Config, SETTING_FIELDS),
                         lambda: import_module(".dashboard", __package__))
    context = ctx.require(CONTEXT)

    def records() -> SummaryRecords:
        return SummaryRecords(ctx.require(OWNER_STATE).open(ctx))

    # 状态查询在线程执行；启动时取得窄读取口，不在线程中重新申请 owner 写权限。
    read_record: Callable[[str], StoredSummary | None] | None = None
    read_current: Callable[[str], StoredSummary | None] | None = None
    maintenance: dict[str, _Maintenance] = {}
    worker: asyncio.Task[None] | None = None

    async def maintain() -> None:
        catalog = ctx.require(MESSAGE_CATALOG)
        async with aclosing(catalog.follow(wake_on=(Output, Control))) as updates:
            async for heads in updates:
                for session_id in heads:
                    job = maintenance.get(session_id)
                    if job is None or job.running:
                        continue
                    async with ctx.runtime_scope():
                        reader = catalog.reader(session_id)
                        rows = await run_file_io(lambda: reader.snapshot(after_seq=job.boundary_seq))
                        ending = next((row for row in rows if row.source == job.source and (
                            isinstance(row.body, Control) or isinstance(row.body, Output)
                            and row.body.finish != "continue")), None)
                        if ending is None:
                            continue
                        value = None
                        try:
                            state = records()
                            current = await run_file_io(lambda: state.head(session_id))
                            if isinstance(ending.body, Control) or (None if current is None else current.reference) != job.parent:
                                continue
                            if maintenance.get(session_id) is not job:
                                continue
                            job.running = True
                            descriptor = job.descriptor
                            async with ctx.require(CHAT_MODELS).independent_execution(
                                model_id=descriptor.model_id, reasoning_effort=descriptor.reasoning_effort,
                            ) as execution:
                                model = execution.chat("agent")
                                if (model.descriptor.model_revision, model.descriptor.capability_digest) != (
                                    descriptor.model_revision, descriptor.capability_digest,
                                ):
                                    continue
                                # Model execution binds the provider's caller;
                                # publication must return to the summary owner.
                                async with ctx.runtime_scope():
                                    value = await job.finish(model, _FrozenProjection(model, job.tail, job.cutoff))
                        except MessageConflict:
                            # A foreground compaction or explicit /new won the CAS.
                            logger.info("后台摘要的父记录已更新，丢弃旧候选: session=%s", session_id)
                        except (RuntimeError, TimeoutError, SummaryError) as error:
                            if not recoverable(error) and ModelError.read(error) is None:
                                raise
                            logger.warning("后台摘要未发布，原始记录保留: session=%s error=%s", session_id, type(error).__name__)
                        finally:
                            if not job.result.done():
                                job.result.set_result(value)
                            if maintenance.get(session_id) is job:
                                del maintenance[session_id]

    def read(reference: str) -> StoredSummary | None:
        if read_record is None:
            raise RuntimeError("摘要状态读取口尚未启动")
        return read_record(reference)

    def head(session_id: str) -> StoredSummary | None:
        if read_current is None:
            raise RuntimeError("摘要状态读取口尚未启动")
        return read_current(session_id)

    async def start(_event: object) -> None:
        nonlocal read_current, read_record, worker
        state = records()
        read_record = state.read
        read_current = state.head
        worker = await ctx.spawn(maintain(), name="context-maintenance")

    async def stop(_event: object) -> None:
        nonlocal read_current, read_record, worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        worker = None
        for job in maintenance.values():
            if not job.result.done():
                job.result.set_result(None)
        maintenance.clear()
        read_record = None
        read_current = None

    _ = await ctx.on(RUNTIME_STARTED, start)
    _ = await ctx.on(RUNTIME_STOPPING, stop)
    lookup = SummaryLookup(read, head)
    _ = await ctx.provide(COMPACTION_SUMMARIES, lookup)
    _ = await ctx.provide(COMPACTION_READER, _CompactionReader(
        settled_prefixes=context.settled_prefixes,
    ))

    async def start_new(request: StartNew):
        reader = ctx.require(MESSAGE_CATALOG).reader(request.session_id)
        state = records()
        reference = "manual-reset:" + hashlib.sha256(json.dumps(
            [request.session_id, request.request_id], separators=(",", ":")
        ).encode()).hexdigest()
        existing = await run_file_io(lambda: state.read(reference))
        if existing is not None:
            if not isinstance(existing, ManualResetRecord):
                raise ValueError("新对话请求标识已被其他操作使用")
            ending = reader.get(existing.source_message_ids[-1])
            if ending is None or ending.seq != request.expected_head:
                raise ValueError("新对话请求标识已用于其他消息边界")
            return {"reference": reference, "retained_messages": len(existing.source_message_ids)}
        snapshot = await reader.snapshot_async(through_seq=request.expected_head)
        if not snapshot or reader.head() != request.expected_head:
            raise ValueError("会话已变化，请刷新后再开始新对话")
        if len(snapshot) not in context.settled_prefixes(snapshot):
            raise ValueError("仍有未结算工具调用，请先中断并等待操作结算")
        parent = await run_file_io(lambda: state.head(request.session_id))
        record = ManualResetRecord(
            reference=reference, session_id=request.session_id,
            generation=1 if parent is None else parent.generation + 1,
            parent=None if parent is None else parent.reference,
            source_message_ids=tuple(row.message_id for row in snapshot),
            content="用户已明确开始新的短期对话。此前的聊天原文与长期记忆保留；这不是此前内容的语义摘要。",
        )
        await state.publish(record, reader, parent=parent, summary_range=context.summary_range)
        return {"reference": reference, "retained_messages": len(snapshot)}

    await ctx.provide(RpcMethod.key("compaction/start-new"), RpcMethod(StartNew, ctx.entrypoint(start_new)))

    async def commands(child):
        async def new_conversation(invocation):
            async with ctx.runtime_scope():
                reader = ctx.require(MESSAGE_CATALOG).reader(invocation.session_key)
                message = reader.get(invocation.message_id)
                if message is None or not isinstance(message.body, Input):
                    return CommandResult("error", "新对话命令缺少实际用户输入")
                # Include the command input in the boundary. Its durable reply
                # remains visible; the previous conversation stays in Ledger.
                await start_new(StartNew(session_id=invocation.session_key,
                    request_id=invocation.message_id, expected_head=message.seq))
                return CommandResult("success", "已开始新的短期对话。历史记录和长期记忆都保留。")
        await child.require(COMMANDS).register(child, CommandDefinition(
            name="new", description="开始新的短期对话，保留历史记录与长期记忆",
            handler=new_conversation, recover=new_conversation,
        ))
    await ctx.inject((COMMANDS,), commands, name="new-conversation-command")

    def material(record: StoredSummary) -> MaterialData:
        reference = ctx.require(BINDINGS).bind(COMPACTION_SUMMARIES, {
            "record_ref": record.reference, "session_id": record.session_id,
        })
        return {
            "reference": reference,
            "source_message_ids": record.source_message_ids,
            "content": record.content,
        }

    # prepare 的只读缓存：head 行版本不变时复用已核对的完整 lineage；
    # 发布走 records().head/publish 原路径，缓存只服务材料读取。
    head_cache: dict[str, tuple[int, StoredSummary]] = {}

    async def prepare(snapshot: tuple[Message, ...], source: str) -> MaterialData:
        if not snapshot:
            return {}
        session_id = snapshot[0].session_id
        state = records()

        def load() -> StoredSummary | None:
            head = state.read_head(session_id, known=head_cache.get(session_id))
            if head is None:
                head_cache.pop(session_id, None)
                return None
            version, record = head
            head_cache[session_id] = (version, record)
            return record

        # 命中与未命中都要读 head 行版本（SQL），恢复在文件线程完成；
        # 只有纯内存命中才值得省 executor 往返，这里不是（评审 #1142）。
        record = await run_file_io(load)
        if record is None:
            return {}
        return {"summary": material(record)}

    async def compact(snapshot: tuple[Message, ...], materials: MaterialData, request: ModelRequest,
                      model: BoundChatModel, projection: ContextModel, *, source: str, force: bool,
                      on_status: ReductionStatus | None = None,
                      summary_limit: int | None = None, defer: bool = False) -> MaterialData | None:
        """按已结算工具批次选旧前缀，再原子发布一份真实摘要。"""
        # 1. 容量与近期保留均按当前已固定的业务模型判断。
        reminder = context.reminder_content(materials)
        reminder_input_id = (
            next(
                (
                    message.message_id
                    for message in reversed(snapshot)
                    if message.source == source and isinstance(message.body, Input)
                ),
                None,
            )
            if reminder is not None
            else None
        )
        window = projection.context_window
        before = projection.estimate(request)
        if window is None or not snapshot or (not force and before < int(window * (0.60 if defer else 0.74))):
            return None
        state = records()
        parent = await run_file_io(lambda: state.head(snapshot[0].session_id))
        current_summary = materials.get("summary")
        if current_summary is not None and not isinstance(current_summary, Mapping):
            raise TypeError("Context 材料摘要必须是对象")
        if (None if parent is None else material(parent)) != current_summary:
            raise ValueError("本次已取得摘要与当前 Session head 不一致")
        turns = ctx.require(TURN_PROJECTION)
        if parent is None:
            # 摘要模型仍只取一个近期窗口；退出请求的更早资料写入 omitted 分区。
            origin = start = 0
        else:
            covered = context.summary_range(snapshot, parent.source_message_ids)
            origin, start = covered.start, covered.stop
        # 摘要候选只取 summary_limit 之前的前缀；容量估算与原文尾部仍用完整请求视图。
        groups = closed_groups(
            snapshot if summary_limit is None else snapshot[:summary_limit],
            turns, settled_prefixes=context.settled_prefixes, after=start,
        )
        if defer and not groups:
            return None
        # 没有新的已结算批次时，0 表示只重写父摘要本身。
        sizes = range(len(groups), 0, -1) if groups else range(0 if parent is not None else -1, -1, -1)
        selected: int | None = None
        fallback: int | None = None
        deepest: int | None = None
        retained_tokens = deepest_tokens = 0
        planned_tokens: dict[int, int] = {}
        # 原文保留量来自实际 Model 投影，包含尚未闭合的尾部与当前输入。
        for size in sizes:
            after_seq = groups[size - 1][-1].seq if size else snapshot[start - 1].seq
            tail = projection.render(
                snapshot,
                after_seq=after_seq,
                fresh=True,
                current_reminder=reminder if reminder_input_id is not None else None,
                current_reminder_input_id=reminder_input_id,
            )
            # 先用占位摘要核对完整输入；真实摘要生成后还会再次核对硬容量。
            count = start + sum(len(group) for group in groups[:size])
            planned = {**materials, "summary": {
                "reference": "compaction-plan", "content": "待生成摘要",
                "source_message_ids": tuple(message.message_id for message in snapshot[origin:count]),
            }}
            candidate, error = context.build_attempt(
                snapshot, materials=planned, model=projection, tools=request.tools,
                max_output_tokens=request.max_output_tokens,
                current_reminder_input_id=reminder_input_id,
            )
            if error is not None:
                continue
            planned_tokens[size] = projection.estimate(candidate)
            if deepest is None:
                deepest, deepest_tokens = size, projection.estimate(tail)
            if planned_tokens[size] > int(window * 0.74):
                continue
            if fallback is None:
                fallback, retained_tokens = size, projection.estimate(tail)
            if projection.estimate(tail) >= config.keep_recent_tokens:
                selected = size
                break
        if selected is None:
            # 没有切点能回到软水位时仍摘要全部已结算内容；是否放得下交给下面的硬容量核对。
            selected, retained_tokens = (fallback, retained_tokens) if fallback is not None else (deepest, deepest_tokens)
            if selected and on_status is not None:
                await on_status(f"上下文空间不足，这一轮只保留了最近约 {retained_tokens:,} tokens 的原文"
                                f"（目标 {config.keep_recent_tokens:,}），更早的内容改用摘要。"
                                "当前输入和进行中的工具调用完整保留。", True)
        if selected is None:
            raise SummaryError("当前输入与进行中的工具调用本身超出容量，没有可摘要的内容")
        inputs = summary_groups(groups[:selected], snapshot)
        if selected and not inputs:
            raise SummaryError("可选范围没有可用于摘要的资料")
        # 2. 摘要必须放进剩余空间：模型生成 → 模型缩写 → 按比例截断各节正文，取第一个放得下的。
        reserve = output_reserve(request.max_output_tokens, window)
        room = window - reserve - planned_tokens[selected]
        count = start + sum(len(group) for group in groups[:selected])
        added = snapshot[start:count]

        async def finish(model: BoundChatModel, projection: ContextModel,
                         reporter: ReductionStatus | None = None) -> MaterialData:
            def measure(text: str, calls: tuple[str, ...],
                        summarized: tuple[tuple[Message, ...], ...]) -> tuple[SummaryRecord, MaterialData, int]:
                summary_ids = tuple(message.message_id for group in summarized for message in group)
                kept = set(summary_ids)
                record = SummaryRecord(
                    reference=uuid4().hex, session_id=snapshot[0].session_id,
                    generation=1 if parent is None else parent.generation + 1,
                    parent=None if parent is None else parent.reference,
                    source_message_ids=tuple(message.message_id for message in snapshot[origin:count]),
                    summary_message_ids=summary_ids,
                    omitted_message_ids=tuple(message.message_id for message in added if message.message_id not in kept),
                    content=text, model_call_ids=calls, trigger="context_overflow" if force else "soft_limit",
                    context_window=window, max_output_tokens=request.max_output_tokens,
                    keep_recent_tokens=config.keep_recent_tokens, tokens_before=before, tokens_after=0,
                )
                summary = material(record)
                after_request, _ = context.build_attempt(
                    snapshot, materials={**materials, "summary": summary}, model=projection,
                    tools=request.tools, max_output_tokens=request.max_output_tokens,
                    current_reminder_input_id=reminder_input_id,
                )
                return record, summary, projection.estimate(after_request)

            def fits(after: int) -> bool:
                return after < before and after + reserve <= window

            async def generate(groups: tuple[tuple[Message, ...], ...], previous: str,
                               limit: int) -> tuple[str, tuple[str, ...], tuple[tuple[Message, ...], ...]]:
                # 嵌套 execution 复用调用者已经固定的角色，不重读模型配置；
                # 只包住模型调用，摘要 binding 必须回到 compaction 自己的调用方。
                async with ctx.require(CHAT_MODELS).execution() as execution:
                    async def report_fallback(text: str) -> None:
                        if reporter is not None:
                            await reporter(text, True)

                    return await summarize(groups, previous=previous, limit=limit, model=model,
                                           fallback=execution.chat("default"), on_fallback=report_fallback)

            limit = max(1, min(room, MAX_OUTPUT_TOKENS))
            text, calls, summarized = await generate(inputs, "" if parent is None else parent.content, limit)
            record, summary, after = measure(text, calls, summarized)
            if not fits(after):
                try:
                    shorter, more, _ = await generate((), text, max(1, limit // 2))
                except (RuntimeError, TimeoutError, SummaryError) as failure:
                    if not recoverable(failure):
                        raise
                    logger.warning("摘要缩写失败，改为按比例截断已有摘要: %s", failure)
                else:
                    text, calls = shorter, calls + more
                    record, summary, after = measure(text, calls, summarized)
            # 已有合法摘要时本地按比例截断各节正文，直到放得下；只剩标题仍放不下才是真正的容量边界。
            for ratio in (0.5, 0.25, 0.0):
                if fits(after):
                    break
                record, summary, after = measure(clip_summary(text, ratio), calls, summarized)
            if after >= before:
                raise SummaryError("摘要没有降低本次完整请求容量")
            if after + reserve > window:
                raise SummaryError(f"摘要只保留标题后输入仍约 {after:,} tokens，当前输入与进行中的工具调用本身超出容量")
            record = record.model_copy(update={"tokens_after": after})
            # 3. binding 可以先固定，但读者只有在摘要事务成功后才取得此引用。
            reader = ctx.require(MESSAGE_CATALOG).reader(record.session_id)
            _ = await records().publish(
                record, reader, parent=parent, summary_range=context.summary_range,
            )
            return summary

        if not defer:
            return await finish(model, projection, on_status)

        # Freeze only the native rendered tail. No caller model, material view,
        # tool menu, or borrowed execution survives into the owned worker.
        from types import SimpleNamespace
        tails: list[ModelRequest] = []
        def capture(*args, **kwargs):
            tail = projection.render(*args, **kwargs)
            tails.append(tail)
            return tail
        captured = SimpleNamespace(render=capture, estimate=projection.estimate,
            context_window=projection.context_window, max_tool_schemas=projection.max_tool_schemas)
        _ = context.build_attempt(snapshot, materials={**materials, "summary": {
            "reference": "compaction-plan", "content": "待生成摘要",
            "source_message_ids": tuple(message.message_id for message in snapshot[origin:count]),
        }}, model=captured, tools=request.tools, max_output_tokens=request.max_output_tokens,
            current_reminder_input_id=reminder_input_id)
        if len(tails) != 1 or tails[0].continuation is not None:
            raise SummaryError("后台摘要无法取得独立的原生尾部投影")
        session_id = snapshot[0].session_id
        if session_id not in maintenance and len(maintenance) >= 64:
            return None  # A bounded opportunistic queue; foreground remains authoritative.
        previous = maintenance.pop(session_id, None)
        if previous is not None and not previous.result.done():
            previous.result.set_result(None)
        maintenance[session_id] = _Maintenance(source, snapshot[-1].seq,
            None if parent is None else parent.reference, finish, model.descriptor,
            tails[0], snapshot[count - 1].seq, asyncio.get_running_loop().create_future())
        return None

    async def reduce(snapshot: tuple[Message, ...], materials: MaterialData, request: ModelRequest,
                     model: BoundChatModel, projection: ContextModel, *, source: str, force: bool,
                     on_status: ReductionStatus | None = None,
                     summary_limit: int | None = None) -> MaterialData | None:
        """软水位失败可继续；硬容量或 provider 明确拒绝必须保留失败原因。"""
        window = projection.context_window
        before = projection.estimate(request)
        if window is None or not snapshot or (not force and before < int(window * 0.60)):
            return None
        session_id = snapshot[0].session_id
        must_reduce = force or before + output_reserve(request.max_output_tokens, window) > window
        job = maintenance.get(session_id)
        if job is not None:
            if not must_reduce:
                return None
            if job.running:
                # Join the one real maintenance call; never send a duplicate.
                await asyncio.shield(job.result)
            else:
                # A queued job waits for this reply to finish. It cannot be
                # joined while this very reply needs space to proceed.
                maintenance.pop(session_id, None)
                if not job.result.done():
                    job.result.set_result(None)
        state = records()
        latest = await run_file_io(lambda: state.head(session_id))
        latest_material = None if latest is None else material(latest)
        if latest_material != materials.get("summary"):
            if latest_material is None:
                raise SummaryError("已有摘要 head 不应消失")
            materials = {**materials, "summary": latest_material}
            request, rejection = context.build_attempt(snapshot, materials=materials, model=projection,
                tools=request.tools, max_output_tokens=request.max_output_tokens)
            if rejection is None:
                return latest_material
            before = projection.estimate(request)
            must_reduce = True
        budget = (f"模型 {model.descriptor.model_id}；窗口 {window:,}；输入估算 {before:,}；"
                  f"输出预留 {output_reserve(request.max_output_tokens, window):,}；原文目标 {config.keep_recent_tokens:,} tokens")
        if must_reduce and on_status is not None:
            await on_status("正在压缩上下文：" + budget, False)
        notices: list[str] = []
        async def report(text: str, retain: bool) -> None:
            if retain:
                notices.append(text)
            if on_status is not None:
                await on_status(text, retain)

        try:
            return await compact(snapshot, materials, request, model, projection,
                                 source=source, force=force, on_status=report if must_reduce else None,
                                 summary_limit=summary_limit, defer=not must_reduce)
        except (RuntimeError, TimeoutError, SummaryError) as error:
            if not (ModelError.matches(error, ContextLengthError, ModelTimeoutError, RateLimitError, TransportError) or isinstance(error, SummaryError)):
                raise
            if not must_reduce:
                logger.info("后台摘要暂不安排，前台请求仍满足容量: session=%s error=%s", session_id, type(error).__name__)
                return None
            detail = f"上下文压缩失败：{type(error).__name__}: {error}。{budget}。"
            if force or before + output_reserve(request.max_output_tokens, window) > window:
                raise SummaryError("\n".join((*notices, detail + "本次请求被阻断，未继续业务生成。"))) from error
            if on_status is not None:
                await on_status(detail + "原请求仍满足硬容量，本次继续生成。", True)
            return None

    _ = await ctx.require(MATERIALS_V4).register(ctx, kind="context", name="compaction", prepare=prepare, reduce=reduce, priority=500)
