// Main app — chat runtime with trust, multi-agent, collab features

const { useState: _aUS, useEffect: _aUE, useRef: _aUR, useCallback: _aUC } = React;

const applyTweaks = (t) => {
  const r = document.documentElement;
  r.setAttribute("data-theme", t.dark ? "dark" : "light");
  r.style.setProperty("--accent", t.accent);
  r.style.setProperty("--density", `${t.density}px`);
  r.style.setProperty("--font-sans", `"${t.sansFamily}", "Inter", system-ui, sans-serif`);
  r.style.setProperty("--font-mono", `"${t.monoFamily}", ui-monospace, Menlo, monospace`);
};

// ---- Send + stream helper ----
function streamResponseInto({ text, agentId, onStage, onChunk, onDone, onInit }) {
  const { routed, trace, body, citations, confidence, followUps, handoffChain, traceId, latencyMs } =
    generateFakeResponse(text, agentId);
  onInit({ routed, trace, citations, confidence, followUps, handoffChain, traceId, latencyMs });

  let delay = 400;
  trace.forEach((ev, i) => {
    setTimeout(() => onStage(i, ev.label), delay);
    delay += 450 + Math.random() * 200;
  });

  setTimeout(() => {
    const chunks = chunkText(body);
    let acc = "";
    const tick = (idx) => {
      if (idx >= chunks.length) { onDone(); return; }
      acc += chunks[idx];
      onChunk(acc);
      setTimeout(() => tick(idx + 1), 20 + Math.random() * 40);
    };
    tick(0);
  }, delay);
}

