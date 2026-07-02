// Mock data — agents, conversations, messages (with trust/meta extensions)

const AGENTS = [
  {
    id: "anila-router",
    name: "ANILA Router",
    short: "auto",
    description: "自動路由：根據你的問題，由 Router 決定直接回答或分派給合適的 agent。",
    tag: "官方",
    color: "var(--accent)",
    capabilities: ["auto-dispatch", "multi-agent", "streaming"],
  },
  {
    id: "rag-agent",
    name: "Knowledge RAG",
    short: "rag",
    description: "通用知識檢索 agent。針對內部政策、文件、FAQ 做語意檢索與摘要。",
    tag: "knowledge",
    capabilities: ["vector-search", "citation"],
  },
  {
    id: "code-assist",
    name: "Code Assist",
    short: "code",
    description: "程式碼解釋、debug、重構建議。支援 Python / TypeScript / Go。",
    tag: "dev",
    capabilities: ["code", "debug"],
  },
  {
    id: "hr-policy",
    name: "HR Policy",
    short: "hr",
    description: "人資規定與流程諮詢。請假、出差、簽核常見問題。",
    tag: "dept",
    capabilities: ["policy", "faq"],
  },
  {
    id: "finance-qa",
    name: "Finance QA",
    short: "fin",
    description: "財務相關查詢：報銷、預算、會計科目說明。",
    tag: "dept",
    capabilities: ["policy", "calc"],
  },
  {
    id: "vision-multi",
    name: "Vision Multi",
    short: "vlm",
    description: "多模態視覺模型。可分析上傳的圖片、截圖、圖表。",
    tag: "multimodal",
    capabilities: ["vision", "ocr"],
  },
];

const FOLDERS = [
  { id: "all",       name: "全部", icon: "inbox" },
  { id: "starred",   name: "已加星", icon: "star" },
  { id: "hr",        name: "HR",       icon: "folder" },
  { id: "finance",   name: "Finance",  icon: "folder" },
  { id: "engineering", name: "Engineering", icon: "folder" },
];

const INITIAL_CONVERSATIONS = [
  { id: "c1", title: "Q3 財報重點摘要", ts: "2 小時前", agent: "rag-agent", folder: "finance", tags: ["財報", "Q3"], starred: true,  classified: false },
  { id: "c2", title: "FastAPI SSE streaming 實作", ts: "昨天", agent: "code-assist", folder: "engineering", tags: ["python", "streaming"], starred: false, classified: false },
  { id: "c3", title: "特休計算方式", ts: "昨天", agent: "hr-policy", folder: "hr", tags: ["特休"], starred: false, classified: false },
  { id: "c4", title: "出差報銷流程", ts: "3 天前", agent: "finance-qa", folder: "finance", tags: ["出差"], starred: false, classified: false },
  { id: "c5", title: "解釋這張架構圖", ts: "上週", agent: "vision-multi", folder: "engineering", tags: ["架構"], starred: false, classified: false },
  { id: "c6", title: "SQL index 優化建議", ts: "上週", agent: "code-assist", folder: "engineering", tags: ["sql", "performance"], starred: false, classified: false },
  { id: "c7", title: "2026 併購案評估", ts: "2 週前", agent: "rag-agent", folder: "all", tags: ["M&A"], starred: false, classified: true },
];

// ---- Mock Citations for RAG responses ----
const MOCK_CITATIONS_RAG = [
  {
    id: "cit-1",
    title: "員工手冊 v2.3",
    section: "第 4 章 · 4.7 特別休假",
    snippet: "到職滿 6 個月未滿 1 年者，給予 3 日特別休假。滿 1 年以上未滿 2 年者，給予 7 日。本公司另加給：滿 3 年起每年再額外 +2 日。",
    source_uri: "doc://hr-handbook/v2.3#ch4-7",
    updated_at: "2026-03-15",
    score: 0.91,
  },
  {
    id: "cit-2",
    title: "勞動基準法",
    section: "第 38 條（特別休假）",
    snippet: "勞工在同一雇主或事業單位，繼續工作滿一定期間者，應依規定給予特別休假：六個月以上一年未滿者，三日...",
    source_uri: "gov://lsa/art-38",
    updated_at: "2024-11-02",
    score: 0.84,
  },
  {
    id: "cit-3",
    title: "人資 FAQ",
    section: "Q14 特休未休怎麼辦？",
    snippet: "未休完的特休可於年度結束後選擇折現或遞延至次年 3 月；折現率依當月平均工資計算。",
    source_uri: "doc://hr-faq/q14",
    updated_at: "2026-01-08",
    score: 0.77,
  },
];

