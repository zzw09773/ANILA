// Select — 原生 <select> 的樣式包裝（air-gap／可及性優先：不自造 listbox）。
// API 對齊 Input：label / hint / error；選項用 options 或 children 二擇一。
import React, { useState } from "react";
import { IconChevDown } from "../icons.jsx";

export const Select = ({
  label,
  hint,
  error,
  options,
  children,
  ...rest
}) => {
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
        position: "relative",
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
      <select
        {...rest}
        style={{
          flex: 1,
          minWidth: 0,
          appearance: "none",
          WebkitAppearance: "none",
          background: "transparent",
          border: "none",
          outline: "none",
          padding: "8px 28px 8px 10px",
          fontSize: "var(--anila-text-md)",
          fontFamily: "inherit",
          color: "var(--anila-color-fg)",
          cursor: "pointer",
          ...(rest.style || {}),
        }}
      >
        {options
          ? options.map((opt) => (
              <option key={opt.value} value={opt.value} disabled={opt.disabled}>
                {opt.label}
              </option>
            ))
          : children}
      </select>
      <span
        aria-hidden="true"
        style={{
          position: "absolute",
          right: 8,
          display: "flex",
          pointerEvents: "none",
          color: "var(--anila-color-fg-subtle)",
        }}
      >
        <IconChevDown size={14} />
      </span>
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
