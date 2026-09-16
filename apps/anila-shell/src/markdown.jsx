// Markdown + LaTeX + syntax-highlighted code + copyable table/code.
//
// LLMs commonly emit LaTeX in the `\[ ... \]` / `\( ... \)` escape form rather
// than the `$$ ... $$` / `$ ... $` dollar form that `remark-math` expects.
// We rewrite the escape form to dollars before the markdown parser runs.
import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "katex/dist/katex.min.css";
import "highlight.js/styles/github-dark.css";
import { useArtifactPreview } from "./artifactContext.jsx";
import { detectArtifactKind } from "./runtime/artifactDetect.js";
import {
  canConsumeRevisionTurn,
  extractRevisionCandidate,
  shouldApplyArtifactFence,
} from "./runtime/artifactRevision.js";
import { CitationInline } from "./trust.jsx";

const MarkdownMessageContext = createContext(null);

function preprocessLatex(text) {
  if (!text) return "";
  return text
    // Display math: \[ ... \]  →  $$ ... $$
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, inner) => `\n\n$$${inner.trim()}$$\n\n`)
    // Inline math:  \( ... \)  →  $ ... $
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, inner) => `$${inner.trim()}$`);
}

export function extractThinkTags(text) {
  if (!text) return { thinking: null, body: text || "" };
  const matches = Array.from(text.matchAll(/<think(?:ing)?>([\s\S]*?)(<\/think(?:ing)?>|$)/gi));
  const pieces = matches
    .map((m) => (m[1] || "").trim())
    .filter((s) => s.length > 0);
  if (pieces.length > 0) {
    const stripped = text
      .replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, "")
      .replace(/<think(?:ing)?>[\s\S]*$/gi, "")
      .trim();
    return { thinking: pieces.join("\n\n"), body: stripped };
  }

  // Heuristic fallback for untagged thought sections from reasoning models.
  // (a) Paired header: `thought\n... \n<marker: Plan/Answer/Final/Action/...> \n body`
  const markerRe = /^(?:[*_`#>\s]*(?:thought|thinking)[*_`#>\s]*)\s*:?\s*\n+([\s\S]*?)\n+(?:[*_\s]*(?:plan|answer|final|response|action|回答|最終回答|回覆|行動|結論)[*_\s]*\s*:?\s*\n+)([\s\S]*)$/i;
  const head = markerRe.exec(text);
  if (head) {
    const [, thought, body] = head;
    return { thinking: thought.trim(), body: body.trim() };
  }

  // (b) Leading "thought\n" with a trailing DISPATCH directive that the
  // router couldn't consume — salvage the analysis into the fold.
  const dispatchInThoughtRe = /^(?:[*_`#>\s]*(?:thought|thinking)[*_`#>\s]*)\s*\n([\s\S]*?DISPATCH:[^\n]+)\s*$/i;
  const d = dispatchInThoughtRe.exec(text);
  if (d) {
    return {
      thinking: d[1].trim(),
      body: "（ANILA 沒能把這題交給合適的助手；以下是它的分析過程）",
    };
  }

  // (c) Leading "thought\n" followed by English reasoning that hands off to
  // a Chinese answer. We look for the LAST paragraph break before a long CJK
  // run — models sometimes emit the answer without any marker, but the
  // reasoning is mostly English and the final reply is CJK so the language
  // switch is itself a boundary. Conservative: body must be >= 40 chars and
  // must start with a CJK character.
  const startsWithThought = /^(?:[*_`#>\s]*(?:thought|thinking)[*_`#>\s]*)\s*\n/i.test(text);
  if (startsWithThought) {
    const cjkBoundary = /\n\n(?=[一-鿿])/g;
    let lastIdx = -1;
    let m;
    while ((m = cjkBoundary.exec(text)) !== null) lastIdx = m.index;
    if (lastIdx > 0) {
      const body = text.slice(lastIdx).trim();
      if (body.length >= 40 && /^[一-鿿]/.test(body)) {
        return { thinking: text.slice(0, lastIdx).trim(), body };
      }
    }
    // Even without a clean boundary, if an English "...answer directly." kind
    // of sentence immediately precedes a CJK run, split right at the CJK run
    // (handles the gemma bug where the answer is glued onto the reasoning).
    const inlineSwitch = /(?:answer directly|directly answer|回答如下|以下[是為]回答|我[應該會]直接回答)[.。\s:：]*([一-鿿][\s\S]*)$/i.exec(text);
    if (inlineSwitch && inlineSwitch[1].trim().length >= 40) {
      const body = inlineSwitch[1].trim();
      const thinking = text.slice(0, inlineSwitch.index + inlineSwitch[0].indexOf(body)).trim();
      return { thinking, body };
    }
  }

  return { thinking: null, body: text };
}

// ── Copy button shared by code block / table ────────────────────────────────
function CopyButton({ getText, style }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        const text = getText() || "";
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
        }
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      style={{
        position: "absolute",
        top: 6,
        right: 6,
        zIndex: 2,
        padding: "2px 9px",
        fontSize: 11,
        fontFamily: "var(--font-mono)",
        background: "var(--bg-elev)",
        border: "1px solid var(--border)",
        borderRadius: 4,
        color: copied ? "var(--success)" : "var(--fg-muted)",
        cursor: "pointer",
        opacity: 0.75,
        transition: "opacity 120ms ease",
        ...style,
      }}
      onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
      onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.75"; }}
      title={copied ? "已複製" : "複製"}
    >
      {copied ? "✓ 已複製" : "複製"}
    </button>
  );
}

// Extract raw source text from a hast code node (its text children).
function codeNodeText(codeNode) {
  if (!codeNode?.children) return "";
  return codeNode.children
    .filter((c) => c.type === "text")
    .map((c) => c.value)
    .join("");
}

// rehype-highlight 會把 token 包進 <span>，只讀直接 text children 會漏字。
// 預覽／偵測必須走遞迴，才拿得到完整原始碼（含被標成 xml 的 SVG）。
function hastText(node) {
  if (!node) return "";
  if (node.type === "text") return node.value || "";
  if (!Array.isArray(node.children)) return "";
  return node.children.map(hastText).join("");
}

// Small stable hash → deterministic mermaid render id per source.
function hashString(s) {
  let h = 0;
  for (let i = 0; i < (s || "").length; i++) {
    h = (h << 5) - h + s.charCodeAt(i);
    h |= 0;
  }
  return h;
}

// Mermaid lazy loader — keeps the ~2.8MB lib in its own chunk; only fetched
// when a diagram renders (offline: bundled at build, no CDN).
let _mermaidPromise = null;
export function mermaidThemeForApp(theme) {
  return theme === "dark" ? "dark" : "default";
}

function readAppTheme() {
  if (typeof document === "undefined") return "light";
  return document.documentElement?.getAttribute("data-theme") === "dark"
    ? "dark"
    : "light";
}

// The app's tweaks state is projected to this existing DOM attribute by
// applyTweaks() in app.jsx. Observe that projection so MarkdownView does not
// create a second theme source just for diagrams.
function useAppTheme() {
  const [theme, setTheme] = useState(readAppTheme);

  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const root = document.documentElement;
    const sync = () => setTheme(readAppTheme());
    const observer = typeof MutationObserver === "function"
      ? new MutationObserver(sync)
      : null;
    sync();
    observer?.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer?.disconnect();
  }, []);

  return theme;
}

export function cleanupMermaidError(renderId) {
  if (typeof document === "undefined" || !renderId || !document.body) return;
  const prefix = `d${renderId}`;
  document.body.querySelectorAll("[id]").forEach((node) => {
    if (node.id === prefix) node.remove();
  });
}

function loadMermaid(theme) {
  if (!_mermaidPromise) {
    _mermaidPromise = import("mermaid").then((m) => m.default || m);
  }
  return _mermaidPromise.then((mermaid) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: mermaidThemeForApp(theme),
    });
    return mermaid;
  });
}

