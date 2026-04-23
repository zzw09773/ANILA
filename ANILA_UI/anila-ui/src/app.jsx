// App root — ChatRuntime wired to real CSP + Router backends.
// - Classification is backend-driven:
//     * agent.requires_encryption=true → new conversations start classified
//     * SSE anila.meta.classified=true → conversation latches classified (one-way)
//     * user has NO lock/unlock toggle anywhere
// - All data flows through the real CSP /v1/agents + /v1/chat/completions.

import React, {
  startTransition,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { apiKeyRequest, config } from "./runtime/api.js";
import { useAuth, useLogoutRedirect } from "./runtime/auth.jsx";
import { streamChatCompletion } from "./runtime/sse.js";
import { latchConversationWithMeta } from "./runtime/classified.js";
import { relativeLabel } from "./runtime/time.js";
import {
  listConversations as apiListConversations,
  createConversation as apiCreateConversation,
  getConversation as apiGetConversation,
  updateConversationTitle as apiUpdateConversationTitle,
  deleteConversation as apiDeleteConversation,
  appendMessage as apiAppendMessage,
  rateMessage as apiRateMessage,
  editUserMessage as apiEditUserMessage,
  updateMessage as apiUpdateMessage,
  createShare as apiCreateShare,
  buildShareUrl,
  uploadAttachment as apiUploadAttachment,
  createHandoff as apiCreateHandoff,
} from "./runtime/conversations.js";

import {
  AgentSelector,
  Composer,
  MessageBubble,
  Sidebar,
} from "./chat.jsx";
import {
  Button,
  IconButton,
  Input,
  Modal,
  Dropdown,
} from "./components.jsx";
import {
  AnilaGlyph,
  IconColumns,
  IconEye,
  IconEyeOff,
  IconKey,
  IconLock,
  IconMoon,
  IconNodes,
  IconRefresh,
  IconSettings,
  IconShare,
  IconShield,
  IconSpark,
  IconSun,
  IconUser,
} from "./icons.jsx";
import { BUILTIN_FOLDER_IDS, DEFAULT_FOLDERS } from "./data.jsx";
import {
  CitationsDrawer,
  ConfidentialWatermark,
} from "./trust.jsx";
import { ParallelCompareView } from "./multiagent.jsx";
import { HandoffMenu, ShareDialog } from "./collab.jsx";
import { TweaksPanel } from "./tweaks.jsx";

// ---- Router pseudo-agent ----------------------------------------------------
const ROUTER_AGENT = Object.freeze({
  id: "anila-router",
  name: "ANILA Router",
  short: "auto",
  description: "自動路由：由 Router 決定直接回答或分派給合適的 agent。",
  requiresEncryption: false,
});

const STARTER_PROMPTS = [
  { title: "特休怎麼計算？",   sub: "HR · 規則 + 引用來源",      q: "公司特休怎麼計算？請附引用來源。" },
  { title: "出差報銷流程",     sub: "Finance · 4 步流程",        q: "請整理出差報銷流程與需要準備的附件。" },
  { title: "FastAPI SSE",     sub: "Code · streaming proxy",    q: "FastAPI 要怎麼做 SSE streaming proxy？請給我實作方向。" },
  { title: "@vlm 解釋架構圖",  sub: "直接指定 vision agent",     q: "@vlm 解釋這張系統架構圖" },
];

// ---- Helpers ---------------------------------------------------------------
// Build an OpenAI chat message `content` for a user turn. When the message
// has no image attachments we keep the string form (compatible with every
// model). When images are present we switch to the array form with
// `image_url` parts so vision-capable models (Gemma4, gpt-4o, ...) can see
// the image inline. Non-image attachments are referenced by filename in a
// trailing text note.
// Fold prior conversation turns (excluding the live streaming assistant)
// into the OpenAI message history so the model remembers what was said —
// and, critically, so images from earlier turns stay visible.
function buildMessageHistory(priorMsgs, currentText, currentAttachments) {
  const out = [];
  for (const m of priorMsgs || []) {
    if (!m || m.streaming) continue;
    if (m.role === "user") {
      out.push({ role: "user", content: buildUserContent(m.text || "", m.attachments || []) });
    } else if (m.role === "assistant" && m.text) {
      out.push({ role: "assistant", content: m.text });
    }
  }
  out.push({ role: "user", content: buildUserContent(currentText, currentAttachments) });
  return out;
}

function buildUserContent(text, attachments) {
  const list = Array.isArray(attachments) ? attachments : [];
  const images = list.filter((a) => a.dataUrl && (a.kind === "image" || (a.contentType || "").startsWith("image/")));
  const otherFiles = list.filter((a) => !images.includes(a) && a.name);
  if (images.length === 0) {
    if (otherFiles.length === 0) return text;
    const tail = otherFiles.map((a) => `- ${a.name}`).join("\n");
    return `${text}\n\n[附件]\n${tail}`;
  }
  const parts = [{ type: "text", text: text || "" }];
  for (const img of images) {
    parts.push({ type: "image_url", image_url: { url: img.dataUrl } });
  }
  if (otherFiles.length > 0) {
    parts[0].text = `${parts[0].text}\n\n[附件]\n${otherFiles.map((a) => `- ${a.name}`).join("\n")}`;
  }
  return parts;
}

function makeId(prefix) {
  return `${prefix}-${Math.random().toString(16).slice(2, 10)}-${Date.now().toString(36)}`;
}

function makeConversationTitle(text) {
  const t = (text || "").trim();
  if (!t) return "新對話";
  return t.length > 28 ? `${t.slice(0, 28)}…` : t;
}

function nowIso() {
  return new Date().toISOString();
}

function maskApiKey(apiKey) {
  if (!apiKey) return "未設定";
  if (apiKey.length <= 12) return apiKey;
  return `${apiKey.slice(0, 6)}…${apiKey.slice(-4)}`;
}

// normalize backend /v1/agents payload → UI agent model
// includes the ROUTER pseudo-agent in front
export function normalizeAgents(data) {
  return [
    ROUTER_AGENT,
    ...(data || []).map((item) => ({
      id: item.id,
      name: item.name || item.id,
      short: (item.short || item.id || "").slice(0, 12),
      description: item.description_for_router || item.description || "",
      endpointUrl: item.endpoint_url,
      capabilities: item.capabilities || {},
      requiresEncryption: Boolean(item.requires_encryption),
    })),
  ];
}

function applyTweaks(t) {
  const r = document.documentElement;
  r.setAttribute("data-theme", t.dark ? "dark" : "light");
  if (t.accent) r.style.setProperty("--accent", t.accent);
  if (t.density) r.style.setProperty("--density", `${t.density}px`);
  if (t.sansFamily) {
    r.style.setProperty(
      "--font-sans",
      `"${t.sansFamily}", "Inter", system-ui, sans-serif`,
    );
  }
  if (t.monoFamily) {
    r.style.setProperty(
      "--font-mono",
      `"${t.monoFamily}", ui-monospace, Menlo, monospace`,
    );
  }
}

// ---- Chat Runtime ----------------------------------------------------------
function ChatRuntime({ user, tweaks, setTweaks, tweaksOpen, setTweaksOpen }) {
  const {
    apiKey,
    apiKeyStatus,
    updateApiKey,
    authRequest,
    multipartRequest,
    isAuthenticated,
  } = useAuth();
  const logoutAndRedirect = useLogoutRedirect();

  // --- agents / conversations / messages ---
  const [agents, setAgents] = useState([ROUTER_AGENT]);
  const [selectedAgentId, setSelectedAgentId] = useState(ROUTER_AGENT.id);
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [runtimeError, setRuntimeError] = useState("");

  const [conversations, setConversations] = useState([]);
  const [messagesByConv, setMessagesByConv] = useState({});
  const [selectedConvId, setSelectedConvId] = useState(null);

  // --- compare mode ---
  const [compareMode, setCompareMode] = useState(false);
  const [compareColumns, setCompareColumns] = useState([]);
  const [compareMsgs, setCompareMsgs] = useState({});

  // --- UI state ---
  const [citationsOpen, setCitationsOpen] = useState(false);
  const [activeCitations, setActiveCitations] = useState([]);
  const [activeCitationId, setActiveCitationId] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState("general");
  const [shareOpen, setShareOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [folder, setFolder] = useState("all");

  // folders: persisted locally. Users can add/delete; built-ins (all, starred)
  // are guarded because the sidebar filter logic treats them specially.
  const [folders, setFolders] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_FOLDERS;
    try {
      const raw = window.localStorage.getItem("anila-folders");
      if (!raw) return DEFAULT_FOLDERS;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed) || parsed.length === 0) return DEFAULT_FOLDERS;
      return parsed.filter((f) => f && typeof f.id === "string" && typeof f.name === "string");
    } catch {
      return DEFAULT_FOLDERS;
    }
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem("anila-folders", JSON.stringify(folders));
    } catch {
      /* quota / private mode — fall back silently */
    }
  }, [folders]);

  const createFolder = useCallback((rawName) => {
    const name = (rawName || "").trim();
    if (!name) return;
    const baseId = `usr-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "folder"}`;
    setFolders((prev) => {
      if (prev.some((f) => f.name === name)) return prev;
      let id = baseId;
      let n = 2;
      while (prev.some((f) => f.id === id)) {
        id = `${baseId}-${n++}`;
      }
      return [...prev, { id, name, icon: "folder" }];
    });
  }, []);

  const deleteFolder = useCallback((id) => {
    if (BUILTIN_FOLDER_IDS.has(id)) return;
    setFolders((prev) => prev.filter((f) => f.id !== id));
    setConversations((prevConvs) => {
      const doomed = prevConvs.filter((c) => c.folder === id).map((c) => c.id);
      if (doomed.length > 0) {
        setMessagesByConv((prevMsgs) => {
          const next = { ...prevMsgs };
          for (const cid of doomed) delete next[cid];
          return next;
        });
        setSelectedConvId((cur) => (doomed.includes(cur) ? null : cur));
      }
      return prevConvs.filter((c) => c.folder !== id);
    });
    setFolder((current) => (current === id ? "all" : current));
  }, []);

  const scrollRef = useRef(null);

  const selectedConv = useMemo(
    () => conversations.find((c) => c.id === selectedConvId) || null,
    [conversations, selectedConvId],
  );
  const currentMsgs = selectedConvId ? messagesByConv[selectedConvId] || [] : [];
  const isClassified = Boolean(selectedConv?.classified);
  const activeAgent = useMemo(
    () => agents.find((a) => a.id === selectedAgentId) || ROUTER_AGENT,
    [agents, selectedAgentId],
  );
  const activeEncryptionRequired = Boolean(activeAgent?.requiresEncryption);
  const directAgents = useMemo(
    () => agents.filter((a) => a.id !== ROUTER_AGENT.id),
    [agents],
  );
  const latestAssistantMessage = useMemo(
    () =>
      [...currentMsgs]
        .reverse()
        .find((m) => m.role === "assistant" && (m.text || m.streaming)) || null,
    [currentMsgs],
  );

  // autoscroll
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [currentMsgs.length, currentMsgs[currentMsgs.length - 1]?.text]);

  // agents: load on apiKey valid
  useEffect(() => {
    if (apiKeyStatus.valid && apiKey) {
      void refreshAgents();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        if (!normalized.some((a) => a.id === selectedAgentId)) {
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

  function agentRequiresEncryption(agentId) {
    return Boolean(agents.find((a) => a.id === agentId)?.requiresEncryption);
  }

  // Map a backend ConversationOut row → the sidebar shape the UI already uses.
  function mapServerConversation(serverRow, agentNameLookup) {
    const agentName =
      serverRow.agent_id != null ? agentNameLookup(serverRow.agent_id) : null;
    return {
      id: serverRow.id,
      title: serverRow.title,
      ts: relativeLabel(),
      updatedLabel: relativeLabel(),
      agent: agentName || null,
      agentId: serverRow.agent_id || null,
      agentName: agentName || null,
      folder: "all",
      tags: serverRow.classified ? ["classified"] : [],
      starred: false,
      classified: Boolean(serverRow.classified),
      updatedAt: serverRow.updated_at || serverRow.created_at || nowIso(),
    };
  }

  function mapServerMessage(msg) {
    const meta = msg.metadata || {};
    return {
      id: `srv-${msg.id}`,
      dbId: msg.id,
      role: msg.role,
      text: msg.content || "",
      trace: meta.trace || [],
      citations: meta.citations || [],
      followUps: meta.follow_ups || [],
      handoffChain: meta.handoff_chain || [],
      confidence: meta.confidence,
      classified: meta.classified,
      traceId: msg.trace_id || meta.trace_id,
      latencyMs: msg.latency_ms,
      routedAgentId: meta.routed_agent_id || null,
      rating: msg.rating || null,
      reasoning: meta.reasoning || null,
      streaming: false,
      attachments: (msg.attachments || []).map((a) => ({
        id: a.reference_id,
        name: a.filename,
        contentType: a.content_type,
        size: a.size_bytes,
      })),
      conversationId: null, // patched by caller
      createdAt: msg.created_at,
    };
  }

  // Fetch the user's conversations on login and whenever JWT changes. Messages
  // are loaded lazily when a conversation is clicked (keeps the initial
  // payload small).
  useEffect(() => {
    if (!isAuthenticated) {
      setConversations([]);
      setMessagesByConv({});
      setSelectedConvId(null);
      return;
    }
    let active = true;
    (async () => {
      try {
        const rows = await apiListConversations(authRequest);
        if (!active) return;
        const lookup = (id) => agents.find((a) => a.id === id)?.name || null;
        setConversations(rows.map((r) => mapServerConversation(r, lookup)));
      } catch (error) {
        if (active) {
          setRuntimeError(error.message || "無法載入對話清單");
        }
      }
    })();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // Hydrate messages when the selected conversation changes and we haven't
  // loaded its messages yet.
  useEffect(() => {
    if (!selectedConvId || typeof selectedConvId !== "number") return;
    if (messagesByConv[selectedConvId]?.length) return;
    let active = true;
    (async () => {
      try {
        const detail = await apiGetConversation(authRequest, selectedConvId);
        if (!active) return;
        const msgs = (detail.messages || []).map((m) => ({
          ...mapServerMessage(m),
          conversationId: selectedConvId,
        }));
        setMessagesByConv((prev) => ({ ...prev, [selectedConvId]: msgs }));
      } catch (error) {
        if (active) {
          setRuntimeError(error.message || "無法載入對話內容");
        }
      }
    })();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedConvId]);

  // ---- conversation helpers (classification is one-way latch) ----
  // Returns the backend integer conversation id. Creates a new row on the
  // server if none selected. Falls back to an optimistic local id if the
  // network call fails — so sending still works in degraded mode, but that
  // row will only persist on retry (user can discard it via delete).
  async function ensureConversation(text, agentId) {
    const effectiveAgentId = agentId || ROUTER_AGENT.id;
    const encryption = agentRequiresEncryption(effectiveAgentId);
    const agentName =
      agents.find((a) => a.id === effectiveAgentId)?.name || effectiveAgentId;

    if (!selectedConvId) {
      let convId;
      let serverRow = null;
      try {
        serverRow = await apiCreateConversation(authRequest, {
          title: makeConversationTitle(text),
          agentId: typeof effectiveAgentId === "number" ? effectiveAgentId : null,
        });
        convId = serverRow.id;
      } catch (error) {
        convId = makeId("cv-local");
        setRuntimeError(error.message || "對話儲存失敗（離線模式）");
      }
      setConversations((prev) => [
        {
          id: convId,
          title: serverRow?.title || makeConversationTitle(text),
          ts: relativeLabel(),
          updatedLabel: relativeLabel(),
          agent: effectiveAgentId,
          agentId: effectiveAgentId,
          agentName,
          folder: "all",
          tags: encryption ? ["classified"] : [],
          starred: false,
          classified: Boolean(serverRow?.classified) || encryption,
          updatedAt: serverRow?.updated_at || nowIso(),
        },
        ...prev,
      ]);
      setSelectedConvId(convId);
      return convId;
    }

    setConversations((prev) =>
      prev.map((c) => {
        if (c.id !== selectedConvId) return c;
        const nextClassified = c.classified || encryption;
        const tags =
          encryption && !(c.tags || []).includes("classified")
            ? [...(c.tags || []), "classified"]
            : c.tags;
        return {
          ...c,
          ts: relativeLabel(),
          updatedLabel: relativeLabel(),
          agent: effectiveAgentId,
          agentId: effectiveAgentId,
          classified: nextClassified,
          tags,
          updatedAt: nowIso(),
        };
      }),
    );
    return selectedConvId;
  }

  function updateConv(id, patch) {
    setConversations((cs) =>
      cs.map((c) => (c.id === id ? { ...c, ...patch } : c)),
    );
  }

  // ---- rename / delete a conversation from the sidebar ----
  async function handleRenameConv(convId, nextTitle) {
    const trimmed = (nextTitle || "").trim();
    if (!trimmed) return;
    // Optimistic local update for instant UI; rollback on backend failure.
    const prev = conversations.find((c) => c.id === convId);
    updateConv(convId, { title: trimmed });
    if (typeof convId !== "number") return; // local-only row (offline)
    try {
      await apiUpdateConversationTitle(authRequest, convId, trimmed);
    } catch (err) {
      if (prev) updateConv(convId, { title: prev.title });
      setRuntimeError(err.message || "重新命名失敗");
    }
  }

  async function handleDeleteConv(convId) {
    const target = conversations.find((c) => c.id === convId);
    if (!target) return;
    if (!window.confirm(`確定要刪除「${target.title}」？此動作無法復原。`)) return;
    const prev = conversations;
    setConversations((cs) => cs.filter((c) => c.id !== convId));
    if (selectedConvId === convId) {
      setSelectedConvId(null);
    }
    setMessagesByConv((prev2) => {
      const { [convId]: _, ...rest } = prev2;
      return rest;
    });
    if (typeof convId !== "number") return;
    try {
      await apiDeleteConversation(authRequest, convId);
    } catch (err) {
      setConversations(prev);
      setRuntimeError(err.message || "刪除對話失敗");
    }
  }

  // ---- title auto-generation (runs once after the first turn lands) ----
  // Uses the same LLM pathway that answered the question, via the router's
  // primary model, so no extra admin configuration is required.
  async function generateConversationTitle(convId, userText, assistantText, effectiveTarget) {
    if (typeof convId !== "number") return;
    if (!apiKey) return;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const systemPrompt =
      "你是對話標題產生器。閱讀以下 Q&A，回覆一個不超過 15 個繁體中文字的標題，" +
      "只能輸出標題本身，不要加引號、冒號、標點或其他說明。";
    const userPrompt = `使用者：${userText}\n助理：${assistantText}`;
    try {
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify({
          model: effectiveTarget,
          stream: false,
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userPrompt },
          ],
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      const raw = data?.choices?.[0]?.message?.content || "";
      // Strip whitespace, quotes, trailing punctuation; clamp length.
      const cleaned = raw
        .trim()
        .replace(/^["「『](.+)["」』]$/s, "$1")
        .replace(/[。．.!！?？,、]+$/, "")
        .slice(0, 30);
      if (!cleaned) return;
      updateConv(convId, { title: cleaned });
      try {
        await apiUpdateConversationTitle(authRequest, convId, cleaned);
      } catch {
        // Non-fatal: title stays local if persist fails.
      }
    } catch {
      // Title generation is best-effort; silent failure is fine.
    }
  }

  // ---- edit a user message + re-run the chat turn ----
  async function handleEditUser(userMsg, nextText) {
    const trimmed = (nextText || "").trim();
    if (!trimmed || trimmed === userMsg.text) return;
    if (!apiKey) {
      setRuntimeError("尚未設定 CSP API Key，請先到設定 → API Key 設定。");
      return;
    }
    const convId = userMsg.conversationId;
    const existing = messagesByConv[convId] || [];
    const idx = existing.findIndex((m) => m.id === userMsg.id);
    if (idx < 0) return;

    const effectiveTarget = selectedAgentId;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;

    // Local: truncate after the edited user message and rewrite its text;
    // create a fresh assistant placeholder so the stream fills in below.
    const assistantId = makeId("a");
    const assistantMsg = {
      id: assistantId,
      role: "assistant",
      text: "",
      trace: [],
      citations: [],
      followUps: [],
      streaming: true,
      routedAgentId: effectiveTarget,
      conversationId: convId,
      createdAt: nowIso(),
      timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
    };
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [
        ...existing.slice(0, idx),
        { ...existing[idx], text: trimmed },
        assistantMsg,
      ],
    }));

    // Backend: persist the edit + server-side truncate so a future reload
    // matches the local state.
    if (typeof convId === "number" && typeof userMsg.dbId === "number") {
      try {
        await apiEditUserMessage(authRequest, convId, userMsg.dbId, trimmed);
      } catch (err) {
        setRuntimeError(err.message || "訊息編輯儲存失敗");
      }
    }

    // Re-run the chat turn with the new user text.
    const payload = {
      model: effectiveTarget,
      messages: [{ role: "user", content: trimmed }],
    };
    let finalText = "";
    let finalMeta = null;
    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        apiKey,
        payload,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onTrace: (step) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    trace: [...(m.trace || []), step],
                    stageLabel: step.label,
                    stage: (m.trace?.length ?? 0),
                  }
                : m,
            ),
          }));
        },
        onMeta: (meta) => {
          finalMeta = meta;
          applyMeta(convId, assistantId, effectiveTarget, meta);
        },
        onReasoning: (delta) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantId
                ? { ...m, reasoning: (m.reasoning || "") + delta }
                : m,
            ),
          }));
        },
      });
      updateMsg(convId, assistantId, { streaming: false });

      if (typeof convId === "number") {
        const agentNameForPersist =
          agents.find((a) => a.id === effectiveTarget)?.name ||
          String(effectiveTarget);
        try {
          const saved = await apiAppendMessage(authRequest, convId, {
            role: "assistant",
            content: finalText,
            traceId: finalMeta?.trace_id,
            latencyMs: finalMeta?.latency_ms,
            agentName: agentNameForPersist,
            metadata: finalMeta || null,
          });
          if (saved && typeof saved.id === "number") {
            updateMsg(convId, assistantId, { dbId: saved.id });
          }
        } catch (persistError) {
          setRuntimeError(persistError.message || "對話訊息儲存失敗");
        }
      }
    } catch (error) {
      updateMsg(convId, assistantId, {
        streaming: false,
        text: `請求失敗：${error.message || "unknown error"}`,
      });
    }
  }

  function updateConversationAgent(conversationId, agentId) {
    const agentName = agents.find((a) => a.id === agentId)?.name || agentId;
    const encryption = agentRequiresEncryption(agentId);
    setConversations((prev) =>
      prev.map((c) => {
        if (c.id !== conversationId) return c;
        const nextClassified = c.classified || encryption;
        const tags =
          encryption && !(c.tags || []).includes("classified")
            ? [...(c.tags || []), "classified"]
            : c.tags;
        return {
          ...c,
          agent: agentId,
          agentId,
          agentName,
          classified: nextClassified,
          tags,
          ts: relativeLabel(),
          updatedLabel: relativeLabel(),
          updatedAt: nowIso(),
        };
      }),
    );
  }

  function updateMsg(convId, msgId, patch) {
    setMessagesByConv((prev) => {
      const list = prev[convId] || [];
      return {
        ...prev,
        [convId]: list.map((m) => (m.id === msgId ? { ...m, ...patch } : m)),
      };
    });
  }

  function applyMeta(convId, msgId, agentId, meta) {
    // Streaming paths emit each trace step as its own SSE event, and the
    // final ``anila.meta`` intentionally ships ``trace: []`` to avoid
    // duplicating them. Keep the accumulated trace in that case; only
    // replace when the meta actually carries trace data (non-stream
    // paths bundle everything into one frame).
    const metaTrace = Array.isArray(meta.trace) ? meta.trace : [];
    const tracePatch = metaTrace.length > 0 ? { trace: metaTrace } : {};
    updateMsg(convId, msgId, {
      traceId: meta.trace_id,
      ...tracePatch,
      citations: meta.citations || [],
      confidence: meta.confidence,
      handoffChain: meta.handoff_chain || [],
      followUps: meta.follow_ups || [],
      latencyMs: meta.latency_ms,
      classified: meta.classified,
      reasoning: meta.reasoning || null,
      routedAgentId: meta.handoff_chain?.at?.(-1)?.agent_id || agentId,
      stageLabel: meta.trace?.at?.(-1)?.label,
      conversationId: convId,
    });

    // Classification latch — one-way. See runtime/classified.js.
    if (meta.classified === true) {
      setConversations((prev) =>
        prev.map((c) => (c.id === convId ? latchConversationWithMeta(c, meta) : c)),
      );
    }
  }

  // ---- send single ----
  async function sendMessage(text, attachments = [], meta = {}) {
    if (!apiKey) {
      setRuntimeError("尚未設定 CSP API Key，請先到設定 → API Key 設定。");
      return;
    }
    const { explicitAgents = [], piiHits = [] } = meta;
    if (explicitAgents.length > 1) {
      return sendCompare(text, attachments, { explicitAgents, piiHits });
    }

    const effectiveTarget = explicitAgents[0] || selectedAgentId;
    const convId = await ensureConversation(text, effectiveTarget);
    updateConversationAgent(convId, effectiveTarget);

    const userMsg = {
      id: makeId("u"),
      role: "user",
      text,
      attachments,
      piiHits,
      explicitAgents,
      conversationId: convId,
      createdAt: nowIso(),
    };
    const assistantId = makeId("a");
    const assistantMsg = {
      id: assistantId,
      role: "assistant",
      text: "",
      trace: [],
      citations: [],
      followUps: [],
      streaming: true,
      routedAgentId: effectiveTarget,
      conversationId: convId,
      createdAt: nowIso(),
      timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
    };
    const priorForHistory = messagesByConv[convId] || [];
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [...(prev[convId] || []), userMsg, assistantMsg],
    }));

    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const payload = {
      model: effectiveTarget,
      messages: buildMessageHistory(priorForHistory, text, attachments),
    };

    let finalText = "";
    let finalMeta = null;
    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        apiKey,
        payload,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onTrace: (step) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    trace: [...(m.trace || []), step],
                    stageLabel: step.label,
                    stage: (m.trace?.length ?? 0),
                  }
                : m,
            ),
          }));
        },
        onMeta: (meta) => {
          finalMeta = meta;
          applyMeta(convId, assistantId, effectiveTarget, meta);
        },
        onReasoning: (delta) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantId
                ? { ...m, reasoning: (m.reasoning || "") + delta }
                : m,
            ),
          }));
        },
      });
      updateMsg(convId, assistantId, { streaming: false });

      // Persist both turns to the backend so they survive reload. Server-side
      // errors here surface as a toast but don't break the live UI.
      if (typeof convId === "number") {
        const agentNameForPersist =
          agents.find((a) => a.id === effectiveTarget)?.name ||
          String(effectiveTarget);
        try {
          await apiAppendMessage(authRequest, convId, {
            role: "user",
            content: text,
          });
          const savedAssistant = await apiAppendMessage(authRequest, convId, {
            role: "assistant",
            content: finalText,
            traceId: finalMeta?.trace_id,
            latencyMs: finalMeta?.latency_ms,
            agentName: agentNameForPersist,
            metadata: finalMeta || null,
          });
          // Capture DB id so thumbs-up/down can PUT to the backend.
          if (savedAssistant && typeof savedAssistant.id === "number") {
            updateMsg(convId, assistantId, { dbId: savedAssistant.id });
          }
        } catch (persistError) {
          setRuntimeError(persistError.message || "對話訊息儲存失敗");
        }

        // Bump updatedAt so the sidebar re-sorts / re-labels with live time.
        updateConv(convId, { updatedAt: nowIso() });

        // First-turn auto title: fires once (only when the existing title was
        // produced by the first-message truncator). Background task; silent
        // failure is acceptable.
        const convRow = conversations.find((c) => c.id === convId);
        const looksLikeAutoTitle =
          !convRow?.title || convRow.title === makeConversationTitle(text);
        if (looksLikeAutoTitle && finalText) {
          generateConversationTitle(convId, text, finalText, effectiveTarget);
        }
      }
    } catch (error) {
      updateMsg(convId, assistantId, {
        streaming: false,
        text: `請求失敗：${error.message || "unknown error"}`,
      });
    }
  }

  // ---- regenerate a single assistant message ----
  // Finds the user message immediately before the target assistant message
  // and re-runs the chat call, replacing the assistant message's text /
  // trace in place. Caller API key permissions and routing target are
  // inherited from the original turn.
  async function regenerateMessage(assistantMsg) {
    if (!apiKey) {
      setRuntimeError("尚未設定 CSP API Key，請先到設定 → API Key 設定。");
      return;
    }
    const convId = assistantMsg.conversationId;
    const msgs = messagesByConv[convId] || [];
    const idx = msgs.findIndex((m) => m.id === assistantMsg.id);
    if (idx <= 0) return;
    // Walk back to the nearest user message. Older conversations may contain
    // runs of consecutive assistant rows (router handoff announcements, or
    // legacy duplicates from the pre-fix regenerate path) between the user's
    // prompt and the reply being regenerated.
    let userIdx = idx - 1;
    while (userIdx >= 0 && msgs[userIdx].role !== "user") {
      userIdx -= 1;
    }
    if (userIdx < 0) {
      setRuntimeError("找不到對應的使用者訊息，無法重試。");
      return;
    }
    const prevUser = msgs[userIdx];

    // Messages that came *after* this assistant reply. ChatGPT-style
    // regenerate forks a new branch: the tail belongs to the old revision
    // and must be hidden from the active thread while kept retrievable via
    // the < N/M > pager.
    const tailMsgs = msgs.slice(idx + 1);

    const effectiveTarget = assistantMsg.routedAgentId || selectedAgentId;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const payload = {
      model: effectiveTarget,
      messages: buildMessageHistory(msgs.slice(0, userIdx), prevUser.text, prevUser.attachments || []),
    };

    // Snapshot the current top-level fields into revisions[] so the user can
    // flip back to the previous answer with the < / > pager. If this is the
    // first regenerate we seed revisions with the original reply too, and
    // attach the abandoned tail to it so flipping back restores the old
    // branch of follow-up turns.
    const currentSnapshot = {
      text: assistantMsg.text,
      trace: assistantMsg.trace || [],
      reasoning: assistantMsg.reasoning || null,
      traceId: assistantMsg.traceId,
      latencyMs: assistantMsg.latencyMs,
      dbId: assistantMsg.dbId,
      timestamp: assistantMsg.timestamp,
      tail: tailMsgs,
    };
    const existingRevs = Array.isArray(assistantMsg.revisions) && assistantMsg.revisions.length > 0
      ? assistantMsg.revisions.map((r, i) =>
          i === assistantMsg.activeRev ? { ...r, tail: tailMsgs } : r,
        )
      : [currentSnapshot];
    // Placeholder for the revision currently being streamed; new branch
    // starts with empty tail — follow-up turns will accumulate into it as
    // the user continues the conversation.
    const nextRevs = [...existingRevs, { text: "", trace: [], reasoning: null, tail: [] }];
    const nextActiveIdx = nextRevs.length - 1;

    // Drop the tail from the active thread; it stays preserved on the
    // previous revision so the user can flip back to it. Reset the
    // assistant row to streaming state at the same time.
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: (prev[convId] || []).slice(0, idx + 1).map((m) =>
        m.id === assistantMsg.id
          ? {
              ...m,
              text: "",
              trace: [],
              citations: [],
              followUps: [],
              streaming: true,
              rating: null,
              reasoning: null,
              revisions: nextRevs,
              activeRev: nextActiveIdx,
              timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
            }
          : m,
      ),
    }));

    let finalText = "";
    let finalMeta = null;
    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        apiKey,
        payload,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantMsg.id, { text: acc });
        },
        onTrace: (step) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantMsg.id
                ? {
                    ...m,
                    trace: [...(m.trace || []), step],
                    stageLabel: step.label,
                    stage: (m.trace?.length ?? 0),
                  }
                : m,
            ),
          }));
        },
        onMeta: (meta) => {
          finalMeta = meta;
          applyMeta(convId, assistantMsg.id, effectiveTarget, meta);
        },
        onReasoning: (delta) => {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: (prev[convId] || []).map((m) =>
              m.id === assistantMsg.id
                ? { ...m, reasoning: (m.reasoning || "") + delta }
                : m,
            ),
          }));
        },
      });
      // Freeze the finished revision into revisions[nextActiveIdx] so that
      // switching back and forth after completion shows stable text/trace.
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: (prev[convId] || []).map((m) => {
          if (m.id !== assistantMsg.id) return m;
          const revs = Array.isArray(m.revisions) ? [...m.revisions] : [];
          revs[nextActiveIdx] = {
            text: finalText,
            trace: [...(m.trace || [])],
            reasoning: m.reasoning || null,
            traceId: finalMeta?.trace_id,
            latencyMs: finalMeta?.latency_ms,
            dbId: m.dbId,
            timestamp: m.timestamp,
          };
          return { ...m, streaming: false, revisions: revs };
        }),
      }));

      if (typeof convId === "number") {
        const agentNameForPersist =
          agents.find((a) => a.id === effectiveTarget)?.name ||
          String(effectiveTarget);
        try {
          if (typeof assistantMsg.dbId === "number") {
            // Replace the existing row in place — avoids piling up orphan
            // assistant rows that trip the "no preceding user message" guard
            // on reload.
            await apiUpdateMessage(authRequest, convId, assistantMsg.dbId, {
              content: finalText,
              traceId: finalMeta?.trace_id,
              latencyMs: finalMeta?.latency_ms,
              agentName: agentNameForPersist,
              metadata: finalMeta || null,
            });
          } else {
            const savedAssistant = await apiAppendMessage(authRequest, convId, {
              role: "assistant",
              content: finalText,
              traceId: finalMeta?.trace_id,
              latencyMs: finalMeta?.latency_ms,
              agentName: agentNameForPersist,
              metadata: finalMeta || null,
            });
            if (savedAssistant && typeof savedAssistant.id === "number") {
              updateMsg(convId, assistantMsg.id, { dbId: savedAssistant.id });
            }
          }
        } catch (persistError) {
          setRuntimeError(persistError.message || "重試訊息儲存失敗");
        }
      }
    } catch (error) {
      updateMsg(convId, assistantMsg.id, {
        streaming: false,
        text: `重試失敗：${error.message || "unknown error"}`,
      });
    }
  }

  // ---- switch between assistant-reply revisions (< 2/3 > pager) ----
  // Revisions only live in client state (not persisted), so on reload the
  // message collapses back to the latest active revision. Swapping pulls
  // the fields out of revisions[i] back into the top-level mirror so the
  // rest of the render path (MarkdownView, thinking fold, rating) doesn't
  // need to know about revisions at all.
  function switchRevision(assistantMsg, nextIdx) {
    const convId = assistantMsg.conversationId;
    const revs = Array.isArray(assistantMsg.revisions) ? assistantMsg.revisions : [];
    if (nextIdx < 0 || nextIdx >= revs.length) return;
    if (nextIdx === assistantMsg.activeRev) return;
    const target = revs[nextIdx] || {};
    setMessagesByConv((prev) => {
      const list = prev[convId] || [];
      const idx = list.findIndex((m) => m.id === assistantMsg.id);
      if (idx < 0) return prev;
      // Before swapping, snapshot the tail currently attached to this
      // assistant so the revision we're leaving keeps its own branch of
      // follow-up turns — the user can continue on either revision.
      const currentTail = list.slice(idx + 1);
      const updatedRevs = revs.map((r, i) =>
          i === assistantMsg.activeRev ? { ...r, tail: currentTail } : r,
      );
      const updatedAssistant = {
        ...list[idx],
        text: target.text || "",
        trace: target.trace || [],
        reasoning: target.reasoning || null,
        traceId: target.traceId,
        latencyMs: target.latencyMs,
        timestamp: target.timestamp,
        activeRev: nextIdx,
        revisions: updatedRevs,
      };
      const nextList = [
        ...list.slice(0, idx),
        updatedAssistant,
        ...(Array.isArray(target.tail) ? target.tail : []),
      ];
      return { ...prev, [convId]: nextList };
    });
  }

  // ---- thumbs up / down ----
  // Optimistically toggles rating locally for instant feedback, then PUTs to
  // the CSP rating endpoint. On failure the optimistic value is rolled back
  // so the UI never drifts from persisted state.
  async function handleRate(targetMsg, nextRating) {
    const convId = targetMsg.conversationId;
    const prevRating = targetMsg.rating ?? null;
    updateMsg(convId, targetMsg.id, { rating: nextRating });

    if (typeof convId !== "number" || typeof targetMsg.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存至後端，反饋僅保留於本地。");
      return;
    }
    try {
      await apiRateMessage(authRequest, convId, targetMsg.dbId, nextRating);
    } catch (err) {
      updateMsg(convId, targetMsg.id, { rating: prevRating });
      setRuntimeError(err.message || "反饋儲存失敗");
    }
  }

  // ---- send compare ----
  async function sendCompare(text, attachments = [], meta = {}) {
    if (!apiKey) {
      setRuntimeError("尚未設定 CSP API Key。");
      return;
    }
    const explicit = (meta.explicitAgents || []).filter(
      (id) => id !== ROUTER_AGENT.id,
    );
    const cols = explicit.length
      ? explicit.slice(0, 3).map((agentId, i) => ({
          id: `col-${i + 1}-${Date.now()}`,
          agentId,
        }))
      : compareColumns;
    if (cols.length && cols !== compareColumns) setCompareColumns(cols);

    setCompareMode(true);
    setCitationsOpen(false);

    await Promise.all(
      cols.map(async (col) => {
        const uId = makeId("u-" + col.id);
        const aId = makeId("a-" + col.id);
        const userMsg = {
          id: uId,
          role: "user",
          text,
          attachments,
          piiHits: meta.piiHits || [],
          conversationId: col.id,
          createdAt: nowIso(),
        };
        const assistantMsg = {
          id: aId,
          role: "assistant",
          text: "",
          trace: [],
          citations: [],
          followUps: [],
          streaming: true,
          routedAgentId: col.agentId,
          conversationId: col.id,
          createdAt: nowIso(),
        };
        setCompareMsgs((prev) => ({
          ...prev,
          [col.id]: [...(prev[col.id] || []), userMsg, assistantMsg],
        }));

        try {
          await streamChatCompletion({
            url: `${config.cspBaseUrl}/v1/chat/completions`,
            apiKey,
            payload: {
              model: col.agentId,
              messages: [{ role: "user", content: buildUserContent(text, attachments) }],
            },
            onText: (acc) => {
              setCompareMsgs((prev) => ({
                ...prev,
                [col.id]: (prev[col.id] || []).map((m) =>
                  m.id === aId ? { ...m, text: acc } : m,
                ),
              }));
            },
            onTrace: (step) => {
              setCompareMsgs((prev) => ({
                ...prev,
                [col.id]: (prev[col.id] || []).map((m) =>
                  m.id === aId
                    ? {
                        ...m,
                        trace: [...(m.trace || []), step],
                        stageLabel: step.label,
                        stage: (m.trace?.length ?? 0),
                      }
                    : m,
                ),
              }));
            },
            onMeta: (m) => {
              setCompareMsgs((prev) => ({
                ...prev,
                [col.id]: (prev[col.id] || []).map((msg) =>
                  msg.id === aId
                    ? {
                        ...msg,
                        traceId: m.trace_id,
                        trace: m.trace || [],
                        citations: m.citations || [],
                        confidence: m.confidence,
                        handoffChain: m.handoff_chain || [],
                        followUps: m.follow_ups || [],
                        latencyMs: m.latency_ms,
                        classified: m.classified,
                        routedAgentId:
                          m.handoff_chain?.at?.(-1)?.agent_id || col.agentId,
                      }
                    : msg,
                ),
              }));
            },
          });
          setCompareMsgs((prev) => ({
            ...prev,
            [col.id]: (prev[col.id] || []).map((m) =>
              m.id === aId ? { ...m, streaming: false } : m,
            ),
          }));
        } catch (error) {
          setCompareMsgs((prev) => ({
            ...prev,
            [col.id]: (prev[col.id] || []).map((m) =>
              m.id === aId
                ? { ...m, streaming: false, text: `請求失敗：${error.message}` }
                : m,
            ),
          }));
        }
      }),
    );
  }

  function enterCompare() {
    const pick = directAgents.slice(0, 2);
    if (pick.length < 2) {
      setRuntimeError("需要至少 2 個可用的 agent 才能比較");
      return;
    }
    setCompareColumns(
      pick.map((agent, i) => ({ id: `col-${i + 1}`, agentId: agent.id })),
    );
    setCompareMsgs({});
    setCompareMode(true);
  }

  function exitCompare() {
    setCompareMode(false);
    setCompareColumns([]);
    setCompareMsgs({});
  }

  function adoptColumn(col) {
    const msgs = compareMsgs[col.id] || [];
    if (!msgs.length) return;
    const firstUser = msgs.find((m) => m.role === "user");
    const agentName =
      agents.find((a) => a.id === col.agentId)?.name || col.agentId;
    const encryption = agentRequiresEncryption(col.agentId);
    const convId = makeId("cv");
    setConversations((prev) => [
      {
        id: convId,
        title: makeConversationTitle(firstUser?.text || "採用比較結果"),
        ts: relativeLabel(),
        updatedLabel: relativeLabel(),
        agent: col.agentId,
        agentId: col.agentId,
        agentName,
        folder: "all",
        tags: encryption ? ["compared", "classified"] : ["compared"],
        starred: false,
        classified: encryption,
        updatedAt: nowIso(),
      },
      ...prev,
    ]);
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: msgs.map((m) => ({ ...m, conversationId: convId })),
    }));
    setSelectedConvId(convId);
    setSelectedAgentId(col.agentId);
    exitCompare();
  }

  // ---- misc handlers ----
  function newChat() {
    setSelectedConvId(null);
    setCitationsOpen(false);
    setCompareMode(false);
    setShareOpen(false);
  }

  function onOpenCitation(c) {
    const msg = [...currentMsgs]
      .reverse()
      .find((m) => m.role === "assistant" && m.citations?.some((x) => x.id === c?.id));
    const target =
      msg ||
      [...currentMsgs]
        .reverse()
        .find((m) => m.role === "assistant" && m.citations?.length);
    if (!target) return;
    setActiveCitations(target.citations || []);
    setActiveCitationId(c?.id || null);
    setCitationsOpen(true);
  }

  function handoffToAgent(newAgentId) {
    if (!selectedConvId) return;
    const label =
      agents.find((a) => a.id === newAgentId)?.name || newAgentId;
    const sysMsg = {
      id: makeId("sys"),
      role: "assistant",
      text: `[Router] 已從 ${selectedAgentId} 交接給 ${label}，繼承上下文。`,
      streaming: false,
      routedAgentId: newAgentId,
      trace: [],
      handoffChain: [
        { agent_id: selectedAgentId, label: "current owner" },
        { agent_id: newAgentId, label: "manual handoff" },
      ],
      confidence: { level: "high", score: 1.0, reasons: ["manual_handoff"] },
      conversationId: selectedConvId,
      createdAt: nowIso(),
    };
    setMessagesByConv((prev) => ({
      ...prev,
      [selectedConvId]: [...(prev[selectedConvId] || []), sysMsg],
    }));
    updateConversationAgent(selectedConvId, newAgentId);
    setSelectedAgentId(newAgentId);
  }

  // ---- render: classified watermark + top bar + messages + composer ----
  return (
    <div style={{ display: "flex", height: "100vh", background: "var(--bg)", position: "relative" }}>
      {isClassified && (
        <ConfidentialWatermark
          userEmail={user?.email || user?.username}
          traceId={latestAssistantMessage?.traceId}
        />
      )}
      <Sidebar
        conversations={conversations}
        selectedConvId={selectedConvId}
        onSelectConv={(id) => {
          setSelectedConvId(id);
          setCitationsOpen(false);
          setCompareMode(false);
        }}
        onNewChat={newChat}
        agents={agents}
        user={user}
        onLogout={logoutAndRedirect}
        onOpenSettings={(tab) => {
          setSettingsTab(tab || "general");
          setSettingsOpen(true);
        }}
        onOpenAgentBrowser={() => {}}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        folder={folder}
        setFolder={setFolder}
        folders={folders}
        onCreateFolder={createFolder}
        onDeleteFolder={deleteFolder}
        onOpenTagEditor={(id, patch) => updateConv(id, patch)}
        onRenameConv={handleRenameConv}
        onDeleteConv={handleDeleteConv}
      />

      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "10px 18px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
        }}>
          {tweaks.agentSwitcherPosition === "top" && !compareMode ? (
            <AgentSelector agents={agents} value={selectedAgentId} onChange={setSelectedAgentId} />
          ) : (
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600, fontSize: 14 }}>
              {selectedConv?.classified && <IconLock size={14} style={{ color: "var(--danger)" }} />}
              <span>
                {compareMode
                  ? "比較模式"
                  : selectedConv?.title || "新對話"}
              </span>
            </div>
          )}

          <div style={{ flex: 1 }} />

          {selectedConv && !compareMode && (
            <>
              {selectedConv.classified && (
                <span
                  title="此對話已鎖為加密模式（由後端 agent 設定強制啟用）。"
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 4,
                    padding: "3px 9px",
                    background: "oklch(0.95 0.02 25 / 0.4)",
                    border: "1px solid var(--danger)",
                    borderRadius: 999,
                    fontSize: 11, fontFamily: "var(--font-mono)",
                    color: "var(--danger)",
                  }}
                >
                  <IconLock size={11} /> 加密模式
                </span>
              )}
              <Dropdown align="right" width={260} trigger={() => (
                <IconButton title="交接 handoff"><IconNodes size={14} /></IconButton>
              )}>
                {(close) => (
                  <HandoffMenu
                    agents={agents}
                    currentAgentId={selectedAgentId}
                    onHandoffAgent={handoffToAgent}
                    onHandoffUser={async (target) => {
                      if (!selectedConvId || typeof selectedConvId !== "number") {
                        setRuntimeError("尚未建立後端對話，無法交接");
                        return;
                      }
                      try {
                        await apiCreateHandoff(authRequest, {
                          conversationId: selectedConvId,
                          note: `交接給 ${target}`,
                        });
                      } catch (error) {
                        setRuntimeError(error.message || "交接請求失敗");
                      }
                    }}
                    close={close}
                  />
                )}
              </Dropdown>
              <IconButton
                title={selectedConv.classified ? "加密對話不可分享" : "分享"}
                onClick={() => !selectedConv.classified && setShareOpen(true)}
                disabled={selectedConv.classified}
                style={selectedConv.classified ? { opacity: 0.4, cursor: "not-allowed" } : {}}
              >
                <IconShare size={14} />
              </IconButton>
            </>
          )}

          <IconButton
            title={
              compareMode
                ? "退出比較"
                : directAgents.length < 2
                  ? "比較模式 (需至少 2 個 agent)"
                  : "比較模式 (兩個 agent 並排)"
            }
            onClick={() => (compareMode ? exitCompare() : enterCompare())}
            active={compareMode}
          >
            <IconColumns size={14} />
          </IconButton>

          <Dropdown align="right" width={320} trigger={() => (
            <button title="CSP API Key" style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "4px 9px",
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: 999, cursor: "pointer",
              color: "var(--fg-muted)", fontSize: 11,
              fontFamily: "var(--font-mono)",
            }}>
              <span style={{
                width: 6, height: 6, borderRadius: 999,
                background: apiKeyStatus.valid ? "var(--success)" : "var(--danger)",
              }} />
              <IconKey size={12} />
              <span>{maskApiKey(apiKey)}</span>
            </button>
          )}>
            {(close) => (
              <ApiKeyPopover
                onClose={close}
                onSaveApiKey={async (next) => {
                  try {
                    await updateApiKey(next);
                    await refreshAgents();
                    close();
                  } catch (error) {
                    alert(error.message || "API Key 驗證失敗");
                  }
                }}
                apiKey={apiKey}
                status={apiKeyStatus}
              />
            )}
          </Dropdown>

          <IconButton title="重新載入 agent" onClick={() => void refreshAgents()} disabled={loadingAgents}>
            <IconRefresh size={14} />
          </IconButton>

          <IconButton title="設定" onClick={() => { setSettingsTab("general"); setSettingsOpen(true); }}>
            <IconSettings />
          </IconButton>
          <IconButton
            title={tweaks.dark ? "切換淺色" : "切換深色"}
            onClick={() => setTweaks({ ...tweaks, dark: !tweaks.dark })}
          >
            {tweaks.dark ? <IconSun /> : <IconMoon />}
          </IconButton>
          <IconButton title="Tweaks" onClick={() => setTweaksOpen((o) => !o)} active={tweaksOpen}>
            <IconSpark />
          </IconButton>
        </div>

        {runtimeError && (
          <div style={{
            padding: "8px 18px",
            background: "oklch(0.97 0.03 25)",
            borderBottom: "1px solid oklch(0.88 0.08 25)",
            color: "var(--danger)",
            fontSize: 12, fontFamily: "var(--font-mono)",
          }}>
            {runtimeError}
          </div>
        )}

        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
            {compareMode ? (
              <ParallelCompareView
                agents={directAgents}
                columns={compareColumns}
                setColumns={setCompareColumns}
                messagesByColumn={compareMsgs}
                onSend={(text, atts, meta) => sendCompare(text, atts, meta)}
                onExit={exitCompare}
                onAdoptColumn={adoptColumn}
                AgentSelector={AgentSelector}
                Composer={Composer}
                MessageBubble={MessageBubble}
              />
            ) : (
              <>
                <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", background: "var(--bg)" }}>
                  <div style={{
                    maxWidth: 760, margin: "0 auto",
                    padding: `calc(var(--density) * 1.2) var(--density)`,
                  }}>
                    {currentMsgs.length === 0 ? (
                      <EmptyState
                        agent={activeAgent}
                        loading={loadingAgents}
                        onPick={(q) => sendMessage(q, [], {})}
                      />
                    ) : (
                      currentMsgs.map((m) => (
                        <MessageBubble
                          key={m.id}
                          msg={m}
                          agents={agents}
                          conversationId={selectedConvId}
                          classified={isClassified}
                          onRegenerate={regenerateMessage}
                          onRate={handleRate}
                          onEditUser={handleEditUser}
                          onSwitchRevision={switchRevision}
                          onOpenCitation={onOpenCitation}
                          onPickFollowUp={(q) => sendMessage(q, [], {})}
                        />
                      ))
                    )}
                  </div>
                </div>

                <div style={{ padding: "0 var(--density) var(--density)", background: "var(--bg)" }}>
                  <div style={{ maxWidth: 760, margin: "0 auto" }}>
                    {tweaks.agentSwitcherPosition === "bottom" && (
                      <div style={{ marginBottom: 8, display: "flex", gap: 6, alignItems: "center" }}>
                        <span style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
                          target:
                        </span>
                        <AgentSelector agents={agents} value={selectedAgentId} onChange={setSelectedAgentId} />
                        {activeEncryptionRequired && (
                          <span title="此 agent 為加密模型" style={{
                            display: "inline-flex", alignItems: "center", gap: 3,
                            padding: "1px 7px",
                            background: "oklch(0.95 0.02 25 / 0.4)",
                            border: "1px solid var(--danger)",
                            borderRadius: 999,
                            fontSize: 11, color: "var(--danger)",
                            fontFamily: "var(--font-mono)",
                          }}>
                            <IconLock size={10} /> 加密模型
                          </span>
                        )}
                      </div>
                    )}
                    <Composer
                      onSend={sendMessage}
                      agents={agents}
                      placeholder="問 ANILA 任何事情，或用 @agent 指定 agent · Shift+Enter 換行"
                      footer={
                        selectedAgentId === ROUTER_AGENT.id
                          ? "Auto route · 由 ANILA Router 判斷是否分派 agent"
                          : `Direct target · ${activeAgent.name}`
                      }
                      onUpload={(file) =>
                        apiUploadAttachment(multipartRequest, file, {
                          conversationId:
                            typeof selectedConvId === "number" ? selectedConvId : undefined,
                        })
                      }
                    />
                    <div style={{
                      marginTop: 6, fontSize: 11,
                      color: "var(--fg-subtle)", textAlign: "center",
                      fontFamily: "var(--font-mono)",
                    }}>
                      ANILA {activeAgent?.id === ROUTER_AGENT.id
                        ? "會自動分派給合適的 agent"
                        : `→ ${activeAgent?.name}`}
                      {" · 所有呼叫經 CSP · "}
                      <span style={{ color: "var(--fg-muted)" }}>回應僅供參考</span>
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>

          {citationsOpen && !compareMode && (
            <CitationsDrawer
              open={citationsOpen}
              citations={activeCitations}
              activeId={activeCitationId}
              onClose={() => setCitationsOpen(false)}
              onJumpTo={(c) => c?.source_uri && window.open(c.source_uri, "_blank", "noopener")}
            />
          )}
        </div>
      </div>

      <SettingsModal
        open={settingsOpen}
        tab={settingsTab}
        setTab={setSettingsTab}
        onClose={() => setSettingsOpen(false)}
        user={user}
        apiKey={apiKey}
        apiKeyStatus={apiKeyStatus}
        onSaveApiKey={async (next) => {
          try {
            await updateApiKey(next);
            await refreshAgents();
          } catch (error) {
            alert(error.message || "API Key 驗證失敗");
          }
        }}
        agents={agents}
      />

      <TweaksPanel
        open={tweaksOpen}
        onClose={() => setTweaksOpen(false)}
        tweaks={tweaks}
        setTweaks={setTweaks}
      />

      <ShareDialog
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        conversation={selectedConv}
        user={user}
        onCreateShare={async ({ mode, allowFork, expiresAt }) => {
          if (!selectedConvId || typeof selectedConvId !== "number") {
            throw new Error("尚未建立後端對話 — 請先送出第一則訊息");
          }
          const share = await apiCreateShare(authRequest, selectedConvId, {
            mode,
            allowFork,
            expiresAt,
          });
          return { ...share, url: buildShareUrl(share.token) };
        }}
      />
    </div>
  );
}

