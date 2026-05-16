# AgenticRAG enhancement plan (anila-core untouched)

> **Status**: design draft, awaiting review
> **Date**: 2026-05-02
> **Constraint**: 整批工作住在 `AgenticRAG/src/agentic_rag/` 子樹內，**不動 `anila-core/`**。換言之：避開所有 deep-dive P0 表上需要 anila-core framework 先就位的 R-side 工作（lifecycle hooks framework、guardrail framework、tracing framework、handoff framework、MCP framework、retry framework）。

## 為什麼這條路徑

- AgenticRAG 自己 own 的 surface（retrieval pipeline / prompts / tools / preprocessor / reranker）已經夠大，**RAG-quality 還有大量低垂果實**
- 真正會回頭重做的東西不要兩次：handoff / guardrail / tracing 這些 fleet-wide framework 該等 anila-core 一起 ship
- 本 plan 7 個項目**完全獨立**，沒有 framework dependency，每個 1–2 天可上線

## P0 enhancement 表（全 R-side、無 anila-core 依賴）

| # | 強化點 | 預估 LOC | 為什麼值得做 |
|---|---|---|---|
| 1 | **Query rewriting**（multi-query expansion + HyDE） | ~150 | 業界普遍 retrieval recall@k 提 15–30%；最大 cost-benefit ratio |
| 2 | **Self-RAG / reflection loop**（answer 完判斷是否 grounded，不行就 refine query 重來） | ~200 | hallucination 殺手；citation 驗證的延伸 |
| 3 | **Citation enforcement post-processor**（每個 claim 必有 citation，否則重試或抛錯） | ~80 | RAG 的 trust 基石；當前完全沒有 |
| 4 | **In-pipeline RAG guardrails**（pre-retrieval / post-retrieval / pre-answer 三點 ad-hoc check） | ~100 | 不用 framework，純 function check；比等 anila-core framework 早 ship 8 週 |
| 5 | **Multi-step query decomposition**（複雜 query → sub-queries → 各自 retrieval → 整合答案） | ~250 | 法律 / 研究類 long-form question 必需；多 hop |
| 6 | **Reranker cascade**（cross-encoder 失敗 / score 過低時降到 keyword-only RRF） | ~80 | resilience；reranker 服務 unavailable 時不雪崩 |
| 7 | **Retrieval result audit log**（trace_id 串起 query / chunks / scores / final answer，寫進 ingestion_eval_runs 供 EvaluatorView 用） | ~120 | quality debug 起點；不需要 anila-core tracing framework |

**總**：~980 LOC、估 ~7–10 天工作量。

不在這個 plan 的 deep-dive R-side 工作（**等 anila-core phase 上來**）：
- 1' RAG-specific lifecycle hooks ← 等 anila-core lifecycle framework
- 2' RAG guardrails as Guardrail class instances ← #4 先用 ad-hoc function 模式撐著，待 framework 到位再升級
- 3' RAG span data types ← 等 anila-core tracing framework；#7 用簡化的 trace_id + dict 替代
- 4' RAG handoff sub-agents (RetrievalAgent / AnswerAgent / VerifierAgent) ← 等 anila-core handoff framework；#5 用「multi-step pipeline as functions」替代
- 6' MCP yaml parsing ← 等 anila-core MCP framework
- 7' RAG retry advice ← 等 anila-core retry framework

---

## 細節 — 每條強化怎麼做

### 1 · Query rewriting（multi-query + HyDE）

**問題**：使用者 query 通常 suboptimal — 要嘛太短（「第八條」），要嘛多義（「請假要怎麼辦？」），要嘛跟 corpus 用詞不對齊（user 說「年假」vs 公文寫「特休假」）。直接 embedding search 命中率不高。

**做法**：在 retrieval 之前插一段 query rewriting：

- **Multi-query**：LLM 把 user query 改寫成 3 個變體（同義替換、不同切角度、加上 context guess），對每個都跑 retrieval，最後 RRF fusion
- **HyDE**：LLM 寫一段「假想答案」，用該答案 embedding 去搜（典型答案的 embedding 通常比 query embedding 跟真正的 chunk 接近）

**架構**：
```
agentic_rag/engine/query_rewriter.py
├── generate_multi_queries(query, n=3, llm) -> list[str]
├── generate_hypothetical_answer(query, llm) -> str
└── rewrite_pipeline(query, mode='multi_query'|'hyde'|'both', llm) -> list[str]
```

**整合點**：`engine/rag_preprocessor.py` 的 `_extract_latest_query` 之後、實際呼 vector_search 之前。配 `chunking_config` 同層級的 `retrieval_config` 開關（default off）。

**驗證**：用既有 `EvaluatorView` 對同樣 corpus 跑 baseline / multi-query / HyDE 三組，看 recall@5 / answer correctness 改善幅度。

**LOC**：~150（含 prompts 跟 RRF 合併邏輯）

### 2 · Self-RAG / reflection loop

