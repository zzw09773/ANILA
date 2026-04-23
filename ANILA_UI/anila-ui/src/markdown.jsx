// Markdown + LaTeX + syntax-highlighted code + copyable table/code.
//
// LLMs commonly emit LaTeX in the `\[ ... \]` / `\( ... \)` escape form rather
// than the `$$ ... $$` / `$ ... $` dollar form that `remark-math` expects.
// We rewrite the escape form to dollars before the markdown parser runs.
import React, { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "katex/dist/katex.min.css";
import "highlight.js/styles/github-dark.css";

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
      body: "（Router 未能依分析結果分派；以下為分析過程）",
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

// ── Code block: language label + syntax highlight (via rehype-highlight) +
//    copy button. rehype-highlight has already injected <span class="hljs-…">
//    children on the <code>, so the CSS theme takes over visually.
function CodeBlock({ node, children, ...props }) {
  const ref = useRef(null);
  const codeNode = node?.children?.find((c) => c.tagName === "code");
  const classes = codeNode?.properties?.className || [];
  const langClass = Array.isArray(classes)
    ? classes.find((c) => typeof c === "string" && c.startsWith("language-"))
    : "";
  const lang = langClass ? String(langClass).replace("language-", "") : "";
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
      <CopyButton getText={() => ref.current?.innerText || ""} />
      <pre
        ref={ref}
        {...props}
        style={{
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          padding: "10px 12px",
          paddingTop: lang ? 28 : 10,
          paddingRight: 56,
          overflowX: "auto",
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
    <div style={{ position: "relative", overflowX: "auto", margin: "8px 0" }}>
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

// Component overrides for react-markdown.
//
// Block-level margin/spacing lives in the global `.anila-msg-body …` rules
// (index.html) so a) we can target nested list contexts with a specificity
// strong enough to beat default CommonMark "loose list" <p> wrapping, and
// b) unit-level tweaks don't require rebuilding the JS bundle. Only the
// h4 renderer keeps an inline rule because it needs a muted color that
// doesn't exist as a standalone CSS class.
const components = {
  p: ({ node, ...props }) => <p {...props} />,
  ul: ({ node, ordered, ...props }) => <ul {...props} />,
  ol: ({ node, ordered, ...props }) => <ol {...props} />,
  li: ({ node, ordered, ...props }) => <li {...props} />,
  h1: ({ node, ...props }) => <h1 {...props} />,
  h2: ({ node, ...props }) => <h2 {...props} />,
  h3: ({ node, ...props }) => <h3 {...props} />,
  h4: ({ node, ...props }) => (
    <h4 style={{ fontSize: 13, fontWeight: 600, margin: "10px 0 4px", color: "var(--fg-muted)" }} {...props} />
  ),
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
  th: ({ node, ...props }) => (
    <th
      style={{
        border: "1px solid var(--border)",
        padding: "5px 10px",
        background: "var(--bg-subtle)",
        fontWeight: 600,
        textAlign: "left",
      }}
      {...props}
    />
  ),
  td: ({ node, ...props }) => (
    <td style={{ border: "1px solid var(--border)", padding: "5px 10px" }} {...props} />
  ),
  a: ({ node, ...props }) => (
    <a style={{ color: "var(--accent)" }} target="_blank" rel="noopener noreferrer" {...props} />
  ),
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

export function MarkdownView({ text }) {
  return (
    <div className="anila-markdown">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {preprocessLatex(text || "")}
      </ReactMarkdown>
    </div>
  );
}
