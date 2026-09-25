// 續寫接到同一則答案上。
// 只有整段重複（至少 40 字）才剪掉續寫開頭。短的相同字尾，像程式裡第二個
// return，留著，避免改掉語意。
// 程式碼圍欄沒關時不剪重疊、也不插空白；只拿掉確實重複的那道開頭。
// 一般文字若在英數中間被截斷，接縫補一個空白。

const MIN_OVERLAP = 40;
const FENCE_LINE = /^( {0,3})(`{3,}|~{3,})(.*)$/;

/** 跟 Router 約定的同一句。續寫回合由 Shell 放進使用者訊息。 */
export const CONTINUE_INSTRUCTION =
  "請接續上文，直接從中斷處往下寫，不要重複已經寫過的內容。";

function fenceMarker(line) {
  const matched = FENCE_LINE.exec(line);
  if (!matched) return null;
  const tick = matched[2];
  const info = matched[3] ?? "";
  if (tick[0] === "`" && info.includes("`")) return null;
  return { char: tick[0], len: tick.length, info };
}

function unclosedFence(text) {
  let open = null;
  for (const line of String(text || "").split("\n")) {
    const marker = fenceMarker(line);
    if (!marker) continue;
    if (
      open
      && marker.char === open.char
      && marker.len >= open.len
      && marker.info.trim() === ""
    ) {
      open = null;
      continue;
    }
    if (!open) open = marker;
  }
  return open;
}

function stripRepeatedOpener(extra, open) {
  const lines = extra.split("\n");
  let index = 0;
  while (index < lines.length && lines[index].trim() === "") index += 1;
  if (index >= lines.length) return extra;
  const marker = fenceMarker(lines[index]);
  if (!marker || marker.char !== open.char) return extra;
  // 長度夠、後面沒有資訊，才是 CommonMark 的關閉標記，要留著。
  const closes = marker.len >= open.len && marker.info.trim() === "";
  if (closes) return extra;
  // 字元、長度、資訊都一樣，才是把開頭再寫一次。較短的圍欄是內容。
  const sameOpener = marker.len === open.len && marker.info.trim() === open.info.trim();
  if (!sameOpener) return extra;
  lines.splice(0, index + 1);
  return lines.join("\n");
}

function overlapLength(prior, extra) {
  const max = Math.min(prior.length, extra.length, 400);
  for (let size = max; size >= MIN_OVERLAP; size -= 1) {
    if (prior.slice(-size) === extra.slice(0, size)) return size;
  }
  return 0;
}

function dropOverlap(prior, extra) {
  const direct = overlapLength(prior, extra);
  if (direct > 0) return extra.slice(direct);
  const stripped = extra.replace(/^\n+/, "");
  if (stripped === extra) return extra;
  const nested = overlapLength(prior, stripped);
  if (nested > 0) return stripped.slice(nested);
  return extra;
}

function boundarySpace(base, extra) {
  const left = base.slice(-1);
  const right = extra.slice(0, 1);
  if (!left || !right) return "";
  if (/\s/.test(left) || /\s/.test(right)) return "";
  if (/[A-Za-z0-9]/.test(left) && /[A-Za-z0-9]/.test(right)) return " ";
  return "";
}

/** 把這一輪續寫接到已寫出的正文後面。空的續寫就留原文。 */
export function joinContinuation(prior, addition) {
  const base = typeof prior === "string" ? prior : "";
  let extra = typeof addition === "string" ? addition : "";
  if (!extra) return base;
  if (!base) return extra;
  const open = unclosedFence(base);
  if (open) extra = stripRepeatedOpener(extra, open);
  // 圍欄裡的重疊分不出是重播還是下一行真的要同樣的程式，不剪。
  if (!open) extra = dropOverlap(base, extra);
  if (!extra) return base;
  const gap = open ? "" : boundarySpace(base, extra);
  return base + gap + extra;
}
