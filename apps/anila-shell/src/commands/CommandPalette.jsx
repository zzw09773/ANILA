// 命令面板(⌘K / Ctrl+K)—— 單一輸入框同時做:
//   ① 搜尋對話(本地清單即時比對 + debounce 打伺服器全文搜尋)
//   ② 跳轉動作(新對話 / 切換 agent / 開設定 / 開記憶分頁 / 快捷鍵清單)
//   ③ Open WebUI 風格前綴過濾:folder: / starred: / archived: / tag:
//
// inline style(本專案無 CSS 檔)、零外部資源(air-gapped)。
import React, { useEffect, useMemo, useRef, useState } from "react";

import { IconSearch, IconArrowRight, IconArchive, IconStar, IconLock } from "../icons.jsx";
import { relativeLabel } from "../runtime/time.js";
import { ConfidentialWatermark } from "../trust.jsx";
import {
  filterConversationsForPalette,
  filterPaletteActions,
  parsePaletteQuery,
} from "./paletteQuery.js";

const PREFIX_HINTS = [
  { token: "folder:", label: "限定資料夾" },
  { token: "tag:", label: "限定標籤" },
  { token: "starred:", label: "只看已加星" },
  { token: "archived:", label: "只看已封存" },
];

export const CommandPalette = ({
  open,
  onClose,
  conversations = [],
  folders = [],
  actions = [],
  onSelectConv,
  onServerSearch,
  // 鑑識浮水印歸屬資訊 —— 面板列出 classified 標題時要在面板內重繪。
  watermarkUser,
  watermarkTraceId,
}) => {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [serverHits, setServerHits] = useState([]);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  // 每次開啟都從乾淨狀態開始(對齊 claude.ai / ChatGPT)。
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    setServerHits([]);
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  const parsed = useMemo(() => parsePaletteQuery(query), [query]);

  // 伺服器端全文搜尋(比對訊息內文)。tag:/folder: 這類純結構過濾不打後端。
  useEffect(() => {
    if (!open || typeof onServerSearch !== "function") {
      setServerHits([]);
      return undefined;
    }
    const text = parsed.text;
    if (!text || text.length < 2) {
      setServerHits([]);
      return undefined;
    }
    let alive = true;
    const t = setTimeout(() => {
      onServerSearch(text)
        .then((rows) => { if (alive) setServerHits(Array.isArray(rows) ? rows : []); })
        .catch(() => { if (alive) setServerHits([]); });
    }, 260);
    return () => { alive = false; clearTimeout(t); };
  }, [open, onServerSearch, parsed.text]);

  const visibleActions = useMemo(() => {
    // 帶結構過濾時使用者明顯在找對話,不要用動作列干擾。
    if (parsed.hasFilters) return [];
    return filterPaletteActions(actions, parsed.text).slice(0, 6);
  }, [actions, parsed]);

  const visibleConversations = useMemo(() => {
    const local = filterConversationsForPalette(conversations, parsed, folders);
    const knownIds = new Set(local.map((c) => c.id));
    const extras = serverHits
      .filter((h) => !knownIds.has(h.id))
      .filter((h) => !conversations.some((c) => c.id === h.id));
    const merged = [...local, ...filterConversationsForPalette(extras, parsed, folders)];
    return merged
      .sort((a, b) => {
        const ta = new Date(a.updatedAt || a.createdAt || 0).getTime();
        const tb = new Date(b.updatedAt || b.createdAt || 0).getTime();
        return tb - ta;
      })
      .slice(0, 24);
  }, [conversations, folders, parsed, serverHits]);

  const rows = useMemo(
    () => [
      ...visibleActions.map((a) => ({ kind: "action", key: `a:${a.id}`, action: a })),
      ...visibleConversations.map((c) => ({ kind: "conv", key: `c:${c.id}`, conversation: c })),
    ],
    [visibleActions, visibleConversations],
  );

  // 面板只要列出任何一則 classified 對話的標題,這一層就有涉密內容 →
  // 面板內部必須自己重繪鑑識浮水印(面板 z-index 高於一般畫面,單靠全域
  // 那層會被自己的 backdrop 蓋住歸屬資訊)。
  const showsClassified = useMemo(
    () => visibleConversations.some((c) => c.classified),
    [visibleConversations],
  );
  const classifiedLevel = useMemo(
    () =>
      visibleConversations.find((c) => c.classified)?.classificationLevel || undefined,
    [visibleConversations],
  );

  useEffect(() => { setActive(0); }, [rows.length, query]);

  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector(`[data-row="${active}"]`);
    el?.scrollIntoView?.({ block: "nearest" });
  }, [active, open, rows.length]);

  if (!open) return null;

  const runRow = (row) => {
    if (!row) return;
    if (row.kind === "action") {
      onClose?.();
      row.action.run?.();
      return;
    }
    onClose?.();
    onSelectConv?.(row.conversation.id);
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (rows.length) setActive((i) => (i + 1) % rows.length);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (rows.length) setActive((i) => (i - 1 + rows.length) % rows.length);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      runRow(rows[active]);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      onClose?.();
    }
  };

  return (
    <div
      onClick={onClose}
      style={{
        // Modal 是 100(tokens 的 --anila-z-modal);面板要壓在 Modal 之上,
        // 但**不可**蓋掉 z-200 的密等/繼承警示橫幅,也**不可**蓋掉 z-150 的
        // 全域鑑識浮水印(涉密內容不得出現在無浮水印的圖層)。
        position: "fixed", inset: 0, zIndex: 120,
        background: "oklch(0.10 0 0 / 0.4)",
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        padding: "12vh 20px 20px",
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%", maxWidth: 620,
          background: "var(--bg-elev)",
          border: "1px solid var(--border-strong)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "0 24px 64px -16px oklch(0.10 0 0 / 0.35)",
          overflow: "hidden",
          display: "flex", flexDirection: "column",
          maxHeight: "70vh",
          position: "relative",
        }}
      >
        {showsClassified && (
          <ConfidentialWatermark
            absolute
            zIndex={1}
            level={classifiedLevel}
            userEmail={watermarkUser}
            traceId={watermarkTraceId}
          />
        )}
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "12px 14px",
          borderBottom: "1px solid var(--border)",
        }}>
          <IconSearch size={15} style={{ color: "var(--fg-subtle)", flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label="搜尋對話或跳轉動作"
            placeholder="搜尋對話、或輸入動作… (folder: / tag: / starred: / archived:)"
            style={{
              flex: 1, minWidth: 0,
              background: "transparent", border: "none", outline: "none",
              fontSize: 14, color: "var(--fg)", fontFamily: "inherit",
            }}
          />
          <span style={{
            fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)",
            flexShrink: 0,
          }}>Esc 關閉</span>
        </div>

        <div ref={listRef} style={{ flex: 1, overflowY: "auto", padding: 6, minHeight: 0 }}>
          {rows.length === 0 && (
            <div style={{
              padding: "26px 14px", textAlign: "center",
              color: "var(--fg-subtle)", fontSize: 12, lineHeight: 1.8,
            }}>
              <div>沒有符合的結果</div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                {PREFIX_HINTS.map((h) => `${h.token} ${h.label}`).join(" · ")}
              </div>
            </div>
          )}

          {visibleActions.length > 0 && (
            <div style={{
              padding: "6px 8px 4px", fontSize: 10, color: "var(--fg-subtle)",
              fontFamily: "var(--font-mono)", letterSpacing: 0.4,
            }}>動作</div>
          )}
          {rows.map((row, i) => {
            const isActive = i === active;
            if (row.kind === "action") {
              const a = row.action;
              return (
                <button
                  key={row.key}
                  type="button"
                  data-row={i}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => { e.preventDefault(); runRow(row); }}
                  style={{
                    display: "flex", alignItems: "center", gap: 10, width: "100%",
                    padding: "8px 10px",
                    background: isActive ? "var(--bg-subtle)" : "transparent",
                    border: "none", borderRadius: "var(--radius)",
                    color: "var(--fg)", textAlign: "left", cursor: "pointer",
                    fontFamily: "inherit",
                  }}
                >
                  <span style={{ display: "inline-flex", color: "var(--fg-muted)", flexShrink: 0 }}>
                    {a.icon || <IconArrowRight size={13} />}
                  </span>
                  <span style={{ flex: 1, minWidth: 0, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {a.label}
                  </span>
                  {a.hint && (
                    <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
                      {a.hint}
                    </span>
                  )}
                </button>
              );
            }

            const c = row.conversation;
            const firstConv = rows.findIndex((r) => r.kind === "conv") === i;
            return (
              <React.Fragment key={row.key}>
                {firstConv && (
                  <div style={{
                    padding: "8px 8px 4px", fontSize: 10, color: "var(--fg-subtle)",
                    fontFamily: "var(--font-mono)", letterSpacing: 0.4,
                  }}>對話</div>
                )}
                <button
                  type="button"
                  data-row={i}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => { e.preventDefault(); runRow(row); }}
                  style={{
                    display: "block", width: "100%",
                    padding: "8px 10px",
                    background: isActive ? "var(--bg-subtle)" : "transparent",
                    border: "none", borderRadius: "var(--radius)",
                    color: "var(--fg)", textAlign: "left", cursor: "pointer",
                    fontFamily: "inherit",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    {c.classified && <IconLock size={11} style={{ color: "var(--danger)", flexShrink: 0 }} />}
                    {c.starred && <IconStar size={11} style={{ color: "var(--warn)", flexShrink: 0 }} />}
                    {c.archived && <IconArchive size={11} style={{ color: "var(--fg-subtle)", flexShrink: 0 }} />}
                    <span style={{
                      flex: 1, minWidth: 0, fontSize: 13, fontWeight: 500,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>{c.title}</span>
                    <span style={{ fontSize: 10, color: "var(--fg-subtle)", flexShrink: 0 }}>
                      {relativeLabel(c.updatedAt || c.createdAt)}
                    </span>
                  </div>
                  {(c.snippet || (c.tags || []).length > 0 || c.archived) && (
                    <div style={{
                      marginTop: 2, fontSize: 11, color: "var(--fg-subtle)",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {c.archived && <span style={{ marginRight: 6 }}>已封存</span>}
                      {(c.tags || []).slice(0, 3).map((t) => (
                        <span key={t} style={{ marginRight: 6, fontFamily: "var(--font-mono)" }}>#{t}</span>
                      ))}
                      {c.snippet}
                    </div>
                  )}
                </button>
              </React.Fragment>
            );
          })}
        </div>

        <div style={{
          padding: "6px 12px", borderTop: "1px solid var(--border)",
          fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)",
          display: "flex", gap: 12, flexWrap: "wrap",
        }}>
          <span>↑↓ 移動</span>
          <span>Enter 開啟</span>
          <span>Esc 關閉</span>
        </div>
      </div>
    </div>
  );
};