const MOCK_CITATIONS_FIN = [
  {
    id: "cit-1",
    title: "財務處 SOP 手冊",
    section: "出差報銷 · 第 3 節",
    snippet: "出差前於 EIP 送簽核單（TR-form）並附預估金額；返回後 7 個工作日內於報銷系統上傳憑證。",
    source_uri: "doc://finance-sop/travel",
    updated_at: "2026-02-20",
    score: 0.88,
  },
  {
    id: "cit-2",
    title: "會計作業規範 2026",
    section: "§5.2 電子發票格式",
    snippet: "B2B 電子發票須包含統一編號、發票號碼、品項明細；影像檔不得替代正本。",
    source_uri: "doc://accounting/2026/5-2",
    updated_at: "2026-01-30",
    score: 0.72,
  },
];

// Fake agent response generator
function generateFakeResponse(userText, agentId) {
  const lower = userText.toLowerCase();
  let routed = agentId;
  let handoffChain = null;

  if (agentId === "anila-router") {
    if (/code|程式|python|debug|sql|bug|錯誤/.test(lower)) routed = "code-assist";
    else if (/財報|報銷|預算|發票|費用/.test(lower)) routed = "finance-qa";
    else if (/請假|特休|加班|簽核|薪/.test(lower)) routed = "hr-policy";
    else if (/圖|照片|image|screenshot/.test(lower)) routed = "vision-multi";
    else routed = "rag-agent";

    // Demo: 請假 / 特休 類問題觸發多 agent handoff（HR → Finance）
    if (/特休|請假/.test(lower)) {
      handoffChain = [
        { agent_id: "anila-router", label: "Router 分類", latency_ms: 42, status: "ok",
          input_summary: "特休計算方式", output_summary: "判定為 HR 類，分派 hr-policy" },
        { agent_id: "hr-policy",   label: "HR Policy 查詢", latency_ms: 830, status: "ok",
          input_summary: "特休天數如何計算", output_summary: "取得規則 + 未休轉換" },
        { agent_id: "finance-qa",  label: "Finance 折現率", latency_ms: 1230, status: "ok",
          input_summary: "未休特休折現率", output_summary: "依當月平均工資" },
      ];
    }
  }

  const trace = [
    { kind: "thinking", label: "Router 分析意圖中", detail: "解析 query: " + userText.slice(0, 40) + (userText.length > 40 ? "…" : "") },
    { kind: "dispatch", label: "選擇 agent", detail: `dispatch_to_agent("${routed}")` },
    { kind: "call",     label: `呼叫 ${routed}`, detail: "POST /v1/chat/completions (經 CSP proxy)" },
    { kind: "stream",   label: "接收 streaming 回應", detail: "SSE chunk…" },
  ];

  let body = "";
  let citations = [];
  let confidence = { level: "high", score: 0.88, reasons: ["doc_coverage_full"] };
  let followUps = [];

  if (routed === "code-assist") {
    body = `好的，這個問題的核心在於 async streaming 的處理方式。以下是建議做法：

使用 \`httpx.AsyncClient.stream()\` 搭配 FastAPI 的 \`StreamingResponse\`，可以做到真正的逐 chunk 轉發，避免整個 response 先在 memory 堆完再一次吐出。

關鍵點：
1. 下游請求務必帶 \`stream_options={"include_usage": true}\`，否則最後一個 chunk 不會有 usage。
2. 攔截最後一個 chunk 把 usage 寫回 \`token_usage\` 表。
3. 若下游未回 usage，fallback 用 tiktoken 估算 input / output tokens。

這樣你的 proxy 層就能在不阻塞 streaming 的前提下正確結算計費。`;
    confidence = { level: "high", score: 0.92, reasons: ["code_pattern_matched"] };
  } else if (routed === "finance-qa") {
    body = `根據財務處最新規定（2026 Q1 版）[1]：

出差報銷流程大致分四步：
1. 出差前在 EIP 送簽核單（TR-form）並附預估金額
2. 出差中保留正式發票或收據（電子發票需為 B2B 格式[2]）
3. 返回後 7 個工作日內於報銷系統上傳憑證
4. 主管審核後由財務撥款，通常 3–5 個工作日入帳

若超過 7 日未報銷，系統會自動鎖單，需另請主管補簽。`;
    citations = MOCK_CITATIONS_FIN;
    confidence = { level: "high", score: 0.86, reasons: ["doc_coverage_full"] };
    followUps = ["超過 7 日怎麼補救？", "出國出差匯率如何處理？"];
  } else if (routed === "hr-policy") {
    body = `特休計算規則（依勞基法第 38 條 [2]）：

- 到職滿 6 個月未滿 1 年：3 日
- 1 年以上未滿 2 年：7 日
- 2 年以上未滿 3 年：10 日
- 3 年以上未滿 5 年：每年 14 日
- 5 年以上未滿 10 年：每年 15 日
- 10 年以上：每滿 1 年加 1 日，最高 30 日

本公司另加給：滿 3 年起每年再額外 +2 日（公司福利 [1]）。未休完的特休可在年度結束後選擇折現或遞延至次年 3 月 [3]。`;
    citations = MOCK_CITATIONS_RAG;
    confidence = { level: "high", score: 0.89, reasons: ["rule_matched", "doc_coverage_full"] };
    followUps = ["半天假怎麼計算？", "離職時未休特休怎麼處理？"];
  } else if (routed === "vision-multi") {
    body = `從你上傳的圖片看，這是一個典型的 three-tier 架構：

- 上層：client（瀏覽器 / mobile）透過 HTTPS 進入
- 中層：API Gateway + 微服務群（看起來是 FastAPI + gRPC 混合）
- 底層：PostgreSQL 主從 + Redis cache

值得注意的是中層缺少一個獨立的 message queue，若未來要支援 async job 建議補上 RabbitMQ 或 Redis Streams 以解耦長任務。`;
    confidence = { level: "medium", score: 0.64, reasons: ["image_partial_occlusion", "text_ambiguity"] };
    followUps = ["你是指整體架構，還是只問 API Gateway？", "需要我估算每層的 QPS 嗎？"];
  } else {
    body = `根據檢索到的內部文件（共 3 筆相關片段）[1][3]：

這個問題的關鍵在於我們在 v7 計畫中明確定下「CSP = Data plane」的原則 — 所有 LLM / agent 流量一律經 CSP proxy，usage 在 CSP 一次結算。Router 與 agent 不持有 upstream key，只持有 CSP 發的 Key [2]。

實務上這代表：
- Router 呼叫主 LLM → 經 CSP（第一筆 usage）
- Router dispatch agent → 經 CSP，model 欄位帶 agent ID（第二筆 usage）
- Agent 內部若再呼叫 LLM → 也回經 CSP（第三筆 usage）

三筆 token_usage 都 attribution 到同一個 user，就是 data plane 決策的實質含義。`;
    citations = MOCK_CITATIONS_RAG.slice(0, 3).map((c, i) => ({ ...c, id: `cit-${i + 1}` }));
    confidence = { level: "medium", score: 0.58, reasons: ["doc_coverage_partial", "entity_ambiguity"] };
    followUps = ["你是指 2025 版還是 2026 版規定？", "要我列出每層的 token 計費明細嗎？"];
  }

  // Generate audit IDs
  const traceId = "tr_" + Math.random().toString(16).slice(2, 10);
  const latencyMs = Math.floor(80 + Math.random() * 180);

  return { routed, trace, body, citations, confidence, followUps, handoffChain, traceId, latencyMs };
}

