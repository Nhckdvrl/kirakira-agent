/**
 * Verify how the Akasha panel classifies a recalled original.
 *
 * The panel's own modules are not covered by eslint or tsc, so this exercises the
 * delivery mapping directly: the four outcomes a receipt can describe, the
 * ordering used when two real queries hit the same experience, and the fallback
 * for receipts saved before the window rule existed.
 *
 * It renders nothing and touches no workspace. Run from a full checkout:
 *   node scripts/check-recall-panel.mjs
 */

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

if (failures) {
  console.error(`${failures} recall panel check(s) failed`);
  process.exit(1);
}
console.log("recall panel delivery mapping verified");
