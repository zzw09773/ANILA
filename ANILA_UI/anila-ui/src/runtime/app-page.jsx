import React, { startTransition, useDeferredValue, useEffect, useMemo, useState } from "react";

import { apiKeyRequest, config } from "./api.js";
import { useAuth, useLogoutRedirect } from "./auth.jsx";
import {
  AgentPill,
  ApiKeyGate,
  Button,
  ClassifiedBanner,
  CitationsDrawer,
  ComparePanel,
  Composer,
  EmptyState,
  HandoffDialog,
  MessageBubble,
  Modal,
  ShareDialog,
  SettingsDialog,
  Sidebar,
} from "./components.jsx";
import { IconColumns, IconLock, IconNodes, IconSettings } from "./icons.jsx";
import { streamChatCompletion } from "./sse.js";

const RUNTIME_STATE_VERSION = 1;

const ROUTER_AGENT = {
  id: "anila-router",
  name: "ANILA Router",
  short: "auto",
  description: "自動路由：根據你的問題，由 Router 決定直接回答或分派給合適的 agent。",
};

const RUNTIME_WAVES = [
  { label: "WAVE 1", title: "信任與透明", detail: "引用來源、trace、confidence" },
  { label: "WAVE 2", title: "Multi-agent UX", detail: "direct target、平行比較" },
  { label: "WAVE 3", title: "CSP-aware runtime", detail: "JWT + API key + audit" },
];

const STARTER_PROMPTS = [
  {
    title: "特休怎麼計算？",
    detail: "HR policy · 引用來源 + 規則摘要",
    question: "公司特休怎麼計算？請附引用來源。",
    targetId: "anila-router",
  },
  {
    title: "出差報銷流程",
    detail: "Finance ops · 梳理步驟與提交資料",
    question: "請整理出差報銷流程與需要準備的附件。",
    targetId: "anila-router",
  },
  {
    title: "FastAPI SSE proxy",
    detail: "Code assist · 技術實作",
    question: "FastAPI 要怎麼做 SSE streaming proxy？請給我實作方向。",
    targetId: "anila-router",
  },
  {
    title: "比較兩個 agent",
    detail: "Multi-agent · 平行輸出比較",
    question: "請分別從政策與工程實作角度解釋企業內部 AI Runtime 的核心需求。",
    compare: true,
  },
];

function makeId(prefix) {
  return `${prefix}-${Math.random().toString(16).slice(2, 10)}`;
}

function makeConversationTitle(text) {
  return text.length > 28 ? `${text.slice(0, 28)}…` : text;
}

function relativeLabel() {
  return "剛剛";
}

function nowIso() {
  return new Date().toISOString();
}

function runtimeStorageKey(username) {
  return `anila-runtime-state:${username || "anonymous"}`;
}

