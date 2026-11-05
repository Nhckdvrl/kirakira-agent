/** 召回经历的交付形态分类；纯逻辑，不引样式，便于单独核对。 */

import { escapeHtml } from "./escape.js";

// 一条经历实际怎样到达这次请求：编入正文、只给一行索引、原文已在上文因此
// 不再注入，或者只是候选。旧回执没有 form 字段，按原来的布尔值解释。
const DELIVERY_RANK = { body: 3, index: 2, quoted: 1, candidate: 0 };

export function deliveryOf(message) {
  const form = message.form;
  if (form === "body" || form === "index" || form === "quoted" || form === "candidate") return form;
  return message.presented ? "body" : "candidate";
}

export function deliveryRank(message) {
  return DELIVERY_RANK[deliveryOf(message)] ?? 0;
}

export function deliveryLabel(message) {
  switch (deliveryOf(message)) {
    case "body":
      return message.excerpt
        ? `入选片段 [${escapeHtml(message.excerpt.start)}:${escapeHtml(message.excerpt.end)}]`
        : "入选原文";
    case "index":
      return "仅索引行";
    case "quoted":
      return "原文已在上文";
    default:
      return "候选 · 未入选";
  }
}

export function deliveryTitle(message) {
  const recorded = message.recorded_at || "";
  switch (deliveryOf(message)) {
    case "index":
      return `${recorded}\n本次只注入一行索引；原文已在当前对话里，可用 read_memory_source 展开`;
    case "quoted":
      return `${recorded}\n原文已在当前对话里，本次没有再注入，预算留给看不到的内容`;
    case "body":
      return `${recorded}\n本次编入召回材料；不代表模型已经接收`;
    default:
      return `${recorded}\n检索候选，本次没有编入材料`;
  }
}

