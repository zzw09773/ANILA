// Shared low-level components — 基礎元件已收編到共用設計系統 @anila/ui
// （packages/ui：tokens + Button/IconButton/Input/Modal/Dropdown/MenuItem/
// Kbd/Divider…）。此檔保留為相容出口，全 shell 既有 import 路徑不變；
// 僅留 shell 領域專屬元件（AgentPill）在本檔。
import React from "react";

export {
  Button,
  IconButton,
  Kbd,
  Divider,
  Dropdown,
  MenuItem,
  Modal,
  Input,
} from "@anila/ui";

// Agent badge / pill — ANILA runtime 領域元件（agent 概念不屬於通用設計
// 系統），樣式沿用橋接後的舊變數（= 共用 tokens）。
export const AgentPill = ({ agent, size = "md", selected }) => {
  if (!agent) return null;
  const s = size === "sm";
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: s ? "2px 7px" : "3px 9px",
      fontSize: s ? 11 : 12,
      fontFamily: "var(--font-mono)",
      fontWeight: 500,
      background: selected ? "var(--accent-soft)" : "var(--bg-subtle)",
      color: "var(--fg)",
      border: "1px solid " + (selected ? "var(--accent)" : "var(--border)"),
      borderRadius: 999,
      lineHeight: 1.3,
    }}>
      <span style={{
        width: s ? 5 : 6, height: s ? 5 : 6, borderRadius: 999,
        background: agent.id === "anila-router" ? "var(--accent)" : "var(--fg-muted)",
      }}/>
      {agent.short || agent.id}
    </div>
  );
};
