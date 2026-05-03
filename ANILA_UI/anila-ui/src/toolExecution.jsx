// Sprint 13 PR B3 — specialised renderers for tool_call_started /
// tool_call_finished events.
//
// The Sprint 9-12 backend emits typed tool-call events; the UI gets a
// payload like:
//
//   started:  { tool_call_id, tool_name, input }
//   finished: { tool_call_id, tool_name, is_error, output_preview }
//
// Generic display would just dump JSON — not great for shell output,
// patches, or directory listings. <ToolExecutionWidget> picks a
// per-tool renderer based on `tool_name` and falls back to a plain
// preformatted preview otherwise.

import React, { useState } from "react";


/**
 * Top-level tool-call widget. `call` carries the merged shape:
 *
 *   {
 *     tool_call_id, tool_name, input?,
 *     status: "running" | "ok" | "error",
 *     output_preview?: string,
 *   }
 */
export function ToolExecutionWidget({ call }) {
  if (!call) return null;
  const { tool_name: toolName, status = "running" } = call;
  const Renderer = pickRenderer(toolName);

  return (
    <details
      style={widgetStyle}
      open={status === "running" || status === "error"}
    >
      <summary style={summaryStyle}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <StatusDot status={status} />
          <code style={inlineCodeStyle}>{toolName}</code>
        </span>
        <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>
          {status === "running" && "執行中…"}
          {status === "ok" && "完成"}
          {status === "error" && "錯誤"}
        </span>
      </summary>
      <div style={{ padding: "8px 10px" }}>
        {call.input !== undefined && (
          <InputPreview input={call.input} />
        )}
        {call.output_preview !== undefined && (
          <Renderer output={call.output_preview} status={status} />
        )}
      </div>
    </details>
  );
}


// ---------------------------------------------------------------------
// Renderer selection
// ---------------------------------------------------------------------

const SHELL_TOOLS = new Set(["exec_bash", "exec_python", "shell"]);
const DIFF_TOOLS = new Set(["apply_patch", "file_edit", "edit"]);
const FILE_LIST_TOOLS = new Set(["glob", "ls", "list_files"]);


export function pickRenderer(toolName) {
  if (SHELL_TOOLS.has(toolName)) return TerminalOutput;
  if (DIFF_TOOLS.has(toolName)) return DiffOutput;
  if (FILE_LIST_TOOLS.has(toolName)) return FileTreeOutput;
  return PlainOutput;
}


// ---------------------------------------------------------------------
// Renderers
// ---------------------------------------------------------------------

/**
 * Terminal-style mono block with status-colored border. Used for
 * exec_bash / exec_python output.
 */
export function TerminalOutput({ output = "", status = "ok" }) {
  return (
    <pre
      style={{
        margin: "6px 0 0",
        padding: "8px 10px",
        background: "#0e1116",
        color: "#e6edf3",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        lineHeight: 1.5,
        borderRadius: "var(--radius)",
        border: status === "error"
          ? "1px solid var(--danger, #d73a49)"
          : "1px solid #2c313a",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        maxHeight: 320,
        overflow: "auto",
      }}
    >
      {output || "(empty)"}
    </pre>
  );
}


/**
 * Render a unified-diff-style or V4A-patch output. Lines are coloured
 * by leading character so add/delete/context are visually distinct.
 */
export function DiffOutput({ output = "" }) {
  if (!output) {
    return <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>(no diff returned)</div>;
  }
  const lines = output.split("\n");
  return (
    <pre
      style={{
        margin: "6px 0 0",
        padding: 0,
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        lineHeight: 1.5,
        maxHeight: 320,
        overflow: "auto",
      }}
    >
      {lines.map((line, idx) => (
        <DiffLine key={idx} line={line} />
      ))}
    </pre>
  );
}


