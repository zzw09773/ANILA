// Button — 自 apps/anila-shell src/components.jsx 收編升級：API 相容
// （variant / size / leftIcon / rightIcon），樣式改吃 --anila-* tokens。
import React from "react";

export const Button = ({
  variant = "default",
  size = "md",
  children,
  leftIcon,
  rightIcon,
  className = "",
  ...rest
}) => {
  const base = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    border: "1px solid transparent",
    borderRadius: "var(--anila-radius-md)",
    fontFamily: "var(--anila-font-sans)",
    fontWeight: "var(--anila-weight-medium)",
    fontSize: size === "sm" ? "var(--anila-text-sm)" : "var(--anila-text-md)",
    padding:
      size === "sm" ? "4px 10px" : size === "lg" ? "9px 16px" : "6px 12px",
    transition: "all .12s ease",
    whiteSpace: "nowrap",
    userSelect: "none",
    background: "transparent",
    color: "var(--anila-color-fg)",
    cursor: "pointer",
  };
  const variants = {
    default: {
      background: "var(--anila-color-bg-elev)",
      borderColor: "var(--anila-color-border)",
    },
    primary: {
      background: "var(--anila-color-accent)",
      color: "var(--anila-color-accent-fg)",
      borderColor: "var(--anila-color-accent)",
    },
    ghost: { background: "transparent", color: "var(--anila-color-fg-muted)" },
    subtle: {
      background: "var(--anila-color-bg-subtle)",
      borderColor: "var(--anila-color-border)",
    },
    danger: {
      background: "transparent",
      color: "var(--anila-color-danger)",
      borderColor: "var(--anila-color-border)",
    },
  };
  return (
    <button
      {...rest}
      className={className}
      style={{ ...base, ...variants[variant], ...(rest.style || {}) }}
      onMouseEnter={(e) => {
        if (variant !== "primary")
          e.currentTarget.style.background = "var(--anila-color-bg-subtle)";
        rest.onMouseEnter?.(e);
      }}
      onMouseLeave={(e) => {
        Object.assign(e.currentTarget.style, variants[variant]);
        rest.onMouseLeave?.(e);
      }}
    >
      {leftIcon}
      {children}
      {rightIcon}
    </button>
  );
};

// 純圖示按鈕 — title 會鏡射進 aria-label（icon-only 需可及性名稱）。
export const IconButton = ({ children, active, title, className = "", ...rest }) => (
  <button
    aria-label={title}
    {...rest}
    title={title}
    className={className}
    style={{
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      width: 30,
      height: 30,
      background: active ? "var(--anila-color-bg-subtle)" : "transparent",
      color: active ? "var(--anila-color-fg)" : "var(--anila-color-fg-muted)",
      border:
        "1px solid " + (active ? "var(--anila-color-border)" : "transparent"),
      borderRadius: "var(--anila-radius-md)",
      cursor: "pointer",
      transition: "all .12s ease",
      ...(rest.style || {}),
    }}
    onMouseEnter={(e) => {
      e.currentTarget.style.background = "var(--anila-color-bg-subtle)";
      e.currentTarget.style.color = "var(--anila-color-fg)";
      rest.onMouseEnter?.(e);
    }}
    onMouseLeave={(e) => {
      e.currentTarget.style.background = active
        ? "var(--anila-color-bg-subtle)"
        : "transparent";
      e.currentTarget.style.color = active
        ? "var(--anila-color-fg)"
        : "var(--anila-color-fg-muted)";
      rest.onMouseLeave?.(e);
    }}
  >
    {children}
  </button>
);
