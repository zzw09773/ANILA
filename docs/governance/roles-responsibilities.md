# ANILA AIMS 角色與職責(RACI)

> **狀態**:Draft v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Clause 5.3 + Annex A.3.2 / A.3.3 / A.4.6 / A.10.2
>
> **目的**:界定 ANILA 平台 AI Management System(AIMS)的角色、職責、權限,確保稽核時對「誰負責什麼」有明確答案。

---

## 1. ISO 42001 定義的 AI 角色

ISO 42001 對 AI 系統的生命週期定義了 6 種角色;ANILA 內部對應如下:

| ISO 42001 角色 | 定義 | ANILA 內部對應 |
|---|---|---|
| **AI provider** | 提供 AI 系統 / 服務 | ANILA 平台團隊(`myCSPPlatform` + `anila-core` + Router + UI) |
| **AI developer** | 設計 / 開發 AI 系統 | 各 agent 開發單位(fork `anila-agent` template);平台團隊負責 runtime / 控制面 |
| **AI deployer** | 將 AI 系統部署到 production | ANILA 平台團隊(內網部署);agent 開發單位(各自服務上線) |
| **AI user** | 直接使用 AI 系統的人 | 中科院內部使用者(透過 UI、SDK、Studio、Router) |
| **AI subject** | 被 AI 系統處理資料的個人 | 中科院員工(對話內容、user_memory 包含個資) |
| **AI relevant authority** | 監管當局 | 中科院資安組、稽核單位、(未來)外部 ISO 42001 驗證機構 |

---

## 2. AIMS 內部角色(平台組織)

| 角色 | 負責人 | 主要職責 |
|---|---|---|
| **AIMS Owner**(管理代表) | 平台架構負責人 | 整體 ISO 42001 合規、政策維護、管理審查召集 |
| **AI Risk Manager** | 平台 PM | 維護 [`risk-register.md`](./risk-register.md)、每季 review 風險、追蹤 mitigation |
| **AI Security Lead** | 資安代表 | 對齊 ISO 42001 vs 27001;`ai-incident-response.md` 主理 |
| **Data Steward** | 平台後端負責人 | 維護 [`data-governance.md`](./data-governance.md);ingestion pipeline 資料品質 |
| **Model Owner**(per model) | 模型供應單位的對口 | 維護 model card、`weights_sha256`、`training_dataset_ref` |
| **Agent Owner**(per agent) | agent 開發單位的對口 | 維護 agent 的 AIIA、`source_commit_sha`、`vv_status` |
| **Auditor / Reviewer** | SRE + 資安組(獨立) | 每季抽查 agent 合規、首次 internal audit 執行者 |

---

## 3. RACI:關鍵活動的責任分配

| 活動 | AIMS Owner | Risk Mgr | Security Lead | Data Steward | Model Owner | Agent Owner | Auditor |
|---|---|---|---|---|---|---|---|
| 維護 [`ai-policy.md`](./ai-policy.md) | **R+A** | C | C | C | I | I | I |
| 維護 [`risk-register.md`](./risk-register.md) | A | **R** | C | C | C | C | I |
| 每季 risk review | A | **R** | C | C | I | I | I |
| 新 agent 上線前 AIIA | A | C | C | C | I | **R** | I |
| 新 model 註冊 model card | A | I | C | C | **R** | I | I |
| Production AI 事件回應 | C | C | **R+A** | C | C | C | I |
| 半年 management review | **R+A** | C | C | C | I | I | I |
| Internal audit | A | C | C | C | I | I | **R** |
| Agent V&V(部署前) | A | C | C | I | I | **R** | C |
| 對外溝通(重大事件) | **R+A** | C | C | I | I | I | I |
| ISO 42001 awareness training | **R+A** | C | C | I | C | C | I |

R = Responsible, A = Accountable, C = Consulted, I = Informed

---

## 4. 能力要求(Clause 7.2 competence)

進入 AIMS 範圍的角色,需具備的最低能力證據:

| 角色 | 最低要求 | 證據 |
|---|---|---|
| AIMS Owner | ISO 42001:2023 完整訓練(最少 16h) | 訓練證書,存於 `docs/governance/training-records/` |
| AI Risk Manager | AI 風險管理訓練 + 至少 1 次 risk review 主理經驗 | 同上 |
| Agent Owner | ANILA 平台 onboarding + AIIA template walkthrough | GitLab MR template checkbox 簽署 |
| Model Owner | model card 範本說明 | 同上 |
| 其他開發者 | ISO 42001 awareness(2h e-learning) | 平台內部 LMS 結業紀錄 |

**生效時程**:2026 Q3 上線前完成所有 Agent Owner / Model Owner 的 awareness training。

---

## 5. 利益衝突管理

- AIMS Owner / AI Risk Manager / Auditor **不可由同一人擔任**(分權)
- Internal audit 不可由被審查對象的直屬主管執行
- Model Owner 若與 AI Risk Manager 是同一人,該模型走外部 reviewer(SRE / 資安組)

---

## 6. 內部疑慮回報通道(A.3.3)

任何員工(含外包)發現 AI 系統可能違反本政策、產生不當輸出、或洩漏敏感資料時,回報路徑:

1. **第一線**:Agent Owner / Model Owner(直接接觸)
2. **第二線**:AIMS Owner(平台架構負責人)
3. **獨立通道**(避免報復):中科院 ethics hotline(若有);或寄信給 AI Security Lead
4. **匿名通道**:中科院內部建議信箱(若有)

詳細流程見 [`ai-incident-response.md`](./ai-incident-response.md) §3。

---

**Last updated**: 2026-05-16 · **Owner**: ANILA 平台團隊 · **Next review**: 每年或角色異動時