function MermaidDiagram({ source }) {
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  const theme = useAppTheme();
  const idRef = useRef(`mmd-${Math.abs(hashString(source))}`);
  useEffect(() => {
    let alive = true;
    setErr("");
    loadMermaid(theme)
      .then((mermaid) => mermaid.render(idRef.current, source))
      .then(({ svg }) => { if (alive) setSvg(svg); })
      .catch((e) => {
        cleanupMermaidError(idRef.current);
        if (alive) setErr(e?.message || "圖表渲染失敗");
      });
    return () => { alive = false; };
  }, [source, theme]);
  if (err) {
    return (
      <pre style={{
        background: "var(--bg-subtle)", border: "1px solid var(--danger)",
        borderRadius: "var(--radius)", padding: "10px 12px", fontSize: 12.5,
        overflowX: "auto", margin: "8px 0",
      }}>{`mermaid 錯誤：${err}\n\n${source}`}</pre>
    );
  }
  // XSS note: the SVG is mermaid's own output rendered under
  // securityLevel:"strict", which sanitises via mermaid's bundled DOMPurify
  // and disables htmlLabels / click handlers / script injection. The diagram
  // source is LLM text, so strict mode is the required defence — do NOT relax
  // securityLevel. The placeholder fallback is a static literal.
  return (
    <div
      style={{
        background: "var(--bg-subtle)", border: "1px solid var(--border)",
        borderRadius: "var(--radius)", padding: 12, margin: "8px 0",
        overflowX: "auto", textAlign: "center",
      }}
      dangerouslySetInnerHTML={{ __html: svg || "<span style='color:var(--fg-subtle);font-size:12px'>圖表載入中…</span>" }}
    />
  );
}

