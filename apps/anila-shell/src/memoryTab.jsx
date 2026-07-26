// 設定 → 記憶 tab。原本內嵌在 `app.jsx`,W1-3 抽成獨立檔:① 它自己管
// 資料生命週期(facts + chunks);② 依部署能力旗標分流的邏輯需要能被單獨
// 測到。行為除下述 capability 分流外逐字保留。
//
// MVP scope (P2):
//   - List user_facts; per-row delete; clear-all-facts
//   - List recent chunks (preview only); clear-all-chunks
//   - Surface 密等鎖定來源 markers (P3 will inherit them)
// Out of scope until we see real demand:
//   - Inline edit of fact value (delete-and-let-LLM-re-extract is fine)
//   - Per-chunk delete (cascade via conversation delete is fine)
//   - Search / filter (volume is small)
//
// ── W1-3 ②:capability 分流 ────────────────────────────────────────────────
// `ENABLE_MEMORY` 預設 False(`config.py:38`),卡登部署明確 false。功能關閉
// 時後端 `/api/memory/*` 回空 200,而舊文案寫「和 ANILA 多聊聊…平台會自動
// 學習」——**承諾一個永遠不會來的東西**。關閉時改說實話,並且**保留**既有
// 殘留資料的檢視與刪除路徑(旗標不得吃掉使用者的刪除權)。

import React, { useCallback, useEffect, useState } from "react";

import { useConfirm, useToast } from "./confirm.jsx";
import { IconTrash } from "./icons.jsx";
import { DEFAULT_CAPABILITIES } from "./runtime/capabilities.js";
import styles from "./memoryTab.module.css";
import {
  clearChunks as apiClearMemoryChunks,
  clearFacts as apiClearMemoryFacts,
  deleteFact as apiDeleteMemoryFact,
  listChunks as apiListMemoryChunks,
  listFacts as apiListMemoryFacts,
} from "./runtime/memory.js";

export function MemoryTab({ authRequest, capabilities }) {
  const confirm = useConfirm();
  const toast = useToast();
  // capabilities 缺漏時 fail-closed:不知道有沒有開,就不要承諾。
  const enabled = Boolean((capabilities || DEFAULT_CAPABILITIES).enableMemory);
  const [factsState, setFactsState] = useState({ loading: true, error: null, facts: [], total: 0 });
  const [chunksState, setChunksState] = useState({
    loading: true, error: null, items: [],
    total: 0, encrypted_total: 0, distinct_conversations: 0,
  });

  const reload = useCallback(async () => {
    setFactsState((s) => ({ ...s, loading: true, error: null }));
    setChunksState((s) => ({ ...s, loading: true, error: null }));
    try {
      const [facts, chunks] = await Promise.all([
        apiListMemoryFacts(authRequest),
        apiListMemoryChunks(authRequest, { limit: 25 }),
      ]);
      setFactsState({
        loading: false, error: null,
        facts: facts.facts || [], total: facts.total || 0,
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
      message: `清空全部 ${factsState.total} 筆事實？此動作無法復原。`,
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

  return (
    <div style={{ display: "grid", gap: 18, fontSize: 13 }}>
      {enabled ? (
        <div style={{ fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.6 }}>
          平台會在每輪對話後，把可能對你長期有用的事實萃取為 key/value 存起來，
          並把訊息向量化以便跨對話語意檢索。下次任何對話都會自動帶入相關記憶。
          所有資料只屬於你個人，不與其他使用者共享。
        </div>
      ) : (
        <div role="status" className={styles.notice}>
          <div className={styles.noticeTitle}>本部署未啟用記憶功能</div>
          <div className={styles.noticeBody}>
            這個部署把長期記憶關閉了（運維端的 <code>ENABLE_MEMORY</code> 旗標）。
            平台不會從你的對話萃取任何事實，也不會建立跨對話的語意索引；
            每個對話都是獨立的。下方列出的是既有殘留資料，你隨時可以刪除。
          </div>
        </div>
      )}

      {/* ── Facts ──────────────────────────────────────────────────────── */}
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
            {enabled
              ? "目前還沒有萃取到任何事實。和 ANILA 多聊聊「我是誰、我喜歡什麼」之類的訊息，平台會自動學習。"
              : "沒有任何事實紀錄（記憶功能未啟用，也不會產生新的紀錄）。"}
          </div>
        )}
        {!factsState.loading && factsState.facts.length > 0 && (
          <div style={{ display: "grid", gap: 6 }}>
            {factsState.facts.map((f) => (
              <div key={f.id} style={{
                display: "grid",
                gridTemplateColumns: "minmax(110px, 1fr) 2fr auto auto",
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
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--fg-subtle)" }}>
                  {(f.confidence * 100).toFixed(0)}%
                </div>
                <button
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

      {/* ── Chunks ─────────────────────────────────────────────────────── */}
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
                  · {chunksState.encrypted_total} 段來自密等鎖定對話
                </span>
              )}
            </span>
          </div>
          <button
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
            {enabled
              ? "目前還沒有對話片段索引。對話幾輪之後再回來看。"
              : "沒有任何對話片段索引（記憶功能未啟用，也不會產生新的索引）。"}
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
                <span style={{
                  display: "inline-block", minWidth: 70,
                  color: "var(--fg-subtle)",
                }}>
                  {c.role === "user" ? "user" : "asst"} · #{c.conversation_id}
                </span>
                {c.is_encrypted && <span title="來自密等鎖定的對話" style={{ marginRight: 4 }}>🔒</span>}
                <span style={{ color: "var(--fg)" }}>{c.content}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ fontSize: 10, color: "var(--fg-subtle)", lineHeight: 1.6 }}>
        {enabled ? (
          <>
            清空後立即生效；下次對話起，平台會重新從新對話內容重新學習。
            若需暫時停用記憶整合，請聯絡管理員（runtime feature flag 由運維端控制）。
          </>
        ) : (
          <>
            清空後立即生效。若需要啟用記憶功能，請聯絡管理員
            （runtime feature flag 由運維端控制，不是使用者可切換的偏好）。
          </>
        )}
      </div>
    </div>
  );
}

export default MemoryTab;
