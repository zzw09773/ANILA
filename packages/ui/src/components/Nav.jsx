// NavGroup / NavItem — 側欄導覽基礎元件（自 shell shellNav.jsx 的 NavRow
// 收編）。href 走 <a>、onClick 走 <button>；collapsed 縮成 icon 方塊；
// current 標記 aria-current="page"。
import React from "react";

const rowBase = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  width: "100%",
  padding: "7px 10px",
  fontSize: "var(--anila-text-md)",
  fontWeight: "var(--anila-weight-medium)",
  fontFamily: "var(--anila-font-sans)",
  textAlign: "left",
  background: "transparent",
  border: "1px solid transparent",
  borderRadius: "var(--anila-radius-md)",
  color: "var(--anila-color-fg)",
  cursor: "pointer",
  textDecoration: "none",
  boxSizing: "border-box",
};

function hoverOn(e) {
  e.currentTarget.style.background = "var(--anila-color-bg-elev)";
}
function hoverOff(e) {
  e.currentTarget.style.background = "transparent";
}

export function NavItem({ icon: Icon, label, href, onClick, current, collapsed }) {
  const ariaCurrent = current ? "page" : undefined;

  const style = collapsed
    ? {
        ...rowBase,
        width: 36,
        height: 36,
        padding: 0,
        justifyContent: "center",
        color: current ? "var(--anila-color-fg)" : "var(--anila-color-fg-muted)",
      }
    : rowBase;

  const body = collapsed ? (
    <Icon size={18} />
  ) : (
    <>
      <Icon
        size={15}
        style={{ color: "var(--anila-color-fg-muted)", flexShrink: 0 }}
      />
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

export function NavGroup({ children, collapsed, "aria-label": ariaLabel }) {
  return (
    <nav
      aria-label={ariaLabel}
      style={
        collapsed
          ? { display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }
          : { display: "flex", flexDirection: "column", gap: 2, padding: "0 10px 8px" }
      }
    >
      {children}
    </nav>
  );
}
