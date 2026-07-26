import { readFile } from "node:fs/promises";

import { contrastRatio, ratio2, THRESHOLD } from "./contrast.mjs";
import { PAIRS, checkSourcesMatch } from "./contrast-pairs.mjs";

// ── 1. 必要 token 存在性 + air-gap 自足性(原有檢查,不動)────────────────
const css = await readFile(new URL("../src/tokens.css", import.meta.url), "utf8");
const required = [
  "--anila-color-accent",
  "--anila-color-bg",
  "--anila-color-fg",
  "--anila-font-sans",
  "--anila-font-mono",
  "--anila-radius-md",
];

for (const token of required) {
  if (!css.includes(`${token}:`)) throw new Error(`Missing required token: ${token}`);
}
if (/@import\s|url\s*\(/i.test(css)) {
  throw new Error("Framework-neutral tokens must remain self-contained and air-gap safe");
}
console.log(`Verified ${required.length} required design tokens`);

// ── 1.5 配對表與產品來源一致性(先驗這個,再算對比)────────────────────────
// 沒有這一步,gate 會拿「手抄且可能過時」的常數計算 —— 改了真正的 token 之後
// 對比退化仍然綠燈。由 PR #52 的 Codex review 抓到。
const { readFileSync } = await import("node:fs");
const sourceProblems = checkSourcesMatch((url) => readFileSync(url, "utf8"));
if (sourceProblems.length) {
  for (const p of sourceProblems) console.error(`  ✖ ${p}`);
  throw new Error(
    `對比配對表與產品來源不一致(${sourceProblems.length} 處)。` +
      "請同步 packages/tokens/scripts/contrast-pairs.mjs —— 不同步的話這個 gate " +
      "會拿舊值計算,對比退化不會被抓到。",
  );
}
console.log("Contrast sources: 配對表與 tokens.css / index.html / tweaks.jsx 相符");

// ── 2. WCAG 對比門檻(W0-4 新增)──────────────────────────────────────────
//
// 為什麼這個 gate 必須存在:D1/D2 的對比值原本靠手算,而**手算已經錯過一輪**
// ——七個 accent 值恆為正確值的 ÷1.347(同一係數,系統性錯誤),結論從
// 「7/7 不合格」修正為「5/7 不合格」。把演算法寫成程式碼並在 CI 跑,是唯一
// 能讓這類缺陷不復發的機制。
//
// ratchet 語意:`src/contrast-baseline.json` 是**已知**未達標清單,只准縮不准增。
//   - 清單內變好(達標)→ 提示移除,不 fail(不擋修復)
//   - 清單外出現新的未達標 → fail
//   - 清單內比值退化 → fail
const baselineRaw = await readFile(
  new URL("../src/contrast-baseline.json", import.meta.url),
  "utf8",
);
const baseline = JSON.parse(baselineRaw);
const known = new Map(baseline.failures.map((f) => [f.id, f]));

const newFailures = [];
const regressions = [];
const fixed = [];

for (const pair of PAIRS) {
  // 判定用完整精度(exact),訊息與 baseline 比對用兩位小數(shown)。
  // 兩者分開是因為第一版拿 round 後的值判定,4.496 會變 4.50 而誤判合格。
  const exact = contrastRatio(pair.fg, pair.bg);
  const ratio = ratio2(pair.fg, pair.bg);
  const requiredRatio = THRESHOLD[pair.level];
  if (requiredRatio === undefined) {
    throw new Error(`Unknown level "${pair.level}" on ${pair.id}`);
  }

  const prior = known.get(pair.id);
  if (exact >= requiredRatio) {
    if (prior) fixed.push({ id: pair.id, ratio });
    continue;
  }
  if (!prior) {
    newFailures.push({ id: pair.id, ratio, required: requiredRatio, why: pair.why });
  } else if (ratio < prior.ratio - 0.01) {
    regressions.push({ id: pair.id, ratio, was: prior.ratio });
  }
}

console.log(
  `Contrast: checked ${PAIRS.length} pairs — ` +
    `${known.size} known failures in baseline, ${fixed.length} now passing`,
);
for (const f of fixed) {
  console.log(`  ✔ ${f.id} 現在達標(${f.ratio})→ 請從 contrast-baseline.json 移除`);
}
for (const r of regressions) {
  console.error(`  ✖ REGRESSION ${r.id}: ${r.ratio}(baseline ${r.was})`);
}
for (const f of newFailures) {
  console.error(`  ✖ NEW FAILURE ${f.id}: ${f.ratio} < ${f.required} — ${f.why}`);
}

if (regressions.length || newFailures.length) {
  throw new Error(
    `WCAG contrast gate failed: ${newFailures.length} new, ${regressions.length} regressed。` +
      `門檻:文字 ${THRESHOLD.text}:1、UI 元件與狀態指示 ${THRESHOLD.nonText}:1。`,
  );
}
