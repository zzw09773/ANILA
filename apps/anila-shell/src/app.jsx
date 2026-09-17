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
import { createTaskForConversation } from "./runtime/tasks.js";
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
import {
  buildPersistMeta,
  resolveAgentNameForPersist,
  resolveAnsweringAgentId,
} from "./runtime/messageMeta.js";
import {
  AGENT_REPLY_OBSERVATION_KEY,
  agentReplyNotice,
} from "./runtime/agentReplySignal.js";
import { cleanGeneratedTitle } from "./runtime/titleClean.js";
import { resolveEditResend } from "./runtime/editResend.js";
import { relativeLabel } from "./runtime/time.js";
import { MemoryTab } from "./memory.jsx";
import {
  listConversations as apiListConversations,
  listRouterModels as apiListRouterModels,
  setConversationRouterModel as apiSetConversationRouterModel,
  setConversationThinking as apiSetConversationThinking,
  createConversation as apiCreateConversation,
  adoptConversation as apiAdoptConversation,
  getConversation as apiGetConversation,
  updateConversationTitle as apiUpdateConversationTitle,
  updateConversation as apiUpdateConversation,
  deleteConversation as apiDeleteConversation,
  appendMessage as apiAppendMessage,
  startTurn as apiStartTurn,
  branchTurn as apiBranchTurn,
  updateMessage as apiUpdateMessage,
  rateMessage as apiRateMessage,
  branchMessage as apiBranchMessage,
  setActiveLeaf as apiSetActiveLeaf,
  deleteMessageBranch as apiDeleteMessageBranch,
  classifyConversation as apiClassifyConversation,
  createShare as apiCreateShare,
  listShares as apiListShares,
  revokeShare as apiRevokeShare,
  uploadAttachment as apiUploadAttachment,
  bindAttachments as apiBindAttachments,
  getAttachmentMeta as apiGetAttachmentMeta,
  createHandoff as apiCreateHandoff,
  listAgentFunctions as apiListAgentFunctions,
  getUiSettings,
  putUiSettings,
  searchConversations,
  listActiveBanners as apiListActiveBanners,
  getConversationUsage as apiGetConversationUsage,
  setConversationCompact as apiSetConversationCompact,
  clearConversationCompact as apiClearConversationCompact,
  requestConversationCompact as apiRequestConversationCompact,
} from "./runtime/conversations.js";
import {
  COMPACT_SUMMARY_PREFIX,
  compactBoundaryOnPath,
  compactFieldsFromServer,
  compactPutFailureState,
  compactStateFromConv,
  keptMessageCount,
  readPendingCompact,
  resolveBoundaryDbId,
  resolveKeptBoundary,
  writePendingCompact,
} from "./runtime/compact.js";
import { CompactBoundaryBanner } from "./compactBoundary.jsx";
import { CONV_USAGE_DEBOUNCE_MS } from "./runtime/usageDisplay.js";
import { ConversationUsageChip, UsagePage } from "./usage.jsx";
import { promoteAdoptedAnswer } from "./runtime/adoptCompare.js";
import { mapServerAttachments } from "./runtime/messageAttachments.js";
import {
  applyServerPath,
  persistRegeneratedAssistant,
  reconcilePersistedAssistant,
  runRegenerateStreamPhase,
  sanitizeRestoredMessages,
  switchBranch as switchBranchPath,
} from "./runtime/messageTree.js";
import {
  listVisibleActions,
  runActionInvokeFillback,
} from "./runtime/messageActions.js";
import {
  ANSWER_PERSIST_FAILURE_NOTICE,
  STREAM_STATE,
  createTurnChain,
  finalizeStreamedAssistant,
  historyBefore,
  makeStreamWriter,
  persistTurnHead,
  readStreamState,
  streamStateNotice,
  isLengthBudgetError,
  isHarnessEmptyNotice,
  lengthBudgetNotice,
} from "./runtime/reservedTurn.js";

import RouterModelPicker from "./components/RouterModelPicker.jsx";
import ThinkingPicker from "./components/ThinkingPicker.jsx";
import {
  conversationSelectionFromServer,
  normalizeThinkingTier,
  outgoingThinkingApplied,
  persistThinkingTierPreference,
  readStoredThinkingTier,
  shouldReplayOneShotDeep,
} from "./runtime/thinkingTier.js";
import { persistFieldsFromSaved } from "./runtime/reasoningPersist.js";
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
import { useConfirm, useToast } from "./confirm.jsx";
import { AnilaLogoImg, AnilaLogoVideo } from "./AnilaBrand.jsx";
import {
  IconColumns,
  IconHistory,
  IconLock,
  IconMoon,
  IconNodes,
  IconSettings,
  IconShare,
  IconShield,
  IconSpark,
  IconGift,
  IconSun,
  IconUser,
} from "./icons.jsx";
import { BUILTIN_FOLDER_IDS, DEFAULT_FOLDERS, blockingHits, detectPII, summarizePIIHits } from "./data.jsx";
import {
  CitationsDrawer,
  ConfidentialWatermark,
  ClassificationLevelBadge,
  watermarkLevel,
  watermarkReaderLabel,
  WATERMARK_DISCLAIMER,
  REDACTION_MODES,
  REDACTION_MODE_DEFAULT,
} from "./trust.jsx";
import { ParallelCompareView } from "./multiagent.jsx";
import { HandoffMenu, ShareDialog } from "./collab.jsx";
import { TweaksPanel } from "./tweaks.jsx";
import { ChangelogModal, CHANGELOG_VERSION } from "./changelog.jsx";
import { BannerBar } from "./banners.jsx";
import { ServicesPanel } from "./services.jsx";
import { ANILA_LM_ENTRY_ENABLED } from "./anilalmReleaseGate.js";
import { originHref } from "./shellNav.jsx";
import { classifiedShareDenial, handoffNotice } from "./uxCopy.js";
import { ArtifactPanel } from "./artifact.jsx";
import { ArtifactPreviewProvider } from "./artifactContext.jsx";

// ---- Router pseudo-agent ----------------------------------------------------
const ROUTER_AGENT = Object.freeze({
  id: "anila-router",
  name: "ANILA 自動選助手",
  short: "auto",
  description: "ANILA 會幫你找合適的助手，也可能直接回答你。",
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
      sub: `${countLine}，點一下讓 ANILA 介紹平台與各助手的能力`,
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
/**
 * 從一個「要送出去的 payload」裡取出**這一輪使用者新講的那段話**。
 *
 * 敏感資訊閘門看的就是這一段:不是整串歷史(那會讓任何一個曾經出現過個資的
 * 對話再也不能重試),而是使用者這次提交的內容。
 *
 * 刻意只認 payload 本身,不讓呼叫端多傳任何東西 —— 呼叫端一旦能選擇「傳不傳」,
 * 這個閘門就退化回一份要大家記得去呼叫的清單,而那份清單已經漏掉兩次了。
 */
function outgoingUserText(payload) {
  const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i];
    if (!m || m.role !== "user") continue;
    const c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      return c
        .filter((p) => p && p.type === "text")
        .map((p) => p.text || "")
        .join("\n");
    }
    return "";
  }
  return "";
}

/**
 * 組出送給 Router 的 OpenAI messages。
 *
 * 刻意不走這函式（不帶對話歷史）的呼叫端：
 * 1. generateConversationTitle — 標題產生器，自帶 system＋單則 Q&A
 * 2. buildDeclarativeActionMessages / runActionInvokeFillback — 宣告式 custom action，只有渲染後的 prompt
 * 3. sendCompare — 比較／direct 並排，每欄只送當則 user
 *
 * CSP `POST /api/conversations/{id}/messages`（append_message）的 role pattern
 * 含 tool，且會原樣持久化。這裡把 persisted `role:"tool"` 轉成帶
 * `tool_result` block 的 user 訊息；Router `_split_turns` 會把它併進前一回合，
 * 不新開回合、不影響 `kept_from_index`。
 */
function buildMessageHistory(priorMsgs, currentText, currentAttachments, options = null) {
  const out = [];
  const sources = [];
  for (const m of priorMsgs || []) {
    if (!m || m.streaming) continue;
    if (m.role === "user") {
      out.push({ role: "user", content: buildUserContent(m.text || "", m.attachments || []) });
      sources.push(m);
    } else if (m.role === "assistant" && m.text) {
      out.push({ role: "assistant", content: m.text });
      sources.push(m);
    } else if (m.role === "tool") {
      const toolUseId = m.toolCallId || m.tool_call_id || (
        typeof m.dbId === "number" ? String(m.dbId) : String(m.id ?? "")
      );
      out.push({
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: toolUseId,
          content: m.text || "",
        }],
      });
      sources.push(m);
    }
  }
  if (currentText !== null && currentText !== undefined) {
    out.push({ role: "user", content: buildUserContent(currentText, currentAttachments) });
    sources.push(options?.currentUserMsg || null);
  }
  const compact = options?.compact;
  const boundaryId = compact?.boundaryMessageId;
  if (
    compact?.summary
    && boundaryId != null
    && sources.some((m) => m && m.dbId === boundaryId)
  ) {
    const idx = sources.findIndex((m) => m && m.dbId === boundaryId);
    const kept = out.slice(idx);
    const keptSources = sources.slice(idx);
    const messages = [
      { role: "system", content: `${COMPACT_SUMMARY_PREFIX}${compact.summary}` },
      ...kept,
    ];
    options?.onBuilt?.({ messages, sources: keptSources });
    return messages;
  }
  options?.onBuilt?.({ messages: out, sources });
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

