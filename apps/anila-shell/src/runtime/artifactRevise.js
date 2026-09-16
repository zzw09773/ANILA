/** Build the chat prompt for 「改這一段」. Pure; no DOM. */

export function longestBacktickRun(text) {
  let max = 0;
  let run = 0;
  const s = String(text ?? "");
  for (let i = 0; i < s.length; i += 1) {
    if (s[i] === "`") {
      run += 1;
      if (run > max) max = run;
    } else {
      run = 0;
    }
  }
  return max;
}

export function fenceLanguage(language, kind) {
  const raw = String(language || kind || "text").trim();
  if (/^[A-Za-z0-9_+#.-]{1,24}$/.test(raw)) return raw;
  return "text";
}

export function wrapFencedBlock(language, body) {
  const n = Math.max(3, longestBacktickRun(body) + 1);
  const ticks = "`".repeat(n);
  return `${ticks}${language}\n${body}\n${ticks}`;
}

/**
 * Accept a selection only when every end and every range ancestor stay inside root.
 * Returns the raw text (indentation kept). Null if empty or not fully inside.
 *
 * @param {Selection | { isCollapsed?: boolean, toString?: Function, anchorNode?: Node, focusNode?: Node, rangeCount?: number, getRangeAt?: Function } | null} selection
 * @param {{ contains: (node: Node) => boolean } | null} root
 * @returns {string | null}
 */
export function sourceSelectionText(selection, root) {
  if (!selection || selection.isCollapsed || !root) return null;
  const anchor = selection.anchorNode;
  const focus = selection.focusNode;
  if (!anchor || !focus) return null;
  if (!root.contains(anchor) || !root.contains(focus)) return null;
  const count = Number(selection.rangeCount || 0);
  if (count > 0 && typeof selection.getRangeAt === "function") {
    for (let i = 0; i < count; i += 1) {
      const range = selection.getRangeAt(i);
      const ancestor = range?.commonAncestorContainer;
      if (!ancestor || !root.contains(ancestor)) return null;
    }
  }
  const raw = String(selection.toString() ?? "");
  if (!raw.trim()) return null;
  return raw;
}

/**
 * @param {{
 *   kind?: string,
 *   language?: string,
 *   source?: string,
 *   selected?: string,
 *   instruction?: string,
 * }} input
 * @returns {string | null}
 */
export function buildArtifactRevisePrompt(input) {
  const selectedRaw = String(input?.selected ?? "");
  const instruction = String(input?.instruction ?? "").trim();
  const source = String(input?.source ?? "");
  if (!selectedRaw.trim() || !instruction) return null;
  const fence = fenceLanguage(input?.language, input?.kind);
  const kind = String(input?.kind || fence);
  return [
    `請修改下面這份產物（${kind}）。只改我標出的片段，其餘維持原樣。`,
    `改法：${instruction}`,
    "",
    "【要改的片段】",
    wrapFencedBlock(fence, selectedRaw),
    "",
    "【完整產物】",
    wrapFencedBlock(fence, source),
    "",
    `請回完整更新後的產物。外層程式碼區塊請用 ${fence}，反引號數量必須比產物內最長的連續反引號多至少一個，前後不要解釋。`,
  ].join("\n");
}
