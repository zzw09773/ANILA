import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { apiKeyRequest, config } from "./api.js";
import { useAuth, useLogoutRedirect } from "./auth.jsx";
import {
  ApiKeyGate,
  Button,
  CitationsDrawer,
  ComparePanel,
  Composer,
  EmptyState,
  MessageBubble,
  SettingsDialog,
  Sidebar,
} from "./components.jsx";
import { AnilaGlyph, IconChevronDown, IconColumns, IconSearch } from "./icons.jsx";
import { streamChatCompletion } from "./sse.js";

const ROUTER_AGENT = {
  id: "anila-router",
  name: "ANILA Router",
  short: "auto",
  description: "Auto route via ANILA Router",
};

function makeId(prefix) {
  return `${prefix}-${Math.random().toString(16).slice(2, 10)}`;
}

function makeConversationTitle(text) {
  return text.length > 28 ? `${text.slice(0, 28)}…` : text;
}

function relativeLabel() {
  return "剛剛";
}

function normalizeAgents(data) {
  return [
    ROUTER_AGENT,
    ...data.map((item) => ({
      id: item.id,
      name: item.name || item.id,
      short: (item.id || "").slice(0, 8),
      description: item.description_for_router || "",
      endpointUrl: item.endpoint_url,
      capabilities: item.capabilities || {},
    })),
  ];
}

function chooseCompareColumns(agents) {
  const directAgents = agents.filter((agent) => agent.id !== ROUTER_AGENT.id);
  return directAgents.slice(0, 2).map((agent, index) => ({
    id: `col-${index + 1}`,
    agentId: agent.id,
  }));
}