// ---- Chat Runtime ----
const ChatRuntime = ({ user, apiKey, setApiKey, onLogout, tweaks, setTweaks, tweaksOpen, setTweaksOpen }) => {
  const [conversations, setConversations] = _aUS(INITIAL_CONVERSATIONS);
  const [selectedConvId, setSelectedConvId] = _aUS(null);
  const [messagesByConv, setMessagesByConv] = _aUS({});
  const [agentId, setAgentId] = _aUS("anila-router");
  const [collapsed, setCollapsed] = _aUS(false);
  const [settingsOpen, setSettingsOpen] = _aUS(false);
  const [settingsTab, setSettingsTab] = _aUS("general");
  const [folder, setFolder] = _aUS("all");

  // Trust UI state
  const [citationsOpen, setCitationsOpen] = _aUS(false);
  const [activeCitations, setActiveCitations] = _aUS([]);
  const [activeCitationId, setActiveCitationId] = _aUS(null);

  // Collab UI state
  const [shareOpen, setShareOpen] = _aUS(false);

  // Multi-agent: compare mode
  const [compareMode, setCompareMode] = _aUS(false);
  const [compareColumns, setCompareColumns] = _aUS([]);
  const [compareMsgs, setCompareMsgs] = _aUS({});

  const scrollRef = _aUR(null);
  const currentMsgs = selectedConvId ? (messagesByConv[selectedConvId] || []) : [];
  const selectedConv = conversations.find(c => c.id === selectedConvId);
  const isClassified = selectedConv?.classified;

  _aUE(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [currentMsgs.length, currentMsgs[currentMsgs.length - 1]?.text]);

  const updateConv = (id, patch) => {
    setConversations(cs => cs.map(c => c.id === id ? { ...c, ...patch } : c));
  };

  const newChat = () => { setSelectedConvId(null); setCitationsOpen(false); };

  const updateMsg = (convId, msgId, patch) => {
    setMessagesByConv(prev => {
      const list = prev[convId] || [];
      return { ...prev, [convId]: list.map(m => m.id === msgId ? { ...m, ...patch } : m) };
    });
  };

  const sendMessage = (text, attachments, meta = {}) => {
    let convId = selectedConvId;
    if (!convId) {
      convId = "cv_" + Math.random().toString(16).slice(2, 10);
      const title = text.slice(0, 28) || (attachments[0]?.name) || "新對話";
      setConversations(prev => [
        { id: convId, title, ts: "剛剛", agent: agentId, folder: "all", tags: [], starred: false, classified: false },
        ...prev,
      ]);
      setSelectedConvId(convId);
    }

    const effectiveAgent = (meta.explicitAgents && meta.explicitAgents.length === 1)
      ? meta.explicitAgents[0] : agentId;

    const userMsg = {
      id: "u-" + Date.now(), role: "user", text, attachments,
      piiHits: meta.piiHits || [],
      explicitAgents: meta.explicitAgents || [],
    };
    const assistantId = "a-" + Date.now();
    const assistantMsg = {
      id: assistantId, role: "assistant", text: "", streaming: true,
      stage: 0, trace: [], routedAgentId: effectiveAgent,
      timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
    };

    setMessagesByConv(prev => ({
      ...prev, [convId]: [...(prev[convId] || []), userMsg, assistantMsg],
    }));

    streamResponseInto({
      text, agentId: effectiveAgent,
      onInit: ({ routed, trace, citations, confidence, followUps, handoffChain, traceId, latencyMs }) => {
        updateMsg(convId, assistantId, {
          routedAgentId: routed, trace, citations, confidence, followUps, handoffChain, traceId, latencyMs,
          stageLabel: trace[0]?.label,
        });
      },
      onStage: (i, label) => updateMsg(convId, assistantId, { stage: i, stageLabel: label }),
      onChunk: (acc) => updateMsg(convId, assistantId, { text: acc }),
      onDone: () => updateMsg(convId, assistantId, { streaming: false }),
    });
  };

  const openCitations = (c) => {
    // find the latest assistant msg with citations
    const msg = [...currentMsgs].reverse().find(m => m.role === "assistant" && m.citations?.length);
    if (msg) {
      setActiveCitations(msg.citations);
      setActiveCitationId(c?.id || null);
      setCitationsOpen(true);
    }
  };

  // ---- Compare mode helpers ----
  const enterCompare = () => {
    setCompareMode(true);
    setCompareColumns([
      { id: "col-a", agentId: "rag-agent" },
      { id: "col-b", agentId: "code-assist" },
    ]);
    setCompareMsgs({});
  };
  const exitCompare = () => { setCompareMode(false); setCompareColumns([]); setCompareMsgs({}); };

  const sendCompare = (text, attachments, meta = {}) => {
    compareColumns.forEach(col => {
      const uId = "u-" + col.id + "-" + Date.now();
      const aId = "a-" + col.id + "-" + Date.now();
      const userMsg = { id: uId, role: "user", text, attachments, piiHits: meta.piiHits || [] };
      const assistantMsg = { id: aId, role: "assistant", text: "", streaming: true, stage: 0, trace: [], routedAgentId: col.agentId };
      setCompareMsgs(prev => ({ ...prev, [col.id]: [...(prev[col.id] || []), userMsg, assistantMsg] }));

      streamResponseInto({
        text, agentId: col.agentId,
        onInit: (init) => setCompareMsgs(prev => ({
          ...prev,
          [col.id]: (prev[col.id] || []).map(m => m.id === aId ? { ...m, ...init, routedAgentId: init.routed, stageLabel: init.trace[0]?.label } : m),
        })),
        onStage: (i, label) => setCompareMsgs(prev => ({
          ...prev,
          [col.id]: (prev[col.id] || []).map(m => m.id === aId ? { ...m, stage: i, stageLabel: label } : m),
        })),
        onChunk: (acc) => setCompareMsgs(prev => ({
          ...prev,
          [col.id]: (prev[col.id] || []).map(m => m.id === aId ? { ...m, text: acc } : m),
        })),
        onDone: () => setCompareMsgs(prev => ({
          ...prev,
          [col.id]: (prev[col.id] || []).map(m => m.id === aId ? { ...m, streaming: false } : m),
        })),
      });
    });
  };

  const adoptColumn = (col) => {
    // Move adopted column's messages into a new (or current) conversation
    const msgs = compareMsgs[col.id] || [];
    let convId = selectedConvId;
    if (!convId) {
      convId = "cv_" + Math.random().toString(16).slice(2, 10);
      const firstUser = msgs.find(m => m.role === "user");
      setConversations(prev => [
        { id: convId, title: firstUser?.text?.slice(0, 28) || "採用比較結果", ts: "剛剛", agent: col.agentId, folder: "all", tags: ["compared"], starred: false, classified: false },
        ...prev,
      ]);
      setSelectedConvId(convId);
    }
    setMessagesByConv(prev => ({ ...prev, [convId]: [...(prev[convId] || []), ...msgs] }));
    exitCompare();
  };

  const selectedAgent = AGENTS.find(a => a.id === agentId);

  // Auto-open citations when clicking a [N]
  const onOpenCitation = (c) => {
    const msg = [...currentMsgs].reverse().find(m => m.role === "assistant" && m.citations?.some(x => x.id === c.id));
    if (msg) { setActiveCitations(msg.citations); setActiveCitationId(c.id); setCitationsOpen(true); }
  };

  return (
    <div style={{ display: "flex", height: "100vh", background: "var(--bg)", position: "relative" }}>
      {isClassified && <ConfidentialWatermark userEmail={user.username} traceId={currentMsgs[currentMsgs.length - 1]?.traceId}/>}
      <Sidebar
        conversations={conversations}
        selectedConvId={selectedConvId}
        onSelectConv={(id) => { setSelectedConvId(id); setCitationsOpen(false); }}
        onNewChat={newChat}
        agents={AGENTS}
        user={user}
        onLogout={onLogout}
        onOpenSettings={(tab) => { setSettingsTab(tab || "general"); setSettingsOpen(true); }}
        onOpenAgentBrowser={() => {}}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed(c => !c)}
        folder={folder} setFolder={setFolder} folders={FOLDERS}
        onOpenTagEditor={(id, patch) => updateConv(id, patch)}
      />

      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {/* top bar */}
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "10px 18px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
        }}>
          {tweaks.agentSwitcherPosition === "top" && !compareMode && (
            <AgentSelector agents={AGENTS} value={agentId} onChange={setAgentId} />
          )}
          {(tweaks.agentSwitcherPosition !== "top" || compareMode) && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600, fontSize: 14 }}>
              {selectedConv?.classified && <IconLock size={14} style={{ color: "var(--danger)" }}/>}
              {selectedConv?.title || (compareMode ? "比較模式" : "新對話")}
            </div>
          )}
          <div style={{ flex: 1 }}/>

          {selectedConv && !compareMode && (
            <>
              <IconButton title={selectedConv.classified ? "解除機密 (admin)" : "設為機密"}
                onClick={() => {
                  if (selectedConv.classified) {
                    alert("機密對話需由 admin 在 CSP 控制面解除。");
                  } else {
                    if (confirm("此對話將設為機密：禁止複製/匯出/分享，且所有操作寫入 audit log。確定？")) {
                      updateConv(selectedConv.id, { classified: true });
                    }
                  }
                }}>
                {selectedConv.classified ? <IconLock size={14} style={{ color: "var(--danger)" }}/> : <IconUnlock size={14}/>}
              </IconButton>
              <IconButton title="分享" onClick={() => {
                if (selectedConv.classified) return alert("機密對話禁止分享");
                setShareOpen(true);
              }}
                disabled={selectedConv.classified}
                style={selectedConv.classified ? { opacity: 0.4, cursor: "not-allowed" } : {}}>
                <IconShare size={14}/>
              </IconButton>
              <Dropdown align="right" width={260} trigger={() => (
                <IconButton title="交接 handoff"><IconNodes size={14}/></IconButton>
              )}>
                {(close) => (
                  <HandoffMenu agents={AGENTS} currentAgentId={agentId}
                    onHandoffAgent={(newAgentId) => {
                      const sysMsg = {
                        id: "sys-" + Date.now(), role: "assistant", text: `[Router] 從 ${agentId} 交接給 ${newAgentId}，繼承上下文。`, streaming: false,
                        routedAgentId: newAgentId, trace: [],
                        confidence: { level: "high", score: 1.0, reasons: ["manual_handoff"] },
                      };
                      setMessagesByConv(prev => ({ ...prev, [selectedConv.id]: [...(prev[selectedConv.id] || []), sysMsg] }));
                      setAgentId(newAgentId);
                    }}
                    onHandoffUser={(u) => alert(`已送出交接請求給 ${u}`)}
                    close={close}/>
                )}
              </Dropdown>
            </>
          )}

          <IconButton title={compareMode ? "退出比較" : "比較模式 (兩個 agent 並排)"}
            onClick={() => compareMode ? exitCompare() : enterCompare()}
            active={compareMode}>
            <IconColumns size={14}/>
          </IconButton>

          {/* API key status chip */}
          <Dropdown align="right" width={320} trigger={() => (
            <button title="CSP API Key" style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "4px 9px",
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: 999, cursor: "pointer",
              color: "var(--fg-muted)", fontSize: 11,
              fontFamily: "var(--font-mono)",
            }}>
              <span style={{ width: 6, height: 6, borderRadius: 999, background: apiKey ? "var(--success)" : "var(--danger)" }}/>
              <IconKey size={12}/>
              <span>{apiKey ? apiKey.slice(0, 7) + "…" + apiKey.slice(-4) : "未設定"}</span>
            </button>
          )}>
            {(close) => <ApiKeyPopover apiKey={apiKey} setApiKey={setApiKey} onClose={close}/>}
          </Dropdown>

          <IconButton title="設定" onClick={() => { setSettingsTab("general"); setSettingsOpen(true); }}>
            <IconSettings/>
          </IconButton>
          <IconButton title={tweaks.dark ? "切換淺色" : "切換深色"}
            onClick={() => {
              const next = { ...tweaks, dark: !tweaks.dark };
              setTweaks(next);
              window.parent.postMessage({ type: "__edit_mode_set_keys", edits: { dark: next.dark } }, "*");
            }}>
            {tweaks.dark ? <IconSun/> : <IconMoon/>}
          </IconButton>
          <IconButton title="Tweaks" onClick={() => setTweaksOpen(o => !o)} active={tweaksOpen}>
            <IconSpark/>
          </IconButton>
        </div>

        {/* content */}
        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
            {compareMode ? (
              <ParallelCompareView
                agents={AGENTS}
                columns={compareColumns}
                setColumns={setCompareColumns}
                messagesByColumn={compareMsgs}
                onSend={sendCompare}
                onExit={exitCompare}
                onAdoptColumn={adoptColumn}
              />
            ) : (
              <>
                <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", background: "var(--bg)" }}>
                  <div style={{
                    maxWidth: 760, margin: "0 auto",
                    padding: `calc(var(--density) * 1.2) var(--density)`,
                  }}>
                    {currentMsgs.length === 0 ? (
                      <EmptyState agent={selectedAgent} onPick={(q) => sendMessage(q, [], {})}/>
                    ) : (
                      currentMsgs.map(m => (
                        <MessageBubble key={m.id} msg={m} agents={AGENTS}
                          conversationId={selectedConvId}
                          classified={isClassified}
                          onRegenerate={() => {}}
                          onOpenCitation={onOpenCitation}
                          onPickFollowUp={(q) => sendMessage(q, [], {})}/>
                      ))
                    )}
                  </div>
                </div>

                {/* composer */}
                <div style={{ padding: `0 var(--density) var(--density)`, background: "var(--bg)" }}>
                  <div style={{ maxWidth: 760, margin: "0 auto" }}>
                    {tweaks.agentSwitcherPosition === "bottom" && (
                      <div style={{ marginBottom: 8, display: "flex", gap: 6, alignItems: "center" }}>
                        <span style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>target:</span>
                        <AgentSelector agents={AGENTS} value={agentId} onChange={setAgentId}/>
                      </div>
                    )}
                    <Composer onSend={sendMessage} agents={AGENTS} />
                    <div style={{
                      marginTop: 6, fontSize: 11,
                      color: "var(--fg-subtle)", textAlign: "center",
                      fontFamily: "var(--font-mono)",
                    }}>
                      ANILA {selectedAgent?.id === "anila-router" ? "會自動分派給合適的 agent" : `→ ${selectedAgent?.name}`}
                      {" · 所有呼叫經 CSP · "}
                      <span style={{ color: "var(--fg-muted)" }}>回應僅供參考</span>
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>

          {citationsOpen && !compareMode && (
            <CitationsDrawer
              open={citationsOpen}
              citations={activeCitations}
              activeId={activeCitationId}
              onClose={() => setCitationsOpen(false)}
              onJumpTo={(c) => window.open(c.source_uri, "_blank")}
            />
          )}
        </div>
      </div>

      <SettingsModal open={settingsOpen} tab={settingsTab} setTab={setSettingsTab}
        onClose={() => setSettingsOpen(false)}
        user={user} apiKey={apiKey} setApiKey={setApiKey} tweaks={tweaks}/>

      <TweaksPanel open={tweaksOpen} onClose={() => setTweaksOpen(false)}
        tweaks={tweaks} setTweaks={setTweaks}/>

      <ShareDialog open={shareOpen} onClose={() => setShareOpen(false)}
        conversation={selectedConv} user={user}/>
    </div>
  );
};