// ── Code block: language label + syntax highlight (via rehype-highlight) +
//    copy button. rehype-highlight has already injected <span class="hljs-…">
//    children on the <code>, so the CSS theme takes over visually.
function CodeBlock({ node, children, ...props }) {
  const ref = useRef(null);
  const preview = useArtifactPreview();
  const messageCtx = useContext(MarkdownMessageContext);
  const codeNode = node?.children?.find((c) => c.tagName === "code");
  const classes = codeNode?.properties?.className || [];
  const langClass = Array.isArray(classes)
    ? classes.find((c) => typeof c === "string" && c.startsWith("language-"))
    : "";
  const lang = langClass ? String(langClass).replace("language-", "") : "";
  // Mermaid:用圖表渲染取代程式碼區塊。
  if (lang === "mermaid") {
    return <MermaidDiagram source={codeNodeText(codeNode)} />;
  }
  const source = hastText(codeNode) || "";
  const artifactKind = detectArtifactKind(lang, source);
  const canPreview = Boolean(artifactKind && preview?.openArtifact);
  useEffect(() => {
    const cur = preview?.current;
    if (!canPreview || !cur || !artifactKind) return;
    const decision = shouldApplyArtifactFence({
      ticket: preview.pendingRevision,
      fenceMessageId: messageCtx?.messageId ?? null,
      conversationId: messageCtx?.conversationId ?? null,
      streaming: Boolean(messageCtx?.streaming),
      streamState: messageCtx?.streamState,
      finishReason: messageCtx?.finishReason,
      artifactKind,
      source,
      current: cur,
    });
    if (!decision.apply || decision.consume) return;
    preview.openArtifact({ kind: artifactKind, source, language: lang });
  }, [source, artifactKind, lang, canPreview, preview, messageCtx]);
  return (
    <div style={{ position: "relative", margin: "8px 0" }}>
      {lang && (
        <span style={{
          position: "absolute",
          top: 6,
          left: 10,
          zIndex: 2,
          fontSize: 10,
          padding: "1px 7px",
          fontFamily: "var(--font-mono)",
          color: "var(--fg-subtle)",
          background: "var(--bg)",
          border: "1px solid var(--border)",
          borderRadius: 3,
          letterSpacing: 0.4,
        }}>{lang}</span>
      )}
      <div
        data-testid="md-code-actions"
        style={{
          position: "absolute",
          top: 6,
          right: 6,
          zIndex: 2,
          display: "flex",
          alignItems: "center",
          gap: 4,
        }}
      >
        {canPreview && (
          <button
            type="button"
            data-testid="artifact-preview-btn"
            onClick={() => preview.openArtifact({
              kind: artifactKind,
              source: source || ref.current?.innerText || "",
              language: lang,
            })}
            style={{
              padding: "2px 9px",
              fontSize: 11,
              fontFamily: "var(--font-mono)",
              background: "var(--bg-elev)",
              border: "1px solid var(--border)",
              borderRadius: 4,
              color: "var(--fg-muted)",
              cursor: "pointer",
              opacity: 0.75,
            }}
            onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
            onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.75"; }}
            title="在右側預覽"
          >
            預覽
          </button>
        )}
        <CopyButton
          getText={() => ref.current?.innerText || ""}
          style={{ position: "static", top: "auto", right: "auto" }}
        />
      </div>
      <pre
        ref={ref}
        data-testid="md-code-pre"
        {...props}
        onWheel={(e) => {
          const el = e.currentTarget;
          if (el.scrollHeight > el.clientHeight + 1) e.stopPropagation();
        }}
        style={{
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          padding: "10px 12px",
          paddingTop: lang ? 28 : 10,
          overflow: "auto",
          maxHeight: "min(60vh, 520px)",
          overscrollBehavior: "contain",
          fontSize: 12.5,
          lineHeight: 1.55,
          margin: 0,
        }}
      >
        {children}
      </pre>
    </div>
  );
}