export function RuntimePage() {
  const navigate = useNavigate();
  const logoutAndRedirect = useLogoutRedirect();
  const { apiKey, apiKeyStatus, updateApiKey, user } = useAuth();
  const [agents, setAgents] = useState([ROUTER_AGENT]);
  const [selectedAgentId, setSelectedAgentId] = useState(ROUTER_AGENT.id);
  const [conversations, setConversations] = useState([]);
  const [messagesByConversation, setMessagesByConversation] = useState({});
  const [selectedConversationId, setSelectedConversationId] = useState(null);
  const [compareMode, setCompareMode] = useState(false);
  const [compareColumns, setCompareColumns] = useState([]);
  const [compareMessages, setCompareMessages] = useState({});
  const [citationsOpen, setCitationsOpen] = useState(false);
  const [activeCitations, setActiveCitations] = useState([]);
  const [activeCitationId, setActiveCitationId] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiKeyDraft, setApiKeyDraft] = useState(apiKey || "");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [runtimeError, setRuntimeError] = useState("");
  const [loadingAgents, setLoadingAgents] = useState(false);

  const currentMessages = selectedConversationId
    ? messagesByConversation[selectedConversationId] || []
    : [];

  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId) || null,
    [conversations, selectedConversationId],
  );

  useEffect(() => {
    setApiKeyDraft(apiKey || "");
  }, [apiKey]);

  useEffect(() => {
    if (!apiKeyStatus.valid || !apiKey) {
      return;
    }
    refreshAgents();
  }, [apiKeyStatus.valid, apiKey]);

  async function refreshAgents() {
    setLoadingAgents(true);
    setRuntimeError("");
    try {
      const response = await apiKeyRequest(config.cspBaseUrl, "/v1/agents", apiKey);
      const data = await response.json();
      const normalized = normalizeAgents(data.data || []);
      setAgents(normalized);
      if (!normalized.some((agent) => agent.id === selectedAgentId)) {
        setSelectedAgentId(ROUTER_AGENT.id);
      }
      if (!compareColumns.length) {
        setCompareColumns(chooseCompareColumns(normalized));
      }
    } catch (error) {
      setRuntimeError(error.message || "無法載入 agent 清單");
      setAgents([ROUTER_AGENT]);
    } finally {
      setLoadingAgents(false);
    }
  }

  function ensureConversation(text, agentId) {
    const conversationId = selectedConversationId || makeId("cv");
    if (!selectedConversationId) {
      const agentName = agents.find((agent) => agent.id === agentId)?.name || agentId;
      setConversations((current) => [
        {
          id: conversationId,
          title: makeConversationTitle(text),
          updatedLabel: relativeLabel(),
          agentName,
        },
        ...current,
      ]);
      setSelectedConversationId(conversationId);
    } else {
      setConversations((current) =>
        current.map((conversation) =>
          conversation.id === conversationId
            ? { ...conversation, updatedLabel: relativeLabel() }
            : conversation,
        ),
      );
    }
    return conversationId;
  }

  function updateMessage(conversationId, messageId, patch) {
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: (current[conversationId] || []).map((message) =>
        message.id === messageId ? { ...message, ...patch } : message,
      ),
    }));
  }

  function applyMeta(conversationId, messageId, agentId, meta) {
    updateMessage(conversationId, messageId, {
      traceId: meta.trace_id,
      trace: meta.trace || [],
      citations: meta.citations || [],
      confidence: meta.confidence,
      handoffChain: meta.handoff_chain || [],
      followUps: meta.follow_ups || [],
      latencyMs: meta.latency_ms,
      classified: meta.classified,
      routedAgentId: meta.handoff_chain?.at(-1)?.agent_id || agentId,
      stageLabel: meta.trace?.at(-1)?.label,
    });
  }

  async function sendMessage(text, targetAgentId = selectedAgentId) {
    const conversationId = ensureConversation(text, targetAgentId);
    const userMessage = { id: makeId("u"), role: "user", text };
    const assistantId = makeId("a");
    const assistantMessage = {
      id: assistantId,
      role: "assistant",
      text: "",
      trace: [],
      citations: [],
      followUps: [],
      streaming: true,
      routedAgentId: targetAgentId,
    };

    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [...(current[conversationId] || []), userMessage, assistantMessage],
    }));

    const baseUrl =
      targetAgentId === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const payload = {
      model: targetAgentId,
      messages: [{ role: "user", content: text }],
    };

    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        apiKey,
        payload,
        onText: (accumulatedText) => {
          updateMessage(conversationId, assistantId, { text: accumulatedText });
        },
        onTrace: (traceStep) => {
          setMessagesByConversation((current) => ({
            ...current,
            [conversationId]: (current[conversationId] || []).map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    trace: [...(message.trace || []), traceStep],
                    stageLabel: traceStep.label,
                  }
                : message,
            ),
          }));
        },
        onMeta: (meta) => applyMeta(conversationId, assistantId, targetAgentId, meta),
      });
      updateMessage(conversationId, assistantId, { streaming: false });
    } catch (error) {
      updateMessage(conversationId, assistantId, {
        streaming: false,
        text: `請求失敗：${error.message || "unknown error"}`,
      });
    }
  }

  async function sendCompare(text) {
    const nextCompareMessages = {};
    setCompareMode(true);
    setRuntimeError("");

    await Promise.all(
      compareColumns.map(async (column) => {
        const userMessage = { id: makeId("u"), role: "user", text };
        const assistantId = makeId("a");
        nextCompareMessages[column.id] = [
          userMessage,
          {
            id: assistantId,
            role: "assistant",
            text: "",
            trace: [],
            citations: [],
            followUps: [],
            streaming: true,
            routedAgentId: column.agentId,
          },
        ];
        setCompareMessages((current) => ({
          ...current,
          [column.id]: nextCompareMessages[column.id],
        }));

        try {
          await streamChatCompletion({
            url: `${config.cspBaseUrl}/v1/chat/completions`,
            apiKey,
            payload: {
              model: column.agentId,
              messages: [{ role: "user", content: text }],
            },
            onText: (accumulatedText) => {
              setCompareMessages((current) => ({
                ...current,
                [column.id]: (current[column.id] || []).map((message) =>
                  message.id === assistantId ? { ...message, text: accumulatedText } : message,
                ),
              }));
            },
            onTrace: (traceStep) => {
              setCompareMessages((current) => ({
                ...current,
                [column.id]: (current[column.id] || []).map((message) =>
                  message.id === assistantId
                    ? {
                        ...message,
                        trace: [...(message.trace || []), traceStep],
                        stageLabel: traceStep.label,
                      }
                    : message,
                ),
              }));
            },
            onMeta: (meta) => {
              setCompareMessages((current) => ({
                ...current,
                [column.id]: (current[column.id] || []).map((message) =>
                  message.id === assistantId
                    ? {
                        ...message,
                        text: message.text,
                        traceId: meta.trace_id,
                        trace: meta.trace || [],
                        citations: meta.citations || [],
                        confidence: meta.confidence,
                        handoffChain: meta.handoff_chain || [],
                        followUps: meta.follow_ups || [],
                        latencyMs: meta.latency_ms,
                        routedAgentId: meta.handoff_chain?.at(-1)?.agent_id || column.agentId,
                      }
                    : message,
                ),
              }));
            },
          });
          setCompareMessages((current) => ({
            ...current,
            [column.id]: (current[column.id] || []).map((message) =>
              message.id === assistantId ? { ...message, streaming: false } : message,
            ),
          }));
        } catch (error) {
          setCompareMessages((current) => ({
            ...current,
            [column.id]: (current[column.id] || []).map((message) =>
              message.id === assistantId
                ? { ...message, streaming: false, text: `請求失敗：${error.message}` }
                : message,
            ),
          }));
        }
      }),
    );
  }

  function openCitations(citation, message = null) {
    const targetMessage =
      message ||
      [...currentMessages].reverse().find((entry) => entry.role === "assistant" && entry.citations?.length);
    if (!targetMessage) {
      return;
    }
    setActiveCitations(targetMessage.citations || []);
    setActiveCitationId(citation?.id || null);
    setCitationsOpen(true);
  }

  async function saveApiKey() {
    setSettingsBusy(true);
    setSettingsError("");
    try {
      await updateApiKey(apiKeyDraft);
      await refreshAgents();
      setSettingsOpen(false);
    } catch (error) {
      setSettingsError(error.message || "API Key 驗證失敗");
    } finally {
      setSettingsBusy(false);
    }
  }

  if (!apiKeyStatus.valid) {
    return (
      <ApiKeyGate
        error={apiKeyStatus.error}
        apiKeyDraft={apiKeyDraft}
        setApiKeyDraft={setApiKeyDraft}
        onSubmit={saveApiKey}
        loading={settingsBusy}
      />
    );
  }

  return (
    <div className="runtime-shell">
      <Sidebar
        conversations={conversations}
        selectedId={selectedConversationId}
        onSelect={setSelectedConversationId}
        onNewChat={() => {
          setSelectedConversationId(null);
          setCitationsOpen(false);
          setCompareMode(false);
        }}
        user={user}
        onLogout={logoutAndRedirect}
        onOpenSettings={() => setSettingsOpen(true)}
        onEnterCompare={() => setCompareMode(true)}
      />

      <main className="runtime-main">
        <header className="runtime-header">
          <div>
            <div className="runtime-title">
              {compareMode ? "比較模式" : selectedConversation?.title || "新對話"}
            </div>
            <div className="runtime-subtitle">
              {loadingAgents ? "同步 agent 清單中…" : runtimeError || "JWT + API Key 雙軌模式"}
            </div>
          </div>
          {!compareMode ? (
            <div className="header-controls">
              <select
                className="agent-select"
                value={selectedAgentId}
                onChange={(event) => setSelectedAgentId(event.target.value)}
              >
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
              <Button onClick={() => setCompareMode(true)} disabled={agents.length <= 2}>
                <IconColumns size={14} /> Compare
              </Button>
            </div>
          ) : null}
        </header>

        {compareMode ? (
          <ComparePanel
            columns={compareColumns}
            agents={agents.filter((agent) => agent.id !== ROUTER_AGENT.id)}
            onChangeAgent={(columnId, agentId) =>
              setCompareColumns((current) =>
                current.map((column) =>
                  column.id === columnId ? { ...column, agentId } : column,
                ),
              )
            }
            onExit={() => setCompareMode(false)}
          >
            {(column) => (
              <div className="compare-column-body">
                <div className="compare-messages">
                  {(compareMessages[column.id] || []).map((message) => (
                    <MessageBubble
                      key={message.id}
                      message={message}
                      agents={agents}
                      onOpenCitation={(citation) => openCitations(citation, message)}
                      onPickFollowUp={() => {}}
                    />
                  ))}
                </div>
                <Composer
                  onSubmit={sendCompare}
                  placeholder={`把同一個問題送到 ${column.agentId}`}
                  footer="每欄獨立串流與 metadata"
                />
              </div>
            )}
          </ComparePanel>
        ) : (
          <>
            <section className="message-pane">
              {currentMessages.length ? (
                currentMessages.map((message) => (
                  <MessageBubble
                    key={message.id}
                    message={message}
                    agents={agents}
                    onOpenCitation={(citation) => openCitations(citation, message)}
                    onPickFollowUp={(followUp) => sendMessage(followUp, selectedAgentId)}
                  />
                ))
              ) : (
                <EmptyState
                  title="準備開始一段新對話"
                  body="可以讓 Router 自動分派，或直接指定某個 agent。"
                />
              )}
            </section>

            <section className="composer-pane">
              <Composer
                onSubmit={(text) => sendMessage(text, selectedAgentId)}
                placeholder="問我任何問題，或讓 Router 幫你找最適合的 agent"
                footer={
                  selectedAgentId === ROUTER_AGENT.id
                    ? "Auto mode · 經 ANILA Router"
                    : `Direct mode · ${selectedAgentId}`
                }
              />
            </section>
          </>
        )}
      </main>

      <CitationsDrawer
        open={citationsOpen}
        citations={activeCitations}
        activeId={activeCitationId}
        onClose={() => setCitationsOpen(false)}
      />

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        apiKeyDraft={apiKeyDraft}
        setApiKeyDraft={setApiKeyDraft}
        onSaveApiKey={saveApiKey}
        apiKeyError={settingsError}
        loading={settingsBusy}
      />
    </div>
  );
}
