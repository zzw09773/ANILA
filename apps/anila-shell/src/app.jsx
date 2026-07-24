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
import { executionCallbacks, reduceExecution } from "./runtime/executionReducer.js";
import {
  cancelTaskExecution,
  createTaskForConversation,
  shouldAbortAfterCancellation,
} from "./runtime/tasks.js";
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
// 對話 origin scope + 「開啟前必須先 hydrate 完整 conversation」的判準。
import {
  OUT_OF_SCOPE_MESSAGE,
  filterShellScopedRows,
  isConversationHydrated,
  renderableMessages,
  resolveConversationOpen,
} from "./runtime/convScope.js";
import { sanitizeUserTags, tagRejectionMessage } from "./runtime/tagRules.js";
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
  listShares as apiListShares,
  revokeShare as apiRevokeShare,
  buildShareUrl,
  uploadAttachment as apiUploadAttachment,
  createHandoff as apiCreateHandoff,
  listAgentFunctions as apiListAgentFunctions,
  getUiSettings,
  putUiSettings,
  searchConversations,
  listActiveBanners as apiListActiveBanners,
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
import { useConfirm, useToast } from "./confirm.jsx";
// 殼層佈局元件（共用設計系統）：AppShell = 全高 flex 容器（overlay/側欄/主欄
// 三個 slot），Topbar = 主欄頂部工具列外殼。
import { AppShell, Topbar } from "@anila/ui";
import {
  AnilaGlyph,
  IconArchive,
  IconColumns,
  IconGrid,
  IconKeyboard,
  IconPlus,
  IconHistory,
  IconLock,
  IconMoon,
  IconNodes,
  IconRefresh,
  IconSettings,
  IconShare,
  IconShield,
  IconSpark,
  IconGift,
  IconSun,
  IconTrash,
  IconUser,
} from "./icons.jsx";
import { BUILTIN_FOLDER_IDS, DEFAULT_FOLDERS } from "./data.jsx";
// 全域導覽:命令面板(⌘K)、快捷鍵面板(⌘/)與集中式 shortcuts registry。
import { CommandPalette } from "./commands/CommandPalette.jsx";
import { ShortcutsPanel } from "./commands/ShortcutsPanel.jsx";
import { useShortcuts } from "./commands/useShortcuts.js";
import { DEFAULT_MESSAGE_ACTIONS, actionTemplate } from "./commands/promptActions.js";
import { formatShortcut, getShortcut, isMacPlatform } from "./commands/shortcuts.js";
import {
  CitationsDrawer,
  ConfidentialWatermark,
  ClassificationLevelBadge,
} from "./trust.jsx";
import { ParallelCompareView } from "./multiagent.jsx";
import { HandoffMenu, ShareDialog } from "./collab.jsx";
import { TweaksPanel } from "./tweaks.jsx";
import { applyTweaks } from "./tweakRuntime.js";
import { ChangelogModal, CHANGELOG_VERSION } from "./changelog.jsx";
import { BannerBar } from "./banners.jsx";
import { TraceExplorer } from "./spanTree.jsx";
import { ServicesPanel } from "./services.jsx";
import { originHref } from "./shellNav.jsx";

// ---- Router pseudo-agent ----------------------------------------------------
const ROUTER_AGENT = Object.freeze({
  id: "anila-router",
  name: "ANILA Router",
  short: "auto",
  description: "自動路由：由 Router 決定直接回答或分派給合適的 agent。",
  requiresEncryption: false,
});

