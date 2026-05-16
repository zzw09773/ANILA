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

import { config, readCsrfCookie } from "./runtime/api.js";
import { useAuth, useLogoutRedirect } from "./runtime/auth.jsx";
import { streamChatCompletion } from "./runtime/sse.js";
import {
  appendClassifiedTag,
  computeConversationClassified,
  latchConversationWithMeta,
} from "./runtime/classified.js";
import {
  enqueueClassifyRetry,
  flushAll as flushClassifyRetries,
  installFocusFlush,
} from "./runtime/classifyRetryQueue.js";
import { buildPersistMeta } from "./runtime/messageMeta.js";
import { cleanGeneratedTitle } from "./runtime/titleClean.js";
import { relativeLabel } from "./runtime/time.js";
import {
  clearChunks as apiClearMemoryChunks,
  clearFacts as apiClearMemoryFacts,
  deleteFact as apiDeleteMemoryFact,
  listChunks as apiListMemoryChunks,
  listFacts as apiListMemoryFacts,
} from "./runtime/memory.js";
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
  classifyConversation as apiClassifyConversation,
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
  IconHistory,
  IconLock,
  IconMoon,
  IconNodes,
  IconRefresh,
  IconSettings,
  IconShare,
  IconShield,
  IconSpark,
  IconSun,
  IconTrash,
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

// Default starter — a single card that asks the Router itself to
// introduce ANILA AND list every agent available to this user. The
// Router already has the agent manifest in its system prompt so it can
// produce an accurate, up-to-date answer on first ask, and the user
// sees their real option set instead of hand-curated marketing cards.
function buildStarterPrompts(agents) {
  const real = (agents || []).filter((a) => a.id !== ROUTER_AGENT.id);
  const countLine = real.length > 0
    ? `你目前可以使用 ${real.length} 個 agent`
    : "平台目前尚未註冊 agent";
  return [
    {
      title: "ANILA 可以做什麼？",
      sub: `${countLine}，點一下讓 Router 介紹平台與各 agent 的能力`,
      q: "請介紹 ANILA 這個平台能做什麼，並列出我目前可用的每一個 agent 與它們各自能解決的問題。",
      primary: true,
    },
  ];
}

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

