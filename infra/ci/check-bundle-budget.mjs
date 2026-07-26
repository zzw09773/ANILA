#!/usr/bin/env node
// Bundle 預算檢查 —— W0-5(補救計畫 Wave 0)。
//
// 為什麼需要:`apps/anila-shell` 的 entry chunk 是 967 kB,Vite 的 500 kB 警告
// **只是印出來、沒人管**(vite.config.js 無 manualChunks、也沒調
// chunkSizeWarningLimit)。CI 會跑 `npm run build`,但 Vite 警告不會 fail build,
// 所以 gate 攔不到。這支腳本把警告變成硬門檻。
//
// 語意:預算凍結在**現值**,只准降不准升(ratchet)。縮減 bundle 屬 Wave 3 的
// React.lazy / manualChunks 工作,不在本包 —— 本包只負責「不准再變大」。
//
// 用法:node infra/ci/check-bundle-budget.mjs <app-dir>
//   app-dir 需含 dist/ 與 bundle-budget.json

import { readFile, readdir, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { exit, argv } from "node:process";

const appDir = resolve(argv[2] ?? ".");
const distDir = join(appDir, "dist");
const budgetPath = join(appDir, "bundle-budget.json");

/** 遞迴收集 dist 下所有 .js 的位元組數。 */
async function collectJs(dir) {
  const out = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    const full = join(dir, e.name);
    if (e.isDirectory()) {
      out.push(...(await collectJs(full)));
    } else if (e.name.endsWith(".js")) {
      const s = await stat(full);
      out.push({ name: e.name, bytes: s.size });
    }
  }
  return out;
}

const files = await collectJs(distDir);
if (files.length === 0) {
  console.error(`No .js found under ${distDir} — 先跑 npm run build`);
  exit(2);
}

let budget;
try {
  budget = JSON.parse(await readFile(budgetPath, "utf8"));
} catch {
  console.error(`Missing or invalid ${budgetPath}`);
  exit(2);
}

const totalBytes = files.reduce((a, f) => a + f.bytes, 0);
const largest = files.reduce((a, f) => (f.bytes > a.bytes ? f : a), files[0]);
const kb = (b) => Math.round(b / 102.4) / 10; // → kB(一位小數)

const problems = [];
if (totalBytes > budget.totalJsBytes) {
  problems.push(
    `總 JS ${kb(totalBytes)} kB 超過預算 ${kb(budget.totalJsBytes)} kB ` +
      `(+${kb(totalBytes - budget.totalJsBytes)} kB)`,
  );
}
if (largest.bytes > budget.largestChunkBytes) {
  problems.push(
    `最大 chunk「${largest.name}」${kb(largest.bytes)} kB 超過預算 ` +
      `${kb(budget.largestChunkBytes)} kB (+${kb(largest.bytes - budget.largestChunkBytes)} kB)`,
  );
}

console.log(
  `Bundle: ${files.length} 個 JS,總計 ${kb(totalBytes)} kB / 預算 ${kb(budget.totalJsBytes)} kB;` +
    `最大 chunk ${largest.name} ${kb(largest.bytes)} kB / 預算 ${kb(budget.largestChunkBytes)} kB`,
);

if (problems.length) {
  for (const p of problems) console.error(`  ✖ ${p}`);
  console.error(
    "\nbundle 變大要嘛是真的加了東西(請說明並更新 bundle-budget.json 且附理由)," +
      "要嘛是誤加依賴。air-gapped 環境的每一 kB 都要隨 bundle 重打包進內網。",
  );
  exit(1);
}

// 明顯縮小時提示可以下修預算(ratchet 往下走)
const slackTotal = budget.totalJsBytes - totalBytes;
if (slackTotal > budget.totalJsBytes * 0.05) {
  console.log(
    `  ✔ 總量比預算小 ${kb(slackTotal)} kB(>5%)→ 可考慮下修 bundle-budget.json`,
  );
}
