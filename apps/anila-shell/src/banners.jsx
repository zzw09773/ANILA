// Admin announcement banner bar. Shows active banners at the top of the chat
// area; each is dismissable and the dismissal is remembered per-user in
// localStorage (anila-dismissed-banners). Plain text content (no HTML exec).
import React from "react";
import { IconX } from "./icons.jsx";

const LEVEL_STYLE = {
  info:    { bg: "var(--accent-soft)", fg: "var(--fg)", border: "var(--accent)" },
  warning: { bg: "oklch(0.95 0.06 85)", fg: "oklch(0.35 0.10 75)", border: "var(--warn)" },
  error:   { bg: "oklch(0.95 0.05 25)", fg: "oklch(0.40 0.15 25)", border: "var(--danger)" },
  success: { bg: "oklch(0.95 0.05 150)", fg: "oklch(0.35 0.10 150)", border: "var(--success)" },
};

export function BannerBar({ banners, onDismiss }) {
  if (!banners || banners.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {banners.map((b) => {
        const s = LEVEL_STYLE[b.level] || LEVEL_STYLE.info;
        return (
          <div key={b.id} style={{
            display: "flex", alignItems: "flex-start", gap: 10,
            padding: "8px 16px", background: s.bg, color: s.fg,
            borderBottom: `1px solid ${s.border}`, fontSize: 13, lineHeight: 1.5,
          }}>
            <span style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{b.content}</span>
            <button
              onClick={() => onDismiss?.(b.id)}
              aria-label="關閉公告"
              style={{ display: "inline-flex", background: "transparent", border: "none", cursor: "pointer", color: "inherit", opacity: 0.6, padding: 2, flexShrink: 0 }}
            ><IconX size={14} /></button>
          </div>
        );
      })}
    </div>
  );
}
