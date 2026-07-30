// Collaboration: named share dialog, handoff-to menu, tag/folder editor (ESM)
import React, { useState, useEffect } from "react";
import { IconShield, IconX, IconCheck, IconStar, IconFolder } from "./icons.jsx";
import { Button, Modal, MenuItem, Divider, Input } from "./components.jsx";
import { formatShareTarget } from "./runtime/conversations.js";

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
      </div>
    </Modal>
  );
};

// ---- Handoff to Agent / User menu ----
export const HandoffMenu = ({ agents, currentAgentId, onHandoffAgent, onHandoffUser, close }) => {
  const [mode, setMode] = useState("agent");
  const [user, setUser] = useState("");

  return (
    <div style={{ minWidth: 260 }}>
      <div style={{ padding: "6px 10px 8px", fontSize: 11, color: "var(--fg-subtle)",
        fontFamily: "var(--font-mono)", letterSpacing: 0.4 }}>
        HANDOFF 交接
      </div>
      <div style={{ display: "flex", gap: 2, padding: "0 6px 6px" }}>
        {[{k: "agent", label: "給 agent"}, {k: "user", label: "給同事"}].map(t => (
          <button key={t.k} onClick={() => setMode(t.k)} style={{
            flex: 1, padding: "5px 8px", fontSize: 12,
            background: mode === t.k ? "var(--bg-subtle)" : "transparent",
            border: "1px solid " + (mode === t.k ? "var(--border)" : "transparent"),
            borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
          }}>{t.label}</button>
        ))}
      </div>
      {mode === "agent" ? (
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
      ) : (
        <div style={{ padding: "0 8px 8px" }}>
          <Input placeholder="輸入同事帳號 (e.g. bob.lin)" value={user}
            onChange={e => setUser(e.target.value)} />
          <Button size="sm" variant="primary" style={{ marginTop: 6, width: "100%" }}
            onClick={() => { if (user) { onHandoffUser(user); close(); } }}>
            送出交接請求
          </Button>
        </div>
      )}
    </div>
  );
};

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
            <button onClick={() => onUpdate({ tags: (conversation.tags || []).filter(x => x !== t) })}
              style={{ background: "transparent", border: "none", cursor: "pointer",
                color: "var(--fg-subtle)", padding: 0, display: "flex" }}>
              <IconX size={10}/>
            </button>
          </span>
        ))}
      </div>
      <div style={{ padding: "0 6px 4px", display: "flex", gap: 4 }}>
        <input value={tagInput} onChange={e => setTagInput(e.target.value)}
          placeholder="新增標籤"
          onKeyDown={e => {
            if (e.key === "Enter" && tagInput.trim()) {
              onUpdate({ tags: [...new Set([...(conversation.tags || []), tagInput.trim()])] });
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
