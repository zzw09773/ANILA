// ANILA Shell 主導覽 —  唯一產品入口 +  Shell IA。
//
// 一般使用者只看到 ANILA 的入口：對話 / 我的知識庫 / 專案入口 / 用量 / 記憶 / 文件比對。
// （原本另有「產出中心」，但它與「我的知識庫」是同一個 /anilalm 連結，
//   兩個標籤指同一頁只會讓人以為點錯；產出中心這個產品概念仍在 anilalm 裡。）
// 治理中心（CSP 控制面）不是一般使用者的日常入口，只對 owner / admin /
// developer 顯示。標籤一律用產品語彙，不得暴露 ANILALM /
// Studio / CSP 等技術品牌名。
//
// 外部同源介面（nginx 佈局：/anila=shell、/anilalm=知識 SPA、
// / = 治理中心）以 origin 絕對路徑連結，不可相對於 shell 的 /anila/ base；
// 因同屬單一 SSO origin，於同一分頁開啟即可共用登入 cookie。

import React, { useEffect, useState } from "react";

import {
  ANILA_LM_COMING_SOON_LABEL,
  ANILA_LM_ENTRY_ENABLED,
} from "./anilalmReleaseGate.js";
import { Modal } from "./components.jsx";
import { DocumentVersionCompare } from "./documentVersionDiff.jsx";
import { HandoverInterviewDialog } from "./handoverInterview.jsx";
import { TranslationPanel } from "./translationPanel.jsx";
import { LetterRewritePanel } from "./letterRewrite.jsx";
import {
  IconBook,
  IconColumns,
  IconPencil,
  IconUser,
  IconGauge,
  IconGrid,
  IconHistory,
  IconMessage,
  IconChevDown,
  IconChevRight,
  IconShield,
  IconSpark,
} from "./icons.jsx";

// 首次沒有偏好時收合，長說明不常駐擠側欄。記住的是展開或收合，不是選了哪一項。
const ADDINS_EXPANDED_KEY = "anila.addinsExpanded";

function readAddinsExpanded() {
  try {
    const stored = window.localStorage.getItem(ADDINS_EXPANDED_KEY);
    if (stored === "1") return true;
    if (stored === "0") return false;
  } catch {
    /* 私人模式或配額不足時用預設：收合。 */
  }
  return false;
}

// 治理中心 = Admin / Developer / Service Admin 控制面，非一般入口。
// 對應 CSP UserRole：owner / admin / developer 可見；user / system 或未知一律隱藏。
const GOVERNANCE_ROLES = new Set(["owner", "admin", "developer"]);

/**
 * 是否顯示治理中心入口。角色未知（欄位缺漏 / 尚未載入）時採防禦式隱藏。
 * 單位管理員（is_unit_admin）也看得到：2026-09-30 起所有人登入後先進 ANILA，
 * 單位管理員要從這裡回治理中心核准同仁。
 * @param {{ role?: string, is_unit_admin?: boolean } | null | undefined} user
 * @returns {boolean}
 */
export function canSeeGovernance(user) {
  if (user?.is_unit_admin === true) return true;
  const role = user?.role;
  if (typeof role !== "string") return false;
  return GOVERNANCE_ROLES.has(role);
}

/**
 * 原始思考只給平台擁有者、管理員、開發者。
 * 一般使用者與單位管理員只看中文摘要。單位管理員看得到治理中心，但看不到原文。
 * @param {{ role?: string } | null | undefined} user
 * @returns {boolean}
 */
export function canSeeRawReasoning(user) {
  const role = user?.role;
  if (typeof role !== "string") return false;
  return GOVERNANCE_ROLES.has(role);
}

/**
 * 組出 origin 絕對路徑。外部同源介面（/anilalm、/）必須從 origin 起算，
 * 不能相對於 shell 的 /anila/ base，否則會被解析成 /anila/anilalm。
 * @param {string} path 以 '/' 開頭的路徑
 * @returns {string}
 */
export function originHref(path) {
  const origin =
    (typeof window !== "undefined" && window.location && window.location.origin) || "";
  return `${origin}${path}`;
}

