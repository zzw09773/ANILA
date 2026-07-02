// ANILA Shell 主導覽（Slice 9a）— doc 00 §2 唯一產品入口 + doc 10 §11 Shell IA。
//
// 一般使用者只看到 ANILA 的四個入口：任務中心 / 我的知識庫 / 產出中心 / 專案入口。
// 治理中心（CSP 控制面）不是一般使用者的日常入口，只對 owner / admin /
// developer 顯示（doc 00 §2）。標籤一律用產品語彙，不得暴露 ANILALM /
// Studio / CSP 等技術品牌名。
//
// 外部同源介面（doc 02 §10 nginx 佈局：/anila=shell、/anilalm=知識 SPA、
// / = 治理中心）以 origin 絕對路徑連結，不可相對於 shell 的 /anila/ base；
// 因同屬單一 SSO origin，於同一分頁開啟即可共用登入 cookie。

import React from "react";

import {
  IconBook,
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
 * 四大使用者入口（doc 00 §2 / doc 10 §11 順序）。
 * @param {{ onTaskCenter?: () => void, onOpenServices?: () => void }} handlers
 */
export function buildShellEntries({ onTaskCenter, onOpenServices } = {}) {
  return [
    // 任務中心 = 現有聊天工作區（預設視圖，chat 即任務工作台）。
    { id: "tasks", label: "任務中心", Icon: IconMessage, current: true, onClick: onTaskCenter },
    // 我的知識庫 = 同源知識 SPA。
    { id: "knowledge", label: "我的知識庫", Icon: IconBook, href: originHref("/anilalm") },
    // 產出中心 = 知識 SPA 承載 Studio / artifacts。
    { id: "output", label: "產出中心", Icon: IconSpark, href: originHref("/anilalm") },
    // 專案入口 = ServicesPanel（Registry 服務卡片）。
    { id: "projects", label: "專案入口", Icon: IconGrid, onClick: onOpenServices },
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
  const { Icon, label, href, onClick, current } = entry;
  const ariaCurrent = current ? "page" : undefined;

  const style = collapsed
    ? {
        ...rowBase,
        width: 36,
        height: 36,
        padding: 0,
        justifyContent: "center",
        color: current ? "var(--fg)" : "var(--fg-muted)",
      }
    : { ...rowBase, color: current ? "var(--fg)" : "var(--fg)" };

  const body = collapsed ? (
    <Icon size={18} />
  ) : (
    <>
      <Icon size={15} style={{ color: "var(--fg-muted)", flexShrink: 0 }} />
      <span>{label}</span>
    </>
  );

  if (href) {
    return (
      <a
        href={href}
        title={label}
        aria-label={collapsed ? label : undefined}
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
      title={label}
      aria-label={collapsed ? label : undefined}
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
 * ANILA Shell 主導覽群組：四大使用者入口 + admin-gated 治理中心。
 * @param {{
 *   user?: { role?: string } | null,
 *   collapsed?: boolean,
 *   onTaskCenter?: () => void,
 *   onOpenServices?: () => void,
 * }} props
 */
export function ShellNav({ user, collapsed = false, onTaskCenter, onOpenServices }) {
  const entries = buildShellEntries({ onTaskCenter, onOpenServices });
  if (canSeeGovernance(user)) {
    entries.push(governanceEntry());
  }

  return (
    <nav
      aria-label="ANILA 主導覽"
      style={
        collapsed
          ? { display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }
          : { display: "flex", flexDirection: "column", gap: 2, padding: "0 10px 8px" }
      }
    >
      {entries.map((entry) => (
        <NavRow key={entry.id} entry={entry} collapsed={collapsed} />
      ))}
    </nav>
  );
}