// Empty state
const EmptyState = ({ agent, onPick }) => {
  const suggestions = [
    { title: "特休怎麼計算？", sub: "HR · 規則 + 引用來源",     q: "公司特休怎麼計算？" },
    { title: "出差報銷流程",   sub: "Finance · 4 步流程",      q: "出差報銷流程怎麼走？" },
    { title: "FastAPI SSE",   sub: "Code · streaming proxy", q: "FastAPI 怎麼實作 SSE streaming proxy？" },
    { title: "@vlm 解釋架構圖", sub: "直接指定 vision agent",  q: "@vlm 解釋這張系統架構圖" },
  ];
  return (
    <div style={{ padding: "64px 12px 32px", textAlign: "center" }}>
      <AnilaGlyph size={40} />
      <div style={{ marginTop: 16, fontSize: 22, fontWeight: 600, letterSpacing: -0.2 }}>
        你今天想問 ANILA 什麼？
      </div>
      <div style={{ marginTop: 6, color: "var(--fg-muted)", fontSize: 13 }}>
        {agent?.id === "anila-router"
          ? "輸入問題，Router 會自動分派；也可用 @agent 直接指定"
          : `當前 agent: ${agent?.name}`}
      </div>
      <div style={{
        marginTop: 36, display: "grid",
        gridTemplateColumns: "1fr 1fr", gap: 10,
        maxWidth: 600, margin: "36px auto 0", textAlign: "left",
      }}>
        {suggestions.map((s, i) => (
          <button key={i} onClick={() => onPick(s.q)} style={{
            padding: "12px 14px",
            background: "var(--bg-elev)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            cursor: "pointer", textAlign: "left",
            transition: "all .12s",
          }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--border-strong)"; e.currentTarget.style.transform = "translateY(-1px)"; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border)"; e.currentTarget.style.transform = ""; }}>
            <div style={{ fontSize: 13, fontWeight: 500, color: "var(--fg)" }}>{s.title}</div>
            <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 3 }}>{s.sub}</div>
          </button>
        ))}
      </div>
    </div>
  );
};

