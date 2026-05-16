# ANILA AI 風險登錄簿(Risk Register)

> **狀態**:Draft v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Clause 6.1.2 / 6.1.3 / 8.2 / 8.3
>
> **維護負責**:AI Risk Manager(平台 PM)
>
> **review 頻率**:每季 + 重大事件觸發

---

## 1. 風險分級

| Severity | 條件 | 必要審查 |
|---|---|---|
| **Critical** | 已造成 / 必然造成 個資外洩、機密外洩、業務中斷 > 4h | 立即 escalate,AIMS Owner 親簽 |
| **High** | 可能造成上述但有 partial mitigation | Risk Mgr + Security Lead 共同簽核 |
| **Medium** | 已知問題,有暫時 workaround | Risk Mgr 簽核 |
| **Low** | 監控中,沒有立即影響 | 季度 review |

---

## 2. 風險登錄(Active)

| ID | 描述 | 類別 | Severity | Mitigation 現況 | Owner | 對應 Annex A | 下次 review |
|---|---|---|---|---|---|---|---|
| R-001 | LLM hallucination 導致使用者引用錯誤資訊做決策 | Model | **High** | 強制 citation;UI footer 標示 AI disclosure;每季 refusal/accuracy 評測 | Agent Owner | A.6.2.4 / A.9.2 | 2026-08 |
| R-002 | User memory 萃取出敏感事實(健康、政治、機密)被跨 agent 讀取 | Privacy | **High** | `user_facts` schema 有 sensitivity flag(待 migration);ingestion 端 PII detection | Data Steward | A.7.4 / A.9.4 | 2026-08 |
| R-003 | 對話內容被未授權使用者讀取(權限漏洞) | Security | **Critical** | RLS per-collection;`access_control.py` role gate;`required_roles` | Security Lead | A.9.4 | 2026-06 |
| R-004 | 模型版本部署到 prod 卻無法回溯到 git commit | Traceability | **High** | migration 0035 加 `source_commit_sha`;CI 綁定 image tag 待補 | AIMS Owner | A.6.2.5 | 2026-07 |
| R-005 | Agent 上線無 AIIA,使用者承受未評估風險 | Governance | **High** | migration 0035 加 `aiia_doc_path`;UI gate;PR template checkbox | AIMS Owner | A.5.2 / A.5.4 | 2026-07 |
| R-006 | model_registry 模型無 model card,使用者不知道訓練資料 / 限制 | Transparency | Medium | migration 0035 加 `model_card_url`;model card template | Model Owner | A.6.2.7 | 2026-07 |
| R-007 | 上游模型供應商修改 weights 但 ANILA 沒察覺 | Supply chain | Medium | migration 0035 加 `weights_sha256`;啟動時對照 | Model Owner | A.10.3 | 2026-07 |
| R-008 | Audit log 寫入失敗導致關鍵事件無紀錄 | Logging | Medium | `audit_service.py` 採 fail-soft + logger 雙寫(已實作);DB-level 永久保留(實際 retention = 永久) | Security Lead | A.6.2.8 | 2026-12 |
| R-009 | 第三方 LLM endpoint 被換成 typosquat / 中間人 | Supply chain | **High** | nginx `trusted_hosts` allowlist(已實作);TLS pin 待補 | Security Lead | A.10.2 | 2026-06 |
| R-010 | Refusal rate 對不同角色 / 卡別差異過大(偏見) | Fairness | Medium | 每季評測,差異 > 20% 觸發 review | Risk Mgr | A.9.3 | 2026-08 |
| R-011 | Sandbox 不足:dev 上傳 Python 跑在平台 infra(ingestion-platform 已明確 ❌ 不做) | Sandbox | Low | 設計上拒絕此能力,改走 chunking 評測器 | Data Steward | A.7.2 | 2026-12 |
| R-012 | GitLab 未上線 → agent 原始碼無中央存放庫,違反 A.6.2.5 | Infra | **High** | 已列入 multi-service-integration-plan §7.4,但實際部署 pending | AIMS Owner | A.6.2.5 / A.8.2 | 2026-06 |
| R-013 | 使用者不知道自己在跟 AI 互動(transparency) | UX | Medium | UI footer AI disclosure 本輪上線;model name banner 在 chat composer | Agent Owner | A.8.2 | 2026-06 |
| R-014 | 大規模事件無 incident playbook(只有零散 runbook) | Process | Medium | 本輪建立 [`ai-incident-response.md`](./ai-incident-response.md) | Security Lead | A.10.1 | 2026-08 |
| R-015 | 開發者沒受過 ISO 42001 awareness training | Competence | Medium | Q3 上線前完成 e-learning | AIMS Owner | Clause 7.2 / 7.3 | 2026-07 |

---

## 3. 已關閉(Closed)風險

| ID | 描述 | 關閉日期 | 解決方式 |
|---|---|---|---|
| _(本登錄簿首次建立,尚無 closed item)_ | | | |

---

## 4. Review 紀錄

| Date | Reviewer | 變更 |
|---|---|---|
| 2026-05-16 | AI Risk Manager(初版) | 首次建立,15 條 active risks |

---

## 5. Treatment 進度追蹤

每個風險的 mitigation 進度,在每季 review 時填:

| Risk ID | Mitigation 狀態 | 完成度 | 備註 |
|---|---|---|---|
| R-001 | Citation ✅ / UI banner ⏳ / 季評測 ⏳ | 33% | UI 本輪上線 |
| R-002 | ingestion PII ✅ / sensitivity flag ⏳ | 50% | 待 user_facts schema 補欄位 |
| R-003 | RLS ✅ / role gate ✅ / required_roles ✅ | 100% | 持續監控 |
| R-004 | migration 0035 ⏳ / CI 綁定 ⏳ | 0% | 本輪寫 migration,CI 下輪做 |
| R-005 | migration 0035 ⏳ / PR template ⏳ | 0% | 同上 |
| R-006 | template ✅ / 落地 ⏳ | 50% | Q3 補完所有 model |
| R-007 | migration 0035 ⏳ | 0% | |
| R-008 | fail-soft ✅ | 100% | |
| R-009 | trusted_hosts ✅ / TLS pin ⏳ | 60% | |
| R-010 | 評測流程 ⏳ | 0% | Q3 上線 |
| R-011 | 設計上排除 ✅ | 100% | |
| R-012 | 設計 ✅ / 部署 ⏳ | 30% | |
| R-013 | UI footer ⏳ | 0% | 本輪上線 |
| R-014 | playbook ✅ | 100% | 持續演練 |
| R-015 | e-learning ⏳ | 0% | Q3 上線前 |

---

**Last updated**: 2026-05-16 · **Owner**: AI Risk Manager · **Next review**: 2026-08
