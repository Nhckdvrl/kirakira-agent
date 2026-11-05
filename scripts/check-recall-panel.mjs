/**
 * Verify how the Akasha panel classifies a recalled original.
 *
 * The panel's own modules are not covered by eslint or tsc, so this exercises the
 * delivery mapping directly: the four outcomes a receipt can describe, the
 * ordering used when two real queries hit the same experience, and the fallback
 * for receipts saved before the window rule existed.
 *
 * It touches no workspace. Run from a full checkout:
 *   node scripts/check-recall-panel.mjs
 *
 * With --html <path> it also writes a standalone page carrying the same card
 * and the panel's real stylesheets, so the markup can be opened in an actual
 * browser engine. Asserting on strings cannot show that the summary's three
 * column grid separates the lane name from its counts, or that a body reads
 * heavier than an index line; those are the things the page is for.
 */

import { writeFileSync } from "node:fs";
import { readFileSync } from "node:fs";
import {
  deliveryLabel,
  deliveryOf,
  deliveryRank,
  deliveryTitle,
} from "../frontend/plugins/akasha/src/recall-delivery.js";
import { renderRecall } from "../frontend/plugins/akasha/src/recall-card.js";

let failures = 0;

function check(name, actual, expected) {
  const same = JSON.stringify(actual) === JSON.stringify(expected);
  if (!same) {
    failures += 1;
    console.error(`FAIL ${name}\n  expected ${JSON.stringify(expected)}\n  actual   ${JSON.stringify(actual)}`);
  }
  return same;
}

// 1. Each form the receipt states is reported as itself.
for (const form of ["body", "index", "quoted", "candidate"]) {
  check(`form ${form}`, deliveryOf({ form, presented: form === "body" }), form);
}

// 2. An unknown or missing form falls back to the receipt's older boolean, so
//    records written before the window rule keep their original meaning.
check("legacy presented", deliveryOf({ presented: true }), "body");
check("legacy candidate", deliveryOf({ presented: false }), "candidate");
check("unknown form, presented", deliveryOf({ form: "weird", presented: true }), "body");
check("unknown form, not presented", deliveryOf({ form: null, presented: false }), "candidate");

// 3. Merging two queries keeps the richer delivery; a body is never downgraded.
const ranks = ["candidate", "quoted", "index", "body"].map((form) => deliveryRank({ form }));
check("rank ordering", ranks, [...ranks].sort((left, right) => left - right));
check("body outranks index", deliveryRank({ form: "body" }) > deliveryRank({ form: "index" }), true);
check("index outranks quoted", deliveryRank({ form: "index" }) > deliveryRank({ form: "quoted" }), true);
check("quoted outranks candidate", deliveryRank({ form: "quoted" }) > deliveryRank({ form: "candidate" }), true);

// 4. The label distinguishes an excerpt from a whole original, and never claims
//    a body for something that only earned an index line.
check("excerpt label", deliveryLabel({ form: "body", excerpt: { start: 10, end: 90 } }), "入选片段 [10:90]");
check("whole label", deliveryLabel({ form: "body" }), "入选原文");
check("index label", deliveryLabel({ form: "index" }), "仅索引行");
check("quoted label", deliveryLabel({ form: "quoted" }), "原文已在上文");
check("candidate label", deliveryLabel({ form: "candidate" }), "候选 · 未入选");

// 5. Every form explains itself, and none of them claims the model read it.
for (const form of ["body", "index", "quoted", "candidate"]) {
  const title = deliveryTitle({ form, recorded_at: "2026-11-05T00:00:00+00:00" });
  if (!title.includes("2026-11-05T00:00:00+00:00") || title.split("\n").length !== 2) {
    failures += 1;
    console.error(`FAIL title ${form}: ${JSON.stringify(title)}`);
  }
}
check("body title does not promise receipt",
      deliveryTitle({ form: "body" }).includes("不代表模型已经接收"), true);

