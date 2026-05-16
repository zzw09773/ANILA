# ANILA Governance(AI 治理文件)

> ANILA 平台對 **ISO/IEC 42001:2023(AI Management System)** 的合規文件集。
>
> **主索引**:[`iso-42001-compliance.md`](./iso-42001-compliance.md) — 條款 vs 實作對照、Annex A 反向 mapping、差距收斂計畫。

---

## 文件結構

| 文件 | 對應 ISO 42001 條款 | 用途 |
|---|---|---|
| [`iso-42001-compliance.md`](./iso-42001-compliance.md) | Clause 4–10 + Annex A | **主索引**;盤點現況、差距、行動項 |
| [`ai-policy.md`](./ai-policy.md) | 5.2 / A.2.2 / A.2.3 | AI 政策(簽核版) |
| [`roles-responsibilities.md`](./roles-responsibilities.md) | 5.3 / A.3.2 / A.3.3 / A.4.6 | RACI + 能力要求 + 內部回報通道 |
| [`risk-register.md`](./risk-register.md) | 6.1.2 / 8.3 | 風險登錄簿(每季 review) |
| [`aiia-template.md`](./aiia-template.md) | 6.1.4 / 8.4 / A.5.2 | AI Impact Assessment **填寫範本** |
| [`data-governance.md`](./data-governance.md) | A.7.x / A.4.3 | 資料治理:分類、來源、品質、保護 |
| [`model-card-template.md`](./model-card-template.md) | A.6.2.7 | Model card **填寫範本** |
| [`ai-incident-response.md`](./ai-incident-response.md) | 10.1 / A.3.3 / A.8.3 / A.8.4 | AI 事件回應流程 |
| [`third-party-ai-register.md`](./third-party-ai-register.md) | A.10.2 / A.10.3 | 第三方 AI 供應商登錄 + 評估流程 |

---

## 子目錄(填寫產出物存放處)

```
docs/governance/
├── README.md                       <-- 本檔
├── iso-42001-compliance.md         <-- 主索引
├── ai-policy.md
├── roles-responsibilities.md
├── risk-register.md
├── aiia-template.md                <-- 範本
├── data-governance.md
├── model-card-template.md          <-- 範本
├── ai-incident-response.md
├── third-party-ai-register.md
├── aiia/                           <-- 各 agent 填好的 AIIA(待建立)
├── model-cards/                    <-- 各 model 填好的 model card(待建立)
├── incidents/                      <-- T1/T2 事件文件(待建立)
├── drills/                         <-- 半年演練紀錄(待建立)
├── supplier-assessments/           <-- 第三方供應商評估表(待建立)
└── training-records/               <-- ISO 42001 訓練紀錄(待建立)
```

---

## 快速入門

### 開發者:我要新增一個 agent
1. 讀 [`ai-policy.md`](./ai-policy.md) §3-§4 — 平台的承諾與限制
2. 填一份 [`aiia-template.md`](./aiia-template.md) → 存到 `aiia/<agent-name>.md`
3. PR 審查時把 `aiia_doc_path` 填入 `agents` table(migration 0035)
4. 模型若是新註冊,先填 [`model-card-template.md`](./model-card-template.md) → 存到 `model-cards/`

### 稽核員:我要查現況
1. 從 [`iso-42001-compliance.md`](./iso-42001-compliance.md) §3 看 Annex A 反向對照
2. 抽查 `aiia/` / `model-cards/` 看實際填寫品質
3. 抽查 [`risk-register.md`](./risk-register.md) treatment 進度

### Risk Manager / Security Lead:我要做季度 review
1. 更新 [`risk-register.md`](./risk-register.md) §5 treatment 進度
2. 確認 [`third-party-ai-register.md`](./third-party-ai-register.md) §4 沒有變更未追蹤
3. 把 review 結論寫入對應文件 §「Review 紀錄」

### AIMS Owner:我要做半年管理審查
1. 召集 Risk Mgr / Security Lead / Data Steward
2. 走過 [`iso-42001-compliance.md`](./iso-42001-compliance.md) §4 Phase 1/2/3 進度
3. 產出 management review 紀錄(目前無模板,待 2026 Q3 首次審查後固定下來)

---

**Last updated**: 2026-05-16 · **Owner**: ANILA 平台團隊
