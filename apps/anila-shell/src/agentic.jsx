// Sprint 13 PR B2 — agentic-loop UI surfaces.
//
// Components added in this PR are wired into chat.jsx by Sprint 13's
// streaming loop. Each one corresponds to a single Sprint 9-12 typed
// SSE event:
//
//   <PausedBadge>      anila.interrupt_requested → reset by anila.resumed
//   <InterruptCard>    anila.interrupt_requested  (ask_user / plan)
//   <TodoChecklist>    anila.todos_updated
//   <FollowUpChips>    anila.follow_ups
//
// All components are styling-only (no fetch, no async). The parent
// owns answer submission so the same Card can serve a fresh
// interrupt and a replayed-from-history interrupt without duplicating
// the resume call.

import React, { useState } from "react";
import { Button } from "./components.jsx";
import { IconCheck, IconAlert } from "./icons.jsx";


/**
 * Small badge shown next to the assistant message header while a
 * pause is in flight. Disappears once anila.resumed clears the
 * paused state in the parent.
 *
 * @param {{ kind?: string, label?: string }} props
 */
export function PausedBadge({ kind = "ask_user", label }) {
  const text = label || (kind === "plan"
    ? "等待您核准計畫"
    : kind === "tool_approval"
    ? "等待工具授權"
    : "等待您回答");
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 8px",
        fontSize: 11,
        fontWeight: 500,
        background: "var(--bg-subtle)",
        color: "var(--fg-muted)",
        border: "1px dashed var(--border)",
        borderRadius: 999,
        lineHeight: 1.4,
      }}
      title="Agent 已暫停，請於下方互動以繼續"
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: 999,
          background: "var(--accent, #0066cc)",
          animation: "pulse 1.5s ease-in-out infinite",
        }}
      />
      {text}
    </span>
  );
}


/**
 * Render an `anila.interrupt_requested` payload as an interactive card
 * the user can answer. Three kinds:
 *
 *   * ask_user        — question + options (radio / checkbox / textbox)
 *   * plan            — markdown plan + accept/decline
 *   * tool_approval   — tool name + input preview + approve/deny
 *
 * `onSubmit(answer)` receives the value the parent should pass to
 * `streamSessionAnswer`. Disabled state is handled internally while
 * the submission is in flight. After a successful resume the parent
 * passes `answer` and the card collapses to a one-line summary.
 *
 * @param {object} props
 * @param {string} props.kind
 * @param {object} props.payload
 * @param {(answer: any) => Promise<void> | void} props.onSubmit
 * @param {boolean} [props.disabled] — set by parent once the resume
 *   is in flight; cleared when anila.resumed arrives.
 * @param {object} [props.answer] — persisted `{selected, other_text}`
 */
export function InterruptCard({
  kind,
  payload = {},
  onSubmit,
  disabled = false,
  answer = null,
}) {
  if (answer) {
    const summary = kind === "ask_user"
      ? formatAskUserSummary(payload, answer)
      : kind === "plan"
        ? (answer.decision === "accept" ? "已核准計畫" : "已拒絕計畫")
        : kind === "tool_approval"
          ? (answer.approved ? "已授權工具" : "已拒絕工具")
          : "已回覆";
    return (
      <div style={cardWrapperStyle} data-testid="interrupt-summary">
        {summary}
      </div>
    );
  }
  if (kind === "ask_user") {
    return (
      <AskUserCard
        payload={payload}
        onSubmit={onSubmit}
        disabled={disabled}
      />
    );
  }
  if (kind === "plan") {
    return (
      <PlanCard payload={payload} onSubmit={onSubmit} disabled={disabled} />
    );
  }
  if (kind === "tool_approval") {
    return (
      <ToolApprovalCard
        payload={payload}
        onSubmit={onSubmit}
        disabled={disabled}
      />
    );
  }
  // Unknown kind — surface raw payload so debugging is possible.
  return (
    <div style={cardWrapperStyle}>
      <div style={cardHeaderStyle}>未知中斷類型: {kind}</div>
      <pre style={{
        margin: 0,
        fontSize: 11,
        color: "var(--fg-muted)",
        whiteSpace: "pre-wrap",
      }}>
        {JSON.stringify(payload, null, 2)}
      </pre>
    </div>
  );
}


function optionValue(opt) {
  if (opt && typeof opt === "object") {
    return String(opt.value ?? opt.label ?? "");
  }
  return String(opt ?? "");
}

function optionLabel(opt) {
  if (opt && typeof opt === "object") {
    return String(opt.label ?? opt.value ?? "");
  }
  return String(opt ?? "");
}

function optionDescription(opt) {
  if (opt && typeof opt === "object") {
    return String(opt.description ?? "").trim();
  }
  return "";
}

/**
 * One-line recap kept on the message after a successful ask_user resume.
 * Example: `已選擇：重點摘要；補充：主管閱讀、一頁以內`
 */
