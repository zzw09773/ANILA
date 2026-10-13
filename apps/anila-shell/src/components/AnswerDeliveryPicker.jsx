import React, { useState } from "react";

const OPTIONS = [
  {
    value: "short",
    label: "快速短回答",
    hint: "這則會先給簡短結論。",
  },
  {
    value: "background",
    label: "完整回答，做好再通知我",
    hint: "送出後可以先去做別的事，做好會用通知告訴你。",
  },
];

const SR_ONLY = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
};

// 說明不再常駐在選項下方；滑鼠移上去或鍵盤聚焦時以 tooltip 顯示。
// 說明文字一直在 DOM 裡（aria-describedby），螢幕閱讀器不必等 hover。
export default function AnswerDeliveryPicker({
  value = "short",
  groupName = "answer-delivery",
  onChange,
}) {
  const [tip, setTip] = useState(null);
  return (
    <div style={{ padding: "0 2px 8px" }}>
      <div
        role="radiogroup"
        aria-label="回答方式"
        style={{ display: "flex", flexWrap: "wrap", gap: 8 }}
      >
        {OPTIONS.map((option) => {
          const selected = value === option.value;
          const hintId = `${groupName}-hint-${option.value}`;
          const shown = tip === option.value;
          return (
            <span
              key={option.value}
              style={{ position: "relative", display: "inline-flex" }}
              onMouseEnter={() => setTip(option.value)}
              onMouseLeave={() => setTip((t) => (t === option.value ? null : t))}
            >
            <label
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid " + (selected ? "var(--accent)" : "var(--border)"),
                background: selected ? "var(--accent-soft)" : "transparent",
                color: "var(--fg)",
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              <input
                type="radio"
                name={groupName}
                value={option.value}
                checked={selected}
                aria-describedby={hintId}
                onFocus={() => setTip(option.value)}
                onBlur={() => setTip((t) => (t === option.value ? null : t))}
                onKeyDown={(e) => { if (e.key === "Escape") setTip(null); }}
                onChange={() => {
                  if (typeof onChange === "function") onChange(option.value);
                }}
              />
              {option.label}
            </label>
              <span
                id={hintId}
                role="tooltip"
                data-testid={`answer-delivery-tip-${option.value}`}
                data-visible={shown ? "true" : "false"}
                style={shown ? {
                  position: "absolute",
                  left: 0,
                  bottom: "calc(100% + 6px)",
                  zIndex: 90,
                  width: "max-content",
                  maxWidth: 260,
                  padding: "6px 8px",
                  borderRadius: 6,
                  background: "var(--fg)",
                  color: "var(--bg)",
                  fontSize: 11,
                  lineHeight: 1.45,
                  whiteSpace: "normal",
                  pointerEvents: "none",
                } : SR_ONLY}
              >
                {option.hint}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}