// ── Table: wrapper with copy button. Copies back as a Markdown table so the
//    pasted result stays structured in e.g. another chat / doc / notes app.
function Table({ children, ...props }) {
  const ref = useRef(null);
  const toMarkdown = () => {
    const el = ref.current;
    if (!el) return "";
    const rows = Array.from(el.rows || []);
    if (rows.length === 0) return "";
    const cellsOf = (row) =>
      Array.from(row.cells || []).map((c) =>
        (c.innerText || "").trim().replace(/\|/g, "\\|").replace(/\n+/g, " "),
      );
    const lines = [];
    lines.push("| " + cellsOf(rows[0]).join(" | ") + " |");
    lines.push("| " + cellsOf(rows[0]).map(() => "---").join(" | ") + " |");
    for (let i = 1; i < rows.length; i++) {
      lines.push("| " + cellsOf(rows[i]).join(" | ") + " |");
    }
    return lines.join("\n");
  };
  return (
    <div className="anila-md-table" style={{ position: "relative", overflowX: "auto", margin: "8px 0", maxWidth: "100%" }}>
      <CopyButton getText={toMarkdown} />
      <table
        ref={ref}
        {...props}
        style={{
          borderCollapse: "collapse",
          fontSize: 13,
          minWidth: 240,
        }}
      >
        {children}
      </table>
    </div>
  );
}

// Fit the painted box to the viewport while keeping the native ratio.
// Exported so the resize-deformation guard can call the same numbers
// the lightbox uses — a second copy would drift.
export function fitLightboxBox(nw, nh, vw, vh) {
  if (!nw || !nh || !vw || !vh) return null;
  const maxW = vw * 0.92;
  const maxH = vh * 0.92;
  const capped = Math.min(maxW / nw, maxH / nh);
  return {
    width: Math.round(nw * capped),
    height: Math.round(nh * capped),
  };
}

