"""Memory tool provider: schemas, evidence formatting and original-message access."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agent.tools.registry import ToolRegistry, object_schema
from core.memory.engine import (
    MemoryCapability,
    MemoryMutation,
    MemoryQuery,
    MemoryQueryFilters,
    MemoryScope,
)
from core.memory.legacy import MemoryRuntime
from core.schema import ToolSpec
from session.manager import SessionManager

# 引擎的注入选择器只接受这四类(retriever.py 的 `else: continue`),其余类型即使被
# 检索命中也**永远不会注入上下文**。Reference 靠工具 schema 的 enum 防住;kirakira 的
# 旧 schema 曾提供 identity/fact/requested_memory,写进去的行会静默失效。
# 这里在写入边界归一(与旧 MemoryRuntime._canonical_memory_type 同一张映射),
# 不改镜像的 default_engine.py。
_CANONICAL_MEMORY_KINDS = frozenset({"event", "profile", "preference", "procedure"})
_LEGACY_MEMORY_KIND_MAP = {
    "identity": "profile",
    "fact": "profile",
    "requested_memory": "profile",
}


def _canonical_memory_kind(kind: str) -> str:
    value = (kind or "").strip()
    if not value or value in _CANONICAL_MEMORY_KINDS:
        return value
    return _LEGACY_MEMORY_KIND_MAP.get(value, value)


# ── 记忆工具的渲染与过滤(照 Reference agent/tools/recall_memory.py / forget_memory.py) ──

_MEMORY_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_MEMORY_RECENT_PRESETS = {
    "recent_3d": 3,
    "recent_7d": 7,
    "recent_30d": 30,
}


def _normalize_recall_intent(value: str) -> str:
    intents = {"context", "answer", "timeline", "interest", "procedure"}
    return value if value in intents else "answer"


def _parse_memory_day(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_MEMORY_LOCAL_TZ)
    except ValueError:
        return None


def _parse_time_filter(value: str) -> tuple[datetime, datetime] | None:
    text = (value or "").strip()
    if not text:
        return None
    now = datetime.now(_MEMORY_LOCAL_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today, today + timedelta(days=1)
    if text == "yesterday":
        start = today - timedelta(days=1)
        return start, today
    if text in _MEMORY_RECENT_PRESETS:
        return now - timedelta(days=_MEMORY_RECENT_PRESETS[text]), now
    if "~" in text:
        left, right = [part.strip() for part in text.split("~", 1)]
        start = _parse_memory_day(left)
        end_day = _parse_memory_day(right)
        if start is None or end_day is None:
            return None
        return start, end_day + timedelta(days=1)
    day = _parse_memory_day(text)
    if day is None:
        return None
    return day, day + timedelta(days=1)


def _render_recall_records(records: list, *, trace: dict) -> str:
    """带 `§cited:` 引用协议的结构化返回;记忆问责依赖它,不是可选的花样。"""
    items: list[dict[str, object]] = []
    for record in records:
        evidence = [
            {
                "kind": ref.kind,
                "refs": ref.refs,
                "resolver": ref.resolver,
                "source_ref": ref.source_ref,
                "metadata": ref.metadata,
            }
            for ref in (record.evidence or [])
        ]
        source_ref = ""
        for entry in evidence:
            candidate = str(entry.get("source_ref") or "").strip()
            if candidate:
                source_ref = candidate
                break
            refs = entry.get("refs")
            if isinstance(refs, list):
                for ref in refs:
                    if isinstance(ref, str) and ref.strip():
                        source_ref = ref.strip()
                        break
            if source_ref:
                break
        item: dict[str, object] = {
            "id": record.id,
            "memory_type": record.kind,
            "summary": record.summary,
            "score": round(float(record.score), 4),
            "evidence": evidence,
            "signals": record.signals,
        }
        if source_ref:
            item["source_ref"] = source_ref
        items.append(item)
    cited_item_ids = [
        str(item["id"]) for item in items if str(item.get("id", "")).strip()
    ]
    return json.dumps(
        {
            "count": len(items),
            "items": items,
            "trace": trace,
            "citation_required": True,
            "citation_format": "§cited:[id1,id2,...]§",
            "cited_item_ids": cited_item_ids,
            "citation_rule": (
                "若最终回复使用了本工具返回的任何记忆条目，"
                "必须在正文末尾输出 §cited:[实际使用的id列表]§"
            ),
        },
        ensure_ascii=False,
    )


def _render_forget_result(
    requested_ids: list[str],
    affected_ids: list,
    missing_ids: list,
    items: list,
) -> str:
    return json.dumps(
        {
            "requested_ids": requested_ids,
            "superseded_ids": list(affected_ids),
            "missing_ids": list(missing_ids),
            "count": len(affected_ids),
            "items": list(items),
        },
        ensure_ascii=False,
    )


class MemoryTools:
    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        memory: MemoryRuntime | None = None,
        session_manager: SessionManager | None = None,
        memory_services: Any = None,
    ):
        self.registry = registry
        self.memory = memory
        self.session_manager = session_manager
        self.memory_services = memory_services

    async def compact(self) -> str:
        """把当前会话的历史强制归档进长期记忆,推进 consolidation 游标。

        之前这里是一个只返回固定文案的空壳——模型调用后会以为上下文已压缩。
        现在接到真实的 MarkdownMemoryMaintenance:游标推进后,下一轮的历史窗口
        从归档点开始(runtime 以 last_consolidated 为 start_index 切历史)。
        """
        maintenance = getattr(
            getattr(self.memory_services, "markdown", None), "maintenance", None
        )
        if maintenance is None or self.session_manager is None or self.registry is None:
            return "Error: 当前 runtime 未配置记忆归档能力，无法压缩上下文"
        session_key = str(self.registry.context.get("session_key") or "")
        if not session_key:
            return "Error: 当前上下文没有会话，无法压缩"
        from core.memory.markdown import ConsolidateRequest

        # 与 pipeline 持有的是同一个缓存实例,游标推进不会被 turn 提交覆盖。
        session = self.session_manager.get_or_create(session_key)
        before = int(session.last_consolidated or 0)
        await maintenance.consolidate(
            ConsolidateRequest(
                session=session,
                force=True,
                scope_channel=str(session.metadata.get("channel") or ""),
                scope_chat_id=str(session.metadata.get("chat_id") or ""),
            )
        )
        after = int(session.last_consolidated or 0)
        if after > before:
            await self.session_manager.save_async(session)
            return "已归档 %d 条历史消息到长期记忆；下一轮起上下文从归档点开始。" % (
                after - before
            )
        return "没有需要归档的新历史。"

    def _live_memory_engine(self):
        """返回承重的记忆引擎;未配置或能力不足时返回 None,调用方回退旧路径。"""
        services = self.memory_services
        engine = getattr(services, "engine", None) if services is not None else None
        if engine is None:
            return None
        descriptor = getattr(engine, "DESCRIPTOR", None)
        capabilities = getattr(descriptor, "capabilities", frozenset())
        return (
            engine if MemoryCapability.RETRIEVE_CONTEXT_BLOCK in capabilities else None
        )

    def _memory_scope(self) -> MemoryScope:
        if self.registry is None:
            return MemoryScope()
        ctx = self.registry.context
        return MemoryScope(
            session_key=str(ctx.get("session_key") or ""),
            channel=str(ctx.get("channel") or ""),
            chat_id=str(ctx.get("chat_id") or ""),
        )

    def _next_source_ref(self) -> str:
        if self.registry is None or self.session_manager is None:
            return ""
        session_key = str(self.registry.context.get("session_key") or "")
        if not session_key:
            return ""
        return self.session_manager.peek_next_message_id(session_key)

    async def memorize(
        self,
        content: str = "",
        memory_type: str = "",
        summary: str = "",
        memory_kind: str = "",
        tool_requirement: str | None = None,
        steps=None,
    ) -> str:
        """写入长期记忆。

        引擎承重时参数面照 Reference `agent/tools/memorize.py`:`summary`/`memory_kind`,
        `tool_requirement`/`steps` 进 mutation metadata——procedure 类记忆靠它们生成
        rule_schema 与触发标签。旧参数名 `content`/`memory_type` 继续接受(回退路径的 schema)。
        """
        text = str(summary or content or "").strip()
        if not text:
            return "Error: summary 不能为空"
        kind = _canonical_memory_kind(str(memory_kind or memory_type or "").strip())
        source_ref = self._next_source_ref()
        engine = self._live_memory_engine()
        if engine is not None:
            metadata: dict[str, object] = {}
            if tool_requirement is not None:
                metadata["tool_requirement"] = str(tool_requirement)
            if steps is not None:
                metadata["steps"] = [str(step) for step in steps]
            result = await engine.mutate(
                MemoryMutation(
                    kind="remember",
                    summary=text,
                    memory_kind=kind,
                    source_ref=source_ref or "memorize_tool",
                    scope=self._memory_scope(),
                    metadata=metadata,
                )
            )
            if not result.accepted:
                return "Error: 记忆写入被拒绝"
            # 返回格式照 Reference memorize.py:_format_result。
            actual_kind = (result.actual_kind or "").strip()
            status = (result.status or "new").strip()
            if actual_kind:
                return "已记住（item_id=%s；kind=%s；status=%s）：%s" % (
                    result.item_id,
                    actual_kind,
                    status,
                    text,
                )
            return "已记住（item_id=%s；status=%s）：%s" % (
                result.item_id,
                status,
                text,
            )
        if self.memory is None:
            return "Error: Memory runtime is not enabled"
        record = self.memory.memorize(
            text, source_ref=source_ref, memory_type=kind or "requested_memory"
        )
        return "记忆已写入: %s" % record.id

    async def recall_memory(
        self,
        query: str,
        limit: int = 8,
        intent: str = "answer",
        memory_kind: str = "",
        time_filter: str = "",
        memory_types=None,
        since: str = "",
        until: str = "",
    ) -> str:
        """检索长期记忆。

        引擎承重时对齐 Reference `agent/tools/recall_memory.py`:
        - `intent` answer/timeline(timeline 分支在引擎里真实存在,之前模型永远够不到);
        - `time_filter` 预设串解析成 filters.time_start/end,非法值明确报错;
        - limit 钳制 1..200;
        - 返回带 evidence/source_ref/trace 与 `§cited:` 引用协议的结构。
        旧参数 `memory_types`/`since`/`until` 只在词法回退路径继续生效。
        """
        text = str(query or "").strip()
        if isinstance(memory_types, str):
            memory_types = [memory_types]
        legacy_kinds = tuple(str(item) for item in (memory_types or []))
        kinds = (
            (memory_kind.strip(),) if str(memory_kind or "").strip() else legacy_kinds
        )
        engine = self._live_memory_engine()
        if engine is not None:
            if not text:
                return _render_recall_records([], trace={})
            time_window = _parse_time_filter(time_filter)
            if time_filter and time_window is None:
                return json.dumps(
                    {"count": 0, "items": [], "error": "invalid_time_filter"},
                    ensure_ascii=False,
                )
            result = await engine.query(
                MemoryQuery(
                    text=text,
                    intent=_normalize_recall_intent(intent),
                    effect="read_only",
                    scope=self._memory_scope(),
                    filters=MemoryQueryFilters(
                        kinds=kinds,
                        time_start=time_window[0] if time_window else None,
                        time_end=time_window[1] if time_window else None,
                    ),
                    limit=max(1, min(int(limit), 200)),
                    # akasha 用它算图激活的时间衰减;DefaultMemoryEngine 忽略。
                    timestamp=datetime.now(timezone.utc),
                )
            )
            return _render_recall_records(result.records, trace=result.trace)
        if self.memory is None:
            return "[]"
        records = self.memory.recall(
            text,
            limit=limit,
            memory_types=list(kinds),
            since=since,
            until=until,
        )
        return json.dumps(
            [r.to_public_json() for r in records], ensure_ascii=False, indent=2
        )

    async def forget_memory(self, ids) -> str:
        if isinstance(ids, str):
            ids = [ids]
        # 去重保序,照 Reference forget_memory.py:_clean_ids。
        clean: list[str] = []
        seen: set[str] = set()
        for raw in ids or []:
            item_id = str(raw).strip()
            if item_id and item_id not in seen:
                seen.add(item_id)
                clean.append(item_id)
        engine = self._live_memory_engine()
        if engine is not None:
            if not clean:
                return _render_forget_result([], [], [], [])
            result = await engine.mutate(
                MemoryMutation(
                    kind="forget", ids=tuple(clean), scope=self._memory_scope()
                )
            )
            return _render_forget_result(
                clean, result.affected_ids, result.missing_ids, result.items
            )
        if self.memory is None:
            return "Error: Memory runtime is not enabled"
        forgotten = self.memory.forget(clean)
        return json.dumps({"superseded_ids": forgotten}, ensure_ascii=False)

    async def memory_signal(self, **kwargs: Any) -> str:
        """engine 自定义记忆工具的通用回执(对照 Reference `_MemorySignalTool`)。

        这类工具的真实语义在引擎侧的副作用里(例如 akasha 的 `reinforce_memory`
        是给激活图加权),工具本身只确认收到——所以一个 handler 覆盖所有自定义工具。
        """
        return "已记录。"

    def search_messages(self, query: str, limit: int = 10) -> str:
        if self.session_manager is None:
            return "[]"
        return json.dumps(
            self.session_manager.search_messages(query, limit=limit),
            ensure_ascii=False,
            indent=2,
        )

    def fetch_messages(self, source_ref: str, context: int = 2) -> str:
        if self.session_manager is None:
            return "[]"
        return json.dumps(
            self.session_manager.fetch_messages(source_ref, context=context),
            ensure_ascii=False,
            indent=2,
        )


def _register_memory_tools(
    registry: ToolRegistry, handlers: "MemoryTools", *, read_only: bool = False
) -> None:
    """注册显式记忆工具。

    引擎承重时,**工具面完全由 `engine.tool_profile()` 决定**(照 Reference
    `agent/tools/meta/register.py`):声明了哪个就注册哪个,没声明的一律不注册,
    并把 `profile.tools` 里的自定义工具一并注册。这条很重要——akasha 只声明
    `recall` 与自定义的 `reinforce_memory`,**没有 memorize/forget**(它从 turn 自动
    摄入)。此前这里"profile 没有就退回旧 schema 注册",导致 akasha 下模型能看到
    memorize,调用后被引擎拒绝写入。

    引擎未承重时才注册 kirakira 词法路径的旧 schema——这是与 Reference 的显式偏离:
    Reference 在 Disabled 时干脆不注册记忆工具,kirakira 保留词法降级。
    """
    engine = handlers._live_memory_engine()
    if engine is None:
        _register_legacy_memory_tools(registry, handlers, read_only=read_only)
        return

    profile = engine.tool_profile()
    if not read_only and profile.memorize is not None:
        registry.register(
            ToolSpec(
                "memorize", profile.memorize.description, profile.memorize.parameters
            ),
            handlers.memorize,
            risk="write",
        )
    if profile.recall is not None:
        registry.register(
            ToolSpec(
                "recall_memory", profile.recall.description, profile.recall.parameters
            ),
            handlers.recall_memory,
        )
    if not read_only and profile.forget is not None:
        registry.register(
            ToolSpec(
                "forget_memory", profile.forget.description, profile.forget.parameters
            ),
            handlers.forget_memory,
            risk="write",
        )
    # engine 自定义工具槽(对照 Reference 的 `_MemorySignalTool`):引擎可以追加任意工具,
    # 语义由引擎在自己的副作用里实现,工具本身只回执。
    for spec in () if read_only else getattr(profile, "tools", ()) or ():
        name = str(getattr(spec, "name", "") or "").strip()
        if not name:
            raise ValueError("自定义 memory 工具缺少 name")
        registry.register(
            ToolSpec(name, spec.description, spec.parameters),
            handlers.memory_signal,
            risk=spec.risk,
            search_hint=spec.search_hint or None,
        )


def _register_legacy_memory_tools(
    registry: ToolRegistry, handlers: "MemoryTools", *, read_only: bool = False
) -> None:
    """引擎未承重时的词法降级工具面。"""
    if not read_only:
        registry.register(
            ToolSpec(
                "memorize",
                "Write a stable user fact or preference into long-term memory.",
                object_schema(
                    {
                        "content": {"type": "string"},
                        "memory_type": {
                            "type": "string",
                            "enum": [
                                "requested_memory",
                                "identity",
                                "preference",
                                "procedure",
                                "event",
                            ],
                        },
                    },
                    ["content"],
                ),
            ),
            handlers.memorize,
            risk="write",
        )
    if not read_only or handlers.memory is not None:
        registry.register(
            ToolSpec(
                "recall_memory",
                "Search long-term memory semantically/lexically.",
                object_schema(
                    {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "memory_types": {"type": "array", "items": {"type": "string"}},
                        "since": {"type": "string"},
                        "until": {"type": "string"},
                    },
                    ["query"],
                ),
            ),
            handlers.recall_memory,
        )
    if not read_only:
        registry.register(
            ToolSpec(
                "forget_memory",
                "Mark memory items as forgotten by id.",
                object_schema(
                    {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]
                ),
            ),
            handlers.forget_memory,
            risk="write",
        )


def register_memory_tools(
    registry: ToolRegistry,
    *,
    memory: MemoryRuntime | None = None,
    session_manager: SessionManager | None = None,
    memory_services: Any = None,
    read_only: bool = False,
) -> None:
    handlers = MemoryTools(
        registry=registry,
        memory=memory,
        session_manager=session_manager,
        memory_services=memory_services,
    )
    _register_memory_tools(registry, handlers, read_only=read_only)
    if not read_only:
        registry.register(
            ToolSpec(
                "compact",
                "将当前会话的历史归档进长期记忆并推进归档游标；下一轮起上下文从归档点开始。仅在上下文确实过长时使用。",
                object_schema({}, []),
            ),
            handlers.compact,
            risk="write",
        )

    registry.register(
        ToolSpec(
            "search_messages",
            "Keyword search persisted chat messages and return source refs.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
                ["query"],
            ),
        ),
        handlers.search_messages,
    )

    registry.register(
        ToolSpec(
            "fetch_messages",
            "Fetch persisted chat messages around a source_ref.",
            object_schema(
                {"source_ref": {"type": "string"}, "context": {"type": "integer"}},
                ["source_ref"],
            ),
        ),
        handlers.fetch_messages,
    )
