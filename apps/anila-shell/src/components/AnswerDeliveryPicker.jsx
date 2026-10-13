import React from "react";

const OPTIONS = [
  {
    value: "short",
    label: "快速短回答",
  },
  {
    value: "background",
    label: "完整回答，做好再通知我",
  },
];

export default function AnswerDeliveryPicker({
  value = "short",
  groupName = "answer-delivery",
  onChange,
}) {
  return (
    <div style={{ padding: "0 2px 8px" }}>
      <div
        role="radiogroup"
        aria-label="回答方式"
        style={{ display: "flex", flexWrap: "wrap", gap: 8 }}
      >
        {OPTIONS.map((option) => {
          const selected = value === option.value;
          return (
            <label
              key={option.value}
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
                onChange={() => {
                  if (typeof onChange === "function") onChange(option.value);
                }}
              />
              {option.label}
            </label>
          );
        })}
      </div>
      <div style={{ marginTop: 4, fontSize: 11, color: "var(--fg-subtle)" }}>
        {value === "background"
          ? "送出後可以先去做別的事，做好會用通知告訴你。"
          : "這則會先給簡短結論。"}
      </div>
    </div>
  );
}
