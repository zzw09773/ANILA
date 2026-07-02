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

import { fetchTrace as defaultFetchTrace } from "./runtime/traces.js";


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


// ---------------------------------------------------------------------
// Slice 4d — flat persisted trace → SpanTreeViewer tree shape.
// ---------------------------------------------------------------------

/**
 * Parse a span timestamp — ISO-8601 string per the Trace Span Schema, or a
 * numeric epoch — into milliseconds. Returns null when unparseable.
 */
function toMillis(value) {
  if (value == null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}


function spanDurationMs(span) {
  const start = toMillis(span.started_at);
  const end = toMillis(span.ended_at);
  if (start == null || end == null) return null;
  const delta = end - start;
  return delta >= 0 ? delta : null;
}


function spanError(span, attributes) {
  if (span.status !== "error" || !attributes) return null;
  const candidate =
    attributes.error || attributes.error_message || attributes["exception.message"];
  return typeof candidate === "string" && candidate.length > 0 ? candidate : null;
}


function toTreeNode(span) {
  const attributes =
    span.attributes && typeof span.attributes === "object" ? span.attributes : undefined;
  const node = {
    span_id: span.span_id,
    name: span.name,
    kind: span.span_type,
    status: span.status || "unset",
    children: [],
  };
  const durationMs = spanDurationMs(span);
  if (durationMs != null) node.duration_ms = durationMs;
  if (attributes) node.attributes = attributes;
  const error = spanError(span, attributes);
  if (error) node.error = error;
  return node;
}


/**
 * Convert a flat, backend-shaped span list
 * (`{span_id, parent_span_id, span_type, name, started_at, ended_at,
 *   status, attributes, producer}`) into the recursive tree shape that
 * `SpanTreeViewer` renders (`{span_id, name, kind, status, duration_ms,
 *  attributes, error?, children:[...]}`).
 *
 * Rules:
 *   * ordering — stable, ascending by `started_at`; undated spans keep
 *     their input order and sort last.
 *   * orphan parents — a span whose `parent_span_id` points at a span not
 *     present in the list is promoted to a root (never dropped).
 *   * a span with no `parent_span_id` (or self-referential) is a root.
 *
 * Pure — no side effects. Safe on non-arrays (returns []).
 *
 * @param {Array<object>} spans
 * @returns {Array<object>} top-level tree nodes
 */
export function spansToTree(spans) {
  if (!Array.isArray(spans) || spans.length === 0) return [];

  // Stable sort by started_at; unparseable timestamps sort last but keep
  // their relative input order (index tiebreak).
  const ordered = spans
    .map((span, index) => ({ span, index }))
    .sort((a, b) => {
      const ka = toMillis(a.span?.started_at);
      const kb = toMillis(b.span?.started_at);
      if (ka == null && kb == null) return a.index - b.index;
      if (ka == null) return 1;
      if (kb == null) return -1;
      if (ka !== kb) return ka - kb;
      return a.index - b.index;
    })
    .map((entry) => entry.span);

  const nodeById = new Map();
  for (const span of ordered) {
    if (span == null || span.span_id == null) continue;
    nodeById.set(span.span_id, toTreeNode(span));
  }

  const roots = [];
  for (const span of ordered) {
    if (span == null || span.span_id == null) continue;
    const node = nodeById.get(span.span_id);
    const parentId = span.parent_span_id;
    const parent =
      parentId != null && parentId !== span.span_id ? nodeById.get(parentId) : null;
    if (parent) {
      parent.children.push(node);
    } else {
      // No parent, self-parent, or orphan (parent not in list) → root.
      roots.push(node);
    }
  }
  return roots;
}


/**
 * Slice 4d — Trace Explorer affordance.
 *
 * Renders a 「檢視軌跡」 control for a conversation that owns a
 * `taskTraceId`. On click it fetches the persisted trace, converts the
 * flat span list into the tree `SpanTreeViewer` renders, and shows it.
 * If live `anila.spans` already streamed a flat span list into the active
 * message, pass it as `liveSpans` — it renders immediately and is REPLACED
 * (simple swap, no merge) by the persisted spans on click.
 *
 * Failure / 404 / empty → inline muted 「尚無軌跡資料」 notice; never throws.
 *
 * @param {object} props
 * @param {string|null} [props.traceId] - the conversation's taskTraceId
 * @param {Array<object>|null} [props.liveSpans] - flat spans from live SSE
 * @param {(id: string) => Promise<{spans: Array<object>}|null>} [props.fetchTrace]
 */
export function TraceExplorer({ traceId, liveSpans = null, fetchTrace = defaultFetchTrace }) {
  const [tree, setTree] = useState(() =>
    Array.isArray(liveSpans) ? spansToTree(liveSpans) : [],
  );
  const [status, setStatus] = useState("idle"); // idle | loading | ready | empty
  const [loading, setLoading] = useState(false);

  if (!traceId) return null;

  async function loadPersisted() {
    if (loading) return;
    setLoading(true);
    let data = null;
    try {
      data = await fetchTrace(traceId);
    } finally {
      setLoading(false);
    }
    const spans = data && Array.isArray(data.spans) ? data.spans : null;
    if (!spans || spans.length === 0) {
      setTree([]);
      setStatus("empty");
      return;
    }
    setTree(spansToTree(spans));
    setStatus("ready");
  }

  const hasTree = Array.isArray(tree) && tree.length > 0;

  return (
    <div style={{ margin: "8px 0" }}>
      <button
        type="button"
        onClick={loadPersisted}
        disabled={loading}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 9px",
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          borderRadius: 999,
          fontSize: 11,
          color: "var(--fg-muted)",
          fontFamily: "var(--font-mono)",
          cursor: loading ? "default" : "pointer",
        }}
      >
        🔍 {loading ? "載入軌跡中…" : "檢視軌跡"}
      </button>
      {status === "empty" && !hasTree && (
        <div
          style={{
            marginTop: 6,
            fontSize: 11,
            color: "var(--fg-muted)",
            fontFamily: "var(--font-mono)",
          }}
        >
          尚無軌跡資料
        </div>
      )}
      {hasTree && (
        <div style={{ marginTop: 4 }}>
          <SpanTreeViewer tree={tree} devOnly={false} />
        </div>
      )}
    </div>
  );
}
