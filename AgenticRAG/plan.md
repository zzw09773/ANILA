# ANILA Core — 評估報告（2026-04-07 更新）

> 基於 `agentic-rag-single-model-design.md` 的差距分析，
> 並納入已完成的實作與 gemma4 FC 實測結果。

---

## 一、已完成的實作（本期）

| 功能 | 說明 |
|---|---|
| `api.py` Port 24786 | OpenWebUI 相容的 RAG proxy，pre-process injection |
| RAG trace（thinking block） | 每次回覆前在思考塊顯示「🔍 RAG 檢索結果」及相似度 |
| 來源附記 | 回覆末尾自動附上 `**參考來源：**` 清單 |
| `delta.reasoning` → `delta.reasoning_content` | 讓 OpenWebUI 正確顯示 gemma4 的思考內容 |
| `index_documents.py` | 批次索引 `data/documents/` 下所有文件 |
| Docker Compose 三服務 | `api`（8000）、`rag-api`（24786）、`db`（pgvector） |

---

## 二、gemma4 Function Calling 實測結果

**測試日期：2026-04-07**
**測試方式：** 完整 multi-turn FC loop，工具包含 `vector_search` + `assess_quality`，查詢真實 pgvector

### 結果

| 指標 | 結果 |
|---|---|
| FC 基礎支援 | ✅ `finish_reason=tool_calls`，正確輸出 `tool_calls` 結構 |
| 多輪自主搜尋 | ✅ 3 輪搜尋，每輪自動改寫查詢關鍵字 |
| 自主停止判斷 | ✅ 不依賴 `assess_quality`，自己判斷資料充足後 `finish_reason=stop` |
| 答案品質 | ✅ 正確引用第 23 條，說明記過 3 次視為記大過等具體規定 |
| JSON Schema 限制 | ⚠️ 參數型別只接受 `"number"`，不接受 `"integer"`（422 錯誤） |
| `assess_quality` 工具使用率 | ⚠️ 模型傾向跳過，直接自行評估充足性 |

### 結論

**gemma4 的 FC 能力完整，tool-driven Agentic RAG 技術上可行。**
`plan.md` 原先將「FC adapter」列為最高風險項，經實測可降級。

---

## 三、架構符合度（更新版）

### ✅ 已完整符合

| 設計文件要求 | 現有實作 |
|---|---|
| §2 LLMProvider 抽象 | `providers/base.py` → `Provider` Protocol |
| §2 OpenAI-Compatible Provider | `providers/openai_compat.py` → stream + tool calls |
| §2 ModelConfig（換模型改一行） | `config.py` + `.env MODEL=xxx` |
| §2 Embedding 獨立配置 | `config.py` EMBEDDING_* 系列 |
| §3 Tool 定義 | `models/tool.py` → `ToolDefinition` |
| §3 Tool Router | `router/tool_router.py` → `ToolRegistry` |
| §5 Layer 1 截斷舊 tool 結果 | `compact/micro_compact.py` |
| §5 Layer 2 LLM 摘要壓縮 | `compact/auto_compact.py` + `session_memory.py` |
| §6 Sub-Agent fork | `coordinator/coordinator.py` — 比設計更完整 |
| §8 全部解析器 | txt / md / pdf / docx / doc / odt |
| §8 Chunking | `ingestion/chunker.py` → `RecursiveTextSplitter` |
| §8 攝取服務 | `ingestion/service.py` |
| §11 向量存儲 | `storage/adapters/pgvector_store.py` + pgvector |
| §11 Embedding | `providers/embedding_nvidia.py` (NV-Embed-V2) |
| API（文件/搜尋/對話） | documents.py / search.py / server.py |
| Auth 中介層 | `api/middleware/auth.py` |
| RAG pre-process | `engine/rag_preprocessor.py` |
| OpenWebUI RAG proxy | `api.py` Port 24786（本期完成） |

### ⚠️ 部分符合

| 差異 | 說明 | 影響 |
|---|---|---|
| §3 Agentic RAG ReAct 循環 | 目前 `api.py` 是 pre-process，`/chat` 尚未掛 tool-driven loop | 🔴 核心架構差異（但有明確改善路徑） |
| §5 Layer 3 滑動窗口 | 缺少硬截斷 fallback | 🟡 長對話可能 OOM |
| §8 per-format chunking | 統一用 RecursiveTextSplitter，未按格式專屬切割 | 🟡 精細度不足 |
| §2 非流式 `chat()` | 只有 `stream_completion()`，無一次性回傳 | 🟢 可用 stream 模擬 |

### ❌ 尚未實作

| 項目 | 影響 | 備註 |
|---|---|---|
| **VectorSearchTool**（LLM 可呼叫） | 🔴 | tool-driven RAG 的核心，已有 pgvector 可直接包裝 |
| **KeywordSearchTool（BM25）** | 🟡 | 與向量搜尋互補，精確詞匹配 |
| **ReadDocumentTool** | 🟡 | LLM 看摘要後想看全文 |
| **AgenticRAG ReAct loop on `/chat`** | 🔴 | 讓 `/chat` 端點也走 tool-driven 路徑 |
| **FunctionCallingAdapter** | 🟡 | 統一 FC vs 模擬 FC 介面（gemma4 支援 FC，短期次要）|
| **DeepResearchTool** | 🟡 | 啟動子 agent 多輪深度調研 |
| **PPTX 解析器** | 🟢 | 可選 |
| **Code 解析器** | 🟢 | 可選 |
| **ChunkingConfig dataclass** | 🟢 | 次要，現有 env 已夠用 |
| **System Prompt 設定入口（api.py）** | 🟡 | api.py 目前無 system prompt，影響回覆風格與引用格式 |

