// ANILA Shell 主導覽（Slice 9a）— doc 00 §2 唯一產品入口 + doc 10 §11 Shell IA。
//
// 一般使用者只看到 ANILA 的三個入口：對話 / 我的知識庫 / 專案入口。
// （原本另有「產出中心」，但它與「我的知識庫」是同一個 /anilalm 連結，
//   兩個標籤指同一頁只會讓人以為點錯；產出中心這個產品概念仍在 anilalm 裡。）
// 治理中心（CSP 控制面）不是一般使用者的日常入口，只對 owner / admin /
// developer 顯示（doc 00 §2）。標籤一律用產品語彙，不得暴露 ANILALM /
// Studio / CSP 等技術品牌名。
//
// 外部同源介面（doc 02 §10 nginx 佈局：/anila=shell、/anilalm=知識 SPA、
// / = 治理中心）以 origin 絕對路徑連結，不可相對於 shell 的 /anila/ base；
// 因同屬單一 SSO origin，於同一分頁開啟即可共用登入 cookie。

import React, { useState } from "react";

import {
  ANILA_LM_COMING_SOON_LABEL,
  ANILA_LM_ENTRY_ENABLED,
} from "./anilalmReleaseGate.js";
import { Modal } from "./components.jsx";
import {
  IconBook,
  IconGauge,
  IconGrid,
  IconMessage,
  IconShield,
  IconSpark,
} from "./icons.jsx";

// doc 00 §2：治理中心 = Admin / Developer / Service Admin 控制面，非一般入口。
// 對應 CSP UserRole：owner / admin / developer 可見；user / system 或未知一律隱藏。
const GOVERNANCE_ROLES = new Set(["owner", "admin", "developer"]);

/**
 * 是否顯示治理中心入口。角色未知（欄位缺漏 / 尚未載入）時採防禦式隱藏。
 * @param {{ role?: string } | null | undefined} user
 * @returns {boolean}
 */
export function canSeeGovernance(user) {
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
 * 使用者入口（doc 00 §2 / doc 10 §11 順序）。
 * @param {{ onTaskCenter?: () => void, onOpenServices?: () => void, onOpenUsage?: () => void, currentId?: string }} handlers
 */
export function buildShellEntries({ onTaskCenter, onOpenServices, onOpenUsage, currentId = "tasks" } = {}) {
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
    // 本 release 關閉：保留列、停用、標「即將推出」。重開改 anilalmReleaseGate.js。
    knowledge,
    // 專案入口 = ServicesPanel（Registry 服務卡片）。
    { id: "projects", label: "專案入口", Icon: IconGrid, current: currentId === "projects", onClick: onOpenServices },
    { id: "usage", label: "用量", Icon: IconGauge, current: currentId === "usage", onClick: onOpenUsage },
  ];
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
 * ANILA Shell 主導覽：側欄只留一顆「平台入口」，點開後用彈窗列出
 * 對話 / 知識庫 / 專案入口（+ admin 的治理中心）。
 * @param {{
 *   user?: { role?: string } | null,
 *   collapsed?: boolean,
 *   onTaskCenter?: () => void,
 *   onOpenServices?: () => void,
 *   onOpenUsage?: () => void,
 *   currentId?: string,
 * }} props
 */
export function ShellNav({ user, collapsed = false, onTaskCenter, onOpenServices, onOpenUsage, currentId = "tasks" }) {
  const [open, setOpen] = useState(false);
  const closeThen = (fn) => () => {
    setOpen(false);
    fn?.();
  };
  const entries = buildShellEntries({
    onTaskCenter: closeThen(onTaskCenter),
    onOpenServices: closeThen(onOpenServices),
    onOpenUsage: closeThen(onOpenUsage),
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
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="平台入口"
        subtitle="對話、知識庫、專案與用量"
        width={360}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 2, margin: "-8px 0" }}>
          {entries.map((entry) => (
            <NavRow key={entry.id} entry={entry} />
          ))}
        </div>
      </Modal>
    </>
  );
}
