# ISO/IEC 42001:2023 合規對照與差距清單

> **狀態**:Draft v1(2026-05-16)。本文件是 ANILA 平台對 ISO/IEC 42001:2023(AI Management System)的**主索引**:盤點各條款現況、對應到既有機制、列出仍待補的 gap 與時程。
>
> **配套文件**(`docs/governance/`):
> - [`ai-policy.md`](./ai-policy.md) — AI 政策(A.2.2)
> - [`roles-responsibilities.md`](./roles-responsibilities.md) — AIMS 角色與 RACI
> - [`aiia-template.md`](./aiia-template.md) — AI System Impact Assessment 範本
> - [`risk-register.md`](./risk-register.md) — AI 風險登錄簿
> - [`data-governance.md`](./data-governance.md) — 資料治理(A.7)
> - [`model-card-template.md`](./model-card-template.md) — Model Card 範本(A.6.2.7)
> - [`ai-incident-response.md`](./ai-incident-response.md) — AI 事件回應(Clause 10)
> - [`third-party-ai-register.md`](./third-party-ai-register.md) — 第三方 AI 供應商登錄(A.10)

---

## 1. AIMS 範圍(Scope)

ANILA 平台對 ISO/IEC 42001:2023 的角色:

| ISO 42001 角色 | ANILA 對照 |
|---|---|
| **AI provider** | ANILA 平台團隊 — 提供 OpenAI 相容介面、Router、agent template、ingestion pipeline 給內部使用者 |
| **AI developer** | 各 agent 開發單位(fork `anila-agent` template);平台團隊負責 `anila-core` runtime / CSP 控制面 |
| **AI deployer** | ANILA 平台團隊(內網部署)、agent 開發單位(各自 agent 服務) |
| **AI user** | 中科院內部使用者(透過 ANILA UI / Studio / Router / SDK) |
| **AI subject** | 中科院員工(對話記錄、user memory、檔案上傳會留存) |

範圍邊界:
- ✅ **In scope**:`myCSPPlatform`、`anila-core`、`anila-core-router`、`anila-agent` template、`ingestion-worker`、`ANILA_UI`、`ANILALM`、ingestion 走 pgvector 的所有資料、`audit_logs`、第三方 LLM 模型(經 model_registry 註冊者)
- ❌ **Out of scope**:上游 LLM 模型的訓練流程(由模型供應商負責)、使用者私人筆電上的 fork repo(進入 GitLab 後才納入範圍)

---

## 2. Clause 4–10 主幹條文現況