// Default starter — a single card that asks the Router to introduce ANILA
// and explain how it decides between answering directly and dispatching to
// an agent. The copy is grounded: it names the agents the shell already
// knows are registered (when any) instead of baiting the model to
// "list every agent", which previously invited fabricated agent names.
// The Router's own direct-answer prompt is separately pinned to the real
// registry, so the answer stays truthful even for an empty registry.
function buildStarterPrompts(agents) {
  const real = (agents || []).filter((a) => a.id !== ROUTER_AGENT.id);
  const names = real
    .map((a) => a.name || a.id)
    .filter(Boolean);
  const hasAgents = names.length > 0;
  const sub = hasAgents
    ? `目前已註冊 ${names.length} 個 agent，點一下讓 Router 介紹平台並說明它如何決定直接回答或派工`
    : "目前尚未註冊 agent，點一下讓 Router 介紹平台並說明它的運作方式";
  const registeredClause = hasAgents
    ? `目前實際註冊的 agent 有：${names.join("、")}，請據實說明它們各自能解決的問題`
    : "並據實說明目前是否已註冊任何 agent（若沒有，請直接說明由 Router 回答）";
  return [
    {
      title: "ANILA 可以做什麼？",
      sub,
      q: `請介紹 ANILA 這個平台能做什麼，並說明 Router 會如何依我的問題決定「直接回答」或「派工給合適的 agent」。${registeredClause}。`,
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
  const confirm = useConfirm();
  const toast = useToast();

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
  // 專案入口（Service Platform）overlay。
  const [servicesOpen, setServicesOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [folder, setFolder] = useState("all");
  // 全域導覽 overlay:命令面板(⌘K)與快捷鍵面板(⌘/)。
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

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

  // convMeta:對話層級的 UI 組織資料 —— 封存(archived)與使用者標籤(tags)。
  // 刻意**不動後端 schema**:跟 folders 走同一層 server-synced UI 設定
  // (users.ui_settings 是 client 擁有形狀的 opaque JSON blob,見 CSP
  // /api/users/me/ui-settings)。形狀 { "<convId>": { archived?, tags? } };
  // 空值會被清掉,避免 blob 無限長大(後端上限 256KB)。
  const [convMeta, setConvMeta] = useState({});

  // Server-synced settings:後端是 source of truth(共用工作站下使用者的資料夾
  // 不會殘留在瀏覽器給下一個人看到)。掛載時抓後端覆寫;之後變動 debounce 存回。
  // localStorage 仍寫(離線/載入前的暫存),但後端值優先。
  const uiSettingsLoadedRef = useRef(false);
  // PUT 是「整包取代」語意 → 保留載入時看到的其他鍵,只覆寫我們自己管的欄位。
  const uiSettingsBlobRef = useRef({});
  useEffect(() => {
    if (!isAuthenticated) return;
    let alive = true;
    getUiSettings(authRequest)
      .then((res) => {
        const s = res?.ui_settings || {};
        if (!alive) return;
        uiSettingsBlobRef.current = s && typeof s === "object" ? s : {};
        if (Array.isArray(s.folders) && s.folders.length > 0) {
          setFolders(s.folders.filter((f) => f && typeof f.id === "string" && typeof f.name === "string"));
        }
        if (s.convMeta && typeof s.convMeta === "object" && !Array.isArray(s.convMeta)) {
          setConvMeta(s.convMeta);
        }
      })
      .catch(() => { /* 後端無設定 → 維持 localStorage 值 */ })
      .finally(() => { uiSettingsLoadedRef.current = true; });
    return () => { alive = false; };
  }, [isAuthenticated, authRequest]);

  // ui_settings 是「整包取代」語意的 opaque blob(後端沒有 PATCH / CAS)。
  // 多分頁同時寫會互相覆蓋 —— 本輪不動後端,緩解方式是「寫入前重新 GET,把
  // 別的分頁寫進去的其他鍵合併回來」;folders / convMeta 是本頁擁有的欄位,
  // 仍以本地為準。殘留風險:GET 與 PUT 之間仍是 last-write-wins。
  //
  // 失敗一律外顯。過去 `.catch(() => {})` 把 413(blob 超過後端 256KB 上限)
  // 吞掉,使用者以為存好了,實際上之後每一次變更都沒生效。
  const persistUiSettings = useCallback(
    async (patch) => {
      let base = uiSettingsBlobRef.current;
      try {
        const res = await getUiSettings(authRequest);
        const remote = res?.ui_settings;
        if (remote && typeof remote === "object" && !Array.isArray(remote)) {
          base = remote;
        }
      } catch {
        /* 讀不到遠端就用手上這份 baseline,至少不會弄丟本地變更 */
      }
      const next = { ...base, ...patch };
      try {
        await putUiSettings(authRequest, next);
        uiSettingsBlobRef.current = next;
        return true;
      } catch (error) {
        const status = error?.status;
        setRuntimeError(
          status === 413
            ? "個人設定（資料夾／標籤）已超過後端 256KB 上限，這次變更沒有存到雲端。請刪掉一些標籤或資料夾後再試。"
            : `個人設定同步失敗：${error?.message || "未知錯誤"}（這次變更只存在本機）`,
        );
        return false;
      }
    },
    [authRequest],
  );

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem("anila-folders", JSON.stringify(folders));
    } catch {
      /* quota / private mode — fall back silently */
    }
    // 載入後才回存後端(避免用初始 localStorage 值蓋掉後端真值)。debounce。
    if (!uiSettingsLoadedRef.current || !isAuthenticated) return;
    const t = setTimeout(() => {
      void persistUiSettings({ folders, convMeta });
    }, 600);
    return () => clearTimeout(t);
  }, [folders, convMeta, isAuthenticated, persistUiSettings]);

  // ---- 封存 / 標籤 ---------------------------------------------------------
  const patchConvMeta = useCallback((convId, patch) => {
    setConvMeta((prev) => {
      const key = String(convId);
      const merged = { ...(prev[key] || {}), ...patch };
      if (!merged.archived) delete merged.archived;
      if (!Array.isArray(merged.tags) || merged.tags.length === 0) delete merged.tags;
      const next = { ...prev };
      if (Object.keys(merged).length === 0) delete next[key];
      else next[key] = merged;
      return next;
    });
  }, []);

  // 對話列表視圖:把持久化的封存 / 標籤疊到 server 來的列上。自動標籤
  // (classified / compared,由 conversation 列本身帶)保持原樣,使用者標籤
  // 疊加在後 —— 因此使用者無法藉「刪標籤」把 classified 標記弄掉(更嚴,不弱化)。
  const conversationsView = useMemo(
    () =>
      conversations.map((c) => {
        const meta = convMeta[String(c.id)];
        if (!meta) return c;
        const userTags = Array.isArray(meta.tags) ? meta.tags : [];
        return {
          ...c,
          archived: Boolean(meta.archived),
          tags: userTags.length > 0
            ? [...new Set([...(c.tags || []), ...userTags])]
            : (c.tags || []),
        };
      }),
    [conversations, convMeta],
  );

  // 匯出對話為 JSON / Markdown(純前端,離線可用)。未載入的對話先抓訊息。
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

  // 封存 / 取消封存單一對話。封存當前開啟的對話 → 退回新對話畫面(對齊
  // ChatGPT:封存後它就不該再佔著主視圖)。後端對話本身不受影響。
  const archiveConversation = useCallback((convId, archived) => {
    patchConvMeta(convId, { archived: Boolean(archived) });
    if (archived) {
      setSelectedConvId((current) => (current === convId ? null : current));
    }
  }, [patchConvMeta]);

  // 側欄 TagEditor 的 patch 分流:tags → convMeta(持久化);folder / starred
  // 維持既有的 in-memory updateConv 行為,不變。
  const organizeConversation = useCallback((convId, patch) => {
    if (!patch) return;
    if (Object.prototype.hasOwnProperty.call(patch, "tags")) {
      const { tags, ...rest } = patch;
      const autoTags = conversations.find((c) => c.id === convId)?.tags || [];
      // 數量 / 長度上限在這裡收斂(TagEditor 也擋一次,這是後線)。超限一律
      // 告訴使用者被略過了哪些 —— 靜默截斷會讓人以為標籤存進去了。
      const { tags: userTags, rejected } = sanitizeUserTags(tags, autoTags);
      const warning = tagRejectionMessage(rejected);
      if (warning) toast(warning, { tone: "error" });
      patchConvMeta(convId, { tags: userTags });
      if (Object.keys(rest).length > 0) updateConv(convId, rest);
      return;
    }
    updateConv(convId, patch);
  }, [conversations, patchConvMeta, toast]);

  // 伺服器全文搜尋的結果也要帶上封存 / 標籤 meta,否則「已封存」的舊對話會
  // 從搜尋結果漏回主清單。
  //
  // ⚠ 安全:`/api/conversations/search` **不吃 origin 參數**,會把 ANILALM
  // (知識庫 SPA)的對話一起回來,而側欄清單是 `exclude_origin=anilalm`。
  // 不過濾的話,使用者能從搜尋結果打開一則本地清單根本沒有的對話 →
  // selectedConv=null → isClassified=false → 機密內容以未分類姿態渲染。
  // 這是第一道防線;第二道在 openConversation(hydrate 時再驗一次 origin)。
  //
  // 同時把後端 snake_case 正規化成面板讀的 camelCase —— 過去 `updated_at`
  // 沒轉,面板讀 `updatedAt` 取到 undefined,伺服器結果一律排到最後並顯示
  // 「剛剛」。
  const serverSearchWithMeta = useCallback(
    (q) =>
      searchConversations(authRequest, q).then((rows) =>
        filterShellScopedRows(rows).map((row) => {
          const meta = convMeta[String(row.id)] || {};
          return {
            id: row.id,
            title: row.title,
            snippet: row.snippet || "",
            origin: row.origin ?? null,
            agentId: row.agent_id ?? null,
            classified: Boolean(row.classified),
            classificationLevel: row.classification_level,
            classificationInherited: Boolean(row.classification_inherited),
            createdAt: row.created_at || null,
            updatedAt: row.updated_at || row.created_at || null,
            archived: Boolean(meta.archived),
            tags: Array.isArray(meta.tags) ? meta.tags : [],
          };
        }),
      ),
    [authRequest, convMeta],
  );

  // ---- 全域快捷鍵(集中式 registry → 單一 window listener)-------------------
  // 只有 registry 裡 global:true 的條目會被攔(全是 mod 系組合鍵),因此
  // 焦點在輸入框內也能觸發,而純 Enter / Esc 等既有區域行為完全不受影響。
  useShortcuts({
    "command-palette": () => setPaletteOpen((open) => !open),
    "shortcuts-panel": () => setShortcutsOpen((open) => !open),
    "new-chat": () => { setPaletteOpen(false); newChat(); },
  });

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
  async function streamWithAbort(convId, opts) {
    const controller = new AbortController();
    streamAbortRef.current.set(convId, controller);
    try {
      return await streamChatCompletion({
        // 對話已綁 Task 時所有後續 chat 呼叫(送出/編輯/重試)自動帶上;
        // 呼叫端可用 opts.taskId 覆寫(sendMessage 首回合的 state 尚未落地)。
        taskId: taskIdForConv(convId),
        ...opts,
        signal: controller.signal,
      });
    } finally {
      streamAbortRef.current.delete(convId);
    }
  }
  async function stopStreaming(convId) {
    const controller = streamAbortRef.current.get(convId);
    if (!controller) return;
    const taskId = taskIdForConv(convId);
    const cancellation = taskId !== null
      ? await cancelTaskExecution(taskId)
      : { accepted: false, status: "no_task" };
    // A delivered signal lets CSP close the downstream Agent and emit the
    // single trusted cancelled terminal.  Idempotent retries report
    // ``accepted=true, status=cancellation_in_progress`` and must follow the
    // same path; Abort is only the legacy/failure fallback where no live Task
    // stream was registered.
    if (shouldAbortAfterCancellation(cancellation)) controller.abort();
  }
  function timelineCallbacks(convId, messageId, { compare = false } = {}) {
    return executionCallbacks((action) => {
      const setter = compare ? setCompareMsgs : setMessagesByConv;
      setter((prev) => ({
        ...prev,
        [convId]: (prev[convId] || []).map((message) =>
          message.id === messageId ? reduceExecution(message, action) : message,
        ),
      }));
    });
  }

  const selectedConv = useMemo(
    () => conversations.find((c) => c.id === selectedConvId) || null,
    [conversations, selectedConvId],
  );
  // ⚠ 涉密安全閘門(見 runtime/convScope.js 的說明)。
  // classified / classification_level **只存在於 conversation 物件上**。
  // 只握有一個 id 就渲染訊息 → isClassified=false → 浮水印消失、複製 /
  // 編輯 / prompt-action 的機密限制全開。openConversation 已保證「先
  // hydrate 再選取」,這裡是最後一道防線:conversation 還沒到手就
  // **一則訊息都不渲染**。
  const conversationHydrated = isConversationHydrated(selectedConvId, selectedConv);
  const currentMsgs = renderableMessages(selectedConvId, selectedConv, messagesByConv);
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
  // Renderer registry split:preset_prompt → composer picker、prompt_action → 訊息鈕。
  const presetPrompts = useMemo(
    () => agentFunctions.filter((f) => f.kind === "preset_prompt"),
    [agentFunctions],
  );
  const promptActionFns = useMemo(
    () => agentFunctions.filter((f) => f.kind === "prompt_action"),
    [agentFunctions],
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

  // 命令面板的「跳轉動作」。鍵位提示由集中式 registry 產生(跨平台)。
  const paletteActions = useMemo(() => {
    const mac = isMacPlatform();
    const base = [
      {
        id: "new-chat",
        label: "新對話",
        hint: formatShortcut(getShortcut("new-chat"), { mac }),
        keywords: ["new chat", "新對話", "開新"],
        icon: <IconPlus size={13} />,
        run: () => newChat(),
      },
      {
        id: "open-settings",
        label: "開啟設定",
        keywords: ["settings", "設定", "偏好"],
        icon: <IconSettings size={13} />,
        run: () => { setSettingsTab("general"); setSettingsOpen(true); },
      },
      {
        id: "open-memory",
        label: "開啟設定 → 記憶",
        keywords: ["memory", "記憶", "facts"],
        icon: <IconHistory size={13} />,
        run: () => { setSettingsTab("memory"); setSettingsOpen(true); },
      },
      {
        id: "open-archived",
        label: "檢視已封存對話",
        keywords: ["archive", "archived", "封存"],
        icon: <IconArchive size={13} />,
        run: () => setFolder("archived"),
      },
      {
        id: "open-shortcuts",
        label: "顯示快捷鍵清單",
        hint: formatShortcut(getShortcut("shortcuts-panel"), { mac }),
        keywords: ["shortcut", "快捷鍵", "keys"],
        icon: <IconKeyboard size={13} />,
        run: () => setShortcutsOpen(true),
      },
      {
        id: "open-services",
        label: "專案入口",
        keywords: ["services", "專案", "平台"],
        icon: <IconGrid size={13} />,
        run: () => setServicesOpen(true),
      },
    ];
    for (const agent of agents) {
      base.push({
        id: `agent:${agent.id}`,
        label: `切換 agent → ${agent.name}`,
        keywords: ["agent", "切換", "switch", agent.id, agent.short].filter(Boolean),
        icon: <IconNodes size={13} />,
        run: () => setSelectedAgentId(agent.id),
      });
    }
    return base;
    // newChat 是 hoisted function declaration,身分穩定;其餘 setter 亦然。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents]);

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
      // Slice 3c: five-level classification label (added by the multi-level
      // classification backend). Absent on boolean-only payloads → the level
      // badge simply renders nothing; the boolean latch above is unaffected.
      classificationLevel: serverRow.classification_level,
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
  //
  // ⚠ 只有「本地清單已經有這則 conversation」才補訊息。只有 id 的情況必須走
  // openConversation 的 hydrate 路徑(先拿完整 conversation 再選取);否則會
  // 先把訊息灌進畫面、classification 卻還是空的 → 機密內容以未分類姿態渲染。
  useEffect(() => {
    if (!selectedConvId || typeof selectedConvId !== "number") return;
    if (!conversations.some((c) => c.id === selectedConvId)) return;
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

  // 「開啟一則對話」的**唯一**入口 —— 命令面板與側欄(含側欄搜尋結果)共用。
  //
  // 安全不變式:渲染任何訊息之前一定要先握有完整的 conversation 物件。
  // 搜尋結果可能包含本地清單沒有的對話(見 runtime/convScope.js),此時
  //   ① 先抓 /api/conversations/{id}(含 classified / classification_level);
  //   ② 驗 origin 是否屬於本 app,不屬於就**拒絕開啟並說明原因**;
  //   ③ 先把 conversation 寫進清單、再寫訊息、最後才 setSelectedConvId ——
  //      順序反了就會出現一個「有 id、沒 classification」的 render frame。
  const [openingConvId, setOpeningConvId] = useState(null);
  async function openConversation(convId) {
    if (convId === null || convId === undefined) return;
    setCitationsOpen(false);
    setCompareMode(false);
    // 本地已有(側欄一般點選)→ classification 已知,直接選取。
    if (conversations.some((c) => c.id === convId)) {
      setSelectedConvId(convId);
      return;
    }
    setOpeningConvId(convId);
    const result = await resolveConversationOpen({
      convId,
      localConversations: conversations,
      fetchDetail: (id) => apiGetConversation(authRequest, id),
    });
    setOpeningConvId((cur) => (cur === convId ? null : cur));
    if (result.status === "denied") {
      setRuntimeError(
        result.reason === "origin"
          ? OUT_OF_SCOPE_MESSAGE
          : "無法開啟這則對話（找不到對應的對話資料）。",
      );
      return;
    }
    if (result.status === "error") {
      setRuntimeError(result.error?.message || "無法載入對話內容");
      return;
    }
    if (result.fromLocal) {
      setSelectedConvId(convId);
      return;
    }
    const detail = result.detail;
    const lookupName = (id) => agents.find((a) => a.id === id)?.name || null;
    const lookupRequiresEncryption = (id) =>
      Boolean(agents.find((a) => a.id === id)?.requiresEncryption);
    const conv = mapServerConversation(detail, lookupName, lookupRequiresEncryption);
    const msgs = (detail.messages || []).map((m) => ({
      ...mapServerMessage(m),
      conversationId: convId,
    }));
    setConversations((prev) => (prev.some((c) => c.id === conv.id) ? prev : [conv, ...prev]));
    setMessagesByConv((prev) => ({ ...prev, [convId]: msgs }));
    setSelectedConvId(convId);
  }

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
    if (!(await confirm({
      title: "刪除對話",
      message: `確定要刪除「${target.title}」？此動作無法復原。`,
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    // 樂觀刪除:後端失敗要能**完整**還原。過去只還原 conversations,
    // convMeta(封存/標籤)、messages 與選取狀態都回不來 —— 對話「復活」後
    // 標籤與封存狀態已遺失,而且 ui_settings 已經把清空後的值同步上去了。
    const prevConversations = conversations;
    const prevMeta = convMeta[String(convId)];
    const prevMsgs = messagesByConv[convId];
    const wasSelected = selectedConvId === convId;

    setConversations((cs) => cs.filter((c) => c.id !== convId));
    // 對話沒了就把它的封存/標籤 meta 一併清掉,避免 ui_settings blob 長草。
    patchConvMeta(convId, { archived: false, tags: [] });
    if (wasSelected) {
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
      setConversations(prevConversations);
      if (prevMeta) {
        setConvMeta((prev2) => ({ ...prev2, [String(convId)]: prevMeta }));
      }
      if (prevMsgs) {
        setMessagesByConv((prev2) => ({ ...prev2, [convId]: prevMsgs }));
      }
      if (wasSelected) setSelectedConvId(convId);
      setRuntimeError(err.message || "刪除對話失敗");
    }
  }

  // ---- title auto-generation (runs once after the first turn lands) ----
  // Uses the same LLM pathway that answered the question, via the router's
  // primary model, so no extra admin configuration is required.
  async function generateConversationTitle(convId, userText, assistantText, effectiveTarget) {
    if (typeof convId !== "number") return;
    if (!isAuthenticated) return;
    // R3 formal authority:Router 聊天一律經 CSP 代理(X-ANILA-Router-Context
    // token 由 CSP 鑄造),瀏覽器直打 /router/ 會被 formal path 以 401 拒絕。
    // /v1/sessions/* 的 resume 流維持直連 router(cookie 認證,非 formal chat)。
    const baseUrl = config.cspBaseUrl;
    const systemPrompt =
      "你是對話標題產生器。閱讀以下 Q&A，回覆一個不超過 15 個繁體中文字的標題，" +
      "只能輸出標題本身，不要加引號、冒號、標點或其他說明。";
    const userPrompt = `使用者：${userText}\n助理：${assistantText}`;
    try {
      const csrf = readCsrfCookie();
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(csrf ? { "X-CSRF-Token": csrf } : {}),
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
    // R3 formal authority:Router 聊天一律經 CSP 代理(X-ANILA-Router-Context
    // token 由 CSP 鑄造),瀏覽器直打 /router/ 會被 formal path 以 401 拒絕。
    // /v1/sessions/* 的 resume 流維持直連 router(cookie 認證,非 formal chat)。
    const baseUrl = config.cspBaseUrl;

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
      await streamWithAbort(convId, {
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        ...timelineCallbacks(convId, assistantId),
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onFinishReason: (reason) => {
          // Continue Response:截斷標記存到訊息,UI 才知道要不要顯示「繼續」鈕。
          updateMsg(convId, assistantId, { finishReason: reason });
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
      usage: meta.usage || null,
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

    // R3 formal authority:Router 聊天一律經 CSP 代理(X-ANILA-Router-Context
    // token 由 CSP 鑄造),瀏覽器直打 /router/ 會被 formal path 以 401 拒絕。
    // /v1/sessions/* 的 resume 流維持直連 router(cookie 認證,非 formal chat)。
    const baseUrl = config.cspBaseUrl;
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
      await streamWithAbort(convId, {
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        ...timelineCallbacks(convId, assistantId),
        // 首回合 taskId 剛建立、state 還沒落地,顯式覆寫 streamWithAbort
        // 的 state 查找;null(建立失敗)= 不送標頭。
        taskId,
        onText: (acc) => {
          finalText = acc;
          updateMsg(convId, assistantId, { text: acc });
        },
        onFinishReason: (reason) => {
          // Continue Response:截斷標記存到訊息,UI 才知道要不要顯示「繼續」鈕。
          updateMsg(convId, assistantId, { finishReason: reason });
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
  // Message Actions(回應動作鈕,kind='prompt_action'):正/倒讚旁的一鍵動作。
  // 動作宣告式 {label, config.template},template 內 {content} 換成該則回覆
  // 全文,組好後當新使用者訊息送出。**不執行任意腳本**(air-gap 軍方不開
  // client eval)。來源優先序:該 agent 的 prompt_action functions(開發者在
  // CSP 設計) > 沒設時用通用預設(DEFAULT_MESSAGE_ACTIONS,已搬到
  // commands/promptActions.js 與斜線指令共用同一份),所以一定有翻譯/摘要/公文。
  const messageActions = promptActionFns.length > 0 ? promptActionFns : DEFAULT_MESSAGE_ACTIONS;

  function runMessageAction(msg, action) {
    const template = actionTemplate(action);
    if (!template || !msg?.text) return;
    const prompt = template.replace(/\{content\}/g, msg.text);
    sendMessage(prompt, [], {});
  }

  // 斜線指令 `/翻譯`(不帶參數)→ 套用在最新一則回覆上,等同按快捷動作鈕。
  // 回傳 false = 沒有可套用的回覆(呼叫端會退回「把指示填進輸入框」)。
  // classified 對話一律拒絕 —— 與 MessageBubble 的 `!classified` 閘門同姿態。
  function runPromptActionOnLatest(action) {
    if (isClassified) return false;
    const target = latestAssistantMessage;
    if (!target?.text || target.streaming) return false;
    const template = actionTemplate(action);
    if (!template) return false;
    runMessageAction(target, action);
    return true;
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
    const effectiveTarget = assistantMsg.routedAgentId || selectedAgentId;
    // R3 formal authority:同上,Router 聊天經 CSP 代理。
    const baseUrl = config.cspBaseUrl;
    // history 含截斷的這則 assistant + 一句續寫指示。buildMessageHistory 會把
    // 截斷訊息(已非 streaming)當 assistant role 帶上。
    const payload = {
      model: effectiveTarget,
      messages: buildMessageHistory(
        msgs.slice(0, idx + 1),
        "請接續上文，直接從中斷處往下寫，不要重複已經寫過的內容。",
        [],
      ),
    };
    updateMsg(convId, assistantMsg.id, { streaming: true, finishReason: null });
    const ctrl = streamAbortRef.current.get(convId) || null;
    try {
      const controller = new AbortController();
      streamAbortRef.current.set(convId, controller);
      let appended = "";
      await streamChatCompletion({
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        ...timelineCallbacks(convId, assistantMsg.id),
        taskId: taskIdForConv(convId),
        signal: controller.signal,
        onText: (acc) => {
          appended = acc;
          // 接在原文後(若原文未以空白結尾補一個空格,避免黏字)。
          const joiner = existing && !/\s$/.test(existing) ? " " : "";
          updateMsg(convId, assistantMsg.id, { text: existing + joiner + acc });
        },
        onFinishReason: (reason) => updateMsg(convId, assistantMsg.id, { finishReason: reason }),
      });
    } catch (err) {
      setRuntimeError(err?.message || "續寫失敗");
    } finally {
      streamAbortRef.current.delete(convId);
      updateMsg(convId, assistantMsg.id, { streaming: false });
    }
  }

  // steer:guided regenerate 的調整指令(更詳細/更簡潔/換個說法/自由文字);
  // 空 = 盲目重試(原行為)。non-empty 時附加到使用者原文後重新生成。
  async function regenerateMessage(assistantMsg, steer = "") {
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
    // R3 formal authority:Router 聊天一律經 CSP 代理(X-ANILA-Router-Context
    // token 由 CSP 鑄造),瀏覽器直打 /router/ 會被 formal path 以 401 拒絕。
    // /v1/sessions/* 的 resume 流維持直連 router(cookie 認證,非 formal chat)。
    const baseUrl = config.cspBaseUrl;
    const steeredUserText = steer
      ? `${prevUser.text}\n\n（重新回答時請依此調整：${steer}）`
      : prevUser.text;
    const payload = {
      model: effectiveTarget,
      messages: buildMessageHistory(msgs.slice(0, userIdx), steeredUserText, prevUser.attachments || []),
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
      await streamWithAbort(convId, {
        url: `${baseUrl}/v1/chat/completions`,
        payload,
        conversationId: typeof convId === "number" ? convId : undefined,
        ...timelineCallbacks(convId, assistantMsg.id),
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
  async function handleRate(targetMsg, nextRating, feedback = null) {
    const convId = targetMsg.conversationId;
    const prevRating = targetMsg.rating ?? null;
    updateMsg(convId, targetMsg.id, { rating: nextRating });

    if (typeof convId !== "number" || typeof targetMsg.dbId !== "number") {
      setRuntimeError("此訊息尚未儲存至後端，反饋僅保留於本地。");
      return;
    }
    try {
      await apiRateMessage(authRequest, convId, targetMsg.dbId, nextRating, feedback);
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
          await streamWithAbort(col.id, {
            url: `${config.cspBaseUrl}/v1/chat/completions`,
            payload: {
              model: col.agentId,
              messages: [{ role: "user", content: buildUserContent(text, attachments) }],
            },
            conversationId: typeof col.id === "number" ? col.id : undefined,
            ...timelineCallbacks(col.id, aId, { compare: true }),
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
  // 殼層容器改用共用設計系統 AppShell（@anila/ui）；密等浮水印與繼承橫幅
  // 走 overlay slot——內容、props、層級與判斷條件逐字保留（安全元件不動）。
  return (
    <AppShell
      overlay={
        <>
          {isClassified && (
            <ConfidentialWatermark
              userEmail={user?.email || user?.username}
              traceId={latestAssistantMessage?.traceId}
              level={selectedConv?.classificationLevel}
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
        </>
      }
      sidebar={
        <Sidebar
        conversations={conversationsView}
        onServerSearch={serverSearchWithMeta}
        onExportConv={exportConversation}
        selectedConvId={selectedConvId}
        // 側欄搜尋同樣可能命中本地清單以外的對話 → 走同一條 hydrate 入口。
        onSelectConv={(id) => { void openConversation(id); }}
        onNewChat={newChat}
        agents={agents}
        user={user}
        onLogout={logoutAndRedirect}
        onOpenSettings={(tab) => {
          setSettingsTab(tab || "general");
          setSettingsOpen(true);
        }}
        onOpenAgentBrowser={() => {}}
        onOpenServices={() => setServicesOpen(true)}
        onTaskCenter={() => setServicesOpen(false)}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        folder={folder}
        setFolder={setFolder}
        folders={folders}
        onCreateFolder={createFolder}
        onDeleteFolder={deleteFolder}
        onOpenTagEditor={organizeConversation}
        onRenameConv={handleRenameConv}
        onDeleteConv={handleDeleteConv}
        onArchiveConv={archiveConversation}
        onOpenCommandPalette={() => setPaletteOpen(true)}
        />
      }
    >
        <BannerBar banners={visibleBanners} onDismiss={dismissBanner} />
        <Topbar>
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

          {openingConvId != null && (
            <span
              role="status"
              aria-live="polite"
              style={{
                fontSize: 11, color: "var(--fg-subtle)",
                fontFamily: "var(--font-mono)", marginLeft: 8,
              }}
            >
              開啟對話中…
            </span>
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
              <ClassificationLevelBadge conversation={selectedConv} />
              {/* Slice 9a — Task result 可轉 artifact（doc 10 §11）：對話已建立
                  Task 時，提供薄連結深連到知識 SPA 的 Studio 面，帶 taskId
                  query 讓 ALM 承接；不在 shell 內另建 Studio 啟動器。 */}
              {selectedConv?.taskId != null && (
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
              流程，使用者沒有也不該管理 key；SDK 使用者仍可從 control
              plane 取得 sk-* 並用 Authorization header 呼叫。 */}

          <IconButton title="重新載入 agent" onClick={() => void refreshAgents()} disabled={loadingAgents}>
            <IconRefresh size={14} />
          </IconButton>

          <IconButton title="專案入口" onClick={() => setServicesOpen(true)} active={servicesOpen}>
            <IconGrid />
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
        </Topbar>

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
                    {!conversationHydrated ? (
                      // 只有 id、conversation 還沒到手 → 一則訊息都不渲染。
                      // (少了 conversation 就沒有 classified /
                      //  classification_level,會以未分類姿態渲染機密內容。)
                      <div
                        role="status"
                        aria-live="polite"
                        style={{
                          padding: "64px 12px",
                          textAlign: "center",
                          color: "var(--fg-subtle)",
                          fontSize: 13,
                        }}
                      >
                        對話載入中…
                      </div>
                    ) : currentMsgs.length === 0 ? (
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
                          classificationLevel={selectedConv?.classificationLevel}
                          onRegenerate={regenerateMessage}
                          onRate={handleRate}
                          onEditUser={handleEditUser}
                          onSwitchRevision={switchRevision}
                          onOpenCitation={onOpenCitation}
                          onPickFollowUp={(q) => sendMessage(q, [], {})}
                          messageActions={messageActions}
                          onAction={runMessageAction}
                          onContinue={continueMessage}
                        />
                      ))
                    )}
                    {/* Slice 4d — Trace Explorer:對話有 taskTraceId 時提供
                        「檢視軌跡」,取持久化 trace 並以 SpanTreeViewer 呈現。
                        live anila.spans(若已串入最新訊息)先顯示,點擊後被
                        持久化 spans 取代(simple replace)。 */}
                    {selectedConv?.taskTraceId && (
                      <TraceExplorer
                        key={selectedConv.taskTraceId}
                        traceId={selectedConv.taskTraceId}
                        liveSpans={latestAssistantMessage?.spans || null}
                      />
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
                      conversationId={selectedConvId}
                      presetPrompts={presetPrompts}
                      streaming={currentMsgs.some((m) => m.streaming)}
                      onStop={() => stopStreaming(selectedConvId)}
                      // 斜線指令:重用同一份快捷動作;機密對話停用動作類指令。
                      messageActions={messageActions}
                      classified={isClassified}
                      onOpenShortcuts={() => setShortcutsOpen(true)}
                      onOpenPalette={() => setPaletteOpen(true)}
                      onRunPromptAction={runPromptActionOnLatest}
                      placeholder="問 ANILA 任何事情 — / 開指令、@agent 指定 agent · Shift+Enter 換行"
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

      <SettingsModal
        open={settingsOpen}
        tab={settingsTab}
        setTab={setSettingsTab}
        onClose={() => setSettingsOpen(false)}
        user={user}
        agents={agents}
        authRequest={authRequest}
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

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        conversations={conversationsView}
        folders={folders}
        actions={paletteActions}
        onServerSearch={serverSearchWithMeta}
        onSelectConv={(id) => { void openConversation(id); }}
        // 面板會列出 classified 對話的標題 → 面板自己那一層也要有鑑識浮水印
        // (面板 z-index 壓在全域浮水印之上時,涉密內容不能落在無浮水印圖層)。
        watermarkUser={user?.email || user?.username}
        watermarkTraceId={latestAssistantMessage?.traceId}
      />

      <ShortcutsPanel open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </AppShell>
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
  const confirm = useConfirm();
  const toast = useToast();
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
    if (!(await confirm({
      title: "刪除事實",
      message: `刪除事實「${key}」？此動作無法復原。`,
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    try {
      await apiDeleteMemoryFact(authRequest, id);
      await reload();
    } catch (err) {
      toast(err?.message || "刪除失敗", { tone: "error" });
    }
  };

  const onClearFacts = async () => {
    if (factsState.total === 0) return;
    if (!(await confirm({
      title: "清空事實",
      message: `清空全部 ${factsState.total} 筆事實？此動作無法復原。`,
      confirmText: "清空",
      tone: "danger",
    }))) return;
    try {
      await apiClearMemoryFacts(authRequest);
      await reload();
    } catch (err) {
      toast(err?.message || "清空失敗", { tone: "error" });
    }
  };

  const onClearChunks = async () => {
    if (chunksState.total === 0) return;
    if (!(await confirm({
      title: "清空對話片段",
      message:
        `清空全部 ${chunksState.total} 段對話片段？\n` +
        `這會抹除跨對話語意檢索的記憶（已記住的事實不受影響）。\n` +
        `此動作無法復原。`,
      confirmText: "清空",
      tone: "danger",
    }))) return;
    try {
      await apiClearMemoryChunks(authRequest);
      await reload();
    } catch (err) {
      toast(err?.message || "清空失敗", { tone: "error" });
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
                role: {user?.role || "user"}
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
