// Toast — 呈現層元件（狀態外掛：由 app 端的 provider / hook 管理佇列，
// 例如 shell 的 confirm.jsx）。tone 對應語意色，含關閉鈕與 aria live。
import React from "react";
import { IconAlert, IconCheck, IconInfo, IconX } from "../icons.jsx";

const TONES = {
  info: { color: "var(--anila-color-info)", Icon: IconInfo },
  success: { color: "var(--anila-color-success)", Icon: IconCheck },
  warn: { color: "var(--anila-color-warn)", Icon: IconAlert },
  danger: { color: "var(--anila-color-danger)", Icon: IconAlert },
};

export const Toast = ({ tone = "info", children, onClose, ...rest }) => {
  const t = TONES[tone] || TONES.info;
  const { Icon } = t;
  return (
    <div
      role="status"
      aria-live="polite"
      {...rest}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        minWidth: 220,
        maxWidth: 420,
        padding: "10px 12px",
        background: "var(--anila-color-bg-elev)",
        border: "1px solid var(--anila-color-border)",
        borderLeft: `3px solid ${t.color}`,
        borderRadius: "var(--anila-radius-md)",
        boxShadow: "var(--anila-shadow-md)",
        color: "var(--anila-color-fg)",
        fontSize: "var(--anila-text-md)",
        fontFamily: "var(--anila-font-sans)",
        ...(rest.style || {}),
      }}
    >
      <span style={{ color: t.color, display: "flex", flexShrink: 0 }}>
        <Icon size={15} />
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>{children}</span>
      {onClose && (
        <button
          aria-label="關閉"
          title="關閉"
          onClick={onClose}
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            background: "transparent",
            border: "none",
            color: "var(--anila-color-fg-muted)",
            cursor: "pointer",
            padding: 2,
          }}
        >
          <IconX size={13} />
        </button>
      )}
    </div>
  );
};

// Toast 疊放容器 — 固定右下（沿用 shell 慣例位置與 z 層級）。
export const ToastStack = ({ children, ...rest }) => (
  <div
    {...rest}
    style={{
      position: "fixed",
      right: 16,
      bottom: 16,
      zIndex: "var(--anila-z-toast)",
      display: "flex",
      flexDirection: "column",
      gap: 8,
      ...(rest.style || {}),
    }}
  >
    {children}
  </div>
);