export function formatAskUserSummary(payload = {}, answer = {}) {
  if (typeof answer === "string") {
    const trimmed = answer.trim();
    return trimmed ? `補充：${trimmed}` : "已回答";
  }
  const options = Array.isArray(payload.options) ? payload.options : [];
  const selected = Array.isArray(answer.selected) ? answer.selected : [];
  const labels = selected
    .map((value) => {
      const match = options.find((opt) => optionValue(opt) === String(value));
      return match ? optionLabel(match) : String(value);
    })
    .filter(Boolean);
  const other = String(answer.other_text ?? "").trim();
  const parts = [];
  if (labels.length) parts.push(`已選擇：${labels.join("、")}`);
  if (other) parts.push(`補充：${other}`);
  return parts.join("；") || "已回答";
}

export function normalizeInterrupt(raw) {
  if (!raw || typeof raw !== "object") return null;
  const hasAnswer = raw.answer != null && raw.answer !== "";
  return {
    ...raw,
    status: raw.status || (hasAnswer ? "answered" : "pending"),
  };
}

function AskUserCard({ payload, onSubmit, disabled }) {
  const {
    question = "(no question)",
    options = [],
    multi_select: multiSelect = false,
  } = payload;
  const [selected, setSelected] = useState(multiSelect ? [] : "");
  const [other, setOther] = useState("");
  const [busy, setBusy] = useState(false);

  const toggle = (value) => {
    if (multiSelect) {
      setSelected((prev) =>
        prev.includes(value) ? prev.filter((x) => x !== value) : [...prev, value],
      );
    } else {
      setSelected(value);
    }
  };

  const handleSubmit = async () => {
    const selectedValues = multiSelect
      ? [...selected]
      : selected ? [selected] : [];
    const otherText = other.trim();
    if (selectedValues.length === 0 && !otherText) return;
    setBusy(true);
    try {
      await onSubmit?.({ selected: selectedValues, other_text: otherText });
    } finally {
      setBusy(false);
    }
  };

  const isDisabled = disabled || busy;

  return (
    <div style={cardWrapperStyle}>
      <div style={cardHeaderStyle}>
        <IconAlert width={14} height={14} />
        Agent 請您回答
      </div>
      <div style={{ fontSize: 13, color: "var(--fg)", margin: "8px 0 12px" }}>
        {question}
      </div>
      {options.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {options.map((opt, index) => {
            const value = optionValue(opt);
            const label = optionLabel(opt);
            const description = optionDescription(opt);
            const checked = multiSelect
              ? selected.includes(value)
              : selected === value;
            return (
              <label
                key={value || index}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 8,
                  fontSize: 12,
                  color: "var(--fg)",
                  cursor: isDisabled ? "not-allowed" : "pointer",
                  opacity: isDisabled ? 0.6 : 1,
                }}
              >
                <input
                  type={multiSelect ? "checkbox" : "radio"}
                  name="ask_user_option"
                  checked={checked}
                  onChange={() => toggle(value)}
                  disabled={isDisabled}
                  style={{ marginTop: 2 }}
                />
                <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span>{label}</span>
                  {description ? (
                    <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>
                      {description}
                    </span>
                  ) : null}
                </span>
              </label>
            );
          })}
        </div>
      )}
      <input
        type="text"
        placeholder="或輸入其他回應…"
        value={other}
        onChange={(e) => setOther(e.target.value)}
        disabled={isDisabled}
        style={{
          marginTop: 8,
          width: "100%",
          padding: "6px 8px",
          fontSize: 12,
          background: "var(--bg)",
          color: "var(--fg)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
        }}
      />
      <div style={{ marginTop: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button
          variant="primary"
          size="sm"
          onClick={handleSubmit}
          disabled={isDisabled}
        >
          {busy ? "送出中…" : "送出回答"}
        </Button>
      </div>
    </div>
  );
}


function PlanCard({ payload, onSubmit, disabled }) {
  const { plan = "" } = payload;
  const [busy, setBusy] = useState(false);

  const send = async (decision) => {
    setBusy(true);
    try {
      await onSubmit?.({ decision });
    } finally {
      setBusy(false);
    }
  };

  const isDisabled = disabled || busy;

  return (
    <div style={cardWrapperStyle}>
      <div style={cardHeaderStyle}>
        <IconAlert width={14} height={14} />
        Agent 提出計畫，等待您核准
      </div>
      <pre style={{
        margin: "8px 0 12px",
        padding: "8px 10px",
        fontSize: 12,
        color: "var(--fg)",
        background: "var(--bg)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        whiteSpace: "pre-wrap",
        fontFamily: "var(--font-sans)",
      }}>
        {plan || "(空計畫)"}
      </pre>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button
          size="sm"
          onClick={() => send("decline")}
          disabled={isDisabled}
        >
          拒絕
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => send("accept")}
          disabled={isDisabled}
        >
          {busy ? "送出中…" : "核准計畫"}
        </Button>
      </div>
    </div>
  );
}