// 6. The rendered card: every form reaches the markup, and the lane heading
//    counts originals rather than hits, so two numerators cannot exceed the
//    total when one hit carries both a body and an index line.
const card = renderRecall({ items: [{ hits: [
  { score: 1, lane: "dense", sources: [], messages: [
    { message_id: "a", form: "body", preview: "正文", recorded_at: "2026-11-05T00:00:00+00:00",
      author: "user", excerpt: { start: 10, end: 90 } },
    { message_id: "b", form: "index", preview: "索引", recorded_at: "2026-11-05T00:00:00+00:00", author: "user" },
    { message_id: "c", form: "quoted", preview: "上文", recorded_at: "2026-11-05T00:00:00+00:00", author: "user" },
    { message_id: "d", form: "candidate", preview: "候选", recorded_at: "2026-11-05T00:00:00+00:00", author: "user" },
  ] },
] }] });

for (const [form, label] of [["body", "入选片段 [10:90]"], ["index", "仅索引行"],
                             ["quoted", "原文已在上文"], ["candidate", "未入选"]]) {
  if (!card.includes(`data-form="${form}"`) || !card.includes(label)) {
    failures += 1;
    console.error(`FAIL rendered card missing ${form} / ${label}`);
  }
}
check("heading counts originals", card.includes("已选 1 · 索引 1 / 4"), true);

// A preview is escaped, so a recalled original can never inject markup.
const escaped = renderRecall({ items: [{ hits: [{ score: 1, lane: "dense", sources: [], messages: [
  { message_id: "x", form: "body", preview: "<img src=x onerror=alert(1)>",
    recorded_at: "2026-11-05T00:00:00+00:00", author: "user" },
] }] }] });
check("preview escaped", escaped.includes("&lt;img") && !escaped.includes("<img src=x"), true);

// A standalone page for looking at the result in a browser. The panel
// stylesheet @imports the plugin one and both rely on host theme tokens, so a
// page that loads neither shows a collapsed layout the real app never has.
const htmlFlag = process.argv.indexOf("--html");
if (htmlFlag !== -1) {
  const target = process.argv[htmlFlag + 1];
  if (!target) {
    console.error("--html needs a path");
    process.exit(2);
  }
  const here = new URL(".", import.meta.url).pathname;
  const style = readFileSync(`${here}../plugins/akasha/plugin_ui.css`, "utf8") + "\n"
    + readFileSync(`${here}../frontend/plugins/akasha/src/plugin-ui.css`, "utf8")
      .replace(/@import[^;]+;/g, "");
  const at = "2026-11-05T00:00:00+00:00";
  const page = renderRecall({ items: [{ hits: [
    { score: 0.91, lane: "dense", sources: ["direct_dense"], messages: [
      { message_id: "a", form: "body", preview: "预算由 500 更正为 760，必须防水，只在周末施工。",
        recorded_at: at, author: "user", excerpt: { start: 10, end: 90 } },
      { message_id: "b", form: "index", preview: "雪松资料归档完整报告。本报告以归档栏登记正式签收凭证号。",
        recorded_at: at, author: "user" },
      { message_id: "c", form: "quoted", preview: "这条原文此刻仍逐字留在本次请求里。",
        recorded_at: at, author: "user" },
      { message_id: "d", form: "candidate", preview: "候选但未入选的经历。",
        recorded_at: at, author: "assistant" },
    ] },
    { score: 0.42, lane: "completion", sources: ["graph_completion"], messages: [
      { message_id: "e", form: "index", preview: "另一条只拿到索引行的原文，预览被截断于此",
        recorded_at: at, author: "user", truncated: true },
    ] },
  ] }] });
  writeFileSync(target, `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Akasha 召回卡片</title><style>
:root {
  --ak-color-text-primary: #16181d; --ak-color-text-secondary: #5c6370;
  --ak-color-bg-canvas: #ffffff; --ak-color-bg-surface: #f7f8fa;
  --ak-color-action-primary: #2f6fed; --ak-color-on-action-container: #0b2b6b;
  --ak-color-status-error: #c0392b; --ak-color-status-trace: #7a5af5;
  --chat-muted: #5c6370; --chat-chip: #eef0f4;
}
body { margin: 0; padding: 24px; width: 760px; font: 14px/1.6 system-ui, sans-serif;
       background: var(--ak-color-bg-canvas); color: var(--ak-color-text-primary); }
${style}
</style></head><body>
<p>折叠态</p>${page}
<p>展开态</p>${page.replaceAll("<details", "<details open")}
</body></html>
`);
  console.log(`wrote ${target}`);
}

if (failures) {
  console.error(`${failures} recall panel check(s) failed`);
  process.exit(1);
}
console.log("recall panel delivery mapping verified");
