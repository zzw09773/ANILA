// 設定 → 記憶：使用者自寫回覆偏好 + 已記住事實／對話片段。
import React, { useCallback, useEffect, useRef, useState } from "react";

import { useConfirm, useToast } from "./confirm.jsx";
import { IconTrash } from "./icons.jsx";
import { bindExpectedUser, deleteSummaryBatches, positiveUserId } from "./runtime/bulkActions.js";
import {
  REPLY_STYLE_KEY,
  bulkDeleteSummaries as apiBulkDeleteSummaries,
  clearFacts as apiClearMemoryFacts,
  clearSummaries as apiClearSummaries,
  deleteFact as apiDeleteMemoryFact,
  deleteSummary as apiDeleteMemorySummary,
  getPreference as apiGetPreference,
  listFacts as apiListMemoryFacts,
  listSummaries as apiListMemorySummaries,
  putPreference as apiPutPreference,
  updateFact as apiUpdateMemoryFact,
} from "./runtime/memory.js";

const summaryBarButton = {
  fontSize: 11,
  padding: "3px 8px",
  background: "var(--bg-elev)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  color: "var(--fg)",
  cursor: "pointer",
  flex: "0 0 auto",
};

const summarySrOnly = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};

/**
 * @param {{
 *   authRequest: (path: string, options?: object) => Promise<any>,
 *   onOpenConversation?: (id: number) => void,
 *   userId?: number,
 * }} props
 */
