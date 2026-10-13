// 翻譯忠實度檢查：比對「原文」和「譯文」。
// 數字、單位、否定詞、譯名是否前後一致，都由這裡的程式判定。
// 模型只負責翻譯，不參與判定，也不看引用來源。

export const MODE_READING = "reading";
export const MODE_EXTERNAL = "external";

export const MODE_LABELS = Object.freeze({
  [MODE_READING]: "快速閱讀",
  [MODE_EXTERNAL]: "對外使用",
});

export const TARGET_LANGUAGES = Object.freeze([
  { id: "zh-Hant-TW", label: "繁體中文（臺灣）" },
  { id: "en", label: "英文" },
  { id: "ja", label: "日文" },
]);

/** 合成例句。數字、否定、單位、譯名各錯一處，方便在沒有模型時看到紅框。 */
export const SYNTHETIC_EXAMPLE = Object.freeze({
  note: "合成範例，不是真實文件。",
  source: "Sensor calibration must finish within 12 hours. Do not skip the sensor calibration. The spare unit weighs 5 kg.",
  translation: "感測器校正必須在 13 小時內完成。可以略過感測器校準。備品重量為 5 g。",
});

const HAN_DIGIT = {
  零: 0, 〇: 0, 一: 1, 二: 2, 兩: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9,
};
const HAN_SMALL = { 十: 10, 百: 100, 千: 1000 };
const HAN_BIG = { 萬: 10000, 億: 100000000 };

const NUMBER_RE = /百分之\s*[0-9０-９]+(?:\.[0-9０-９]+)?|百分之\s*[零〇一二三四五六七八九十百千萬億兩]{1,12}|[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+(?:\.[0-9０-９]+)?|[0-9０-９]+(?:\.[0-9０-９]+)?|[零〇一二三四五六七八九十百千萬億兩]{1,12}/g;

const UNIT_FORMS = [
  ["celsius", ["degrees celsius", "degree celsius", "°c", "℃", "celsius", "攝氏"]],
  ["fahrenheit", ["degrees fahrenheit", "degree fahrenheit", "°f", "℉", "fahrenheit", "華氏"]],
  ["kg", ["kilograms", "kilogram", "公斤", "千克", "kg"]],
  ["mg", ["milligrams", "milligram", "毫克", "mg"]],
  ["g", ["grams", "gram", "公克", "克", "g"]],
  ["km", ["kilometers", "kilometres", "kilometer", "kilometre", "公里", "km"]],
  ["cm", ["centimeters", "centimeter", "公分", "厘米", "cm"]],
  ["mm", ["millimeters", "millimeter", "公釐", "毫米", "mm"]],
  ["ml", ["milliliters", "millilitres", "milliliter", "millilitre", "毫升", "ml"]],
  ["hour", ["hours", "hour", "個小時", "小時", "hrs", "hr"]],
  ["minute", ["minutes", "minute", "分鐘", "min"]],
  ["second", ["seconds", "second", "秒鐘", "秒", "sec"]],
  ["day", ["days", "day", "天"]],
  ["dom", ["日"]],
  ["week", ["weeks", "week", "星期", "週", "周"]],
  ["month", ["months", "month", "個月"]],
  ["year", ["years", "year", "年"]],
  ["twd", ["新臺幣", "新台幣", "元"]],
  ["usd", ["美元", "usd"]],
  ["m", ["meters", "metres", "meter", "metre", "公尺", "米", "m"]],
  ["l", ["liters", "litres", "liter", "litre", "公升", "升", "l"]],
  ["degree", ["度"]],
];

const UNIT_MATCHES = UNIT_FORMS
  .flatMap(([id, forms]) => forms.map((form) => ({ id, form: form.toLowerCase() })))
  .sort((a, b) => b.form.length - a.form.length);

const UNIT_SURFACE = new Set(UNIT_MATCHES.map((item) => item.form));

const OPTIONAL_UNITS = new Set(["year", "dom"]);

const COUNTERS = ["個", "次", "名", "位", "項", "條", "件", "份", "倍", "張", "組", "台", "臺"];

const EN_NEGATION = [
  "cannot", "can't", "can not",
  "do not", "don't", "does not", "doesn't", "did not", "didn't",
  "will not", "won't", "is not", "isn't", "are not", "aren't",
  "was not", "wasn't", "were not", "weren't",
  "would not", "wouldn't", "should not", "shouldn't", "could not", "couldn't",
  "must not", "mustn't", "have not", "haven't", "has not", "hasn't", "had not", "hadn't",
  "nothing", "nobody", "nowhere", "neither", "never", "without", "none", "nor", "not", "no",
].sort((a, b) => b.length - a.length);

const EN_NEGATION_RE = new RegExp(`\\b(?:${EN_NEGATION.map(escapeRegExp).join("|")})\\b`, "gi");

const ZH_SKIP = ["不得不", "不得已", "非常", "未來", "無論", "非洲", "非凡", "別人", "別的", "特別"];
const ZH_NEGATION = [
  "並不是", "並沒有", "並非", "並未", "尚未", "從未", "未曾", "沒有",
  "不可", "不能", "不會", "不必", "不准", "不許", "不要", "不得", "不是",
  "禁止", "勿", "別", "未", "無", "非", "不", "沒",
].sort((a, b) => b.length - a.length);

const JA_NEGATION = ["ではない", "じゃない", "ません", "ない", "なし"].sort((a, b) => b.length - a.length);

const EN_STOP = new Set(`the a an of to and or in on for with by from at as is are was were be been
do does did this that these those it its not no nor never within after before must should can could
will would into onto over than then so if but also per via without under`.split(/\s+/));

const ZH_STOP = new Set(`的 了 是 在 與 和 或 及 並 就 都 而 被 把 對 從 以 為 有 這 那
可以 必須 應該 已經 內 之後 以前 以及 請 你 我 他 她 它 們
不要 不得 不可 不能 不會 不必 不准 不許 沒有 並非 並未 尚未 從未 禁止 勿
這個 一個 我們 他們 因此 如果 因為 所以 但是 而且 或者 還是 就是`.split(/\s+/));

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function asciiDigits(value) {
  return value.replace(/[０-９]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0));
}