**問題**：LLM 拿到 chunks 直接生答案，沒有 verify「答案是否真的 grounded 在 chunks」。Hallucination 來源。

**做法**：answer 出來後加一輪 reflection：

```
1. retrieval → answer (current behavior)
2. reflection LLM call: given (query, chunks, answer), score:
   - groundedness: 0-3 (3 = every claim cited)
   - completeness: 0-3 (3 = answers all aspects)
   - relevance: 0-3 (3 = on topic)
3. If score < threshold:
   - refine query based on reflection's gap analysis
   - back to step 1 (max 2 reflection rounds)
4. Else return answer
```

**架構**：
```
agentic_rag/engine/reflection.py
├── ReflectionScore(groundedness, completeness, relevance, gap_analysis)
├── reflect(query, chunks, answer, llm) -> ReflectionScore
└── reflect_loop(query, chunks, answer, llm, max_rounds=2) -> answer
```

**整合點**：`/agentic-chat` 端點的 final answer 出來後、回 user 前。`reflection_config: { enabled: bool, max_rounds: int, threshold: int }` 在 `chunking_config` 同層級。

**驗證**：sample 一份 RAG benchmark dataset（HotPotQA 或自家 corpus）；reflection on/off 對 hallucination rate 的影響。

**LOC**：~200（含 prompt + retry logic）

### 3 · Citation enforcement post-processor

**問題**：current AgenticRAG 的 answer 有 citation 是因為 prompt 寫得好；沒有硬性檢查。如果 LLM 偷懶不附 citation，就放行。

**做法**：純後處理，answer 出來後 parse：

```
1. parse answer → extract claims (LLM-based 或 sentence-level)
2. for each claim, check if there's a citation marker (e.g. [1] or [chunk-ID])
3. if any claim missing citation:
   - mode A (strict): raise + force regenerate
   - mode B (lenient): annotate "warning: claim X has no source"
   - mode C (auto-fix): re-run with prompt "add citation for: <claim>"
```

**架構**：
```
agentic_rag/engine/citation_check.py
├── extract_claims(answer) -> list[Claim]
├── verify_citations(claims, chunks) -> list[CitationGap]
└── enforce(answer, chunks, mode='strict'|'lenient'|'auto_fix', llm) -> answer
```

**整合點**：`/chat` 跟 `/agentic-chat` 的 final answer 出來後（reflection 之後或之前都可，獨立模組）。

**LOC**：~80（用 regex / sentence split 抽 claim 即可，不必另跑 LLM）

### 4 · In-pipeline RAG guardrails（ad-hoc function 版）

**問題**：當 anila-core 的 guardrail framework 還沒上來，AgenticRAG 完全沒有 query / retrieval / answer 三點的 safety check。

**做法**：寫 6 個純 function 嵌進 pipeline：

| 位置 | guardrail | 行為 |
|---|---|---|
| pre-retrieval | `query_too_short` | < 5 chars → "請補充查詢內容" |
| pre-retrieval | `query_contains_pii` | regex match 身分證 / 信用卡 / 電話 → "請去除個資後重試" |
| post-retrieval | `no_relevant_chunks` | 全部 chunks score < 0.3 → "找不到相關內容" 給 LLM 看，避免 hallucinate |
| post-retrieval | `chunk_count_outlier` | 命中數量極端高（> 50） → 警告 reranker 失效 |
| pre-answer | `answer_request_in_scope` | LLM 已決定要回但內容跟 retrieval 主題偏離 → log 警告 |
| post-answer | `answer_too_short` | answer < 30 chars → 重試一次 |

每個 guardrail 是 pure Python function，回 `(allow: bool, message: str)`。直接呼叫，不用 framework。

**架構**：
```
agentic_rag/engine/guardrails.py
├── @dataclass GuardrailResult: allow, message, suggestion
├── pre_retrieval: list[Callable]
├── post_retrieval: list[Callable]
├── pre_answer: list[Callable]
├── post_answer: list[Callable]
└── run_guardrails(stage, ...args) -> GuardrailResult | None
```

**等 anila-core framework 上線**：把這些 function 包進 `InputGuardrail` / `ToolOutputGuardrail` 是純 mechanical wrapping，半天工作。

**LOC**：~100

### 5 · Multi-step query decomposition

**問題**：複雜 query 譬如「比較 X 跟 Y 的差異並說明適用場景」，single-shot retrieval + answer 沒辦法處理（X 跟 Y 的內容在不同 section / 不同 doc）。

**做法**：query decomposition pipeline：

```
1. classifier LLM: is this query "atomic" or "compound"?
2. if compound:
   a. decomposition LLM: split into sub-queries
   b. for each sub-query: full retrieval + partial answer
   c. synthesis LLM: merge partial answers + de-dupe + cite
3. else atomic: current single-shot path
```

**架構**：
```
agentic_rag/engine/decomposition.py
├── classify_query(query, llm) -> Literal["atomic", "compound"]
├── decompose_query(query, llm) -> list[SubQuery]
└── orchestrate(query, llm, retrieval_fn) -> answer
```

