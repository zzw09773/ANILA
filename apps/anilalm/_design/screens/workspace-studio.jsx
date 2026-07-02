// Workspace — Studio (redesigned: Workbench/Atelier concept, NOT ANILA LM-style)
// Visual language: vertical timeline + category rail + command-palette modal
const WSStudio = ({ t, theme }) => {
  const Icon = window.Icon;
  const [modal, setModal] = React.useState(null);
  const [filter, setFilter] = React.useState("all");

  const categories = [
    { k: "all",    l: "全部",   c: t.text },
    { k: "audio",  l: "聲音",   c: "#FF8FAB" },
    { k: "visual", l: "視覺",   c: "#7C7BFF" },
    { k: "study",  l: "學習",   c: "#3DD68C" },
    { k: "doc",    l: "文件",   c: "#F4B740" },
  ];

  const formats = [
    { k: "podcast",     l: "語音摘要",  i: "mic",   c: "#FF8FAB", cat: "audio",  hint: "兩位主持人對談" },
    { k: "slides",      l: "簡報",      i: "deck",  c: "#7C7BFF", cat: "visual", hint: "12 張投影片" },
    { k: "video",       l: "影片腳本",  i: "video", c: "#5BC0EB", cat: "visual", hint: "含分鏡與旁白" },
    { k: "mindmap",     l: "心智圖",    i: "git",   c: "#3DD68C", cat: "visual", hint: "可展開分支" },
    { k: "report",      l: "深度報告",  i: "file",  c: "#F4B740", cat: "doc",    hint: "含引用文獻" },
    { k: "flashcards",  l: "抽認卡",    i: "flash", c: "#C792EA", cat: "study",  hint: "間隔複習" },
    { k: "quiz",        l: "測驗",      i: "quiz",  c: "#FF6B6B", cat: "study",  hint: "選擇 + 申論" },
    { k: "infographic", l: "資訊圖表",  i: "chart", c: "#5BC0EB", cat: "visual", hint: "數據可視化" },
    { k: "datatable",   l: "資料表",    i: "table", c: "#3DD68C", cat: "doc",    hint: "結構化整理" },
  ];

  const timeline = [
    { i: "mic",   t: "GPT-5 在數學推理的躍進", k: "語音摘要", src: 4, ago: "剛剛",  c: "#FF8FAB", playable: true, dur: "8:24" },
    { i: "deck",  t: "Transformer 架構演進回顧", k: "簡報", src: 3, ago: "今天 14:22",  c: "#7C7BFF", pages: "12 頁" },
    { i: "git",   t: "Constitutional AI 核心概念", k: "心智圖", src: 1, ago: "昨天 09:15", c: "#3DD68C" },
    { i: "flash", t: "Scaling laws 抽認卡組", k: "抽認卡", src: 2, ago: "3 天前", c: "#C792EA", count: "24 張" },
  ];

  const filteredFormats = filter === "all" ? formats : formats.filter(f => f.cat === filter);

  return (
    <aside style={{
      width: 380, height: "100%", background: t.surface,
      borderLeft: `1px solid ${t.border}`,
      display: "flex", flexDirection: "column", position: "relative",
    }}>
      {/* Header */}
      <div style={{
        height: 56, padding: "0 18px", borderBottom: `1px solid ${t.border}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <div style={{
            width: 24, height: 24, borderRadius: 6, background: t.accentSoft,
            display: "grid", placeItems: "center", border: `1px solid ${t.accentBorder}`,
          }}>
            <Icon name="sparkle" size={12} stroke={t.accent} />
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, letterSpacing: -0.1 }}>製作台</div>
          <span style={{ fontSize: 10.5, color: t.textSubtle, padding: "1px 6px", border: `1px solid ${t.border}`, borderRadius: 4 }}>
            {timeline.length}
          </span>
        </div>
        <button style={{ ...window.iconBtn(t), width: 28, height: 28 }}>
          <Icon name="panel" size={13} stroke={t.textMuted} />
        </button>
      </div>

      {/* Quick action — large primary CTA */}
      <div style={{ padding: "14px 14px 6px" }}>
        <button onClick={() => setModal({ k: "all", l: "新製品" })} style={{
          width: "100%", padding: "11px 14px", borderRadius: 10,
          background: t.surface2, border: `1px solid ${t.border}`,
          color: t.textMuted, fontSize: 12.5, cursor: "pointer", fontFamily: "inherit",
          display: "flex", alignItems: "center", gap: 9,
        }}>
          <Icon name="search" size={13} stroke={t.textMuted} />
          <span>輸入想做什麼，或從下方挑選...</span>
          <span style={{ marginLeft: "auto", padding: "1px 5px", border: `1px solid ${t.border}`, borderRadius: 4, fontSize: 10, color: t.textSubtle }}>⌘ J</span>
        </button>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "8px 0 80px" }}>
        {/* Category rail */}
        <div style={{ padding: "8px 14px 10px", display: "flex", gap: 6, flexWrap: "wrap" }}>
          {categories.map(c => (
            <button key={c.k} onClick={() => setFilter(c.k)} style={{
              padding: "5px 10px", borderRadius: 999, cursor: "pointer", fontFamily: "inherit",
              background: filter === c.k ? t.text : "transparent",
              color: filter === c.k ? t.bg : t.textMuted,
              border: filter === c.k ? `1px solid ${t.text}` : `1px solid ${t.border}`,
              fontSize: 11.5, fontWeight: 500,
              display: "inline-flex", alignItems: "center", gap: 5,
            }}>
              {c.k !== "all" && <span style={{ width: 6, height: 6, borderRadius: 999, background: c.c }} />}
              {c.l}
            </button>
          ))}
        </div>

        {/* Format list (single-column rows, NOT a 3×3 grid) */}
        <div style={{ padding: "0 14px", display: "flex", flexDirection: "column", gap: 4 }}>
          {filteredFormats.map((f) => (
            <button key={f.k} onClick={() => setModal(f)} style={{
              padding: "10px 11px", borderRadius: 9, cursor: "pointer",
              background: "transparent", border: `1px solid transparent`,
              display: "flex", alignItems: "center", gap: 11, fontFamily: "inherit", textAlign: "left",
              transition: "all 120ms",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = t.surface2; e.currentTarget.style.borderColor = t.border; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.borderColor = "transparent"; }}
            >
              <div style={{
                width: 28, height: 28, borderRadius: 7,
                background: `${f.c}1f`,
                display: "grid", placeItems: "center", flexShrink: 0,
                border: `1px solid ${f.c}33`,
              }}>
                <Icon name={f.i} size={13} stroke={f.c} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12.5, fontWeight: 500, color: t.text }}>{f.l}</div>
                <div style={{ fontSize: 10.5, color: t.textSubtle, marginTop: 1 }}>{f.hint}</div>
              </div>
              <Icon name="plus" size={13} stroke={t.textMuted} />
            </button>
          ))}
        </div>

        {/* Timeline of generated artifacts */}
        <div style={{ marginTop: 22, padding: "0 14px" }}>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "0 4px 10px",
          }}>
            <div style={{ fontSize: 10.5, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1 }}>
              ── 已完成
            </div>
            <button style={{
              fontSize: 10.5, color: t.textSubtle, background: "transparent",
              border: "none", cursor: "pointer", fontFamily: "inherit",
            }}>查看全部</button>
          </div>

          <div style={{ position: "relative", paddingLeft: 14 }}>
            {/* Vertical timeline rail */}
            <div style={{
              position: "absolute", left: 5, top: 6, bottom: 6, width: 1,
              background: t.border,
            }} />
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {timeline.map((h, i) => (
                <div key={i} style={{ position: "relative" }}>
                  {/* Timeline dot */}
                  <div style={{
                    position: "absolute", left: -14, top: 14,
                    width: 11, height: 11, borderRadius: "50%",
                    background: t.surface, border: `2px solid ${h.c}`,
                  }} />
                  <div style={{
                    padding: "11px 12px", borderRadius: 10, cursor: "pointer",
                    background: t.surface2, border: `1px solid ${t.border}`,
                    display: "flex", flexDirection: "column", gap: 8,
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <div style={{
                        padding: "2px 7px", borderRadius: 4,
                        background: `${h.c}22`, color: h.c,
                        fontSize: 10, fontWeight: 600,
                      }}>{h.k}</div>
                      <span style={{ fontSize: 10.5, color: t.textSubtle, marginLeft: "auto" }}>{h.ago}</span>
                    </div>
                    <div style={{ fontSize: 12.5, fontWeight: 500, color: t.text, lineHeight: 1.35 }}>
                      {h.t}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 10.5, color: t.textMuted }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                        <Icon name="file" size={10} stroke={t.textMuted} /> {h.src} 來源
                      </span>
                      {h.dur && <span>· {h.dur}</span>}
                      {h.pages && <span>· {h.pages}</span>}
                      {h.count && <span>· {h.count}</span>}
                      <div style={{ marginLeft: "auto", display: "flex", gap: 2 }}>
                        {h.playable && (
                          <button style={smIconBtn(t)}>
                            <svg width="11" height="11" viewBox="0 0 24 24" fill={t.textMuted}><polygon points="6 4 20 12 6 20" /></svg>
                          </button>
                        )}
                        <button style={smIconBtn(t)}>
                          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={t.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <circle cx="12" cy="5" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="12" cy="19" r="1" />
                          </svg>
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Sticky footer */}
      <div style={{
        position: "absolute", left: 0, right: 0, bottom: 0,
        padding: "10px 14px",
        background: `linear-gradient(180deg, transparent, ${t.surface} 30%)`,
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <div style={{ fontSize: 10.5, color: t.textSubtle }}>
          基於 4 個來源
        </div>
      </div>

      {modal && <CommandModal t={t} theme={theme} item={modal} onClose={() => setModal(null)} />}
    </aside>
  );
};

const smIconBtn = (t) => ({
  width: 22, height: 22, borderRadius: 5, border: "none",
  background: "transparent", cursor: "pointer",
  display: "grid", placeItems: "center",
});

// ─── Command-style Modal (single-column narrative, NOT tabbed cards) ────
const CommandModal = ({ t, theme, item, onClose }) => {
  const Icon = window.Icon;
  const [step, setStep] = React.useState(0);
  const [selected, setSelected] = React.useState(0);

  const presets = {
    report: [
      { l: "深度技術綜述",    d: "嚴謹學術風格、含完整引用與章節結構", tag: "推薦" },
      { l: "重點摘要",        d: "1-2 頁的精華筆記，適合快速回顧" },
      { l: "教學講義",        d: "概念 + 範例 + 練習題的學習導向格式" },
      { l: "對外溝通文件",    d: "客觀中立、適合分享給非技術讀者" },
    ],
    podcast: [
      { l: "深入對談",  d: "兩位主持人辯證觀點 · 約 8-12 分鐘", tag: "推薦" },
      { l: "說書節奏",  d: "敘事旁白把研究材料說成故事 · 5-7 分鐘" },
      { l: "快速摘要",  d: "單人旁白核心要點 · 3 分鐘以內" },
    ],
    slides: [
      { l: "經典報告結構",  d: "封面 → 大綱 → 內容 → 結論 · 10-15 張", tag: "推薦" },
      { l: "Lightning Talk", d: "5 張投影片濃縮版" },
      { l: "教學投影片",     d: "概念 + 範例 + 練習" },
    ],
  };

  const list = presets[item.k] || [
    { l: `自動${item.l}`,  d: "由 AI 依文件內容決定最適合的呈現方式", tag: "推薦" },
    { l: "簡短版本",        d: "壓縮為核心要點" },
    { l: "詳細版本",        d: "包含完整脈絡與背景" },
  ];

  // AI-suggested angles (insight cards rather than tabs)
  const insights = [
    { l: "聚焦在數學推理章節",  d: "從 GPT-5 報告第 4.2 節抽取 benchmark 對比" },
    { l: "對比 GPT-4 vs GPT-5",  d: "把兩代差異整理成對照表，含百分比變化" },
    { l: "面向學生的科普版",     d: "假設讀者沒有 ML 背景，把術語都解釋一遍" },
  ];

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 100,
      display: "grid", placeItems: "center",
      background: theme === "dark" ? "rgba(0,0,0,0.6)" : "rgba(20,20,20,0.5)",
      backdropFilter: "blur(4px)",
    }} onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 560, maxWidth: "92vw", maxHeight: "84vh",
        background: t.surface, border: `1px solid ${t.border}`,
        borderRadius: 14, display: "flex", flexDirection: "column",
        boxShadow: theme === "dark"
          ? "0 30px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04)"
          : "0 30px 80px rgba(0,0,0,0.18)",
        overflow: "hidden",
      }}>
        {/* Compact header — command-bar style */}
        <div style={{
          padding: "14px 16px", display: "flex", alignItems: "center", gap: 10,
          borderBottom: `1px solid ${t.border}`,
        }}>
          <div style={{
            width: 26, height: 26, borderRadius: 7, background: `${item.c}22`,
            display: "grid", placeItems: "center", border: `1px solid ${item.c}33`,
          }}><Icon name={item.i} size={12} stroke={item.c} /></div>
          <div style={{ fontSize: 13, fontWeight: 500, color: t.text }}>建立 {item.l}</div>
          <span style={{ fontSize: 11, color: t.textSubtle }}>· 步驟 {step + 1} / 2</span>
          <button onClick={onClose} style={{
            marginLeft: "auto", width: 24, height: 24, borderRadius: 6, border: "none",
            background: "transparent", cursor: "pointer",
            display: "grid", placeItems: "center", color: t.textMuted,
          }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: "18px 16px 8px" }}>
          {step === 0 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10 }}>
                01 · 選擇風格
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 22 }}>
                {list.map((p, i) => (
                  <button key={i} onClick={() => setSelected(i)} style={{
                    textAlign: "left", padding: "12px 14px", borderRadius: 10, cursor: "pointer",
                    background: selected === i ? t.accentSoft : t.surface2,
                    border: `1px solid ${selected === i ? t.accentBorder : t.border}`,
                    display: "flex", alignItems: "flex-start", gap: 11, fontFamily: "inherit",
                  }}>
                    <div style={{
                      width: 16, height: 16, borderRadius: "50%", flexShrink: 0, marginTop: 2,
                      background: selected === i ? t.accent : "transparent",
                      border: `1.5px solid ${selected === i ? t.accent : t.borderStrong}`,
                      display: "grid", placeItems: "center",
                    }}>
                      {selected === i && <div style={{ width: 6, height: 6, borderRadius: "50%", background: "#fff" }} />}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 3 }}>
                        <span style={{ fontSize: 13, fontWeight: 500, color: t.text }}>{p.l}</span>
                        {p.tag && <span style={{
                          fontSize: 9.5, fontWeight: 600, color: t.accent,
                          padding: "1px 6px", background: t.accentSoft, borderRadius: 4,
                          letterSpacing: 0.4, border: `1px solid ${t.accentBorder}`,
                        }}>{p.tag.toUpperCase()}</span>}
                      </div>
                      <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.5 }}>{p.d}</div>
                    </div>
                  </button>
                ))}
              </div>

              <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10, display: "flex", alignItems: "center", gap: 7 }}>
                <Icon name="sparkle" size={11} stroke={t.accent} />
                AI 觀察 · 你的來源中可能值得探索的角度
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {insights.map((s, i) => (
                  <button key={i} style={{
                    textAlign: "left", padding: "10px 13px", borderRadius: 9, cursor: "pointer",
                    background: t.surface2, border: `1px dashed ${t.borderStrong}`,
                    display: "flex", flexDirection: "column", gap: 2, fontFamily: "inherit",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 12.5, fontWeight: 500, color: t.text }}>{s.l}</span>
                      <Icon name="plus" size={11} stroke={t.textMuted} />
                    </div>
                    <span style={{ fontSize: 11, color: t.textMuted, lineHeight: 1.5 }}>{s.d}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {step === 1 && (
            <>
              <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10 }}>
                02 · 補充指示（可略過）
              </div>
              <textarea
                placeholder="例：聚焦在 4.2 節的數學評估、避免提及商業競爭..."
                rows={5}
                style={{
                  width: "100%", padding: 13, borderRadius: 10,
                  background: t.surface2, border: `1px solid ${t.border}`,
                  color: t.text, fontSize: 13, fontFamily: "inherit", lineHeight: 1.55,
                  outline: "none", resize: "vertical",
                }}
              />
              <div style={{
                marginTop: 14, padding: "11px 13px", borderRadius: 10,
                background: t.accentSoft, border: `1px solid ${t.accentBorder}`,
                fontSize: 11.5, color: t.text, lineHeight: 1.55,
                display: "flex", gap: 9, alignItems: "flex-start",
              }}>
                <Icon name="sparkle" size={13} stroke={t.accent} style={{ marginTop: 1, flexShrink: 0 }} />
                <div>
                  <div style={{ fontWeight: 500, marginBottom: 2 }}>已選擇：{list[selected].l}</div>
                  <div style={{ color: t.textMuted }}>4 個來源將會被引用 · 預估生成時間 30-60 秒</div>
                </div>
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: "12px 16px", borderTop: `1px solid ${t.border}`,
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div style={{ display: "flex", gap: 4 }}>
            {[0, 1].map(i => (
              <div key={i} style={{
                width: step >= i ? 18 : 6, height: 4, borderRadius: 2,
                background: step >= i ? t.accent : t.border, transition: "all 200ms",
              }} />
            ))}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {step > 0 && (
              <button onClick={() => setStep(step - 1)} style={{
                padding: "7px 14px", borderRadius: 8, border: `1px solid ${t.border}`,
                background: t.surface, color: t.text, fontSize: 12.5, fontWeight: 500,
                cursor: "pointer", fontFamily: "inherit",
              }}>上一步</button>
            )}
            {step === 0 && (
              <button onClick={() => setStep(1)} style={{
                padding: "7px 16px", borderRadius: 8, border: "none",
                background: t.accent, color: "#fff", fontSize: 12.5, fontWeight: 500,
                cursor: "pointer", fontFamily: "inherit",
                display: "inline-flex", alignItems: "center", gap: 6,
                boxShadow: `0 4px 14px -4px ${t.accent}`,
              }}>
                繼續 <Icon name="arrowR" size={11} stroke="#fff" />
              </button>
            )}
            {step === 1 && (
              <button style={{
                padding: "7px 16px", borderRadius: 8, border: "none",
                background: t.accent, color: "#fff", fontSize: 12.5, fontWeight: 500,
                cursor: "pointer", fontFamily: "inherit",
                display: "inline-flex", alignItems: "center", gap: 6,
                boxShadow: `0 4px 14px -4px ${t.accent}`,
              }}>
                <Icon name="sparkle" size={11} stroke="#fff" /> 開始鑄造
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

// Static variants for canvas (modal pre-opened)
const WorkspaceWithModal = ({ theme = "dark", defaultStep = 0 }) => {
  const t = window.useThemeVars(theme);
  const item = { i: "file", l: "深度報告", c: "#F4B740", k: "report" };
  return (
    <div style={{
      width: 1440, height: 900, background: t.bg, color: t.text,
      fontFamily: '"Inter","Noto Sans TC",system-ui,sans-serif',
      display: "flex", overflow: "hidden", position: "relative",
    }}>
      <window.WSSidebar t={t} theme={theme} />
      <window.WSChat t={t} theme={theme} flex={1.4} />
      <WSStudio t={t} theme={theme} />
      <div style={{
        position: "absolute", inset: 0, zIndex: 100,
        display: "grid", placeItems: "center",
        background: theme === "dark" ? "rgba(0,0,0,0.6)" : "rgba(20,20,20,0.5)",
        backdropFilter: "blur(4px)",
      }}>
        <PinnedCommand t={t} theme={theme} item={item} step={defaultStep} />
      </div>
    </div>
  );
};

const PinnedCommand = ({ t, theme, item, step }) => {
  // Reuse CommandModal but with fixed step
  const [s, setS] = React.useState(step);
  React.useEffect(() => setS(step), [step]);
  return <CommandModalInline t={t} theme={theme} item={item} step={s} setStep={setS} onClose={() => {}} />;
};

const CommandModalInline = ({ t, theme, item, step, setStep, onClose }) => {
  const Icon = window.Icon;
  const [selected, setSelected] = React.useState(0);

  const list = [
    { l: "深度技術綜述",    d: "嚴謹學術風格、含完整引用與章節結構", tag: "推薦" },
    { l: "重點摘要",        d: "1-2 頁的精華筆記，適合快速回顧" },
    { l: "教學講義",        d: "概念 + 範例 + 練習題的學習導向格式" },
    { l: "對外溝通文件",    d: "客觀中立、適合分享給非技術讀者" },
  ];
  const insights = [
    { l: "聚焦在數學推理章節",  d: "從 GPT-5 報告第 4.2 節抽取 benchmark 對比" },
    { l: "對比 GPT-4 vs GPT-5",  d: "把兩代差異整理成對照表，含百分比變化" },
    { l: "面向學生的科普版",     d: "假設讀者沒有 ML 背景，把術語都解釋一遍" },
  ];

  return (
    <div style={{
      width: 560, maxWidth: "92vw", maxHeight: "84vh",
      background: t.surface, border: `1px solid ${t.border}`,
      borderRadius: 14, display: "flex", flexDirection: "column",
      boxShadow: theme === "dark" ? "0 30px 80px rgba(0,0,0,0.7)" : "0 30px 80px rgba(0,0,0,0.18)",
      overflow: "hidden",
    }}>
      <div style={{
        padding: "14px 16px", display: "flex", alignItems: "center", gap: 10,
        borderBottom: `1px solid ${t.border}`,
      }}>
        <div style={{
          width: 26, height: 26, borderRadius: 7, background: `${item.c}22`,
          display: "grid", placeItems: "center", border: `1px solid ${item.c}33`,
        }}><Icon name={item.i} size={12} stroke={item.c} /></div>
        <div style={{ fontSize: 13, fontWeight: 500 }}>建立 {item.l}</div>
        <span style={{ fontSize: 11, color: t.textSubtle }}>· 步驟 {step + 1} / 2</span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "18px 16px 8px" }}>
        {step === 0 && (
          <>
            <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10 }}>01 · 選擇風格</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 22 }}>
              {list.map((p, i) => (
                <div key={i} style={{
                  padding: "12px 14px", borderRadius: 10,
                  background: i === 0 ? t.accentSoft : t.surface2,
                  border: `1px solid ${i === 0 ? t.accentBorder : t.border}`,
                  display: "flex", alignItems: "flex-start", gap: 11,
                }}>
                  <div style={{
                    width: 16, height: 16, borderRadius: "50%", flexShrink: 0, marginTop: 2,
                    background: i === 0 ? t.accent : "transparent",
                    border: `1.5px solid ${i === 0 ? t.accent : t.borderStrong}`,
                    display: "grid", placeItems: "center",
                  }}>
                    {i === 0 && <div style={{ width: 6, height: 6, borderRadius: "50%", background: "#fff" }} />}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 3 }}>
                      <span style={{ fontSize: 13, fontWeight: 500 }}>{p.l}</span>
                      {p.tag && <span style={{
                        fontSize: 9.5, fontWeight: 600, color: t.accent,
                        padding: "1px 6px", background: t.accentSoft, borderRadius: 4,
                        letterSpacing: 0.4, border: `1px solid ${t.accentBorder}`,
                      }}>推薦</span>}
                    </div>
                    <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.5 }}>{p.d}</div>
                  </div>
                </div>
              ))}
            </div>
            <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10, display: "flex", alignItems: "center", gap: 7 }}>
              <Icon name="sparkle" size={11} stroke={t.accent} /> AI 觀察 · 你的來源中可能值得探索的角度
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {insights.map((s, i) => (
                <div key={i} style={{
                  padding: "10px 13px", borderRadius: 9,
                  background: t.surface2, border: `1px dashed ${t.borderStrong}`,
                }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 12.5, fontWeight: 500 }}>{s.l}</span>
                    <Icon name="plus" size={11} stroke={t.textMuted} />
                  </div>
                  <div style={{ fontSize: 11, color: t.textMuted, lineHeight: 1.5, marginTop: 2 }}>{s.d}</div>
                </div>
              ))}
            </div>
          </>
        )}
        {step === 1 && (
          <>
            <div style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, textTransform: "uppercase", letterSpacing: 1, marginBottom: 10 }}>02 · 補充指示（可略過）</div>
            <div style={{
              minHeight: 100, padding: 13, borderRadius: 10,
              background: t.surface2, border: `1px solid ${t.border}`,
              color: t.textSubtle, fontSize: 13, lineHeight: 1.55,
            }}>例：聚焦在 4.2 節的數學評估、避免提及商業競爭...</div>
            <div style={{
              marginTop: 14, padding: "11px 13px", borderRadius: 10,
              background: t.accentSoft, border: `1px solid ${t.accentBorder}`,
              fontSize: 11.5, color: t.text, lineHeight: 1.55,
              display: "flex", gap: 9, alignItems: "flex-start",
            }}>
              <Icon name="sparkle" size={13} stroke={t.accent} style={{ marginTop: 1, flexShrink: 0 }} />
              <div>
                <div style={{ fontWeight: 500, marginBottom: 2 }}>已選擇：深度技術綜述</div>
                <div style={{ color: t.textMuted }}>4 個來源將會被引用 · 預估生成時間 30-60 秒</div>
              </div>
            </div>
          </>
        )}
      </div>

      <div style={{
        padding: "12px 16px", borderTop: `1px solid ${t.border}`,
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", gap: 4 }}>
          {[0, 1].map(i => (
            <div key={i} style={{
              width: step >= i ? 18 : 6, height: 4, borderRadius: 2,
              background: step >= i ? t.accent : t.border,
            }} />
          ))}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {step === 1 && (
            <button onClick={() => setStep(0)} style={{
              padding: "7px 14px", borderRadius: 8, border: `1px solid ${t.border}`,
              background: t.surface, color: t.text, fontSize: 12.5, fontWeight: 500,
              cursor: "pointer", fontFamily: "inherit",
            }}>上一步</button>
          )}
          <button style={{
            padding: "7px 16px", borderRadius: 8, border: "none",
            background: t.accent, color: "#fff", fontSize: 12.5, fontWeight: 500,
            cursor: "pointer", fontFamily: "inherit",
            display: "inline-flex", alignItems: "center", gap: 6,
            boxShadow: `0 4px 14px -4px ${t.accent}`,
          }}>
            {step === 0 ? <>繼續 <Icon name="arrowR" size={11} stroke="#fff" /></> : <><Icon name="sparkle" size={11} stroke="#fff" /> 開始鑄造</>}
          </button>
        </div>
      </div>
    </div>
  );
};

window.WSStudio = WSStudio;
window.WorkspaceWithModal = WorkspaceWithModal;
