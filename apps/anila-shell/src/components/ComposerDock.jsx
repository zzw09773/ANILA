// Bottom of the chat column: optional agent/model pickers, the
// model-unavailable notice, the Composer itself, and the disclaimer line.
// Composer (chat.jsx) owns input behaviour; a "/" skills picker hooks in there
// and reads `skills` (useUsableSkills) passed through from here.

import { Composer } from "../chat.jsx";
import { IconLock } from "../icons.jsx";

export function ComposerDock({
  agentControls, encryptionRequired, modelNotice,
  isRouter, activeAgentName,
  // Composer
  onSend, onStop, streaming, restoredDraft, skills, agents,
  redactionMode, onChangeRedactionMode, conversationId, presetPrompts,
  deepThinkNext, onDeepThinkNextChange,
  queuedAttachment, onQueuedAttachmentConsumed,
  onUpload, onFetchAttachmentMeta,
  insertRequest, onInsertConsumed, ghostSuggestion, onTyping,
  maxSkills, onToggleSkillFavorite,
}) {
  return (
    <div style={{ padding: "0 var(--density) var(--density)", background: "var(--bg)" }}>
      <div style={{ maxWidth: 760, margin: "0 auto" }}>
        {agentControls && (
          <div style={{ marginBottom: 8, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            {agentControls}
            {encryptionRequired && (
              <span title="此 agent 為列管模型（受控存取）" style={{
                display: "inline-flex", alignItems: "center", gap: 3,
                padding: "1px 7px",
                background: "oklch(0.95 0.02 25 / 0.4)",
                border: "1px solid var(--danger)",
                borderRadius: 999,
                fontSize: 11, color: "var(--danger)",
                fontFamily: "var(--font-mono)",
              }}>
                <IconLock size={10} /> 列管模型
              </span>
            )}
          </div>
        )}
        {modelNotice ? (
          <div
            role="alert"
            data-testid="model-unavailable-notice"
            style={{
              marginBottom: 8,
              padding: "10px 12px",
              borderRadius: "var(--radius)",
              border: "1px solid var(--danger)",
              background: "color-mix(in oklch, var(--danger) 12%, var(--bg))",
              color: "var(--danger)",
              fontSize: 14,
              lineHeight: 1.55,
            }}
          >
            {modelNotice}
          </div>
        ) : null}
        <Composer
          onSend={onSend}
          restoredDraft={restoredDraft}
          skills={skills}
          maxSkills={maxSkills}
          onToggleSkillFavorite={onToggleSkillFavorite}
          agents={agents}
          redactionMode={redactionMode}
          onChangeRedactionMode={onChangeRedactionMode}
          conversationId={conversationId}
          presetPrompts={presetPrompts}
          deepThinkNext={deepThinkNext}
          onDeepThinkNextChange={onDeepThinkNextChange}
          streaming={streaming}
          onStop={onStop}
          placeholder="傳訊息給 ANILA，Shift+Enter 換行"
          footer={
            isRouter
              ? "ANILA 會幫你找合適的助手"
              : `已指定助手 · ${activeAgentName}`
          }
          queuedAttachment={queuedAttachment}
          onQueuedAttachmentConsumed={onQueuedAttachmentConsumed}
          onUpload={onUpload}
          onFetchAttachmentMeta={onFetchAttachmentMeta}
          insertRequest={insertRequest}
          onInsertConsumed={onInsertConsumed}
          ghostSuggestion={ghostSuggestion}
          onTyping={onTyping}
        />
        <div style={{
          marginTop: 6, fontSize: 11,
          color: "var(--fg-subtle)", textAlign: "center",
          fontFamily: "var(--font-mono)",
        }}>
          ANILA {isRouter
            ? "會幫你找合適的助手"
            : `→ ${activeAgentName}`}
          {" · 所有呼叫經 CSP · "}
          <span
            style={{ color: "var(--fg-muted)" }}
            title="本系統由大型語言模型(LLM)驅動,輸出內容可能包含錯誤或偏誤,僅供參考、不可作為唯一決策依據。"
          >
            AI 系統 · 內容僅供參考
          </span>
        </div>
      </div>
    </div>
  );
}