function parseChineseNumber(raw) {
  let total = 0;
  let section = 0;
  let num = 0;
  for (const ch of raw) {
    if (Object.prototype.hasOwnProperty.call(HAN_DIGIT, ch)) {
      num = HAN_DIGIT[ch];
    } else if (Object.prototype.hasOwnProperty.call(HAN_SMALL, ch)) {
      section += (num || 1) * HAN_SMALL[ch];
      num = 0;
    } else if (Object.prototype.hasOwnProperty.call(HAN_BIG, ch)) {
      section += num;
      total += (section || 1) * HAN_BIG[ch];
      section = 0;
      num = 0;
    } else {
      return null;
    }
  }
  return total + section + num;
}

function matchUnit(text, index) {
  let i = index;
  const limit = Math.min(text.length, index + 4);
  while (i < limit && /\s/.test(text[i])) i += 1;
  const rest = text.slice(i);
  const lower = rest.toLowerCase();
  for (const candidate of UNIT_MATCHES) {
    if (!lower.startsWith(candidate.form)) continue;
    const after = rest[candidate.form.length] || "";
    const formEndsWithLetter = /[a-z]$/i.test(candidate.form);
    if (formEndsWithLetter && /[a-z]/i.test(after)) continue;
    return { id: candidate.id, end: i + candidate.form.length };
  }
  return null;
}

function matchCounter(text, index) {
  let i = index;
  const limit = Math.min(text.length, index + 2);
  while (i < limit && /\s/.test(text[i])) i += 1;
  const rest = text.slice(i);
  const found = COUNTERS.find((counter) => rest.startsWith(counter));
  return found ? i + found.length : 0;
}

function canonicalNumber(raw, percent) {
  const numeric = raw.replace(/^百分之\s*/, "");
  let value;
  if (/[零〇一二三四五六七八九十百千萬億兩]/.test(numeric)) {
    const hasMagnitude = /[十百千萬億]/.test(numeric);
    const single = [...numeric].length === 1;
    if (!hasMagnitude && !single) return null;
    value = parseChineseNumber(numeric);
  } else {
    value = Number(asciiDigits(numeric).replace(/[,，]/g, ""));
  }
  if (!Number.isFinite(value)) return null;
  const body = Number.isInteger(value) ? String(value) : String(value);
  return percent ? `${body}%` : body;
}

function trimSpan(text, start, end) {
  let s = start;
  let e = end;
  while (s < e && /\s/.test(text[s])) s += 1;
  while (e > s && /\s/.test(text[e - 1])) e -= 1;
  return { start: s, end: e, raw: text.slice(s, e) };
}

