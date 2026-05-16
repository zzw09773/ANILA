# AI 事件回應(Incident Response)

> **狀態**:Draft v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Clause 10.1(nonconformity & corrective action)+ Annex A.3.3 / A.8.3 / A.8.4
>
> **目的**:當 AI 系統行為偏離預期 / 造成傷害時的標準回應流程。本文件補足現有 [`docs/runbooks/`](../runbooks/) 缺少的 AI-specific 維度。

---

## 1. 事件分類

| Tier | 定義 | 範例 | SLA |
|---|---|---|---|
| **T1 Critical** | 個資外洩 / 機密外洩 / 不正確 AI 輸出造成業務決策錯誤 | 對話被別使用者讀到、agent 把機密寫進 user_memory 共享 | 立即 escalate,< 1h 介入 |
| **T2 High** | AI 輸出明顯違反政策但未造成立即傷害 | refusal 失效、hallucination 高比例、bias 偏移 | < 4h |
| **T3 Medium** | 影響可用性或品質但無安全顧慮 | citation 失效、retrieval 命中率掉、latency 超 SLO | < 1 個工作日 |
| **T4 Low** | 觀察中,需追蹤 | refusal rate 對特定 role 有 < 20% 差異 | 下個季度 review |

---

## 2. 偵測管道

事件可能從以下任一管道觸發:

| 管道 | 自動 / 人工 | 接收人 |
|---|---|---|
| 使用者透過 UI 「回報問題」按鈕 | 人工 | Agent Owner |
| `audit_logs` 異常 pattern | 自動 | Security Lead(待 alerting 補) |
| Daily 評測 dashboard 偏移告警 | 自動 | AI Risk Manager(待補) |
| 資安組通報 | 人工 | Security Lead |
| 員工內部回報 | 人工 | 見 [`roles-responsibilities.md`](./roles-responsibilities.md) §6 |
| 外部稽核發現 | 人工 | AIMS Owner |

---

## 3. 回報通道(A.3.3)

1. **一般回報**:UI 「回報問題」按鈕 → 寫 `audit_logs` action=`incident_report`,通知 Agent Owner
2. **資安相關**:寄信 / 跟 Security Lead 直接聯絡
3. **匿名 / 反映管道**:中科院內部建議信箱(若有);避免報復
4. **緊急(已造成傷害)**:打電話給 AIMS Owner

> **無報復承諾**:任何員工出於善意回報 AI 系統疑慮,不會因此受到不利處置。本承諾由 AIMS Owner 在 [`ai-policy.md`](./ai-policy.md) 簽核時一併確認。

---

## 4. 事件回應流程

```
事件偵測 → 分級(§1)→ 立即遏止 → 根因分析 → 矯正措施 → 文件化 → 對外溝通
```

### 4.1 立即遏止(< 1h for T1)

對應動作清單:

| 情境 | 立即動作 |
|---|---|
| 個資外洩疑慮 | 停掉相關 agent(`agents.health_status = 'disabled'`);保留 `audit_logs` snapshot |
| 模型不當輸出大規模發生 | 在 router 移除該模型 alias;切到 fallback |
| RLS 漏洞疑慮 | 暫停受影響 collection 的 retrieval;告警給所有相關使用者 |
| 卡片驗證異常 | 切回備援登入機制;通知 IT 換 HiPKI session |

執行者:**Security Lead + Agent Owner / Model Owner 共同**

### 4.2 根因分析(RCA)

模板:
- **What happened**:
- **Timeline**:
- **Why it happened**(5 whys):
- **Why detection took N hours**:
- **What broke vs what worked**:

### 4.3 矯正措施(Corrective Action,Clause 10.1)

| 類型 | 範例 |
|---|---|
| 立即修補(patch) | 程式碼 hotfix |
| 流程修補 | PR template / AIIA 補欄位 |
| 訓練修補 | 開發者 awareness 加強 |
| 監控修補 | 加 alert / 加 daily 評測指標 |

每項矯正措施寫入 [`risk-register.md`](./risk-register.md),分配 owner + 完成日期。

### 4.4 對外溝通(A.8.4)

| Tier | 對象 | 時程 |
|---|---|---|
| T1 | 受影響使用者 + 中科院資安組 + AIMS Owner | < 24h |
| T2 | Agent Owner + AI Risk Manager | < 3 個工作日 |
| T3 | 內部週報 | 當週 |
| T4 | 季報 | 下季 |

對外溝通模板:
- 發生了什麼
- 影響範圍(受影響使用者數 / 涉及資料)
- 已採取的立即措施
- 後續矯正計畫 + 預期完成日期
- 連絡窗口

### 4.5 事件文件化

所有 T1 / T2 事件必須寫入 `docs/governance/incidents/<YYYY-MM-DD>-<slug>.md`,結構:

```markdown
# Incident: <one-line summary>

- **Tier**: T1 / T2
- **Detected**: <datetime>
- **Resolved**: <datetime>
- **Affected**: <users / collections / agents>
- **Owner**: <Agent Owner>

## Timeline
## Root cause
## What worked / what broke
## Corrective actions
## Lessons learned
```

---

## 5. 演練(Drill)

- **頻率**:每半年一次
- **範圍**:T1 + T2 各一個情境
- **參與**:AIMS Owner + Security Lead + Agent Owner 代表
- **產出**:演練報告存於 `docs/governance/drills/<YYYY-MM>.md`

---

## 6. 對外通報(A.8.3)

何時需通報中科院外部:

- 個資外洩 + 影響範圍 ≥ N 人(依個資法門檻)→ 24h 內通報主管機關
- 國安級事件 → 立即通報資安組,由資安組對接外部
- ISO 42001 重大不符合 → 半年管理審查時對外揭露

---

## 7. 既有 runbook 引用

本流程與既有 runbooks 互補:

| 既有 runbook | 用途 |
|---|---|
| [`runbooks/rotate-tls-cert.md`](../runbooks/rotate-tls-cert.md) | TLS 私鑰外洩時的緊急輪換 |
| [`runbooks/service-token-cutover.md`](../runbooks/service-token-cutover.md) | Service token 洩漏的緊急切換 |
| [`runbooks/legacy-agent-bootstrap.md`](../runbooks/legacy-agent-bootstrap.md) | Legacy agent 異常的處理 |
| [`runbooks/intranet-deployment-runbook.md`](../runbooks/intranet-deployment-runbook.md) | 內網部署回滾 |

---

**Last updated**: 2026-05-16 · **Owner**: AI Security Lead · **Next review**: 2026-11