// Sprint 7 X follow-up：API key UI 已下線（cookie 流程後 SPA 不持有 key），
// 因此原本的 maskApiKey helper 也一併移除，避免 UI 仍假裝可以管理 key。

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
  // Sprint 7 X follow-up：SPA 完全不持有 API Key，認證統一走 httpOnly
  // session cookie + double-submit CSRF（見 runtime/sse.js）。原本為了
  // 過渡保留的 apiKey / apiKeyStatus / updateApiKey stub 已移除，避免
  // 使用者誤以為 settings 內可以管理 key。SDK / curl 仍可在 Authorization
  // header 帶 sk-* token，但 SPA 不再有 UI 入口。
  // ``isAuthenticated`` is referenced by 9 callsites below (auth-gated
  // effects + sendMessage / regenerateMessage / sendCompare / handleEditUser
  // early returns). Missing from this destructure → ReferenceError once the
  // first guard triggers, which crashes the whole App after login.
  const { authRequest, multipartRequest, isAuthenticated } = useAuth();
  const logoutAndRedirect = useLogoutRedirect();

  // --- agents / conversations / messages ---
  const [agents, setAgents] = useState([ROUTER_AGENT]);
  const [selectedAgentId, setSelectedAgentId] = useState(ROUTER_AGENT.id);
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [runtimeError, setRuntimeError] = useState("");
  // 上次抓 /v1/agents 的時間戳，給 focus-refresh 用做 15s 節流，
  // 避免使用者頻繁 alt-tab 把 CSP 打爆。CSP 端管理員刪了 agent
  // 後，下一次 ANILA UI 重新取得焦點時(且距離上一次抓超過 15s)
  // 會自動重抓清單。
  const lastAgentsRefreshAtRef = useRef(0);

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
  const isClassificationInherited = Boolean(selectedConv?.classificationInherited);
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

  // agents: load once the session is ready (cookie already attached)
  useEffect(() => {
    if (isAuthenticated) {
      void refreshAgents();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // Window-focus 重抓 agent 清單 — CSP 端管理員刪了 / approve 了 agent
  // 後，使用者切回 ANILA UI 時自動同步，不用 hard refresh。15 秒
  // 節流避免 alt-tab 連發 fetch。refreshAgents 自身會把
  // selectedAgentId 不在新清單時退回 ROUTER_AGENT (line ~387)，
  // 所以即便當下選的 agent 被刪了 UI 也能自我恢復。
  useEffect(() => {
    if (!isAuthenticated) return undefined;
    const handler = () => {
      const now = Date.now();
      if (now - lastAgentsRefreshAtRef.current < 15_000) return;
      lastAgentsRefreshAtRef.current = now;
      void refreshAgents();
    };
    window.addEventListener("focus", handler);
    return () => window.removeEventListener("focus", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // Sprint 8 X / Phase K — drain any pending classify retries on
  // window focus so a flaky network or a page that was backgrounded
  // mid-stream still ends up with the lock persisted to CSP.
  useEffect(() => {
    if (!isAuthenticated) return undefined;
    const sender = (numericId) => apiClassifyConversation(authRequest, numericId);
    void flushClassifyRetries(sender);
    return installFocusFlush(sender);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  async function refreshAgents() {
    setLoadingAgents(true);
    setRuntimeError("");
    // 在送出 fetch 的那一刻就標記時間戳 — 即使後續 await 還沒完成，
    // 也能擋掉緊接著的 focus 事件造成的重覆 fetch。
    lastAgentsRefreshAtRef.current = Date.now();
    try {
      // /v1/agents accepts the session cookie (Wave 1 caller dep) so the
      // same call works without the SPA holding an API Key.
      const res = await fetch(`${config.cspBaseUrl}/v1/agents`, {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
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
  //
  // Classification follows the latch invariant in runtime/classified.js:
  // ANY truthy signal (server-persisted classified + agent.requires_encryption)
  // wins. Without this OR, a hard refresh that races the agents fetch ahead
  // of the conversations fetch would silently drop encryption mode for
  // conversations whose classified=true wasn't (yet) persisted on the row.
  function mapServerConversation(serverRow, agentNameLookup, agentRequiresEncryptionLookup) {
    const agentName =
      serverRow.agent_id != null ? agentNameLookup(serverRow.agent_id) : null;
    const agentRequiresEncryption =
      serverRow.agent_id != null && agentRequiresEncryptionLookup
        ? Boolean(agentRequiresEncryptionLookup(serverRow.agent_id))
        : false;
    const classified = computeConversationClassified(
      { classified: serverRow.classified },
      { agentRequiresEncryption },
    );
    return {
      id: serverRow.id,
      title: serverRow.title,
      ts: relativeLabel(),
      updatedLabel: relativeLabel(),
      agent: agentName || null,
      agentId: serverRow.agent_id || null,
      agentName: agentName || null,
      folder: "all",
      tags: classified ? appendClassifiedTag([]) : [],
      starred: false,
      classified,
      // P3: distinguishes inheritance-driven latch from agent-required
      // or admin-set classification. Drives the warning banner copy
      // and the (lighter-weight) lock icon variant on the sidebar.
      classificationInherited: Boolean(serverRow.classification_inherited),
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
        const lookupName = (id) => agents.find((a) => a.id === id)?.name || null;
        const lookupRequiresEncryption = (id) =>
          Boolean(agents.find((a) => a.id === id)?.requiresEncryption);
        setConversations(
          rows.map((r) =>
            mapServerConversation(r, lookupName, lookupRequiresEncryption),
          ),
        );
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

  // Race recovery: conversations and agents fetch in parallel on login /
  // hard refresh. If conversations land first, their classified flag was
  // computed against an empty agents list and any agent-driven encryption
  // was silently lost. When agents finally arrive, walk the existing
  // conversations and re-apply the latch — never downgrades because
  // ``computeConversationClassified`` honours ``conversation.classified``
  // as a "prior" signal.
  useEffect(() => {
    if (!isAuthenticated) return;
    if (agents.length <= 1) return; // only ROUTER_AGENT loaded — nothing to upgrade against
    setConversations((prev) =>
      prev.map((c) => {
        const requires = agentRequiresEncryption(c.agentId);
        const next = computeConversationClassified(c, {
          agentRequiresEncryption: requires,
        });
        if (next === Boolean(c.classified)) return c;
        return {
          ...c,
          classified: next,
          tags: appendClassifiedTag(c.tags),
        };
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, isAuthenticated]);

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
    if (!isAuthenticated) return;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const systemPrompt =
      "你是對話標題產生器。閱讀以下 Q&A，回覆一個不超過 15 個繁體中文字的標題，" +
      "只能輸出標題本身，不要加引號、冒號、標點或其他說明。";
    const userPrompt = `使用者：${userText}\n助理：${assistantText}`;
    try {
      const csrf = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf[1]) } : {}),
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
      // Reject outputs that echo a Router fallback placeholder etc. When the
      // generator produces garbage, keep the user-text truncation rather
      // than replacing a readable title with a useless one.
      const cleaned = cleanGeneratedTitle(raw);
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
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
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
    // See comment on sendMessage — stale closure on messagesByConv forces
    // us to accumulate trace / reasoning locally for the persist call.
    const accumulatedTrace = [];
    let accumulatedReasoning = "";
    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onTrace: (step) => {
          accumulatedTrace.push(step);
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
          accumulatedReasoning += delta;
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
        const persistMeta = buildPersistMeta(finalMeta, {
          trace: accumulatedTrace,
          reasoning: accumulatedReasoning,
        });
        try {
          const saved = await apiAppendMessage(authRequest, convId, {
            role: "assistant",
            content: finalText,
            traceId: finalMeta?.trace_id,
            latencyMs: finalMeta?.latency_ms,
            agentName: agentNameForPersist,
            metadata: persistMeta,
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
    //
    // Persistence:
    //   * If convId is already a numeric server id → POST /classify
    //     immediately. Failures land in the retry queue (page focus /
    //     next user message will re-send).
    //   * If convId is still a client temp string id (the brief
    //     window between optimistic create and the server reply) →
    //     queue with numericId=null so resolveTempId can replay it
    //     once ensureConversation hands us the real id.
    if (meta.classified === true) {
      setConversations((prev) => {
        const prior = prev.find((c) => c.id === convId);
        if (prior && !prior.classified) {
          if (typeof convId === "number") {
            apiClassifyConversation(authRequest, convId).catch((err) => {
              // eslint-disable-next-line no-console
              console.error("[classified-latch] persistence failed", err);
              enqueueClassifyRetry(convId, { numericId: convId });
            });
          } else {
            enqueueClassifyRetry(convId, { numericId: null });
          }
        }
        return prev.map((c) =>
          c.id === convId ? latchConversationWithMeta(c, meta) : c,
        );
      });
    }
  }

  // ---- send single ----
  async function sendMessage(text, attachments = [], meta = {}) {
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
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
    // Keep trace / reasoning accumulators as plain locals so we are NOT at
    // the mercy of React's stale-closure semantics when persisting below.
    // `messagesByConv` captured by this function is frozen at the render
    // that dispatched sendMessage — reading it after streaming always
    // yields empty trace / reasoning even though setState visibly updated
    // the UI. The locals here collect the same deltas in lockstep and
    // feed buildPersistMeta with the live values.
    const accumulatedTrace = [];
    let accumulatedReasoning = "";
    try {
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onTrace: (step) => {
          accumulatedTrace.push(step);
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
          accumulatedReasoning += delta;
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
        const persistMeta = buildPersistMeta(finalMeta, {
          trace: accumulatedTrace,
          reasoning: accumulatedReasoning,
        });
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
            metadata: persistMeta,
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
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
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
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
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
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
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
            payload: {
              model: col.agentId,
              messages: [{ role: "user", content: buildUserContent(text, attachments) }],
            },
            conversationId: typeof col.id === "number" ? col.id : undefined,
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
      {isClassificationInherited && (
        <div style={{
          position: "fixed", top: 0, left: 0, right: 0,
          padding: "8px 16px", zIndex: 200,
          background: "var(--warning-bg, oklch(0.45 0.15 50 / 0.92))",
          color: "var(--warning-fg, oklch(0.99 0.005 80))",
          fontSize: 12, fontWeight: 500,
          display: "flex", alignItems: "center", gap: 8,
          borderBottom: "1px solid oklch(0.30 0.10 50 / 0.4)",
        }}>
          <IconLock size={14} />
          <span>
            此對話因引用過往加密記憶而升級為機密。
            刪除對話的加密記憶引用可解除（設定 → 記憶）；
            一旦升級無法在此對話手動退回。
          </span>
        </div>
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

          {/* Sprint 7 X follow-up：API Key dropdown 已下線。SPA 用 cookie
              流程，使用者沒有也不該管理 key；SDK 用戶仍可從 control
              plane 取得 sk-* 並用 Authorization header 呼叫。 */}

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
                        agents={agents}
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
                      <span
                        style={{ color: "var(--fg-muted)" }}
                        title="本系統由大型語言模型(LLM)驅動,輸出內容可能包含錯誤或偏誤,僅供參考、不可作為唯一決策依據。完整 AI 政策見 docs/governance/ai-policy.md。"
                      >
                        AI 系統 · 內容僅供參考
                      </span>
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
        agents={agents}
        authRequest={authRequest}
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
function EmptyState({ agent, agents, onPick, loading }) {
  const prompts = buildStarterPrompts(agents);
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
        gridTemplateColumns: prompts.length === 1 ? "1fr" : "1fr 1fr",
        gap: 10,
        maxWidth: 560, margin: "36px auto 0", textAlign: "left",
      }}>
        {prompts.map((s, i) => {
          const isPrimary = s.primary === true;
          return (
            <button key={i} onClick={() => onPick(s.q)} style={{
              padding: isPrimary ? "16px 18px" : "12px 14px",
              background: isPrimary ? "var(--accent-soft, var(--bg-elev))" : "var(--bg-elev)",
              border: "1px solid " + (isPrimary ? "var(--accent, var(--border-strong))" : "var(--border)"),
              borderRadius: "var(--radius)",
              cursor: "pointer", textAlign: "left",
              transition: "all .12s",
              fontFamily: "inherit",
              color: "var(--fg)",
            }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "var(--border-strong)";
                e.currentTarget.style.transform = "translateY(-1px)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = isPrimary
                  ? "var(--accent, var(--border-strong))"
                  : "var(--border)";
                e.currentTarget.style.transform = "";
              }}>
              <div style={{
                fontSize: isPrimary ? 15 : 13,
                fontWeight: 600,
                color: "var(--fg)",
              }}>{s.title}</div>
              <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 4 }}>{s.sub}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Sprint 7 X follow-up：ApiKeyPopover 元件已移除（cookie 流程後完全 dead code）。

// ---- Settings modal --------------------------------------------------------
// Settings → 記憶 tab. Lives in SettingsModal but factored out
// because it owns its own data-loading lifecycle (facts + chunks).
//
// MVP scope (P2):
//   - List user_facts; per-row delete; clear-all-facts
//   - List recent chunks (preview only); clear-all-chunks
//   - Surface encrypted-source markers (P3 will inherit them)
// Out of scope until we see real demand:
//   - Inline edit of fact value (delete-and-let-LLM-re-extract is fine)
//   - Per-chunk delete (cascade via conversation delete is fine)
//   - Search / filter (volume is small)
function MemoryTab({ authRequest }) {
  const [factsState, setFactsState] = useState({ loading: true, error: null, facts: [], total: 0 });
  const [chunksState, setChunksState] = useState({
    loading: true, error: null, items: [],
    total: 0, encrypted_total: 0, distinct_conversations: 0,
  });

  const reload = useCallback(async () => {
    setFactsState((s) => ({ ...s, loading: true, error: null }));
    setChunksState((s) => ({ ...s, loading: true, error: null }));
    try {
      const [facts, chunks] = await Promise.all([
        apiListMemoryFacts(authRequest),
        apiListMemoryChunks(authRequest, { limit: 25 }),
      ]);
      setFactsState({
        loading: false, error: null,
        facts: facts.facts || [], total: facts.total || 0,
      });
      setChunksState({
        loading: false, error: null,
        items: chunks.items || [],
        total: chunks.total || 0,
        encrypted_total: chunks.encrypted_total || 0,
        distinct_conversations: chunks.distinct_conversations || 0,
      });
    } catch (err) {
      const msg = err?.message || "載入失敗";
      setFactsState((s) => ({ ...s, loading: false, error: msg }));
      setChunksState((s) => ({ ...s, loading: false, error: msg }));
    }
  }, [authRequest]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onDeleteFact = async (id, key) => {
    if (!window.confirm(`刪除事實「${key}」？此動作無法復原。`)) return;
    try {
      await apiDeleteMemoryFact(authRequest, id);
      await reload();
    } catch (err) {
      window.alert(err?.message || "刪除失敗");
    }
  };

  const onClearFacts = async () => {
    if (factsState.total === 0) return;
    if (!window.confirm(`清空全部 ${factsState.total} 筆事實？此動作無法復原。`)) return;
    try {
      await apiClearMemoryFacts(authRequest);
      await reload();
    } catch (err) {
      window.alert(err?.message || "清空失敗");
    }
  };

  const onClearChunks = async () => {
    if (chunksState.total === 0) return;
    if (!window.confirm(
      `清空全部 ${chunksState.total} 段對話片段？\n` +
      `這會抹除跨對話語意檢索的記憶（已記住的事實不受影響）。\n` +
      `此動作無法復原。`
    )) return;
    try {
      await apiClearMemoryChunks(authRequest);
      await reload();
    } catch (err) {
      window.alert(err?.message || "清空失敗");
    }
  };

  return (
    <div style={{ display: "grid", gap: 18, fontSize: 13 }}>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
        平台會在每輪對話後，把可能對你長期有用的事實萃取為 key/value 存起來，
        並把訊息向量化以便跨對話語意檢索。下次任何對話都會自動帶入相關記憶。
        所有資料只屬於你個人，不與其他使用者共享。
      </div>

      {/* ── Facts ──────────────────────────────────────────────────────── */}
      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <div style={{ fontWeight: 500 }}>
            已記住的事實 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>· {factsState.total}</span>
          </div>
          <button
            disabled={factsState.total === 0 || factsState.loading}
            onClick={onClearFacts}
            style={{
              fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius)",
              background: "transparent", border: "1px solid var(--border)",
              color: factsState.total === 0 ? "var(--fg-subtle)" : "var(--danger)",
              cursor: factsState.total === 0 ? "default" : "pointer",
            }}
          >清空全部</button>
        </div>
        {factsState.loading && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>載入中…</div>
        )}
        {factsState.error && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>{factsState.error}</div>
        )}
        {!factsState.loading && !factsState.error && factsState.facts.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
            目前還沒有萃取到任何事實。和 ANILA 多聊聊「我是誰、我喜歡什麼」之類的訊息，平台會自動學習。
          </div>
        )}
        {!factsState.loading && factsState.facts.length > 0 && (
          <div style={{ display: "grid", gap: 6 }}>
            {factsState.facts.map((f) => (
              <div key={f.id} style={{
                display: "grid",
                gridTemplateColumns: "minmax(110px, 1fr) 2fr auto auto",
                gap: 10, alignItems: "center",
                padding: "6px 8px",
                background: "var(--bg-subtle)",
                borderRadius: "var(--radius)",
                fontSize: 12,
              }}>
                <div style={{ fontFamily: "var(--font-mono)", color: "var(--fg-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.key}
                </div>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.value}
                </div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--fg-subtle)" }}>
                  {(f.confidence * 100).toFixed(0)}%
                </div>
                <button
                  onClick={() => onDeleteFact(f.id, f.key)}
                  title="刪除這筆事實"
                  style={{
                    width: 22, height: 22, padding: 0,
                    background: "transparent", border: "none",
                    color: "var(--fg-subtle)", cursor: "pointer",
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.color = "var(--danger)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.color = "var(--fg-subtle)"; }}
                >
                  <IconTrash size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Chunks ─────────────────────────────────────────────────────── */}
      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <div style={{ fontWeight: 500 }}>
            對話片段索引 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>
              · {chunksState.total} 段 / {chunksState.distinct_conversations} 個對話
              {chunksState.encrypted_total > 0 && (
                <span style={{ marginLeft: 8, color: "var(--warning, var(--accent))" }}>
                  · {chunksState.encrypted_total} 段加密來源
                </span>
              )}
            </span>
          </div>
          <button
            disabled={chunksState.total === 0 || chunksState.loading}
            onClick={onClearChunks}
            style={{
              fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius)",
              background: "transparent", border: "1px solid var(--border)",
              color: chunksState.total === 0 ? "var(--fg-subtle)" : "var(--danger)",
              cursor: chunksState.total === 0 ? "default" : "pointer",
            }}
          >清空全部</button>
        </div>
        {chunksState.loading && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>載入中…</div>
        )}
        {chunksState.error && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>{chunksState.error}</div>
        )}
        {!chunksState.loading && !chunksState.error && chunksState.items.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
            目前還沒有對話片段索引。對話幾輪之後再回來看。
          </div>
        )}
        {!chunksState.loading && chunksState.items.length > 0 && (
          <div style={{ display: "grid", gap: 4, maxHeight: 240, overflowY: "auto" }}>
            {chunksState.items.map((c) => (
              <div key={c.id} style={{
                fontSize: 11, padding: "4px 6px",
                fontFamily: "var(--font-mono)",
                color: c.is_encrypted ? "var(--fg)" : "var(--fg-muted)",
              }}>
                <span style={{
                  display: "inline-block", minWidth: 70,
                  color: "var(--fg-subtle)",
                }}>
                  {c.role === "user" ? "user" : "asst"} · #{c.conversation_id}
                </span>
                {c.is_encrypted && <span style={{ marginRight: 4 }}>🔒</span>}
                <span style={{ color: "var(--fg)" }}>{c.content}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ fontSize: 10, color: "var(--fg-subtle)", lineHeight: 1.6 }}>
        清空後立即生效；下次對話起，平台會重新從新對話內容重新學習。
        若需暫時停用記憶整合，請聯絡管理員（runtime feature flag 由運維端控制）。
      </div>
    </div>
  );
}

function SettingsModal({
  open, tab, setTab, onClose,
  user, agents, authRequest,
}) {
  return (
    <Modal open={open} onClose={onClose} title="設定" subtitle="runtime 偏好與帳號" width={680}>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 20 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {[
            { id: "general", label: "一般",       icon: <IconSettings size={13} /> },
            { id: "privacy", label: "隱私 / 信任", icon: <IconShield   size={13} /> },
            { id: "memory",  label: "記憶",        icon: <IconHistory  size={13} /> },
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

          {/* Sprint 7 X follow-up：apikey tab 已下線 — SPA 不再持有 key。 */}

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

          {tab === "memory" && (
            <MemoryTab authRequest={authRequest} />
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

// Sprint 7 X follow-up：ApiKeyTab 元件已移除（cookie 流程後 dead code）。

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