function DiffLine({ line }) {
  const c = line[0];
  let bg = "transparent";
  let color = "var(--fg)";
  if (c === "+" && !line.startsWith("+++")) {
    bg = "rgba(46, 160, 67, 0.15)";
    color = "var(--success, #2ea043)";
  } else if (c === "-" && !line.startsWith("---")) {
    bg = "rgba(248, 81, 73, 0.12)";
    color = "var(--danger, #f85149)";
  } else if (line.startsWith("@@") || line.startsWith("***")) {
    bg = "var(--bg-subtle)";
    color = "var(--fg-muted)";
  } else if (line.startsWith("+++") || line.startsWith("---")) {
    color = "var(--fg-muted)";
  }
  return (
    <div style={{ background: bg, color, padding: "0 8px", whiteSpace: "pre" }}>
      {line || " "}
    </div>
  );
}


/**
 * Render a list of file paths as a collapsible tree. Each path with
 * the same prefix is grouped under that prefix (one level deep —
 * we don't try to fully reconstruct the tree from a flat path list).
 */
export function FileTreeOutput({ output = "" }) {
  let entries = [];
  // Accept either a JSON-stringified array or a newline-separated list.
  try {
    const parsed = JSON.parse(output);
    if (Array.isArray(parsed)) {
      entries = parsed.map((x) => String(x));
    }
  } catch {
    entries = output.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  if (entries.length === 0) {
    return <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>(no matches)</div>;
  }
  return (
    <ul
      style={{
        margin: "6px 0 0",
        padding: "8px 12px",
        listStyle: "none",
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        lineHeight: 1.6,
        maxHeight: 280,
        overflow: "auto",
      }}
    >
      {entries.map((path) => (
        <li key={path} style={{ color: "var(--fg)" }}>
          <span style={{ color: "var(--fg-muted)", marginRight: 6 }}>•</span>
          {path}
        </li>
      ))}
    </ul>
  );
}


/**
 * Generic fallback — preformatted text with no special parsing.
 */
export function PlainOutput({ output = "" }) {
  return (
    <pre
      style={{
        margin: "6px 0 0",
        padding: "8px 10px",
        background: "var(--bg)",
        color: "var(--fg)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        lineHeight: 1.5,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        maxHeight: 280,
        overflow: "auto",
      }}
    >
      {output || "(no output)"}
    </pre>
  );
}


// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function StatusDot({ status }) {
  const color =
    status === "running" ? "var(--accent, #0066cc)"
    : status === "error" ? "var(--danger, #d73a49)"
    : "var(--success, #2ea043)";
  return (
    <span
      style={{
        display: "inline-block",
        width: 7,
        height: 7,
        borderRadius: 999,
        background: color,
        ...(status === "running"
          ? { animation: "pulse 1.5s ease-in-out infinite" }
          : null),
      }}
    />
  );
}


function InputPreview({ input }) {
  if (input == null) return null;
  const text =
    typeof input === "string" ? input : JSON.stringify(input, null, 2);
  if (!text) return null;
  return (
    <details>
      <summary
        style={{
          fontSize: 11,
          color: "var(--fg-muted)",
          cursor: "pointer",
          userSelect: "none",
          padding: "2px 0",
        }}
      >
        Input
      </summary>
      <pre
        style={{
          margin: "4px 0 8px",
          padding: "6px 8px",
          background: "var(--bg)",
          fontSize: 11,
          color: "var(--fg)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          fontFamily: "var(--font-mono)",
          whiteSpace: "pre-wrap",
          maxHeight: 160,
          overflow: "auto",
        }}
      >
        {text}
      </pre>
    </details>
  );
}


// ---------------------------------------------------------------------
// Style constants
// ---------------------------------------------------------------------

const widgetStyle = {
  margin: "8px 0",
  background: "var(--bg-subtle)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
};

const summaryStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "6px 10px",
  fontSize: 12,
  cursor: "pointer",
  userSelect: "none",
};

const inlineCodeStyle = {
  fontFamily: "var(--font-mono)",
  fontSize: 11,
  padding: "1px 5px",
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: 4,
  color: "var(--fg)",
};
