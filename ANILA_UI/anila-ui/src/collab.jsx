// Collaboration: share dialog, handoff-to menu, tag/folder editor (ESM)
import React, { useState } from "react";
import { IconShield, IconLink, IconX, IconCheck, IconStar, IconFolder } from "./icons.jsx";
import { Button, Modal, MenuItem, Divider, Input } from "./components.jsx";

// TTL choice → ISO timestamp the backend understands.
function ttlToExpiresAt(ttlKey) {
  if (ttlKey === "never") return null;
  const map = { "1h": 3600, "24h": 86400, "7d": 604800 };
  const seconds = map[ttlKey];
  if (!seconds) return null;
  return new Date(Date.now() + seconds * 1000).toISOString();
}

// ---- Share Dialog ----
// `onCreateShare({ mode, allowFork, expiresAt })` is expected to return a
// promise resolving to `{ url, token, ... }`. The dialog stays UI-only and
// delegates persistence to the caller.
export const ShareDialog = ({ open, onClose, conversation, user, onCreateShare }) => {
  const [ttl, setTtl] = useState("24h");
  const [scope, setScope] = useState("org");
  const [allowFork, setAllowFork] = useState(true);
  const [linkCreated, setLinkCreated] = useState(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!open) return null;

  const createLink = async () => {
    if (!onCreateShare) {
      setError("尚未提供 onCreateShare handler，無法建立分享連結");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await onCreateShare({
        mode: "read_only",
        allowFork,
        expiresAt: ttlToExpiresAt(ttl),
      });
      setLinkCreated(result?.url || result);
    } catch (err) {
      setError(err?.message || "建立分享連結失敗");
    } finally {
      setBusy(false);
    }
  };
  const copyLink = () => {
    navigator.clipboard?.writeText(linkCreated);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
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
            <IconShield size={13} /> <b>唯讀分享</b>
          </div>
          被分享者只能讀取這份對話。分享紀錄會寫入 CSP audit log。
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
              <button key={o.k} onClick={() => setTtl(o.k)} style={{
                flex: 1, padding: "6px 8px", fontSize: 12,
                background: ttl === o.k ? "var(--accent-soft)" : "var(--bg-elev)",
                border: "1px solid " + (ttl === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>可存取對象</div>
          <div style={{ display: "flex", gap: 4 }}>
            {[
              { k: "org", label: "組織內" },
              { k: "team", label: "同部門" },
              { k: "specific", label: "指定人" },
            ].map(o => (
              <button key={o.k} onClick={() => setScope(o.k)} style={{
                flex: 1, padding: "6px 8px", fontSize: 12,
                background: scope === o.k ? "var(--accent-soft)" : "var(--bg-elev)",
                border: "1px solid " + (scope === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: "var(--radius)", cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
        </div>

        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12 }}>
          <input type="checkbox" checked={allowFork} onChange={e => setAllowFork(e.target.checked)} />
          允許 fork 到對方的空間繼續對話
        </label>

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

        {!linkCreated ? (
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button onClick={onClose} disabled={busy}>取消</Button>
            <Button variant="primary" leftIcon={<IconLink size={13}/>} onClick={createLink} disabled={busy}>
              {busy ? "建立中…" : "產生連結"}
            </Button>
          </div>
        ) : (
          <div style={{
            padding: 10,
            background: "var(--bg-elev)",
            border: "1px solid var(--accent)",
            borderRadius: "var(--radius)",
            display: "flex", alignItems: "center", gap: 8,
          }}>
            <IconLink size={14} style={{ color: "var(--accent)" }}/>
            <div style={{
              flex: 1, fontFamily: "var(--font-mono)", fontSize: 12,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>{linkCreated}</div>
            <Button size="sm" variant="primary" onClick={copyLink}>
              {copied ? "✓ 已複製" : "複製"}
            </Button>
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