// ---- Empty state -----------------------------------------------------------
function EmptyState({ agent, onPick, loading }) {
  return (
    <div style={{ padding: "64px 12px 32px", textAlign: "center" }}>
      <AnilaGlyph size={40} />
      <div style={{ marginTop: 16, fontSize: 22, fontWeight: 600, letterSpacing: -0.2 }}>
        你今天想問 ANILA 什麼？
      </div>
      <div style={{ marginTop: 6, color: "var(--fg-muted)", fontSize: 13 }}>
        {loading
          ? "agent 清單載入中…"
          : agent?.id === ROUTER_AGENT.id
            ? "輸入問題，Router 會自動分派；也可用 @agent 直接指定"
            : `當前 agent: ${agent?.name}`}
      </div>
      <div style={{
        marginTop: 36, display: "grid",
        gridTemplateColumns: "1fr 1fr", gap: 10,
        maxWidth: 600, margin: "36px auto 0", textAlign: "left",
      }}>
        {STARTER_PROMPTS.map((s, i) => (
          <button key={i} onClick={() => onPick(s.q)} style={{
            padding: "12px 14px",
            background: "var(--bg-elev)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            cursor: "pointer", textAlign: "left",
            transition: "all .12s",
          }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = "var(--border-strong)";
              e.currentTarget.style.transform = "translateY(-1px)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border)";
              e.currentTarget.style.transform = "";
            }}>
            <div style={{ fontSize: 13, fontWeight: 500, color: "var(--fg)" }}>{s.title}</div>
            <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 3 }}>{s.sub}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---- Api Key popover -------------------------------------------------------
function ApiKeyPopover({ apiKey, onSaveApiKey, onClose, status }) {
  const [val, setVal] = useState(apiKey || "");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await onSaveApiKey(val);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ padding: 10, minWidth: 300 }}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>CSP API Key</div>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 10 }}>
        所有 ANILA 的 agent 呼叫都會帶這把 key。格式{" "}
        <span style={{ fontFamily: "var(--font-mono)" }}>sk-…</span>
      </div>
      <Input
        type={show ? "text" : "password"}
        value={val}
        onChange={(e) => setVal(e.target.value)}
        leftIcon={<IconKey size={13} />}
        rightEl={
          <IconButton onClick={() => setShow((s) => !s)}>
            {show ? <IconEyeOff /> : <IconEye />}
          </IconButton>
        }
      />
      <div style={{ display: "flex", gap: 6, marginTop: 10, justifyContent: "flex-end" }}>
        <Button size="sm" onClick={onClose}>取消</Button>
        <Button size="sm" variant="primary" onClick={save} disabled={busy || !val}>
          {busy ? "驗證中…" : "儲存"}
        </Button>
      </div>
      <div style={{
        marginTop: 10, padding: "7px 9px",
        background: "var(--bg-subtle)", border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.5,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{
            width: 6, height: 6, borderRadius: 999,
            background: status?.valid ? "var(--success)" : "var(--danger)",
          }} />
          <span>{status?.valid ? "可呼叫 /v1/agents" : status?.error || "尚未驗證"}</span>
        </div>
      </div>
    </div>
  );
}