/**
 * 使用者入口。
 * @param {{ onTaskCenter?: () => void, onOpenServices?: () => void, onOpenUsage?: () => void, onOpenMemory?: () => void, onOpenDocDiff?: () => void, currentId?: string }} handlers
 */
export function buildShellEntries({ onTaskCenter, onOpenServices, onOpenUsage, onOpenMemory, currentId = "tasks" } = {}) {
  const knowledge = ANILA_LM_ENTRY_ENABLED
    ? { id: "knowledge", label: "我的知識庫", Icon: IconBook, href: originHref("/anilalm") }
    : {
        id: "knowledge",
        label: "我的知識庫",
        Icon: IconBook,
        disabled: true,
        badge: ANILA_LM_COMING_SOON_LABEL,
      };

  return [
    // 任務中心 = 現有聊天工作區（預設視圖，chat 即任務工作台）。
    { id: "tasks", label: "對話", Icon: IconMessage, current: currentId === "tasks", onClick: onTaskCenter },
    // 我的知識庫 = 同源知識 SPA（也承載 Studio / 產出）。
    // 開關在 anilalmReleaseGate.js。關上時保留這一列，標「即將推出」、不可點。
    knowledge,
    { id: "quick-builder", label: "助手快製", Icon: IconPencil, href: originHref("/quick-builder") },
    // 專案入口 = ServicesPanel（Registry 服務卡片）。
    { id: "projects", label: "專案入口", Icon: IconGrid, current: currentId === "projects", onClick: onOpenServices },
    { id: "memory", label: "記憶", Icon: IconHistory, current: currentId === "memory", onClick: onOpenMemory },
  ];
}

/**
 * 增益集：不是對話本身、各自開一個視窗的工具。每項一行白話說明。
 * 翻譯的說明要分清楚兩種模式的用途。
 */
export const ADDIN_ITEMS = [
  {
    id: "translation",
    label: "翻譯",
    Icon: IconMessage,
    description: "快速閱讀：給自己看懂；對外使用：要寄出的譯文，程式核對數字、單位與否定詞。",
  },
  {
    id: "letter-rewrite",
    label: "改寫信件與公文",
    Icon: IconPencil,
    description: "把信或公文改得更正式、更和緩或更精簡，和原文並排對照。",
  },
  {
    id: "doc-diff",
    label: "文件比對",
    Icon: IconColumns,
    description: "比對兩版 Word，依條號或「一、（一）」列出改了哪裡。",
  },
  {
    id: "handover-interview",
    label: "交接訪談",
    Icon: IconUser,
    description: "調動或離開專案前，用固定問題留下沒寫下來的事。",
  },
  {
    id: "skills",
    label: "我的 skill",
    Icon: IconBook,
    description: "建立、修改常用的 skill，對話時從 / 或輸入框的 skill 按鈕套用。",
  },
];

function AddinRow({ item, collapsed, onOpen }) {
  const { Icon, label, description } = item;
  const descId = `addin-desc-${item.id}`;
  if (collapsed) {
    return (
      <button
        type="button"
        data-addin={item.id}
        onClick={onOpen}
        title={`${label}：${description}`}
        aria-label={label}
        aria-description={description}
        style={{ ...rowBase, width: 36, height: 36, padding: 0, justifyContent: "center", color: "var(--fg-muted)" }}
        onMouseEnter={hoverOn}
        onMouseLeave={hoverOff}
      >
        <Icon size={18} />
      </button>
    );
  }
  return (
    <button
      type="button"
      data-addin={item.id}
      onClick={onOpen}
      aria-describedby={descId}
      title={description}
      style={{ ...rowBase, color: "var(--fg)" }}
      onMouseEnter={hoverOn}
      onMouseLeave={hoverOff}
    >
      <Icon size={15} style={{ color: "var(--fg-muted)", flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0 }}>{label}</span>
      <span id={descId} className="sr-only">{description}</span>
    </button>
  );
}

// 治理中心入口（僅 admin 面向）——連到同源 CSP 治理 UI（origin 根路徑）。
function governanceEntry() {
  return { id: "governance", label: "治理中心", Icon: IconShield, href: originHref("/") };
}