// Lightbox: full-screen overlay 顯示放大的圖片。點背景 / 按 Esc / 點 ✕ 關閉。
// 用 createPortal 跳出 markdown 容器,避免父層 transform/overflow 影響 fixed 定位。
export function ImageLightbox({ src, alt, onClose }) {
  const imgRef = useRef(null);
  const [box, setBox] = useState(null);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // body 滾動鎖,避免背景跟著滾。
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  useEffect(() => {
    function apply() {
      const el = imgRef.current;
      if (!el) return;
      const next = fitLightboxBox(
        el.naturalWidth, el.naturalHeight,
        window.innerWidth, window.innerHeight,
      );
      if (next) setBox(next);
    }
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, []);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt || "圖片預覽"}
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0, 0, 0, 0.85)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
        cursor: "zoom-out",
        padding: 24,
      }}
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        aria-label="關閉預覽"
        style={{
          position: "absolute",
          top: 16,
          right: 20,
          width: 36,
          height: 36,
          borderRadius: "50%",
          border: "1px solid rgba(255,255,255,0.3)",
          background: "rgba(0,0,0,0.4)",
          color: "#fff",
          fontSize: 20,
          lineHeight: 1,
          cursor: "pointer",
        }}
      >
        ×
      </button>
      <img
        ref={imgRef}
        src={src}
        alt={alt || ""}
        onLoad={(e) => {
          const next = fitLightboxBox(
            e.currentTarget.naturalWidth, e.currentTarget.naturalHeight,
            window.innerWidth, window.innerHeight,
          );
          if (next) setBox(next);
        }}
        onClick={(e) => e.stopPropagation()}
        style={{
          // Before onLoad, width/height stay auto. max-* then scales a
          // replaced element proportionally (Reviewer measured 6000×1500
          // at 760×900 → 699×175, AR 4.000). After onLoad the pixel box
          // takes over; resize recomputes it. No object-fit:contain.
          width: box ? `${box.width}px` : "auto",
          height: box ? `${box.height}px` : "auto",
          maxWidth: "92vw",
          maxHeight: "92vh",
          borderRadius: 8,
          boxShadow: "0 12px 40px rgba(0,0,0,0.6)",
          // Clicking the figure does not close (save-as / inspect).
          // zoom-out here would promise a shrink that never happens.
          cursor: "default",
        }}
      />
    </div>,
    document.body,
  );
}

// 訊息區內的 markdown 圖片:置中顯示 + 點擊開 lightbox。
// 須是 module-level component,react-markdown 才能維持 useState 跨 re-render。
function MarkdownImage({ node, src, alt, ...rest }) {
  const [open, setOpen] = useState(false);
  return (
    <span style={{ display: "block", textAlign: "center", margin: "10px 0" }}>
      <img
        src={src}
        alt={alt || ""}
        {...rest}
        onClick={() => setOpen(true)}
        style={{
          display: "inline-block",
          maxWidth: "min(100%, 520px)",
          height: "auto",
          borderRadius: 8,
          border: "1px solid var(--border)",
          cursor: "zoom-in",
        }}
      />
      {open && <ImageLightbox src={src} alt={alt} onClose={() => setOpen(false)} />}
    </span>
  );
}

// Citation chips live inside markdown text nodes so RAG answers can be
// both formatted and cited. Code / inline-code are left alone so `[1]`
// in a fence stays a literal. Context rather than a rebuilt `components`
// map — mermaid / artifact tests keep calling <MarkdownView text={…} />.
const CitationContext = React.createContext({
  citations: null,
  onOpen: null,
  shownFigures: null,
});

function citationFigurePks(citation) {
  const raw = citation && citation.image_pks;
  if (!Array.isArray(raw)) return [];
  return raw.filter((n) => Number.isInteger(n) && n > 0);
}

// First occurrence of `${citationIndex}:${pk}` in this MarkdownView wins.
// The same ownerId may re-claim (React Strict Mode double-invokes render);
// a different chip cannot.
function claimFigureOnce(shownFigures, figureKey, ownerId) {
  if (!shownFigures) return true;
  const existing = shownFigures.get(figureKey);
  if (existing === undefined) {
    shownFigures.set(figureKey, ownerId);
    return true;
  }
  return existing === ownerId;
}

function CitationFigures({ citation, pks: pksOverride }) {
  const pks = pksOverride ?? citationFigurePks(citation);
  if (pks.length === 0) return null;
  return pks.map((pk) => (
    <MarkdownImage
      key={`fig-${pk}`}
      src={`/api/ingestion/images/${pk}/blob`}
      alt={citation.title || "引用段落的圖"}
    />
  ));
}