const ApiKeyPopover = ({ apiKey, setApiKey, onClose }) => {
  const [val, setVal] = _aUS(apiKey);
  const [show, setShow] = _aUS(false);
  return (
    <div style={{ padding: 10, minWidth: 300 }}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>CSP API Key</div>
      <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 10 }}>
        所有 ANILA 的 agent 呼叫都會帶這把 key。格式 <span style={{ fontFamily: "var(--font-mono)" }}>sk-...</span>
      </div>
      <Input type={show ? "text" : "password"}
        value={val} onChange={e => setVal(e.target.value)}
        leftIcon={<IconKey size={13}/>}
        rightEl={<IconButton onClick={() => setShow(s => !s)}>{show ? <IconEyeOff/> : <IconEye/>}</IconButton>}/>
      <div style={{ display: "flex", gap: 6, marginTop: 10, justifyContent: "flex-end" }}>
        <Button size="sm" onClick={onClose}>取消</Button>
        <Button size="sm" variant="primary" onClick={() => { setApiKey(val); onClose(); }}>儲存</Button>
      </div>
      <div style={{
        marginTop: 10, padding: "7px 9px",
        background: "var(--bg-subtle)", border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        fontSize: 11, color: "var(--fg-muted)", lineHeight: 1.5,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: 999, background: "var(--success)" }}/>
          <span>最近呼叫: 200 OK · 142ms</span>
        </div>
        <div style={{ fontFamily: "var(--font-mono)", marginTop: 3, color: "var(--fg-subtle)" }}>
          今日 usage: 24.3K tokens · 剩餘配額 ∞
        </div>
      </div>
    </div>
  );
};