function extractMeasures(text) {
  const items = [];
  const re = new RegExp(NUMBER_RE.source, "g");
  let match;
  while ((match = re.exec(text))) {
    const token = match[0];
    const start = match.index;
    const percent = token.startsWith("百分之") || text.slice(match.index + token.length).startsWith("%")
      || text.slice(match.index + token.length).startsWith("％");
    const numberKey = canonicalNumber(token, percent);
    if (!numberKey) continue;
    let end = start + token.length;
    if (!token.startsWith("百分之") && (text[end] === "%" || text[end] === "％")) end += 1;
    const suffix = matchUnit(text, end);
    let unit = suffix ? suffix.id : null;
    if (suffix) end = suffix.end;
    const prefix = text.slice(Math.max(0, start - 4), start);
    if (prefix.includes("攝氏") && (unit == null || unit === "degree")) unit = "celsius";
    if (prefix.includes("華氏") && (unit == null || unit === "degree")) unit = "fahrenheit";
    const chinese = /[零〇一二三四五六七八九十百千萬億兩]/.test(token.replace(/^百分之\s*/, ""));
    if (chinese && !token.startsWith("百分之")) {
      const bare = token.replace(/^百分之\s*/, "");
      const hasMagnitude = /[十百千萬億]/.test(bare);
      const ordinal = text.slice(Math.max(0, start - 1), start) === "第";
      const counterEnd = matchCounter(text, end);
      if (!hasMagnitude && !ordinal && !suffix && !counterEnd && !percent) continue;
    }
    items.push({ numberKey, unit, ...trimSpan(text, start, end) });
  }
  return items;
}

function optionalUnitGap(left, right) {
  const oneEmpty = (left == null) !== (right == null);
  return oneEmpty && OPTIONAL_UNITS.has(left || right);
}

function measureMismatches(source, translation) {
  const src = extractMeasures(source).map((item) => ({ ...item, used: false }));
  const tgt = extractMeasures(translation).map((item) => ({ ...item, used: false }));
  const mismatches = [];

  for (const item of src) {
    const peer = tgt.find((other) => !other.used && other.numberKey === item.numberKey && other.unit === item.unit);
    if (!peer) continue;
    item.used = true;
    peer.used = true;
  }

  for (const item of src) {
    if (item.used) continue;
    const peer = tgt.find((other) => !other.used && other.numberKey === item.numberKey && other.unit !== item.unit);
    if (!peer) continue;
    item.used = true;
    peer.used = true;
    if (optionalUnitGap(item.unit, peer.unit)) continue;
    mismatches.push({
      kind: "unit",
      message: `譯文的「${peer.raw}」和原文的「${item.raw}」單位不同。`,
      translationStart: peer.start,
      translationEnd: peer.end,
    });
  }

  for (const item of src) {
    if (item.used) continue;
    const peer = tgt.find((other) => !other.used && other.unit === item.unit && other.numberKey !== item.numberKey);
    if (!peer) continue;
    item.used = true;
    peer.used = true;
    mismatches.push({
      kind: "number",
      message: `譯文的「${peer.raw}」對不上原文的「${item.raw}」。`,
      translationStart: peer.start,
      translationEnd: peer.end,
    });
  }

  for (const item of src) {
    if (item.used) continue;
    mismatches.push({
      kind: "number",
      message: `原文的「${item.raw}」沒有出現在譯文。`,
      translationStart: null,
      translationEnd: null,
    });
  }
  for (const item of tgt) {
    if (item.used) continue;
    mismatches.push({
      kind: "number",
      message: `譯文多了原文沒有的「${item.raw}」。`,
      translationStart: item.start,
      translationEnd: item.end,
    });
  }
  return mismatches;
}

function countEnglishNegations(text) {
  const re = new RegExp(EN_NEGATION_RE.source, "gi");
  let count = 0;
  let match;
  while ((match = re.exec(text))) {
    if (match[0].toLowerCase() === "no" && /^\.?\s*[0-9０-９]/.test(text.slice(match.index + match[0].length))) {
      continue;
    }
    count += 1;
  }
  return count;
}

