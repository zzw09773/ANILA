# ADR-0007: ModelEndpoint.allowed_task_types 暫緩實作

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的決策紀錄(ADR),保留決策當時的理由與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> Status: accepted
> Date: 2026-07-02
> Deciders: Claude（現場裁決，依文件衝突原則）；待 user 於 doc 04 修訂時覆核
> Related: doc 04 §2 vs §11、Slice 6（r1_0005）

## 背景

doc 04 的 §11 缺口清單列有 `allowed_task_types`（按任務類型限制模型可用性），但 §2 的 ModelEndpoint 目標 schema 並未定義該欄位——文件內部不一致。

## 決策

Slice 6（migration r1_0005）**不加** `allowed_task_types` 欄位。等 doc 04 修訂拍板欄位定義（型別、值域、與 Task.task_type 的對應與 enforcement 點）後，以獨立 migration 補上。

## 理由

- 憲法 §5 准入合約要求功能有明確歸屬與可測收斂；規格不一致時先不做，避免臆測 schema 造成後續遷移負擔。
- 現階段模型准入控制已有 classification_ceiling（本 slice 落地）承擔安全面需求；task-type 級的路由偏好屬產品功能非安全紅線。

## 影響

- doc 04 §11 該項狀態維持「缺口」；roadmap 補列。
- Router 依 task_type 選模型的功能（若future 需要）暫以 Router 既有 dispatch 邏輯處理。

## 憲法檢核

- [x] 不違反 §5（暫緩非新增）
- [x] 不落入 §6 凍結清單
- [x] 不弱化安全不變量
