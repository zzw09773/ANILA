// ANILA Shell 主導覽 — 左側四個等重入口。系統管理只在帳號選單。

import React from "react";

import { isGovernanceRole } from "../../shared/product.js";
import { ANILA_LM_ENTRY_ENABLED } from "./anilalmReleaseGate.js";
import { appHref, knowledgeHref } from "./appOrigins.js";

/**
 * 組出 origin 絕對路徑。外部同源介面（/anilalm、/）必須從 origin 起算，
 * 不能相對於 shell 的 /anila/ base，否則會被解析成 /anila/anilalm。
 * @param {string} path 以 '/' 開頭的路徑
 * @returns {string}
 */
export function originHref(path) {
  if (path === "/") return appHref("governance", "/");
  if (path.startsWith("/anilalm")) {
    return knowledgeHref(path.slice("/anilalm".length) || "/");
  }
  return appHref("shell", path);
}

/**
 * 使用者入口。標籤跟 編：工作臺／我的知識庫／製作／專案入口。
 * @param {{ onTaskCenter?: () => void, onOpenServices?: () => void }} handlers
 */
export function buildShellEntries({ onTaskCenter, onOpenServices } = {}) {
  const entries = [
    { id: "tasks", label: "工作臺", current: true, onClick: onTaskCenter },
    { id: "projects", label: "專案入口", onClick: onOpenServices },
  ];
  if (ANILA_LM_ENTRY_ENABLED) {
    entries.splice(
      1,
      0,
      { id: "knowledge", label: "我的知識庫", href: knowledgeHref("/") },
      { id: "outputs", label: "製作", href: knowledgeHref("/outputs") },
    );
  }
  return entries;
}

function NavRow({ entry, collapsed }) {
  const { label, href, onClick, current } = entry;
  const ariaCurrent = current ? "page" : undefined;
  const className = "anila-rail__item";
  const style = collapsed
    ? { justifyContent: "center", padding: 0, minHeight: 40, width: 40 }
    : undefined;

  const body = collapsed ? label.slice(0, 1) : label;

  if (href) {
    return (
      <a
        href={href}
        title={label}
        aria-label={collapsed ? label : undefined}
        aria-current={ariaCurrent}
        className={className}
        style={style}
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
      className={className}
      style={style}
    >
      {body}
    </button>
  );
}

/**
 * 左側四個等重入口。系統管理不在這裡。
 */
export function ShellNav({ collapsed = false, onTaskCenter, onOpenServices }) {
  const entries = buildShellEntries({ onTaskCenter, onOpenServices });

  return (
    <nav
      aria-label="ANILA 主導覽"
      className="anila-rail"
      style={collapsed ? { alignItems: "center", padding: "8px 0" } : undefined}
    >
      {entries.map((entry) => (
        <NavRow key={entry.id} entry={entry} collapsed={collapsed} />
      ))}
    </nav>
  );
}

export function canSeeGovernance(user) {
  return isGovernanceRole(user?.role);
}
