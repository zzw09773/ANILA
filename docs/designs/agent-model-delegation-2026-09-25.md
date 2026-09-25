# Agent 模型代理與用量歸屬（2026-09-25）

> 狀態：設計定稿（擁有者 2026-09-25 同意方向），尚未實作。
> 相關：SYSTEM-MAP §7 用量與配額、`docs/designs/agent-quickstart-scaffold-2026-09-22.md`。

## 1. 要解決的問題

1. **用量記錯人**：快速包的 agent 用開發者自己的 `sk-` 呼叫 CSP `/v1`，token 記在開發者名下。SYSTEM-MAP §7 規定 agent 呼叫模型的 token 要**算觸發的使用者**，並同時掛在 agent 上。
2. **重複或失敗的記帳**：CSP 把對話派給 agent 時，又寫一筆 `token_usage`，`model_id` 填的是 agent 的 id。這個欄位的外鍵指向模型表，id 撞到時會記到別的模型，沒撞到時整筆寫入失敗。
3. **權限的矛盾**：使用者有權限用某個 agent，卻不一定有權限用它背後的模型。

## 2. 決定

| 項目 | 決定 |
|---|---|
| 底層模型 | 開發者**註冊時選定**（`agents.base_model_id`），admin 核准的是「agent 加上模型」這個組合 |
| 權限 | **有 agent 權限，就能透過這個 agent 使用它的模型**。提問者不需要另外有該模型的權限 |
| 換模型 | 必須重新送審。CSP 拒絕 agent 呼叫註冊模型以外的模型 |
| 用量 | 記在**提問者**身上，同時標註 agent。報表能回答「某部門透過哪些 agent 用了多少」 |
| 開發者金鑰 | 只在 MLSteam lab 自己測試時使用，套用開發者自己的模型權限 |
| 底層模型下線 | agent 自動標為不可用，通知開發者重新選模型並送審；使用者看到「此助手暫時無法使用」 |

## 3. 流程

```
使用者 → Router → CSP 派工（簽 5 分鐘 RS256 JWT：user_id / department / agent_id）
                     → agent
                          ├─ 搜尋知識庫：帶派工 JWT → CSP /api/ingestion/.../search（已實作）
                          └─ 呼叫模型：帶派工 JWT → CSP /v1/chat/completions（本設計新增）
                                   CSP 驗 JWT → 找到 agent → 檢查 model == agent.base_model
                                   → 轉給模型 → token_usage(user=提問者, agent=agent, model=base_model)
```

## 4. 要改的地方

1. **CSP `/v1/chat/completions` 接受派工 JWT**
   - 驗證方式：驗簽、`aud=anila-agent`、未過期，agent 存在且已核准。
   - 請求裡的 `model` 必須等於 `agent.base_model` 的名稱，否則回 403，並說明「此 agent 只核准使用 X」。
   - 權限檢查**跳過提問者自己的模型權限**，改看「提問者是否有這個 agent 的權限」。這一點在派工時已經檢查過，JWT 本身就是證明。
   - 記帳寫入 `user_id`（提問者）、`department`、`agent_id`、`model_id=base_model_id`。
2. **修正派工時的那筆 `token_usage`**
   - 拿掉「把 agent id 當成 model_id」的寫法。
   - 派工這一跳本身不打模型，不記 token；用量由 agent 實際呼叫模型的那一筆來記。
   - 報表要能用 `agent_id` 加總。
3. **快速包**
   - `llm.py`：收到派工請求時，用這次請求帶來的派工 JWT 當作 Bearer 呼叫 `LLM_BASE_URL`，模型名稱用註冊時選的那一個（由 `deployment.env` 帶入）。
   - 開發者在 lab 自己測試（沒有派工 JWT）時，才使用 `LLM_API_KEY`。
   - README 補一段：上線後用量算提問者，開發者的 key 只用在測試。
4. **底層模型下線的連動**
   - 模型被停用或刪除時：所有綁定它的 agent 標成 `unavailable`（沿用健康狀態欄位或新增原因欄位），並通知 agent 擁有者。
   - Router 派工時略過 `unavailable` 的 agent，使用者看到「此助手暫時無法使用」。
5. **註冊表單**：底層模型的說明改成「此 agent 實際使用的模型；更換需重新送審」。

## 5. 不在範圍內

- **知識庫以提問者的權限檢索**：目前 agent 是以擁有者（開發者）的身分搜尋，知識庫也都是開發者自己的。接人資這類依人區分權限的資料時，另外設計。
- **配額硬停**：目前只計量、不設上限（SYSTEM-MAP §7）。

## 6. 驗收

- 用派工 JWT 呼叫 agent 的註冊模型：回 200，`token_usage` 記在提問者身上並帶 agent_id。
- 呼叫其他模型：回 403。
- JWT 過期或 agent 未核准：回 401 或 403。
- 提問者本身沒有該模型的權限，但有 agent 權限：可以用。
- 停用 base model：agent 變成 unavailable，Router 不再派工給它，使用者看到提示。
- 派工這一跳不再寫出 model_id 錯誤的 `token_usage`。
