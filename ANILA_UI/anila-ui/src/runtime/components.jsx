import React, { useEffect, useMemo, useRef, useState } from "react";

import {
  AnilaGlyph,
  IconAt,
  IconBook,
  IconChevronDown,
  IconChevronRight,
  IconColumns,
  IconCopy,
  IconExternal,
  IconFile,
  IconImage,
  IconKey,
  IconLock,
  IconLogout,
  IconMessage,
  IconNodes,
  IconPaperclip,
  IconRoute,
  IconSearch,
  IconSend,
  IconSettings,
  IconStar,
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

export function Input({ label, hint, error, className = "", ...props }) {
  return (
    <label className={`ui-field ${className}`.trim()}>
      {label ? <span className="ui-field-label">{label}</span> : null}
      <span className={`ui-input-shell ${error ? "is-error" : ""}`.trim()}>
        <input className="ui-input" {...props} />
      </span>
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

export function AgentPill({ agent, emphasis = "neutral" }) {
  if (!agent) {
    return null;
  }
  return (
    <span className={`agent-pill agent-pill-${emphasis}`.trim()}>
      <span className="agent-pill-dot" />
      {agent.short || agent.id}
    </span>
  );
}

function normalizeTag(tag) {
  return tag.trim().toLowerCase();
}

function parseMentions(text, agents = []) {
  const explicitAgents = [];
  const seen = new Set();
  const regex = /@([a-z0-9][\w-]*)/gi;
  let match;

  while ((match = regex.exec(text)) !== null) {
    const token = match[1].toLowerCase();
    const agent = agents.find(
      (item) =>
        item.id?.toLowerCase() === token ||
        item.short?.toLowerCase() === token ||
        item.name?.toLowerCase() === token,
    );
    if (agent && !seen.has(agent.id)) {
      explicitAgents.push(agent.id);
      seen.add(agent.id);
    }
  }

  return explicitAgents;
}

function maskValue(value, label) {
  if (label === "email") {
    const [local, domain] = value.split("@");
    return `${local.slice(0, 2)}***@${domain}`;
  }
  if (label === "api key") {
    return `${value.slice(0, 5)}***${value.slice(-3)}`;
  }
  if (label === "phone") {
    return `${value.slice(0, 2)}***${value.slice(-2)}`;
  }
  return `${value.slice(0, 2)}***`;
}

function detectPii(text) {
  if (!text) {
    return [];
  }

  const detectors = [
    { label: "email", regex: /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi },
    { label: "phone", regex: /\b(?:\+?\d[\d\s-]{7,}\d)\b/g },
    { label: "api key", regex: /\bsk-[A-Za-z0-9_-]{8,}\b/g },
  ];
  const hits = [];

  for (const detector of detectors) {
    let match;
    while ((match = detector.regex.exec(text)) !== null) {
      hits.push({
        label: detector.label,
        start: match.index,
        end: match.index + match[0].length,
        value: match[0],
        masked: maskValue(match[0], detector.label),
      });
    }
  }

  hits.sort((left, right) => left.start - right.start);
  return hits.filter((hit, index) => index === 0 || hit.start >= hits[index - 1].end);
}

function applyPiiMask(text, hits) {
  if (!hits.length) {
    return text;
  }

  let cursor = 0;
  let output = "";
  for (const hit of hits) {
    output += text.slice(cursor, hit.start);
    output += hit.masked;
    cursor = hit.end;
  }
  output += text.slice(cursor);
  return output;
}

function summarizePii(hits) {
  const counts = hits.reduce((accumulator, hit) => {
    accumulator[hit.label] = (accumulator[hit.label] || 0) + 1;
    return accumulator;
  }, {});
  return Object.entries(counts)
    .map(([label, count]) => `${count} ${label}`)
    .join(" · ");
}

function normalizeAttachment(file) {
  const kind = file.type?.startsWith("image/") ? "image" : "file";
  return {
    id: `${file.name}-${file.size}-${file.lastModified}`,
    name: file.name,
    size: file.size,
    kind,
    type: file.type || "",
    previewUrl: kind === "image" ? URL.createObjectURL(file) : "",
    localOnly: true,
  };
}

function formatAttachmentSize(size = 0) {
  if (!size) {
    return "0 B";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function AttachmentGlyph({ kind }) {
  return kind === "image" ? <IconImage size={12} /> : <IconFile size={12} />;
}

function formatAuditTimestamp(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-TW", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function stripMention(text, agentId) {
  const pattern = new RegExp(`(^|\\s)@${agentId}(?=\\s|$)`, "gi");
  return text.replace(pattern, " ").replace(/\s+/g, " ").trimStart();
}

function NavPill({ label, active = false, suffix = null, onClick }) {
  return (
    <button className={`sidebar-nav-pill ${active ? "is-active" : ""}`.trim()} onClick={onClick}>
      <span>{label}</span>
      {suffix ? <span className="sidebar-nav-pill-suffix">{suffix}</span> : null}
    </button>
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
  activeAgent,
  searchValue,
  onSearchChange,
  agentsCount,
  selectedConversation,
  folder,
  onChangeFolder,
  onToggleStar,
  onAddTag,
  onRemoveTag,
  notifications = [],
  onSelectNotification,
}) {
  const [tagDraft, setTagDraft] = useState("");

  useEffect(() => {
    setTagDraft("");
  }, [selectedConversation?.id]);

  return (
    <aside className="sidebar">
      <div className="sidebar-section sidebar-section-brand">
        <div className="brand-lockup">
          <AnilaGlyph size={28} />
          <div>
            <div className="brand-title">ANILA</div>
            <div className="brand-subtitle">企業 AI Runtime Client</div>
          </div>
        </div>
        <button className="icon-button" onClick={onOpenSettings}>
          <IconSettings />
        </button>
      </div>

      <div className="sidebar-runtime-strip">
        <span>runtime · healthy</span>
        <span>agents · {agentsCount}</span>
        <span>search · ready</span>
      </div>

      <div className="sidebar-section">
        <Button variant="primary" className="sidebar-new-chat" onClick={onNewChat}>
          <IconMessage size={14} /> 新對話
        </Button>
      </div>

      <div className="sidebar-section sidebar-nav-row">
        <NavPill label="對話" active />
        <NavPill label="Agents" suffix={agentsCount} />
        <Button className="sidebar-compare-button" onClick={onEnterCompare}>
          <IconColumns size={14} /> 比較
        </Button>
      </div>

      <div className="sidebar-section">
        <label className="sidebar-search">
          <IconSearch size={14} />
          <input
            value={searchValue}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="搜尋對話或 agent"
          />
        </label>
      </div>

      <div className="sidebar-folder-row">
        {[
          { id: "all", label: "全部" },
          { id: "starred", label: "收藏" },
          { id: "compared", label: "比較" },
        ].map((item) => (
          <button
            key={item.id}
            className={`sidebar-folder-pill ${folder === item.id ? "is-active" : ""}`.trim()}
            onClick={() => onChangeFolder?.(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="sidebar-section sidebar-agent-banner">
        <div className="sidebar-kicker">CURRENT TARGET</div>
        <div className="sidebar-agent-row">
          <div className="sidebar-agent-name">{activeAgent?.name || "ANILA Router"}</div>
          <AgentPill agent={activeAgent} emphasis="accent" />
        </div>
        <div className="sidebar-agent-description">
          {activeAgent?.description || "自動路由：依問題內容決定直接回答或分派 agent。"}
        </div>
      </div>

      {selectedConversation ? (
        <div className="sidebar-selection-panel">
          <div className="sidebar-selection-head">
            <div className="sidebar-kicker">SELECTED CHAT</div>
            <button
              className={`sidebar-star-button ${selectedConversation.starred ? "is-active" : ""}`.trim()}
              onClick={() => onToggleStar?.(selectedConversation.id)}
            >
              <IconStar size={13} />
              {selectedConversation.starred ? "已收藏" : "加入收藏"}
            </button>
          </div>

          {selectedConversation.tags?.length ? (
            <div className="sidebar-tag-list">
              {selectedConversation.tags.map((tag) => (
                <span className="sidebar-tag-chip" key={tag}>
                  {tag}
                  <button onClick={() => onRemoveTag?.(selectedConversation.id, tag)}>
                    <IconX size={10} />
                  </button>
                </span>
              ))}
            </div>
          ) : (
            <div className="sidebar-selection-note">目前沒有 tags。可新增用於 `tag:` 搜尋。</div>
          )}

          <form
            className="sidebar-tag-form"
            onSubmit={(event) => {
              event.preventDefault();
              const nextTag = normalizeTag(tagDraft);
              if (!nextTag) {
                return;
              }
              onAddTag?.(selectedConversation.id, nextTag);
              setTagDraft("");
            }}
          >
            <input
              value={tagDraft}
              onChange={(event) => setTagDraft(event.target.value)}
              placeholder="新增 tag，例如 hr"
            />
            <Button type="submit">加入</Button>
          </form>
        </div>
      ) : null}

      {notifications.length ? (
        <div className="sidebar-section sidebar-selection-panel">
          <div className="sidebar-selection-head">
            <div className="sidebar-kicker">LOCAL INBOX</div>
            <span className="sidebar-selection-note">{notifications.length} drafts</span>
          </div>
          <div className="sidebar-notification-list">
            {notifications.slice(0, 4).map((notification) => (
              <button
                key={notification.id}
                className="sidebar-notification-card"
                onClick={() => onSelectNotification?.(notification)}
              >
                <div className="sidebar-notification-title">{notification.title}</div>
                <div className="sidebar-notification-note">{notification.note}</div>
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="sidebar-list">
        <div className="sidebar-kicker">RECENT CHATS</div>
        {conversations.length === 0 ? (
          <div className="sidebar-empty">尚無對話紀錄。從新對話開始，Router 會帶出第一段 trace。</div>
        ) : (
          conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={`conversation-row ${selectedId === conversation.id ? "is-active" : ""}`.trim()}
              onClick={() => onSelect(conversation.id)}
            >
              <div className="conversation-title-row">
                <div className="conversation-title">{conversation.title}</div>
                <div className="conversation-flag-row">
                  {conversation.classified ? <IconLock size={12} /> : null}
                  {conversation.starred ? <IconStar size={12} /> : null}
                </div>
              </div>
              <div className="conversation-target-line">
                <span className="conversation-target-label">target</span>
                <span>{conversation.agentName || conversation.agentId || "ANILA Router"}</span>
              </div>
              {conversation.shareDraft?.status ? (
                <div className="conversation-target-line">
                  <span className="conversation-target-label">share</span>
                  <span>{conversation.shareDraft.status}</span>
                </div>
              ) : null}
              {conversation.handoffState?.status ? (
                <div className="conversation-target-line">
                  <span className="conversation-target-label">handoff</span>
                  <span>{conversation.handoffState.status}</span>
                </div>
              ) : null}
              {conversation.tags?.length ? (
                <div className="conversation-tag-row">
                  {conversation.tags.slice(0, 3).map((tag) => (
                    <span key={tag} className="conversation-tag-chip">
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}
              <div className="conversation-meta">
                <span>{conversation.updatedLabel || conversation.ts || "剛剛"}</span>
                <AgentPill
                  agent={{
                    id: conversation.agentId || conversation.agentName || "anila-router",
                    short: (conversation.agentName || conversation.agentId || "auto").slice(0, 8),
                  }}
                />
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
            <div className="sidebar-user-role">{user?.role || "runtime · user"}</div>
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
    <div className={`trace-card ${open ? "is-open" : ""}`.trim()}>
      <button className="trace-header" onClick={() => setOpen((value) => !value)}>
        <div className="trace-header-main">
          <IconRoute size={13} />
          <span className="trace-title">routing trace</span>
          <span className="trace-summary">{trace.length} steps</span>
          <span className="trace-status">{stageLabel || trace.at(-1)?.label}</span>
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
              <span className={`trace-dot trace-dot-${step.status || "ok"}`.trim()} />
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
    <span className={`confidence-chip level-${confidence.level || "medium"}`.trim()}>
      <span className="confidence-dot" />
      <span>{confidence.level || "unknown"}</span>
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

export function MessageBubble({
  message,
  agents,
  onOpenCitation,
  onPickFollowUp,
  classified = false,
  conversationId = "",
  hideAuditIds = false,
}) {
  const routedAgent = useMemo(
    () => agents.find((agent) => agent.id === message.routedAgentId),
    [agents, message.routedAgentId],
  );
  const auditText = [
    message.traceId ? `trace: ${message.traceId}` : "",
    conversationId ? `conv: ${conversationId}` : "",
    formatAuditTimestamp(message.createdAt),
    message.latencyMs ? `${message.latencyMs}ms` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  if (message.role === "user") {
    const piiSummary = message.piiHits?.length ? summarizePii(message.piiHits) : "";
    return (
      <div className="message-row is-user">
        <div className="message-card user-card">
          <div className="message-user-eyebrow">USER PROMPT</div>
          {classified ? <div className="classified-pill">CONFIDENTIAL</div> : null}
          {message.explicitAgents?.length ? (
            <div className="message-user-agent-row">
              {message.explicitAgents.map((agentId) => {
                const agent = agents.find((item) => item.id === agentId);
                return agent ? <AgentPill key={agentId} agent={agent} emphasis="accent" /> : null;
              })}
            </div>
          ) : null}
          <div className="message-user-text">{message.text}</div>
          {message.attachments?.length ? (
            <div className="message-attachment-row">
              {message.attachments.map((attachment) => (
                <div key={attachment.id || attachment.name} className="message-attachment-chip">
                  {attachment.kind === "image" && attachment.previewUrl ? (
                    <img
                      className="message-attachment-preview"
                      src={attachment.previewUrl}
                      alt={attachment.name}
                    />
                  ) : (
                    <AttachmentGlyph kind={attachment.kind} />
                  )}
                  <span>{attachment.name}</span>
                  <span>{formatAttachmentSize(attachment.size)}</span>
                  {attachment.localOnly ? <span className="local-draft-pill">local draft</span> : null}
                </div>
              ))}
            </div>
          ) : null}
          {piiSummary ? <div className="message-user-meta">PII · {piiSummary}</div> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="message-row">
      <article className="message-card assistant-card">
        <div className="assistant-head">
          <div className="assistant-head-main">
            <AnilaGlyph size={16} />
            <span className="assistant-name">ANILA</span>
            {classified ? <span className="classified-pill">CONFIDENTIAL</span> : null}
            {routedAgent ? (
              <>
                <IconChevronRight size={12} />
                <AgentPill agent={routedAgent} emphasis="accent" />
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

        <div className="assistant-footer">
          {message.citations?.length ? (
            <button className="source-link" onClick={() => onOpenCitation?.(message.citations[0])}>
              <IconBook size={12} /> 查看 {message.citations.length} 筆來源
            </button>
          ) : (
            <span className="assistant-footnote">回答未附來源，請依需求交叉查核。</span>
          )}

          {!message.streaming && auditText ? (
            <div className="message-audit">
              <span>{hideAuditIds ? "audit ready" : auditText}</span>
              <button
                className="icon-button"
                title="複製稽核資訊"
                onClick={() => navigator.clipboard?.writeText(auditText)}
              >
                <IconCopy size={14} />
              </button>
            </div>
          ) : null}
        </div>

        {!message.streaming ? (
          <div className="assistant-action-row">
            <button
              className="assistant-action-link"
              disabled={classified}
              onClick={() => navigator.clipboard?.writeText(message.text)}
            >
              <IconCopy size={12} /> 複製回答
            </button>
            {message.citations?.[0]?.source_uri ? (
              <a
                className="assistant-action-link"
                href={message.citations[0].source_uri}
                target="_blank"
                rel="noreferrer"
              >
                <IconExternal size={12} /> 第一來源
              </a>
            ) : null}
            {message.trace?.length ? (
              <span className="assistant-inline-meta">
                <IconRoute size={12} /> {message.trace.length} steps
              </span>
            ) : null}
            {auditText ? (
              <a
                className="assistant-action-link"
                href={`mailto:support@anila.local?subject=${encodeURIComponent("ANILA runtime trace")}&body=${encodeURIComponent(auditText)}`}
              >
                <IconMessage size={12} /> 回報問題
              </a>
            ) : null}
          </div>
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
      </article>
    </div>
  );
}

export function CitationsDrawer({ open, citations = [], activeId, onClose }) {
  const [selectedCitationId, setSelectedCitationId] = useState(activeId || citations[0]?.id || null);

  useEffect(() => {
    if (!open) {
      return;
    }
    setSelectedCitationId(activeId || citations[0]?.id || null);
  }, [activeId, citations, open]);

  if (!open) {
    return null;
  }

  const selectedCitation =
    citations.find((citation) => citation.id === selectedCitationId) || citations[0] || null;

  return (
    <aside className="citations-drawer">
      <div className="citations-header">
        <div className="citations-title">
          <IconBook size={14} />
          <span>引用來源</span>
        </div>
        <span className="citation-count">{citations.length} 筆</span>
        <button className="icon-button" onClick={onClose}>
          <IconX />
        </button>
      </div>
      {selectedCitation ? (
        <div className="citation-preview">
          <div className="citation-preview-topline">
            <span className="citation-preview-label">active source</span>
            <span className="citation-preview-score">
              {selectedCitation.score ? `${Math.round(selectedCitation.score * 100)}% match` : "linked"}
            </span>
          </div>
          <div className="citation-preview-title">{selectedCitation.title}</div>
          <div className="citation-preview-section">{selectedCitation.section}</div>
          <div className="citation-preview-snippet">{selectedCitation.snippet}</div>
          <div className="citation-preview-footer">
            <span>{selectedCitation.updated_at || selectedCitation.source_uri}</span>
            <a
              className="citation-open-link"
              href={selectedCitation.source_uri}
              target="_blank"
              rel="noreferrer"
            >
              開啟原文
              <IconExternal size={12} />
            </a>
          </div>
        </div>
      ) : null}
      <div className="citations-body">
        {citations.map((citation, index) => (
          <div
            key={citation.id || `${citation.title}-${index}`}
            className={`citation-card ${selectedCitationId === citation.id ? "is-active" : ""}`.trim()}
            onClick={() => setSelectedCitationId(citation.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setSelectedCitationId(citation.id);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="citation-card-head">
              <span className="citation-score">{citation.score ? `${Math.round(citation.score * 100)}%` : `[${index + 1}]`}</span>
              <div>
                <div className="citation-title">{citation.title}</div>
                <div className="citation-section">{citation.section}</div>
              </div>
            </div>
            <div className="citation-snippet">{citation.snippet}</div>
            {citation.score ? (
              <div className="citation-relevance">
                <div
                  className="citation-relevance-fill"
                  style={{ width: `${Math.round(citation.score * 100)}%` }}
                />
              </div>
            ) : null}
            <div className="citation-footer">
              <span>{citation.updated_at || citation.source_uri}</span>
              <a
                className="citation-open-link"
                href={citation.source_uri}
                target="_blank"
                rel="noreferrer"
              >
                開啟原文
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
  agents = [],
  selectedAgentId,
  onChangeAgent,
  classified = false,
}) {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [redactionMode, setRedactionMode] = useState("mask");
  const fileInputRef = useRef(null);
  const piiHits = useMemo(() => detectPii(value), [value]);
  const explicitAgents = useMemo(() => parseMentions(value, agents), [agents, value]);
  const piiSummary = useMemo(() => summarizePii(piiHits), [piiHits]);

  function appendAttachments(fileList) {
    const nextItems = Array.from(fileList || []).map(normalizeAttachment);
    if (!nextItems.length) {
      return;
    }
    setAttachments((current) => {
      const existingIds = new Set(current.map((item) => item.id));
      return [...current, ...nextItems.filter((item) => !existingIds.has(item.id))];
    });
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = value.trim();
        if (!trimmed || disabled) {
          return;
        }
        if (redactionMode === "block" && piiHits.length) {
          return;
        }
        if (
          classified &&
          !window.confirm("此對話已標記為機密，送出內容將僅保留在本地草稿與稽核脈絡中。確定送出？")
        ) {
          return;
        }
        const finalText = redactionMode === "mask" ? applyPiiMask(trimmed, piiHits) : trimmed;
        onSubmit(finalText, {
          explicitAgents,
          piiHits,
          redactionMode,
          attachments,
        });
        setValue("");
        setAttachments([]);
      }}
    >
      {explicitAgents.length || piiHits.length ? (
        <div className="composer-signal-row">
          {explicitAgents.length ? (
            <div className="composer-signal-block">
              <span className="composer-target-label">mentions</span>
              <div className="composer-agent-preview">
                {explicitAgents.map((agentId) => {
                  const agent = agents.find((item) => item.id === agentId);
                  return agent ? <AgentPill key={agentId} agent={agent} emphasis="accent" /> : null;
                })}
              </div>
            </div>
          ) : null}
          {piiHits.length ? (
            <div className="composer-signal-block">
              <span className="composer-target-label">privacy</span>
              <span className="composer-signal-text">{piiSummary}</span>
              <div className="composer-mode-row">
                {["warn", "mask", "block"].map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    className={`composer-mode-pill ${redactionMode === mode ? "is-active" : ""}`.trim()}
                    onClick={() => setRedactionMode(mode)}
                  >
                    {mode}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="composer-topline">
        <div className="composer-target">
          <span className="composer-target-label">target</span>
          <span className="composer-target-shell">
            <AnilaGlyph size={14} />
            <select value={selectedAgentId} onChange={(event) => onChangeAgent?.(event.target.value)}>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
          </span>
        </div>
        <div className="composer-footer">{footer}</div>
      </div>

      <div className="composer-shell">
        {explicitAgents.length ? (
          <div className="composer-mention-row">
            {explicitAgents.map((agentId) => {
              const agent = agents.find((item) => item.id === agentId);
              if (!agent) {
                return null;
              }
              return (
                <div key={agentId} className="composer-mention-chip">
                  <span>@{agent.short || agent.id}</span>
                  <button
                    type="button"
                    onClick={() => setValue((current) => stripMention(current, agent.id))}
                    aria-label={`移除 ${agent.name}`}
                  >
                    <IconX size={10} />
                  </button>
                </div>
              );
            })}
            <span className="composer-draft-note">tokenized mention · local draft</span>
          </div>
        ) : null}
        {attachments.length ? (
          <div className="composer-attachment-row">
            {attachments.map((attachment) => (
              <div key={attachment.id} className="composer-attachment-chip">
                {attachment.kind === "image" && attachment.previewUrl ? (
                  <img
                    className="composer-attachment-preview"
                    src={attachment.previewUrl}
                    alt={attachment.name}
                  />
                ) : (
                  <AttachmentGlyph kind={attachment.kind} />
                )}
                <span>{attachment.name}</span>
                <span>{formatAttachmentSize(attachment.size)}</span>
                {attachment.localOnly ? <span className="local-draft-pill">local draft</span> : null}
                <button
                  type="button"
                  className="composer-attachment-remove"
                  onClick={() =>
                    setAttachments((current) => current.filter((item) => item.id !== attachment.id))
                  }
                  aria-label={`移除附件 ${attachment.name}`}
                >
                  <IconX size={10} />
                </button>
              </div>
            ))}
          </div>
        ) : null}
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={placeholder}
          rows={4}
          disabled={disabled}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="composer-actions">
          <div className="composer-shortcuts">
            <button
              type="button"
              className="composer-inline-button"
              onClick={() => setValue((current) => `${current}${current.endsWith(" ") || !current ? "" : " "}@`)}
            >
              <IconAt size={12} /> @agent
            </button>
            <button
              type="button"
              className="composer-inline-button"
              onClick={() => fileInputRef.current?.click()}
            >
              <IconPaperclip size={12} /> 附件
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="composer-file-input"
              accept="image/*,.pdf,.txt,.md,.csv,.json"
              onChange={(event) => {
                appendAttachments(event.target.files);
                event.target.value = "";
              }}
            />
            <span className="composer-draft-note">附件目前只保留在前端 local draft，不會上傳到後端。</span>
            <span>Shift+Enter 換行 · Enter 送出</span>
            {value ? <span>· {value.length} 字</span> : null}
            {piiHits.length ? <span>· {piiHits.length} PII</span> : null}
            {attachments.length ? <span>· {attachments.length} 附件</span> : null}
          </div>
          <Button type="submit" variant="primary" disabled={disabled || !value.trim()}>
            <IconSend size={14} /> 送出
          </Button>
        </div>
      </div>
    </form>
  );
}

export function ApiKeyGate({ error, apiKeyDraft, setApiKeyDraft, onSubmit, loading }) {
  return (
    <div className="api-key-gate">
      <div className="api-key-gate-card">
        <div className="api-key-gate-kicker">ENTRY POINT</div>
        <div className="api-key-gate-title">
          <IconLock size={16} /> 需要一把有效的 CSP API Key
        </div>
        <p className="api-key-gate-body">
          JWT 只負責控制面登入。進入 runtime 後，每次 `/v1/agents` 與 `/v1/chat/completions`
          仍會帶上你自己的 API Key，由 CSP 代理並寫入稽核。
        </p>
        <Input
          label="CSP API Key"
          value={apiKeyDraft}
          onChange={(event) => setApiKeyDraft(event.target.value)}
          placeholder="sk-..."
          hint="只存於本次瀏覽器 session"
          error={error}
        />
        <Button variant="primary" onClick={onSubmit} disabled={loading}>
          <IconKey size={14} /> {loading ? "驗證中…" : "驗證並進入 Runtime"}
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
  hideAuditIds = false,
  onToggleHideAuditIds,
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
          hint="更新後會重新驗證 `/v1/agents` 並同步 target 清單"
        />
        <label className="settings-toggle">
          <input
            type="checkbox"
            checked={hideAuditIds}
            onChange={(event) => onToggleHideAuditIds?.(event.target.checked)}
          />
          <span>隱藏 trace / conversation ID 顯示，但保留複製稽核資訊能力</span>
        </label>
        <Button variant="primary" onClick={onSaveApiKey} disabled={loading}>
          {loading ? "驗證中…" : "儲存並重新驗證"}
        </Button>
      </div>
    </Modal>
  );
}

export function ClassifiedBanner({ conversation }) {
  if (!conversation?.classified) {
    return null;
  }

  return (
    <div className="classified-banner">
      <div className="classified-banner-title">
        <IconLock size={14} /> 機密對話已啟用
      </div>
      <div className="classified-banner-body">
        此對話禁止分享，複製操作視為受限；所有輸入與回應都應視為需稽核內容。前端會把它視為不可逆 local lock。
      </div>
    </div>
  );
}

export function HandoffDialog({
  open,
  onClose,
  agents,
  currentAgentId,
  onSubmit,
}) {
  const [mode, setMode] = useState("agent");
  const [nextAgentId, setNextAgentId] = useState(currentAgentId);
  const [recipient, setRecipient] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) {
      return;
    }
    setMode("agent");
    setNextAgentId(currentAgentId);
    setRecipient("");
    setNote("");
  }, [currentAgentId, open]);

  return (
    <Modal open={open} onClose={onClose} title="Handoff Draft" subtitle="建立 agent 或同事交接草稿">
      <div className="settings-stack">
        <div className="dialog-tab-row">
          {[
            { id: "agent", label: "交接給 agent" },
            { id: "user", label: "交接給同事" },
          ].map((item) => (
            <button
              key={item.id}
              type="button"
              className={`dialog-tab-button ${mode === item.id ? "is-active" : ""}`.trim()}
              onClick={() => setMode(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <label className="ui-field">
          <span className="ui-field-label">{mode === "agent" ? "Next Agent" : "Colleague"}</span>
          {mode === "agent" ? (
            <span className="ui-input-shell">
              <select
                className="ui-input"
                value={nextAgentId}
                onChange={(event) => setNextAgentId(event.target.value)}
              >
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </span>
          ) : (
            <span className="ui-input-shell">
              <input
                className="ui-input"
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                placeholder="輸入帳號或 email，例如 alice.chen"
              />
            </span>
          )}
        </label>
        <label className="ui-field">
          <span className="ui-field-label">Handoff Note</span>
          <span className="ui-input-shell">
            <input
              className="ui-input"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="可選：補充交接理由或上下文"
            />
          </span>
        </label>
        <div className="dialog-draft-note">
          {mode === "agent"
            ? "Agent handoff 會更新目前 target 並留下 handoff chain。"
            : "User handoff 目前只建立 local inbox draft，不會寫入後端通知。"}
        </div>
        <Button
          variant="primary"
          disabled={mode === "user" && !recipient.trim()}
          onClick={() => {
            onSubmit({
              type: mode,
              targetId: mode === "agent" ? nextAgentId : recipient.trim(),
              targetLabel: mode === "agent"
                ? agents.find((agent) => agent.id === nextAgentId)?.name || nextAgentId
                : recipient.trim(),
              note: note.trim(),
            });
            onClose();
          }}
        >
          確認交接
        </Button>
      </div>
    </Modal>
  );
}

export function ShareDialog({
  open,
  onClose,
  conversation,
  messages = [],
  user,
  draft,
  onSaveDraft,
}) {
  const [expiry, setExpiry] = useState("24h");
  const [mode, setMode] = useState("readonly");
  const [recipientScope, setRecipientScope] = useState("org");
  const [recipients, setRecipients] = useState("");
  const [allowFork, setAllowFork] = useState(true);

  useEffect(() => {
    if (!open) {
      return;
    }
    setExpiry(draft?.expiry || "24h");
    setMode(draft?.mode || "readonly");
    setRecipientScope(draft?.recipientScope || "org");
    setRecipients((draft?.recipients || []).join(", "));
    setAllowFork(draft?.allowFork ?? true);
  }, [draft, open]);

  const shareText = useMemo(() => {
    if (!conversation) {
      return "";
    }
    const relevantMessages = messages
      .filter((message) => message.role === "assistant" || message.role === "user")
      .slice(-6)
      .map((message) => {
        const attachmentLine = message.attachments?.length
          ? `\nAttachments: ${message.attachments
              .map((attachment) => `${attachment.name} (${formatAttachmentSize(attachment.size)})`)
              .join(", ")}`
          : "";
        return `${message.role === "user" ? "User" : "ANILA"}: ${message.text}${attachmentLine}`;
      })
      .join("\n\n");

    return [
      `Conversation: ${conversation.title}`,
      `Owner: ${user?.username || "runtime-user"}`,
      `Target: ${conversation.agentName || conversation.agentId || "anila-router"}`,
      `Share mode: ${mode}`,
      `Expiry: ${expiry}`,
      `Recipient scope: ${recipientScope}`,
      `Allow fork: ${allowFork ? "yes" : "no"}`,
      "",
      relevantMessages,
    ].join("\n");
  }, [allowFork, conversation, expiry, messages, mode, recipientScope, user?.username]);
  const localLink = conversation ? `local://share/${conversation.id}` : "";

  return (
    <Modal open={open} onClose={onClose} title="Share Draft" subtitle="建立唯讀分享草稿；目前僅保留在前端 local draft">
      <div className="settings-stack">
        <div className="dialog-form-grid">
          <label className="ui-field">
            <span className="ui-field-label">Expiry</span>
            <span className="ui-input-shell">
              <select className="ui-input" value={expiry} onChange={(event) => setExpiry(event.target.value)}>
                {["1h", "24h", "7d", "never"].map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </span>
          </label>
          <label className="ui-field">
            <span className="ui-field-label">Recipient Scope</span>
            <span className="ui-input-shell">
              <select
                className="ui-input"
                value={recipientScope}
                onChange={(event) => setRecipientScope(event.target.value)}
              >
                <option value="org">組織內</option>
                <option value="specific">指定人</option>
              </select>
            </span>
          </label>
          <label className="ui-field">
            <span className="ui-field-label">Mode</span>
            <span className="ui-input-shell">
              <select className="ui-input" value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="readonly">唯讀</option>
                <option value="editable">可編輯草稿</option>
              </select>
            </span>
          </label>
          <label className="ui-field">
            <span className="ui-field-label">Recipients</span>
            <span className="ui-input-shell">
              <input
                className="ui-input"
                value={recipients}
                onChange={(event) => setRecipients(event.target.value)}
                placeholder="選填，多人可用逗號分隔"
              />
            </span>
          </label>
        </div>
        <label className="settings-toggle">
          <input
            type="checkbox"
            checked={allowFork}
            onChange={(event) => setAllowFork(event.target.checked)}
          />
          <span>允許被分享者 fork 到自己的空間</span>
        </label>
        <div className="share-dialog-card">
          <div className="share-dialog-title">{conversation?.title || "未選取對話"}</div>
          <div className="local-draft-pill local-draft-pill-static">local draft</div>
          <div className="share-dialog-meta">{localLink || "尚未建立本地分享識別"}</div>
          <div className="share-dialog-body">{shareText || "目前沒有可分享內容。"}</div>
        </div>
        <div className="share-preview-card">
          <div className="share-preview-banner">
            這是 {user?.username || "runtime-user"} 分享的對話預覽（{mode}）
          </div>
          <div className="share-preview-body">
            後端尚未啟用分享連結、view log、fork permission；目前僅保留本地草稿與預覽。
          </div>
        </div>
        <div className="dialog-action-row">
          <Button
            onClick={() =>
              onSaveDraft?.({
                mode,
                expiry,
                allowFork,
                recipientScope,
                recipients: recipients
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean),
                status: "local draft",
                localLink,
              })
            }
          >
            儲存本地草稿
          </Button>
          <Button
            variant="primary"
            disabled={!shareText}
            onClick={() => navigator.clipboard?.writeText(`${localLink}\n\n${shareText}`)}
          >
            複製分享內容
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function ComparePanel({
  columns,
  agents,
  onChangeAgent,
  children,
  onExit,
  onMerge,
  columnSummaries = [],
}) {
  return (
    <div className="compare-shell">
      <div className="compare-header">
        <div>
          <div className="workspace-kicker">MULTI-AGENT</div>
          <div className="compare-title">
            <IconColumns size={13} /> 平行比較
          </div>
          <div className="compare-subtitle">同一個 prompt，並排檢視回答品質、trace 與來源密度。</div>
        </div>
        <div className="compare-header-actions">
          {onMerge ? <Button onClick={onMerge}>合併結果</Button> : null}
          <Button onClick={onExit}>退出比較</Button>
        </div>
      </div>
      <div className="compare-overview-row">
        {columns.map((column) => {
          const summary = columnSummaries.find((item) => item.id === column.id);
          const agent = agents.find((item) => item.id === column.agentId);
          return (
            <div className="compare-overview-card" key={column.id}>
              <div className="compare-overview-head">
                <span className="compare-column-label">column</span>
                {agent ? <AgentPill agent={agent} emphasis="accent" /> : null}
              </div>
              <div className="compare-overview-grid">
                <div>
                  <div className="compare-overview-metric">{summary?.status || "waiting"}</div>
                  <div className="compare-overview-label">status</div>
                </div>
                <div>
                  <div className="compare-overview-metric">{summary?.traceSteps ?? 0}</div>
                  <div className="compare-overview-label">trace</div>
                </div>
                <div>
                  <div className="compare-overview-metric">{summary?.citations ?? 0}</div>
                  <div className="compare-overview-label">sources</div>
                </div>
                <div>
                  <div className="compare-overview-metric">{summary?.confidence || "n/a"}</div>
                  <div className="compare-overview-label">confidence</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div className="compare-grid" style={{ gridTemplateColumns: `repeat(${columns.length}, 1fr)` }}>
        {columns.map((column) => (
          <div className="compare-column" key={column.id}>
            <label className="compare-column-label">column target</label>
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

export function EmptyState({
  title,
  body,
  actions,
  suggestions = [],
  onSelectSuggestion,
}) {
  return (
    <div className="empty-state">
      <div className="empty-state-mark">
        <AnilaGlyph size={28} />
      </div>
      <div className="empty-state-kicker">CORE RUNTIME</div>
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-body">{body}</div>
      {suggestions.length ? (
        <div className="empty-state-grid">
          {suggestions.map((suggestion) => (
            <button
              key={suggestion.question}
              className="empty-state-suggestion"
              onClick={() => onSelectSuggestion?.(suggestion)}
            >
              <div className="empty-state-suggestion-title">{suggestion.title}</div>
              <div className="empty-state-suggestion-detail">{suggestion.detail}</div>
            </button>
          ))}
        </div>
      ) : null}
      {actions ? <div className="empty-state-actions">{actions}</div> : null}
    </div>
  );
}
