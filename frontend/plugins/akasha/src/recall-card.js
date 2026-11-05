/** 召回卡片的纯呈现：输入 Inspector 结果，输出 HTML 字符串。
 *
 * 不引样式、不触碰 DOM，因此可以在浏览器之外渲染并逐字核对产出。
 */

import { escapeHtml } from "./escape.js";
import { deliveryLabel, deliveryOf, deliveryRank, deliveryTitle } from "./recall-delivery.js";

const sourceTime = value => {
  if (typeof value !== "string" || !value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value ?? "")
    : date.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
};

/** 先计算展示内容；相同快照不改写真实卡片、选择与焦点。 */
export function renderRecall(result) {
  return result.items.length ? `<div class="akasha-plugin-ui-recall-group">${[
    ["dense", "语义匹配", "precise"], ["completion", "关联补全", "completion"],
  ].map(([lane, title, style]) => {
    // 多次真实查询可以命中同一回忆；卡片按消息成员展示一次，原查询留在 Inspector。
    const merged = new Map();
    for (const hit of result.items.flatMap(item => item.hits.filter(hit => hit.lane === lane))) {
      const key = JSON.stringify(hit.messages.map(message => message.message_id));
      const previous = merged.get(key);
      merged.set(key, previous ? { ...hit, messages: hit.messages.map(message => {
        const earlier = previous.messages.find(item => item.message_id === message.message_id);
        // Keep whichever query delivered the most of this original, so merging
        // two real queries never downgrades a body to a bare candidate.
        return earlier && deliveryRank(earlier) > deliveryRank(message) ? earlier : message;
      }) } : hit);
    }
    const hits = [...merged.values()];
    // Counted per original, not per hit: one hit can carry a body and an index
    // line at once, and two per-hit numerators over a per-hit total read as
    // more than the whole.
    const shown = hits.flatMap(hit => hit.messages.filter(message => message.preview));
    const presented = shown.filter(message => deliveryOf(message) === "body").length;
    const indexed = shown.filter(message => deliveryOf(message) === "index").length;
    return `<details data-lane="${lane}" class="akasha-plugin-ui-recall akasha-plugin-ui-recall--${style}">
      <summary><span>${title}</span><b title="编入正文的原文 · 只给索引行的原文 / 本次命中的原文；不代表模型已经接收">已选 ${presented}${indexed ? ` · 索引 ${indexed}` : ""} / ${shown.length}</b></summary>
      <ol class="akasha-plugin-ui-memories">${hits.map((hit) => `<li><div>${hit.messages.filter(message => message.preview).map((message) =>
        `<p><small class="akasha-plugin-ui-evidence" data-form="${escapeHtml(deliveryOf(message))}" title="${escapeHtml(deliveryTitle(message))}">${deliveryLabel(message)} · ${escapeHtml(message.author || "")} · ${escapeHtml(sourceTime(message.recorded_at))}</small>${escapeHtml(message.preview)}${message.truncated ? "…" : ""}</p>`).join("")}</div></li>`).join("")
        || '<li class="akasha-plugin-ui-empty">本次没有命中</li>'}</ol></details>`;
  }).join("")}</div>` : "";
}

export { sourceTime };
