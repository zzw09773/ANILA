// 設定 → 記憶：使用者自寫回覆偏好 + 已記住事實／對話片段。
import React, { useCallback, useEffect, useState } from "react";

import { useConfirm, useToast } from "./confirm.jsx";
import { IconTrash } from "./icons.jsx";
import {
  REPLY_STYLE_KEY,
  clearFacts as apiClearMemoryFacts,
  deleteFact as apiDeleteMemoryFact,
  deleteSummary as apiDeleteMemorySummary,
  getPreference as apiGetPreference,
  listFacts as apiListMemoryFacts,
  listSummaries as apiListMemorySummaries,
  putPreference as apiPutPreference,
  updateFact as apiUpdateMemoryFact,
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
  const [summariesState, setSummariesState] = useState({
    loading: true, error: null, items: [], total: 0,
  });
  const [editingId, setEditingId] = useState(null);
  const [editingValue, setEditingValue] = useState("");
  const [prefText, setPrefText] = useState("");
  const [prefSaved, setPrefSaved] = useState("");
  const [prefBusy, setPrefBusy] = useState(false);

  const reload = useCallback(async () => {
    setFactsState((s) => ({ ...s, loading: true, error: null }));
    setSummariesState((s) => ({ ...s, loading: true, error: null }));
    try {
      const [facts, summaries, pref] = await Promise.all([
        apiListMemoryFacts(authRequest),
        apiListMemorySummaries(authRequest),
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
      setSummariesState({
        loading: false, error: null,
        items: summaries.items || [],
        total: summaries.total || 0,
      });
    } catch (err) {
      const msg = err?.message || "載入失敗";
      setFactsState((s) => ({ ...s, loading: false, error: msg }));
      setSummariesState((s) => ({ ...s, loading: false, error: msg }));
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

  const onSaveFact = async (id) => {
    const value = editingValue.trim();
    if (!value) return;
    try {
      await apiUpdateMemoryFact(authRequest, id, value);
      setEditingId(null);
      await reload();
    } catch (err) {
      toast(err?.message || "儲存失敗", { tone: "error" });
    }
  };

  const onDeleteSummary = async (id) => {
    if (!(await confirm({
      title: "刪除摘要",
      message: "刪除這則對話摘要？之後就不會再被搜尋到。此動作無法復原。",
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    try {
      await apiDeleteMemorySummary(authRequest, id);
      await reload();
    } catch (err) {
      toast(err?.message || "刪除失敗", { tone: "error" });
    }
  };

  const prefDirty = prefText !== prefSaved;

  return (
    <div style={{ display: "grid", gap: 18, fontSize: 13 }}>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
        回覆偏好由你自己寫，下一則對話就會帶進模型。平台只固定附上事實與偏好；
        過往對話要等你提到「延續上次」這類需求時才搜尋摘要。所有資料只屬於你。
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
                gridTemplateColumns: "minmax(90px, 1fr) 2fr auto auto auto auto",
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
                  {editingId === f.id ? (
                    <input
                      data-testid={`memory-fact-value-${f.id}`}
                      value={editingValue}
                      onChange={(e) => setEditingValue(e.target.value)}
                      style={{
                        width: "100%",
                        fontSize: 12,
                        padding: "2px 4px",
                        border: "1px solid var(--border)",
                        borderRadius: 4,
                        background: "var(--bg)",
                        color: "var(--fg)",
                      }}
                    />
                  ) : f.value}
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
                {editingId === f.id ? (
                  <button
                    type="button"
                    data-testid={`memory-fact-save-${f.id}`}
                    onClick={() => onSaveFact(f.id)}
                    style={{
                      fontSize: 11, padding: "2px 6px",
                      background: "transparent", border: "1px solid var(--border)",
                      color: "var(--accent)", cursor: "pointer",
                    }}
                  >儲存</button>
                ) : (
                  <button
                    type="button"
                    data-testid={`memory-fact-edit-${f.id}`}
                    onClick={() => { setEditingId(f.id); setEditingValue(f.value); }}
                    title="修改這筆事實"
                    style={{
                      fontSize: 11, padding: "2px 6px",
                      background: "transparent", border: "none",
                      color: "var(--fg-subtle)", cursor: "pointer",
                    }}
                  >修改</button>
                )}
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
        <div style={{ fontWeight: 500, marginBottom: 8 }}>
          對話摘要 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>· {summariesState.total}</span>
        </div>
        {summariesState.loading && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>載入中…</div>
        )}
        {summariesState.error && (
          <div style={{ fontSize: 11, color: "var(--danger)" }}>{summariesState.error}</div>
        )}
        {!summariesState.loading && !summariesState.error && summariesState.items.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--fg-muted)" }}>
            目前還沒有對話摘要。一段對話閒置之後，或你另開新對話時，才會整理出來。
          </div>
        )}
        {!summariesState.loading && summariesState.items.length > 0 && (
          <div style={{ display: "grid", gap: 6 }}>
            {summariesState.items.map((item) => (
              <div key={item.id} style={{
                display: "grid",
                gridTemplateColumns: "1fr auto",
                gap: 10,
                alignItems: "center",
                padding: "6px 8px",
                background: "var(--bg-subtle)",
                borderRadius: "var(--radius)",
                fontSize: 12,
              }}>
                <button
                  type="button"
                  onClick={() => onOpenConversation?.(item.conversation_id)}
                  style={{
                    textAlign: "left",
                    background: "transparent",
                    border: "none",
                    color: "var(--fg)",
                    cursor: onOpenConversation ? "pointer" : "default",
                    padding: 0,
                    font: "inherit",
                  }}
                >
                  {item.summary}
                </button>
                <button
                  type="button"
                  data-testid={`memory-summary-delete-${item.id}`}
                  onClick={() => onDeleteSummary(item.id)}
                  title="刪除這則摘要"
                  style={{
                    width: 22, height: 22, padding: 0,
                    background: "transparent", border: "none",
                    color: "var(--fg-subtle)", cursor: "pointer",
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                  }}
                >
                  <IconTrash size={12} />
                </button>
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