| Clause | 要求 | 現況 | 行動項 |
|---|---|---|---|
| **4.1 organization context** | 內外部議題盤點 | ⚠️ README 有產品定位但未以 ISO 視角文件化 | 在 `ai-policy.md` §2 補充利害關係人(中科院、agent dev、end user、稽核員) |
| **4.2 interested parties needs** | 利害關係人需求 | ❌ 無 | 同上 |
| **4.3 AIMS scope** | 明確界定 AIMS 範圍 | ✅ 本文件 §1 | — |
| **5.1 leadership** | 高階管理者承諾 | ⚠️ 文件化中 | 平台 leader 在 `ai-policy.md` 簽核 |
| **5.2 AI policy** | 書面 AI 政策 | ✅ 本輪建立 [`ai-policy.md`](./ai-policy.md) | 內部審閱簽核 |
| **5.3 roles & authorities** | 角色職責 | ✅ 本輪建立 [`roles-responsibilities.md`](./roles-responsibilities.md) | RACI 走兩輪審閱 |
| **6.1.2 risk management** | AI 風險評估 | ✅ 本輪建立 [`risk-register.md`](./risk-register.md) | 每季 review |
| **6.1.4 AI impact assessment** | AIIA 程序 | ✅ 範本建立 [`aiia-template.md`](./aiia-template.md) | 既有 5+ 個 agent 補做 retro-AIIA(Q3 完成) |
| **6.2 AI objectives** | 量化目標 | ⚠️ Sprint plan 內有,但未集中 | 在 `ai-policy.md` §5 收斂為 5 條 SLO/KPI |
| **7.2 competence** | 開發者能力證據 | ❌ 無訓練紀錄 | Q3 上線時要求所有 agent dev 完成 ISO 42001 awareness training |
| **7.3 awareness** | 知曉 AI 政策 | ❌ 無 | 政策上線後郵件通知 + GitLab MR template 加 checkbox |
| **7.4 communication** | 利害關係人溝通 | ⚠️ 內部 channel 有但未制度化 | `ai-policy.md` §6 規範對外溝通管道 |
| **7.5 documented information** | 文件控管 | ✅ 全部走 git + PR review | GitLab 上線後強化 |
| **8.1 operational planning** | 維運計畫 | ✅ docker-compose + runbooks/ + intranet-deployment-runbook | — |
| **8.2 AI risk assessment(ops)** | 變更時重評風險 | ❌ 無觸發機制 | 將 AIIA 列為 agent 註冊強制欄位(migration 0035 + UI gate) |
| **8.3 AI risk treatment** | 風險處置 | ⚠️ 安全控制有,但未對應到風險登錄 | `risk-register.md` 補 treatment 欄位 |
| **8.4 AI impact assessment(ops)** | 上線前/變更時 AIIA | ❌ 無強制 | 同 8.2 |
| **9.1 monitoring** | 效能/偏誤/漂移監控 | ⚠️ `audit_logs` ✅、模型 drift / refusal rate / PII leak 監控 ❌ | Q3 補評測器對 production agent 跑 daily 評測 |
| **9.2 internal audit** | 內稽 | ❌ 無 | Q3 跑首次 internal audit(委由 SRE / 資安組外部視角) |
| **9.3 management review** | 管理階層審查 | ❌ 無 | 每半年一次,首次 2026 Q3 |
| **10.1 nonconformity & CA** | 不符合 + 矯正措施 | ⚠️ 有 incident runbooks 但非 AI-specific | 本輪建立 [`ai-incident-response.md`](./ai-incident-response.md) |
| **10.2 continual improvement** | 持續改進 | ✅ Sprint plan + CHANGELOG | 把 ISO 42001 review 加入 sprint retro |

---

## 3. Annex A 控制反向對照表

> 標記說明:✅ 完全達成 / ⚠️ 部分達成(需補文件) / ❌ 未達成