// ---- PII detection patterns (front-end UX only; real redaction at CSP proxy) ----
const PII_PATTERNS = [
  { kind: "id",      label: "身分證",     regex: /\b[A-Z]\d{9}\b/g },
  { kind: "phone",   label: "電話",       regex: /\b09\d{2}-?\d{3}-?\d{3}\b/g },
  { kind: "email",   label: "Email",      regex: /[\w.+-]+@[\w-]+\.[\w.-]+/g },
  { kind: "card",    label: "信用卡",     regex: /\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b/g },
];

function detectPII(text) {
  if (!text) return [];
  const hits = [];
  PII_PATTERNS.forEach(p => {
    // reset regex state
    p.regex.lastIndex = 0;
    let m;
    while ((m = p.regex.exec(text)) !== null) {
      hits.push({ kind: p.kind, label: p.label, value: m[0], index: m.index });
    }
  });
  return hits.sort((a, b) => a.index - b.index);
}

function maskPII(value, kind) {
  if (!value) return value;
  if (kind === "email") {
    const [u, d] = value.split("@");
    return u.slice(0, 2) + "***@" + d;
  }
  if (kind === "phone") {
    return value.replace(/(\d{2,4})[^\d]?(\d{3})[^\d]?(\d{3,4})/, "$1-***-$3");
  }
  if (kind === "id") return value.slice(0, 1) + "****" + value.slice(-3);
  if (kind === "card") return "****-****-****-" + value.slice(-4);
  return "[REDACTED]";
}

// Render user text with PII redacted (returns array of React nodes)
function renderWithRedaction(text, hits) {
  if (!hits || hits.length === 0) return text;
  const parts = [];
  let cursor = 0;
  hits.forEach((h, i) => {
    if (h.index > cursor) parts.push(text.slice(cursor, h.index));
    parts.push({ __redacted: true, kind: h.kind, label: h.label, masked: maskPII(h.value, h.kind), idx: i });
    cursor = h.index + h.value.length;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

// Split text into realistic typewriter chunks (2-8 chars)
function chunkText(text) {
  const chunks = [];
  let i = 0;
  while (i < text.length) {
    const len = Math.floor(Math.random() * 6) + 2;
    chunks.push(text.slice(i, i + len));
    i += len;
  }
  return chunks;
}

Object.assign(window, {
  AGENTS, INITIAL_CONVERSATIONS, FOLDERS,
  generateFakeResponse, chunkText,
  detectPII, maskPII, renderWithRedaction,
  MOCK_CITATIONS_RAG, MOCK_CITATIONS_FIN,
});