**整合點**：`/agentic-chat` 的 query 進來後第一站。compound 走 multi-step path、atomic 走原本 path。可在 `chunking_config` 同層級加 `decomposition_config: {enabled: bool}` 開關。

**為什麼不用 handoff framework**：function-based 多步驟 = 在單一 RAG agent 內用 internal helper functions 處理。Handoff framework 是「control transfer between top-level agents」這個 RAG 問題不需要那層抽象。等 anila-core framework 上來之後，重構成 `RetrievalAgent.handoff_to(SynthesisAgent)` 是純 wrapping。

**LOC**：~250（含 3 個 LLM call + prompts）

### 6 · Reranker cascade

**問題**：current AgenticRAG 用 mxbai cross-encoder reranker。模型服務 down / 載入 OOM 時，整個 retrieval 崩。

**做法**：cascade fallback：

```
1. try cross-encoder rerank
2. if exception or top_score < min_threshold:
   → fallback to BM25 / keyword score
3. if STILL fail:
   → fallback to vector score only (no rerank)
```

**架構**：
```
agentic_rag/providers/reranker.py
├── ABC Reranker.rerank(query, chunks) -> list[Chunk]
├── CrossEncoderReranker (existing)
├── BM25Reranker (new fallback layer)
└── CascadingReranker(primary, fallbacks) -> auto-selected
```

**LOC**：~80

### 7 · Retrieval result audit log

**問題**：debug RAG quality issue 時看不到 query / chunks / scores / answer 的時序紀錄。當前只有 LLM token usage 寫進 token_usage 表。

**做法**：每次 `/chat` / `/agentic-chat` 寫一個 audit row，**不依賴 anila-core tracing framework**：

```
ingestion_eval_runs 表（既有）加一個 row 但 type='retrieval_audit':
  query: str
  rewritten_queries: list[str]   # 來自 #1 query rewriter
  chunks: list[{id, score, content_preview}]
  reflection_score: dict          # 來自 #2 reflection
  final_answer: str
  trace_id: uuid
  duration_ms: int
```

EvaluatorView 改一條 query 串起來：給 trace_id 顯示完整 retrieval flow。

**LOC**：~120（含 schema 微調 + frontend 一個 detail view）

**等 anila-core tracing framework 上來**：替換成 structured span tree。但 audit log table 留著當 long-term storage（trace 通常短期，audit log 長期）。

---

## 順序建議

1. **Week 1**：#3 citation + #4 guardrails（最便宜，2-3 天可全部 ship；建立 RAG quality 防線）
2. **Week 1**：#7 audit log（純加表寫資料，0 risk，當 baseline 對照組）
3. **Week 2**：#1 query rewriting（最大 quality lift，3-4 天）
4. **Week 2-3**：#6 reranker cascade（resilience，1 天）
5. **Week 3**：#2 self-RAG reflection（hallucination killer，4-5 天，需要 #1 的 baseline 對比驗證）
6. **Week 4**：#5 query decomposition（最複雜，需要前面的工具當底；5 天）

---

## 怎麼跟 anila-core phase 整合

當 anila-core 的 framework 後續 phase 上線時，**本 plan 寫的東西不會丟**：

| AgenticRAG 寫的 | 升級後變什麼 |
|---|---|
| `engine/guardrails.py` 純 function | 包成 anila-core `InputGuardrail` / `ToolGuardrail` 實例 |
| `engine/citation_check.py` post-processor | 變成 anila-core `OutputGuardrail` |
| `engine/reflection.py` 顯式 loop | 變成 anila-core handoff（refine_agent → retrieve_agent）|
| `engine/decomposition.py` function pipeline | 變成 anila-core handoff（decompose_agent → retrieval_agent x N → synthesis_agent）|
| `query_rewriter.py` | 留著當「retrieval-side enhancement」，不需要升級 |
| `reranker_cascade.py` | 留著當「provider-side enhancement」，不需要升級 |
| audit log 寫 ingestion_eval_runs | 升級成 anila-core trace 寫入；audit log 表變成 derived 從 trace generate |

**核心 insight**：先用 ad-hoc function 模式跑通 RAG 流程，等 framework 上來再 wrap。比反過來省工。

---

## 不該做的東西

跟 deep-dive P2/⚫ 列表對齊：

- ❌ 寫 RAG-specific 的 lifecycle hooks framework — 等 anila-core 1
- ❌ 在 AgenticRAG 內自寫 handoff abstraction — 等 anila-core 4
- ❌ 在 AgenticRAG 內自寫 tracing span tree — 等 anila-core 3
- ❌ 在 AgenticRAG 內自寫 MCP server integration — 等 anila-core 6
- ❌ Sandbox tool execution — 短期不需要

這些做下去都會在 anila-core phase 上來時被 ripped out + rewritten。**省一次重做**。

---

**Last updated**: 2026-05-02 · **Constraint**: 不動 anila-core；R-side 工作獨立可 ship · **Total LOC**: ~980 / ~7–10 working days
