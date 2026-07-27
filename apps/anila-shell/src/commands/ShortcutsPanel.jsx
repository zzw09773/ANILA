// 快捷鍵面板(⌘/ · Ctrl+/)—— 內容由集中式登記表自動產生,不手寫清單。
// 鍵位顯示跨平台:macOS 顯示 ⌘/⇧/⌥,其他平台顯示 Ctrl+/Shift+/Alt+。
import React, { useMemo } from "react";

import { Kbd, Modal } from "../components.jsx";
import { formatShortcut, groupedShortcuts, isMacPlatform } from "./shortcuts.js";

export const ShortcutsPanel = ({ open, onClose }) => {
  const mac = useMemo(() => isMacPlatform(), []);
  const groups = useMemo(() => groupedShortcuts(), []);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="鍵盤快捷鍵"
      subtitle={mac ? "⌘ = Command" : "Ctrl = Control"}
      width={560}
    >
      <div style={{ display: "grid", gap: 18 }}>
        {groups.map((group) => (
          <div key={group.id}>
            <div style={{
              fontSize: 11, fontWeight: 600, letterSpacing: 0.4,
              color: "var(--fg-subtle)", fontFamily: "var(--font-mono)",
              textTransform: "uppercase", marginBottom: 6,
            }}>
              {group.label}
            </div>
            <div style={{ display: "grid", gap: 2 }}>
              {group.items.map((shortcut) => (
                <div
                  key={shortcut.id}
                  style={{
                    display: "flex", alignItems: "center", gap: 12,
                    padding: "6px 8px",
                    borderRadius: "var(--radius)",
                    background: "var(--bg-subtle)",
                  }}
                >
                  <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: "var(--fg)" }}>
                    {shortcut.description}
                  </span>
                  <span style={{ display: "inline-flex", gap: 4, flexShrink: 0 }}>
                    <Kbd>{formatShortcut(shortcut, { mac })}</Kbd>
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
        <div style={{ fontSize: 11, color: "var(--fg-subtle)", lineHeight: 1.7 }}>
          斜線指令(輸入框行首打 <code>/</code>)另含 /翻譯 /摘要 /公文 /清空 /快捷鍵 /搜尋；
          機密對話會停用快捷動作類指令。
        </div>
      </div>
    </Modal>
  );
};
