// WCAG 對比計算 —— W0-4(補救計畫 Wave 0)。
//
// 為什麼要有這支:D1/D2 的對比值原本是手算的,而**手算已經錯過一輪**——
// 稽核第一版給出的七個 accent 對比值恆為正確值的 ÷1.347(七個全部同一係數,
// 是換算或分母的系統性錯誤,不是隨機誤差)。結論從「7/7 全部不合格」修正為
// 「5/7 不合格」。這種錯誤只有把演算法寫成程式碼並用 golden 值釘住才不會再犯。
//
// 為什麼不用現成套件:air-gapped 環境的供應鏈紀律(見補救計畫 W3-12j2)——
// 每一個新依賴都是成本,而這段數學是封閉的、可驗證的、五十行以內。
//
// 支援輸入格式:`#rgb` / `#rrggbb` / `oklch(L C H)`(tokens.css 全部用這兩種)。

// ── oklch → linear sRGB ───────────────────────────────────────────────────
// 參考 Björn Ottosson 的 OKLab 定義。oklch 的 L 是 0–1(tokens.css 用小數),
// C 是 chroma,H 是度數。
function oklchToLinearSrgb(L, C, Hdeg) {
  const h = (Hdeg * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);

  // OKLab → LMS'(非線性)
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;

  // LMS' → LMS
  const l = l_ * l_ * l_;
  const m = m_ * m_ * m_;
  const s = s_ * s_ * s_;

  // LMS → linear sRGB
  return [
    +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
}

/** sRGB gamma channel (0–255) → linear (0–1)。WCAG 2.x 的定義。 */
function srgbChannelToLinear(c255) {
  const c = c255 / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function hexToLinearSrgb(hex) {
  let h = hex.trim().replace(/^#/, "");
  if (h.length === 3) h = h.split("").map((ch) => ch + ch).join("");
  if (!/^[0-9a-fA-F]{6}$/.test(h)) throw new Error(`Unsupported hex color: ${hex}`);
  return [
    srgbChannelToLinear(parseInt(h.slice(0, 2), 16)),
    srgbChannelToLinear(parseInt(h.slice(2, 4), 16)),
    srgbChannelToLinear(parseInt(h.slice(4, 6), 16)),
  ];
}

const OKLCH_RE = /^oklch\(\s*([\d.]+%?)\s+([\d.]+)\s+([\d.]+)(?:deg)?\s*(?:\/.*)?\)$/i;

/**
 * 任一支援格式 → linear sRGB triple(已 clamp 到 [0,1] 做 gamut 裁切)。
 * @param {string} value
 * @returns {[number, number, number]}
 */
export function toLinearSrgb(value) {
  const v = String(value).trim();
  if (v.startsWith("#")) return hexToLinearSrgb(v);

  const m = OKLCH_RE.exec(v);
  if (!m) throw new Error(`Unsupported color format: ${value}`);
  const L = m[1].endsWith("%") ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
  const rgb = oklchToLinearSrgb(L, parseFloat(m[2]), parseFloat(m[3]));
  // gamut 裁切:超出 sRGB 的部分夾回 [0,1]。對「顯示器上實際看到的顏色」
  // 而言這就是瀏覽器行為,對比值也應以裁切後為準。
  return rgb.map((c) => Math.min(1, Math.max(0, c)));
}

/** WCAG 相對亮度。輸入為 linear sRGB。 */
export function relativeLuminance(value) {
  const [r, g, b] = toLinearSrgb(value);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * WCAG 對比比值(1–21),**完整精度**。順序無關。
 *
 * ⚠ 刻意不四捨五入。第一版在這裡先 round 到小數兩位才回傳,而 `verify.mjs`
 * 直接拿該值比 4.5 / 3.0 門檻 —— 於是實際 4.496:1 會變成 4.50 而**誤判合格**,
 * 恰好低於門檻的新顏色可以整批溜過這個 gate(由 PR #52 的 Codex review 抓到)。
 * 判定一律用完整精度,只有「顯示」與「golden 比對」才 round(見 `ratio2()`)。
 * @returns {number}
 */
export function contrastRatio(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/** 給人看的兩位小數版本 —— 只用於訊息輸出與 golden 比對,不用於判定。 */
export function ratio2(a, b) {
  return Math.round(contrastRatio(a, b) * 100) / 100;
}

/** WCAG 門檻。非文字 UI 元件與狀態指示 = 3:1;正常文字 = 4.5:1。 */
export const THRESHOLD = { text: 4.5, nonText: 3.0 };
