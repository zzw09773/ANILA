// 信件改寫的日期、金額、期限比對。
// 標紅由這支程式決定，不請模型自己標記，也不信任模型說「沒有改動」。

const DIGITS = {
  零: 0, 〇: 0, "○": 0,
  一: 1, 二: 2, 兩: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9,
  壹: 1, 貳: 2, 參: 3, 叁: 3, 肆: 4, 伍: 5, 陸: 6, 柒: 7, 捌: 8, 玖: 9,
};

const SMALL_UNIT = { 十: 10, 拾: 10, 百: 100, 佰: 100, 千: 1000, 仟: 1000 };
const BIG_UNIT = { 萬: 10000, 億: 100000000 };

const NUM = "[0-9０-９]{1,4}|[零〇○一二三四五六七八九十百千萬億兩壹貳參叁肆伍陸柒捌玖拾佰仟]{1,12}";
const AMOUNT_NUM = "[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+(?:[.．][0-9０-９]+)?|[0-9０-９]+(?:[.．][0-9０-９]+)?|[零〇○一二三四五六七八九十百千萬億兩壹貳參叁肆伍陸柒捌玖拾佰仟]+";
const CURRENCY = "新臺幣|新台幣|新台币|臺幣|台幣|NT\\$|NTD|＄|\\$";

function toAsciiDigits(value) {
  return String(value || "").replace(/[０-９]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0));
}

function parseChineseInteger(token) {
  const text = String(token || "");
  if (!text) return null;
  let total = 0n;
  let section = 0n;
  let number = 0n;
  let seen = false;
  let lastWasDigit = false;
  for (const ch of text) {
    if (Object.prototype.hasOwnProperty.call(DIGITS, ch)) {
      const digit = BigInt(DIGITS[ch]);
      number = lastWasDigit ? number * 10n + digit : digit;
      lastWasDigit = true;
      seen = true;
      continue;
    }
    lastWasDigit = false;
    if (Object.prototype.hasOwnProperty.call(SMALL_UNIT, ch)) {
      section += (number === 0n ? 1n : number) * BigInt(SMALL_UNIT[ch]);
      number = 0n;
      seen = true;
      continue;
    }
    if (Object.prototype.hasOwnProperty.call(BIG_UNIT, ch)) {
      section += number;
      total += (section === 0n ? 1n : section) * BigInt(BIG_UNIT[ch]);
      section = 0n;
      number = 0n;
      seen = true;
      continue;
    }
    return null;
  }
  if (!seen) return null;
  return total + section + number;
}

function parseIntegerToken(token) {
  const compact = toAsciiDigits(token).replace(/[,，\s]/g, "");
  if (/^\d+$/.test(compact)) return Number(compact);
  const chinese = parseChineseInteger(compact);
  if (chinese == null) return null;
  const value = Number(chinese);
  if (!Number.isSafeInteger(value)) return null;
  return value;
}