function countListedNegations(text, phrases, skips = []) {
  let count = 0;
  for (let i = 0; i < text.length;) {
    const skip = skips.find((phrase) => text.startsWith(phrase, i));
    if (skip) {
      i += skip.length;
      continue;
    }
    const hit = phrases.find((phrase) => text.startsWith(phrase, i));
    if (hit) {
      count += 1;
      i += hit.length;
      continue;
    }
    i += 1;
  }
  return count;
}

function negationCount(text) {
  return countEnglishNegations(text)
    + countListedNegations(text, ZH_NEGATION, ZH_SKIP)
    + countListedNegations(text, JA_NEGATION);
}

function splitSentences(text) {
  const out = [];
  let start = 0;
  const isDigit = (ch) => /[0-9０-９]/.test(ch || "");
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const englishStop = (ch === "." || ch === "!" || ch === "?")
      && !isDigit(text[i - 1])
      && !isDigit(text[i + 1])
      && (i === text.length - 1 || /\s/.test(text[i + 1]));
    const boundary = ch === "\n" || "。！？!?；;".includes(ch) || englishStop;
    if (!boundary) continue;
    const piece = trimSpan(text, start, i + 1);
    if (piece.raw) out.push(piece);
    start = i + 1;
  }
  if (start < text.length) {
    const piece = trimSpan(text, start, text.length);
    if (piece.raw) out.push(piece);
  }
  return out;
}

function alignSentences(source, translation) {
  const src = splitSentences(source);
  const tgt = splitSentences(translation);
  if (src.length === tgt.length) {
    return src.map((item, index) => ({ source: item, translation: tgt[index] }));
  }
  const used = new Set();
  const pairs = [];
  for (const item of src) {
    const sourceKeys = new Set(extractMeasures(item.raw).map((measure) => measure.numberKey));
    let best = -1;
    let bestScore = 0;
    tgt.forEach((other, index) => {
      if (used.has(index)) return;
      const shared = extractMeasures(other.raw).filter((measure) => sourceKeys.has(measure.numberKey)).length;
      if (shared > bestScore) {
        bestScore = shared;
        best = index;
      }
    });
    if (best >= 0 && bestScore > 0) {
      used.add(best);
      pairs.push({ source: item, translation: tgt[best] });
    }
  }
  return pairs;
}

function clip(text) {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 48 ? `${flat.slice(0, 47)}…` : flat;
}

function negationMismatches(pairs) {
  const mismatches = [];
  for (const pair of pairs) {
    const sourcePolarity = negationCount(pair.source.raw) % 2;
    const translationPolarity = negationCount(pair.translation.raw) % 2;
    if (sourcePolarity === translationPolarity) continue;
    mismatches.push({
      kind: "negation",
      message: `這句的否定和原文相反。原文：「${clip(pair.source.raw)}」`,
      translationStart: pair.translation.start,
      translationEnd: pair.translation.end,
    });
  }
  return mismatches;
}

function segmenter() {
  if (typeof Intl === "undefined" || typeof Intl.Segmenter !== "function") return null;
  return new Intl.Segmenter("zh-Hant", { granularity: "word" });
}

function rawWords(text) {
  const seg = segmenter();
  if (!seg) return [];
  return [...seg.segment(text)]
    .filter((part) => part.isWordLike)
    .map((part) => ({ text: part.segment, index: part.index }));
}

function glueHan(words) {
  const out = [];
  let buf = null;
  const flush = () => {
    if (!buf) return;
    out.push(buf);
    buf = null;
  };
  for (const word of words) {
    const singleHan = [...word.text].length === 1 && /^\p{Script=Han}$/u.test(word.text);
    if (singleHan) {
      if (!buf) buf = { text: word.text, index: word.index };
      else buf.text += word.text;
    } else {
      flush();
      out.push({ text: word.text, index: word.index });
    }
  }
  flush();
  return out;
}