// ---- Settings modal --------------------------------------------------------
function SettingsModal({
  open, tab, setTab, onClose,
  user, apiKey, apiKeyStatus, onSaveApiKey, agents,
}) {
  return (
    <Modal open={open} onClose={onClose} title="設定" subtitle="runtime 偏好與帳號" width={620}>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 20 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {[
            { id: "general", label: "一般",       icon: <IconSettings size={13} /> },
            { id: "apikey",  label: "API Key",    icon: <IconKey      size={13} /> },
            { id: "privacy", label: "隱私 / 信任", icon: <IconShield   size={13} /> },
            { id: "account", label: "帳號",        icon: <IconUser     size={13} /> },
            { id: "about",   label: "關於",        icon: <AnilaGlyph   size={13} /> },
          ].map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)} style={{
              display: "flex", alignItems: "center", gap: 8,
              padding: "7px 10px", fontSize: 13,
              background: tab === t.id ? "var(--bg-subtle)" : "transparent",
              border: "1px solid " + (tab === t.id ? "var(--border)" : "transparent"),
              borderRadius: "var(--radius)",
              color: "var(--fg)", textAlign: "left", cursor: "pointer",
            }}>
              {t.icon}{t.label}
            </button>
          ))}
        </div>
        <div>
          {tab === "general" && (
            <div style={{ display: "grid", gap: 12 }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 500 }}>預設 agent</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 4 }}>
                  目前由 /v1/agents 動態載入，共 {Math.max(agents.length - 1, 0)} 個可用 agent。
                  切換預設 agent 請從主介面的 agent selector 進行。
                </div>
              </div>
              <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>
                加密模式由 agent 設定（requires_encryption）或後端 meta 決定，使用者無法手動切換。
              </div>
            </div>
          )}

          {tab === "apikey" && (
            <ApiKeyTab
              apiKey={apiKey}
              status={apiKeyStatus}
              onSaveApiKey={onSaveApiKey}
            />
          )}

          {tab === "privacy" && (
            <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>敏感資訊處理</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  實際遮罩在 CSP proxy 層執行。UI 只在送出前提示；無法關閉後端的審計與遮罩。
                </div>
              </div>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>加密對話</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  若指定的 agent 為加密模型（requires_encryption=true），此對話會自動鎖為機密：
                  禁止複製、禁止分享，且加上稽核浮水印。此狀態無法由使用者解除。
                </div>
              </div>
            </div>
          )}

          {tab === "account" && (
            <div style={{ fontSize: 13 }}>
              <div style={{ marginBottom: 4 }}><b>{user?.username}</b></div>
              <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>
                role: {user?.role || "user"} · auth: {user?.auth_source || "csp"}
              </div>
            </div>
          )}

          {tab === "about" && (
            <div style={{ fontSize: 13, lineHeight: 1.7 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <AnilaGlyph size={24} />
                <div style={{ fontSize: 16, fontWeight: 600 }}>ANILA Runtime Client</div>
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-muted)" }}>
                v0.2.0 · trust + multi-agent + collab
              </div>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

function ApiKeyTab({ apiKey, status, onSaveApiKey }) {
  const [val, setVal] = useState(apiKey || "");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function save() {
    setBusy(true);
    setMsg("");
    try {
      await onSaveApiKey(val);
      setMsg("✓ 已儲存");
    } catch (error) {
      setMsg(error.message || "API Key 驗證失敗");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 500 }}>CSP API Key</div>
        <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 6 }}>
          ANILA 使用 CSP 發的 API Key 做 runtime 呼叫。格式 sk-…
        </div>
        <Input
          type={show ? "text" : "password"}
          value={val}
          onChange={(e) => { setVal(e.target.value); setMsg(""); }}
          leftIcon={<IconKey size={13} />}
          rightEl={
            <IconButton onClick={() => setShow((s) => !s)}>
              {show ? <IconEyeOff /> : <IconEye />}
            </IconButton>
          }
        />
        <div style={{ display: "flex", gap: 6, marginTop: 8, alignItems: "center" }}>
          <Button size="sm" variant="primary" onClick={save} disabled={busy || !val}>
            {busy ? "驗證中…" : "儲存"}
          </Button>
          <span style={{
            fontSize: 11,
            color: msg.startsWith("✓") ? "var(--success)" : "var(--fg-muted)",
          }}>
            {msg || (status?.valid ? `目前狀態：已驗證 · ${maskApiKey(apiKey)}` : status?.error || "尚未驗證")}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---- Root App (protected) --------------------------------------------------
const DEFAULT_TWEAKS = {
  accent: "#0b7285",
  dark: false,
  density: 18,
  sansFamily: "Noto Sans TC",
  monoFamily: "JetBrains Mono",
  agentSwitcherPosition: "bottom",
  traceStyle: "collapsible",
};

function resolveInitialTweaks() {
  const globalTweaks =
    typeof window !== "undefined" ? window.ANILA_TWEAKS : null;
  return { ...DEFAULT_TWEAKS, ...(globalTweaks || {}) };
}

export default function App() {
  const { user } = useAuth();
  const [tweaks, setTweaks] = useState(resolveInitialTweaks);
  const [tweaksOpen, setTweaksOpen] = useState(false);

  useEffect(() => {
    applyTweaks(tweaks);
    if (typeof window !== "undefined") {
      window.ANILA_TWEAKS = tweaks;
    }
  }, [tweaks]);

  useEffect(() => {
    const onMessage = (e) => {
      if (e.data?.type === "__activate_edit_mode") setTweaksOpen(true);
      if (e.data?.type === "__deactivate_edit_mode") setTweaksOpen(false);
    };
    window.addEventListener("message", onMessage);
    try {
      window.parent?.postMessage({ type: "__edit_mode_available" }, "*");
    } catch {
      // ignore — not embedded
    }
    return () => window.removeEventListener("message", onMessage);
  }, []);

  return (
    <ChatRuntime
      user={user}
      tweaks={tweaks}
      setTweaks={setTweaks}
      tweaksOpen={tweaksOpen}
      setTweaksOpen={setTweaksOpen}
    />
  );
}
