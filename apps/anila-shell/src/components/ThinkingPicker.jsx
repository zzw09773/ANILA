import React, { useEffect, useRef, useState } from "react";
import { IconChevDown, IconCheck } from "../icons.jsx";
import {
  GLM_OFF_REASON,
  isGlmFamily,
  isThinkingOptionActive,
  thinkingPickerOptions,
  thinkingTriggerLabel,
} from "../runtime/thinkingTier.js";

export default function ThinkingPicker({
  value = "default",
  model = null,
  disabled = false,
  error = "",
  onChange,
}) {
  const levelsSupported = model == null ? null : model.thinking_levels_supported;
  const adminLocked = model?.thinking_user_selectable === false;
  const locked = disabled || adminLocked;
  const options = thinkingPickerOptions(levelsSupported, model);
  const selectedLabel = thinkingTriggerLabel(value, levelsSupported, model);
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) setOpen(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (locked) setOpen(false);
  }, [locked]);

  return (
    <div className="thinking-picker" ref={rootRef} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        aria-label="思考"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={locked}
        title={adminLocked ? "管理員已鎖定此模型的思考程度" : "此則對話的思考程度"}
        onClick={() => {
          if (!locked) setOpen((current) => !current);
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "transparent",
          border: "1px solid " + (open ? "var(--border-strong)" : "var(--border)"),
          borderRadius: "var(--radius)",
          padding: "5px 8px 5px 10px",
          cursor: locked ? "not-allowed" : "pointer",
          color: "var(--fg)",
          opacity: locked ? 0.55 : 1,
          fontFamily: "inherit",
        }}
      >
        <span style={{ fontWeight: 500, fontSize: 13, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {selectedLabel}
        </span>
        <IconChevDown size={14} style={{ color: "var(--fg-muted)" }} />
      </button>
      {open ? (
        <div
          role="listbox"
          aria-label="思考程度"
          style={{
            position: "absolute",
            left: 0,
            bottom: "calc(100% + 6px)",
            width: 260,
            maxHeight: 320,
            overflowY: "auto",
            zIndex: 80,
            background: "var(--bg-elev)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-lg)",
            boxShadow: "0 12px 32px -8px oklch(0.10 0 0 / 0.18)",
            padding: 4,
          }}
        >
          <div style={{ padding: "6px 10px 8px", fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>
            思考程度
          </div>
          {options.map((option) => {
            const active = isThinkingOptionActive(option.tier, value, levelsSupported);
            return (
              <button
                key={option.tier}
                type="button"
                role="option"
                aria-selected={active}
                disabled={!option.enabled}
                title={option.reason || undefined}
                onClick={() => {
                  if (!option.enabled) return;
                  if (typeof onChange === "function") onChange(option.tier);
                  setOpen(false);
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  width: "100%",
                  padding: "8px 10px",
                  background: active ? "var(--bg-subtle)" : "transparent",
                  border: "none",
                  borderRadius: "var(--radius)",
                  color: "var(--fg)",
                  textAlign: "left",
                  cursor: option.enabled ? "pointer" : "not-allowed",
                  opacity: option.enabled ? 1 : 0.45,
                  fontSize: 13,
                  fontFamily: "inherit",
                }}
              >
                <span style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 500 }}>{option.label}</div>
                  {option.reason ? (
                    <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 1 }}>{option.reason}</div>
                  ) : null}
                </span>
                {active ? <IconCheck size={14} style={{ color: "var(--accent)" }} /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
      {value === "deep" ? (
        <span className="thinking-picker__hint" style={{ display: "block", marginTop: 4, fontSize: 11, color: "var(--fg-subtle)" }}>
          思考會用掉較多時間與額度
        </span>
      ) : null}
      {value === "off" && isGlmFamily(model) ? (
        <span className="thinking-picker__hint" style={{ display: "block", marginTop: 4, fontSize: 11, color: "var(--fg-subtle)" }}>
          {GLM_OFF_REASON}
        </span>
      ) : null}
      {error ? (
        <span className="thinking-picker__error" style={{ display: "block", marginTop: 4, fontSize: 12, color: "var(--danger)" }}>
          {error}
        </span>
      ) : null}
    </div>
  );
}