function splitTextWithCitations(text, citations, onOpen, keyPrefix, shownFigures) {
  if (!text) return [];
  if (!citations || citations.length === 0) return [text];
  const re = /\[(\d+)\]/g;
  const parts = [];
  let last = 0;
  let m;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    const n = parseInt(m[1], 10);
    if (m.index > last) {
      parts.push(
        <React.Fragment key={`${keyPrefix}t${key++}`}>
          {text.slice(last, m.index)}
        </React.Fragment>,
      );
    }
    const citation = citations[n - 1];
    parts.push(
      <CitationInline
        key={`${keyPrefix}c${key++}`}
        n={n}
        citation={citation}
        onOpen={onOpen}
      />,
    );
    const pks = citationFigurePks(citation);
    if (pks.length > 0) {
      // Stable across Strict Mode remounts (unlike useId). Distinct
      // [N] matches in the same node differ by m.index; distinct
      // markdown blocks differ by `text`.
      const ownerId = `${text}\0${keyPrefix}${m.index}:${n}`;
      const newPks = pks.filter((pk) =>
        claimFigureOnce(shownFigures, `${n}:${pk}`, ownerId),
      );
      if (newPks.length > 0) {
        parts.push(
          <CitationFigures
            key={`${keyPrefix}f${key++}`}
            citation={citation}
            pks={newPks}
          />,
        );
      }
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parts.push(
      <React.Fragment key={`${keyPrefix}t${key++}`}>
        {text.slice(last)}
      </React.Fragment>,
    );
  }
  return parts;
}

function Cited({ children }) {
  const { citations, onOpen, shownFigures } = React.useContext(CitationContext);
  if (!citations || citations.length === 0) return children;
  const out = [];
  React.Children.forEach(children, (child, idx) => {
    if (typeof child === "string") {
      out.push(
        ...splitTextWithCitations(
          child, citations, onOpen, `c${idx}-`, shownFigures,
        ),
      );
    } else {
      out.push(child);
    }
  });
  return out;
}

// Component overrides for react-markdown.
//
// Block-level margin/spacing lives in the global `.anila-msg-body …` rules
// (index.html) so a) we can target nested list contexts with a specificity
// strong enough to beat default CommonMark "loose list" <p> wrapping, and
// b) unit-level tweaks don't require rebuilding the JS bundle. Only the
// h4 renderer keeps an inline rule because it needs a muted color that
// doesn't exist as a standalone CSS class.
const components = {
  p: ({ node, children, ...props }) => <p {...props}><Cited>{children}</Cited></p>,
  ul: ({ node, ordered, ...props }) => <ul {...props} />,
  ol: ({ node, ordered, ...props }) => <ol {...props} />,
  li: ({ node, ordered, children, ...props }) => <li {...props}><Cited>{children}</Cited></li>,
  h1: ({ node, children, ...props }) => <h1 {...props}><Cited>{children}</Cited></h1>,
  h2: ({ node, children, ...props }) => <h2 {...props}><Cited>{children}</Cited></h2>,
  h3: ({ node, children, ...props }) => <h3 {...props}><Cited>{children}</Cited></h3>,
  h4: ({ node, children, ...props }) => (
    <h4 style={{ fontSize: 13, fontWeight: 600, margin: "10px 0 4px", color: "var(--fg-muted)" }} {...props}>
      <Cited>{children}</Cited>
    </h4>
  ),
  strong: ({ node, children, ...props }) => <strong {...props}><Cited>{children}</Cited></strong>,
  em: ({ node, children, ...props }) => <em {...props}><Cited>{children}</Cited></em>,
  del: ({ node, children, ...props }) => <del {...props}><Cited>{children}</Cited></del>,
  code: ({ node, inline, className, children, ...props }) => {
    if (inline) {
      return (
        <code
          style={{
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: 4,
            padding: "1px 6px",
            fontSize: "0.92em",
            fontFamily: "var(--font-mono)",
          }}
          {...props}
        >
          {children}
        </code>
      );
    }
    // Block: leave the className ("hljs language-xxx") so rehype-highlight's
    // inline spans + CSS theme take effect.
    return (
      <code className={className} style={{ fontFamily: "var(--font-mono)" }} {...props}>
        {children}
      </code>
    );
  },
  pre: CodeBlock,
  blockquote: ({ node, ...props }) => (
    <blockquote
      style={{
        borderLeft: "3px solid var(--border-strong)",
        margin: "8px 0",
        padding: "2px 0 2px 10px",
        color: "var(--fg-muted)",
      }}
      {...props}
    />
  ),
  table: Table,
  th: ({ node, children, ...props }) => (
    <th
      style={{
        border: "1px solid var(--border)",
        padding: "5px 10px",
        background: "var(--bg-subtle)",
        fontWeight: 600,
        textAlign: "left",
      }}
      {...props}
    >
      <Cited>{children}</Cited>
    </th>
  ),
  td: ({ node, children, ...props }) => (
    <td style={{ border: "1px solid var(--border)", padding: "5px 10px" }} {...props}>
      <Cited>{children}</Cited>
    </td>
  ),
  a: ({ node, children, ...props }) => (
    <a style={{ color: "var(--accent)" }} target="_blank" rel="noopener noreferrer" {...props}>
      <Cited>{children}</Cited>
    </a>
  ),
  // 生成的圖片(image-generator agent 回傳的 markdown `![](data:image/...)`
  // / 上傳預覽 / 其他 image)在訊息區塊中央顯示,點擊放大檢視。
  img: MarkdownImage,
  hr: () => (
    <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "10px 0" }} />
  ),
};

