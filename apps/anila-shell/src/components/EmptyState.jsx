// Empty conversation state: ANILA mark plus starter prompt cards.

import React from "react";
import { AnilaLogoImg } from "../AnilaBrand.jsx";
import { ROUTER_AGENT, buildStarterPrompts } from "../runtime/chatHelpers.js";

export function EmptyState({ agent, agents, onPick, loading }) {
  const prompts = buildStarterPrompts(agents);
  return (
    <div style={{ padding: "72px 16px 32px", textAlign: "center" }}>
      <AnilaLogoImg variant="mark" height={28} style={{ margin: "0 auto" }} />
      <div style={{ marginTop: 20, fontSize: 22, fontWeight: 600, letterSpacing: -0.3 }}>
        你今天想問 ANILA 什麼？
      </div>
      <div style={{ marginTop: 8, color: "var(--fg-muted)", fontSize: 14 }}>
        {loading
          ? "agent 清單載入中…"
          : agent?.id === ROUTER_AGENT.id
            ? "輸入問題，ANILA 會幫你找合適的助手；也可以用 @名稱 直接指定"
            : `當前助手： ${agent?.name}`}
      </div>
      {prompts.length === 0 ? (
        <div style={{ marginTop: 28, color: "var(--fg-muted)", fontSize: 14 }}>
          目前沒有核對過、查得到的範例問句。
        </div>
      ) : (
        <div style={{
          marginTop: 32, display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))",
          gap: 8,
          maxWidth: 720, margin: "32px auto 0", textAlign: "left",
        }}>
          {prompts.map((s) => {
            const isPrimary = s.primary === true;
            return (
              <button key={s.id || s.q} type="button" onClick={() => onPick(s.q)} style={{
                padding: isPrimary ? "14px 16px" : "12px 14px",
                background: "var(--bg-elev)",
                border: "1px solid " + (isPrimary ? "var(--border-strong)" : "var(--border)"),
                borderRadius: "var(--radius)",
                cursor: "pointer", textAlign: "left",
                transition: "border-color .12s",
                fontFamily: "inherit",
                color: "var(--fg)",
              }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = "var(--border-strong)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = isPrimary
                    ? "var(--border-strong)"
                    : "var(--border)";
                }}>
                <div style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: "var(--fg)",
                }}>{s.title}</div>
                <div style={{ fontSize: 13, color: "var(--fg-muted)", marginTop: 4 }}>{s.sub}</div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
