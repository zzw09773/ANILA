// 設定 → 記憶：使用者自寫回覆偏好 + 已記住事實／對話片段。
import React, { useCallback, useEffect, useState } from "react";

import { useConfirm, useToast } from "./confirm.jsx";
import { IconTrash } from "./icons.jsx";
import {
  REPLY_STYLE_KEY,
  clearChunks as apiClearMemoryChunks,
  clearFacts as apiClearMemoryFacts,
  deleteFact as apiDeleteMemoryFact,
  getPreference as apiGetPreference,
  listChunks as apiListMemoryChunks,
  listFacts as apiListMemoryFacts,
  putPreference as apiPutPreference,
} from "./runtime/memory.js";

/**
 * @param {{
 *   authRequest: (path: string, options?: object) => Promise<any>,
 *   onOpenConversation?: (id: number) => void,
 * }} props
 */
export function MemoryTab({ authRequest, onOpenConversation }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [factsState, setFactsState] = useState({ loading: true, error: null, facts: [], total: 0 });
  const [chunksState, setChunksState] = useState({
    loading: true, error: null, items: [],
    total: 0, encrypted_total: 0, distinct_conversations: 0,
  });
  const [prefText, setPrefText] = useState("");
  const [prefSaved, setPrefSaved] = useState("");
  const [prefBusy, setPrefBusy] = useState(false);

  const reload = useCallback(async () => {
    setFactsState((s) => ({ ...s, loading: true, error: null }));
    setChunksState((s) => ({ ...s, loading: true, error: null }));
    try {
      const [facts, chunks, pref] = await Promise.all([
        apiListMemoryFacts(authRequest),
        apiListMemoryChunks(authRequest, { limit: 25 }),
        apiGetPreference(authRequest),
      ]);
      const text = typeof pref?.text === "string" ? pref.text : "";
      setPrefText(text);
      setPrefSaved(text);
      const listed = (facts.facts || []).filter((f) => f.key !== REPLY_STYLE_KEY);
      setFactsState({
        loading: false, error: null,
        facts: listed, total: listed.length,
      });
      setChunksState({
        loading: false, error: null,
        items: chunks.items || [],
        total: chunks.total || 0,
        encrypted_total: chunks.encrypted_total || 0,
        distinct_conversations: chunks.distinct_conversations || 0,
      });
    } catch (err) {
      const msg = err?.message || "載入失敗";
      setFactsState((s) => ({ ...s, loading: false, error: msg }));
      setChunksState((s) => ({ ...s, loading: false, error: msg }));
    }
  }, [authRequest]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onSavePref = async () => {
    setPrefBusy(true);
    try {
      const saved = await apiPutPreference(authRequest, prefText);
      const text = typeof saved?.text === "string" ? saved.text : prefText.trim();
      setPrefText(text);
      setPrefSaved(text);
      toast("回覆偏好已儲存", { tone: "success" });
    } catch (err) {
      toast(err?.message || "儲存失敗", { tone: "error" });
    } finally {
      setPrefBusy(false);
    }
  };

  const onDeleteFact = async (id, key) => {
    if (!(await confirm({
      title: "刪除事實",
      message: `刪除事實「${key}」？此動作無法復原。`,
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    try {
      await apiDeleteMemoryFact(authRequest, id);
      await reload();
    } catch (err) {
      toast(err?.message || "刪除失敗", { tone: "error" });
    }
  };

  const onClearFacts = async () => {
    if (factsState.total === 0) return;
    if (!(await confirm({
      title: "清空事實",
      message: `清空全部 ${factsState.total} 筆事實？此動作無法復原。回覆偏好不會被清掉。`,
      confirmText: "清空",
      tone: "danger",
    }))) return;
    try {
      await apiClearMemoryFacts(authRequest);
      await reload();
    } catch (err) {
      toast(err?.message || "清空失敗", { tone: "error" });
    }
  };

  const onClearChunks = async () => {
    if (chunksState.total === 0) return;
    if (!(await confirm({
      title: "清空對話片段",
      message:
        `清空全部 ${chunksState.total} 段對話片段？\n` +
        `這會抹除跨對話語意檢索的記憶（已記住的事實不受影響）。\n` +
        `此動作無法復原。`,
      confirmText: "清空",
      tone: "danger",
    }))) return;
    try {
      await apiClearMemoryChunks(authRequest);
      await reload();
    } catch (err) {
      toast(err?.message || "清空失敗", { tone: "error" });
    }
  };

  const prefDirty = prefText !== prefSaved;

  return (
    <div style={{ display: "grid", gap: 18, fontSize: 13 }}>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
        回覆偏好由你自己寫，下一則對話就會帶進模型。平台也會在每輪之後萃取穩定事實、
        並把訊息向量化做跨對話檢索。所有資料只屬於你，不與其他人共享。
      </div>

      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <div style={{ fontWeight: 500 }}>回覆偏好</div>
          <button
            type="button"
            data-testid="memory-pref-save"
            disabled={prefBusy || !prefDirty}
            onClick={onSavePref}
            style={{
              fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius)",
              background: "transparent",
              border: "1px solid var(--border)",
              color: prefDirty ? "var(--accent)" : "var(--fg-subtle)",
              cursor: prefBusy || !prefDirty ? "default" : "pointer",
            }}
          >
            {prefBusy ? "儲存中…" : "儲存"}
          </button>
        </div>
        <textarea
          data-testid="memory-pref-input"
          value={prefText}
          onChange={(e) => setPrefText(e.target.value)}
          rows={4}
          maxLength={2000}
          placeholder="例如：請用繁體中文，先給結論再補細節。不要客套。"
          style={{
            width: "100%",
            boxSizing: "border-box",
            resize: "vertical",
            minHeight: 84,
            padding: 8,
            fontSize: 13,
            lineHeight: 1.5,
            fontFamily: "inherit",
            color: "var(--fg)",
            background: "var(--bg)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
          }}
        />
        <div style={{ fontSize: 10, color: "var(--fg-subtle)", marginTop: 6 }}>
          空白並儲存等於清除。這段不會被自動萃取覆寫。
        </div>
      </div>

      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <div style={{ fontWeight: 500 }}>
            已記住的事實 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>· {factsState.total}</span>
          </div>
          <button
            type="button"
            disabled={factsState.total === 0 || factsState.loading}
            onClick={onClearFacts}
            style={{
              fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius)",
              background: "transparent", border: "1px solid var(--border)",
              color: factsState.total === 0 ? "var(--fg-subtle)" : "var(--danger)",
              cursor: factsState.total === 0 ? "default" : "pointer",
            }}
          >清空全部</button>
        </div>
        {factsState.loading && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>載入中…</div>
        )}
        {factsState.error && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>{factsState.error}</div>
        )}
        {!factsState.loading && !factsState.error && factsState.facts.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
            目前還沒有萃取到任何事實。和 ANILA 多聊聊「我是誰、我喜歡什麼」之類的訊息，平台會自動學習。
          </div>
        )}
        {!factsState.loading && factsState.facts.length > 0 && (
          <div style={{ display: "grid", gap: 6 }}>
            {factsState.facts.map((f) => (
              <div key={f.id} style={{
                display: "grid",
                gridTemplateColumns: "minmax(90px, 1fr) 2fr auto auto auto",
                gap: 10, alignItems: "center",
                padding: "6px 8px",
                background: "var(--bg-subtle)",
                borderRadius: "var(--radius)",
                fontSize: 12,
              }}>
                <div style={{ fontFamily: "var(--font-mono)", color: "var(--fg-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.key}
                </div>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {f.value}
                </div>
                {f.source_conversation_id ? (
                  <button
                    type="button"
                    data-testid={`memory-fact-source-${f.id}`}
                    onClick={() => onOpenConversation?.(f.source_conversation_id)}
                    title="開啟來源對話"
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 10,
                      color: "var(--accent)",
                      background: "transparent",
                      border: "none",
                      cursor: onOpenConversation ? "pointer" : "default",
                      padding: 0,
                    }}
                  >
                    對話 #{f.source_conversation_id}
                  </button>
                ) : (
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--fg-subtle)" }}>
                    —
                  </span>
                )}
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--fg-subtle)" }}>
                  {(f.confidence * 100).toFixed(0)}%
                </div>
                <button
                  type="button"
                  onClick={() => onDeleteFact(f.id, f.key)}
                  title="刪除這筆事實"
                  style={{
                    width: 22, height: 22, padding: 0,
                    background: "transparent", border: "none",
                    color: "var(--fg-subtle)", cursor: "pointer",
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.color = "var(--danger)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.color = "var(--fg-subtle)"; }}
                >
                  <IconTrash size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <div style={{ fontWeight: 500 }}>
            對話片段索引 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>
              · {chunksState.total} 段 / {chunksState.distinct_conversations} 個對話
              {chunksState.encrypted_total > 0 && (
                <span style={{ marginLeft: 8, color: "var(--warning, var(--accent))" }}>
                  · {chunksState.encrypted_total} 段加密來源
                </span>
              )}
            </span>
          </div>
          <button
            type="button"
            disabled={chunksState.total === 0 || chunksState.loading}
            onClick={onClearChunks}
            style={{
              fontSize: 11, padding: "4px 10px", borderRadius: "var(--radius)",
              background: "transparent", border: "1px solid var(--border)",
              color: chunksState.total === 0 ? "var(--fg-subtle)" : "var(--danger)",
              cursor: chunksState.total === 0 ? "default" : "pointer",
            }}
          >清空全部</button>
        </div>
        {chunksState.loading && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>載入中…</div>
        )}
        {chunksState.error && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>{chunksState.error}</div>
        )}
        {!chunksState.loading && !chunksState.error && chunksState.items.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
            目前還沒有對話片段索引。對話幾輪之後再回來看。
          </div>
        )}
        {!chunksState.loading && chunksState.items.length > 0 && (
          <div style={{ display: "grid", gap: 4, maxHeight: 240, overflowY: "auto" }}>
            {chunksState.items.map((c) => (
              <div key={c.id} style={{
                fontSize: 11, padding: "4px 6px",
                fontFamily: "var(--font-mono)",
                color: c.is_encrypted ? "var(--fg)" : "var(--fg-muted)",
              }}>
                {onOpenConversation ? (
                  <button
                    type="button"
                    data-testid={`memory-chunk-source-${c.id}`}
                    onClick={() => onOpenConversation(c.conversation_id)}
                    style={{
                      display: "inline-block", minWidth: 70,
                      color: "var(--accent)",
                      background: "transparent",
                      border: "none",
                      cursor: "pointer",
                      padding: 0,
                      fontFamily: "inherit",
                      fontSize: "inherit",
                    }}
                  >
                    {c.role === "user" ? "user" : "asst"} · #{c.conversation_id}
                  </button>
                ) : (
                  <span style={{ display: "inline-block", minWidth: 70, color: "var(--fg-subtle)" }}>
                    {c.role === "user" ? "user" : "asst"} · #{c.conversation_id}
                  </span>
                )}
                {c.is_encrypted && <span style={{ marginRight: 4 }}>🔒</span>}
                <span style={{ color: "var(--fg)" }}>{c.content}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ fontSize: 10, color: "var(--fg-subtle)", lineHeight: 1.6 }}>
        清空事實後立即生效；回覆偏好要另外儲存。下次對話起，平台會從新內容重新學習。
        若需暫時停用記憶整合，請聯絡管理員。
      </div>
    </div>
  );
}
