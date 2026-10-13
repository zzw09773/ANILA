import {
  AUDIENCE_DISMISS_LABEL,
  AUDIENCE_OPTIONS,
  AUDIENCE_PROMPT_HINT,
  AUDIENCE_PROMPT_TITLE,
  audienceIdFromMarker,
  labelForAudience,
  stripAudienceMarker,
} from "../runtime/audienceDraft.js";

const optionStyle = {
  padding: "4px 10px",
  fontSize: 13,
  lineHeight: 1.4,
  background: "var(--bg)",
  border: "1px solid var(--border-strong)",
  borderRadius: 999,
  color: "var(--fg)",
  cursor: "pointer",
};

/**
 * 四個對象按鈕。沒有預設、沒有第五個對象。
 * onChoose 只在同仁點下去時呼叫，參數是選項 id。
 */
export function AudiencePrompt({ onChoose, onDismiss }) {
  return (
    <div data-testid="audience-prompt" style={{ padding: "8px 12px 4px" }}>
      <div role="group" aria-label={AUDIENCE_PROMPT_TITLE}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--fg)" }}>
          {AUDIENCE_PROMPT_TITLE}
        </div>
        <p style={{
          margin: "4px 0 8px",
          fontSize: 12,
          lineHeight: 1.5,
          color: "var(--fg-muted)",
        }}
        >
          {AUDIENCE_PROMPT_HINT}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {AUDIENCE_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={false}
              data-audience-id={option.id}
              onClick={() => onChoose?.(option.id)}
              style={optionStyle}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
      {typeof onDismiss === "function" ? (
        <button
          type="button"
          data-testid="audience-dismiss"
          onClick={onDismiss}
          style={{
            marginTop: 6,
            padding: 0,
            border: "none",
            background: "transparent",
            color: "var(--fg-subtle)",
            fontSize: 12,
            cursor: "pointer",
          }}
        >
          {AUDIENCE_DISMISS_LABEL}
        </button>
      ) : null}
    </div>
  );
}

/** 氣泡只顯示同仁已經選過的對象，不再變成一組按鈕。 */
export function UserDraftText({ text }) {
  const audienceId = audienceIdFromMarker(text);
  const label = labelForAudience(audienceId);
  if (!label) return text ?? "";
  const body = stripAudienceMarker(text).trim();
  return (
    <>
      {body}
      <div
        data-testid="audience-chosen"
        style={{ marginTop: 8, fontSize: 12, color: "var(--fg-muted)" }}
      >
        {AUDIENCE_PROMPT_TITLE}：{label}
      </div>
    </>
  );
}