function amountCents(token) {
  const compact = toAsciiDigits(token).replace(/[,，\s]/g, "").replace(/．/g, ".");
  if (/^\d+$/.test(compact)) return (BigInt(compact) * 100n).toString();
  if (/^\d+\.\d+$/.test(compact)) {
    const [whole, frac] = compact.split(".");
    const digits = `${frac}00`;
    let cents = BigInt(whole) * 100n + BigInt(digits.slice(0, 2));
    if (digits[2] >= "5") cents += 1n;
    return cents.toString();
  }
  const chinese = parseChineseInteger(compact);
  if (chinese == null) return null;
  return (chinese * 100n).toString();
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function dateKey(year, month, day) {
  return `date:${String(year).padStart(4, "0")}-${pad2(month)}-${pad2(day)}`;
}

function monthDayKey(month, day) {
  return `date:--${pad2(month)}-${pad2(day)}`;
}

function scan(text, regex, build) {
  const flags = regex.flags.includes("g") ? regex.flags : `${regex.flags}g`;
  const pattern = new RegExp(regex.source, flags);
  const found = [];
  let match = pattern.exec(text);
  while (match) {
    if (match.index === pattern.lastIndex) pattern.lastIndex += 1;
    const fact = build(match);
    if (fact && fact.end > fact.start && fact.key) found.push(fact);
    match = pattern.exec(text);
  }
  return found;
}

function matchDates(text) {
  const full = scan(
    text,
    new RegExp(`(民國\\s*)?(${NUM})\\s*年\\s*(${NUM})\\s*月\\s*(${NUM})\\s*日`, "g"),
    (match) => {
      const roc = Boolean(match[1]);
      let year = parseIntegerToken(match[2]);
      const month = parseIntegerToken(match[3]);
      const day = parseIntegerToken(match[4]);
      if (year == null || !month || !day) return null;
      if (roc || year < 1912) year += 1911;
      return {
        kind: "date",
        key: dateKey(year, month, day),
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  );
  const iso = scan(
    text,
    /([0-9０-９]{4})[/\-.／－．]([0-9０-９]{1,2})[/\-.／－．]([0-9０-９]{1,2})/g,
    (match) => {
      const year = parseIntegerToken(match[1]);
      const month = parseIntegerToken(match[2]);
      const day = parseIntegerToken(match[3]);
      if (!year || !month || !day) return null;
      return {
        kind: "date",
        key: dateKey(year, month, day),
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  );
  const monthDay = scan(
    text,
    new RegExp(`(?<![年0-9０-９])(${NUM})\\s*月\\s*(${NUM})\\s*日`, "g"),
    (match) => {
      const month = parseIntegerToken(match[1]);
      const day = parseIntegerToken(match[2]);
      if (!month || !day) return null;
      return {
        kind: "date",
        key: monthDayKey(month, day),
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  );
  return [...full, ...iso, ...monthDay];
}

function matchAmounts(text) {
  return scan(
    text,
    new RegExp(`(?:${CURRENCY})\\s*(${AMOUNT_NUM})\\s*(?:元|圓)?|(${AMOUNT_NUM})\\s*(?:元|圓)`, "g"),
    (match) => {
      const token = match[1] || match[2];
      const cents = amountCents(token);
      if (cents == null) return null;
      return {
        kind: "amount",
        key: `amount:${cents}`,
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  );
}

function durationUnit(unit) {
  if (unit === "工作日" || unit === "工作天") return "workday";
  if (unit === "日" || unit === "天") return "day";
  if (unit === "週" || unit === "周") return "week";
  if (unit === "小時") return "hour";
  if (unit === "個月") return "month";
  return unit;
}

function matchDurations(text) {
  return scan(
    text,
    new RegExp(`(${NUM})\\s*個?\\s*(工作日|工作天|個月|小時|週|周|日|天)\\s*內`, "g"),
    (match) => {
      const count = parseIntegerToken(match[1]);
      if (count == null) return null;
      return {
        kind: "deadline",
        key: `deadline:dur:${count}:${durationUnit(match[2])}`,
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  ).concat(scan(
    text,
    new RegExp(`(${NUM})\\s*個\\s*(工作日|工作天)`, "g"),
    (match) => {
      const count = parseIntegerToken(match[1]);
      if (count == null) return null;
      return {
        kind: "deadline",
        key: `deadline:dur:${count}:${durationUnit(match[2])}`,
        start: match.index,
        end: match.index + match[0].length,
        raw: match[0],
      };
    },
  ));
}

function boundNorm(word) {
  if (word === "以前" || word === "之前" || word === "前") return "前";
  if (word === "以後" || word === "之後" || word === "後") return "後";
  return "內";
}

function extendWithBound(text, start, end) {
  const tail = text.slice(end);
  const match = /^\s*(以前|之後|之前|以後|前|後|內)/.exec(tail);
  if (!match) return { end, norm: "" };
  return {
    end: end + match[0].length,
    norm: boundNorm(match[1]),
  };
}

function matchRelative(text) {
  return scan(
    text,
    /(今|明|後)天|(本|下|上)(?:週|周|星期)([一二三四五六日天])/g,
    (match) => {
      const bound = extendWithBound(text, match.index, match.index + match[0].length);
      if (match[1]) {
        const key = bound.norm
          ? `deadline:rel:${match[1]}:${bound.norm}`
          : `deadline:rel:${match[1]}`;
        return {
          kind: "deadline",
          key,
          start: match.index,
          end: bound.end,
          raw: text.slice(match.index, bound.end),
        };
      }
      const weekday = match[3] === "天" ? "日" : match[3];
      const key = bound.norm
        ? `deadline:rel:${match[2]}:${weekday}:${bound.norm}`
        : `deadline:rel:${match[2]}:${weekday}`;
      return {
        kind: "deadline",
        key,
        start: match.index,
        end: bound.end,
        raw: text.slice(match.index, bound.end),
      };
    },
  );
}

function matchTimes(text) {
  return scan(
    text,
    /(?:(上午|下午|中午|晚上)\s*)?([0-9０-９]{1,2}|[零〇○一二三四五六七八九十兩]{1,3})\s*時(?:\s*([0-9０-９]{1,2}|[零〇○一二三四五六七八九十兩]{1,3})\s*分)?/g,
    (match) => {
      const hour = parseIntegerToken(match[2]);
      if (hour == null) return null;
      const minute = match[3] ? parseIntegerToken(match[3]) : null;
      if (match[3] && minute == null) return null;
      const clockEnd = match.index + match[0].length;
      const bound = extendWithBound(text, match.index, clockEnd);
      const period = match[1] || "";
      const minuteKey = minute == null ? "" : String(minute);
      const key = bound.norm
        ? `deadline:time:${period}:${hour}:${minuteKey}:${bound.norm}`
        : `deadline:time:${period}:${hour}:${minuteKey}`;
      return {
        kind: "deadline",
        key,
        start: match.index,
        end: bound.end,
        raw: text.slice(match.index, bound.end),
      };
    },
  );
}

function takeNonOverlapping(candidates) {
  const sorted = candidates
    .filter((item) => item && item.end > item.start && item.key)
    .sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const accepted = [];
  let cursor = 0;
  for (const item of sorted) {
    if (item.start < cursor) continue;
    accepted.push(item);
    cursor = item.end;
  }
  return accepted;
}

function boundsAfterDates(text, facts) {
  const extra = [];
  for (const fact of facts) {
    if (fact.kind !== "date") continue;
    const tail = text.slice(fact.end);
    const match = /^\s*(以前|之後|之前|以後|前|後|內)/.exec(tail);
    if (!match) continue;
    const word = match[1];
    const start = fact.end + (match[0].length - word.length);
    extra.push({
      kind: "deadline",
      key: `deadline:bound:${fact.key}:${boundNorm(word)}`,
      start,
      end: fact.end + match[0].length,
      raw: word,
      anchorStart: fact.start,
      anchorRaw: fact.raw,
    });
  }
  return extra;
}

export function extractLetterFacts(text) {
  const source = String(text ?? "");
  const facts = takeNonOverlapping([
    ...matchDates(source),
    ...matchAmounts(source),
    ...matchDurations(source),
    ...matchRelative(source),
    ...matchTimes(source),
  ]);
  return takeNonOverlapping([...facts, ...boundsAfterDates(source, facts)]);
}

function monthDayOf(dateKeyValue) {
  const match = /^date:(?:--|\d{4}-)(\d{2})-(\d{2})$/.exec(dateKeyValue);
  return match ? `${match[1]}-${match[2]}` : null;
}

function splitBound(key) {
  const match = /^deadline:bound:(date:(?:--|\d{4}-)\d{2}-\d{2}):(前|後|內)$/.exec(key);
  if (!match) return null;
  return { dateKey: match[1], norm: match[2] };
}

function dateKeysCompatible(rewriteKey, sourceKey) {
  if (rewriteKey === sourceKey) return true;
  if (!rewriteKey.startsWith("date:--")) return false;
  return monthDayOf(rewriteKey) != null && monthDayOf(rewriteKey) === monthDayOf(sourceKey);
}

function factsMatch(rewriteFact, sourceFact) {
  if (rewriteFact.kind !== sourceFact.kind) return false;
  if (rewriteFact.key === sourceFact.key) return true;
  if (rewriteFact.kind === "date") return dateKeysCompatible(rewriteFact.key, sourceFact.key);
  const rewriteBound = splitBound(rewriteFact.key);
  const sourceBound = splitBound(sourceFact.key);
  if (!rewriteBound || !sourceBound || rewriteBound.norm !== sourceBound.norm) return false;
  return monthDayOf(rewriteBound.dateKey) != null
    && monthDayOf(rewriteBound.dateKey) === monthDayOf(sourceBound.dateKey);
}

function segmentsFromFlags(text, flagged) {
  const spans = flagged
    .slice()
    .sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const segments = [];
  let cursor = 0;
  for (const span of spans) {
    if (span.start < cursor) continue;
    if (span.end > text.length) continue;
    if (span.start > cursor) {
      segments.push({ text: text.slice(cursor, span.start), flag: null });
    }
    segments.push({ text: text.slice(span.start, span.end), flag: span.kind });
    cursor = span.end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), flag: null });
  if (segments.length === 0) segments.push({ text, flag: null });
  return segments;
}

function missingLabels(missing) {
  const bounds = missing.filter((fact) => fact.key.startsWith("deadline:bound:"));
  const rest = missing.filter((fact) => !fact.key.startsWith("deadline:bound:"));
  const labels = rest.map((fact) => {
    const bound = bounds.find((item) => item.anchorStart === fact.start);
    return bound ? `${fact.raw}${bound.raw}` : fact.raw;
  });
  for (const bound of bounds) {
    if (rest.some((fact) => fact.start === bound.anchorStart)) continue;
    labels.push(`${bound.anchorRaw || ""}${bound.raw}`);
  }
  return [...new Set(labels.filter(Boolean))];
}

/**
 * 比對改寫相對原文新增或改動的日期、金額、期限。
 * 改寫裡對不上的片段要標出；原文有、改寫沒有的另列清單。
 */
export function compareLetterRewrite(original, rewrite) {
  const sourceFacts = extractLetterFacts(original).map((fact) => ({ ...fact, used: false }));
  const rewriteFacts = extractLetterFacts(rewrite);
  const changed = [];
  for (const fact of rewriteFacts) {
    const index = sourceFacts.findIndex((item) => !item.used && factsMatch(fact, item));
    if (index >= 0) sourceFacts[index].used = true;
    else changed.push(fact);
  }
  const missing = sourceFacts.filter((fact) => !fact.used);
  return {
    segments: segmentsFromFlags(String(rewrite ?? ""), changed),
    changed,
    missing,
    missingLabels: missingLabels(missing),
  };
}
