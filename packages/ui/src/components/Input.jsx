// Input — 自 shell components.jsx 收編：label / hint / error / leftIcon /
// rightEl API 相容；焦點邊框改官方藍。
import React, { useState } from "react";

export const Input = ({ label, hint, error, leftIcon, rightEl, ...rest }) => {
  const [focused, setFocused] = useState(false);

  return (
    <label style={{ display: "block", fontFamily: "var(--anila-font-sans)" }}>
    {label && (
      <div
        style={{
          fontSize: "var(--anila-text-sm)",
          fontWeight: "var(--anila-weight-medium)",
          color: "var(--anila-color-fg-muted)",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
    )}
    <div
      style={{
        display: "flex",
        alignItems: "center",
        background: "var(--anila-color-bg-elev)",
        border: `1px solid ${
          error
            ? "var(--anila-color-danger)"
            : focused
              ? "var(--anila-color-accent)"
              : "var(--anila-color-border)"
        }`,
        borderRadius: "var(--anila-radius-md)",
        transition: "border-color .12s",
      }}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setFocused(false);
      }}
    >
      {leftIcon && (
        <span
          style={{
            color: "var(--anila-color-fg-subtle)",
            paddingLeft: 10,
            display: "flex",
          }}
        >
          {leftIcon}
        </span>
      )}
      <input
        {...rest}
        style={{
          flex: 1,
          minWidth: 0,
          background: "transparent",
          border: "none",
          outline: "none",
          padding: "8px 10px",
          fontSize: "var(--anila-text-md)",
          fontFamily: "inherit",
          color: "var(--anila-color-fg)",
          ...(rest.style || {}),
        }}
      />
      {rightEl && (
        <span style={{ paddingRight: 4, display: "flex" }}>{rightEl}</span>
      )}
    </div>
    {error && (
      <div
        style={{
          fontSize: "var(--anila-text-xs)",
          color: "var(--anila-color-danger)",
          marginTop: 4,
        }}
      >
        {error}
      </div>
    )}
    {hint && !error && (
      <div
        style={{
          fontSize: "var(--anila-text-xs)",
          color: "var(--anila-color-fg-subtle)",
          marginTop: 4,
        }}
      >
        {hint}
      </div>
    )}
    </label>
  );
};
