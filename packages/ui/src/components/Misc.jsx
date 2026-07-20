// Kbd / Divider — 小型工具元件（自 shell components.jsx 收編）。
import React from "react";

export const Kbd = ({ children }) => (
  <span
    style={{
      fontFamily: "var(--anila-font-mono)",
      fontSize: 10.5,
      padding: "1px 5px",
      background: "var(--anila-color-bg)",
      border: "1px solid var(--anila-color-border)",
      borderRadius: "var(--anila-radius-sm)",
      color: "var(--anila-color-fg-muted)",
    }}
  >
    {children}
  </span>
);

export const Divider = ({ vertical, style = {} }) => (
  <div
    style={{
      background: "var(--anila-color-border)",
      ...(vertical
        ? { width: 1, alignSelf: "stretch" }
        : { height: 1, width: "100%" }),
      ...style,
    }}
  />
);
