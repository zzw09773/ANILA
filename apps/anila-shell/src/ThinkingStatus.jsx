import React from "react";
import { ThinkingOrb } from "thinking-orbs";

const STATE_FROM_LABEL = [
  [/搜尋|檢索|search/i, "searching"],
  [/連線|連接|connect/i, "connecting"],
  [/聽|語音|listen/i, "listening"],
  [/寫|撰|compose/i, "composing"],
  [/解|算|solv/i, "solving"],
];

export function orbStateFromLabel(label) {
  const text = String(label || "");
  for (const [re, state] of STATE_FROM_LABEL) {
    if (re.test(text)) return state;
  }
  return "working";
}

export function ThinkingStatus({
  state = "working",
  size = 20,
  label = "思考中",
  style,
}) {
  return (
    <span
      role="status"
      aria-live="polite"
      aria-label={label || "思考中"}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        color: "var(--fg-muted)",
        fontSize: 13,
        ...style,
      }}
    >
      <ThinkingOrb state={state} size={size} theme="auto" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}
