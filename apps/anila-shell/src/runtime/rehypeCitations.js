// 引用標記 `[n]` 的 rehype 後處理 —— 補救計畫 W2-5。
//
// 缺陷本體:`chat.jsx` 原本是三元式二選一 —— **有 citations 就繞過
// `MarkdownView`**,改用 `trust.jsx` 的 `renderTextWithCitations()` 做純文字
// 切割。於是 RAG 回答(平台的招牌情境)失去表格 / 代碼 / KaTeX / Mermaid;
// 更糟的是純文字切割不認得語法邊界,連 code block 裡的 `data[1]` 都會被換成
// 一顆可點的引用按鈕(有測試釘住這條)。
//
// 改法:把 `[n]` 變成 markdown pipeline 的**最後一道 rehype 後處理**,
// `MarkdownView` 恆走。插在 rehype-katex / rehype-highlight 之後,所以數學與
// 語法highlight的結構已經成形,只需要跳過它們的子樹。
//
// 為什麼不用 `unist-util-visit`:那是 react-markdown 的 transitive 依賴,直接
// import 等於把別人的相依關係當自己的 API。這個 walker 20 行,air-gapped 環境
// 零新依賴的價值遠高於省這 20 行。

/** 產出的元素標籤。刻意不含 `-`:自訂元素名的屬性處理有平台差異,而這個節點
 *  永遠會被 `components` 表接走、不會真的進 DOM。 */
export const CITATION_TAG = "anilacite";

/** 引用序號放在 data 屬性上;讀取端一律用這個常數,不要各寫一份字面。 */
export const CITATION_INDEX_ATTR = "data-anila-cite";

/**
 * 不進入的子樹:
 * - `code` / `pre`:程式碼裡的 `[1]` 是索引語法,不是引用(W2-5 的風險釘子)。
 * - `a`:連結文字改成按鈕會產生嵌套互動元素。
 * - `style` / `script`:不該被當文字處理(react-markdown 預設不產生,防禦性)。
 */
const SKIP_TAGS = new Set(["code", "pre", "a", "style", "script"]);

/** KaTeX 產出的子樹內含大量拆碎的符號節點,不碰。 */
const SKIP_CLASS_PATTERN = /(^|\s)katex(\s|$)/;

const MARKER_RE = /\[(\d+)\]/g;

function hasSkippedClass(node) {
  const className = node?.properties?.className;
  const asString = Array.isArray(className) ? className.join(" ") : className;
  return typeof asString === "string" && SKIP_CLASS_PATTERN.test(asString);
}

/**
 * 把一個文字節點切成 [文字, 引用元素, 文字…]。
 *
 * @param {string} value
 * @param {number} count 可用的來源筆數;`[n]` 的 n 必須落在 1..count,否則
 *   當字面留著 —— 超出範圍的標記渲染成「點了不會開任何東西」的按鈕比留著
 *   字面更糟。
 * @returns {Array|null} null = 這個節點沒有任何有效標記(呼叫端就不動它)。
 */
export function splitCitationText(value, count) {
  if (typeof value !== "string" || !value.includes("[")) return null;
  MARKER_RE.lastIndex = 0;
  const out = [];
  let last = 0;
  let match;
  let hit = false;
  while ((match = MARKER_RE.exec(value)) !== null) {
    const n = Number.parseInt(match[1], 10);
    if (!Number.isInteger(n) || n < 1 || n > count) continue;
    hit = true;
    if (match.index > last) {
      out.push({ type: "text", value: value.slice(last, match.index) });
    }
    out.push({
      type: "element",
      tagName: CITATION_TAG,
      properties: { [CITATION_INDEX_ATTR]: String(n) },
      children: [],
    });
    last = match.index + match[0].length;
  }
  if (!hit) return null;
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

/**
 * rehype plugin factory。`count === 0` 時整個 plugin 是 no-op —— 沒有來源就
 * 不該把 `[1]` 變成任何東西。
 *
 * @param {{count?: number}} [options]
 */
export function rehypeCitations(options = {}) {
  const count = Number.isInteger(options.count) && options.count > 0 ? options.count : 0;
  return (tree) => {
    if (!count) return tree;
    walk(tree, count);
    return tree;
  };
}

function walk(node, count) {
  if (!node || !Array.isArray(node.children) || node.children.length === 0) return;
  if (node.type === "element" && (SKIP_TAGS.has(node.tagName) || hasSkippedClass(node))) {
    return;
  }
  let next = null;
  for (let i = 0; i < node.children.length; i += 1) {
    const child = node.children[i];
    if (child?.type === "text") {
      const parts = splitCitationText(child.value, count);
      if (parts) {
        if (!next) next = node.children.slice(0, i);
        next.push(...parts);
        continue;
      }
    } else {
      walk(child, count);
    }
    if (next) next.push(child);
  }
  if (next) node.children = next;
}