const SettingsModal = ({ open, tab, setTab, onClose, user, apiKey, setApiKey, tweaks }) => (
  <Modal open={open} onClose={onClose} title="設定" subtitle="runtime 偏好與帳號" width={620}>
    <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 20 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {[
          { id: "general", label: "一般", icon: <IconSettings size={13}/> },
          { id: "apikey",  label: "API Key", icon: <IconKey size={13}/> },
          { id: "privacy", label: "隱私 / 信任", icon: <IconShield size={13}/> },
          { id: "account", label: "帳號",   icon: <IconUser size={13}/> },
          { id: "about",   label: "關於",   icon: <AnilaGlyph size={13}/> },
        ].map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "7px 10px", fontSize: 13,
            background: tab === t.id ? "var(--bg-subtle)" : "transparent",
            border: "1px solid " + (tab === t.id ? "var(--border)" : "transparent"),
            borderRadius: "var(--radius)",
            color: "var(--fg)", textAlign: "left", cursor: "pointer",
          }}>{t.icon}{t.label}</button>
        ))}
      </div>
      <div>
        {tab === "general" && (
          <div style={{ display: "grid", gap: 12 }}>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500 }}>語言</div>
              <select defaultValue="zh-TW" style={{
                padding: "6px 10px", fontSize: 13, marginTop: 6,
                background: "var(--bg-elev)", border: "1px solid var(--border)",
                borderRadius: "var(--radius)", color: "var(--fg)",
              }}>
                <option value="zh-TW">繁體中文</option>
                <option value="zh-CN">简体中文</option>
                <option value="en">English</option>
              </select>
            </div>
            <div>
              <div style={{ fontSize: 13, fontWeight: 500 }}>預設 agent</div>
              <select defaultValue="anila-router" style={{
                padding: "6px 10px", fontSize: 13, marginTop: 6,
                background: "var(--bg-elev)", border: "1px solid var(--border)",
                borderRadius: "var(--radius)", color: "var(--fg)",
              }}>
                {AGENTS.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
          </div>
        )}
        {tab === "apikey" && <ApiKeyTab apiKey={apiKey} setApiKey={setApiKey}/>}
        {tab === "privacy" && (
          <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
            <div>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>偵測到敏感資訊時</div>
              <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 8 }}>
                真正遮罩在 CSP proxy 層執行。此處僅影響 UI 送出前的處理方式。
              </div>
              <div style={{ display: "flex", gap: 4 }}>
                {[
                  { k: "warn", label: "警告" },
                  { k: "mask", label: "自動遮罩（預設）" },
                  { k: "block", label: "阻擋" },
                ].map(o => (
                  <button key={o.k} style={{
                    padding: "6px 10px", fontSize: 12,
                    background: o.k === "mask" ? "var(--accent-soft)" : "var(--bg-elev)",
                    border: "1px solid " + (o.k === "mask" ? "var(--accent)" : "var(--border)"),
                    borderRadius: "var(--radius)", color: "var(--fg)", cursor: "pointer",
                  }}>{o.label}</button>
                ))}
              </div>
            </div>
            <div>
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input type="checkbox" defaultChecked /> 在訊息下方顯示 trace_id / conversation_id
              </label>
              <div style={{ fontSize: 11, color: "var(--fg-muted)", marginLeft: 22, marginTop: 3 }}>
                關閉後，複製資訊時仍會帶 ID（僅影響畫面顯示）
              </div>
            </div>
            <div>
              <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input type="checkbox" defaultChecked /> 低信心回答顯示追問建議
              </label>
            </div>
          </div>
        )}
        {tab === "account" && (
          <div style={{ fontSize: 13 }}>
            <div style={{ marginBottom: 4 }}><b>{user.username}</b></div>
            <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>role: user · 透過本機帳號登入</div>
          </div>
        )}
        {tab === "about" && (
          <div style={{ fontSize: 13, lineHeight: 1.7 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <AnilaGlyph size={24}/>
              <div style={{ fontSize: 16, fontWeight: 600 }}>ANILA Runtime Client</div>
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-muted)" }}>
              v0.2.0 · trust + multi-agent + collab
            </div>
          </div>
        )}
      </div>
    </div>
  </Modal>
);