function normTerm(text) {
  const value = text.trim();
  if (!value) return "";
  if (/^[A-Za-z][A-Za-z'-]*$/.test(value)) return value.toLowerCase();
  return value;
}

function contentTokens(text) {
  return glueHan(rawWords(text)).map((word) => normTerm(word.text)).filter((token) => {
    if (!token) return false;
    if (EN_STOP.has(token) || ZH_STOP.has(token) || UNIT_SURFACE.has(token.toLowerCase())) return false;
    if (/^[0-9]+(?:\.[0-9]+)?%?$/.test(token)) return false;
    const latin = /^[a-z][a-z'-]*$/i.test(token);
    if (latin) return token.length >= 4;
    return [...token].length >= 2;
  });
}

export function terminologyAvailable() {
  return segmenter() != null;
}

function unsettledTerms(pairs) {
  if (!terminologyAvailable()) return [];
  const rows = pairs.map((pair) => ({
    source: contentTokens(pair.source.raw),
    translation: contentTokens(pair.translation.raw),
  }));
  const counts = new Map();
  for (const row of rows) {
    for (const token of row.source) counts.set(token, (counts.get(token) || 0) + 1);
  }
  const glosses = new Map();
  for (const row of rows) {
    row.source.forEach((token, index) => {
      if ((counts.get(token) || 0) < 2) return;
      if (!row.translation.length) return;
      const target = ((index + 0.5) / row.source.length) * row.translation.length;
      let best = null;
      let dist = Infinity;
      row.translation.forEach((gloss, glossIndex) => {
        const gap = Math.abs(glossIndex + 0.5 - target);
        if (gap < dist) {
          dist = gap;
          best = gloss;
        }
      });
      // 對齊偏了一格時，兩個譯名往往同時出現在兩句裡。那種情況不算兩種譯法。
      if (best == null || dist > 0.75) return;
      if (!glosses.has(token)) glosses.set(token, []);
      glosses.get(token).push({ gloss: best, tokens: row.translation });
    });
  }
  const out = [];
  for (const [term, records] of glosses) {
    if (records.length < 2) continue;
    let conflict = false;
    for (let i = 0; i < records.length; i += 1) {
      for (let j = i + 1; j < records.length; j += 1) {
        if (records[i].gloss === records[j].gloss) continue;
        const otherHasThis = records[j].tokens.includes(records[i].gloss);
        const thisHasOther = records[i].tokens.includes(records[j].gloss);
        if (!otherHasThis && !thisHasOther) conflict = true;
      }
    }
    if (!conflict) continue;
    const variants = [...new Set(records.map((record) => record.gloss))].sort((a, b) => a.localeCompare(b, "zh-Hant"));
    if (variants.length < 2) continue;
    out.push({
      term,
      variants,
      message: `「${term}」有兩種譯法：${variants.join("、")}。標為未定譯。`,
    });
  }
  return out;
}

/**
 * 比對原文和譯文。回傳的紅框座標落在譯文上；對不上但譯文沒有對應片段時，座標是 null。
 * @param {string} source
 * @param {string} translation
 */
export function checkTranslation(source, translation) {
  const left = typeof source === "string" ? source : "";
  const right = typeof translation === "string" ? translation : "";
  if (!left.trim() || !right.trim()) {
    return { mismatches: [], unsettled: [], terminologyChecked: terminologyAvailable() };
  }
  const pairs = alignSentences(left, right);
  return {
    mismatches: [...measureMismatches(left, right), ...negationMismatches(pairs)],
    unsettled: unsettledTerms(pairs),
    terminologyChecked: terminologyAvailable(),
  };
}

/** 把譯文切成一般片段和要畫紅框的片段。重疊的對不上會併在同一段。 */
export function annotateTranslation(translation, mismatches) {
  const text = typeof translation === "string" ? translation : "";
  const marks = (mismatches || []).filter((item) =>
    Number.isInteger(item.translationStart)
    && Number.isInteger(item.translationEnd)
    && item.translationEnd > item.translationStart);
  const cuts = new Set([0, text.length]);
  for (const mark of marks) {
    cuts.add(Math.max(0, Math.min(text.length, mark.translationStart)));
    cuts.add(Math.max(0, Math.min(text.length, mark.translationEnd)));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const segments = [];
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i];
    const end = points[i + 1];
    if (end <= start) continue;
    const hits = marks.filter((mark) => mark.translationStart < end && mark.translationEnd > start);
    segments.push({ text: text.slice(start, end), start, end, hits });
  }
  return segments;
}

/** 只要求完整翻譯。不請模型檢查數字、單位、否定或譯名。 */
export function buildTranslationPrompt(source, targetLabel) {
  const label = targetLabel || TARGET_LANGUAGES[0].label;
  return [
    `請把下面這段完整翻譯成${label}。`,
    "保留數字、單位和否定，不要省略，不要摘要，不要加標題或說明。",
    "只輸出譯文。",
    "",
    String(source || "").trim(),
  ].join("\n");
}
