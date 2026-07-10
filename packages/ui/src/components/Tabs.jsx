// Tabs — 分段切換器（segmented control 語彙，對齊 shell 側欄「對話/Agents」
// 切換的視覺）。完整 tablist 語意 + 左右方向鍵切換。
import React from "react";

export const Tabs = ({ tabs = [], value, onChange, "aria-label": ariaLabel }) => {
  const ids = tabs.map((t) => t.id);

  const onKeyDown = (e) => {
    const idx = ids.indexOf(value);
    if (idx < 0) return;
    let next = null;
    if (e.key === "ArrowRight") next = ids[(idx + 1) % ids.length];
    if (e.key === "ArrowLeft") next = ids[(idx - 1 + ids.length) % ids.length];
    if (next) {
      e.preventDefault();
      onChange?.(next);
    }
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel || "分頁"}
      onKeyDown={onKeyDown}
      style={{ display: "flex", gap: 2, fontFamily: "var(--anila-font-sans)" }}
    >
      {tabs.map((t) => {
        const active = t.id === value;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange?.(t.id)}
            style={{
              flex: 1,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 5,
              padding: "5px 8px",
              fontSize: "var(--anila-text-sm)",
              fontWeight: "var(--anila-weight-medium)",
              background: active ? "var(--anila-color-bg-elev)" : "transparent",
              border:
                "1px solid " +
                (active ? "var(--anila-color-border)" : "transparent"),
              borderRadius: "var(--anila-radius-md)",
              color: active
                ? "var(--anila-color-fg)"
                : "var(--anila-color-fg-muted)",
              cursor: "pointer",
            }}
          >
            {t.icon}
            {t.label}
          </button>
        );
      })}
    </div>
  );
};
