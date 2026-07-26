// W0-4 驗收②:把 D1 七個 accent 的對比值當 golden 釘住演算法。
//
// 為什麼需要 golden 而不只是「有跑就好」:這些數字**已經錯過一輪**——稽核
// 第一版的七個值恆為正確值的 ÷1.347(七個全部同一係數 → 系統性換算錯誤),
// 導致結論從「5/7 不合格」誤報成「7/7 全滅」。演算法若哪天被人「優化」壞了,
// golden 就是第一個爆點。
//
// golden 來源:對抗式驗證(07.md)獨立重算的表,已由本檔演算法逐項複現。

import test from "node:test";
import assert from "node:assert/strict";

import { contrastRatio, ratio2, relativeLuminance, toLinearSrgb, THRESHOLD } from "../scripts/contrast.mjs";
import { PAIRS, ACCENTS, DARK_BG, LIGHT_BG } from "../scripts/contrast-pairs.mjs";

// ── 基本正確性:用 WCAG 定義的已知端點校準 ────────────────────────────────
test("純黑與純白的對比是 21:1(WCAG 的理論最大值)", () => {
  assert.equal(ratio2("#000000", "#ffffff"), 21);
});

test("同色對比是 1:1", () => {
  assert.equal(ratio2("#2b4c7e", "#2b4c7e"), 1);
});

test("相對亮度:黑 0、白 1", () => {
  assert.equal(relativeLuminance("#000000"), 0);
  assert.equal(relativeLuminance("#ffffff"), 1);
});

test("對比與順序無關", () => {
  assert.equal(ratio2("#2b4c7e", LIGHT_BG), ratio2(LIGHT_BG, "#2b4c7e"));
});

test("三碼 hex 等於展開後的六碼", () => {
  assert.equal(relativeLuminance("#fff"), relativeLuminance("#ffffff"));
  assert.equal(relativeLuminance("#08f"), relativeLuminance("#0088ff"));
});

test("oklch 解析:白與黑的極端值落在預期範圍", () => {
  // oklch(1 0 0) = 白;oklch(0 0 0) = 黑
  assert.ok(relativeLuminance("oklch(1 0 0)") > 0.99);
  assert.ok(relativeLuminance("oklch(0 0 0)") < 0.01);
});

test("超出 sRGB 色域的 oklch 會被夾回 [0,1](與瀏覽器行為一致)", () => {
  const rgb = toLinearSrgb("oklch(0.6 0.4 20)"); // 高 chroma,必然出界
  for (const c of rgb) {
    assert.ok(c >= 0 && c <= 1, `channel ${c} 應落在 [0,1]`);
  }
});

test("不支援的格式要明確報錯,不要靜默給錯數字", () => {
  assert.throws(() => toLinearSrgb("rgb(1,2,3)"), /Unsupported color format/);
  assert.throws(() => toLinearSrgb("#12345"), /Unsupported hex color/);
});

// ── D1 golden:七個 accent 對深色底 ───────────────────────────────────────
const D1_GOLDEN = {
  official: 2.25,
  teal: 3.48,
  slate: 1.87,
  moss: 2.96,
  clay: 3.69,
  indigo: 2.44,
  crimson: 2.60,
};

test("D1 golden:七個 accent 對深色底的對比逐項相符(±0.01)", () => {
  for (const accent of ACCENTS) {
    const actual = ratio2(accent.value, DARK_BG);
    const golden = D1_GOLDEN[accent.name];
    assert.ok(golden !== undefined, `accent ${accent.name} 缺 golden 值`);
    assert.ok(
      Math.abs(actual - golden) <= 0.01,
      `${accent.name}: ${actual} vs golden ${golden}`,
    );
  }
});

test("D1 結論:深色底下 5/7 未達 3:1(**不是** 7/7,原稽核誤報)", () => {
  const failing = ACCENTS.filter((a) => contrastRatio(a.value, DARK_BG) < THRESHOLD.nonText);
  assert.equal(failing.length, 5);
  // teal 與 clay 是通過的那兩個
  const passing = ACCENTS.filter((a) => contrastRatio(a.value, DARK_BG) >= THRESHOLD.nonText)
    .map((a) => a.name)
    .sort();
  assert.deepEqual(passing, ["clay", "teal"]);
});

test("D1:七個 accent 對**淺色**底全部達標 → 問題出在 dark 段沒覆寫 --accent", () => {
  for (const a of ACCENTS) {
    assert.ok(
      contrastRatio(a.value, LIGHT_BG) >= THRESHOLD.nonText,
      `${a.name} 在淺色底應達標`,
    );
  }
});

// ── D2 golden:先前標記「未經驗證」的三值,實測完全相符 ────────────────────
test("D2 golden:fg-subtle / warn / success 三值與原稽核相符(該三值先前被標為未驗證)", () => {
  const expected = { "fg-subtle/light": 3.41, "warn/light": 2.55, "success/light": 4.16 };
  for (const [id, golden] of Object.entries(expected)) {
    const pair = PAIRS.find((p) => p.id === id);
    assert.ok(pair, `找不到配對 ${id}`);
    const actual = ratio2(pair.fg, pair.bg);
    assert.ok(Math.abs(actual - golden) <= 0.01, `${id}: ${actual} vs golden ${golden}`);
  }
});

// ── 門檻判定不得先四捨五入 ────────────────────────────────────────────────
test("contrastRatio 回傳完整精度,ratio2 才是顯示用的兩位小數", () => {
  // 找一個小數位夠多的實際配對來證明兩者確實不同
  const exact = contrastRatio("oklch(0.62 0.014 260)", LIGHT_BG); // fg-subtle
  const shown = ratio2("oklch(0.62 0.014 260)", LIGHT_BG);
  assert.notEqual(exact, shown, "contrastRatio 不該回傳已 round 的值");
  assert.equal(Math.round(exact * 100) / 100, shown);
});

test("恰好低於門檻的值必須被判為不合格(不可因 round 而放行)", () => {
  // 這條釘住 Codex 在 PR #52 指出的漏洞:第一版 contrastRatio 先 round 到
  // 兩位小數才回傳,而 verify.mjs 直接拿它比 4.5 —— 於是實際 4.496 變成 4.50
  // 而誤判合格,恰好低於 WCAG 門檻的新顏色可以整批溜過 gate。
  const justUnder = 4.4996;
  const rounded = Math.round(justUnder * 100) / 100; // → 4.5
  assert.equal(rounded, 4.5, "前提:這個值 round 後恰好等於門檻");
  assert.ok(justUnder < THRESHOLD.text, "完整精度下必須是不合格");
  assert.ok(!(rounded < THRESHOLD.text), "round 後會被誤判為合格 —— 這正是漏洞");
});

// ── 配對表自身的完整性 ────────────────────────────────────────────────────
test("配對表每一列都有合法 level 與說明", () => {
  for (const p of PAIRS) {
    assert.ok(THRESHOLD[p.level] !== undefined, `${p.id} 的 level 非法:${p.level}`);
    assert.ok(p.why && p.why.trim().length > 0, `${p.id} 缺說明`);
  }
});

test("配對表沒有重複 id(重複會讓 baseline ratchet 失效)", () => {
  const ids = PAIRS.map((p) => p.id);
  assert.equal(new Set(ids).size, ids.length);
});