export function MemoryTab({ authRequest, onOpenConversation, userId }) {
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
  const [summarySelectMode, setSummarySelectMode] = useState(false);
  const [summarySelected, setSummarySelected] = useState(() => new Set());
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [summaryStatus, setSummaryStatus] = useState("");
  const summaryBusyRef = useRef(false);
  const summaryOpRef = useRef(0);
  const summaryEpochRef = useRef(0);
  const summaryListEpochRef = useRef(0);
  const summaryMountedRef = useRef(true);
  const summaryAuthRef = useRef(authRequest);
  if (summaryAuthRef.current !== authRequest) {
    summaryAuthRef.current = authRequest;
    summaryEpochRef.current += 1;
    summaryOpRef.current += 1;
    summaryBusyRef.current = false;
    summaryListEpochRef.current += 1;
  }
  const summaryUserRef = useRef(userId);
  if (summaryUserRef.current !== userId) {
    summaryUserRef.current = userId;
    summaryEpochRef.current += 1;
    summaryOpRef.current += 1;
    summaryBusyRef.current = false;
    summaryListEpochRef.current += 1;
  }

  const summaryViewCurrent = useCallback((epoch, request) => (
    summaryMountedRef.current
    && epoch === summaryEpochRef.current
    && request === summaryAuthRef.current
  ), []);

  const reload = useCallback(async () => {
    const epoch = summaryEpochRef.current;
    const listEpoch = summaryListEpochRef.current;
    const request = authRequest;
    setFactsState((s) => ({ ...s, loading: true, error: null }));
    setSummariesState((s) => ({ ...s, loading: true, error: null }));
    try {
      const [facts, summaries, pref] = await Promise.all([
        apiListMemoryFacts(request),
        apiListMemorySummaries(request),
        apiGetPreference(request),
      ]);
      if (!summaryViewCurrent(epoch, request)) return null;
      const text = typeof pref?.text === "string" ? pref.text : "";
      setPrefText(text);
      setPrefSaved(text);
      const listed = (facts.facts || []).filter((f) => f.key !== REPLY_STYLE_KEY);
      setFactsState({
        loading: false, error: null,
        facts: listed, total: listed.length,
      });
      const items = summaries.items || [];
      if (listEpoch !== summaryListEpochRef.current) {
        setSummariesState((s) => ({ ...s, loading: false }));
        return { skippedSummaries: true };
      }
      setSummariesState({
        loading: false, error: null,
        items,
        total: summaries.total || 0,
      });
      return { items };
    } catch (err) {
      if (!summaryViewCurrent(epoch, request)) return null;
      const msg = err?.message || "載入失敗";
      setFactsState((s) => ({ ...s, loading: false, error: msg }));
      if (listEpoch !== summaryListEpochRef.current) {
        setSummariesState((s) => ({ ...s, loading: false }));
        return { skippedSummaries: true };
      }
      setSummariesState((s) => ({ ...s, loading: false, error: msg }));
      return null;
    }
  }, [authRequest, userId, summaryViewCurrent]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    summaryMountedRef.current = true;
    return () => {
      summaryMountedRef.current = false;
      summaryEpochRef.current += 1;
      summaryOpRef.current += 1;
    };
  }, []);

  const summaryResetEpoch = useRef(summaryEpochRef.current);
  useEffect(() => {
    if (summaryResetEpoch.current === summaryEpochRef.current) return;
    summaryResetEpoch.current = summaryEpochRef.current;
    if (!summaryBusyRef.current) setSummaryBusy(false);
    setSummarySelected(new Set());
    setSummaryStatus("");
  }, [authRequest, userId]);

  const onSavePref = async () => {
    const epoch = summaryEpochRef.current;
    const request = authRequest;
    setPrefBusy(true);
    try {
      const saved = await apiPutPreference(request, prefText);
      if (!summaryViewCurrent(epoch, request)) return;
      const text = typeof saved?.text === "string" ? saved.text : prefText.trim();
      setPrefText(text);
      setPrefSaved(text);
      toast("回覆偏好已儲存", { tone: "success" });
    } catch (err) {
      if (summaryViewCurrent(epoch, request)) toast(err?.message || "儲存失敗", { tone: "error" });
    } finally {
      if (summaryMountedRef.current) setPrefBusy(false);
    }
  };

  const onDeleteFact = async (id, key) => {
    const epoch = summaryEpochRef.current;
    const request = authRequest;
    if (!(await confirm({
      title: "刪除事實",
      message: `刪除事實「${key}」？此動作無法復原。`,
      confirmText: "刪除",
      tone: "danger",
    }))) return;
    if (!summaryViewCurrent(epoch, request)) return;
    try {
      await apiDeleteMemoryFact(request, id);
      if (!summaryViewCurrent(epoch, request)) return;
      await reload();
    } catch (err) {
      if (summaryViewCurrent(epoch, request)) toast(err?.message || "刪除失敗", { tone: "error" });
    }
  };

  const onClearFacts = async () => {
    if (factsState.total === 0) return;
    const epoch = summaryEpochRef.current;
    const request = authRequest;
    if (!(await confirm({
      title: "清空事實",
      message: `清空全部 ${factsState.total} 筆事實？此動作無法復原。回覆偏好不會被清掉。`,
      confirmText: "清空",
      tone: "danger",
    }))) return;
    if (!summaryViewCurrent(epoch, request)) return;
    try {
      await apiClearMemoryFacts(request);
      if (!summaryViewCurrent(epoch, request)) return;
      await reload();
    } catch (err) {
      if (summaryViewCurrent(epoch, request)) toast(err?.message || "清空失敗", { tone: "error" });
    }
  };

  const onSaveFact = async (id) => {
    const value = editingValue.trim();
    if (!value) return;
    const epoch = summaryEpochRef.current;
    const request = authRequest;
    try {
      await apiUpdateMemoryFact(request, id, value);
      if (!summaryViewCurrent(epoch, request)) return;
      setEditingId(null);
      await reload();
    } catch (err) {
      if (summaryViewCurrent(epoch, request)) toast(err?.message || "儲存失敗", { tone: "error" });
    }
  };

  const toggleSummary = (id) => {
    setSummarySelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectableSummaryIds = summariesState.items.map((item) => item.id);
  const summaryBulkReady = positiveUserId(userId) != null;
  const allSummariesSelected = selectableSummaryIds.length > 0
    && selectableSummaryIds.every((id) => summarySelected.has(id));

  function beginSummaryOp() {
    if (summaryBusyRef.current) return null;
    summaryBusyRef.current = true;
    const token = ++summaryOpRef.current;
    setSummaryBusy(true);
    return { token, epoch: summaryEpochRef.current };
  }

  function summaryOpCurrent(op) {
    return Boolean(op)
      && op.token === summaryOpRef.current
      && summaryViewCurrent(op.epoch, summaryAuthRef.current);
  }

  function endSummaryOp(op) {
    if (!op || summaryOpRef.current !== op.token) return;
    summaryBusyRef.current = false;
    if (summaryMountedRef.current) setSummaryBusy(false);
  }

  function acceptedClearCount(res) {
    const deleted = res && res.deleted;
    if (typeof deleted !== "number" || !Number.isInteger(deleted) || deleted < 0) return null;
    return deleted;
  }

  function summaryAccountChanged(error) {
    const message = typeof error?.message === "string" ? error.message : "";
    if (error?.status === 409 && message.includes("登入帳號已變更")) return message;
    return "";
  }

  const onBulkDeleteSummaries = async () => {
    const ids = selectableSummaryIds.filter((id) => summarySelected.has(id));
    if (ids.length === 0) return;
    const lockedUserId = positiveUserId(userId);
    if (lockedUserId == null) {
      setSummaryStatus("請重新登入或重新整理後再操作。");
      return;
    }
    const op = beginSummaryOp();
    if (!op) return;
    const request = bindExpectedUser(authRequest, lockedUserId);
    let accountChanged = "";
    try {
      const ok = await confirm({
        title: "刪除對話摘要",
        message: `確定刪除 ${ids.length} 則對話摘要？只會刪除對話摘要，不會刪除原始對話、已記住的事實、對話片段或回覆偏好。此動作無法復原。`,
        confirmText: "刪除",
        tone: "danger",
      });
      if (!ok || !summaryOpCurrent(op)) return;
      const { succeeded, failed } = await deleteSummaryBatches(
        ids,
        async (chunk) => {
          if (!summaryOpCurrent(op) || accountChanged) {
            const err = new Error(accountChanged || "登入帳號已變更，請重新整理後再操作");
            err.status = 409;
            throw err;
          }
          try {
            return await apiBulkDeleteSummaries(request, chunk);
          } catch (err) {
            const note = summaryAccountChanged(err);
            if (note) accountChanged = note;
            throw err;
          }
        },
      );
      if (!summaryOpCurrent(op)) return;
      const gone = new Set(succeeded);
      if (gone.size > 0) {
        summaryListEpochRef.current += 1;
        setSummariesState((current) => {
          const items = current.items.filter((item) => !gone.has(item.id));
          return { ...current, loading: false, items, total: items.length };
        });
        setSummarySelected((prev) => {
          const next = new Set();
          for (const id of prev) if (!gone.has(id)) next.add(id);
          return next;
        });
      }
      const note = accountChanged;
      if (failed.length === 0) {
        setSummaryStatus(`已刪除 ${succeeded.length} 則對話摘要。`);
      } else if (succeeded.length === 0) {
        setSummaryStatus(`刪除失敗，${failed.length} 則對話摘要仍保留。${note}`);
      } else {
        setSummaryStatus(`已刪除 ${succeeded.length} 則對話摘要，${failed.length} 則失敗。失敗的摘要仍保留。${note}`);
      }
    } finally {
      endSummaryOp(op);
    }
  };

  const onClearSummaries = async () => {
    const count = summariesState.items.length;
    if (count === 0) return;
    const lockedUserId = positiveUserId(userId);
    if (lockedUserId == null) {
      setSummaryStatus("請重新登入或重新整理後再操作。");
      return;
    }
    const op = beginSummaryOp();
    if (!op) return;
    const request = bindExpectedUser(authRequest, lockedUserId);
    try {
      const ok = await confirm({
        title: "刪除全部對話摘要",
        message: `確定刪除全部 ${count} 則對話摘要？只會刪除對話摘要，不會刪除原始對話、已記住的事實、對話片段或回覆偏好。此動作無法復原。`,
        confirmText: "全部刪除",
        tone: "danger",
      });
      if (!ok || !summaryOpCurrent(op)) return;
      const res = await apiClearSummaries(request);
      if (!summaryOpCurrent(op)) return;
      const deleted = acceptedClearCount(res);
      if (deleted === count) {
        summaryListEpochRef.current += 1;
        setSummariesState((current) => ({ ...current, loading: false, error: null, items: [], total: 0 }));
        setSummarySelected(new Set());
        setSummarySelectMode(false);
        setSummaryStatus("已刪除全部對話摘要。原始對話、事實、對話片段與回覆偏好都還在。");
        return;
      }
      summaryListEpochRef.current += 1;
      const listed = await reload();
      if (!summaryOpCurrent(op)) return;
      if (deleted !== null && listed && !listed.skippedSummaries && listed.items.length === 0) {
        setSummarySelected(new Set());
        setSummarySelectMode(false);
        setSummaryStatus("已刪除全部對話摘要。原始對話、事實、對話片段與回覆偏好都還在。");
        return;
      }
      setSummaryStatus("刪除失敗，對話摘要仍保留。");
      toast("刪除失敗，對話摘要仍保留。", { tone: "error" });
    } catch (err) {
      if (!summaryOpCurrent(op)) return;
      setSummaryStatus(`刪除失敗，對話摘要仍保留。${err?.message || ""}`.trim());
      toast(err?.message || "刪除失敗，對話摘要仍保留。", { tone: "error" });
    } finally {
      endSummaryOp(op);
    }
  };

  const onDeleteSummary = async (id) => {
    const op = beginSummaryOp();
    if (!op) return;
    const request = authRequest;
    try {
      const ok = await confirm({
        title: "刪除摘要",
        message: "刪除這則對話摘要？之後就不會再被搜尋到。此動作無法復原。",
        confirmText: "刪除",
        tone: "danger",
      });
      if (!ok || !summaryOpCurrent(op)) return;
      await apiDeleteMemorySummary(request, id);
      if (!summaryOpCurrent(op)) return;
      summaryListEpochRef.current += 1;
      await reload();
    } catch (err) {
      if (summaryOpCurrent(op)) toast(err?.message || "刪除失敗", { tone: "error" });
    } finally {
      endSummaryOp(op);
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
        <div style={{ fontWeight: 500, marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <span>
            對話摘要 <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}>· {summariesState.total}</span>
          </span>
          <span style={{ flex: 1 }} />
          {summariesState.items.length > 0 && (
            <button
              type="button"
              disabled={summaryBusy || !summaryBulkReady}
              onClick={onClearSummaries}
              style={summaryBarButton}
            >
              全部刪除
            </button>
          )}
          {!summariesState.loading && !summarySelectMode ? (
            <button
              type="button"
              disabled={summaryBusy || summariesState.items.length === 0 || !summaryBulkReady}
              onClick={() => { setSummarySelectMode(true); setSummaryStatus(""); }}
              style={summaryBarButton}
            >
              多選
            </button>
          ) : null}
        </div>
        {summarySelectMode && (
          <div
            data-testid="memory-summary-bulk-bar"
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: 6,
              marginBottom: 8,
              minWidth: 0,
            }}
          >
            <button
              type="button"
              disabled={summaryBusy}
              onClick={() => { setSummarySelectMode(false); setSummarySelected(new Set()); }}
              style={summaryBarButton}
            >
              取消
            </button>
            <button
              type="button"
              disabled={summaryBusy || selectableSummaryIds.length === 0}
              onClick={() => {
                setSummarySelected(allSummariesSelected ? new Set() : new Set(selectableSummaryIds));
              }}
              style={summaryBarButton}
            >
              {allSummariesSelected ? "取消全選" : "全選"}
            </button>
            <span data-testid="memory-summary-count" style={{ fontSize: 11, color: "var(--fg-muted)" }}>
              已選 {selectableSummaryIds.filter((id) => summarySelected.has(id)).length}
            </span>
            <button
              type="button"
              disabled={summaryBusy || !summaryBulkReady || !selectableSummaryIds.some((id) => summarySelected.has(id))}
              onClick={onBulkDeleteSummaries}
              style={summaryBarButton}
            >
              刪除所選
            </button>
          </div>
        )}
        {!summaryBulkReady && (
          <div
            data-testid="memory-summary-relogin"
            role="status"
            style={{ fontSize: 11, color: "var(--danger)", marginBottom: 8, lineHeight: 1.5 }}
          >
            請重新登入或重新整理後再刪除摘要。
          </div>
        )}
        {summaryStatus && (
          <div
            data-testid="memory-summary-bulk-status"
            role="status"
            style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 8, lineHeight: 1.5 }}
          >
            {summaryStatus}
          </div>
        )}
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
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
                padding: "6px 8px",
                background: "var(--bg-subtle)",
                borderRadius: "var(--radius)",
                fontSize: 12,
                minWidth: 0,
              }}>
                {summarySelectMode && (
                  <label className="anila-bulk-check-hit" style={{ position: "relative", display: "inline-flex", flex: "0 0 auto" }}>
                    <input
                      className="anila-bulk-check"
                      type="checkbox"
                      checked={summarySelected.has(item.id)}
                      disabled={summaryBusy}
                      onChange={() => toggleSummary(item.id)}
                    />
                    <span style={summarySrOnly}>選取摘要 {item.summary}</span>
                  </label>
                )}
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
                    flex: "1 1 160px",
                    minWidth: 0,
                  }}
                >
                  {item.summary}
                </button>
                <button
                  type="button"
                  data-testid={`memory-summary-delete-${item.id}`}
                  disabled={summaryBusy}
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
