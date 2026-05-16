# ANILA AI 政策(AI Policy)

> **狀態**:Draft v1(2026-05-16),待平台 leader 簽核。
>
> **對應 ISO/IEC 42001:2023**:Clause 5.2 + Annex A.2.2 / A.2.3 / A.6.1.3 / A.9.3
>
> **適用範圍**:ANILA 平台所有元件(`myCSPPlatform`、`anila-core`、`anila-core-router`、`anila-agent` 模板、`ingestion-worker`、`ANILA_UI`、`ANILALM`)、註冊到平台的 agent、所有經 `model_registry` 註冊的 LLM 模型。

---

## 1. 目的

確保 ANILA 平台及其 agent 在開發、部署、維運過程中:
1. **負責任**:遵守中科院內部資訊安全、隱私、研發倫理規範
2. **可追溯**:每個 AI 推論結果可回溯到原始碼、模型版本、訓練資料來源
3. **可驗證**:有獨立評測證據證明其符合預期用途
4. **可監督**:管理階層可定期審查 AI 系統健康狀態

---

## 2. 利害關係人(Interested Parties)

| 利害關係人 | 需求 | 平台如何回應 |
|---|---|---|
| 中科院使用者 | 取得正確、可信、合理速度的 AI 回應 | SLO、refusal banner、citation 機制 |
| Agent 開發單位 | 共用基礎設施、無重工 | `anila-agent` template、CSP `/v1/agents`、router |
| 中科院資安組 | 系統符合內網資安政策 | 卡片登入、RLS、audit_logs、nginx hardening |
| 中科院稽核單位 | 符合 ISO 42001、可審計 | 本治理文件、AIIA、model card、incident response |
| 中科院 IT 維運 | 部署 / 升級 / rollback 流程清楚 | runbooks/、CHANGELOG、deployment pipeline |
| 模型供應商(內部) | 模型納入註冊有正式程序 | `third-party-ai-register.md`、model card 強制填寫 |

---

## 3. 平台對 AI 的承諾

1. **不做下列事**(absolute prohibitions):
   - 不將中科院內部對話或文件外送任何**外部**雲端 LLM(僅限內網 on-prem 推論)
   - 不在未告知使用者的情況下蒐集或留存對話內容(透明度 banner + Privacy Notice)
   - 不部署未經 AIIA(影響評估)的 agent 到 production
   - 不啟用未經 V&V(verification & validation)的模型版本到 production

2. **承諾做下列事**(positive commitments):
   - 每個 production agent 必須有 [`aiia-template.md`](./aiia-template.md) 填寫並 review
   - 每個 production 模型必須有 [`model-card-template.md`](./model-card-template.md) 填寫
   - 每筆 user-facing AI 回應的請求 / 回應都進 `audit_logs`
   - 每次 production 部署,docker image tag 可回溯到 git commit SHA

---

## 4. 負責任 AI 原則(Responsible AI Principles)

對照 ISO 42001 Annex A.9.3:

| 原則 | 落地方式 |
|---|---|
| **Fairness / 公平** | 模型 refusal rate 對不同角色 / 卡別記錄,每季 review;發現顯著差異要 root-cause |
| **Transparency / 透明** | UI footer 標示「本系統由 AI 驅動」+ 連到本政策;agent 回答必附 citation(若是 RAG 類) |
| **Accountability / 課責** | 每個 agent / model 有 `last_reviewer_id`(migration 0035);事件回應 [`ai-incident-response.md`](./ai-incident-response.md) |
| **Privacy / 隱私** | 對話與 user_memory 走中科院內網 PostgreSQL,不外送;PII detection 在 ingestion 端篩 |
| **Robustness / 穩健** | 模型推論失敗 fail-soft;audit log fail-soft;router fallback 機制 |
| **Safety / 安全** | RLS per-collection 隔離;`required_roles` gate;trusted_hosts allowlist;卡片二次認證 |
| **Human Oversight / 人類監督** | UI 提供「重新生成」/ 「人類接管(handoff)」/ 「分享給其他人 review(share)」 |

---

## 5. AI 目標(Objectives,Clause 6.2)

平台層次的量化目標(每季 review,首次設定 2026-05-16):

| KPI | 目標值 | 量測方式 |
|---|---|---|
| Production agent AIIA 完成率 | 100% | `agents.aiia_doc_path IS NOT NULL` |
| Production model card 完成率 | 100% | `model_registry.model_card_url IS NOT NULL` |
| Docker image → git SHA 可追溯率 | 100% | CI gate |
| AI-related security incident MTTR | < 4 小時 | `ai-incident-response.md` 紀錄 |
| Audit log availability | ≥ 99.9% | `audit_logs` insert success rate |
| Refusal-rate 跨角色 / 跨卡別差異 | < 20% relative gap | 每季 evaluation 報告 |

---

## 6. 溝通(Clause 7.4)

| 對象 | 管道 | 頻率 |
|---|---|---|
| Agent 開發單位 | GitLab MR + 平台週會 | 持續 / 每週 |
| End user | UI banner + 平台公告頁 | 重大變更時 |
| 資安組 | 月報 + incident notification | 月 / 即時 |
| 稽核單位 | `docs/governance/` 公開 + 半年管理審查 | 半年 / 隨需 |
| 中科院領導層 | 半年管理審查報告 | 半年 |

---

## 7. 政策審查

- **審查頻率**:每年至少一次,或重大變更觸發
- **觸發重審條件**:法規變動、重大事件、平台架構大改、新增高風險 agent
- **審查紀錄**:本文件下方 §8

---

## 8. 簽核與審查紀錄

| Date | 角色 | 姓名 | 簽核 / 變更 |
|---|---|---|---|
| 2026-05-16 | 平台架構負責人 | _待簽_ | Draft v1 首次建立 |
| 2026-05-16 | 平台 PM | _待簽_ | Draft v1 |
| 2026-05-16 | 資安代表 | _待簽_ | Draft v1 |

---

**Last updated**: 2026-05-16 · **Next review**: 2027-05 · **Owner**: ANILA 平台團隊
