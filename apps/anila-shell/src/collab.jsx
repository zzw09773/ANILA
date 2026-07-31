// Collaboration: named share dialog, handoff-to menu, tag/folder editor (ESM)
import React, { useState, useEffect } from "react";
import { IconShield, IconX, IconCheck, IconStar, IconFolder } from "./icons.jsx";
import { Button, Modal, MenuItem, Divider, Input } from "./components.jsx";
import { useAuth } from "./runtime/auth.jsx";
import {
  formatShareTarget,
  createHandoff,
  listHandoffs,
  acceptHandoff,
  rejectHandoff,
  incomingPendingHandoffs,
} from "./runtime/conversations.js";
import { searchDirectory, formatColleague } from "./runtime/directory.js";

// TTL choice → ISO timestamp the backend understands.
function ttlToExpiresAt(ttlKey) {
  if (ttlKey === "never") return null;
  const map = { "1h": 3600, "24h": 86400, "7d": 604800 };
  const seconds = map[ttlKey];
  if (!seconds) return null;
  return new Date(Date.now() + seconds * 1000).toISOString();
}

// ---- Share Dialog (P4.3) ----
// Named person XOR unit. Anonymous link retired (SYSTEM-MAP §分享).
// `onCreateShare({ targetUsername | targetDepartmentName, mode, allowFork, expiresAt })`
export const ShareDialog = ({ open, onClose, conversation, user, onCreateShare, onListShares, onRevokeShare }) => {
  const { authRequest } = useAuth();
  // 「唯讀分享」= 對方看得到；「交給同事接手」= 對方接受後成為擁有者，
  // 可以繼續聊，原擁有者保留讀取(見 services/handoff_transfer.py)。
  const [tab, setTab] = useState("share");
  const [ttl, setTtl] = useState("24h");
  const [kind, setKind] = useState("person"); // person | unit
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [existingShares, setExistingShares] = useState([]);
  const [listTick, setListTick] = useState(0);

  useEffect(() => {
    if (!open || typeof onListShares !== "function") { setExistingShares([]); return; }
    let alive = true;
    onListShares()
      .then((rows) => { if (alive) setExistingShares(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (alive) setExistingShares([]); });
    return () => { alive = false; };
  }, [open, onListShares, listTick]);

  useEffect(() => {
    if (!open) {
      setError("");
      setNotice("");
      setTarget("");
      setBusy(false);
      setTab("share");
    }
  }, [open]);

  const revoke = async (shareId) => {
    if (typeof onRevokeShare !== "function") return;
    try {
      await onRevokeShare(shareId);
      setExistingShares((prev) => prev.filter((s) => s.id !== shareId));
      setNotice("已撤銷分享。對方之後無法再開啟此對話；已開啟的內容不會被收回。");
    } catch (err) {
      setError(err?.message || "撤銷失敗");
    }
  };

  if (!open) return null;

  const share = async () => {
    if (!onCreateShare) {
      setError("尚未提供分享 handler，無法建立分享");
      return;
    }
    const trimmed = target.trim();
    if (!trimmed) {
      setError(
        kind === "person"
          ? "請輸入對方帳號（例如 bob.lin）。"
          : "請輸入單位名稱（與平台部門名稱一致）。",
      );
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        mode: "read_only",
        expiresAt: ttlToExpiresAt(ttl),
      };
      if (kind === "person") payload.targetUsername = trimmed;
      else payload.targetDepartmentName = trimmed;
      await onCreateShare(payload);
      setTarget("");
      setNotice(
        kind === "person"
          ? `已分享給帳號「${trimmed}」。對方登入後即可在對話列表看到。`
          : `已分享給單位「${trimmed}」及其下屬單位。該範圍內的同仁登入後即可看到。`,
      );
      setListTick((n) => n + 1);
    } catch (err) {
      setError(err?.message || "建立分享失敗");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="分享對話" subtitle={conversation?.title || "對話"} width={520}>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={{ display: "flex", gap: 4 }}>
          {[
            { k: "share", label: "唯讀分享" },
            { k: "handoff", label: "交給同事接手" },
          ].map(o => (
            <button key={o.k} type="button" onClick={() => { setTab(o.k); setError(""); setNotice(""); }} style={{
              flex: 1, padding: "7px 8px", fontSize: 12,
              background: tab === o.k ? "var(--accent-soft)" : "var(--bg-elev)",
              border: "1px solid " + (tab === o.k ? "var(--accent)" : "var(--border)"),
              borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
              fontWeight: tab === o.k ? 600 : 400,
            }}>{o.label}</button>
          ))}
        </div>

        {tab === "handoff" ? (
          <HandoffToColleague
            conversationId={conversation?.id}
            authRequest={authRequest}
            onClose={onClose}
          />
        ) : (<>
        <div style={{
          padding: 10, background: "var(--bg-subtle)",
          border: "1px solid var(--border)", borderRadius: "var(--radius)",
          fontSize: 12, color: "var(--fg-muted)", lineHeight: 1.6,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--fg)", marginBottom: 3 }}>
            <IconShield size={13} /> <b>指定對象分享</b>
          </div>
          分享給具名帳號或單位（含下屬單位）。對方需登入後讀取；匿名連結已停用。
          撤銷後對方無法再開啟，但已開啟的內容不會被收回。營業秘密以上會落稽核；密／機密不可分享。
        </div>

        <div>
          <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>分享給</div>
          <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
            {[
              { k: "person", label: "指定人" },
              { k: "unit", label: "指定單位" },
            ].map(o => (
              <button key={o.k} type="button" onClick={() => { setKind(o.k); setError(""); }} style={{
                flex: 1, padding: "6px 8px", fontSize: 12,
                background: kind === o.k ? "var(--accent-soft)" : "var(--bg-elev)",
                border: "1px solid " + (kind === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
          <Input
            placeholder={kind === "person" ? "對方帳號（例如 bob.lin）" : "單位名稱（例如 資訊所）"}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !busy) share(); }}
          />
        </div>

        <div>
          <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>過期時間</div>
          <div style={{ display: "flex", gap: 4 }}>
            {[
              { k: "1h", label: "1 小時" },
              { k: "24h", label: "24 小時" },
              { k: "7d", label: "7 天" },
              { k: "never", label: "不過期" },
            ].map(o => (
              <button key={o.k} type="button" onClick={() => setTtl(o.k)} style={{
                flex: 1, padding: "6px 8px", fontSize: 12,
                background: ttl === o.k ? "var(--accent-soft)" : "var(--bg-elev)",
                border: "1px solid " + (ttl === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
        </div>

        {error && (
          <div style={{
            padding: "6px 10px",
            background: "oklch(0.97 0.03 25)",
            border: "1px solid oklch(0.88 0.08 25)",
            borderRadius: "var(--radius)",
            color: "var(--danger)",
            fontSize: 12,
          }}>{error}</div>
        )}
        {notice && !error && (
          <div style={{
            padding: "6px 10px",
            background: "var(--accent-soft)",
            border: "1px solid var(--accent)",
            borderRadius: "var(--radius)",
            color: "var(--fg)",
            fontSize: 12,
          }}>{notice}</div>
        )}

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button onClick={onClose} disabled={busy}>關閉</Button>
          <Button variant="primary" onClick={share} disabled={busy}>
            {busy ? "分享中…" : "分享"}
          </Button>
        </div>

        {existingShares.length > 0 && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>已建立的分享</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {existingShares.map((s) => (
                <div key={s.id} style={{
                  display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 8px", background: "var(--bg-subtle)",
                  border: "1px solid var(--border)", borderRadius: "var(--radius)",
                  fontSize: 12,
                }}>
                  <span style={{ flex: 1, color: "var(--fg-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {formatShareTarget(s)} · 唯讀
                  </span>
                  <Button size="sm" onClick={() => revoke(s.id)}>撤銷</Button>
                </div>
              ))}
            </div>
          </div>
        )}
        </>)}
      </div>
    </Modal>
  );
};

// ---- 交給同事(OWNER Q9)------------------------------------------------------
// 2026-07-30 這顆按鈕被拿掉的原因是它送出的酬載沒有 `to_user_id`——輸入框只是
// 自由文字，建立的是一筆交給「沒有人」的交接。這一版的不變式：
// **沒有從清單裡挑到一個真的帳號，送出鍵就按不下去**(`disabled={... || !selected}`，
// submit 裡再擋一次以防鍵盤繞過)。挑定之後輸入框直接換成那個人的名牌，要換人
// 得先按「換一位」——不會出現「顯示 A、其實送 B」。
export const HandoffToColleague = ({ conversationId, authRequest, onClose }) => {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [selected, setSelected] = useState(null);
  const [note, setNote] = useState("");
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const q = query.trim();
    if (selected || !q) { setResults([]); setSearching(false); return; }
    let alive = true;
    setSearching(true);
    // 打字時每個字都打一次 API 太吵；等手停下來再查。
    const timer = setTimeout(() => {
      searchDirectory(authRequest, q)
        .then((rows) => { if (alive) setResults(Array.isArray(rows) ? rows : []); })
        .catch((err) => {
          if (!alive) return;
          setResults([]);
          setError(err?.message || "查詢同仁清單失敗");
        })
        .finally(() => { if (alive) setSearching(false); });
    }, 250);
    return () => { alive = false; clearTimeout(timer); };
  }, [query, selected, authRequest]);

  const submit = async () => {
    // 送出鍵本來就 disabled；這裡再擋一次，避免鍵盤 Enter 繞過去。
    if (!selected) return;
    if (typeof conversationId !== "number") {
      setError("尚未建立後端對話 — 請先送出第一則訊息。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await createHandoff(authRequest, {
        conversationId,
        toUserId: selected.id,
        note: note.trim() || null,
      });
      setNotice(
        `已送出交接請求給「${formatColleague(selected)}」。` +
        "對方在畫面上方會看到請求；接受之後由他接手繼續，你仍然看得到這串對話。",
      );
      setSelected(null);
      setQuery("");
      setNote("");
    } catch (err) {
      setError(err?.message || "送出交接請求失敗");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{
        padding: 10, background: "var(--bg-subtle)",
        border: "1px solid var(--border)", borderRadius: "var(--radius)",
        fontSize: 12, color: "var(--fg-muted)", lineHeight: 1.6,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--fg)", marginBottom: 3 }}>
          <IconShield size={13} /> <b>交給同事接手</b>
        </div>
        對方接受後成為這串對話的擁有者，可以繼續往下聊；你不會被踢掉，仍然讀得到全部內容。
        對方拒絕的話什麼都不會變。密／機密的對話不可交接。
      </div>

      <div>
        <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>交給誰</div>
        {selected ? (
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "8px 10px", background: "var(--accent-soft)",
            border: "1px solid var(--accent)", borderRadius: "var(--radius)",
            fontSize: 12,
          }}>
            <span style={{ flex: 1 }}>{formatColleague(selected)}</span>
            <Button size="sm" onClick={() => { setSelected(null); setQuery(""); }}>換一位</Button>
          </div>
        ) : (
          <>
            <Input
              placeholder="輸入同事帳號關鍵字（例如 bob）"
              value={query}
              onChange={(e) => { setQuery(e.target.value); setError(""); }}
            />
            <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
              {searching && (
                <div style={{ fontSize: 12, color: "var(--fg-subtle)" }}>查詢中…</div>
              )}
              {!searching && query.trim() && results.length === 0 && (
                <div style={{ fontSize: 12, color: "var(--fg-subtle)" }}>
                  查無符合的同仁。請確認帳號拼寫。
                </div>
              )}
              {results.map((r) => (
                <button key={r.id} type="button"
                  onClick={() => { setSelected(r); setResults([]); }}
                  style={{
                    textAlign: "left", padding: "6px 10px", fontSize: 12,
                    background: "var(--bg-elev)", border: "1px solid var(--border)",
                    borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
                  }}>{formatColleague(r)}</button>
              ))}
            </div>
          </>
        )}
      </div>

      <div>
        <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>附註（選填）</div>
        <Input
          placeholder="想讓對方知道的事，例如：後續請接洽採購"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>

      {error && (
        <div style={{
          padding: "6px 10px", background: "oklch(0.97 0.03 25)",
          border: "1px solid oklch(0.88 0.08 25)", borderRadius: "var(--radius)",
          color: "var(--danger)", fontSize: 12,
        }}>{error}</div>
      )}
      {notice && !error && (
        <div style={{
          padding: "6px 10px", background: "var(--accent-soft)",
          border: "1px solid var(--accent)", borderRadius: "var(--radius)",
          color: "var(--fg)", fontSize: 12,
        }}>{notice}</div>
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button onClick={onClose} disabled={busy}>關閉</Button>
        <Button variant="primary" onClick={submit} disabled={busy || !selected}>
          {busy ? "送出中…" : "送出交接請求"}
        </Button>
      </div>
    </div>
  );
};

// ---- 交接收件匣 ------------------------------------------------------------
// 收件人這一端。沒有這塊，送出的請求就沒有人按得到「接受」——那正是舊版
// 那筆「交給沒有人」的交接的另一半。掛在畫面最上方(banners.jsx)。
//
// 接受成功之後不自動抽換左側列表(那份狀態在 app.jsx 手上)，改成明確給一顆
// 重新整理鍵：寧可多一次點擊，也不要讓使用者以為沒事發生。
export const HandoffInbox = ({ authRequest, currentUserId, onReload }) => {
  const [rows, setRows] = useState([]);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (typeof authRequest !== "function" || typeof currentUserId !== "number") {
      setRows([]);
      return;
    }
    let alive = true;
    listHandoffs(authRequest)
      .then((list) => { if (alive) setRows(incomingPendingHandoffs(list, currentUserId)); })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, [authRequest, currentUserId, tick]);

  // 每分鐘再撈一次。送出的人被告知「對方在畫面上方會看到請求」——如果要等
  // 對方下次重新整理才成真，那句話就是假的。
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), 60000);
    return () => clearInterval(timer);
  }, []);

  const resolve = async (row, accept) => {
    setBusyId(row.id);
    setError("");
    try {
      if (accept) await acceptHandoff(authRequest, row.id);
      else await rejectHandoff(authRequest, row.id);
      if (accept) setAccepted(row);
      setTick((n) => n + 1);
    } catch (err) {
      setError(err?.message || (accept ? "接受交接失敗" : "拒絕交接失敗"));
    } finally {
      setBusyId(null);
    }
  };

  if (rows.length === 0 && !accepted && !error) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {rows.map((row) => (
        <div key={row.id} style={{
          display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          padding: "8px 16px", background: "var(--accent-soft)",
          borderBottom: "1px solid var(--accent)", fontSize: 13, lineHeight: 1.5,
        }}>
          <span style={{ flex: 1, minWidth: 200 }}>
            <b>{row.from_username || "同事"}</b> 想把對話「{row.conversation_title || `#${row.conversation_id}`}」交給你接手。
            {row.note ? `附註：${row.note}` : ""}
          </span>
          <Button size="sm" variant="primary" disabled={busyId === row.id}
            onClick={() => resolve(row, true)}>接受</Button>
          <Button size="sm" disabled={busyId === row.id}
            onClick={() => resolve(row, false)}>拒絕</Button>
        </div>
      ))}
      {accepted && (
        <div style={{
          display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          padding: "8px 16px", background: "var(--bg-subtle)",
          borderBottom: "1px solid var(--border)", fontSize: 13,
        }}>
          <span style={{ flex: 1, minWidth: 200 }}>
            已接手「{accepted.conversation_title || `#${accepted.conversation_id}`}」。
            重新整理後就會出現在左側對話列表。
          </span>
          <Button size="sm" onClick={() => (onReload ? onReload() : window.location.reload())}>
            重新整理
          </Button>
        </div>
      )}
      {error && (
        <div style={{
          padding: "8px 16px", background: "oklch(0.95 0.05 25)",
          borderBottom: "1px solid var(--danger)",
          color: "var(--danger)", fontSize: 13,
        }}>{error}</div>
      )}
    </div>
  );
};

// ---- Handoff to Agent menu ----
// 2026-07-30 拿掉過「給同事」:它以前長得能用——輸入帳號、按送出、沒有錯誤——
// 但送出的酬載根本沒有 to_user_id(只把名字塞進 note 的自由文字),所以建立的是
// 一筆交給「沒有人」的交接,那位同事永遠收不到。
// 2026-07-31 擁有者答了 OWNER Q9(全院可查、只回姓名與單位),已經接回來,
// 但**位置換了**:它需要當下這串對話的 id,而這個選單只拿得到助手清單,
// 所以「交給同事」做在 <ShareDialog> 的第二個分頁(見本檔 HandoffToColleague),
// 收件人那一端在 HandoffInbox。這個選單維持只管「交給其他助手」。
export const HandoffMenu = ({ agents, currentAgentId, onHandoffAgent, close }) => (
    <div style={{ minWidth: 260 }}>
      <div style={{ padding: "6px 10px 8px", fontSize: 11, color: "var(--fg-subtle)",
        fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>
        交給其他助手
      </div>
      <div>
          {agents.filter(a => a.id !== "anila-router" && a.id !== currentAgentId).map(a => (
            <MenuItem key={a.id}
              onClick={() => { onHandoffAgent(a.id); close(); }}
              leftIcon={<div style={{ width: 10, height: 10, border: "1px solid var(--border-strong)", borderRadius: 2 }}/>}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 500 }}>{a.name}</div>
                <div style={{ fontSize: 10, color: "var(--fg-muted)", fontFamily: "var(--font-mono)" }}>{a.short || a.id}</div>
              </div>
            </MenuItem>
          ))}
      </div>
    </div>
  );

// ---- Tag / Folder editor ----
export const TagEditor = ({ folders, conversation, onUpdate, close }) => {
  const [tagInput, setTagInput] = useState("");
  return (
    <div style={{ minWidth: 240, padding: 6 }}>
      <div style={{ padding: "6px 6px 8px", fontSize: 11, color: "var(--fg-subtle)",
        fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>歸入資料夾</div>
      {folders.filter(f => f.id !== "all" && f.id !== "starred").map(f => (
        <MenuItem key={f.id}
          active={conversation.folder === f.id}
          leftIcon={<IconFolder size={13}/>}
          rightIcon={conversation.folder === f.id ? <IconCheck size={12}/> : null}
          onClick={() => { onUpdate({ folder: f.id }); }}>
          {f.name}
        </MenuItem>
      ))}
      <Divider style={{ margin: "6px 0" }}/>
      <div style={{ padding: "4px 6px 4px", fontSize: 11, color: "var(--fg-subtle)",
        fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>標籤</div>
      <div style={{ padding: "4px 6px 6px", display: "flex", flexWrap: "wrap", gap: 4 }}>
        {(conversation.tags || []).map(t => (
          <span key={t} style={{
            display: "inline-flex", alignItems: "center", gap: 3,
            padding: "2px 6px",
            fontSize: 11, fontFamily: "var(--font-mono)",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: 999,
          }}>
            #{t}
            {t !== "classified" && (
              <button onClick={() => onUpdate({ tags: (conversation.tags || []).filter(x => x !== t) })}
                style={{ background: "transparent", border: "none", cursor: "pointer",
                  color: "var(--fg-subtle)", padding: 0, display: "flex" }}>
                <IconX size={10}/>
              </button>
            )}
          </span>
        ))}
      </div>
      <div style={{ padding: "0 6px 4px", display: "flex", gap: 4 }}>
        <input value={tagInput} onChange={e => setTagInput(e.target.value)}
          placeholder="新增標籤"
          onKeyDown={e => {
            if (e.key === "Enter" && tagInput.trim()) {
              const next = tagInput.trim();
              if (next === "classified") { setTagInput(""); return; }
              onUpdate({ tags: [...new Set([...(conversation.tags || []).filter(t => t !== "classified"), next])] });
              setTagInput("");
            }
          }}
          style={{
            flex: 1, padding: "4px 8px", fontSize: 12,
            background: "var(--bg-elev)", border: "1px solid var(--border)",
            borderRadius: "var(--radius)", outline: "none", color: "var(--fg)",
          }}/>
      </div>
      <Divider style={{ margin: "6px 0" }}/>
      <MenuItem
        leftIcon={<IconStar size={13}/>}
        rightIcon={conversation.starred ? <IconCheck size={12}/> : null}
        onClick={() => onUpdate({ starred: !conversation.starred })}>
        {conversation.starred ? "取消加星" : "加入星號"}
      </MenuItem>
    </div>
  );
};
