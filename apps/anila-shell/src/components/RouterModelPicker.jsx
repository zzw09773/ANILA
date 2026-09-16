import React, { useEffect, useRef, useState } from "react";
import { MenuItem } from "../components.jsx";
import { IconChevDown, IconCheck } from "../icons.jsx";

function healthLabel(status) {
  if (!status || status === "healthy") return "";
  if (status === "degraded") return "不穩";
  return "異常";
}

export default function RouterModelPicker({
  models = [],
  selectedId,
  defaultModelId,
  disabled = false,
  error = "",
  fallbackName = "",
  onChange,
}) {
  const options = Array.isArray(models) ? models : [];
  const selected = options.find((model) => model.id === selectedId) || null;
  const selectedName = selected
    ? (selected.display_name || selected.name)
    : (fallbackName || (options.length === 0 ? "沒有可用模型" : "選擇模型"));
  const locked = disabled || options.length === 0;
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
    <div className="router-model-picker" ref={rootRef} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        aria-label="此則對話使用的模型，僅自動選助手時可選"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={locked}
        onClick={() => {
          if (!locked) setOpen((value) => !value);
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
          {selectedName}
        </span>
        {selected && selected.id === defaultModelId ? (
          <span style={{ fontSize: 11, color: "var(--fg-subtle)" }}>預設</span>
        ) : null}
        <IconChevDown size={14} style={{ color: "var(--fg-muted)" }} />
      </button>
      {open ? (
        <div
          role="listbox"
          style={{
            position: "absolute",
            left: 0,
            bottom: "calc(100% + 6px)",
            width: 280,
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
            對話模型
          </div>
          {options.map((model) => {
            const name = model.display_name || model.name;
            const health = healthLabel(model.health_status);
            const isDefault = model.id === defaultModelId;
            const active = model.id === selectedId;
            return (
              <MenuItem
                key={model.id}
                active={active}
                onClick={() => {
                  if (typeof onChange === "function") onChange(model.id);
                  setOpen(false);
                }}
                rightIcon={active ? <IconCheck size={14} style={{ color: "var(--accent)" }} /> : null}
              >
                <div>
                  <div style={{ fontSize: 13, fontWeight: 500, display: "flex", alignItems: "center", gap: 6 }}>
                    {name}
                    {isDefault ? (
                      <span style={{ fontSize: 11, fontWeight: 400, color: "var(--fg-subtle)" }}>全院預設</span>
                    ) : null}
                  </div>
                  {health ? (
                    <div style={{ fontSize: 11, color: "var(--fg-muted)", marginTop: 1 }}>{health}</div>
                  ) : null}
                </div>
              </MenuItem>
            );
          })}
        </div>
      ) : null}
      {error ? (
        <span className="router-model-picker__error" style={{ display: "block", marginTop: 4, fontSize: 12, color: "var(--danger)" }}>
          {error}
        </span>
      ) : null}
    </div>
  );
}