function sanitizeMessagesForStorage(messagesByConversation) {
  return Object.fromEntries(
    Object.entries(messagesByConversation).map(([conversationId, messages]) => [
      conversationId,
      messages.map((message) => ({
        ...message,
        attachments: (message.attachments || []).map(({ previewUrl, ...attachment }) => attachment),
      })),
    ]),
  );
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

function maskApiKey(apiKey) {
  if (!apiKey) {
    return "未設定 API Key";
  }
  if (apiKey.length <= 12) {
    return apiKey;
  }
  return `${apiKey.slice(0, 6)}…${apiKey.slice(-4)}`;
}

function formatConfidence(confidence) {
  if (!confidence?.level) {
    return "尚未產生";
  }
  return confidence.score
    ? `${confidence.level} · ${confidence.score.toFixed(2)}`
    : confidence.level;
}

function normalizeTag(tag) {
  return tag.trim().toLowerCase();
}

export function RuntimePage() {
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
  const [shareOpen, setShareOpen] = useState(false);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [apiKeyDraft, setApiKeyDraft] = useState(apiKey || "");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [runtimeError, setRuntimeError] = useState("");
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [folder, setFolder] = useState("all");
  const [notifications, setNotifications] = useState([]);
  const [hideAuditIds, setHideAuditIds] = useState(false);
  const [compareMergeOpen, setCompareMergeOpen] = useState(false);
  const [storageReady, setStorageReady] = useState(false);
  const clientStorageKey = useMemo(() => runtimeStorageKey(user?.username), [user?.username]);

  const deferredSearchQuery = useDeferredValue(searchQuery.trim().toLowerCase());
  const directAgents = useMemo(
    () => agents.filter((agent) => agent.id !== ROUTER_AGENT.id),
    [agents],
  );
  const currentMessages = selectedConversationId
    ? messagesByConversation[selectedConversationId] || []
    : [];

  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId) || null,
    [conversations, selectedConversationId],
  );

  const activeAgent = useMemo(
    () => agents.find((agent) => agent.id === selectedAgentId) || ROUTER_AGENT,
    [agents, selectedAgentId],
  );
  const latestAssistantMessage = useMemo(
    () =>
      [...currentMessages]
        .reverse()
        .find((message) => message.role === "assistant" && (message.text || message.streaming)) || null,
    [currentMessages],
  );
  const workspaceSignals = useMemo(() => {
    if (!latestAssistantMessage) {
      return [
        {
          label: "Mode",
          value: selectedAgentId === ROUTER_AGENT.id ? "Auto route" : "Direct target",
        },
        { label: "Agents", value: `${directAgents.length} available` },
        { label: "Audit", value: "CSP trace ready" },
      ];
    }

    return [
      {
        label: "Trace",
        value: `${latestAssistantMessage.trace?.length || 0} steps`,
      },
      {
        label: "Sources",
        value: `${latestAssistantMessage.citations?.length || 0} linked`,
      },
      {
        label: "Confidence",
        value: formatConfidence(latestAssistantMessage.confidence),
      },
    ];
  }, [directAgents.length, latestAssistantMessage, selectedAgentId]);
  const compareSummaries = useMemo(
    () =>
      compareColumns.map((column) => {
        const latestAssistant =
          [...(compareMessages[column.id] || [])]
            .reverse()
            .find((message) => message.role === "assistant") || null;

        return {
          id: column.id,
          status: latestAssistant
            ? latestAssistant.streaming
              ? "streaming"
              : "complete"
            : "waiting",
          traceSteps: latestAssistant?.trace?.length || 0,
          citations: latestAssistant?.citations?.length || 0,
          confidence: latestAssistant?.confidence?.level || "n/a",
        };
      }),
    [compareColumns, compareMessages],
  );

  const filteredConversations = useMemo(() => {
    return conversations.filter((conversation) => {
      if (folder === "starred" && !conversation.starred) {
        return false;
      }
      if (folder === "compared" && conversation.folder !== "compared") {
        return false;
      }
      if (!deferredSearchQuery) {
        return true;
      }

      const tagMatch = deferredSearchQuery.match(/^tag:(\S+)(?:\s+(.*))?$/);
      if (tagMatch) {
        const tag = normalizeTag(tagMatch[1]);
        const rest = (tagMatch[2] || "").trim();
        const hasTag = (conversation.tags || []).some((item) => normalizeTag(item) === tag);
        if (!hasTag) {
          return false;
        }
        if (!rest) {
          return true;
        }
        return conversation.title.toLowerCase().includes(rest);
      }

      const haystack = [
        conversation.title,
        conversation.agentName,
        conversation.updatedLabel,
        ...(conversation.tags || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(deferredSearchQuery);
    });
  }, [conversations, deferredSearchQuery, folder]);

  useEffect(() => {
    setApiKeyDraft(apiKey || "");
  }, [apiKey]);

  useEffect(() => {
    if (!clientStorageKey) {
      return;
    }
    try {
      const raw = localStorage.getItem(clientStorageKey);
      if (!raw) {
        setConversations([]);
        setMessagesByConversation({});
        setSelectedConversationId(null);
        setFolder("all");
        setNotifications([]);
        setHideAuditIds(false);
        setStorageReady(true);
        return;
      }
      const parsed = JSON.parse(raw);
      if (parsed.version !== RUNTIME_STATE_VERSION) {
        setStorageReady(true);
        return;
      }
      setConversations(parsed.conversations || []);
      setMessagesByConversation(parsed.messagesByConversation || {});
      setSelectedConversationId(parsed.selectedConversationId || null);
      setFolder(parsed.folder || "all");
      setNotifications(parsed.notifications || []);
      setHideAuditIds(Boolean(parsed.hideAuditIds));
    } catch {
      setRuntimeError("本地對話狀態讀取失敗，已回退為空白狀態。");
    } finally {
      setStorageReady(true);
    }
  }, [clientStorageKey]);

  useEffect(() => {
    if (!storageReady || !clientStorageKey) {
      return;
    }
    localStorage.setItem(
      clientStorageKey,
      JSON.stringify({
        version: RUNTIME_STATE_VERSION,
        conversations,
        messagesByConversation: sanitizeMessagesForStorage(messagesByConversation),
        selectedConversationId,
        folder,
        notifications,
        hideAuditIds,
      }),
    );
  }, [
    clientStorageKey,
    conversations,
    folder,
    hideAuditIds,
    messagesByConversation,
    notifications,
    selectedConversationId,
    storageReady,
  ]);

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
      startTransition(() => {
        setAgents(normalized);
        setCompareColumns((current) => (current.length ? current : chooseCompareColumns(normalized)));
        if (!normalized.some((agent) => agent.id === selectedAgentId)) {
          setSelectedAgentId(ROUTER_AGENT.id);
        }
      });
    } catch (error) {
      startTransition(() => {
        setRuntimeError(error.message || "無法載入 agent 清單");
        setAgents([ROUTER_AGENT]);
        setSelectedAgentId(ROUTER_AGENT.id);
      });
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
          agentId,
          starred: false,
          tags: [],
          folder: "all",
          classified: false,
          shareDraft: null,
          handoffState: null,
          updatedAt: nowIso(),
        },
        ...current,
      ]);
      setSelectedConversationId(conversationId);
    } else {
      setConversations((current) =>
        current.map((conversation) =>
          conversation.id === conversationId
            ? { ...conversation, updatedLabel: relativeLabel(), agentId, updatedAt: nowIso() }
            : conversation,
        ),
      );
    }
    return conversationId;
  }

  function updateConversationAgent(conversationId, agentId) {
    const agentName = agents.find((agent) => agent.id === agentId)?.name || agentId;
    setConversations((current) =>
      current.map((conversation) =>
        conversation.id === conversationId
          ? { ...conversation, updatedLabel: relativeLabel(), agentId, agentName, updatedAt: nowIso() }
          : conversation,
      ),
    );
  }

  function updateConversation(conversationId, updater) {
    setConversations((current) =>
      current.map((conversation) =>
        conversation.id === conversationId ? updater(conversation) : conversation,
      ),
    );
  }

  function toggleConversationStar(conversationId) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      starred: !conversation.starred,
    }));
  }

  function addConversationTag(conversationId, tag) {
    updateConversation(conversationId, (conversation) => {
      const nextTag = normalizeTag(tag);
      if (!nextTag || (conversation.tags || []).some((item) => normalizeTag(item) === nextTag)) {
        return conversation;
      }
      return {
        ...conversation,
        tags: [...(conversation.tags || []), nextTag],
      };
    });
  }

  function removeConversationTag(conversationId, tag) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      tags: (conversation.tags || []).filter((item) => normalizeTag(item) !== normalizeTag(tag)),
    }));
  }

  function classifyConversation(conversationId) {
    const target = conversations.find((conversation) => conversation.id === conversationId);
    if (!target) {
      return;
    }
    if (target.classified) {
      setRuntimeError("機密對話在前端視角中不可直接解除，需由 CSP 控制面處理。");
      return;
    }
    if (!window.confirm("此對話將進入機密模式：禁止分享、複製受限，並以 local lock 形式保存。確定嗎？")) {
      return;
    }
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      classified: true,
      tags: Array.from(new Set([...(conversation.tags || []), "classified"])),
      updatedAt: nowIso(),
    }));
  }

  function performHandoff(payload) {
    if (!selectedConversationId) {
      return;
    }
    const { type, targetId, targetLabel, note = "" } = payload;
    const handoffLabel = note ? `handoff note: ${note}` : "manual handoff";
    const systemMessage = {
      id: makeId("sys"),
      role: "assistant",
      text:
        type === "agent"
          ? `[Router] 已從 ${selectedAgentId} 交接給 ${targetLabel}。${note ? ` ${note}` : ""}`
          : `[Router] 已建立交接草稿給 ${targetLabel}。${note ? ` ${note}` : ""}`,
      trace: [],
      citations: [],
      followUps: [],
      streaming: false,
      routedAgentId: type === "agent" ? targetId : selectedAgentId,
      handoffChain: [
        { agent_id: selectedAgentId, label: "current owner" },
        { agent_id: targetId, label: handoffLabel },
      ],
      confidence: { level: "high", score: 1 },
      traceId: makeId("trace"),
      createdAt: nowIso(),
      conversationId: selectedConversationId,
    };

    setMessagesByConversation((current) => ({
      ...current,
      [selectedConversationId]: [...(current[selectedConversationId] || []), systemMessage],
    }));
    updateConversation(selectedConversationId, (conversation) => ({
      ...conversation,
      updatedLabel: relativeLabel(),
      updatedAt: nowIso(),
      handoffState: {
        type,
        target: targetLabel,
        status: type === "agent" ? "local draft · routed" : "local draft · pending recipient",
        note,
        requestedAt: nowIso(),
      },
      tags: Array.from(new Set([...(conversation.tags || []), "handoff"])),
    }));
    if (type === "agent") {
      updateConversationAgent(selectedConversationId, targetId);
      setSelectedAgentId(targetId);
    } else {
      setNotifications((current) => [
        {
          id: makeId("notif"),
          title: `交接草稿 → ${targetLabel}`,
          note: note || "等待對方接受，尚未同步到後端。",
          conversationId: selectedConversationId,
          status: "pending",
          requestedAt: nowIso(),
        },
        ...current,
      ]);
    }
    addConversationTag(selectedConversationId, "handoff");
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
      conversationId,
    });
  }

  async function sendMessage(text, options = {}) {
    const {
      targetAgentId = selectedAgentId,
      explicitAgents = [],
      piiHits = [],
      attachments = [],
    } = options;

    if (explicitAgents.length > 1) {
      await sendCompare(text, { agentIds: explicitAgents, explicitAgents, piiHits, attachments });
      return;
    }

    const effectiveTarget = explicitAgents[0] || targetAgentId;
    const conversationId = ensureConversation(text, effectiveTarget);
    updateConversationAgent(conversationId, effectiveTarget);
    const userMessage = {
      id: makeId("u"),
      role: "user",
      text,
      explicitAgents,
      piiHits,
      attachments,
      conversationId,
      createdAt: nowIso(),
    };
    const assistantId = makeId("a");
    const assistantMessage = {
      id: assistantId,
      role: "assistant",
      text: "",
      trace: [],
      citations: [],
      followUps: [],
      streaming: true,
      routedAgentId: effectiveTarget,
      conversationId,
      createdAt: nowIso(),
    };

    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [...(current[conversationId] || []), userMessage, assistantMessage],
    }));

    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const payload = {
      model: effectiveTarget,
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
        onMeta: (meta) => applyMeta(conversationId, assistantId, effectiveTarget, meta),
      });
      updateMessage(conversationId, assistantId, { streaming: false });
    } catch (error) {
      updateMessage(conversationId, assistantId, {
        streaming: false,
        text: `請求失敗：${error.message || "unknown error"}`,
      });
    }
  }

  async function sendCompare(text, options = {}) {
    const selectedCompareAgents = (
      options.agentIds?.length
        ? options.agentIds
        : options.explicitAgents?.length
          ? options.explicitAgents
          : compareColumns.map((column) => column.agentId)
    ).filter((agentId) => agentId !== ROUTER_AGENT.id);
    const effectiveColumns = selectedCompareAgents.length
      ? selectedCompareAgents.slice(0, 3).map((agentId, index) => ({
          id: `col-${index + 1}`,
          agentId,
        }))
      : compareColumns;

    if (effectiveColumns.length) {
      setCompareColumns(effectiveColumns);
    }
    setCompareMode(true);
    setCitationsOpen(false);
    setRuntimeError("");

    await Promise.all(
      effectiveColumns.map(async (column) => {
        const userMessage = {
          id: makeId("u"),
          role: "user",
          text,
          explicitAgents: options.explicitAgents || [],
          piiHits: options.piiHits || [],
          attachments: options.attachments || [],
          conversationId: column.id,
          createdAt: nowIso(),
        };
        const assistantId = makeId("a");
        setCompareMessages((current) => ({
          ...current,
          [column.id]: [
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
              conversationId: column.id,
              createdAt: nowIso(),
            },
          ],
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

  function adoptCompareColumn(columnId) {
    const column = compareColumns.find((entry) => entry.id === columnId);
    const columnMessages = compareMessages[columnId] || [];
    if (!column || !columnMessages.length) {
      return;
    }

    const firstUserMessage = columnMessages.find((message) => message.role === "user");
    const conversationId = makeId("cv");
    const adoptedAgentName =
      agents.find((agent) => agent.id === column.agentId)?.name || column.agentId;

    setConversations((current) => [
      {
        id: conversationId,
        title: makeConversationTitle(firstUserMessage?.text || "採用比較結果"),
        updatedLabel: relativeLabel(),
        agentName: adoptedAgentName,
        agentId: column.agentId,
        starred: false,
        tags: ["compared"],
        folder: "compared",
        classified: false,
        shareDraft: null,
        handoffState: null,
        updatedAt: nowIso(),
      },
      ...current,
    ]);
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: columnMessages,
    }));
    setSelectedConversationId(conversationId);
    setSelectedAgentId(column.agentId);
    setCompareMode(false);
  }

  function handleStarterSelection(suggestion) {
    if (suggestion.compare) {
      setCompareMode(true);
      void sendCompare(suggestion.question);
      return;
    }
    const nextTarget =
      suggestion.targetId && agents.some((agent) => agent.id === suggestion.targetId)
        ? suggestion.targetId
        : ROUTER_AGENT.id;
    setSelectedAgentId(nextTarget);
    void sendMessage(suggestion.question, { targetAgentId: nextTarget });
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

  function saveShareDraft(draft) {
    if (!selectedConversationId) {
      return;
    }
    updateConversation(selectedConversationId, (conversation) => ({
      ...conversation,
      shareDraft: draft,
      updatedAt: nowIso(),
    }));
    addConversationTag(selectedConversationId, "shared");
    setShareOpen(false);
  }

  function mergeCompareColumns() {
    const completedColumns = compareColumns.filter((column) =>
      (compareMessages[column.id] || []).some(
        (message) => message.role === "assistant" && !message.streaming && message.text,
      ),
    );
    if (!completedColumns.length) {
      setCompareMergeOpen(false);
      setCompareMode(false);
      return;
    }
    const conversationId = makeId("cv");
    const mergedAssistantText = completedColumns
      .map((column) => {
        const agentName = agents.find((agent) => agent.id === column.agentId)?.name || column.agentId;
        const assistantMessage =
          [...(compareMessages[column.id] || [])]
            .reverse()
            .find((message) => message.role === "assistant" && !message.streaming) || null;
        return assistantMessage ? `### ${agentName}\n${assistantMessage.text}` : "";
      })
      .filter(Boolean)
      .join("\n\n");
    const firstUserMessage =
      (compareMessages[completedColumns[0].id] || []).find((message) => message.role === "user") || null;
    setConversations((current) => [
      {
        id: conversationId,
        title: makeConversationTitle(firstUserMessage?.text || "比較合併結果"),
        updatedLabel: relativeLabel(),
        agentName: "Compare Merge",
        agentId: "compare-merge",
        starred: false,
        tags: ["compared", "merged"],
        folder: "compared",
        classified: false,
        shareDraft: null,
        handoffState: null,
        updatedAt: nowIso(),
      },
      ...current,
    ]);
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [
        ...(firstUserMessage ? [{ ...firstUserMessage, conversationId, createdAt: nowIso() }] : []),
        {
          id: makeId("a"),
          role: "assistant",
          text: mergedAssistantText,
          trace: [],
          citations: [],
          followUps: [],
          streaming: false,
          routedAgentId: "compare-merge",
          conversationId,
          createdAt: nowIso(),
          confidence: { level: "medium", score: 0.72 },
        },
      ],
    }));
    setSelectedConversationId(conversationId);
    setSelectedAgentId(ROUTER_AGENT.id);
    setCompareMode(false);
    setCompareMergeOpen(false);
  }

  function handleCompareExit() {
    const hasCompareMessages = Object.values(compareMessages).some((messages) => messages?.length);
    if (!hasCompareMessages) {
      setCompareMode(false);
      return;
    }
    setCompareMergeOpen(true);
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
        conversations={filteredConversations}
        selectedId={selectedConversationId}
        onSelect={setSelectedConversationId}
        onNewChat={() => {
          setSelectedConversationId(null);
          setCitationsOpen(false);
          setCompareMode(false);
          setShareOpen(false);
          setHandoffOpen(false);
        }}
        user={user}
        onLogout={logoutAndRedirect}
        onOpenSettings={() => setSettingsOpen(true)}
        onEnterCompare={() => setCompareMode(true)}
        activeAgent={activeAgent}
        searchValue={searchQuery}
        onSearchChange={setSearchQuery}
        agentsCount={Math.max(agents.length - 1, 0)}
        selectedConversation={selectedConversation}
        folder={folder}
        onChangeFolder={setFolder}
        onToggleStar={toggleConversationStar}
        onAddTag={addConversationTag}
        onRemoveTag={removeConversationTag}
        notifications={notifications}
        onSelectNotification={(notification) => {
          setSelectedConversationId(notification.conversationId);
          setCompareMode(false);
        }}
      />

      <main className="runtime-main">
        <header className="workspace-header">
          <div className="workspace-heading">
            <div className="workspace-kicker">PAGE 03 · RUNTIME</div>
            <h1 className="workspace-title">
              {compareMode ? "平行比較" : selectedConversation?.title || "你今天想問 ANILA 什麼？"}
            </h1>
            <p className="workspace-description">
              {selectedConversation
                ? "每段回答都保留路由痕跡、引用來源與 handoff context，讓 runtime 不只是聊天視窗。"
                : "以 template 為基線，把 trust、multi-agent 與 auditable runtime 直接落在真實資料流上。"}
            </p>
            <div className="workspace-signal-row">
              {workspaceSignals.map((signal) => (
                <div key={signal.label} className="workspace-signal-card">
                  <div className="workspace-wave-kicker">{signal.label}</div>
                  <div className="workspace-signal-value">{signal.value}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="workspace-controls">
            {selectedConversation && !compareMode ? (
              <>
                <Button
                  onClick={() => classifyConversation(selectedConversation.id)}
                  variant={selectedConversation.classified ? "primary" : "default"}
                >
                  <IconLock size={14} /> {selectedConversation.classified ? "機密已鎖定" : "設為機密"}
                </Button>
                <Button onClick={() => setHandoffOpen(true)}>
                  <IconNodes size={14} /> Handoff
                </Button>
                <Button
                  onClick={() => setShareOpen(true)}
                  disabled={selectedConversation.classified}
                >
                  Share
                </Button>
              </>
            ) : null}
            <div className="workspace-key-badge">
              <span className="workspace-key-dot" />
              <span>{maskApiKey(apiKey)}</span>
            </div>
            <Button onClick={() => setCompareMode(true)} disabled={directAgents.length < 2}>
              <IconColumns size={14} /> Compare
            </Button>
            <Button onClick={() => setSettingsOpen(true)}>
              <IconSettings size={14} /> 設定
            </Button>
          </div>
        </header>

        {!compareMode ? (
          <>
            <ClassifiedBanner conversation={selectedConversation} />
            <section className="workspace-ribbon">
              <div className="workspace-ribbon-left">
                <AgentPill agent={activeAgent} emphasis="accent" />
                <span>{loadingAgents ? "同步 agent 清單中…" : runtimeError || activeAgent.description}</span>
              </div>
              {selectedConversation && latestAssistantMessage ? (
                <div className="workspace-ribbon-right">
                  <span>Current target · {activeAgent.name}</span>
                  <span>Trace ID · {latestAssistantMessage.traceId || "pending"}</span>
                  {selectedConversation?.shareDraft?.status ? (
                    <span>Share · {selectedConversation.shareDraft.status}</span>
                  ) : null}
                  {selectedConversation?.handoffState?.status ? (
                    <span>Handoff · {selectedConversation.handoffState.status}</span>
                  ) : null}
                  {latestAssistantMessage.citations?.length ? (
                    <button
                      className="workspace-inline-link"
                      onClick={() => openCitations(latestAssistantMessage.citations[0], latestAssistantMessage)}
                    >
                      查看 {latestAssistantMessage.citations.length} 筆來源
                    </button>
                  ) : null}
                </div>
              ) : null}
              <div className="workspace-wave-row">
                {RUNTIME_WAVES.map((wave) => (
                  <div key={wave.label} className="workspace-wave-card">
                    <div className="workspace-wave-kicker">{wave.label}</div>
                    <div className="workspace-wave-title">{wave.title}</div>
                    <div className="workspace-wave-detail">{wave.detail}</div>
                  </div>
                ))}
              </div>
            </section>

            <section className="message-pane">
              {currentMessages.length ? (
                currentMessages.map((message) => (
                  <MessageBubble
                    key={message.id}
                    message={message}
                    agents={agents}
                    classified={selectedConversation?.classified}
                    conversationId={selectedConversationId}
                    hideAuditIds={hideAuditIds}
                    onOpenCitation={(citation) => openCitations(citation, message)}
                    onPickFollowUp={(followUp) =>
                      sendMessage(followUp, { targetAgentId: selectedAgentId })
                    }
                  />
                ))
              ) : (
                <EmptyState
                  title="準備開始一段可稽核的對話"
                  body="可以交給 Router 自動分派，也可以直接指定某個 agent。回答會保留 trace、confidence 與 citation UI。"
                  suggestions={STARTER_PROMPTS}
                  onSelectSuggestion={handleStarterSelection}
                  actions={
                    <Button variant="primary" onClick={() => setSelectedAgentId(ROUTER_AGENT.id)}>
                      以 Router 模式開始
                    </Button>
                  }
                />
              )}
            </section>

            <section className="composer-pane">
              <Composer
                onSubmit={(text, meta) => sendMessage(text, { ...meta, targetAgentId: selectedAgentId })}
                placeholder="問 ANILA 任何事情，或用 target 指定 agent"
                footer={
                  selectedAgentId === ROUTER_AGENT.id
                    ? "Auto route · 由 ANILA Router 判斷是否分派 agent"
                    : `Direct target · ${activeAgent.name}`
                }
                agents={agents}
                selectedAgentId={selectedAgentId}
                onChangeAgent={setSelectedAgentId}
                classified={selectedConversation?.classified}
              />
            </section>
          </>
        ) : (
          <ComparePanel
            columns={compareColumns}
            agents={agents.filter((agent) => agent.id !== ROUTER_AGENT.id)}
            columnSummaries={compareSummaries}
            onChangeAgent={(columnId, agentId) =>
              setCompareColumns((current) =>
                current.map((column) =>
                  column.id === columnId ? { ...column, agentId } : column,
                ),
              )
            }
            onMerge={mergeCompareColumns}
            onExit={handleCompareExit}
          >
            {(column) => (
              <div className="compare-column-body">
                <div className="compare-messages">
                  {(compareMessages[column.id] || []).length ? (
                    (compareMessages[column.id] || []).map((message) => (
                      <div
                        key={message.id}
                        className={`compare-message-row compare-message-row-${message.role}`.trim()}
                      >
                        <MessageBubble
                          message={message}
                          agents={agents}
                          classified={false}
                          conversationId={column.id}
                          hideAuditIds={hideAuditIds}
                          onOpenCitation={(citation) => openCitations(citation, message)}
                          onPickFollowUp={() => {}}
                        />
                      </div>
                    ))
                  ) : (
                    <EmptyState
                      title="等待送入同一個問題"
                      body="這一欄會獨立串流回答，方便比較不同 agent 的輸出與 trace。"
                    />
                  )}
                </div>
                <div className="compare-column-toolbar">
                  <div className="compare-column-summary">
                    <span>
                      回答狀態 ·{" "}
                      {(
                        [...(compareMessages[column.id] || [])]
                          .reverse()
                          .find((message) => message.role === "assistant")?.streaming
                      )
                        ? "串流中"
                        : "完成"}
                    </span>
                    <span>
                      來源數 ·{" "}
                      {(
                        [...(compareMessages[column.id] || [])]
                          .reverse()
                          .find((message) => message.role === "assistant")?.citations?.length || 0
                      )}
                    </span>
                  </div>
                  <Button
                    variant="primary"
                    onClick={() => adoptCompareColumn(column.id)}
                    disabled={
                      ![...(compareMessages[column.id] || [])]
                        .reverse()
                        .find((message) => message.role === "assistant" && !message.streaming)
                    }
                  >
                    採用此回答
                  </Button>
                </div>
                <Composer
                  onSubmit={(text, meta) => sendCompare(text, meta)}
                  placeholder={`將同一個問題送往 ${column.agentId}`}
                  footer="每一欄都保留自己的 metadata 與 trace"
                  agents={agents.filter((agent) => agent.id !== ROUTER_AGENT.id)}
                  selectedAgentId={column.agentId}
                  onChangeAgent={(agentId) =>
                    setCompareColumns((current) =>
                      current.map((entry) =>
                        entry.id === column.id ? { ...entry, agentId } : entry,
                        ),
                    )
                  }
                  classified={false}
                />
              </div>
            )}
          </ComparePanel>
        )}
      </main>

      {selectedConversation?.classified ? (
        <div className="classified-watermark">
          {(user?.email || user?.username || "runtime-user")} · {latestAssistantMessage?.traceId || "trace-pending"} · classified
        </div>
      ) : null}

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
        hideAuditIds={hideAuditIds}
        onToggleHideAuditIds={setHideAuditIds}
      />
      <HandoffDialog
        open={handoffOpen}
        onClose={() => setHandoffOpen(false)}
        agents={agents}
        currentAgentId={selectedAgentId}
        onSubmit={performHandoff}
      />
      <ShareDialog
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        conversation={selectedConversation}
        messages={currentMessages}
        user={user}
        draft={selectedConversation?.shareDraft}
        onSaveDraft={saveShareDraft}
      />
      <Modal
        open={compareMergeOpen}
        onClose={() => setCompareMergeOpen(false)}
        title="退出比較"
        subtitle="可直接退出、採用單欄回答，或把目前各欄合併成新對話。"
      >
        <div className="settings-stack">
          <Button
            variant="primary"
            onClick={() => {
              const firstCompleted = compareColumns.find((column) =>
                (compareMessages[column.id] || []).some(
                  (message) => message.role === "assistant" && !message.streaming,
                ),
              );
              if (firstCompleted) {
                adoptCompareColumn(firstCompleted.id);
              } else {
                setCompareMode(false);
              }
              setCompareMergeOpen(false);
            }}
          >
            採用第一個已完成欄位
          </Button>
          <Button onClick={mergeCompareColumns}>合併目前比較結果</Button>
          <Button
            onClick={() => {
              setCompareMode(false);
              setCompareMergeOpen(false);
            }}
          >
            直接退出比較
          </Button>
        </div>
      </Modal>
    </div>
  );
}
