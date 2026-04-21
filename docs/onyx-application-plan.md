# Onyx Future Module Application Plan

## Status

`Onyx` 目前在 ANILA 中 **不進入開發排程**。本文件只定義未來模組的應用方向、啟動條件與整合前提，待相關單位 API 完成後再展開技術設計與實作。

目前主線仍然是：

- `AgenticRAG / anila-core`：各單位 agent 開發主線
- `myCSPPlatform`：控制面、權限、審計、usage、agent registry
- `ANILA Core Router`：query routing / dispatch

`Onyx` 僅保留為 future module 候選。

## Intended Role

未來若納入，`Onyx` 的角色是：

- 跨系統 workflow / compliance agent backend
- 協助多輪補欄、規定檢查、表單填寫、最終送單
- 對接多個內部業務系統 API

`Onyx` **不是**：

- 各單位 knowledge agent 的預設底座
- 最終權限判斷來源
- 現階段 MVP 的關鍵依賴

## Target Scenarios

預期適合的場景：

- 請假申請
- 出差 / 加班 / 報銷
- 簽核查詢與送件
- 工單 / 服務申請
- 需要結合規定文件與系統 API 的多步驟任務

典型流程：

1. User 在 ANILA 提出需求，例如「幫我請假」。
2. Router 判斷這是 workflow 類任務，未來可分派給 Onyx module。
3. Onyx 呼叫目標系統 lookup API 取得表單 schema、使用者資訊、可用選項。
4. Onyx 查詢相關規定、SOP、FAQ。
5. Onyx 多輪詢問缺欄位並補齊資料。
6. Onyx 執行 deterministic validation，必要時再做 LLM-assisted 規則輔助判讀。
7. Onyx 在送出前要求 user 最終確認。
8. Onyx 呼叫 submit API 完成送件。
9. CSP 與目標系統保留 audit correlation 與執行紀錄。

## Prerequisites

啟動 Onyx 模組前，相關單位至少需要提供：

- 穩定的 lookup API
- 穩定的 submit / action API
- 明確的 request / response schema
- 錯誤碼與失敗語意
- 認證方式與 service credential 發放機制
- idempotency / retry 規範
- audit / trace 欄位需求

若上述任一項尚未成熟，Onyx 模組不開工。

## Design Constraints

未來整合時必須遵守：

- `Onyx` 不是最終權限來源；真正授權仍由 CSP、service credential 與目標系統共同承擔
- 硬性業務規則應盡量 deterministic，不只依賴 LLM
- 所有 mutating action 都要有最終確認步驟
- API 呼叫與 workflow 狀態要可審計、可追蹤、可重試
- 不能讓 `Onyx` 取代 `AgenticRAG` 成為所有單位 agent 的通用基底

## Start Criteria

只有在以下條件同時成立時，才重新啟動 Onyx 模組規劃：

- 至少一個代表性內部系統 API 已可供整合
- 該系統的 lookup / submit / auth 契約已穩定
- 相關規定文件與流程已可整理成可維護的知識來源
- ANILA 主線 Phase 1-3 已穩定
- 團隊確認此需求屬於 workflow orchestration，而不是單純 knowledge agent

## Next Step When Ready

條件成熟後，再新增一版技術設計文件，內容至少包含：

- Router → Onyx 的 dispatch contract
- API adapter / auth layer 設計
- form state / confirmation / retry 模型
- audit correlation 設計
- 代表性 use case 的 end-to-end 驗證案例
