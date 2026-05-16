# 第三方 AI 供應商登錄(Third-Party AI Register)

> **狀態**:Draft v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Annex A.10.2 / A.10.3 / A.10.4
>
> **目的**:盤點 ANILA 平台依賴的所有第三方 AI 元件(模型 / 函式庫 / runtime),建立供應商評估流程與責任分配。

---

## 1. 範圍

「第三方 AI 元件」定義:
- 任何在 `model_registry` 註冊的模型(無論是 LLM、embedding、reranker)
- 任何 inference runtime(vLLM、Triton、TensorRT-LLM、ComfyUI、ollama 等)
- 任何 AI 相關函式庫(openai-agents-python、litellm、sentence-transformers 等)
- 任何 outbound 呼叫的 AI 服務(若有,目前內網部署 = 無)

---

## 2. 現行供應商登錄

### 2.1 上游模型(Model Providers)

| 模型 | 供應商 / 來源 | License | 用途 | 在 ANILA 的角色 | Model Owner | Model card |
|---|---|---|---|---|---|---|
| Gemma 系列(待確認版本) | Google | Gemma license | LLM chat | 主要 chat 模型 | _待填_ | _待補_ |
| _其他經 model_registry 註冊_ | | | | | | |

> 完整列表以 production `model_registry` 為準。每個 row 必須有 `model_card_url` 指向 [`model-card-template.md`](./model-card-template.md) 填好的版本。

### 2.2 Inference Runtime / 函式庫

| 元件 | 版本 | License | 功能 | 風險 | Owner |
|---|---|---|---|---|---|
| vLLM | 待補 | Apache 2.0 | LLM 推論 | 上游 CVE | Platform team |
| Triton Inference Server | 待補 | BSD-3 | embedding / 部分模型 | 上游 CVE | Platform team |
| TensorRT-LLM | 待補 | Apache 2.0 | 大模型加速 | 上游 CVE | Platform team |
| ComfyUI | 待補 | GPL-3 | 圖像生成 | 上游 CVE + license 邊界 | Platform team |
| openai-agents-python | 0.x | MIT | agent runtime 參考 | 已 fork 為 `anila-agent` template | Platform team |
| litellm | 1.x | MIT | OpenAI 相容代理 | API 介面相容性 | Platform team |
| sentence-transformers | 待補 | Apache 2.0 | embedding 工具 | 上游 CVE | Platform team |

### 2.3 第三方 API(Outbound)

| 服務 | 用途 | 內網部署是否啟用 |
|---|---|---|
| OpenAI API | _尚未確認_ | ❌ 內網模式禁用(`trusted_hosts` allowlist 控制) |
| Anthropic API | _尚未確認_ | ❌ 同上 |
| 任何 outbound LLM 服務 | — | ❌ 同上 |

> ANILA 內網部署模式下 **不允許** outbound LLM 呼叫;`nginx` + `trusted_hosts` migration 0034 強制限制。

---

## 3. 新供應商評估流程(A.10.3)

新增任何第三方 AI 元件到 `model_registry` / `trusted_hosts` / dependencies 前,**必須** 經過以下評估:

### 3.1 評估清單

- [ ] **Provenance**:來源可信(官方 release / 知名 maintainer)
- [ ] **License**:License 與中科院使用情境相容(commercial use 與否、distribution clause)
- [ ] **Security**:有無已知 CVE;最近一年修補活動
- [ ] **Supply chain**:checksum / signature 驗證(`weights_sha256` 寫入 model card)
- [ ] **Data lineage**(模型):訓練資料來源 / 是否含敏感類別
- [ ] **Performance benchmarks**:有最低可接受的內部評測分數
- [ ] **Privacy**:不會 phone home / telemetry
- [ ] **Update strategy**:upgrade path 清楚,有 deprecation notice
- [ ] **Rollback**:如何在 prod 移除

### 3.2 簽核

| 角色 | 必須簽核 |
|---|---|
| Model Owner | ✅ |
| AI Risk Manager | ✅ |
| Security Lead | ✅(security 評估項) |
| AIMS Owner | ✅(僅 high risk;e.g. 新類別模型) |

### 3.3 文件化

簽核完成 → 將評估清單存於 `docs/governance/supplier-assessments/<vendor>-<component>-<YYYY-MM-DD>.md`。

---

## 4. 持續監控(A.10.2)

每季 review:

- [ ] 確認 supplier 仍在維護(無 EoL)
- [ ] 檢查 CVE database 有無新 CVE
- [ ] 驗證 `weights_sha256` 沒有被悄悄改變
- [ ] 確認 license 沒有 retroactive 變更
- [ ] 確認 trusted_hosts 沒有意外擴張

---

## 5. 責任分配(A.10.2)

| 場景 | ANILA 責任 | 上游 / 第三方責任 |
|---|---|---|
| 模型輸出不正確 | 加 evaluation 與 refusal 機制、提醒使用者 | 模型本身的訓練品質 |
| 模型有偏誤 | 在 ANILA 層級量測 + mitigation | 訓練資料偏誤 |
| Inference runtime CVE | 立即升版 + audit log review | 上游 patch |
| License 變更 | 評估後決定下架 / 替換 | 公告變更 |
| 第三方 telemetry 收集 | 拒絕使用 | 上游揭露 |

---

## 6. 退役流程

何時應從 production 移除一個供應商:

- 上游 EoL 且無安全更新
- License 變更與中科院使用情境衝突
- 已知 CVE 在合理時程內無修補
- 評測指標長期低於替代品

退役程序:
1. AIMS Owner 簽核退役決定
2. 在 `model_registry` 標 `is_internal=False` + `health_status='deprecated'`
3. Router 移除該 model alias
4. 既有依賴 agent 切換到替代品
5. 1 個月後 hard delete row(保留 audit_logs)

---

**Last updated**: 2026-05-16 · **Owner**: AIMS Owner · **Next review**: 2026 Q3
