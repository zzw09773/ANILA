import React, { useEffect, useMemo, useState } from "react";

import {
  AnilaGlyph,
  IconArrowRight,
  IconBook,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconColumns,
  IconCopy,
  IconExternal,
  IconKey,
  IconLock,
  IconLogout,
  IconMessage,
  IconNodes,
  IconRoute,
  IconSearch,
  IconSend,
  IconSettings,
  IconUser,
  IconX,
} from "./icons.jsx";

export function Button({ children, variant = "default", className = "", ...props }) {
  return (
    <button className={`ui-button ui-button-${variant} ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}

export function Input({ label, hint, error, ...props }) {
  return (
    <label className="ui-field">
      {label ? <span className="ui-field-label">{label}</span> : null}
      <input className={`ui-input ${error ? "is-error" : ""}`} {...props} />
      {error ? <span className="ui-field-error">{error}</span> : null}
      {!error && hint ? <span className="ui-field-hint">{hint}</span> : null}
    </label>
  );
}

export function Modal({ open, title, subtitle, onClose, children }) {
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const onEsc = (event) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <div className="modal-title">{title}</div>
            {subtitle ? <div className="modal-subtitle">{subtitle}</div> : null}
          </div>
          <button className="icon-button" onClick={onClose}>
            <IconX />
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function AgentPill({ agent }) {
  if (!agent) {
    return null;
  }
  return (
    <span className="agent-pill">
      <span className="agent-pill-dot" />
      {agent.short || agent.id}
    </span>
  );
}

export function Sidebar({
  conversations,
  selectedId,
  onSelect,
  onNewChat,
  user,
  onLogout,
  onOpenSettings,
  onEnterCompare,
}) {
  return (
    <aside className="sidebar">
      <div className="brand-row">
        <div className="brand-lockup">
          <AnilaGlyph size={24} />
          <div>
            <div className="brand-title">ANILA</div>
            <div className="brand-subtitle">Runtime Client</div>
          </div>
        </div>
        <button className="icon-button" onClick={onOpenSettings}>
          <IconSettings />
        </button>
      </div>

      <div className="sidebar-actions">
        <Button variant="primary" onClick={onNewChat}>
          <IconMessage size={14} /> 新對話
        </Button>
        <Button onClick={onEnterCompare}>
          <IconColumns size={14} /> 比較模式
        </Button>
      </div>

      <div className="sidebar-list">
        {conversations.length === 0 ? (
          <div className="sidebar-empty">尚無對話紀錄</div>
        ) : (
          conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={`conversation-row ${selectedId === conversation.id ? "is-active" : ""}`}
              onClick={() => onSelect(conversation.id)}
            >
              <div className="conversation-title">{conversation.title}</div>
              <div className="conversation-meta">
                <span>{conversation.agentName}</span>
                <span>{conversation.updatedLabel}</span>
              </div>
            </button>
          ))
        )}
      </div>

      <div className="sidebar-user">
        <div className="sidebar-user-main">
          <div className="avatar-pill">
            <IconUser size={14} />
          </div>
          <div>
            <div className="sidebar-user-name">{user?.username || "未登入"}</div>
            <div className="sidebar-user-role">{user?.role || "runtime"}</div>
          </div>
        </div>
        <button className="icon-button" onClick={onLogout}>
          <IconLogout />
        </button>
      </div>
    </aside>
  );
}

export function RoutingTrace({ trace = [], stageLabel, routedAgent }) {
  const [open, setOpen] = useState(false);

  if (!trace.length) {
    return null;
  }

  return (
    <div className="trace-card">
      <button className="trace-header" onClick={() => setOpen((value) => !value)}>
        <div className="trace-header-main">
          <IconRoute size={13} />
          <span>routing trace</span>
          <span className="trace-header-muted">{stageLabel || trace.at(-1)?.label}</span>
        </div>
        <div className="trace-header-main">
          {routedAgent ? <AgentPill agent={routedAgent} /> : null}
          <IconChevronDown size={14} />
        </div>
      </button>
      {open ? (
        <div className="trace-rows">
          {trace.map((step, index) => (
            <div key={`${step.label}-${index}`} className="trace-row">
              <span className={`trace-dot trace-dot-${step.status || "ok"}`} />
              <span className="trace-label">{step.label}</span>
              <span className="trace-detail">{step.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ConfidenceChip({ confidence }) {
  if (!confidence) {
    return null;
  }
  return (
    <span className={`confidence-chip level-${confidence.level || "medium"}`}>
      {confidence.level || "unknown"}
      {confidence.score ? <span>{confidence.score.toFixed(2)}</span> : null}
    </span>
  );
}

function CitationInline({ index, citation, onOpen }) {
  return (
    <button className="citation-inline" onClick={() => onOpen?.(citation)}>
      [{index}]
    </button>
  );
}

export function renderTextWithCitations(text, citations = [], onOpen) {
  if (!text) {
    return null;
  }
  if (!citations.length) {
    return text;
  }

  const regex = /\[(\d+)\]/g;
  const output = [];
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(text)) !== null) {
    const markerIndex = Number(match[1]);
    if (match.index > lastIndex) {
      output.push(text.slice(lastIndex, match.index));
    }
    output.push(
      <CitationInline
        key={`${match.index}-${markerIndex}`}
        index={markerIndex}
        citation={citations[markerIndex - 1]}
        onOpen={onOpen}
      />,
    );
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    output.push(text.slice(lastIndex));
  }

  return output;
}

export function MessageBubble({ message, agents, onOpenCitation, onPickFollowUp }) {
  const routedAgent = useMemo(
    () => agents.find((agent) => agent.id === message.routedAgentId),
    [agents, message.routedAgentId],
  );

  if (message.role === "user") {
    return (
      <div className="message-row is-user">
        <div className="message-card user-card">{message.text}</div>
      </div>
    );
  }

  return (
    <div className="message-row">
      <div className="assistant-head">
        <div className="assistant-head-main">
          <AnilaGlyph size={16} />
          <span className="assistant-name">ANILA</span>
          {routedAgent ? (
            <>
              <IconChevronRight size={12} />
              <AgentPill agent={routedAgent} />
            </>
          ) : null}
        </div>
        {message.streaming ? (
          <span className="assistant-stage">{message.stageLabel || "thinking"}…</span>
        ) : (
          <ConfidenceChip confidence={message.confidence} />
        )}
      </div>

      {message.handoffChain?.length ? (
        <div className="handoff-card">
          <div className="handoff-title">
            <IconNodes size={13} /> agent handoff
          </div>
          <div className="handoff-chain">
            {message.handoffChain.map((step, index) => (
              <div key={`${step.agent_id}-${index}`} className="handoff-step">
                <div className="handoff-step-name">{step.agent_id}</div>
                <div className="handoff-step-label">{step.label}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <RoutingTrace
        trace={message.trace}
        stageLabel={message.stageLabel}
        routedAgent={routedAgent}
      />

      <div className="assistant-body">
        {renderTextWithCitations(message.text, message.citations, onOpenCitation)}
        {message.streaming ? <span className="typing-cursor" /> : null}
      </div>

      {message.citations?.length ? (
        <button className="source-link" onClick={() => onOpenCitation?.(message.citations[0])}>
          <IconBook size={12} /> 查看 {message.citations.length} 筆來源
        </button>
      ) : null}

      {message.followUps?.length ? (
        <div className="follow-ups">
          {message.followUps.map((suggestion) => (
            <button key={suggestion} className="follow-up-chip" onClick={() => onPickFollowUp(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}

      {!message.streaming && message.traceId ? (
        <div className="message-audit">
          <span>{message.traceId}</span>
          {message.latencyMs ? <span>{message.latencyMs}ms</span> : null}
          <button
            className="icon-button"
            title="複製回答"
            onClick={() => navigator.clipboard?.writeText(message.text)}
          >
            <IconCopy size={14} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function CitationsDrawer({ open, citations = [], activeId, onClose }) {
  if (!open) {
    return null;
  }

  return (
    <aside className="citations-drawer">
      <div className="citations-header">
        <div className="citations-title">
          <IconBook size={14} />
          來源
        </div>
        <button className="icon-button" onClick={onClose}>
          <IconX />
        </button>
      </div>
      <div className="citations-body">
        {citations.map((citation, index) => (
          <div
            key={citation.id || `${citation.title}-${index}`}
            className={`citation-card ${activeId === citation.id ? "is-active" : ""}`}
          >
            <div className="citation-card-head">
              <span className="citation-score">[{index + 1}]</span>
              <div>
                <div className="citation-title">{citation.title}</div>
                <div className="citation-section">{citation.section}</div>
              </div>
            </div>
            <div className="citation-snippet">{citation.snippet}</div>
            <div className="citation-footer">
              <span>{citation.updated_at || citation.source_uri}</span>
              <a href={citation.source_uri} target="_blank" rel="noreferrer">
                <IconExternal size={12} />
              </a>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

export function Composer({
  disabled,
  placeholder,
  onSubmit,
  footer,
}) {
  const [value, setValue] = useState("");

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = value.trim();
        if (!trimmed || disabled) {
          return;
        }
        onSubmit(trimmed);
        setValue("");
      }}
    >
      <textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder={placeholder}
        rows={4}
        disabled={disabled}
      />
      <div className="composer-actions">
        <div className="composer-footer">{footer}</div>
        <Button type="submit" variant="primary" disabled={disabled || !value.trim()}>
          <IconSend size={14} /> 送出
        </Button>
      </div>
    </form>
  );
}

export function ApiKeyGate({ error, apiKeyDraft, setApiKeyDraft, onSubmit, loading }) {
  return (
    <div className="api-key-gate">
      <div className="api-key-gate-card">
        <div className="api-key-gate-title">
          <IconLock size={16} /> 需要有效的 CSP API Key
        </div>
        <p>
          JWT 已驗證成功，但 runtime data plane 仍需使用者自己的 API Key 呼叫
          `/v1/agents` 與 `/v1/chat/completions`。
        </p>
        <Input
          label="CSP API Key"
          value={apiKeyDraft}
          onChange={(event) => setApiKeyDraft(event.target.value)}
          placeholder="sk-..."
          hint="只會存到本次瀏覽器 session"
          error={error}
        />
        <Button variant="primary" onClick={onSubmit} disabled={loading}>
          <IconKey size={14} /> {loading ? "驗證中…" : "驗證並進入"}
        </Button>
      </div>
    </div>
  );
}

export function SettingsDialog({
  open,
  onClose,
  apiKeyDraft,
  setApiKeyDraft,
  onSaveApiKey,
  apiKeyError,
  loading,
}) {
  return (
    <Modal open={open} onClose={onClose} title="Runtime 設定" subtitle="更新 API Key 與執行偏好">
      <div className="settings-stack">
        <Input
          label="目前的 CSP API Key"
          value={apiKeyDraft}
          onChange={(event) => setApiKeyDraft(event.target.value)}
          placeholder="sk-..."
          error={apiKeyError}
          hint="更新後會重新驗證 `/v1/agents`"
        />
        <Button variant="primary" onClick={onSaveApiKey} disabled={loading}>
          {loading ? "驗證中…" : "儲存並重新驗證"}
        </Button>
      </div>
    </Modal>
  );
}

export function ComparePanel({ columns, agents, onChangeAgent, children, onExit }) {
  return (
    <div className="compare-shell">
      <div className="compare-header">
        <div className="compare-title">
          <IconColumns size={13} /> 比較模式
        </div>
        <Button onClick={onExit}>退出比較</Button>
      </div>
      <div className="compare-grid" style={{ gridTemplateColumns: `repeat(${columns.length}, 1fr)` }}>
        {columns.map((column) => (
          <div className="compare-column" key={column.id}>
            <select
              className="agent-select"
              value={column.agentId}
              onChange={(event) => onChangeAgent(column.id, event.target.value)}
            >
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
            {children(column)}
          </div>
        ))}
      </div>
    </div>
  );
}

export function EmptyState({ title, body }) {
  return (
    <div className="empty-state">
      <AnilaGlyph size={26} />
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-body">{body}</div>
    </div>
  );
}

export const icons = {
  IconArrowRight,
  IconBook,
  IconCheck,
  IconChevronDown,
  IconColumns,
  IconKey,
  IconLogout,
  IconSearch,
  IconSettings,
};
