# Gate 2 分類回填與全量 reconciliation

本程序是 G1b 的資料維護窗口。它只證明既有分類欄位與 document/chunk
繼承關係完整；不會核發 clearance，也不代表 G2 search gate 可以略過資料
owner 的語意簽核。

## 前置條件

1. 部署映像必須已包含 G1 writer contract：CSP 上傳會鎖定 collection 並讓
   document 繼承其分類；worker 會從 document/collection 取 max，parent/leaf
   chunk 都會寫入 `classification_level`、latched time 與 source；不得在舊
   CSP/worker 上單獨執行 `r1_0011`。先在同一個 revision 執行：

   ```bash
   python -m unittest infra.policy.tests.test_gate2_classification_reconciliation -v
   (cd packages/anila-core && python -m pytest tests/test_collection_scoped_pgvector_store.py -q)
   (cd services/ingestion-worker && python -m pytest tests/test_handlers_helpers.py -q)
   (cd services/csp && python -m pytest tests/test_ingestion_enqueue_rollback.py -q)
   ```

2. 進入維護窗口，停止 CSP API 與所有 ingestion worker，確認沒有 document、
   collection、chunk、callback、registry 或 export writer。migration 仍會以 DB
   table lock 阻擋漏網的並行寫入，但停機可避免 writer 長時間等待。
3. 完成 PostgreSQL 備份並記錄可還原的 backup ID。
4. 使用 migration role；該 role 必須是 superuser 或具 `BYPASSRLS`，不可用
   runtime `csp_app`，否則 FORCE RLS 會讓證據不完整。
5. 把證據輸出到 repo 外的受控目錄，不要提交資料庫 URL、內容、filename 或
   使用者識別。

## 執行

```bash
export MIGRATION_DATABASE_URL='postgresql://<migration-role>:<secret>@<db>/csp'
cd services/csp
python -m alembic upgrade head
cd ../..
python infra/policy/gate2/check_classification_reconciliation.py \
  --sample-size 20 \
  --output /var/lib/anila/evidence/gate2-classification-reconciliation.json
```

`r1_0011` 會先鎖定所有 classification tables，再檢查非法字串、
orphan/cross-collection chunk 與 cross-service callback；任一筆存在便
整筆 rollback，不會把未知資料猜成「無機密」。合法但低於來源的資料只會向上
提升：document 至少等於 collection，chunk 等於 existing/document/collection
三者最大值，callback 等於 payload/既有值與 launch 的最大值。缺 ceiling 會保守
回填成「無機密」；無法追溯 launch 的歷史 callback 會保守回填「絕對機密」。
歷史 export 缺 target floor 時，以該次 artifact classification 作為最小一致值。

## 必須全綠的機器證據

- 每個 manifest 欄位 `expected_rows == actual_rows`
- `null_count == 0`
- `invalid_count == 0`
- document/chunk `actual_joined_rows == expected_rows`
- orphan/cross-collection rows 為 0
- document 低於 collection 為 0
- chunk 低於 document 或 collection 為 0
- linked callback 低於 launch 或跨 service 為 0
- Alembic database current 與唯一 source head 相同，且該 head 的 ancestry
  必須包含 `r1_0011`（後續 Gate 2 migration 不需把 checker 釘回舊 head）
- 每個五級值 CHECK constraint 存在且 `convalidated=true`
- ceiling、callback level 與 export target floor 都是 DB `NOT NULL`，且 server
  default 為明確「無機密」
- checker 證據來自單一 `REPEATABLE READ READ ONLY` snapshot

checker 的 semantic sample 只列 numeric row ID 與各層分類，不讀內容、filename
或 user identity。data owner 必須用另一路徑核對抽樣語意並在 evidence ticket/PR
簽核；抽樣不能取代上述全量計數。

## 失敗處理

- exit 2：工具、權限、連線或 schema 問題；不得把部分輸出當證據。
- exit 1：資料仍不合規；維持 G2 read gate 關閉，依 data owner 核准的 mapping
  修正後重跑。
- migration preflight 失敗：Alembic version 會留在 `r1_0010`。先匯出「row ID +
  欄位 + 當前值」到受控 incident evidence（不得含內容），由 data owner 決定
  正確等級，再重跑。

不得用 downgrade 降低已回填分類。若 migration 後需要復原，使用維護窗口開始前
的完整備份；`alembic downgrade r1_0010` 只移除本 revision 的 CHECK、NOT NULL
與 server-default schema guards，刻意不把任何已回填或提升的資料降級。