| Annex A 控制 | 要求摘要 | ANILA 現況 | 對應實作或文件 |
|---|---|---|---|
| **A.2.2** policies for AI | AI 政策 | ✅ | [`ai-policy.md`](./ai-policy.md) |
| **A.2.3** alignment with other policies | 與資安 / 隱私政策對齊 | ⚠️ | 待補 `info-sec-alignment` 章節在 `ai-policy.md` |
| **A.3.2** AI roles & responsibilities | 角色職責 | ✅ | [`roles-responsibilities.md`](./roles-responsibilities.md) |
| **A.3.3** reporting of concerns | 內部 AI 疑慮回報通道 | ⚠️ | `ai-incident-response.md` §3 待補 whistleblowing |
| **A.4.2** resources for AI | AI 資源 inventory | ⚠️ | docker-compose.yml + models/ 有,但需補正式 inventory(GPU / model / dataset) |
| **A.4.3** data resources | 資料資源 | ⚠️ | [`data-governance.md`](./data-governance.md) §2 |
| **A.4.4** tooling resources | 工具資源 | ✅ | README §子專案 + docker-compose |
| **A.4.5** system & computing resources | 算力資源 | ✅ | 4×H100 + Triton + ComfyUI(README) |
| **A.4.6** human resources | 人力資源 | ⚠️ | 待補在 `roles-responsibilities.md` |
| **A.5.2** AI system impact assessment | AIIA 程序 | ✅ | [`aiia-template.md`](./aiia-template.md) |
| **A.5.3** documentation of AIIA | AIIA 文件保存 | ⚠️ | 將存於 `docs/governance/aiia/<agent-name>.md` |
| **A.5.4** AIIA process | AIIA 觸發條件 | ✅ | `aiia-template.md` §0 + agents schema `aiia_id` 欄位 |
| **A.5.5** assessing AI impact on individuals | 對個人影響 | ⚠️ | `aiia-template.md` §3 待 agent 各自填 |
| **A.6.1.2** objectives for development | 開發階段目標 | ⚠️ | 每個 agent 走 `aiia-template.md` §5 |
| **A.6.1.3** processes for responsible AI | 負責任 AI 流程 | ⚠️ | `ai-policy.md` §4 |
| **A.6.2.2** requirements & specification | AI 需求書 | ⚠️ | `docs/architecture/` 有設計文件,需在 PR template 標示「AI 需求」 |
| **A.6.2.3** design & development docs | 設計與開發文件 | ✅ | `docs/architecture/` |
| **A.6.2.4** verification & validation | V&V | ❌ | 待補評測器 + commit SHA 綁定([planning §7.4](../planning/multi-service-integration-plan.md)) |
| **A.6.2.5** deployment | 部署可追溯 | ⚠️ | docker image tag → git commit 待 CI 補綁定;[`intranet-deployment-runbook.md`](../runbooks/intranet-deployment-runbook.md) ✅ |
| **A.6.2.6** operation & monitoring | 上線後監控 | ⚠️ | `audit_logs` ✅;模型 KPI 監控待補 |
| **A.6.2.7** technical documentation | model card / data sheet | ✅ 範本 ❌ 落地 | [`model-card-template.md`](./model-card-template.md);migration 0035 加 `model_card_url` 欄位 |
| **A.6.2.8** logging | 事件紀錄 | ✅ | `audit_logs` + [`audit_service.py`](../../myCSPPlatform/backend/app/services/audit_service.py)(fail-soft) |
| **A.7.2** data for development | 開發資料 | ⚠️ | [`data-governance.md`](./data-governance.md) §3 |
| **A.7.3** acquisition of data | 資料取得合法性 | ⚠️ | `data-governance.md` §4(中科院內部資料,免外部蒐集同意,但需補使用同意書) |
| **A.7.4** quality of data | 資料品質 | ⚠️ | Chunking Evaluator 服務(LLM-as-judge)✅;對齊 ISO 用語在 `data-governance.md` §5 |
| **A.7.5** data provenance | 來源追溯 | ⚠️ | pgvector 已記 `source_uri`;補 dataset_ref 欄位(migration 0035) |
| **A.7.6** data preparation | 前處理紀錄 | ✅ | `ingestion-worker` parse→chunk→embed pipeline(documented in [`docs/architecture/ingestion-platform-design.md`](../architecture/ingestion-platform-design.md)) |
| **A.8.2** system documentation for users | 給使用者的系統說明 | ❌ → ✅ 本輪 | UI footer AI disclosure + link to `ai-policy.md` |
| **A.8.3** external reporting | 對外通報 | ❌ | `ai-incident-response.md` §4 |
| **A.8.4** communication of incidents | 事件溝通 | ❌ | `ai-incident-response.md` §3 |
| **A.8.5** information for interested parties | 利害關係人資訊 | ⚠️ | 透過 `docs/governance/` 公開 |
| **A.9.2** intended use | 預期用途 | ⚠️ | 每個 agent 的 `aiia-template.md` §1 |
| **A.9.3** objectives for responsible use | 負責任使用目標 | ⚠️ | `ai-policy.md` §4 |
| **A.9.4** intended use by users | 限制使用者超出預期 | ✅ | `access_control.py` + `service_access_grants` + `required_roles` |
| **A.10.2** allocation of responsibilities | 第三方責任 | ✅ | [`third-party-ai-register.md`](./third-party-ai-register.md) |
| **A.10.3** supplier process | 第三方供應商評估 | ⚠️ | `third-party-ai-register.md` §3 — 評估流程已寫,逐家補完 |
| **A.10.4** customer process | 對下游客戶通知 | N/A | ANILA 不對外服務,僅中科院內網 |