function ToolApprovalCard({ payload, onSubmit, disabled }) {
  const {
    tool_name: toolName = "(unknown tool)",
    tool_input: toolInput,
    description = "",
  } = payload;
  const [busy, setBusy] = useState(false);

  const send = async (approved) => {
    setBusy(true);
    try {
      await onSubmit?.({ approved });
    } finally {
      setBusy(false);
    }
  };

  const isDisabled = disabled || busy;

  return (
    <div style={cardWrapperStyle}>
      <div style={cardHeaderStyle}>
        <IconAlert width={14} height={14} />
        Agent 想呼叫工具 <code style={inlineCodeStyle}>{toolName}</code>
      </div>
      {description && (
        <div style={{ fontSize: 12, color: "var(--fg-muted)", margin: "6px 0 8px" }}>
          {description}
        </div>
      )}
      {toolInput !== undefined && (
        <pre style={{
          margin: "8px 0 12px",
          padding: "8px 10px",
          fontSize: 11,
          color: "var(--fg)",
          background: "var(--bg)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          whiteSpace: "pre-wrap",
          fontFamily: "var(--font-mono)",
          maxHeight: 180,
          overflow: "auto",
        }}>
          {typeof toolInput === "string"
            ? toolInput
            : JSON.stringify(toolInput, null, 2)}
        </pre>
      )}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button
          size="sm"
          onClick={() => send(false)}
          disabled={isDisabled}
        >
          拒絕
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => send(true)}
          disabled={isDisabled}
        >
          {busy ? "送出中…" : "授權執行"}
        </Button>
      </div>
    </div>
  );
}


/**
 * Render the agent's task board (`anila.todos_updated` payload).
 * Read-only — the agent owns mutations via the TodoWrite tool.
 *
 * @param {{ todos: Array<{ id: string, content: string, status: string }> }} props
 */
export function TodoChecklist({ todos = [] }) {
  if (!Array.isArray(todos) || todos.length === 0) return null;
  return (
    <div style={{
      ...cardWrapperStyle,
      background: "var(--bg)",
      borderColor: "var(--border)",
    }}>
      <div style={cardHeaderStyle}>📋 任務清單</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
        {todos.map((t) => (
          <TodoRow key={t.id || t.content} todo={t} />
        ))}
      </div>
    </div>
  );
}


function TodoRow({ todo }) {
  const status = todo?.status || "pending";
  const done = status === "completed";
  const inProgress = status === "in_progress";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        fontSize: 12,
        lineHeight: 1.45,
        color: done ? "var(--fg-muted)" : "var(--fg)",
        textDecoration: done ? "line-through" : "none",
      }}
    >
      <span
        style={{
          flex: "0 0 16px",
          width: 16,
          height: 16,
          marginTop: 2,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          border: "1px solid var(--border)",
          borderRadius: 4,
          background: done
            ? "var(--accent, #0066cc)"
            : inProgress
            ? "var(--bg-subtle)"
            : "transparent",
          color: done ? "var(--accent-fg, white)" : "var(--fg-muted)",
        }}
      >
        {done ? <IconCheck width={10} height={10} /> : inProgress ? "•" : ""}
      </span>
      <span style={{ flex: 1 }}>{todo.content}</span>
    </div>
  );
}


/**
 * Render `anila.follow_ups` suggestions as clickable chips. Clicking a
 * chip calls `onPick(suggestion)` so the parent can submit it as the
 * next user turn.
 *
 * @param {{ suggestions: string[], onPick: (s: string) => void }} props
 */
export function FollowUpChips({ suggestions = [], onPick }) {
  if (!Array.isArray(suggestions) || suggestions.length === 0) return null;
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        marginTop: 12,
        paddingTop: 10,
        borderTop: "1px dashed var(--border)",
      }}
    >
      {suggestions.map((s) => (
        <button
          key={s}
          type="button"
          onClick={() => onPick?.(s)}
          style={{
            padding: "5px 10px",
            fontSize: 12,
            color: "var(--fg)",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: 999,
            cursor: "pointer",
            transition: "all .12s ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--accent-soft, var(--bg-elev))";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "var(--bg-subtle)";
          }}
        >
          {s}
        </button>
      ))}
    </div>
  );
}


// ---------------------------------------------------------------------
// Shared style constants
// ---------------------------------------------------------------------

const cardWrapperStyle = {
  margin: "8px 0",
  padding: "10px 12px",
  background: "var(--bg-subtle)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
};

const cardHeaderStyle = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: 12,
  fontWeight: 500,
  color: "var(--fg-muted)",
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