---

## 四、核心架構差距說明

### 兩種 RAG 模式對比

| 面向 | Pre-process Injection（現有 api.py） | Tool-driven AgenticRAG（設計目標） |
|---|---|---|
| 檢索觸發 | 每次對話自動檢索 | LLM 自己決定是否要搜 |
| 多輪檢索 | ❌ 只搜一次 | ✅ 可多輪、改寫查詢 |
| LLM 感知 RAG | ❌ 不知道 RAG 存在 | ✅ 主動參與 |
| 穩定性 | 高（無 FC 依賴） | 依賴 FC 能力（gemma4 已驗證）|
| 適合場景 | OpenWebUI 等前端直接使用 | 複雜多跳問題、自主研究 |
| 實作複雜度 | 低 | 中 |

**兩種模式可共存：**
- `api.py` Port 24786 繼續做 pre-process（OpenWebUI 用）
- `/chat` 端點升級為 tool-driven loop

---

## 五、改善行動計畫

### 短期（快速改善，風險低）

| # | 工作項目 | 工作量 | 說明 |
|---|---|---|---|
| S1 | **`api.py` 加入 system prompt** | XS | 指定回覆格式、引用風格；現在完全沒有 system prompt |
| S2 | **Layer 3 滑動窗口** | S | `compact/` 加硬截斷 fallback，防止長對話 OOM |
| S3 | **JSON Schema `"number"` 規範化** | XS | tool 定義統一用 `"number"`，不用 `"integer"` |
| S4 | **`min_score` 動態調整** | XS | 找不到結果時自動降低閾值重試一次 |

### 中期（Tool-driven RAG）

| # | 工作項目 | 工作量 | 說明 |
|---|---|---|---|
| M1 | **VectorSearchTool** | S | 包裝現有 pgvector search → `ToolDefinition`，schema 用 `"number"` |
| M2 | **KeywordSearchTool（BM25）** | M | 新增 `pg_trgm` 或 Python `rank_bm25`，與向量搜尋互補 |
| M3 | **ReadDocumentTool** | S | 依 doc_id 讀取完整文件 chunks |
| M4 | **AgenticRAG loop on `/chat`** | M | 在 `server.py` 的 `/chat` 掛上 ReAct loop，使用 M1-M3 工具 |
| M5 | **`/agentic-chat` 新端點（或升級現有 /chat）** | S | 區分 pre-process 與 tool-driven 兩條路徑 |

### 長期（增強能力）

| # | 工作項目 | 工作量 | 說明 |
|---|---|---|---|
| L1 | **per-format chunking** | M | PDF 按頁、DOCX 按 heading、code 按 function 邊界 |
| L2 | **FunctionCallingAdapter** | M | 統一 FC vs 模擬 FC 介面（支援不具備 FC 的模型）|
| L3 | **DeepResearchTool** | L | 啟動子 agent 進行多輪深度調研 |
| L4 | **PPTX / Code 解析器** | S | 擴充 `parsers.py` |

---

## 六、整體符合度（更新）

| 維度 | 舊評分 | 新評分 | 變化說明 |
|---|---|---|---|
| 模型抽象層 §2 | 90% | 90% | 無變化 |
| 文件處理 §8 | 80% | 80% | 無變化 |
| 向量存儲 + Embedding | 100% | 100% | 無變化 |
| 上下文壓縮 §5 | 70% | 70% | 缺 Layer 3 |
| Sub-Agent §6 | 85% | 85% | 無變化 |
| 核心 RAG 循環 §3 | **30%** | **35%** | +5%：api.py pre-process 完成、FC 已驗證可行 |
| Function Calling 適配 §4 | **0%** | **15%** | +15%：gemma4 原生支援 FC，adapter 需求降低 |
| API / Auth / 配置 | 95% | **98%** | +3%：api.py、來源附記、RAG trace 完成 |
| **整體** | **65%** | **~68%** | 基礎設施更完整，tool-driven loop 是最大缺口 |

---

## 七、關鍵技術限制（備忘）

1. **JSON Schema 型別**：gemma4 只接受 `"string" | "number" | "boolean" | "array" | "object"`，不接受 `"integer"`
2. **`assess_quality` 工具**：gemma4 傾向跳過，自己評估充足性後直接停止，這是正常行為
3. **`delta.reasoning` vs `delta.content`**：gemma4 thinking 內容在 `delta.reasoning`，需在 proxy 中改名為 `delta.reasoning_content` 才能讓 OpenWebUI 顯示思考塊
4. **OpenWebUI model 欄位**：會送連線名稱（如 "ClaudeRAG"）而非真實 model ID，api.py 必須忽略並固定用 `.env` 中的 `MODEL`
5. **SSL**：內網 LLM endpoint 使用自簽憑證，所有 httpx client 需 `verify=False`
