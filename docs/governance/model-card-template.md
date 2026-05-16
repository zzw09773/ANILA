# Model Card 範本

> **狀態**:Template v1(2026-05-16)
>
> **對應 ISO/IEC 42001:2023**:Annex A.6.2.7(technical documentation)
>
> **使用時機**:任何模型寫入 `model_registry` 前 **必須** 完成本 model card。
>
> **存放位置**:`docs/governance/model-cards/<model-name>-<version>.md`,並在 `model_registry.model_card_url` 欄位記錄(migration 0035 引入)

---

## 1. 模型識別

- **Model name**:
- **Version**:
- **`model_registry.id`**:
- **`weights_sha256`**(8 位前綴):
- **註冊日期**:
- **Model Owner**(對口窗口):

---

## 2. 模型來源(Provenance)

- [ ] 上游公開 release(填 release URL + license):
- [ ] 中科院內部 fine-tune(填上游 base model + fine-tune 資料來源):
- [ ] 中科院內部 from-scratch 訓練(填 dataset + training pipeline 文件):
- [ ] 其他:

**License**:
**License 文件存放路徑**:

---

## 3. 預期用途(Intended Use)

### 3.1 主要用途
> 一句話描述此模型 **應該被用來做什麼**。

### 3.2 預期使用者群體
- [ ] 平台層 chat completion(任何 agent 都可呼叫)
- [ ] 特定 agent 專屬:_____
- [ ] 僅平台內部使用(non-user-facing,如 embedding / reranker)

### 3.3 不該用於什麼(Out of Scope)
1.
2.

---

## 4. 訓練資料(若適用)

> 若是上游公開模型,直接連到上游 model card;以下欄位填「見 upstream」即可。
> 若是中科院 fine-tune,**必須** 填詳細欄位。

| 欄位 | 內容 |
|---|---|
| Training dataset 名稱 | |
| 資料來源 | |
| Dataset 大小 | |
| 資料時間範圍 | |
| 是否含個資 | |
| 個資處理方式 | |
| 是否含敏感類別(健康 / 政治 / 種族) | |

---

## 5. 效能(Performance)

### 5.1 評測 benchmark(若適用)

| Benchmark | 分數 | 評測日期 |
|---|---|---|
| | | |

### 5.2 內部評測(ANILA-specific)

| Test set | 指標 | 分數 |
|---|---|---|
| | | |

### 5.3 已知限制(Limitations)
1.
2.
3.

---

## 6. 偏誤與公平性(Bias & Fairness)

- 已知偏誤類型:
- 跨群差異測試結果(若有):
- 緩解措施:

---

## 7. 安全考量(Safety)

- 已知 prompt injection / jailbreak 漏洞:
- Content safety filter 是否啟用:[ ] 是 [ ] 否
- 對敏感 query 的 refusal 策略:

---

## 8. 環境足跡(可選,Annex A.5.5)

- 推論單次 token 能耗估算:
- 部署 GPU 規格:

---

## 9. 維護與更新

- **下次預定 review 日期**:
- **退役條件**(何時應從 prod 移除):
- **替代方案**:

---

## 10. 連絡

- **技術問題**:Model Owner ___
- **濫用回報**:[`ai-incident-response.md`](./ai-incident-response.md)

---

**Template version**: 1.0 · **Last updated**: 2026-05-16
