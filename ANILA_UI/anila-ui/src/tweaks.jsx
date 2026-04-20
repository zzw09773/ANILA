// Tweaks panel — in-design controls

const TweaksPanel = ({ open, onClose, tweaks, setTweaks }) => {
  if (!open) return null;

  const update = (patch) => {
    const next = { ...tweaks, ...patch };
    setTweaks(next);
    window.parent.postMessage({ type: "__edit_mode_set_keys", edits: patch }, "*");
  };

  const Row = ({ label, children }) => (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", fontFamily: "var(--font-mono)", marginBottom: 6, letterSpacing: 0.3 }}>
        {label}
      </div>
      {children}
    </div>
  );

  const accents = [
    { name: "teal",   v: "#0b7285" },
    { name: "slate",  v: "#334155" },
    { name: "moss",   v: "#4a6444" },
    { name: "clay",   v: "#a05a2c" },
    { name: "indigo", v: "#3949a1" },
    { name: "crimson",v: "#9c2a3b" },
  ];

  return (
    <div style={{
      position: "fixed", right: 16, bottom: 16, zIndex: 90,
      width: 320,
      background: "var(--bg-elev)",
      border: "1px solid var(--border)",
      borderRadius: "var(--radius-lg)",
      boxShadow: "0 12px 40px -10px oklch(0.10 0 0 / 0.25)",
      overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "10px 14px",
        borderBottom: "1px solid var(--border)",
        background: "var(--bg-subtle)",
      }}>
        <IconSpark size={14}/>
        <div style={{ fontWeight: 600, fontSize: 13 }}>Tweaks</div>
        <div style={{ flex: 1 }}/>
        <IconButton onClick={onClose}><IconX/></IconButton>
      </div>

      <div style={{ padding: 14, maxHeight: 460, overflowY: "auto" }}>
        <Row label="ACCENT COLOR">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {accents.map(a => (
              <button key={a.v} onClick={() => update({ accent: a.v })} title={a.name} style={{
                width: 28, height: 28, borderRadius: 999,
                background: a.v,
                border: "2px solid " + (tweaks.accent === a.v ? "var(--fg)" : "transparent"),
                outline: "1px solid var(--border)",
                cursor: "pointer",
              }}/>
            ))}
          </div>
        </Row>

        <Row label="THEME">
          <div style={{ display: "flex", gap: 4, background: "var(--bg-subtle)", padding: 3, borderRadius: "var(--radius)", border: "1px solid var(--border)" }}>
            {[{k: false, label: "淺色"}, {k: true, label: "深色"}].map(t => (
              <button key={String(t.k)} onClick={() => update({ dark: t.k })} style={{
                flex: 1, padding: "5px 8px", fontSize: 12, fontWeight: 500,
                background: tweaks.dark === t.k ? "var(--bg-elev)" : "transparent",
                border: "1px solid " + (tweaks.dark === t.k ? "var(--border)" : "transparent"),
                borderRadius: 4, cursor: "pointer", color: "var(--fg)",
              }}>{t.label}</button>
            ))}
          </div>
        </Row>

        <Row label={`資訊密度 (padding: ${tweaks.density}px)`}>
          <input type="range" min={10} max={24} step={1}
            value={tweaks.density}
            onChange={e => update({ density: Number(e.target.value) })}
            style={{ width: "100%", accentColor: "var(--accent)" }}/>
        </Row>

        <Row label="中文字體">
          <select value={tweaks.sansFamily}
            onChange={e => update({ sansFamily: e.target.value })}
            style={{
              width: "100%", padding: "6px 8px", fontSize: 12,
              background: "var(--bg-elev)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", color: "var(--fg)",
              fontFamily: tweaks.sansFamily,
            }}>
            {["Noto Sans TC", "Inter", "system-ui"].map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </Row>

        <Row label="等寬字體 (routing trace)">
          <select value={tweaks.monoFamily}
            onChange={e => update({ monoFamily: e.target.value })}
            style={{
              width: "100%", padding: "6px 8px", fontSize: 12,
              background: "var(--bg-elev)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", color: "var(--fg)",
              fontFamily: tweaks.monoFamily,
            }}>
            {["JetBrains Mono", "ui-monospace", "Menlo"].map(f => <option key={f} value={f}>{f}</option>)}
          </select>
        </Row>

        <Row label="ROUTING TRACE 顯示">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4 }}>
            {[
              { k: "collapsible", label: "可收合" },
              { k: "always-open", label: "永遠展開" },
              { k: "hidden",      label: "隱藏" },
            ].map(o => (
              <button key={o.k} onClick={() => update({ traceStyle: o.k })} style={{
                padding: "5px 6px", fontSize: 11, fontWeight: 500,
                background: tweaks.traceStyle === o.k ? "var(--accent-soft)" : "var(--bg-subtle)",
                border: "1px solid " + (tweaks.traceStyle === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: 4, cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
        </Row>

        <Row label="AGENT SWITCHER 位置">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
            {[
              { k: "top",    label: "頂部" },
              { k: "bottom", label: "靠近輸入框" },
            ].map(o => (
              <button key={o.k} onClick={() => update({ agentSwitcherPosition: o.k })} style={{
                padding: "5px 6px", fontSize: 11, fontWeight: 500,
                background: tweaks.agentSwitcherPosition === o.k ? "var(--accent-soft)" : "var(--bg-subtle)",
                border: "1px solid " + (tweaks.agentSwitcherPosition === o.k ? "var(--accent)" : "var(--border)"),
                borderRadius: 4, cursor: "pointer", color: "var(--fg)",
              }}>{o.label}</button>
            ))}
          </div>
        </Row>
      </div>
    </div>
  );
};

window.TweaksPanel = TweaksPanel;
