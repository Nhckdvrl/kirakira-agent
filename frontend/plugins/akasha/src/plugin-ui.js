import "./plugin-ui.css";
import { escapeHtml } from "./escape.js";
import { renderRecall, sourceTime } from "./recall-card.js";
import { mountMemoryBadge, mountMemoryField } from "./scope-ui.tsx";

/** 聚合事实在服务端确认闭合前仍可变化，不进入页面结果缓存。 */
async function readRecall(context, isActive) {
  const query = async (messageId, offset) => {
    while (true) {
      if (!isActive()) throw new Error("召回面板已卸载");
      const result = await context.query("recall.turn", {
        message_id: messageId, source: context.block?.source ?? "", ...(offset ? { offset } : {}),
      }, { cache: "none" });
      // 兼容查找按服务端有界页推进，不是等待新事实的轮询。
      if (!result.legacy_pending) return result;
    }
  };
  const result = await query(context.messageId, 0);
  const items = [...result.items];
  let pending = result.pending;
  let offset = result.next_offset;
  while (offset != null) {
    const page = await query(result.input_message_id, offset);
    items.push(...page.items);
    pending = page.pending;
    offset = page.next_offset;
  }
  return { ...result, items, pending };
}

const OPEN_VIEW_PREFIX = "akasha.recall.open:";

/** 展开标志是可选的本页状态；浏览器拒绝存储时保留查询和清理行为。 */
function readOpenedView(key) {
  try {
    const value = sessionStorage.getItem(key) ?? "";
    sessionStorage.removeItem(key);
    return new Set(value.split(",").filter(lane => lane === "dense" || lane === "completion"));
  } catch (error) {
    if (!(error instanceof DOMException) || !["SecurityError", "QuotaExceededError"].includes(error.name)) throw error;
    console.warn("[akasha-ui] 浏览器未允许恢复展开状态", error.name);
    return new Set();
  }
}

/** 仅保留至多64个非空展开标志，不存放召回正文或结果。 */
function saveOpenedView(key, lanes) {
  try {
    if (!lanes.length) { sessionStorage.removeItem(key); return; }
    const keys = Array.from({length: sessionStorage.length}, (_, index) => sessionStorage.key(index))
      .filter(item => item?.startsWith(OPEN_VIEW_PREFIX) && item !== key);
    while (keys.length >= 64) sessionStorage.removeItem(keys.shift());
    sessionStorage.setItem(key, lanes.join(","));
  } catch (error) {
    if (!(error instanceof DOMException) || !["SecurityError", "QuotaExceededError"].includes(error.name)) throw error;
    console.warn("[akasha-ui] 浏览器未允许保存展开状态", error.name);
  }
}


/** 已接纳 Input 持有面板；事实失效只刷新内容，不重新挂载。 */
export function mountRecall(host, context) {
  if (typeof context.onInvalidate !== "function") throw new Error("聊天页面不支持召回刷新，请刷新页面后重试");
  let active = true;
  let loading = false;
  let dirty = false;
  let loaded = false;
  let loadingTimer;
  let rendered = null;
  let unsubscribe;
  // 只交接展开状态，不缓存消息、召回结果或插件版本。
  const keyFor = (messageId) => `${OPEN_VIEW_PREFIX}${JSON.stringify([context.sessionId, messageId, context.turnId, context.block?.source])}`;
  let viewKey = keyFor(context.messageId);
  const remembered = readOpenedView(viewKey);
  const content = document.createElement("div");
  const status = document.createElement("p");
  status.className = "akasha-plugin-ui-query-status";
  status.setAttribute("role", "status");
  status.hidden = true;
  host.replaceChildren(content, status);

  const load = async () => {
    if (!active) return;
    if (loading) { dirty = true; return; }
    loading = true;
    dirty = false;
    let settled = false;
    // 成功空快照也是已读；后续刷新不再显示首次加载占位。
    if (!loaded && status.hidden) loadingTimer = setTimeout(() => {
      if (!active) return;
      status.textContent = "正在读取召回记录…";
      status.hidden = false;
    }, 150);
    try {
      const result = await readRecall(context, () => active);
      if (!active) return;
      // 历史 fallback 使用服务端解析的 Input 交接展开状态，不猜测归属。
      if (result.input_message_id) {
        const resolvedKey = keyFor(result.input_message_id);
        if (viewKey !== resolvedKey) {
          viewKey = resolvedKey;
          if (!loaded) for (const lane of readOpenedView(viewKey)) remembered.add(lane);
        }
      }
      const markup = renderRecall(result);
      if (markup !== rendered) {
        const opened = new Set([
          ...(!loaded ? remembered : []),
          ...Array.from(content.querySelectorAll("details[open]"), (item) => item.dataset.lane),
        ]);
        content.innerHTML = markup;
        content.querySelectorAll("details").forEach((item) => { item.open = opened.has(item.dataset.lane); });
        rendered = markup;
      }
      loaded = true;
      status.hidden = true;
      settled = result.pending === false;
    } catch (error) {
      // 失败只更新局部状态，重试沿同一查询入口走，不销毁召回卡片。
      if (active) {
        const retry = document.createElement("button");
        retry.type = "button";
        const stale = error.code === "plugin_ui_stale_revision" || error.code === "plugin_ui_unavailable";
        retry.textContent = stale ? "刷新页面" : "重试";
        retry.addEventListener("click", () => { if (stale) window.location.reload(); else void load(); });
        status.replaceChildren(document.createTextNode(`情景记忆展示暂不可用：${error.message} `), retry);
        status.hidden = false;
      }
    } finally {
      loading = false;
      clearTimeout(loadingTimer);
      // 读取期间收到的提交不能丢失，即使这份旧快照已经声称闭合。
      if (active && dirty) void load();
      else if (active && settled) { unsubscribe?.(); unsubscribe = undefined; }
    }
  };
  unsubscribe = context.onInvalidate(() => { void load(); });
  void load();
  return () => {
    active = false;
    unsubscribe?.();
    clearTimeout(loadingTimer);
    if (loaded) saveOpenedView(viewKey,
      Array.from(content.querySelectorAll("details[open]"), item => item.dataset.lane));
  };
}

export default {
  slots: {
    "turn.before_reasoning": { mount: mountRecall },
    "scope.fields": { mount: mountMemoryField },
    "scope.badge": { mount: mountMemoryBadge },
  },
};