---

## 4. 差距收斂計畫(Phase 1 / 2 / 3)

### Phase 1 — 文件 + Schema(本 Sprint,2026-05-16 起)
- ✅ 建立 `docs/governance/` 全套 10 份文件(本 commit)
- ✅ Migration 0035:`agents` 加 `source_commit_sha`、`last_reviewer_id`、`vv_status`、`aiia_doc_path`;`model_registry` 加 `model_card_url`、`training_dataset_ref`、`weights_sha256`、`intended_use`、`limitations`
- ✅ UI footer 加 AI disclosure + 政策連結(A.8.2)
- ✅ README 加 Governance 區段

### Phase 2 — Process + V&V(2026 Q3 上線前)
- [ ] CI 把 `docker image tag` 綁 `git rev-parse HEAD`(A.6.2.5)
- [ ] 既有 5+ 個 agent 補做 retro-AIIA(填 `aiia-template.md` 存到 `docs/governance/aiia/`)
- [ ] 既有 model_registry rows 補 `model_card_url`(指到 `docs/governance/model-cards/`)
- [ ] Chunking Evaluator 結果寫 `eval_runs` table,綁 `source_commit_sha`(A.6.2.4)
- [ ] GitLab 上線(planning §7.4 已標明「不上線無法通過驗證」)
- [ ] `audit_logs` retention 確認 ≥ 12 個月(目前 DB-level 無 prune job,實際 retention = 永久,符合 ✓)

### Phase 3 — Monitoring + Audit(2026 Q3 上線後)
- [ ] Production agent 跑 daily 評測(refusal rate、PII leak rate、citation precision)
- [ ] 首次 internal audit(由 SRE 或外部視角執行)
- [ ] 首次 management review(每半年)
- [ ] 完成所有 agent dev 的 ISO 42001 awareness training

---

## 5. 已落地的安全/治理控制(可作為驗證證據)

這些是上兩個 sprint(資安 hardening)的成果,可直接 mapping 到 Annex A:

| 控制 | 實作位置 | 對應 |
|---|---|---|
| Cookies 安全 flags | [`middleware/cookies.py`](../../myCSPPlatform/backend/app/middleware/cookies.py) | A.6.2.6 |
| Startup security checks(CARD_INITIAL_OWNERS 防呆等) | [`services/startup_security.py`](../../myCSPPlatform/backend/app/services/startup_security.py) | A.6.2.5 |
| Audit logging(fail-soft) | [`services/audit_service.py`](../../myCSPPlatform/backend/app/services/audit_service.py) | **A.6.2.8** |
| nginx 6 安全 header + Host allowlist | [`docker/nginx.conf`](../../myCSPPlatform/docker/nginx.conf) | A.6.2.6 |
| 卡片登入(HiPKI / 中科院憑證卡) | [`services/card_auth.py`](../../myCSPPlatform/backend/app/services/card_auth.py) | A.9.4 |
| RLS per-collection 隔離 | migrations 0012/0013 + ingestion pipeline | A.7.2 / A.9.4 |
| Trusted hosts allowlist | migration 0034 | **A.10.2** |
| Service token per-credential | migrations 0017/0027 + `service_clients` | A.10.2 |
| Role gate(`required_roles`) | migration 0012 + `access_control.py` | A.9.4 |

---

## 6. Review 紀錄

| Date | Reviewer | 變更 |
|---|---|---|
| 2026-05-16 | ANILA 平台團隊 | Draft v1 — 首次建立 |

---

**Last updated**: 2026-05-16 · **Owner**: ANILA 平台團隊 · **Next review**: 2026 Q3 上線前
