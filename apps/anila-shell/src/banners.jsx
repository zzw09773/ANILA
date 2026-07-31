// Top-of-chat strips: admin announcements + the conversation-handoff inbox.
//
// Announcements are dismissable and the dismissal is remembered per-user in
// localStorage (anila-dismissed-banners). Plain text content (no HTML exec).
//
// 2026-07-31:交接收件匣也掛在這裡。它需要一個「一定在畫面上、而且跟目前
// 選到哪串對話無關」的位置——這條 strip 是唯一符合的。收件匣自己去打
// `GET /api/handoffs`,不吃 app.jsx 的狀態(見 collab.jsx HandoffInbox)。
import React from "react";
import { IconX } from "./icons.jsx";
import { useAuth } from "./runtime/auth.jsx";
import { HandoffInbox } from "./collab.jsx";

const LEVEL_STYLE = {
  info:    { bg: "var(--accent-soft)", fg: "var(--fg)", border: "var(--accent)" },
  warning: { bg: "oklch(0.95 0.06 85)", fg: "oklch(0.35 0.10 75)", border: "var(--warn)" },
  error:   { bg: "oklch(0.95 0.05 25)", fg: "oklch(0.40 0.15 25)", border: "var(--danger)" },
  success: { bg: "oklch(0.95 0.05 150)", fg: "oklch(0.35 0.10 150)", border: "var(--success)" },
};

export function BannerBar({ banners, onDismiss }) {
  const { authRequest, user } = useAuth();
  const rows = banners || [];
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <HandoffInbox authRequest={authRequest} currentUserId={user?.id} />
      {rows.map((b) => {
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
