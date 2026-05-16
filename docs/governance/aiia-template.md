# AI System Impact Assessment(AIIA)範本

> **狀態**:Template v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Clause 6.1.4 + 8.4 + Annex A.5.2 / A.5.3 / A.5.4 / A.5.5 / A.9.2
>
> **使用時機**:
> - 新 agent / 新 model 上線 production 前(**強制**)
> - 既有 agent 重大變更(模型 / 預期用途 / 資料來源變更)
> - 平台層架構重大變更
>
> **存放位置**:填寫完成後存於 `docs/governance/aiia/<agent-or-system-name>.md`,並在 CSP `agents.aiia_doc_path` 欄位記錄(migration 0035 引入)

---

## 0. 觸發此 AIIA 的事件

- [ ] 新 agent 首次上線
- [ ] 新 model 註冊
- [ ] 既有 agent 改變預期用途
- [ ] 既有 agent 切換底層模型
- [ ] 既有 agent 資料來源變更(新增 collection、改 retrieval 策略)
- [ ] 重大事件後檢討(reactive AIIA)

**觸發日期**:YYYY-MM-DD
**填寫人(Assessor)**:
**Reviewer**(必須與填寫人不同):

---

## 1. AI 系統描述

### 1.1 名稱與識別
- **系統名稱**:
- **版本 / commit SHA**:
- **負責單位**:
- **agents.id**(若已註冊):
- **model_registry.id**(若已註冊):

### 1.2 預期用途(Intended Use)
> 一句話描述這個 agent / model **應該被用來做什麼**。

預期用途:

### 1.3 預期使用者
- [ ] 全體中科院員工
- [ ] 特定角色:_____(對應 `required_roles`)
- [ ] 特定卡別:_____(card holder)
- [ ] 其他:_____

### 1.4 不該用於什麼(Out of Scope)
> 明列禁止用途。稽核員會看這一欄判斷「使用者誤用時系統有沒有提醒」。

禁止用途:
1.
2.
3.

---

## 2. 技術組成

| 層 | 元件 | 版本 / 來源 |
|---|---|---|
| Frontend | ANILA_UI / ANILALM / 自建 | |
| Backend agent | fork from `anila-agent` template,commit ___ | |
| Underlying LLM | model_registry id ___,name ___,weights_sha256 ___ | |
| Retrieval(若有) | collection_id ___,chunking 策略 ___,embedding model ___ | |
| 第三方服務 | 列舉所有 outbound calls,對應 `trusted_hosts` | |

---

## 3. 對個人的影響(A.5.5)

### 3.1 涉及的 AI subject
- 對話內容會留存於 `conversations` table:[ ] 是 [ ] 否
- 對話會被萃取成 `user_facts`:[ ] 是 [ ] 否
- 上傳檔案會留存於 ingestion pipeline:[ ] 是 [ ] 否
- 個資處理範圍(複選):
  - [ ] 姓名 / 員編
  - [ ] 卡片 ID(HiPKI)
  - [ ] 電子郵件
  - [ ] 工作內容描述
  - [ ] 業務機密
  - [ ] 其他:_____

### 3.2 對使用者的潛在傷害(harm taxonomy)
評估以下類別的風險等級(N/A / Low / Medium / High):

| 類別 | 風險 | 說明 |
|---|---|---|
| 不正確資訊(hallucination)導致決策錯誤 | | |
| 隱私洩漏(對話被其他使用者讀到) | | |
| 偏見 / 歧視性回應 | | |
| 安全敏感資訊外洩(業務機密) | | |
| 拒絕服務(refusal 過多影響工作) | | |
| 自動化過度(取代應由人判斷的決策) | | |

### 3.3 高風險群影響
是否會對特定弱勢 / 高風險群造成不成比例影響?[ ] 是 [ ] 否
若是,說明:

---

## 4. 對組織的影響

| 面向 | 影響 |
|---|---|
| 法規 / 合規 | |
| 中科院機密分級 | |
| 業務連續性 | |
| 名譽風險 | |
| 對其他系統的下游影響 | |

---

## 5. 控制措施(Mitigations)

對應 §3 / §4 列出的風險,寫對應控制:

| 風險 | 控制措施 | 對應 ISO 42001 Annex A |
|---|---|---|
| | | |

---

## 6. 評測證據(V&V,A.6.2.4)

- **評測跑過的 dataset**:
- **評測 commit SHA**:
- **指標結果**(refusal rate / accuracy / citation precision 等):
- **Reviewer 結論**:

---

## 7. 監控與重審

- **上線後監控指標**(對應 §6):
- **重審觸發條件**:
  - [ ] 每年
  - [ ] 模型版本變更
  - [ ] 收到 N 次以上相關事件回報
  - [ ] 法規變動
- **下次預定 review 日期**:

---

## 8. 結論與簽核

- [ ] **批准上線**(所有 high risk 已有對應控制)
- [ ] **附條件批准**,列出條件:_____
- [ ] **拒絕上線**,理由:_____

| 角色 | 姓名 | 簽核 | 日期 |
|---|---|---|---|
| Assessor 填寫人 | | | |
| Reviewer | | | |
| Agent Owner | | | |
| AI Risk Manager | | | |
| AIMS Owner(僅 high risk 案件) | | | |

---

**Template version**: 1.0 · **Last updated**: 2026-05-16