// Highlight.js will silently skip unknown languages, but also swallow
// auto-detection if the class isn't `language-xxx` at all — passing
// `{ detect: true, ignoreMissing: true }` so fenced code blocks without
// a language tag still get colored, and unknown langs don't throw.
const rehypePlugins = [
  rehypeKatex,
  [rehypeHighlight, { detect: true, ignoreMissing: true }],
];
const remarkPlugins = [remarkGfm, remarkMath];

export function MarkdownView({
  text,
  citations,
  onOpenCitation,
  messageId = null,
  conversationId = null,
  streaming = false,
  streamState = null,
  finishReason = null,
}) {
  const preview = useArtifactPreview();
  const shownFiguresRef = useRef(new Map());
  // Each MarkdownView render re-assigns first-wins; Strict Mode's second
  // invoke of the same Cited re-claims via ownerId and still renders once.
  shownFiguresRef.current.clear();
  const citationValue = React.useMemo(
    () => ({
      citations: citations || null,
      onOpen: onOpenCitation || null,
      shownFigures: shownFiguresRef.current,
    }),
    [citations, onOpenCitation],
  );
  const messageValue = React.useMemo(
    () => ({
      messageId: messageId ?? null,
      conversationId: conversationId ?? null,
      streaming: Boolean(streaming),
      streamState: streamState ?? null,
      finishReason: finishReason ?? null,
    }),
    [messageId, conversationId, streaming, streamState, finishReason],
  );
  useEffect(() => {
    const ticket = preview?.pendingRevision;
    if (!ticket?.messageId || !messageId) return;
    if (ticket.messageId !== messageId) return;
    if (
      ticket.conversationId != null && ticket.conversationId !== ""
      && conversationId != null && conversationId !== ""
      && String(ticket.conversationId) !== String(conversationId)
    ) {
      return;
    }
    if (streaming) return;
    if (!canConsumeRevisionTurn({ streaming: false, streamState, finishReason })) {
      preview.settleRevision?.(messageId);
      return;
    }
    const picked = extractRevisionCandidate(text, ticket.kind);
    if (picked && preview.consumeRevision?.({
      messageId,
      kind: ticket.kind,
      source: picked.source,
      conversationId,
    })) {
      preview.openArtifact({
        kind: ticket.kind,
        source: picked.source,
        language: picked.language || ticket.kind,
      });
    }
    preview.settleRevision?.(messageId);
  }, [text, messageId, conversationId, streaming, streamState, finishReason, preview]);
  return (
    <MarkdownMessageContext.Provider value={messageValue}>
      <CitationContext.Provider value={citationValue}>
        <div className="anila-markdown">
          <ReactMarkdown
            remarkPlugins={remarkPlugins}
            rehypePlugins={rehypePlugins}
            components={components}
          >
            {preprocessLatex(text || "")}
          </ReactMarkdown>
        </div>
      </CitationContext.Provider>
    </MarkdownMessageContext.Provider>
  );
}
