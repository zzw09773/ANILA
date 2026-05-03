// Sprint 13 PR B4 — OTel-style span tree dev viewer.
//
// Renders the parent-rooted span tree the backend tracing module
// produces (`InMemoryProcessor.to_tree()`). Use case: development
// only — toggle via a dev flag so users in production don't get a
// debug panel they can't action on.
//
// Input shape (per node, recursive `children`):
//
//   {
//     span_id, trace_id, parent_id, name, kind, status, error?,
//     start_ts, end_ts, duration_ms,
//     attributes: { ... }, events: [ ... ],
//     children: [...]
//   }

import React, { useState } from "react";


/**
 * Render a span tree alongside an assistant message. Returns nothing
 * if no spans were emitted.
 *
 * @param {object} props
 * @param {Array<object>} props.tree - top-level nodes
 * @param {boolean} [props.devOnly] - hide unless explicit dev flag is set
 */
export function SpanTreeViewer({ tree, devOnly = true }) {
  if (!Array.isArray(tree) || tree.length === 0) return null;
  if (devOnly && !isDevModeEnabled()) return null;

  const totalSpans = countSpans(tree);
  const totalDuration = sumRootDuration(tree);

  return (
    <details
      style={{
        margin: "8px 0",
        background: "var(--bg-subtle)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
      }}
    >
      <summary
        style={{
          padding: "6px 10px",
          fontSize: 12,
          color: "var(--fg-muted)",
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        🛠 Span tree · {totalSpans} spans
        {totalDuration != null && ` · ${formatMs(totalDuration)} total`}
      </summary>
      <div style={{ padding: "6px 8px 8px" }}>
        {tree.map((node) => (
          <SpanNode key={node.span_id} node={node} depth={0} />
        ))}
      </div>
    </details>
  );
}


function SpanNode({ node, depth }) {
  const hasChildren = Array.isArray(node.children) && node.children.length > 0;
  const [open, setOpen] = useState(depth < 2);

  const indent = depth * 14;
  const status = node.status || "unset";
  const dotColor = STATUS_COLORS[status] || "var(--fg-muted)";

  return (
    <div style={{ marginLeft: indent }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "2px 4px",
          fontSize: 11,
          fontFamily: "var(--font-mono)",
          color: status === "error" ? "var(--danger, #f85149)" : "var(--fg)",
          cursor: hasChildren ? "pointer" : "default",
          userSelect: "none",
        }}
        onClick={() => hasChildren && setOpen(!open)}
      >
        <span style={{ width: 10, color: "var(--fg-muted)" }}>
          {hasChildren ? (open ? "▾" : "▸") : "·"}
        </span>
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: 999,
            background: dotColor,
            flexShrink: 0,
          }}
        />
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
          <KindBadge kind={node.kind} />
          {node.name}
        </span>
        {node.duration_ms != null && (
          <span style={{ color: "var(--fg-muted)" }}>
            {formatMs(node.duration_ms)}
          </span>
        )}
      </div>
      {open && (Array.isArray(node.events) && node.events.length > 0) && (
        <div style={{ marginLeft: 14 }}>
          {node.events.map((evt, idx) => (
            <SpanEvent key={`${node.span_id}-evt-${idx}`} event={evt} />
          ))}
        </div>
      )}
      {open && node.error && (
        <div
          style={{
            marginLeft: 14,
            fontSize: 11,
            color: "var(--danger, #f85149)",
            fontFamily: "var(--font-mono)",
            padding: "1px 4px",
          }}
        >
          ✗ {node.error}
        </div>
      )}
      {open &&
        hasChildren &&
        node.children.map((child) => (
          <SpanNode key={child.span_id} node={child} depth={depth + 1} />
        ))}
    </div>
  );
}


function SpanEvent({ event }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontFamily: "var(--font-mono)",
        color: "var(--fg-muted)",
        padding: "1px 4px",
      }}
    >
      ◇ {event.name}
      {event.attributes && Object.keys(event.attributes).length > 0 && (
        <span style={{ marginLeft: 4 }}>
          {Object.entries(event.attributes)
            .map(([k, v]) => `${k}=${formatAttr(v)}`)
            .join(" ")}
        </span>
      )}
    </div>
  );
}


function KindBadge({ kind }) {
  if (!kind) return null;
  return (
    <span
      style={{
        display: "inline-block",
        marginRight: 6,
        padding: "0 4px",
        fontSize: 9,
        fontWeight: 600,
        textTransform: "uppercase",
        color: "var(--fg-muted)",
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: 3,
      }}
    >
      {kind}
    </span>
  );
}


// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

const STATUS_COLORS = {
  ok: "var(--success, #2ea043)",
  error: "var(--danger, #f85149)",
  unset: "var(--fg-muted)",
};


export function countSpans(tree) {
  if (!Array.isArray(tree)) return 0;
  let count = 0;
  for (const node of tree) {
    count += 1;
    if (Array.isArray(node.children)) count += countSpans(node.children);
  }
  return count;
}


export function sumRootDuration(tree) {
  if (!Array.isArray(tree) || tree.length === 0) return null;
  let total = 0;
  let any = false;
  for (const node of tree) {
    if (typeof node.duration_ms === "number") {
      total += node.duration_ms;
      any = true;
    }
  }
  return any ? total : null;
}


function formatMs(ms) {
  if (ms == null) return "";
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}


function formatAttr(value) {
  if (value === null || value === undefined) return String(value);
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "[object]";
    }
  }
  return String(value);
}


/**
 * Dev mode probe — spans are debug data, not for end users.
 *
 * Three opt-ins (any one enables):
 *
 *   * import.meta.env.DEV               (Vite default)
 *   * localStorage anila_dev=1          (manual toggle)
 *   * URL ?devspans=1
 */
export function isDevModeEnabled() {
  if (typeof window === "undefined") return false;
  try {
    if (window.localStorage?.getItem("anila_dev") === "1") return true;
  } catch {
    // ignore — private mode etc.
  }
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get("devspans") === "1") return true;
  } catch {
    // ignore — non-URL env
  }
  // Vite injects import.meta.env.DEV in dev; in prod build it's stripped.
  try {
    if (typeof import.meta !== "undefined" && import.meta.env?.DEV) {
      return true;
    }
  } catch {
    // ignore — non-vite env
  }
  return false;
}
