# ANILA 資料治理(Data Governance)

> **狀態**:Draft v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Annex A.7.2 / A.7.3 / A.7.4 / A.7.5 / A.7.6 + A.4.3
>
> **目的**:盤點 ANILA 處理的資料類別、來源、生命週期、品質、保護機制,符合 ISO 42001 對「AI 系統資料」的要求。

---

## 1. 資料分類(Classification)

| 類別 | 來源 | 儲存位置 | Sensitivity |
|---|---|---|---|
| 使用者對話 | UI / SDK 即時產生 | PostgreSQL `conversations` | High(可能含業務機密) |
| User memory facts | 從對話自動萃取 | PostgreSQL `user_facts` + embeddings | High |
| 上傳檔案(ingestion) | 使用者上傳到 Knowledge Collection | 物件存儲(S3-like)+ pgvector | Medium-High |
| Document chunks | ingestion-worker 切割產出 | pgvector `document_chunks` | Medium-High |
| Audit logs | API 呼叫自動寫 | PostgreSQL `audit_logs` | Medium(含 actor 資訊) |
| 卡片登入 metadata | HiPKI 驗證後寫 | `users.card_id` 等欄位 | High(個資) |
| Model weights | 從上游供應商下載 | `models/` volume | Low(本身非個資) |
| API key / token | 系統產生 | `api_keys` / `service_clients`(hashed) | **Critical** |

---

## 2. 資料資源 inventory(A.4.3)

對 ISO 稽核要列出所有 AI-related dataset 與其性質。

| Dataset | 用途 | 範圍 | 是否含個資 | Owner |
|---|---|---|---|---|
| `conversations` table | 訓練範圍 ❌、retrieval 範圍 ❌、僅運行時記錄 | 全使用者對話 | 是 | Data Steward |
| `user_facts` + embeddings | 個人化背景 retrieval | 從對話萃取 | 是 | Data Steward |
| Knowledge Collections(per-agent) | RAG retrieval source | 由 agent 開發單位定義 | 視 agent 而定 | Agent Owner |
| Audit log archive | 稽核 / debug | 系統呼叫紀錄 | 部分(actor) | Security Lead |
| Test/eval dataset | V&V 評測 | 內部建立 | 否(去識別) | Agent Owner |

> **重要**:ANILA **不訓練模型**(僅推論);所有「dataset」是 retrieval 或評測用。

---

## 3. 開發資料(A.7.2)

ANILA 開發階段使用的資料:

| 場景 | 來源 | 個資處理 |
|---|---|---|
| Unit test fixture | 平台團隊自造(假資料) | 無 |
| Integration test | 平台團隊自造(假資料) | 無 |
| Dev 環境的 db seed | `auto_seed.py` 產生的測試帳號 | 無(都是 fake) |
| 評測 dataset(per-agent) | 各 agent 自行準備 | **需去識別**;若用真實對話需 §4 同意機制 |

---

## 4. 資料取得(A.7.3 acquisition)

ANILA 處理的資料來源 + 合法性:

| 來源 | 法律基礎 | 同意機制 |
|---|---|---|
| 中科院員工對話 | 內部系統使用條款(隨員工 onboarding 簽核) | UI footer 連結到 Privacy Notice;首次登入跳同意彈窗 |
| 使用者上傳檔案 | 員工對自己工作檔案的處置權 | 上傳時 UI 提示「本檔案會經 AI 處理」 |
| HiPKI 卡片 metadata | 中科院身分認證體系 | 卡片發放時涵蓋 |
| 模型 weights | 從合法供應商下載(Llama, Gemma 等開源 license) | License 文件存於 `models/` 對應 dir |
| 外部 RAG 資料(若有) | 必須是公開資料 OR 有授權 | Agent Owner 在 AIIA §2 說明 |

**禁止來源**:
- ❌ 任何個人健康 / 醫療資料
- ❌ 未經授權的第三方版權內容
- ❌ 透過爬蟲取得且網站 ToS 禁止的內容

---

## 5. 資料品質(A.7.4)

| 維度 | 標準 | 量測 / 控制 |
|---|---|---|
| Accuracy | source_uri 必須真實存在 | ingestion-worker 校驗 |
| Completeness | chunking 不可截斷句子中間 | parent-child chunking 機制 |
| Consistency | 同一檔案多次 ingest 結果穩定 | `content_hash` 去重 |
| Timeliness | 文件變更要可觸發 re-ingest | manual + scheduled |
| Bias | 評測階段檢測 refusal / accuracy 跨群差異 | 每季評測(R-010) |
| Toxicity / PII | ingestion 前篩 | ingestion pipeline PII detection(待完整實作) |

---

## 6. 資料來源追溯(A.7.5 provenance)

每一筆 retrievable chunk 都必須能回答:
- 來自哪個檔案(`source_uri`)
- 哪個 collection(`collection_id`)
- 何時 ingest(`ingested_at`)
- 用什麼 chunking 策略(`chunking_strategy`)
- 用什麼 embedding 模型(`embedding_model_id`)

實作狀態:`document_chunks` 已有前 4 欄;embedding model id 待補(migration 0035 candidate)。

對 model 層級:`model_registry` 加 `training_dataset_ref`(migration 0035),記錄上游模型訓練資料來源(指向 model card)。

---

## 7. 前處理紀錄(A.7.6)

ingestion pipeline 流程(見 [`docs/architecture/ingestion-platform-design.md`](../architecture/ingestion-platform-design.md)):

```
upload → parse → chunk → embed → write pgvector → audit_log
```

每步驟產出 metadata 寫入 `document_chunks`:
- `parser_version` — 解析器版本
- `chunk_strategy` — 切塊策略 + 參數
- `embedding_model_id` — embedding 模型(待補欄位)
- `ingested_at` / `ingested_by` — 何時 / 誰觸發

---

## 8. 保護機制(對應 ISO 27001 + 42001 交集)

| 機制 | 實作 |
|---|---|
| RLS per-collection | migration 0012 + 0013(已實作) |
| 加密傳輸 | nginx TLS + intranet rotate runbook |
| 加密儲存 | PostgreSQL 走中科院內網安全儲存層;model weights 在 volume |
| 存取稽核 | `audit_logs` 全記 |
| 個資去識別(若需 export 給研究用) | 待規劃 — DP / k-anonymity 流程 |

---

## 9. 資料生命週期(Lifecycle)

| 階段 | 保留期限 | 處置 |
|---|---|---|
| `conversations` | 持續保留(內網,無外送) | 使用者要求刪除 → soft delete `is_deleted` flag |
| `user_facts` | 跟 conversations 同步 | 使用者可在 UI 「Memory」tab 個別刪除 |
| Knowledge Collection chunks | Agent Owner 決定 | re-ingest 觸發 delete + insert |
| Audit logs | ≥ 12 個月(42001 建議),目前實際 = 永久 | 未來若需 archive,走 partition + cold storage |
| Model weights | 模型退役後保留 1 年 | 滿期由 Model Owner 簽核刪除 |

---

## 10. 個人資料權利(Data Subject Rights)

ANILA 提供使用者:
- ✅ **查詢權**:UI 可看自己所有對話 + memory facts
- ✅ **刪除權**:UI 「Memory」tab + 對話 archive / delete
- ⚠️ **更正權**:目前需透過 admin 人工處理
- ❌ **可攜權**:export 功能待規劃

---

**Last updated**: 2026-05-16 · **Owner**: Data Steward · **Next review**: 2026 Q3
