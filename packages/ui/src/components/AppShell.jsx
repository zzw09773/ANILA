// AppShell / Topbar / Sidebar — 殼層佈局三件組。
//
// AppShell：全高 flex 容器（側欄 + 主欄）。`overlay` slot 保留給密等浮水印
// 與全域橫幅等安全/系統層——設計系統只提供掛點，不擁有其內容與層級。
// Sidebar：展開（寬欄）/ 收合（icon rail）兩態的側欄外殼；內容由 app 提供。
// Topbar：主欄頂部工具列外殼。
import React from "react";

export const AppShell = ({ sidebar, overlay, children, ...rest }) => (
  <div
    {...rest}
    style={{
      display: "flex",
      height: "100dvh",
      background: "var(--anila-color-bg)",
      color: "var(--anila-color-fg)",
      fontFamily: "var(--anila-font-sans)",
      position: "relative",
      ...(rest.style || {}),
    }}
  >
    {overlay}
    {sidebar}
    <main
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
      }}
    >
      {children}
    </main>
  </div>
);

export const Sidebar = ({
  collapsed = false,
  width = 272,
  railWidth = 52,
  children,
  "aria-label": ariaLabel,
  ...rest
}) => (
  <aside
    aria-label={ariaLabel || "側邊欄"}
    {...rest}
    style={
      collapsed
        ? {
            width: railWidth,
            flexShrink: 0,
            borderRight: "1px solid var(--anila-color-border)",
            background: "var(--anila-color-bg-subtle)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            padding: "12px 0",
            gap: 6,
            ...(rest.style || {}),
          }
        : {
            width,
            flexShrink: 0,
            borderRight: "1px solid var(--anila-color-border)",
            background: "var(--anila-color-bg-subtle)",
            display: "flex",
            flexDirection: "column",
            ...(rest.style || {}),
          }
    }
  >
    {children}
  </aside>
);

export const Topbar = ({ children, ...rest }) => (
  <div
    {...rest}
    style={{
      display: "flex",
      alignItems: "center",
      gap: 10,
      padding: "10px 18px",
      borderBottom: "1px solid var(--anila-color-border)",
      background: "var(--anila-color-bg)",
      ...(rest.style || {}),
    }}
  >
    {children}
  </div>
);