const rowBase = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  width: "100%",
  padding: "7px 10px",
  fontSize: 13,
  fontWeight: 500,
  textAlign: "left",
  background: "transparent",
  border: "1px solid transparent",
  borderRadius: "var(--radius)",
  color: "var(--fg)",
  cursor: "pointer",
  textDecoration: "none",
  boxSizing: "border-box",
};

function hoverOn(e) {
  e.currentTarget.style.background = "var(--bg-elev)";
}
function hoverOff(e) {
  e.currentTarget.style.background = "transparent";
}

function NavRow({ entry, collapsed }) {
  const { Icon, label, href, onClick, current, disabled, badge } = entry;
  const ariaCurrent = current ? "page" : undefined;
  const title = badge ? `${label}（${badge}）` : label;

  const style = collapsed
    ? {
        ...rowBase,
        width: 36,
        height: 36,
        padding: 0,
        justifyContent: "center",
        color: disabled ? "var(--fg-muted)" : current ? "var(--fg)" : "var(--fg-muted)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
      }
    : {
        ...rowBase,
        color: disabled ? "var(--fg-muted)" : "var(--fg)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.7 : 1,
      };

  const body = collapsed ? (
    <Icon size={18} />
  ) : (
    <>
      <Icon size={15} style={{ color: "var(--fg-muted)", flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0 }}>{label}</span>
      {badge ? (
        <span
          data-coming-soon=""
          style={{
            fontSize: 11,
            fontWeight: 500,
            color: "var(--fg-muted)",
            letterSpacing: "0.02em",
            flexShrink: 0,
          }}
        >
          {badge}
        </span>
      ) : null}
    </>
  );

  if (disabled) {
    return (
      <span
        role="link"
        aria-disabled="true"
        title={title}
        aria-label={collapsed ? title : undefined}
        data-nav-disabled={entry.id}
        style={style}
      >
        {body}
      </span>
    );
  }

  if (href) {
    return (
      <a
        href={href}
        title={title}
        aria-label={collapsed ? title : undefined}
        aria-current={ariaCurrent}
        style={style}
        onMouseEnter={hoverOn}
        onMouseLeave={hoverOff}
      >
        {body}
      </a>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={collapsed ? title : undefined}
      aria-current={ariaCurrent}
      style={style}
      onMouseEnter={hoverOn}
      onMouseLeave={hoverOff}
    >
      {body}
    </button>
  );
}

/**
 * ANILA Shell 主導覽：側欄一顆「平台入口」，點開後用彈窗列出
 * 對話 / 知識庫 / 專案入口。看得到治理中心的人，側欄另有直接連結，
 * 彈窗裡也保留同一顆，href 相同。
 * @param {{
 *   user?: { role?: string } | null,
 *   collapsed?: boolean,
 *   onTaskCenter?: () => void,
 *   onOpenServices?: () => void,
 *   onOpenUsage?: () => void,
 *   onOpenMemory?: () => void,
 *   currentId?: string,
 *   authRequest?: Function,
 * }} props
 */
export function ShellNav({ user, collapsed = false, onTaskCenter, onOpenServices, onOpenUsage, onOpenMemory, onOpenSkills, currentId = "tasks", authRequest }) {
  const [open, setOpen] = useState(false);
  const [addinsExpanded, setAddinsExpanded] = useState(readAddinsExpanded);
  useEffect(() => {
    try {
      window.localStorage.setItem(ADDINS_EXPANDED_KEY, addinsExpanded ? "1" : "0");
    } catch {
      /* 記不住就維持這次畫面，不影響對話清單。 */
    }
  }, [addinsExpanded]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [interviewOpen, setInterviewOpen] = useState(false);
  const [translationOpen, setTranslationOpen] = useState(false);
  const [letterOpen, setLetterOpen] = useState(false);
  const openAddin = {
    translation: () => setTranslationOpen(true),
    "letter-rewrite": () => setLetterOpen(true),
    "doc-diff": () => setCompareOpen(true),
    "handover-interview": () => setInterviewOpen(true),
    skills: () => onOpenSkills?.(),
  };
  const addins = ADDIN_ITEMS.filter((item) => item.id !== "skills" || typeof onOpenSkills === "function");
  const closeThen = (fn) => () => {
    setOpen(false);
    fn?.();
  };
  const entries = buildShellEntries({
    onTaskCenter: closeThen(onTaskCenter),
    onOpenServices: closeThen(onOpenServices),
    onOpenUsage: closeThen(onOpenUsage),
    onOpenMemory: closeThen(onOpenMemory),
    currentId,
  });
  if (canSeeGovernance(user)) {
    entries.push(governanceEntry());
  }

  const triggerStyle = collapsed
    ? {
        ...rowBase,
        width: 36,
        height: 36,
        padding: 0,
        justifyContent: "center",
        color: "var(--fg-muted)",
      }
    : { ...rowBase, color: "var(--fg)" };

  return (
    <>
      <nav
        aria-label="ANILA 主導覽"
        style={
          collapsed
            ? { display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }
            : { display: "flex", flexDirection: "column", gap: 2, padding: "0 10px 8px" }
        }
      >
        <button
          type="button"
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label="平台入口"
          title="平台入口"
          onClick={() => setOpen(true)}
          style={triggerStyle}
          onMouseEnter={hoverOn}
          onMouseLeave={hoverOff}
        >
          {collapsed ? (
            <IconSpark size={18} />
          ) : (
            <>
              <IconSpark size={15} style={{ color: "var(--fg-muted)", flexShrink: 0 }} />
              <span style={{ flex: 1, minWidth: 0 }}>平台入口</span>
            </>
          )}
        </button>
      </nav>
      <section
        aria-label="增益集"
        style={
          collapsed
            ? { display: "flex", flexDirection: "column", alignItems: "center", gap: 4, marginTop: 4 }
            : { display: "flex", flexDirection: "column", gap: 2, padding: "0 10px 8px" }
        }
      >
        {collapsed ? (
          <div style={{ width: 20, height: 1, background: "var(--border)", margin: "4px 0" }} aria-hidden="true" />
        ) : (
          <h2 style={{ margin: "4px 0 2px" }}>
            <button
              type="button"
              aria-expanded={addinsExpanded}
              aria-controls="anila-addins"
              onClick={() => setAddinsExpanded((current) => !current)}
              style={{
                ...rowBase,
                gap: 6,
                padding: "4px 10px",
                fontSize: 11,
                fontWeight: 600,
                color: "var(--fg-muted)",
                fontFamily: "var(--font-mono)",
                letterSpacing: 0.4,
              }}
            >
              {addinsExpanded ? <IconChevDown size={12} /> : <IconChevRight size={12} />}
              <span style={{ flex: 1, minWidth: 0 }}>增益集</span>
              <span>{addins.length}</span>
            </button>
          </h2>
        )}
        <div
          id="anila-addins"
          hidden={!collapsed && !addinsExpanded}
          style={{ display: collapsed || addinsExpanded ? "flex" : "none", flexDirection: "column", gap: 2, alignItems: collapsed ? "center" : "stretch" }}
        >
          {addins.map((item) => (
            <AddinRow key={item.id} item={item} collapsed={collapsed} onOpen={openAddin[item.id]} />
          ))}
        </div>
      </section>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="平台入口"
        subtitle="對話、知識庫、專案、用量與治理中心"
        width={360}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 2, margin: "-8px 0" }}>
          {entries.map((entry) => (
            <NavRow key={entry.id} entry={entry} />
          ))}
        </div>
      </Modal>
      <Modal
        open={compareOpen}
        onClose={() => setCompareOpen(false)}
        title="文件比對"
        subtitle="選舊版與新版的 Word，再開始比對。"
        width={720}
      >
        <DocumentVersionCompare />
      </Modal>
      <TranslationPanel open={translationOpen} onClose={() => setTranslationOpen(false)} />
      <LetterRewritePanel open={letterOpen} onClose={() => setLetterOpen(false)} />
      <HandoverInterviewDialog
        open={interviewOpen}
        onClose={() => setInterviewOpen(false)}
        {...(typeof authRequest === "function" ? { authRequest } : {})}
      />
    </>
  );
}