/** reference_id values from composer chips (or AttachmentOut-shaped rows). */
function attachmentBindIds(attachments) {
  const ids = [];
  const seen = new Set();
  for (const a of attachments || []) {
    if (!a || typeof a !== "object") continue;
    const rid = a.referenceId || a.reference_id;
    const fallback = typeof a.id === "string" ? a.id : null;
    const value = typeof rid === "string" && rid.trim() ? rid.trim() : fallback;
    if (typeof value !== "string" || !value.trim()) continue;
    const key = value.trim();
    if (seen.has(key)) continue;
    seen.add(key);
    ids.push(key);
  }
  return ids;
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

/**
 * 院內規章檢索的三個欄位,從 `anila_meta` 抄到 UI 的訊息列上。
 *
 * meta 有**兩個**入口,而且兩個都必須抄:重新載入走 `mapServerMessage`、
 * SSE 現場走 `applyMeta`。只接一邊不會有任何錯誤訊息,只會讓同一則答案在
 * 串完的當下說「依據是人事管理規則」、重新整理之後說不出話來(或反過來)。
 * 抽成一個函式而不是在兩處各寫一次,是為了讓「兩邊講的是同一件事」變成
 * 結構上的事實,而不是靠人記得同步兩份欄位清單。
 *
 * ⚠ **狀態只從 `kb_state` 抄,不從資料反推。** 生產端的事實
 * (`services/csp/app/services/institutional_kb.py:226-233`):`partial_error`
 * **一定帶著命中**,`search_error` 有失敗的庫而沒有命中。所以「有 kb_hits
 * 就當命中」會把 `partial_error` 講成乾淨的命中,使用者就永遠看不到
 * 「這不是全部的依據」——那正是他最需要知道的一句話。
 *
 * `citations` 不在這裡:兩個縫本來就各自映射了那個欄位,CSP 命中時是把
 * 規章**前綴**進既有的 citations(不覆蓋下游來源),沿用原本的管線即可。
 *
 * @param {object | null | undefined} meta - 一份 `anila_meta`
 */
export function kbMetaFields(meta) {
  const m = meta && typeof meta === "object" ? meta : {};
  return {
    // 缺席就是缺席:這個功能上線前存下來的訊息沒有這個欄位,而
    // `undefined` 仍在畫面上保持安靜;明帶的 `"not_searched"` 則由 chat.jsx
    // 揭露「本次未檢索院內規章」,兩者不可混為一談。
    kbState: typeof m.kb_state === "string" ? m.kb_state : undefined,
    kbHits: Array.isArray(m.kb_hits) ? m.kb_hits : [],
    kbFailedCollections: Array.isArray(m.kb_failed_collections)
      ? m.kb_failed_collections
      : [],
  };
}

/**
 * The registered-agent observation has the same two ingress seams as KB meta:
 * server-message reload and live SSE.  Unknown or absent observations remain
 * silent so old messages do not acquire a new claim.
 */
export function agentReplyMetaFields(meta) {
  const m = meta && typeof meta === "object" ? meta : {};
  return {
    agentReplyObservation: m[AGENT_REPLY_OBSERVATION_KEY],
    agentReplyNotice: agentReplyNotice(m),
  };
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
// Exported so tests can mount the real send path. The behavioural suite for
// reserve-then-stream drives THIS component — a previous round's tests only
// grepped the source text and stayed green while the fix was deleted.
export function ChatRuntime({ user, tweaks, setTweaks, tweaksOpen, setTweaksOpen }) {
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
  const confirm = useConfirm();
  const toast = useToast();

  // --- agents / conversations / messages ---
  const [agents, setAgents] = useState([ROUTER_AGENT]);
  const [selectedAgentId, setSelectedAgentId] = useState(ROUTER_AGENT.id);
  const [routerModels, setRouterModels] = useState([]);
  const [routerDefaultId, setRouterDefaultId] = useState(null);
  const [selectedRouterModelId, setSelectedRouterModelId] = useState(null);
  const [routerModelError, setRouterModelError] = useState("");
  const [routerPickerLocked, setRouterPickerLocked] = useState(false);
  const [thinkingTier, setThinkingTier] = useState(() => readStoredThinkingTier());
  const [thinkingError, setThinkingError] = useState("");
  const [deepThinkNext, setDeepThinkNext] = useState(false);
  const deepThinkNextRef = useRef(false);
  const [conversations, setConversations] = useState([]);
  const [selectedConvId, setSelectedConvId] = useState(null);

  const authRequestRef = useRef(authRequest);
  authRequestRef.current = authRequest;

  useEffect(() => {
    if (!isAuthenticated) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const data = await apiListRouterModels(authRequestRef.current);
        if (cancelled) return;
        const models = data?.models || [];
        setRouterModels(models);
        setRouterDefaultId(data?.default_model_id ?? null);
        setSelectedRouterModelId((current) => {
          if (current && models.some((m) => m.id === current)) return current;
          return data?.default_model_id ?? models[0]?.id ?? null;
        });
        setRouterModelError("");
      } catch (err) {
        if (!cancelled) setRouterModelError(err?.message || "無法載入對話模型");
      }
    })();
    return () => { cancelled = true; };
  }, [isAuthenticated]);

  useEffect(() => {
    if (!selectedConvId) {
      setThinkingTier(readStoredThinkingTier());
      return;
    }
    const conv = conversations.find((c) => c.id === selectedConvId);
    if (!conv) return;
    if (typeof conv.routerModelId === "number") {
      setSelectedRouterModelId(conv.routerModelId);
    }
    setThinkingTier(normalizeThinkingTier(conv.thinkingTier));
  }, [selectedConvId, conversations]);

  const selectedRouterModelName = useMemo(() => {
    const row = routerModels.find((m) => m.id === selectedRouterModelId);
    return row?.name || null;
  }, [routerModels, selectedRouterModelId]);
  const selectedThinkingModel = useMemo(
    () => routerModels.find((m) => m.id === selectedRouterModelId) || null,
    [routerModels, selectedRouterModelId],
  );
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [runtimeError, setRuntimeError] = useState("");
  // 上次抓 /v1/agents 的時間戳，給 focus-refresh 用做 15s 節流，
  // 避免使用者頻繁 alt-tab 把 CSP 打爆。CSP 端管理員刪了 agent
  // 後，下一次 ANILA UI 重新取得焦點時(且距離上一次抓超過 15s)
  // 會自動重抓清單。
  const lastAgentsRefreshAtRef = useRef(0);

  const [messagesByConvState, setMessagesByConvState] = useState({});
  // 串流階段是「之後」才跑的,它要的是即時的訊息清單。讀 render 當下捕捉到
  // 的 state 一定是舊的 —— 上一次嘗試就是在這裡把排隊的每一輪都送成零上下文,
  // 而 442 個測試沒有一個抓得到。鏡像寫在 updater 裡面(不是 useEffect),
  // 所以它與 state 完全同步,不會落後一個 commit。
  const messagesRef = useRef({});
  const conversationsRef = useRef([]);
  conversationsRef.current = conversations;
  const lastHistoryRef = useRef(new Map());
  const pendingCompactRef = useRef(new Map());
  const compactAppliedRef = useRef(new Set());
  const compactPersistInFlightRef = useRef(new Map());
  const [compacting, setCompacting] = useState(false);
  const messagesByConv = messagesByConvState;
  const setMessagesByConv = useCallback((update) => {
    setMessagesByConvState((prev) => {
      const next = typeof update === "function" ? update(prev) : update;
      messagesRef.current = next;
      return next;
    });
  }, []);
  // Conversations this tab created itself. They start empty on the server and
  // this client is their only author, so hydrating them can only lose the
  // turn currently being sent — see the hydrate effect below.
  const locallyCreatedConvIdsRef = useRef(new Set());
  // Parallel composer uploads on a new chat share one in-flight create so
  // two files do not mint two conversations (and re-orphan one of them).
  const creatingConversationRef = useRef(null);


  // --- compare mode ---
  const [compareMode, setCompareMode] = useState(false);
  const [compareColumns, setCompareColumns] = useState([]);
  const [compareMsgs, setCompareMsgs] = useState({});

  // --- UI state ---
  const [citationsOpen, setCitationsOpen] = useState(false);
  const [activeCitations, setActiveCitations] = useState([]);
  const [activeCitationId, setActiveCitationId] = useState(null);
  // 右側靜態產物預覽（md／html／svg）；僅使用者按「預覽」才開，不自動彈出。
  const [artifact, setArtifact] = useState(null);
  const [pendingArtifactRevision, setPendingArtifactRevision] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState("general");
  const [shareOpen, setShareOpen] = useState(false);
  // 專案入口（Service Platform）overlay。
  const [servicesOpen, setServicesOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  const [convUsage, setConvUsage] = useState(null);
  const [convUsageTick, setConvUsageTick] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  // 窄視窗（≤900px）自動收合側欄，視窗變寬再展開；使用者手動切換照常。
  // 沒有這條時，400px 寬的視窗會被 272px 的側欄吃掉，主欄擠成直排字
  // （UI 評估 2026-09-02 第 1 件）。jsdom 沒有 matchMedia → 維持展開。
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mql = window.matchMedia("(max-width: 900px)");
    if (!mql) return undefined;
    const apply = (e) => setCollapsed(Boolean(e.matches));
    apply(mql);
    if (typeof mql.addEventListener === "function") mql.addEventListener("change", apply);
    else if (typeof mql.addListener === "function") mql.addListener(apply);
    return () => {
      if (typeof mql.removeEventListener === "function") mql.removeEventListener("change", apply);
      else if (typeof mql.removeListener === "function") mql.removeListener(apply);
    };
  }, []);
  const [folder, setFolder] = useState("all");

  // 敏感資訊模式(提醒／阻擋)。使用者自己在提示列選的偏好,跟資料夾
  // 共用 users.ui_settings 這個 per-user blob。
  //
  // ⚠ 存後端而不是 localStorage,理由和資料夾同一條(見下面那段註解),但對這一
  // 項更要緊:卡登共用工作站上,localStorage 會把前一個人的選擇留給下一個人 ——
  // 那不是「使用者的選擇」,而是別人的。blob 跟著卡走才對得起「這是你的偏好」。
  //
  // ⚠ 這**不是**管理員政策。後端沒有這種欄位,這裡也刻意不做成那樣:要不要有
  // 一個管理員層級的強制政策,是還沒裁決的產品問題(OWNER-QUESTIONS)。
  // 這裡存的只是使用者自己的選擇。
  const [redactionMode, setRedactionMode] = useState(REDACTION_MODE_DEFAULT);

  /**
   * 敏感資訊閘門本體。**唯一**判斷「這段文字准不准離開瀏覽器」的地方。
   *
   * 它自己不知道也不在乎是誰要送 —— 呼叫它的是下面兩個扼流點,而不是各個
   * 發起點。這個形狀是被逼出來的:上一版是一個「呼叫端要記得呼叫」的函式,
   * 兩輪驗收就找出兩批漏掉的路徑(範本 autosend;編輯重問 + 引導式重試 +
   * 建議提示),而且每一次的徵狀都一樣 —— 畫面說擋住了,東西照樣送出去。
   * 會漏第三次的清單不是防線。
   *
   * @returns {boolean} 准不准送。
   */
  const passesRedactionGate = (text) => {
    if (redactionMode !== "block") return true;
    // ⚠ 擋不擋只看 `blockingHits`。憑證(family "credential")會在提示列上
    // 出現,但不在 BLOCKING_FAMILIES 裡 —— 它永遠不會讓訊息送不出去。
    // 那條白名單是這件事的結構(data.jsx),不是這裡的一句約定。
    const blocking = blockingHits(detectPII(text || ""));
    if (blocking.length === 0) return true;
    // ⚠ 這句話必須說出**擋的是什麼**,而且必須給一條**當下真的走得到**的出路。
    // 它曾經只寫「可在上方提示列切換模式」,而提示列只在輸入框裡剛好有個資時
    // 才存在 —— 從範本或重試被擋下來的人,畫面上根本沒有那條提示列,等於被鎖
    // 在外面沒有鑰匙。設定 →「隱私 / 信任」那組按鈕是永遠都在的那一條,
    // 所以指向它。
    toast(
      `這則訊息裡疑似有 ${summarizePIIHits(blocking)}（只比對格式，可能認錯）。目前模式是 block，所以沒有送出 —— 這個模式是你自己選的，可到「設定 → 隱私 / 信任」改。`,
      { tone: "error" },
    );
    return false;
  };

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

  // Server-synced settings:後端是 source of truth(共用工作站下使用者的資料夾
  // 不會殘留在瀏覽器給下一個人看到)。掛載時抓後端覆寫;之後變動 debounce 存回。
  // localStorage 仍寫(離線/載入前的暫存),但後端值優先。
  const uiSettingsLoadedRef = useRef(false);
  useEffect(() => {
    if (!isAuthenticated) return;
    let alive = true;
    getUiSettings(authRequest)
      .then((res) => {
        const s = res?.ui_settings || {};
        if (alive && Array.isArray(s.folders) && s.folders.length > 0) {
          setFolders(s.folders.filter((f) => f && typeof f.id === "string" && typeof f.name === "string"));
        }
        // 白名單驗證:blob 是使用者可寫的,不明值一律退回預設,不要拿它去比對模式。
        //
        // ⚠ 這裡也是**舊值的退場口**。曾經有第三個模式,使用者的 blob 裡可能還
        // 存著它。那不是錯誤、不是壞資料,是我們自己把選項拿掉了 —— 所以它就
        // 安安靜靜地落在預設(warn)上:不 throw、不 toast、也不 console.warn。
        // 對使用者噴一條看起來像 bug 的警告,只會讓他以為自己的帳號壞了。
        if (alive && REDACTION_MODES.includes(s.redactionMode)) {
          setRedactionMode(s.redactionMode);
        }
      })
      .catch(() => { /* 後端無設定 → 維持 localStorage 值 */ })
      .finally(() => { uiSettingsLoadedRef.current = true; });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem("anila-folders", JSON.stringify(folders));
    } catch {
      /* quota / private mode — fall back silently */
    }
    // 載入後才回存後端(避免用初始 localStorage 值蓋掉後端真值)。debounce。
    if (!uiSettingsLoadedRef.current || !isAuthenticated) return;
    // ⚠ PUT 是整包覆寫,所以每一次都要把 blob 的每個 key 都帶上。少帶一個,
    // 另一個設定就會被這次的寫入洗掉。
    const t = setTimeout(() => {
      putUiSettings(authRequest, { folders, redactionMode }).catch(() => { /* best-effort */ });
    }, 600);
    return () => clearTimeout(t);
  }, [folders, redactionMode, isAuthenticated, authRequest]);

  // 匯出對話為 JSON / Markdown(純前端,離線可用)。未載入的對話先抓訊息。
  // OW-1: hydration/list already hold the server active path, so export
  // naturally exports the active path (abandoned branches omitted).
  const exportConversation = useCallback(async (convId, format) => {
    const conv = conversations.find((c) => c.id === convId);
    let msgs = messagesByConv[convId];
    if (!msgs || msgs.length === 0) {
      try {
        const detail = await apiGetConversation(authRequest, convId);
        msgs = (detail.messages || []).map((m) => ({ role: m.role, text: m.content }));
      } catch { msgs = []; }
    }
    const title = conv?.title || "對話";
    let content; let mime; let ext;
    if (format === "markdown") {
      const lines = [`# ${title}`, ""];
      for (const m of msgs) {
        if (!m.text) continue;
        lines.push(m.role === "user" ? "## 使用者" : "## ANILA");
        lines.push("", m.text, "");
      }
      content = lines.join("\n"); mime = "text/markdown"; ext = "md";
    } else {
      content = JSON.stringify({ title, messages: msgs.map((m) => ({ role: m.role, content: m.text })) }, null, 2);
      mime = "application/json"; ext = "json";
    }
    const blob = new Blob([content], { type: `${mime};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[^\w一-鿿 -]/g, "_").slice(0, 40)}.${ext}`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [conversations, messagesByConv, authRequest]);

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

  // 管理員公告 banner:登入後抓 active,使用者關閉後 localStorage 記住。
  const [banners, setBanners] = useState([]);
  const [dismissedBanners, setDismissedBanners] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem("anila-dismissed-banners") || "[]")); }
    catch { return new Set(); }
  });
  useEffect(() => {
    if (!isAuthenticated) return;
    let alive = true;
    apiListActiveBanners(authRequest)
      .then((rows) => { if (alive) setBanners(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (alive) setBanners([]); });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest]);
  const visibleBanners = banners.filter((b) => !dismissedBanners.has(b.id));
  const dismissBanner = useCallback((id) => {
    setDismissedBanners((prev) => {
      const next = new Set(prev); next.add(id);
      try { localStorage.setItem("anila-dismissed-banners", JSON.stringify([...next])); } catch {}
      return next;
    });
  }, []);

  // Changelog「新功能」modal:未看過最新版時頂列鈕顯示小紅點。
  const [changelogOpen, setChangelogOpen] = useState(false);
  const [changelogUnseen, setChangelogUnseen] = useState(() => {
    try { return localStorage.getItem("anila-changelog-seen") !== CHANGELOG_VERSION; }
    catch { return false; }
  });

  // Slice 2b-D 最小 Task 流:taskId 快取在 conversation state 上(與
  // conversationId / agent 選擇同一份資料),串流期間不會消失。查無 = null
  // (任務建立失敗的降級模式 → 後續 /v1 呼叫不帶 X-ANILA-Task-Id)。
  function taskIdForConv(convId) {
    return conversations.find((c) => c.id === convId)?.taskId ?? null;
  }

  // Stop generation:每個進行中的串流對應一個 AbortController,以 convId 為鍵。
  // streamWithAbort 包住串流呼叫管理生命週期;stopStreaming 中止指定對話。
  const streamAbortRef = useRef(new Map());
  const adoptInFlightRef = useRef(false);
  async function streamWithAbort(convId, opts) {
    // ── 敏感資訊閘門・扼流點 2/2:模型呼叫 ─────────────────────────
    // 送出路徑上**每一次**模型呼叫都經過這裡(送出、編輯重問、重試、引導式
    // 重試、對照建議、對比模式),所以檢查放在這裡,而不是放在每一個發起點。
    // 新增一條送出路徑的人不必記得呼叫任何東西,他必須刻意繞開這個函式才躲得掉。
    if (!passesRedactionGate(outgoingUserText(opts?.payload))) {
      return null;
    }
    const controller = new AbortController();
    streamAbortRef.current.set(convId, controller);
    let compactChain = Promise.resolve();
    const queueCompact = (payload) => {
      compactChain = compactChain.then(() => handleIncomingCompact(convId, payload, opts.payload));
    };
    try {
      const result = await streamChatCompletion({
        // 對話已綁 Task 時所有後續 chat 呼叫(送出/編輯/重試)自動帶上;
        // 呼叫端可用 opts.taskId 覆寫(sendMessage 首回合的 state 尚未落地)。
        taskId: taskIdForConv(convId),
        ...opts,
        signal: controller.signal,
        onCompact: (payload) => {
          queueCompact(payload);
          opts.onCompact?.(payload);
        },
        onMeta: (meta) => {
          if (meta?.compact) queueCompact(meta.compact);
          opts.onMeta?.(meta);
        },
      });
      return result;
    } finally {
      // error／abort 也要等已入列的 compact 寫完，不能只在成功路徑 await。
      try {
        await compactChain;
      } finally {
        streamAbortRef.current.delete(convId);
      }
    }
  }
  // 使用者主動按停止 vs 串流自己出錯 —— 落庫的狀態不同(stopped / failed),
  // 使用者看到的說明也不同。少了這個旗標,兩者都會被寫成 failed。
  const userStoppedRef = useRef(new Set());
  // 還沒開始跑、正在鏈上排隊的那幾輪(assistantId → 取消旗標)。
  // 「停止產生」必須連它們一起停掉,理由見 stopStreaming。
  const queuedTurnsRef = useRef(new Map());

  /**
   * @param {boolean} cancelQueued
   *   true = 使用者按的是「停止產生」那顆按鈕:意思是「這個對話現在不要
   *   再產生了」,所以連排隊中的那幾輪一起取消。原本只 abort 當下註冊的
   *   那一個 controller —— 在第一輪按停止,第二輪照樣立刻開始跑,而按鈕
   *   仍然顯示「停止產生」。使用者按了、畫面照樣在動,這正是本專案第四條
   *   教訓(讓使用者以為發生了某件事,而後端根本沒收到那個意圖)。
   *   false(預設)= 編輯重問/重新產生/自訂動作在建立分支前的清場,語意
   *   只有「把現在這一條串流停掉」,不該波及使用者排隊中的訊息。
   */
  function stopStreaming(convId, { cancelQueued = false } = {}) {
    if (cancelQueued) {
      for (const record of queuedTurnsRef.current.values()) {
        if (record.convId === convId) record.cancelled = true;
      }
    }
    const controller = streamAbortRef.current.get(convId);
    if (controller) {
      userStoppedRef.current.add(convId);
      controller.abort();
    }
  }

  // 送出路徑的兩條串行鏈(runtime/reservedTurn.js)。
  // head = POST /turn(使用者訊息落庫 + 助理列預留,伺服器端一次交易)。
  //        樹的形狀由那個交易保證,這條鏈保證的是「送出順序」:兩則訊息
  //        連著按 Enter 時,先按的必須先落庫,否則兩則的請求誰先回來,
  //        對話紀錄裡的順序就跟著顛倒 —— 使用者看得到,而且無法自救。
  // stream = 實際串流:排隊感留在這裡(第二輪要拿到第一輪的答案當上下文)。
  const turnHeadChain = useRef(createTurnChain()).current;
  const chainTurnStream = useRef(createTurnChain()).current;

  /**
   * ── 敏感資訊閘門・扼流點 1/2:使用者訊息落庫 ──────────────────────
   *
   * 只在模型呼叫那一端擋是不夠的:落庫發生在串流之前,擋在後面等於「模型沒
   * 看到,但資料庫裡存了一份原文」。所以新使用者訊息要進資料庫,也只有這一條路。
   *
   * ⚠ `content` 是**必填位置參數**,而且閘門就在這裡面。這是刻意的:呼叫端
   * 沒辦法「忘記讓它過閘門」——他連呼叫都得先把要落庫的文字交出來。
   *
   * 被擋下時回 `{ ok: false, blockedByRedaction: true }`。呼叫端本來就檢查
   * `head.ok`,所以就算沒特別處理這個旗標,也只是錯誤訊息不夠貼切,
   * **不會**變成原文外流。
   */
  const chainTurnHead = (convId, content, fn) => {
    if (!passesRedactionGate(content)) {
      return Promise.resolve({ ok: false, blockedByRedaction: true });
    }
    return turnHeadChain(convId, fn);
  };

  // 串流中的預留列:視窗要關掉時,把半截內容誠實標成 interrupted。
  // 這是 best-effort —— 送不出去也「沒有任何東西遺失」,使用者的文字和
  // 助理的位置早就在伺服器上了;送不出去只是標示會停在非終局狀態,
  // 而那個狀態在 UI 上一樣顯示為未完成。
  const inFlightStreamsRef = useRef(new Map());
  useEffect(() => {
    function markInterrupted() {
      for (const record of inFlightStreamsRef.current.values()) {
        try {
          apiUpdateMessage(authRequest, record.convId, record.messageId, {
            content: record.text,
            metadata: { anila_stream: { state: STREAM_STATE.INTERRUPTED } },
            streamWriter: record.writer,
            keepalive: true,
          });
        } catch {
          // 卸載途中不做任何補救 —— 見上面的註解。
        }
      }
    }
    window.addEventListener("pagehide", markInterrupted);
    return () => window.removeEventListener("pagehide", markInterrupted);
  }, [authRequest]);

  const selectedConv = useMemo(
    () => conversations.find((c) => c.id === selectedConvId) || null,
    [conversations, selectedConvId],
  );
  useEffect(() => {
    const page = selectedConv?.title ? String(selectedConv.title) : "新對話";
    document.title = `${page} · ANILA`;
    return () => { document.title = "ANILA"; };
  }, [selectedConv?.title]);
  const currentMsgs = selectedConvId ? messagesByConv[selectedConvId] || [] : [];
  const isClassified = Boolean(selectedConv?.classified);
  const isClassificationInherited = Boolean(selectedConv?.classificationInherited);
  const activeAgent = useMemo(
    () => agents.find((a) => a.id === selectedAgentId) || ROUTER_AGENT,
    [agents, selectedAgentId],
  );

  // Per-agent preset prompts(開發者在 CSP 設計):切換到某個真 agent 時抓它的
  // 預設提示詞清單,顯示在 composer。Router 是虛擬 agent、沒有 id,不抓。
  // 切到某 agent 時抓它的 functions(可擴充框架:kind+config)。selectedAgentId
  // 在資料面是 agent NAME;Router 是虛擬 agent 沒有 functions。後端以 id-or-name
  // 解析,直接把 name 當 ref 傳。functions 依 kind 分流給不同 UI(renderer registry)。
  const [agentFunctions, setAgentFunctions] = useState([]);
  useEffect(() => {
    if (!selectedAgentId || selectedAgentId === ROUTER_AGENT.id) {
      setAgentFunctions([]);
      return;
    }
    let alive = true;
    apiListAgentFunctions(authRequest, selectedAgentId)
      .then((rows) => { if (alive) setAgentFunctions(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (alive) setAgentFunctions([]); });
    return () => { alive = false; };
  }, [selectedAgentId, authRequest]);
  // Renderer registry split:preset_prompt → composer picker.
  // prompt_action agent functions remain server-side (convergence follow-up);
  // message-row buttons are OW-3 governed actions from /api/message-actions/visible.
  const presetPrompts = useMemo(
    () => agentFunctions.filter((f) => f.kind === "preset_prompt"),
    [agentFunctions],
  );

  // OW-3: fetch visible governed actions once per authenticated session.
  const [customActions, setCustomActions] = useState([]);
  useEffect(() => {
    if (!isAuthenticated) {
      setCustomActions([]);
      return;
    }
    let alive = true;
    listVisibleActions(authRequest)
      .then((rows) => { if (alive) setCustomActions(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (alive) setCustomActions([]); });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest]);
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
      folder: typeof serverRow.folder === "string" && serverRow.folder
        ? serverRow.folder
        : "all",
      tags: (() => {
        const raw = Array.isArray(serverRow.tags) ? serverRow.tags.filter(Boolean) : [];
        const userTags = raw.filter((t) => t !== "classified");
        return classified ? appendClassifiedTag(userTags) : userTags;
      })(),
      starred: Boolean(serverRow.starred),
      classified,
      // P3: distinguishes inheritance-driven latch from agent-required
      // or admin-set classification. Drives the warning banner copy
      // and the (lighter-weight) lock icon variant on the sidebar.
      classificationInherited: Boolean(serverRow.classification_inherited),
      // Slice 3c: four-level classification label (added by the multi-level
      // classification backend). Absent on boolean-only payloads → the level
      // badge simply renders nothing; the boolean latch above is unaffected.
      classificationLevel: serverRow.classification_level,
      // OW-1: server-truth active leaf pointer for the message tree.
      // ⚠ 這個欄位在 shell 裡**沒有任何讀取者**(2026-08-05 全樹確認):路徑一律
      // 由伺服器回傳的 messages 決定,leaf 只是鏡像。保留是因為它零成本且是
      // 除錯時唯一看得到的伺服器指標;動它不會改變任何畫面,所以也不值得測。
      activeLeafMessageId: serverRow.active_leaf_message_id ?? null,
      updatedAt: serverRow.updated_at || serverRow.created_at || nowIso(),
      routerModelId: serverRow.router_model_id ?? null,
      routerModelName: serverRow.router_model_name ?? null,
      routerSelectionVersion: serverRow.router_selection_version ?? 0,
      thinkingTier: normalizeThinkingTier(serverRow.thinking_tier),
      ...compactFieldsFromServer({
        compact_summary: serverRow.compact_summary ?? null,
        compact_boundary_message_id: serverRow.compact_boundary_message_id ?? null,
        compact_updated_at: serverRow.compact_updated_at ?? null,
      }),
    };
  }

  function mapServerMessage(msg) {
    const meta = msg.metadata || {};
    return {
      id: `srv-${msg.id}`,
      dbId: msg.id,
      role: msg.role,
      text: msg.content || "",
      toolCallId: msg.tool_call_id || meta.tool_call_id || null,
      // OW-1 tree nav fields (MessageOut).
      parentId: msg.parent_id ?? null,
      siblingIndex: typeof msg.sibling_index === "number" ? msg.sibling_index : 0,
      siblingCount: typeof msg.sibling_count === "number" ? msg.sibling_count : 1,
      siblingIds: Array.isArray(msg.sibling_ids) ? msg.sibling_ids : [msg.id],
      trace: meta.trace || [],
      citations: meta.citations || [],
      followUps: meta.follow_ups || [],
      handoffChain: meta.handoff_chain || [],
      // 重新載入這一縫。同一份定義也要接在 applyMeta(SSE 現場那一縫)上。
      ...kbMetaFields(meta),
      ...agentReplyMetaFields(meta),
      confidence: meta.confidence,
      classified: meta.classified,
      traceId: msg.trace_id || meta.trace_id,
      latencyMs: msg.latency_ms,
      routedAgentId: meta.routed_agent_id || null,
      rating: msg.rating || null,
      ratingScore: typeof msg.rating_score === "number" ? msg.rating_score : null,
      reasoning: meta.reasoning || null,
      reasoningPersist: meta.reasoning_persist || null,
      thinkingLocked: meta.thinking_locked === true,
      usage: meta.usage || null,
      thinkingApplied: meta.thinking_applied || null,
      // OW-3: action:NAME attribution (second channel alongside metadata.action).
      agentName: msg.agent_name || null,
      // OW-3 provenance (metadata.action) — quiet action-name attribution.
      metadata: meta,
      // 先預留再串流:一則從伺服器載回來的助理訊息可能是半截的(使用者按
      // 停止、串流出錯、寫它的分頁消失)。狀態如實帶進 UI,半截的答案一定
      // 要標示出來,不能長得跟完整答案一樣。
      streamState: readStreamState(meta),
      incompleteNotice: streamStateNotice(
        readStreamState(meta), Boolean(msg.content),
      ),
      streaming: false,
      attachments: mapServerAttachments(msg.attachments),
      conversationId: null, // patched by caller
      createdAt: msg.created_at,
    };
  }

  /** Reload the server active path into local state (preserves client-only fields). */
  async function refreshActivePath(convId) {
    if (typeof convId !== "number") return;
    const detail = await apiGetConversation(authRequest, convId, { view: "active" });
    const mapped = (detail.messages || []).map((m) => ({
      ...mapServerMessage(m),
      conversationId: convId,
    }));
    let nextPath = applyServerPath(messagesRef.current[convId] || [], mapped, convId);
    setMessagesByConv((prev) => {
      nextPath = applyServerPath(prev[convId] || [], mapped, convId);
      return {
        ...prev,
        [convId]: nextPath,
      };
    });
    if (detail.active_leaf_message_id !== undefined) {
      updateConv(convId, {
        activeLeafMessageId: detail.active_leaf_message_id,
        ...compactFieldsFromServer(detail),
      });
    } else {
      updateConv(convId, compactFieldsFromServer(detail));
    }
    await flushPendingCompact(convId, nextPath);
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
    // A conversation this tab just created has nothing on the server we don't
    // already hold. Firing the GET anyway raced the send: the empty active
    // path came back after the optimistic bubbles were appended and erased
    // them, so the first click on a suggestion chip looked like a no-op.
    if (locallyCreatedConvIdsRef.current.has(selectedConvId)) return;
    let active = true;
    (async () => {
      try {
        // OW-1: default view=active — hydrate the server active path only.
        const detail = await apiGetConversation(authRequest, selectedConvId, {
          view: "active",
        });
        if (!active) return;
        const msgs = (detail.messages || []).map((m) => ({
          ...mapServerMessage(m),
          conversationId: selectedConvId,
        }));
        setMessagesByConv((prev) => ({
          ...prev,
          [selectedConvId]: applyServerPath(
            prev[selectedConvId] || [],
            msgs,
            selectedConvId,
          ),
        }));
        if (detail.active_leaf_message_id !== undefined) {
          updateConv(selectedConvId, {
            activeLeafMessageId: detail.active_leaf_message_id,
            ...compactFieldsFromServer(detail),
          });
        } else {
          updateConv(selectedConvId, compactFieldsFromServer(detail));
        }
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

  useEffect(() => {
    if (typeof selectedConvId !== "number") {
      setConvUsage(null);
      return undefined;
    }
    setConvUsage(null);
    let cancelled = false;
    const timer = setTimeout(() => {
      apiGetConversationUsage(authRequest, selectedConvId)
        .then((row) => {
          if (!cancelled) setConvUsage(row);
        })
        .catch(() => {
          if (!cancelled) setConvUsage(null);
        });
    }, CONV_USAGE_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [selectedConvId, authRequest, convUsageTick]);

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
      if (creatingConversationRef.current) {
        return creatingConversationRef.current;
      }
      const pending = (async () => {
        let convId;
        let serverRow = null;
        try {
          serverRow = await apiCreateConversation(authRequest, {
            title: makeConversationTitle(text),
            agentId: typeof effectiveAgentId === "number" ? effectiveAgentId : null,
            routerModelId: typeof selectedRouterModelId === "number" ? selectedRouterModelId : null,
          });
          convId = serverRow.id;
          locallyCreatedConvIdsRef.current.add(convId);
          const desiredTier = normalizeThinkingTier(thinkingTier);
          if (desiredTier !== "default") {
            try {
              serverRow = await apiSetConversationThinking(authRequest, convId, {
                thinkingTier: desiredTier,
                expectedVersion: serverRow.router_selection_version ?? 0,
              });
            } catch (err) {
              setThinkingError(err?.message || "無法保存思考程度");
            }
          }
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
            routerModelId: serverRow?.router_model_id ?? selectedRouterModelId ?? null,
            routerModelName: serverRow?.router_model_name ?? selectedRouterModelName ?? null,
            routerSelectionVersion: serverRow?.router_selection_version ?? 0,
            thinkingTier: normalizeThinkingTier(serverRow?.thinking_tier ?? thinkingTier),
            ...compactFieldsFromServer(serverRow),
          },
          ...prev,
        ]);
        setSelectedConvId(convId);
        return convId;
      })();
      creatingConversationRef.current = pending;
      try {
        return await pending;
      } finally {
        creatingConversationRef.current = null;
      }
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

  function historyOptions(convId, currentUserMsg) {
    const conv = conversationsRef.current.find((c) => c.id === convId);
    return {
      compact: compactStateFromConv(conv),
      currentUserMsg,
      onBuilt: (built) => {
        if (convId != null) lastHistoryRef.current.set(convId, built);
      },
    };
  }

  async function persistConversationCompact(convId, summary, boundaryMsg, pathOverride) {
    const existing = compactPersistInFlightRef.current.get(convId);
    if (existing) return existing;
    const work = persistConversationCompactOnce(convId, summary, boundaryMsg, pathOverride)
      .finally(() => {
        if (compactPersistInFlightRef.current.get(convId) === work) {
          compactPersistInFlightRef.current.delete(convId);
        }
      });
    compactPersistInFlightRef.current.set(convId, work);
    return work;
  }

  async function persistConversationCompactOnce(convId, summary, boundaryMsg, pathOverride) {
    if (typeof convId !== "number" || !summary) return false;
    const path = Array.isArray(pathOverride) ? pathOverride : (messagesRef.current[convId] || []);
    const dbId = resolveBoundaryDbId(boundaryMsg, path);
    if (typeof dbId !== "number") {
      writePendingCompact(pendingCompactRef.current, convId, {
        summary,
        clientId: boundaryMsg?.id ?? null,
      });
      return false;
    }
    const pending = readPendingCompact(pendingCompactRef.current, convId);
    if (pendingCompactRef.current.has(convId) && !pending) {
      console.warn("[compact] pending convId 與要寫的對話不一致", convId);
      return false;
    }
    try {
      const saved = await apiSetConversationCompact(authRequest, convId, {
        summary,
        boundaryMessageId: dbId,
      });
      pendingCompactRef.current.delete(convId);
      updateConv(convId, compactFieldsFromServer(saved));
      return true;
    } catch (err) {
      const failure = compactPutFailureState(pending, convId, summary, boundaryMsg?.id);
      if (failure.abandoned) {
        pendingCompactRef.current.delete(convId);
        console.warn("[compact] 放棄寫回摘要", convId, err);
        return false;
      }
      writePendingCompact(pendingCompactRef.current, convId, failure.pending);
      return false;
    }
  }

  async function flushPendingCompact(convId, pathOverride, options) {
    const pending = readPendingCompact(pendingCompactRef.current, convId);
    if (!pending) {
      if (pendingCompactRef.current.has(convId)) {
        console.warn("[compact] flush convId 與 pending 不一致，略過", convId);
      }
      return false;
    }
    const path = Array.isArray(pathOverride) ? pathOverride : (messagesRef.current[convId] || []);
    const fromHistory = pending.clientId
      ? lastHistoryRef.current.get(convId)?.sources?.find((s) => s && s.id === pending.clientId)
      : null;
    const hinted = fromHistory
      || (pending.clientId ? path.find((m) => m.id === pending.clientId) : null);
    const dbId = resolveBoundaryDbId(hinted, path, options);
    if (typeof dbId !== "number") return false;
    return persistConversationCompact(
      convId,
      pending.summary,
      { dbId, id: pending.clientId },
      path,
    );
  }

  function handleIncomingCompact(convId, compact, fallbackPayload) {
    if (!compact || compact.method !== "summary" || !compact.summary) return;
    const key = `${convId}:${compact.kept_from_index}:${compact.summary}`;
    if (compactAppliedRef.current.has(key)) return;
    compactAppliedRef.current.add(key);
    const stored = lastHistoryRef.current.get(convId);
    const payloadMessages = stored?.messages || fallbackPayload?.messages || [];
    const sources = stored?.sources || [];
    const boundaryMsg = resolveKeptBoundary(
      payloadMessages,
      compact.kept_from_index,
      sources,
    );
    return persistConversationCompact(
      convId,
      compact.summary,
      boundaryMsg,
      messagesRef.current[convId] || [],
    );
  }

  async function restoreFullContext() {
    if (typeof selectedConvId !== "number") return;
    const ok = await confirm({
      title: "還原完整上下文",
      message: "之後送出會把整段對話再給模型看，不再只用摘要。確定還原？",
      confirmText: "還原",
    });
    if (!ok) return;
    try {
      const saved = await apiClearConversationCompact(authRequest, selectedConvId);
      updateConv(selectedConvId, compactFieldsFromServer({
        compact_summary: saved?.compact_summary ?? null,
        compact_boundary_message_id: saved?.compact_boundary_message_id ?? null,
        compact_updated_at: saved?.compact_updated_at ?? null,
      }));
    } catch (err) {
      setRuntimeError(err?.message || "無法還原完整上下文");
    }
  }

  async function handleCompactConversation() {
    if (typeof selectedConvId !== "number") return;
    const liveMsgs = messagesRef.current[selectedConvId] || currentMsgs;
    if (liveMsgs.some((m) => m.streaming) || compacting) return;
    setCompacting(true);
    try {
      const messages = buildMessageHistory(
        liveMsgs,
        null,
        [],
        historyOptions(selectedConvId, null),
      );
      const result = await apiRequestConversationCompact(authRequest, {
        messages,
        routerModel: selectedAgentId === ROUTER_AGENT.id ? selectedRouterModelName : undefined,
        convId: selectedConvId,
      });
      if (result?.method === "none") {
        toast("對話還不夠長，不需要整理");
        return;
      }
      if (result?.method === "summary") {
        await handleIncomingCompact(selectedConvId, result, { messages });
        const n = keptMessageCount(messages.length, result.kept_from_index);
        toast(`已整理，模型現在只看摘要與最近 ${n} 則`);
      }
    } catch (err) {
      setRuntimeError(err?.message || "整理對話失敗");
    } finally {
      setCompacting(false);
    }
  }

  // Persist star / folder / user-tags (same optimistic+rollback pattern as rename).
  // The derived ``classified`` tag is never sent — server strips it and re-derives.
  async function handleUpdateConvMeta(convId, patch) {
    const prev = conversations.find((c) => c.id === convId);
    if (!prev) return;
    const next = { ...patch };
    if (Array.isArray(next.tags)) {
      const userTags = next.tags.filter((t) => t && t !== "classified");
      next.tags = prev.classified ? appendClassifiedTag(userTags) : userTags;
    }
    updateConv(convId, next);
    if (typeof convId !== "number") return;
    const body = {};
    if (typeof next.starred === "boolean") body.starred = next.starred;
    if (typeof next.folder === "string") body.folder = next.folder;
    if (Array.isArray(next.tags)) {
      body.tags = next.tags.filter((t) => t !== "classified");
    }
    try {
      await apiUpdateConversation(authRequest, convId, body);
    } catch (err) {
      updateConv(convId, {
        starred: prev.starred,
        folder: prev.folder,
        tags: prev.tags,
      });
      setRuntimeError(err.message || "儲存對話分類失敗");
    }
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
    if (!(await confirm({
      title: "刪除對話",
      message: `確定要刪除「${target.title}」？此動作無法復原。`,
      confirmText: "刪除",
      tone: "danger",
    }))) return;
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
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
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

  // ---- edit a user message + re-run the chat turn (OW-1 branch) ----
  // New user message is a sibling of the edited one; old subtree retained.
  async function handleEditUser(userMsg, nextText) {
    // Identical text is a legitimate re-send (owner 2026-08-01); only empty aborts.
    const decision = resolveEditResend(nextText);
    if (!decision.ok) return;
    const trimmed = decision.text;
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
      return;
    }
    const convId = userMsg.conversationId;
    const existing = messagesByConv[convId] || [];
    const idx = existing.findIndex((m) => m.id === userMsg.id);
    if (idx < 0) return;
    if (typeof convId !== "number" || typeof userMsg.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存至後端，無法編輯重問。");
      return;
    }

    // Branch-creating actions must stop any in-flight stream first.
    // ⚠ cancelQueued 維持預設 false:編輯重問的語意只有「把現在這條串流停
    // 掉」,不該把使用者排隊中的訊息一起連坐取消。
    stopStreaming(convId);

    const effectiveTarget = selectedAgentId;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const assistantId = makeId("a");
    const writer = makeStreamWriter();

    // head:使用者訊息分支 ＋ 助理列預留,伺服器端一次交易(POST /branch-turn)。
    //
    // ⚠ 這裡曾經是「先 branch 出使用者訊息、串流跑完才 append 助理訊息」。
    // branch 之後 active leaf 落在一則**使用者**訊息上,而且整段串流期間都停
    // 在那裡;使用者這時插一句話送出,新訊息就掛到編輯後的問題底下
    // (user → user),編輯後那題的答案接著被後端以 400「分支訊息的角色必須與
    // 既有子訊息相同」擋掉,重整之後就沒了 —— 而它一秒鐘前還完整地顯示在螢幕
    // 上(驗證者 2026-08-05 於瀏覽器實測,樹是 user → user → assistant)。
    // 不變式:任何會產生一輪問答的路徑,都不得在串流期間把 active leaf 留在
    // 使用者訊息上。送出路徑與這條路徑因此共用同一個 head 原語。
    const head = await chainTurnHead(convId, trimmed, () =>
      persistTurnHead({
        startTurn: (auth, cid, payload) =>
          apiBranchTurn(auth, cid, userMsg.dbId, payload),
        authRequest,
        convId,
        content: trimmed,
        agentName: effectiveTarget,
        writer,
      }),
    );
    // 閘門擋下來的不是故障,toast 已經說明了 —— 不要再蓋一條錯誤橫幅上去。
    if (head.blockedByRedaction) return;
    if (!head.ok) {
      setRuntimeError(head.error?.message || "訊息分支建立失敗");
      // 這一條路徑上舊的問答還原封不動地在畫面上,沒有任何東西被取代 ——
      // 所以不需要在氣泡上留錯誤標示,橫幅就夠了。
      return;
    }
    const savedUser = head.userSaved;
    const reserved = head.assistantSaved;

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
      // 助理列在串流開始之前就有 id 了 —— 這正是這一輪修正的重點。
      dbId: reserved.id,
      parentId: reserved.parent_id ?? savedUser.id,
      siblingIndex: reserved.sibling_index ?? 0,
      siblingCount: reserved.sibling_count ?? 1,
      siblingIds: Array.isArray(reserved.sibling_ids)
        ? reserved.sibling_ids
        : [reserved.id],
      createdAt: nowIso(),
      timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
    };
    const newUserMsg = {
      ...mapServerMessage(savedUser),
      text: trimmed,
      conversationId: convId,
    };
    // Keep ancestors; drop the abandoned sibling subtree from the visible path.
    // Re-find index inside the updater — list may have changed during await.
    setMessagesByConv((prev) => {
      const list = prev[convId] || [];
      const liveIdx = list.findIndex((m) => m.id === userMsg.id);
      const cut = liveIdx >= 0 ? liveIdx : idx;
      return {
        ...prev,
        [convId]: [...list.slice(0, cut), newUserMsg, assistantMsg],
      };
    });
    updateConv(convId, { activeLeafMessageId: reserved.id });

    // 預留列一存在就登記:關視窗/重整時要誠實收尾成 interrupted,
    // 否則它會永遠停在 reserved(誰都寫不進去,也沒有回收程序)。
    setRouterPickerLocked(true);
    inFlightStreamsRef.current.set(assistantId, {
      convId,
      messageId: reserved.id,
      writer,
      text: "",
    });
    const queueRecord = { convId, cancelled: false };
    queuedTurnsRef.current.set(assistantId, queueRecord);

    const historyPrior = existing.slice(0, idx);
    const payload = {
      model: effectiveTarget,
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
      messages: buildMessageHistory(historyPrior, trimmed, userMsg.attachments || [], historyOptions(convId, newUserMsg)),
    };

    await chainTurnStream(convId, async () => {
      // 保留(M6 裁決):cancelled 旗標在下一行就讀完了,所以刪不刪不影響行為 ——
      // 但 queuedTurnsRef 是整個 runtime 共用的一個 Map,不清就只增不減。
      queuedTurnsRef.current.delete(assistantId);
      let finalText = "";
      let finalMeta = null;
      const accumulatedTrace = [];
      let accumulatedReasoning = "";
      let streamState = STREAM_STATE.COMPLETE;
      let streamError = null;

      if (queueRecord.cancelled) {
        // 排隊期間使用者按了「停止產生」—— 串流不開始,但預留列必須誠實收尾。
        streamState = STREAM_STATE.STOPPED;
        inFlightStreamsRef.current.delete(assistantId);
        if (inFlightStreamsRef.current.size === 0) setRouterPickerLocked(false);
      } else {
        try {
          await streamWithAbort(convId, {
            url: `${baseUrl}/v1/chat/completions`,
            payload,
            conversationId: convId,
            onText: (acc) => {
              finalText = acc;
              const record = inFlightStreamsRef.current.get(assistantId);
              if (record) record.text = acc;
              updateMsg(convId, assistantId, { text: acc });
            },
            onFinishReason: (reason) => {
              updateMsg(convId, assistantId, { finishReason: reason, finishedAt: Date.now() });
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
            onMeta: (metaFrame) => {
              finalMeta = metaFrame;
              applyMeta(convId, assistantId, effectiveTarget, metaFrame);
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
          // 按停止時 streamChatCompletion 是正常返回而不是拋出(sse.js:153),
          // 只靠 catch 判斷會把被中斷的半截答案寫成 complete。
          if (userStoppedRef.current.has(convId)) {
            streamState = STREAM_STATE.STOPPED;
          }
        } catch (error) {
          streamError = error;
          streamState = userStoppedRef.current.has(convId)
            ? STREAM_STATE.STOPPED
            : STREAM_STATE.FAILED;
        } finally {
          userStoppedRef.current.delete(convId);
          inFlightStreamsRef.current.delete(assistantId);
        if (inFlightStreamsRef.current.size === 0) setRouterPickerLocked(false);
        }
      }

      const lengthBudget = streamState === STREAM_STATE.FAILED && isLengthBudgetError(streamError);
      if (lengthBudget) streamState = STREAM_STATE.COMPLETE;
      if (
        lengthBudget
        || streamState === STREAM_STATE.STOPPED
        || streamState === STREAM_STATE.FAILED
      ) {
        clearPendingArtifactRevision(assistantId);
      }
      updateMsg(convId, assistantId, {
        streaming: false,
        streamState,
        incompleteNotice: lengthBudget
          ? lengthBudgetNotice(Boolean(finalText))
          : streamStateNotice(streamState, Boolean(finalText)),
        error:
          !lengthBudget && streamState === STREAM_STATE.FAILED
            ? streamError?.message || "產生回應時發生錯誤，請稍後再試。"
            : null,
        ...(lengthBudget && finalText ? { finishReason: "length" } : {}),
      });

      // 內容寫回預留的那一列(不是 append 一則新的)。狀態必須誠實。
      const agentNameForPersist = resolveAgentNameForPersist(
        finalMeta,
        effectiveTarget,
        agents,
      );
      const persistMeta = buildPersistMeta(finalMeta, {
        trace: accumulatedTrace,
        reasoning: accumulatedReasoning,
      });
      const persisted = await finalizeStreamedAssistant({
        updateMessage: apiUpdateMessage,
        authRequest,
        convId,
        messageId: reserved.id,
        writer,
        state: streamState,
        content: finalText,
        traceId: finalMeta?.trace_id,
        latencyMs: finalMeta?.latency_ms,
        agentName: agentNameForPersist,
        metadata: persistMeta,
      });
      if (!persisted.ok) {
        setRuntimeError(persisted.error?.message || "對話訊息儲存失敗");
        updateMsg(convId, assistantId, { persistError: persisted.notice });
        return;
      }
      const persistPatch = persistFieldsFromSaved(persisted.saved);
      if (persistPatch) updateMsg(convId, assistantId, persistPatch);
      await refreshActivePath(convId);
    });
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
    const current = messagesRef.current[convId] || [];
    messagesRef.current = {
      ...messagesRef.current,
      [convId]: current.map((m) => (m.id === msgId ? { ...m, ...patch } : m)),
    };
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
      // SSE 現場這一縫。同一份定義也要接在 mapServerMessage(重新載入那一縫)上。
      ...kbMetaFields(meta),
      ...agentReplyMetaFields(meta),
      latencyMs: meta.latency_ms,
      usage: meta.usage || null,
      classified: meta.classified,
      reasoning: meta.reasoning || null,
      thinkingLocked: meta.thinking_locked === true,
      ...(meta.thinking_applied ? { thinkingApplied: meta.thinking_applied } : {}),
      // Display-only, but it was showing the wrong agent name on every
      // routed answer: BOTH ends of handoff_chain read "anila-router" on the
      // router path, so `.at(-1)` never named the agent that answered.
      routedAgentId: resolveAnsweringAgentId(meta) || agentId,
      stageLabel: meta.trace?.at?.(-1)?.label,
      conversationId: convId,
    });
    if (typeof convId === "number") {
      setConvUsageTick((n) => n + 1);
    }

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

  function applySelectionFromServer(convId, serverRow) {
    const patch = {
      ...conversationSelectionFromServer(serverRow),
      ...compactFieldsFromServer(serverRow),
    };
    setConversations((prev) => prev.map((row) => (
      row.id === convId ? { ...row, ...patch } : row
    )));
    if (selectedConvId === convId) {
      if (typeof patch.routerModelId === "number") {
        setSelectedRouterModelId(patch.routerModelId);
      }
      if (patch.thinkingTier !== undefined) {
        setThinkingTier(patch.thinkingTier);
      }
    }
  }

  async function persistThinkingTier(nextTier) {
    const previous = thinkingTier;
    const normalized = normalizeThinkingTier(nextTier);
    setThinkingTier(normalized);
    setThinkingError("");
    const convId = selectedConvId;
    const conv = conversations.find((c) => c.id === convId);
    if (typeof convId !== "number" || !conv) {
      persistThinkingTierPreference(normalized);
      return;
    }
    try {
      const saved = await apiSetConversationThinking(authRequest, convId, {
        thinkingTier: normalized,
        expectedVersion: conv.routerSelectionVersion || 0,
      });
      applySelectionFromServer(convId, saved);
      persistThinkingTierPreference(normalizeThinkingTier(saved.thinking_tier));
    } catch (err) {
      setThinkingTier(previous);
      setThinkingError(err?.message || "無法保存思考程度");
      try {
        const fresh = await apiGetConversation(authRequest, convId);
        applySelectionFromServer(convId, fresh);
      } catch (_) { /* keep previous picker */ }
    }
  }

  function setDeepThinkNextFlag(value) {
    const next = Boolean(value);
    deepThinkNextRef.current = next;
    setDeepThinkNext(next);
  }

  function consumeOneShotDeep() {
    if (!deepThinkNextRef.current) return false;
    deepThinkNextRef.current = false;
    setDeepThinkNext(false);
    return true;
  }

  // ---- send single ----
  async function sendMessage(text, attachments = [], meta = {}) {
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
      return;
    }
    const { explicitAgents = [] } = meta;
    if (explicitAgents.length > 1) {
      return sendCompare(text, attachments, { explicitAgents });
    }
    // ⚠ 這裡要**早於** ensureConversation / createTaskForConversation:那兩個會
    // 拿這段草稿當標題送到伺服器上。等到落庫扼流點才擋,訊息本身是保住了,
    // 對話標題和 Task 標題卻已經帶著身分證號出去了。
    // 這不是閘門的第二份實作,是同一個閘門在更早的位置再問一次;
    // 後面兩個扼流點仍然是保證,漏掉這一行也不會讓訊息內容外流。
    //
    // ⚠ 但標題會。這一行有自己的釘子:redactionChokePoint 的「Task 標題」
    // (走建議追問,不經過 composer 閘門)+ 突變 `redaction-gate-skipped-before-title`。
    // 走 composer 的測試釘不住它 —— chat.jsx 的閘門會先擋,拿掉這一行照樣全綠。
    if (!passesRedactionGate(text)) return;

    const effectiveTarget = explicitAgents[0] || selectedAgentId;
    const convId = await ensureConversation(text, effectiveTarget);
    updateConversationAgent(convId, effectiveTarget);

    const bindIds = attachmentBindIds(attachments);
    if (bindIds.length > 0) {
      if (typeof convId !== "number") {
        const msg = "附件無法綁定到對話，請重新上傳後再送出";
        setRuntimeError(msg);
        toast(msg, { tone: "error" });
        return;
      }
      try {
        await apiBindAttachments(authRequest, {
          conversationId: convId,
          referenceIds: bindIds,
        });
      } catch (err) {
        const msg = err.message || "附件無法綁定到對話";
        setRuntimeError(msg);
        toast(msg, { tone: "error" });
        return;
      }
    }

    // Slice 2b-D 最小 Task 流(doc 00 §3:提出任務→建立 Task→派發):對話
    // 還沒綁 Task 時先建立一個(標題 = 首句前段),成功後快取到 conversation
    // state;失敗回 null → 靜默降級,聊天照常、只是不帶 Task 標頭。
    let taskId = taskIdForConv(convId);
    if (taskId == null) {
      const task = await createTaskForConversation({
        title: text,
        conversationId: typeof convId === "number" ? convId : undefined,
      });
      if (task) {
        taskId = task.taskId;
        updateConv(convId, { taskId: task.taskId, taskTraceId: task.traceId });
      }
    }

    const oneShotDeep = consumeOneShotDeep();
    const userMsg = {
      id: makeId("u"),
      role: "user",
      text,
      attachments,
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
      thinkingApplied: outgoingThinkingApplied({ oneShotDeep, thinkingTier }),
    };
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [...(prev[convId] || []), userMsg, assistantMsg],
    }));

    const revisionKind = meta?.artifactRevision?.kind
      ? String(meta.artifactRevision.kind)
      : "";
    if (revisionKind) {
      setPendingArtifactRevision({
        messageId: assistantId,
        kind: revisionKind,
        conversationId: convId,
      });
    }

    // 先落庫再串流(runtime/reservedTurn.js)。使用者訊息立刻上伺服器,
    // 助理訊息「在串流開始之前」先預留一列 —— active leaf 因此不會在串流
    // 期間停在使用者訊息上,串流中途送出的下一則訊息會正確掛在它底下。
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;

    // 降級路徑:對話還沒有真的 id(離線/建立失敗)時沒有東西可以落庫,
    // 照舊直接串流,只是這一輪不會被保存。
    const persistable = typeof convId === "number";
    const writer = makeStreamWriter();

    // 第一段:使用者訊息落庫 + 助理列預留,伺服器端一次交易(POST /turn)。
    // 同一個對話的 head 之間仍然要依序 —— 那是為了保住送出順序(先按
    // Enter 的先落庫);樹的形狀則由伺服器的單一交易保證。這一段刻意
    // 不等前一輪的串流:按下 Enter 的當下文字就要到伺服器上。
    let head = null;
    const applyTurnHead = (resolved) => {
      const savedUser = resolved.userSaved;
      const reserved = resolved.assistantSaved;
      // 上一則問題從來沒有得到回答時,伺服器會先替它補一列終局的空回答,
      // 這一輪才接在那一列底下(不然就是 user → user,那則問題永遠拿不到
      // 回答)。它是伺服器做的事,使用者當下就該看見 —— 所以插進清單裡,
      // 而不是等下一次重整才冒出來。
      if (resolved.unansweredSaved) {
        const filler = {
          ...mapServerMessage(resolved.unansweredSaved),
          conversationId: convId,
        };
        setMessagesByConv((prev) => {
          const list = prev[convId] || [];
          if (list.some((m) => m.dbId === filler.dbId)) return prev;
          const at = list.findIndex((m) => m.id === userMsg.id);
          const cut = at >= 0 ? at : list.length;
          return {
            ...prev,
            [convId]: [...list.slice(0, cut), filler, ...list.slice(cut)],
          };
        });
      }
      userMsg.dbId = savedUser.id;
      updateMsg(convId, userMsg.id, {
        dbId: savedUser.id,
        parentId: savedUser.parent_id ?? null,
        siblingIndex: savedUser.sibling_index ?? 0,
        siblingCount: savedUser.sibling_count ?? 1,
        siblingIds: Array.isArray(savedUser.sibling_ids)
          ? savedUser.sibling_ids
          : [savedUser.id],
        persistError: null,
      });
      if (bindIds.length > 0) {
        apiBindAttachments(authRequest, {
          conversationId: convId,
          referenceIds: bindIds,
          messageId: savedUser.id,
        }).catch(() => {
          /* local preview still uses dataUrl; reload chips need this pin */
        });
      }
      updateMsg(convId, assistantId, {
        dbId: reserved.id,
        parentId: reserved.parent_id ?? savedUser.id,
        siblingIndex: reserved.sibling_index ?? 0,
        siblingCount: reserved.sibling_count ?? 1,
        siblingIds: Array.isArray(reserved.sibling_ids)
          ? reserved.sibling_ids
          : [reserved.id],
        persistError: null,
      });
      updateConv(convId, { activeLeafMessageId: reserved.id });

      // 從「預留成功」的這一刻起就登記,而不是等串流開始才登記。
      // 插話送出的第二輪會在這裡排隊等第一輪跑完,那段期間它的預留列
      // 已經在伺服器上了 —— 這時重整或關視窗,如果沒登記,那一列就會
      // 永遠停在 reserved:誰都寫不進去(沒有權杖),也沒有任何清理程序。
      setRouterPickerLocked(true);
      inFlightStreamsRef.current.set(assistantId, {
        convId,
        messageId: reserved.id,
        writer,
        text: "",
      });
    };

    // POST /turn 立刻發出,但不擋住串流:persist 慢的時候 compact 可能先到,
    // 那時邊界還沒有 dbId,必須先 pending,等 turn 回來再 refresh／flush。
    const turnPromise = persistable
      ? chainTurnHead(convId, text, () =>
        persistTurnHead({
          startTurn: apiStartTurn,
          authRequest,
          convId,
          content: text,
          agentName: effectiveTarget,
          writer,
        }),
      ).then(async (resolved) => {
        head = resolved;
        if (resolved.blockedByRedaction) return resolved;
        if (!resolved.ok) {
          setRuntimeError(resolved.error?.message || "這一輪沒有順利送出");
          updateMsg(convId, userMsg.id, { persistError: resolved.notice });
          updateMsg(convId, assistantId, {
            streaming: false,
            persistError: resolved.notice,
          });
          await flushPendingCompact(
            convId,
            messagesRef.current[convId] || [],
            { allowLastPersisted: true },
          );
          return resolved;
        }
        applyTurnHead(resolved);
        await flushPendingCompact(convId, messagesRef.current[convId] || []);
        return resolved;
      })
      : Promise.resolve(null);

    // 排隊登記:在鏈上等著跑的這一輪,「停止產生」按下去時要停得掉。
    const queueRecord = { convId, cancelled: false };
    queuedTurnsRef.current.set(assistantId, queueRecord);

    // 第二段:串流。排隊感在這裡 —— 第二輪要等第一輪跑完才開始,
    // 因為它需要第一輪的答案當上下文。等待期間使用者的文字已經在
    // 伺服器上了,這正是與「把文字留在瀏覽器排隊」的結構差異。
    await chainTurnStream(convId, async () => {
      // 保留(M6 裁決):理由同編輯重問那一條 —— 行為等價,但這個 Map 是整個
      // runtime 共用的一份,不清就只增不減。
      queuedTurnsRef.current.delete(assistantId);
      if (queueRecord.cancelled) {
        // 排隊期間使用者按了「停止產生」。這一輪連串流都不要開始,
        // 但它的預留列已經在伺服器上了 —— 必須誠實收尾成 stopped,
        // 否則它會永遠停在 reserved(誰都寫不進去,也沒有回收程序)。
        const resolvedHead = persistable ? await turnPromise : null;
        const reservedId = resolvedHead?.assistantSaved?.id ?? null;
        inFlightStreamsRef.current.delete(assistantId);
        if (inFlightStreamsRef.current.size === 0) setRouterPickerLocked(false);
        clearPendingArtifactRevision(assistantId);
        updateMsg(convId, assistantId, {
          streaming: false,
          streamState: STREAM_STATE.STOPPED,
          incompleteNotice: streamStateNotice(STREAM_STATE.STOPPED, false),
        });
        if (persistable && reservedId != null) {
          const cancelled = await finalizeStreamedAssistant({
            updateMessage: apiUpdateMessage,
            authRequest,
            convId,
            messageId: reservedId,
            writer,
            state: STREAM_STATE.STOPPED,
            content: "",
          });
          if (!cancelled.ok) {
            updateMsg(convId, assistantId, { persistError: cancelled.notice });
          }
        }
        return;
      }
      // 上下文取即時清單、並且切在這一輪的使用者訊息之前 —— 排在後面
      // 等著跑的那幾輪,它們的訊息已經在清單裡了。
      const priorForHistory = historyBefore(
        messagesRef.current[convId] || [],
        userMsg.id,
      );
      const payload = {
        model: effectiveTarget,
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
        ...(oneShotDeep && effectiveTarget === ROUTER_AGENT.id ? { anila_thinking_tier: "deep" } : {}),
        messages: buildMessageHistory(priorForHistory, text, attachments, historyOptions(convId, userMsg)),
      };

      // trace / reasoning 用純區域變數累積,不受 React stale-closure 影響。
      let finalText = "";
      let finalMeta = null;
      const accumulatedTrace = [];
      let accumulatedReasoning = "";
      let streamState = STREAM_STATE.COMPLETE;
      let streamError = null;

      try {
        await streamWithAbort(convId, {
          url: `${baseUrl}/v1/chat/completions`,
          payload,
          conversationId: persistable ? convId : undefined,
          taskId,
          onText: (acc) => {
            finalText = acc;
            const record = inFlightStreamsRef.current.get(assistantId);
            if (record) record.text = acc;
            updateMsg(convId, assistantId, { text: acc });
          },
          onFinishReason: (reason) => {
            updateMsg(convId, assistantId, { finishReason: reason, finishedAt: Date.now() });
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
          onMeta: (metaFrame) => {
            finalMeta = metaFrame;
            applyMeta(convId, assistantId, effectiveTarget, metaFrame);
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
        // ⚠ 使用者按停止時 streamChatCompletion 是「正常返回」而不是拋出
        // (runtime/sse.js:153 吞掉 AbortError 以保留已累積的文字)。只靠
        // catch 判斷的話,被中斷的半截答案會被寫成 complete —— 半截答案
        // 偽裝成完整答案,正是本專案第四條教訓要擋的事。
        if (userStoppedRef.current.has(convId)) {
          streamState = STREAM_STATE.STOPPED;
        }
      } catch (error) {
        streamError = error;
        // 按停止 vs 真的出錯 —— 使用者看到的說明不同,落庫的狀態也不同。
        streamState = userStoppedRef.current.has(convId)
          ? STREAM_STATE.STOPPED
          : STREAM_STATE.FAILED;
      } finally {
        userStoppedRef.current.delete(convId);
        inFlightStreamsRef.current.delete(assistantId);
        if (inFlightStreamsRef.current.size === 0) setRouterPickerLocked(false);
      }

      const lengthBudget = streamState === STREAM_STATE.FAILED && isLengthBudgetError(streamError);
      if (lengthBudget) streamState = STREAM_STATE.COMPLETE;
      const notice = lengthBudget
        ? lengthBudgetNotice(Boolean(finalText))
        : streamStateNotice(streamState, Boolean(finalText));
      if (
        lengthBudget
        || streamState === STREAM_STATE.STOPPED
        || streamState === STREAM_STATE.FAILED
      ) {
        clearPendingArtifactRevision(assistantId);
      }
      updateMsg(convId, assistantId, {
        streaming: false,
        streamState,
        incompleteNotice: notice,
        error:
          !lengthBudget && streamState === STREAM_STATE.FAILED
            ? streamError?.message || "產生回應時發生錯誤，請稍後再試。"
            : null,
        ...(lengthBudget && finalText ? { finishReason: "length" } : {}),
      });

      if (!persistable) return;
      const resolvedHead = await turnPromise;
      if (!resolvedHead?.ok) return;
      const reservedId = resolvedHead.assistantSaved?.id ?? null;
      if (reservedId == null) return;

      // 內容寫回預留的那一列。狀態必須誠實 —— 半截的答案要標成半截。
      const agentNameForPersist = resolveAgentNameForPersist(
        finalMeta,
        effectiveTarget,
        agents,
      );
      const persistMeta = buildPersistMeta(finalMeta, {
        trace: accumulatedTrace,
        reasoning: accumulatedReasoning,
      });
      const persisted = await finalizeStreamedAssistant({
        updateMessage: apiUpdateMessage,
        authRequest,
        convId,
        messageId: reservedId,
        writer,
        state: streamState,
        content: finalText,
        traceId: finalMeta?.trace_id,
        latencyMs: finalMeta?.latency_ms,
        agentName: agentNameForPersist,
        metadata: persistMeta,
      });
      if (!persisted.ok) {
        setRuntimeError(persisted.error?.message || "對話訊息儲存失敗");
        updateMsg(convId, assistantId, { persistError: persisted.notice });
      } else {
        const persistPatch = persistFieldsFromSaved(persisted.saved);
        if (persistPatch) updateMsg(convId, assistantId, persistPatch);
      }
      await flushPendingCompact(convId);

      updateConv(convId, { updatedAt: nowIso() });

      const convRow = conversations.find((c) => c.id === convId);
      const looksLikeAutoTitle =
        !convRow?.title || convRow.title === makeConversationTitle(text);
      if (looksLikeAutoTitle && finalText) {
        generateConversationTitle(convId, text, finalText, effectiveTarget);
      }
    });
  }

  // ---- regenerate a single assistant message ----
  // Finds the user message immediately before the target assistant message
  // and re-runs the chat call, replacing the assistant message's text /
  // trace in place. Caller API key permissions and routing target are
  // inherited from the original turn.
  // OW-3 governed message actions: server-rendered prompt → client chat stream
  // → assistant sibling via POST /branch (never in-place update).
  // Orchestration lives in runtime/messageActions.js (runActionInvokeFillback).
  async function runMessageAction(msg, action, choice = null) {
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
      return;
    }
    if (typeof msg?.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存，無法執行自訂動作。");
      return;
    }
    const convId = msg.conversationId;
    if (typeof convId !== "number") return;
    const msgs = messagesByConv[convId] || [];
    if (msgs.some((m) => m.streaming)) {
      setRuntimeError("回應產生中，請稍候再試。");
      return;
    }
    const idx = msgs.findIndex((m) => m.id === msg.id);
    if (idx < 0) return;
    let userIdx = idx - 1;
    while (userIdx >= 0 && msgs[userIdx].role !== "user") {
      userIdx -= 1;
    }
    if (userIdx < 0) {
      setRuntimeError("找不到對應的使用者訊息，無法執行自訂動作。");
      return;
    }

    // Branch-creating actions must stop any in-flight stream first (same as edit/regenerate).
    stopStreaming(convId);

    const preActionList = msgs;
    const placeholderId = makeId("a");
    const effectiveTarget = msg.routedAgentId || selectedAgentId;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;

    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [
        ...(prev[convId] || []).slice(0, userIdx + 1),
        {
          id: placeholderId,
          role: "assistant",
          text: "",
          trace: [],
          citations: [],
          followUps: [],
          streaming: true,
          rating: null,
          ratingScore: null,
          reasoning: null,
          routedAgentId: effectiveTarget,
          conversationId: convId,
          createdAt: nowIso(),
          timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
        },
      ],
    }));

    const restorePreAction = () => {
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: sanitizeRestoredMessages(preActionList),
      }));
    };

    await runActionInvokeFillback({
      authRequest,
      action,
      choice,
      conversationId: convId,
      messageId: msg.dbId,
      model: effectiveTarget,
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
      branchMessage: apiBranchMessage,
      refreshActivePath,
      onRestore: restorePreAction,
      onError: (message) => setRuntimeError(message),
      runStream: async (payload) => {
        let finalText = "";
        let finalMeta = null;
        const accumulatedTrace = [];
        let accumulatedReasoning = "";
        const streamPhase = await runRegenerateStreamPhase({
          preList: preActionList,
          stream: async () => {
            await streamWithAbort(convId, {
              url: `${baseUrl}/v1/chat/completions`,
              payload,
              conversationId: convId,
              onText: (acc) => {
                finalText = acc;
                updateMsg(convId, placeholderId, { text: acc });
              },
              onTrace: (step) => {
                accumulatedTrace.push(step);
                setMessagesByConv((prev) => ({
                  ...prev,
                  [convId]: (prev[convId] || []).map((m) =>
                    m.id === placeholderId
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
                applyMeta(convId, placeholderId, effectiveTarget, meta);
              },
              onReasoning: (delta) => {
                accumulatedReasoning += delta;
                setMessagesByConv((prev) => ({
                  ...prev,
                  [convId]: (prev[convId] || []).map((m) =>
                    m.id === placeholderId
                      ? { ...m, reasoning: (m.reasoning || "") + delta }
                      : m,
                  ),
                }));
              },
            });
            updateMsg(convId, placeholderId, { streaming: false });
          },
        });
        if (!streamPhase.ok) {
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: sanitizeRestoredMessages(streamPhase.messages),
          }));
          return {
            ok: false,
            error: streamPhase.error,
            content: "",
          };
        }
        return {
          ok: true,
          content: finalText,
          finalMeta,
          accumulatedTrace,
          accumulatedReasoning,
        };
      },
    });
  }

  // Continue Response:回應被 max_tokens 截斷(finishReason==='length')時,
  // 把已生內容當 assistant 上文 + 一句「請接續」當 user turn 重新串流,新內容
  // **附加**到同一則訊息(非取代)。只做 Router/文字回合(分派 agent 的釘定需
  // 後端 session-pin,user 拍板先只做這條);圖像 agent 回合不會有 length 截斷。
  async function continueMessage(assistantMsg) {
    if (!isAuthenticated) { setRuntimeError("尚未登入，請重新登入後再試。"); return; }
    const convId = assistantMsg.conversationId;
    const msgs = messagesByConv[convId] || [];
    const idx = msgs.findIndex((m) => m.id === assistantMsg.id);
    if (idx < 0) return;
    const existing = assistantMsg.text || "";
    if (isHarnessEmptyNotice(existing)) {
      updateMsg(convId, assistantMsg.id, { streaming: false, finishReason: null });
      return;
    }
    const effectiveTarget = assistantMsg.routedAgentId || selectedAgentId;
    const baseUrl = effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    // history 含截斷的這則 assistant + 一句續寫指示。buildMessageHistory 會把
    // 截斷訊息(已非 streaming)當 assistant role 帶上。
    const payload = {
      model: effectiveTarget,
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
      messages: buildMessageHistory(
        msgs.slice(0, idx + 1),
        "請接續上文，直接從中斷處往下寫，不要重複已經寫過的內容。",
        [],
        historyOptions(convId, null),
      ),
    };
    updateMsg(convId, assistantMsg.id, { streaming: true, finishReason: null });
    let appended = "";
    let combined = existing;
    try {
      await streamWithAbort(convId, {
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        onText: (acc) => {
          if (isHarnessEmptyNotice(acc)) {
            appended = "";
            combined = existing;
            return;
          }
          appended = acc;
          // 接在原文後(若原文未以空白結尾補一個空格,避免黏字)。
          const joiner = existing && !/\s$/.test(existing) ? " " : "";
          combined = existing + joiner + acc;
          updateMsg(convId, assistantMsg.id, { text: combined });
        },
        onFinishReason: (reason) => updateMsg(convId, assistantMsg.id, { finishReason: reason, finishedAt: Date.now() }),
      });
    } catch (err) {
      setRuntimeError(err?.message || "續寫失敗");
    } finally {
      updateMsg(convId, assistantMsg.id, { streaming: false });
    }

    // 續寫的內容要寫回資料庫。
    //
    // ⚠ 2026-08-05 之前這裡什麼都沒有:續寫在螢幕上一個字一個字長出來、
    // 提示詞正確、沒有任何錯誤,而資料庫裡那一列仍然只有續寫前的文字 ——
    // 使用者看著答案變長,重新整理之後就沒了。這不是先落庫再串流引入的,
    // 是那之前就一直存在、順手在這一輪關掉的(舊的 wt/fix-shell-persist
    // 分支曾宣稱修好,那條分支已經作廢,沒有進到任何地方)。
    //
    // 只送 content:不帶 metadata,所以伺服器不會動到那一列的 anila_stream
    // 標記(update_message_content 只在 metadata 非 None 時才碰它)。
    // 那一列若還停在非終局狀態(孤兒 reserved),這個 PUT 會 409 —— 那時
    // 使用者看得到氣泡上的說明,不是靜默失敗。
    if (typeof convId !== "number" || typeof assistantMsg.dbId !== "number") return;
    if (!appended) return;
    try {
      const saved = await apiUpdateMessage(authRequest, convId, assistantMsg.dbId, {
        content: combined,
      });
      if (!saved || typeof saved.id !== "number") {
        throw new Error("對話訊息儲存失敗");
      }
      updateMsg(convId, assistantMsg.id, { persistError: null });
    } catch (err) {
      setRuntimeError(err?.message || "對話訊息儲存失敗");
      updateMsg(convId, assistantMsg.id, {
        persistError: ANSWER_PERSIST_FAILURE_NOTICE,
      });
    }
  }

  // steer:guided regenerate 的調整指令(更詳細/更簡潔/換個說法/自由文字);
  // 空 = 盲目重試(原行為)。non-empty 時附加到使用者原文後重新生成。
  //
  // forceKbSearch:「改用院內規章重查」(設計 §8 的事後自救、擁有者 Q40)。
  // ⚠ 它**不是**第五個 steer。steer 的通道是「把字串進使用者訊息」,而重查要
  // 改變的是後端行為(CSP 檢索院內規章),那個開關只認標頭。走 steer 的話問句
  // 會被改寫、CSP 什麼也收不到,而畫面上看起來一切正常——本專案第四條教訓
  // 說的就是這種控制項。所以兩者是分開的兩個參數,而且重查那一輪**不帶任何
  // steer**:同一個問句原樣重問,答案才可比。
  //
  // ⚠ 第三個參數刻意**不在簽章裡解構、也不給 `= {}` 預設值**:
  // `dupReplyReconcile.test.js` 的原始碼護欄靠「從本函式的宣告處起數大括號」
  // 切出函式本體,簽章裡只要出現一對大括號(解構或預設物件),它就會在參數列
  // 收工,護的那三條不變式全部退化成在比對簽章——**而測試還是綠的**。
  // 那個護欄不在本包範圍內,所以改的是這一邊。
  async function regenerateMessage(assistantMsg, steer = "", opts) {
    const { forceKbSearch = false } = opts || {};
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

    // Branch-creating actions must stop any in-flight stream first.
    stopStreaming(convId);

    const effectiveTarget = assistantMsg.routedAgentId || selectedAgentId;
    const baseUrl =
      effectiveTarget === ROUTER_AGENT.id ? config.routerBaseUrl : config.cspBaseUrl;
    const steeredUserText = steer
      ? `${prevUser.text}\n\n（重新回答時請依此調整：${steer}）`
      : prevUser.text;
    const oneShotDeep = consumeOneShotDeep() || shouldReplayOneShotDeep(assistantMsg.thinkingApplied);
    const payload = {
      model: effectiveTarget,
      ...(effectiveTarget === ROUTER_AGENT.id && selectedRouterModelName ? { router_model: selectedRouterModelName } : {}),
      ...(oneShotDeep && effectiveTarget === ROUTER_AGENT.id ? { anila_thinking_tier: "deep" } : {}),
      messages: buildMessageHistory(msgs.slice(0, userIdx), steeredUserText, prevUser.attachments || [], historyOptions(convId, prevUser)),
    };

    // Capture pre-regenerate list so a stream failure can restore the
    // previous answer (placeholder splice must not leave it permanently gone).
    const preRegenList = msgs;
    const placeholderId = makeId("a");
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [
        ...(prev[convId] || []).slice(0, userIdx + 1),
        {
          id: placeholderId,
          role: "assistant",
          text: "",
          trace: [],
          citations: [],
          followUps: [],
          streaming: true,
          rating: null,
          ratingScore: null,
          reasoning: null,
          routedAgentId: effectiveTarget,
          conversationId: convId,
          createdAt: nowIso(),
          timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
          thinkingApplied: outgoingThinkingApplied({ oneShotDeep, thinkingTier }),
        },
      ],
    }));

    let finalText = "";
    let finalMeta = null;
    const accumulatedTrace = [];
    let accumulatedReasoning = "";
    let branchPersisted = false;
    const streamPhase = await runRegenerateStreamPhase({
      preList: preRegenList,
      stream: async () => {
        await streamWithAbort(convId, {
          url: `${baseUrl}/v1/chat/completions`,
          payload,
          conversationId: typeof convId === "number" ? convId : undefined,
          // 隨這一次呼叫走,不進 state:寫進 state 的旗標會黏在對話上,
          // 之後每一輪都強制檢索,等於前端單方面關掉 Router 的判斷。
          forceKbSearch,
          onText: (acc) => {
            finalText = acc;
            updateMsg(convId, placeholderId, { text: acc });
          },
          onTrace: (step) => {
            accumulatedTrace.push(step);
            setMessagesByConv((prev) => ({
              ...prev,
              [convId]: (prev[convId] || []).map((m) =>
                m.id === placeholderId
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
            applyMeta(convId, placeholderId, effectiveTarget, meta);
          },
          onReasoning: (delta) => {
            accumulatedReasoning += delta;
            setMessagesByConv((prev) => ({
              ...prev,
              [convId]: (prev[convId] || []).map((m) =>
                m.id === placeholderId
                  ? { ...m, reasoning: (m.reasoning || "") + delta }
                  : m,
              ),
            }));
          },
        });
        updateMsg(convId, placeholderId, { streaming: false });
      },
    });
    if (!streamPhase.ok) {
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: sanitizeRestoredMessages(streamPhase.messages),
      }));
      setRuntimeError(
        streamPhase.error?.message
          ? `重試失敗：${streamPhase.error.message}`
          : "重試失敗",
      );
      return;
    }

    try {
      if (typeof convId === "number") {
        const agentNameForPersist = resolveAgentNameForPersist(
          finalMeta,
          effectiveTarget,
          agents,
        );
        const persistMeta = buildPersistMeta(finalMeta, {
          trace: accumulatedTrace,
          reasoning: accumulatedReasoning,
        });
        try {
          // Same contract as sendMessage / handleEditUser: backfill dbId on the
          // optimistic placeholder before refreshActivePath, or applyServerPath
          // will append the pending (no-dbId) bubble after the server copy.
          let savedAssistant = null;
          if (typeof assistantMsg.dbId === "number") {
            // OW-1: never in-place updateMessage — POST /branch sibling.
            savedAssistant = await persistRegeneratedAssistant({
              branchMessage: apiBranchMessage,
              authRequest,
              convId,
              targetMessageId: assistantMsg.dbId,
              content: finalText,
              traceId: finalMeta?.trace_id,
              latencyMs: finalMeta?.latency_ms,
              agentName: agentNameForPersist,
              metadata: persistMeta,
            });
          } else {
            // Unpersisted assistant (degraded mode): fall back to append.
            const parentId =
              typeof prevUser.dbId === "number" ? prevUser.dbId : undefined;
            savedAssistant = await apiAppendMessage(authRequest, convId, {
              role: "assistant",
              content: finalText,
              parentId,
              traceId: finalMeta?.trace_id,
              latencyMs: finalMeta?.latency_ms,
              agentName: agentNameForPersist,
              metadata: persistMeta,
            });
          }
          const reconciled = reconcilePersistedAssistant(savedAssistant, null);
          if (reconciled.ok) {
            updateMsg(convId, placeholderId, reconciled.patch);
            updateConv(convId, {
              activeLeafMessageId: reconciled.activeLeafMessageId,
            });
            branchPersisted = true;
          } else {
            // 2xx-without-id: keep streamed text, surface miss (no silent skip).
            setRuntimeError(reconciled.error?.message || "對話訊息儲存失敗");
            updateMsg(convId, placeholderId, {
              persistError: reconciled.notice,
            });
          }
        } catch (persistError) {
          // Designed 409 (sibling cap) after a successful stream must not leave
          // the previous answer gone behind a phantom unpersisted placeholder.
          setMessagesByConv((prev) => ({
            ...prev,
            [convId]: sanitizeRestoredMessages(preRegenList),
          }));
          setRuntimeError(persistError.message || "重試訊息儲存失敗");
        }
      }
    } finally {
      // Successful branch persist always refreshes, even if a later step throws.
      if (branchPersisted) {
        try {
          await refreshActivePath(convId);
        } catch (refreshError) {
          setRuntimeError(
            refreshError.message || "對話路徑重新載入失敗",
          );
        }
      }
    }
  }

  // ---- switch active branch via PUT /active-leaf (no optimistic mutation) ----
  async function switchBranch(msg, targetMessageId) {
    if (typeof targetMessageId !== "number") return;
    const convId = msg.conversationId;
    if (typeof convId !== "number") return;
    // Best-effort abort of an in-flight stream. An aborted sendMessage still
    // resolves and may persist; pagers/delete stay disabled via
    // conversationStreaming until streaming flags clear.
    stopStreaming(convId);
    try {
      const result = await switchBranchPath({
        setActiveLeaf: apiSetActiveLeaf,
        authRequest,
        convId,
        messageId: targetMessageId,
        prevList: messagesByConv[convId] || [],
        mapServerMessage,
      });
      // Wholesale replace from the server path response.
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: result.messages,
      }));
      updateConv(convId, { activeLeafMessageId: result.activeLeafMessageId });
    } catch (err) {
      setRuntimeError(err.message || "切換分支失敗");
    }
  }

  // ---- delete a message subtree (confirm → DELETE → rebuild path) ----
  async function deleteBranch(msg) {
    const convId = msg.conversationId;
    if (typeof convId !== "number" || typeof msg.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存至後端，無法刪除分支。");
      return;
    }
    if (!(await confirm({
      title: "刪除訊息分支",
      message: "確定要刪除此訊息及其後續內容？此動作無法復原。",
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    stopStreaming(convId);
    try {
      const path = await apiDeleteMessageBranch(authRequest, convId, msg.dbId);
      const mapped = (path.messages || []).map((m) => ({
        ...mapServerMessage(m),
        conversationId: convId,
      }));
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: applyServerPath(prev[convId] || [], mapped, convId),
      }));
      updateConv(convId, {
        activeLeafMessageId: path.active_leaf_message_id ?? null,
      });
    } catch (err) {
      setRuntimeError(err.message || "刪除訊息分支失敗");
    }
  }

  // ---- thumbs up / down (+ optional fine score) ----
  // Optimistically toggles rating locally for instant feedback, then PUTs to
  // the CSP rating endpoint. On failure the optimistic value is rolled back
  // so the UI never drifts from persisted state. Score is optional: pressing
  // a thumb alone is enough; picking 1–5 / 6–10 is a free follow-up.
  async function handleRate(targetMsg, nextRating, feedback = null) {
    const convId = targetMsg.conversationId;
    const prevRating = targetMsg.rating ?? null;
    const prevScore = targetMsg.ratingScore ?? null;
    const nextScore = feedback && Object.prototype.hasOwnProperty.call(feedback, "rating_score")
      ? feedback.rating_score
      : (nextRating === null || nextRating !== prevRating ? null : prevScore);
    updateMsg(convId, targetMsg.id, { rating: nextRating, ratingScore: nextScore });

    if (typeof convId !== "number" || typeof targetMsg.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存至後端，反饋僅保留於本地。");
      return;
    }
    try {
      await apiRateMessage(authRequest, convId, targetMsg.dbId, nextRating, feedback);
    } catch (err) {
      updateMsg(convId, targetMsg.id, { rating: prevRating, ratingScore: prevScore });
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
    setArtifact(null);

    await Promise.all(
      cols.map(async (col) => {
        const uId = makeId("u-" + col.id);
        const aId = makeId("a-" + col.id);
        const userMsg = {
          id: uId,
          role: "user",
          text,
          attachments,
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
          await streamWithAbort(col.id, {
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
                ? {
                    ...m,
                    streaming: false,
                    error: error.message || "產生回應時發生錯誤，請稍後再試。",
                  }
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

  // Promote the chosen compare column into a real server conversation.
  // Failures stay in compare mode and surface an error — never invent a
  // local-only row (that bypassed classification latch + audit).
  async function adoptColumn(col) {
    if (adoptInFlightRef.current) return;
    if (!isAuthenticated) {
      setRuntimeError("尚未登入，請重新登入後再試。");
      return;
    }
    const msgs = compareMsgs[col.id] || [];
    const agentName =
      agents.find((a) => a.id === col.agentId)?.name || col.agentId;
    adoptInFlightRef.current = true;
    try {
      const detail = await promoteAdoptedAnswer({
        authRequest,
        adoptConversation: apiAdoptConversation,
        agentId: col.agentId,
        agentDisplayName: agentName,
        msgs,
        makeTitle: makeConversationTitle,
      });
      if (!detail || typeof detail.id !== "number") {
        throw new Error("採用回答失敗：伺服器未回傳對話");
      }
      const lookupName = (id) =>
        agents.find((a) => a.id === id)?.name || agentName || null;
      const lookupRequiresEncryption = (id) =>
        Boolean(agents.find((a) => a.id === id)?.requiresEncryption);
      // Reflect server latch — do not invent classified client-side.
      const mapped = mapServerConversation(
        detail,
        lookupName,
        lookupRequiresEncryption,
      );
      const tags = Array.isArray(mapped.tags) ? [...mapped.tags] : [];
      if (!tags.includes("compared")) tags.push("compared");
      const convRow = { ...mapped, tags };
      setConversations((prev) => [
        convRow,
        ...prev.filter((c) => c.id !== detail.id),
      ]);
      const msgsMapped = (detail.messages || []).map((m) => ({
        ...mapServerMessage(m),
        conversationId: detail.id,
      }));
      setMessagesByConv((prev) => ({
        ...prev,
        [detail.id]: msgsMapped,
      }));
      setSelectedConvId(detail.id);
      setSelectedAgentId(col.agentId);
      exitCompare();
    } catch (error) {
      setRuntimeError(error.message || "採用回答失敗");
    } finally {
      adoptInFlightRef.current = false;
    }
  }

  // ---- misc handlers ----
  function newChat() {
    setSelectedConvId(null);
    setCitationsOpen(false);
    setArtifact(null);
    setPendingArtifactRevision(null);
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
    setArtifact(null);
    setCitationsOpen(true);
  }

  function clearPendingArtifactRevision(messageId) {
    setPendingArtifactRevision((cur) => {
      if (!cur) return null;
      if (messageId != null && cur.messageId !== messageId) return cur;
      return null;
    });
  }

  function onRevisionSettled(messageId) {
    clearPendingArtifactRevision(messageId);
  }

  function onOpenArtifact(next) {
    if (!next?.kind || typeof next.source !== "string") return;
    setCitationsOpen(false);
    setArtifact(next);
  }

  function onReviseArtifact(prompt, info) {
    if (isClassified) return;
    if (!prompt || typeof prompt !== "string") return;
    const kind = info?.kind || artifact?.kind;
    if (!kind) return;
    void sendMessage(prompt, [], { artifactRevision: { kind } });
  }

  function handoffToAgent(newAgentId) {
    if (!selectedConvId) return;
    const label =
      agents.find((a) => a.id === newAgentId)?.name || newAgentId;
    // 原本這一行寫的是助手 id,還掛著實作名稱的方括號前綴——而它會被存進
    // 逐字稿。顯示名稱優先,找不到才退回 id。
    const fromLabel =
      agents.find((a) => a.id === selectedAgentId)?.name || selectedAgentId;
    const sysMsg = {
      id: makeId("sys"),
      role: "assistant",
      text: handoffNotice(fromLabel, label),
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

  // Full-page forensic watermark: same 密／機密 threshold as ClassificationWatermark
  // (watermarkLevel). Reader comes from the signed-in user already held in auth —
  // no extra request. Time freezes per displayed conversation inside ConfidentialWatermark.
  const pageWatermarkLevel = watermarkLevel({
    classificationLevel: selectedConv?.classificationLevel,
    classified: isClassified,
  });
  const pageWatermarkReader = watermarkReaderLabel(user);
  const showForensicWatermark = Boolean(pageWatermarkLevel && pageWatermarkReader);

  // ---- render: classified watermark + top bar + messages + composer ----
  return (
    <ArtifactPreviewProvider
      artifact={artifact}
      onOpen={onOpenArtifact}
      pendingRevision={pendingArtifactRevision}
      onRevisionSettled={onRevisionSettled}
    >
    <div style={{ display: "flex", height: "100dvh", background: "var(--bg)", position: "relative" }}>
      <a className="skip-link" href="#shell-main">跳到主要內容</a>
      {showForensicWatermark && (
        <ConfidentialWatermark
          level={pageWatermarkLevel}
          reader={pageWatermarkReader}
          conversationId={selectedConvId}
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
            此對話因引用過往機敏記憶而升級為列管。
            刪除對話的機敏記憶引用可解除（設定 → 記憶）；
            一旦升級無法在此對話手動退回。
          </span>
        </div>
      )}
      <Sidebar
        conversations={conversations}
        onServerSearch={(q) => searchConversations(authRequest, q)}
        onExportConv={exportConversation}
        selectedConvId={selectedConvId}
        onSelectConv={(id) => {
          setSelectedConvId(id);
          setCitationsOpen(false);
          setArtifact(null);
          setPendingArtifactRevision(null);
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
        onOpenServices={() => {
          setUsageOpen(false);
          setServicesOpen(true);
        }}
        onOpenUsage={() => {
          setServicesOpen(false);
          setUsageOpen(true);
        }}
        onOpenMemory={() => {
          setServicesOpen(false);
          setUsageOpen(false);
          setSettingsTab("memory");
          setSettingsOpen(true);
        }}
        onTaskCenter={() => {
          setServicesOpen(false);
          setUsageOpen(false);
        }}
        currentNavId={
          usageOpen ? "usage" : settingsOpen && settingsTab === "memory" ? "memory" : "tasks"
        }
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        folder={folder}
        setFolder={setFolder}
        folders={folders}
        onCreateFolder={createFolder}
        onDeleteFolder={deleteFolder}
        onOpenTagEditor={(id, patch) => handleUpdateConvMeta(id, patch)}
        onRenameConv={handleRenameConv}
        onDeleteConv={handleDeleteConv}
      />

      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <BannerBar banners={visibleBanners} onDismiss={dismissBanner} />
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "10px 18px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
        }}>
          {tweaks.agentSwitcherPosition === "top" && !compareMode ? (
            <>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>助手</span>
              <AgentSelector agents={agents} value={selectedAgentId} onChange={setSelectedAgentId} />
            </div>
            {selectedAgentId === ROUTER_AGENT.id ? (
              <>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>模型</span>
              <RouterModelPicker
                models={routerModels}
                selectedId={selectedRouterModelId}
                defaultModelId={routerDefaultId}
                fallbackName={selectedConv?.routerModelName || ""}
                error={routerModelError}
                disabled={routerPickerLocked}
                onChange={async (id) => {
                  const previous = selectedRouterModelId;
                  setSelectedRouterModelId(id);
                  setRouterModelError("");
                  const convId = selectedConvId;
                  const conv = conversations.find((c) => c.id === convId);
                  if (typeof convId === "number" && conv) {
                    try {
                      const saved = await apiSetConversationRouterModel(authRequest, convId, {
                        routerModelId: id,
                        expectedVersion: conv.routerSelectionVersion || 0,
                      });
                      setConversations((prev) => prev.map((row) => row.id === convId ? {
                        ...row,
                        ...conversationSelectionFromServer(saved),
                      } : row));
                      if (selectedConvId === convId) setSelectedRouterModelId(saved.router_model_id);
                    } catch (err) {
                      setSelectedRouterModelId(previous);
                      setRouterModelError(err?.message || "無法保存對話模型");
                      try {
                        const fresh = await apiGetConversation(authRequest, convId);
                        applySelectionFromServer(convId, fresh);
                      } catch (_) { /* keep previous picker */ }
                    }
                  }
                }}
              />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>思考</span>
              <ThinkingPicker
                value={thinkingTier}
                model={selectedThinkingModel}
                error={thinkingError}
                disabled={routerPickerLocked}
                onChange={persistThinkingTier}
              />
              </div>
              </>
            ) : null}
            </>
          ) : (
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600, fontSize: 14, minWidth: 0 }}>
              {selectedConv?.classified && <IconLock size={14} style={{ color: "var(--danger)" }} />}
              <span
                style={{ minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                title={compareMode ? undefined : selectedConv?.title || undefined}
              >
                {compareMode
                  ? "比較模式"
                  : selectedConv?.title || "新對話"}
              </span>
            </div>
          )}

          <div style={{ flex: 1 }} />

          {selectedConv && !compareMode && (
            <>
              {typeof selectedConvId === "number" && convUsage ? (
                <ConversationUsageChip usage={convUsage} />
              ) : null}
              <button
                type="button"
                aria-label="整理對話"
                title="整理對話"
                disabled={
                  compacting
                  || typeof selectedConvId !== "number"
                  || currentMsgs.some((m) => m.streaming)
                }
                onClick={handleCompactConversation}
                style={{
                  fontSize: 11,
                  color: "var(--fg-muted)",
                  background: "var(--bg-elev)",
                  border: "1px solid var(--border)",
                  borderRadius: 999,
                  padding: "3px 9px",
                  cursor: currentMsgs.some((m) => m.streaming) || compacting
                    ? "not-allowed"
                    : "pointer",
                  opacity: currentMsgs.some((m) => m.streaming) || compacting ? 0.45 : 1,
                }}
              >
                整理對話
              </button>
              {selectedConv.classified && (
                <span
                  title="此對話已鎖為列管（由後端依 agent 預設分類等級強制啟用）。"
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
                  <IconLock size={11} /> 列管模式
                </span>
              )}
              <ClassificationLevelBadge conversation={selectedConv} />
              {showForensicWatermark && (
                <span
                  data-watermark-disclaimer=""
                  style={{
                    fontSize: 11,
                    color: "var(--fg-muted)",
                    lineHeight: 1.35,
                    maxWidth: 260,
                  }}
                >
                  {WATERMARK_DISCLAIMER}
                </span>
              )}
              {/* Slice 9a — Task result 可轉 artifact（doc 10 §11）：對話已建立
                  Task 時，提供薄連結深連到知識 SPA 的 Studio 面，帶 taskId
                  query 讓 ALM 承接；不在 shell 內另建 Studio 啟動器。
                  本 release ANILA LM 關閉時一併隱藏（同一旗標 anilalmReleaseGate）。 */}
              {ANILA_LM_ENTRY_ENABLED && selectedConv?.taskId != null && (
                <a
                  href={originHref(`/anilalm?taskId=${encodeURIComponent(selectedConv.taskId)}`)}
                  title="將此任務結果轉為產出（Studio / artifact）"
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 5,
                    padding: "4px 10px", fontSize: 12, fontWeight: 500,
                    background: "var(--bg-elev)", border: "1px solid var(--border)",
                    borderRadius: "var(--radius)", color: "var(--fg)", textDecoration: "none",
                  }}
                >
                  <IconSpark size={13} /> 產出
                </a>
              )}
              <Dropdown align="right" width={260} trigger={() => (
                <IconButton title="交給其他助手"><IconNodes size={14} /></IconButton>
              )}>
                {(close) => (
                  <HandoffMenu
                    agents={agents}
                    currentAgentId={selectedAgentId}
                    onHandoffAgent={handoffToAgent}
                    close={close}
                  />
                )}
              </Dropdown>
              <IconButton
                title={
                  selectedConv.classified
                    ? classifiedShareDenial(selectedConv.classificationLevel)
                    : "分享"
                }
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
              流程，使用者沒有也不該管理 key；SDK 使用者仍可從 control
              plane 取得 sk-* 並用 Authorization header 呼叫。 */}

          <IconButton title="設定" onClick={() => { setSettingsTab("general"); setSettingsOpen(true); }}>
            <IconSettings />
          </IconButton>
          <IconButton
            title={tweaks.dark ? "切換淺色" : "切換深色"}
            onClick={() => setTweaks({ ...tweaks, dark: !tweaks.dark })}
          >
            {tweaks.dark ? <IconSun /> : <IconMoon />}
          </IconButton>
          <span style={{ position: "relative", display: "inline-flex" }}>
            <IconButton title="新功能" onClick={() => { setChangelogOpen(true); try { localStorage.setItem("anila-changelog-seen", CHANGELOG_VERSION); } catch {} setChangelogUnseen(false); }}>
              <IconGift />
            </IconButton>
            {changelogUnseen && (
              <span style={{
                position: "absolute", top: 4, right: 4, width: 7, height: 7,
                borderRadius: "50%", background: "var(--accent)", pointerEvents: "none",
              }} />
            )}
          </span>
        </div>

        {runtimeError && (
          <div role="alert" aria-live="assertive" style={{
            padding: "8px 18px",
            background: "oklch(0.97 0.03 25)",
            borderBottom: "1px solid oklch(0.88 0.08 25)",
            color: "var(--danger)",
            fontSize: 12, fontFamily: "var(--font-mono)",
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
          }}>
            <span>{runtimeError}</span>
            <button
              onClick={() => setRuntimeError("")}
              aria-label="關閉錯誤訊息"
              style={{ background: "none", border: "none", color: "var(--danger)", cursor: "pointer", fontSize: 14, lineHeight: 1, padding: 2 }}
            >
              ✕
            </button>
          </div>
        )}

        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          <div id="shell-main" tabIndex={-1} style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, scrollMarginTop: 80 }}>
            {compareMode ? (
              <ParallelCompareView
                agents={directAgents}
                columns={compareColumns}
                setColumns={setCompareColumns}
                messagesByColumn={compareMsgs}
                onSend={(text, atts, meta) => sendCompare(text, atts, meta)}
                redactionMode={redactionMode}
                onChangeRedactionMode={setRedactionMode}
                onExit={exitCompare}
                onAdoptColumn={adoptColumn}
                AgentSelector={AgentSelector}
                Composer={Composer}
                MessageBubble={MessageBubble}
              />
            ) : (
              <>
                <div ref={scrollRef} style={{
                  flex: 1, overflowY: "auto", background: "var(--bg)",
                  display: "flex", flexDirection: "column",
                }}>
                  <div style={{
                    maxWidth: 760, margin: "0 auto", width: "100%",
                    padding: `calc(var(--density) * 1.2) var(--density)`,
                    ...(currentMsgs.length === 0
                      ? { flex: 1, display: "flex", flexDirection: "column", justifyContent: "center" }
                      : {}),
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
                        <React.Fragment key={m.id}>
                          {selectedConv?.compactSummary
                            && compactBoundaryOnPath(currentMsgs, selectedConv.compactBoundaryMessageId)
                            && m.dbId === selectedConv.compactBoundaryMessageId ? (
                            <CompactBoundaryBanner
                              summary={selectedConv.compactSummary}
                              onRestore={restoreFullContext}
                            />
                          ) : null}
                          <MessageBubble
                            msg={m}
                            agents={agents}
                            conversationId={selectedConvId}
                            classified={isClassified}
                            classificationLevel={selectedConv?.classificationLevel}
                            onRegenerate={regenerateMessage}
                            onRate={handleRate}
                            onEditUser={handleEditUser}
                            onSwitchBranch={switchBranch}
                            onDeleteBranch={deleteBranch}
                            onOpenCitation={onOpenCitation}
                            onPickFollowUp={(q) => sendMessage(q, [], {})}
                            messageActions={customActions}
                            onAction={runMessageAction}
                            onContinue={continueMessage}
                            conversationStreaming={currentMsgs.some((x) => x.streaming)}
                          />
                        </React.Fragment>
                      ))
                    )}
                  </div>
                </div>

                <div style={{ padding: "0 var(--density) var(--density)", background: "var(--bg)" }}>
                  <div style={{ maxWidth: 760, margin: "0 auto" }}>
                    {tweaks.agentSwitcherPosition === "bottom" && (
                      <div style={{ marginBottom: 8, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                        <>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>助手</span>
              <AgentSelector agents={agents} value={selectedAgentId} onChange={setSelectedAgentId} />
            </div>
            {selectedAgentId === ROUTER_AGENT.id ? (
              <>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>模型</span>
              <RouterModelPicker
                models={routerModels}
                selectedId={selectedRouterModelId}
                defaultModelId={routerDefaultId}
                fallbackName={selectedConv?.routerModelName || ""}
                error={routerModelError}
                disabled={routerPickerLocked}
                onChange={async (id) => {
                  const previous = selectedRouterModelId;
                  setSelectedRouterModelId(id);
                  setRouterModelError("");
                  const convId = selectedConvId;
                  const conv = conversations.find((c) => c.id === convId);
                  if (typeof convId === "number" && conv) {
                    try {
                      const saved = await apiSetConversationRouterModel(authRequest, convId, {
                        routerModelId: id,
                        expectedVersion: conv.routerSelectionVersion || 0,
                      });
                      setConversations((prev) => prev.map((row) => row.id === convId ? {
                        ...row,
                        ...conversationSelectionFromServer(saved),
                      } : row));
                      if (selectedConvId === convId) setSelectedRouterModelId(saved.router_model_id);
                    } catch (err) {
                      setSelectedRouterModelId(previous);
                      setRouterModelError(err?.message || "無法保存對話模型");
                      try {
                        const fresh = await apiGetConversation(authRequest, convId);
                        applySelectionFromServer(convId, fresh);
                      } catch (_) { /* keep previous picker */ }
                    }
                  }
                }}
              />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: 11, color: "var(--fg-muted)" }}>思考</span>
              <ThinkingPicker
                value={thinkingTier}
                model={selectedThinkingModel}
                error={thinkingError}
                disabled={routerPickerLocked}
                onChange={persistThinkingTier}
              />
              </div>
              </>
            ) : null}
            </>
                        {activeEncryptionRequired && (
                          <span title="此 agent 為列管模型（受控存取）" style={{
                            display: "inline-flex", alignItems: "center", gap: 3,
                            padding: "1px 7px",
                            background: "oklch(0.95 0.02 25 / 0.4)",
                            border: "1px solid var(--danger)",
                            borderRadius: 999,
                            fontSize: 11, color: "var(--danger)",
                            fontFamily: "var(--font-mono)",
                          }}>
                            <IconLock size={10} /> 列管模型
                          </span>
                        )}
                      </div>
                    )}
                    <Composer
                      onSend={sendMessage}
                      agents={agents}
                      redactionMode={redactionMode}
                      onChangeRedactionMode={setRedactionMode}
                      conversationId={selectedConvId}
                      presetPrompts={presetPrompts}
                      deepThinkNext={deepThinkNext}
                      onDeepThinkNextChange={setDeepThinkNextFlag}
                      streaming={currentMsgs.some((m) => m.streaming)}
                      // 「停止產生」= 這個對話現在不要再產生了,包含還在排隊、
                      // 串流尚未開始的那幾輪。只 abort 當下註冊的那一個
                      // controller 的話,第一輪停掉、第二輪立刻接著跑,而按鈕
                      // 仍然顯示「停止產生」—— 使用者按了、畫面照樣在動。
                      onStop={() =>
                        stopStreaming(selectedConvId, { cancelQueued: true })
                      }
                      placeholder="傳訊息給 ANILA，Shift+Enter 換行"
                      footer={
                        selectedAgentId === ROUTER_AGENT.id
                          ? "ANILA 會幫你找合適的助手"
                          : `已指定助手 · ${activeAgent.name}`
                      }
                      onUpload={async (file) => {
                        let convId = selectedConvId;
                        if (typeof convId !== "number") {
                          convId = await ensureConversation(
                            (file && file.name) || "新對話",
                            selectedAgentId,
                          );
                        }
                        if (typeof convId !== "number") {
                          throw new Error("無法建立對話，附件未上傳");
                        }
                        return apiUploadAttachment(multipartRequest, file, {
                          conversationId: convId,
                        });
                      }}
                      onFetchAttachmentMeta={(referenceId) =>
                        apiGetAttachmentMeta(authRequest, referenceId)
                      }
                    />
                    <div style={{
                      marginTop: 6, fontSize: 11,
                      color: "var(--fg-subtle)", textAlign: "center",
                      fontFamily: "var(--font-mono)",
                    }}>
                      ANILA {activeAgent?.id === ROUTER_AGENT.id
                        ? "會幫你找合適的助手"
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
          {artifact && !compareMode && (
            <ArtifactPanel
              artifact={artifact}
              classified={isClassified}
              classificationLevel={selectedConv?.classificationLevel}
              onClose={() => setArtifact(null)}
              onRevise={onReviseArtifact}
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
        redactionMode={redactionMode}
        onChangeRedactionMode={setRedactionMode}
        onOpenConversation={(id) => {
          if (id == null) return;
          setSettingsOpen(false);
          setSelectedConvId(id);
          setPendingArtifactRevision(null);
        }}
      />

      <ChangelogModal open={changelogOpen} onClose={() => setChangelogOpen(false)} />

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
        onCreateShare={async ({
          targetUsername,
          targetDepartmentName,
          mode,
          allowFork,
          expiresAt,
        }) => {
          if (!selectedConvId || typeof selectedConvId !== "number") {
            throw new Error("尚未建立後端對話 — 請先送出第一則訊息");
          }
          return apiCreateShare(authRequest, selectedConvId, {
            targetUsername,
            targetDepartmentName,
            mode,
            allowFork,
            expiresAt,
          });
        }}
        onListShares={() =>
          (typeof selectedConvId === "number")
            ? apiListShares(authRequest, selectedConvId)
            : Promise.resolve([])
        }
        onRevokeShare={(shareId) => apiRevokeShare(authRequest, selectedConvId, shareId)}
      />

      <ServicesPanel
        open={servicesOpen}
        onClose={() => setServicesOpen(false)}
        request={authRequest}
        toast={toast}
      />

      <UsagePage
        open={usageOpen}
        onClose={() => setUsageOpen(false)}
        request={authRequest}
      />
    </div>
    </ArtifactPreviewProvider>
  );
}

// ---- Empty state -----------------------------------------------------------
export function EmptyState({ agent, agents, onPick, loading }) {
  const prompts = buildStarterPrompts(agents);
  return (
    <div style={{ padding: "48px 12px 24px", textAlign: "center" }}>
      <AnilaLogoVideo width={180} />
      <div style={{ marginTop: 16, fontSize: 22, fontWeight: 600, letterSpacing: -0.2 }}>
        你今天想問 ANILA 什麼？
      </div>
      <div style={{ marginTop: 6, color: "var(--fg-muted)", fontSize: 13 }}>
        {loading
          ? "agent 清單載入中…"
          : agent?.id === ROUTER_AGENT.id
            ? "輸入問題，ANILA 會幫你找合適的助手；也可以用 @名稱 直接指定"
            : `當前助手： ${agent?.name}`}
      </div>
      <div style={{
        marginTop: 36, display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))",
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

function SettingsModal({
  open, tab, setTab, onClose,
  user, agents, authRequest,
  redactionMode, onChangeRedactionMode,
  onOpenConversation,
}) {
  return (
    <Modal open={open} onClose={onClose} title="設定" subtitle="顯示、隱私與帳號" width={680}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, paddingBottom: 8, borderBottom: "1px solid var(--border)" }}>
          {[
            { id: "general", label: "一般",       icon: <IconSettings size={13} /> },
            { id: "privacy", label: "隱私 / 信任", icon: <IconShield   size={13} /> },
            { id: "memory",  label: "記憶",        icon: <IconHistory  size={13} /> },
            { id: "account", label: "帳號",        icon: <IconUser     size={13} /> },
            { id: "about",   label: "關於",        icon: <AnilaLogoImg variant="mark" height={13} /> },
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
                <div style={{ fontSize: 13, fontWeight: 500 }}>預設助手</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 4 }}>
                  目前由 /v1/agents 動態載入，共 {Math.max(agents.length - 1, 0)} 個可用助手。
                  切換預設助手 請從主介面的 agent selector 進行。
                </div>
              </div>
              <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>
                列管等級由 agent 的預設分類等級（無機密／營業秘密／密／機密）決定；對話一旦升至較高等級即單向鎖定，使用者無法手動切換或降級。
              </div>
            </div>
          )}

          {/* Sprint 7 X follow-up：apikey tab 已下線 — SPA 不再持有 key。 */}

          {tab === "privacy" && (
            <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>敏感資訊處理</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  平台只負責提醒你草稿裡可能有什麼，不會替你改寫內容：送給模型的文字、以及存下來的對話紀錄，都是你打的原文（只去掉頭尾的空白），中間一字不改。送出去之後就收不回來，要不要送由你決定。
                </div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6, marginTop: 6 }}>
                  下面選的模式會存在你的帳號下，換一台機器登入同一張卡也會保留。這是你自己的偏好，沒有管理員替你設定過，你隨時可以改回來。
                </div>
                {/* ⚠ 這組按鈕是這個設定在整個平台上**唯一永遠到得了**的入口。
                    在它之前,唯一能改模式的地方是輸入框上方那條提示列 —— 而那條
                    提示列只在草稿裡剛好偵測到個資時才出現。於是被「阻擋」擋下來的
                    人(尤其是從範本或重試被擋的,輸入框根本是空的)看不到任何開關,
                    而那個偏好現在還是跨機器長期保存的:幾週前在別台機器設的,
                    今天在這裡把自己鎖在門外。擋人的控制項一定要有一條自救的路。 */}
                <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  {REDACTION_MODES.map((m) => (
                    <button
                      key={m}
                      onClick={() => onChangeRedactionMode?.(m)}
                      aria-pressed={redactionMode === m}
                      style={{
                        padding: "4px 10px",
                        fontSize: 11, fontFamily: "var(--font-mono)",
                        background: redactionMode === m ? "var(--bg-subtle)" : "transparent",
                        border: "1px solid " + (redactionMode === m ? "var(--border-strong)" : "var(--border)"),
                        borderRadius: 4, cursor: "pointer", color: "var(--fg)",
                      }}
                    >{m === "warn" ? "偵測個資時提醒" : "偵測個資時阻止送出"}</button>
                  ))}
                </div>
                <div style={{ fontSize: 10, color: "var(--fg-subtle)", lineHeight: 1.6, marginTop: 6 }}>
                  偵測只認得它知道的那幾種形狀（身分證、電話、Email、信用卡、
                  API 金鑰、權杖、私密金鑰、密碼），認不出來的不代表沒有，認出來的也可能認錯。
                  「偵測個資時提醒」會先告訴你再照樣送出；「偵測個資時阻止送出」只攔個資。
                  金鑰、權杖、密碼仍只提醒、不會擋送出。
                </div>
              </div>
              <div>
                <div style={{ fontWeight: 500, marginBottom: 4 }}>列管對話</div>
                <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
                  若指定的 agent 預設分類等級高於無機密，此對話會依該等級列管（受控存取）：
                  密／機密禁止分享與匯出，營業秘密可分享但會落稽核，並加上浮水印。此狀態無法由使用者解除。
                </div>
              </div>
            </div>
          )}

          {tab === "memory" && (
            <MemoryTab authRequest={authRequest} onOpenConversation={onOpenConversation} />
          )}

          {tab === "account" && (
            <div style={{ fontSize: 13 }}>
              <div style={{ marginBottom: 4 }}><b>{user?.username}</b></div>
              <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>
                角色：{user?.role || "user"}
              </div>
            </div>
          )}

          {tab === "about" && (
            <div style={{ fontSize: 13, lineHeight: 1.7 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                <AnilaLogoImg variant="logo" height={32} />
                <div style={{ fontSize: 16, fontWeight: 600 }}>ANILA Runtime Client</div>
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-muted)" }}>
                v{typeof __ANILA_VERSION__ !== "undefined" ? __ANILA_VERSION__ : "dev"} · ANILA 對話介面
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