const ApiKeyTab = ({ apiKey, setApiKey }) => {
  const [val, setVal] = _aUS(apiKey);
  const [show, setShow] = _aUS(false);
  const [saved, setSaved] = _aUS(false);
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 500 }}>CSP API Key</div>
        <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 6 }}>
          ANILA 使用 CSP 發的 API Key 做 runtime 呼叫。
        </div>
        <Input type={show ? "text" : "password"}
          value={val} onChange={e => { setVal(e.target.value); setSaved(false); }}
          leftIcon={<IconKey size={13}/>}
          rightEl={<IconButton onClick={() => setShow(s => !s)}>{show ? <IconEyeOff/> : <IconEye/>}</IconButton>}/>
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <Button size="sm" variant="primary" onClick={() => { setApiKey(val); setSaved(true); }}>儲存</Button>
          {saved && <span style={{ fontSize: 11, color: "var(--success)", alignSelf: "center" }}>✓ 已儲存</span>}
        </div>
      </div>
    </div>
  );
};

// ---- Root App ----
const App = () => {
  const [loggedIn, setLoggedIn] = _aUS(false);
  const [user, setUser] = _aUS({ username: "" });
  const [apiKey, setApiKey] = _aUS(
    localStorage.getItem("anila.apikey") || "sk-anila-demo-0000000000000000000000"
  );
  const [tweaks, setTweaks] = _aUS(window.ANILA_TWEAKS);
  const [tweaksOpen, setTweaksOpen] = _aUS(false);

  _aUE(() => { applyTweaks(tweaks); }, [tweaks]);
  _aUE(() => { localStorage.setItem("anila.apikey", apiKey); }, [apiKey]);

  _aUE(() => {
    const u = localStorage.getItem("anila.user");
    if (u) { try { setUser(JSON.parse(u)); setLoggedIn(true); } catch {} }
  }, []);

  _aUE(() => {
    const handler = (e) => {
      if (e.data?.type === "__activate_edit_mode")   setTweaksOpen(true);
      if (e.data?.type === "__deactivate_edit_mode") setTweaksOpen(false);
    };
    window.addEventListener("message", handler);
    window.parent.postMessage({ type: "__edit_mode_available" }, "*");
    return () => window.removeEventListener("message", handler);
  }, []);

  const login = ({ username, apiKey: k }) => {
    setUser({ username });
    setApiKey(k);
    localStorage.setItem("anila.user", JSON.stringify({ username }));
    setLoggedIn(true);
  };
  const logout = () => {
    localStorage.removeItem("anila.user");
    setLoggedIn(false);
    setUser({ username: "" });
  };

  if (!loggedIn) return <LoginView onLogin={login} />;
  return (
    <ChatRuntime user={user} apiKey={apiKey} setApiKey={setApiKey} onLogout={logout}
      tweaks={tweaks} setTweaks={setTweaks}
      tweaksOpen={tweaksOpen} setTweaksOpen={setTweaksOpen}/>
  );
};

const _style = document.createElement("style");
_style.textContent = `@keyframes anila-blink { 50% { opacity: 0; } }`;
document.head.appendChild(_style);

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
