# -*- coding: utf-8 -*-
"""治理帳 + api_keys 的 timestamp 欄轉 timestamptz —— W2-10 批次 1(子計畫 C1)。

Revision ID: r1_0040
Revises: r1_0038
Create Date: 2026-07-26

⚠ ``down_revision`` 暫指 ``r1_0038``:另一支並行工作正在建 ``r1_0039``,合併時
由整合者改成 ``r1_0039``(本檔與 r1_0039 無資料相依,順序可任意)。

════════════════════════════════════════════════════════════════════════════
判讀脈絡段(C1 §c 要求逐字寫進 migration,不是只寫在計畫裡)
════════════════════════════════════════════════════════════════════════════

**這一段刻意寫在 migration 檔頭而不是只寫在補救計畫裡。** 理由:未來做稽核複查
的人打開這支 migration 時,如果只看到一個沒有脈絡的 ``AT TIME ZONE 'UTC'``,
他既無法判斷那是深思後的決定還是手滑,也無法自行評估治理紀錄
的時點可不可信。所以決定、決策軌跡、以及可量測的後果三件事全部留在這裡。

① **判讀決定:既有 naive 值一律視同 UTC 牆鐘**,轉換語句是
   ``ALTER COLUMN <col> TYPE timestamptz USING <col> AT TIME ZONE 'UTC'``。
   決策軌跡(兩次拍板、方向相反,皆留痕):
     * 2026-07-26:user 拍板「視同 UTC+8(台北牆鐘)」(經一次反對意見後重申)。
     * 2026-07-27:實作者以具體例子重新說明「顯示層一律 UTC+8」與「舊值原本
       是哪個時區的牆鐘」是兩個獨立的決定,且台北判讀會讓歷史紀錄的顯示時間
       比事件實際發生時刻早 8 小時;user 改拍板「**判讀為 UTC**」。本檔依此執行。
   決定紀錄在 ``docs/planning/platform-remediation-plan-2026-07-26.md`` 子計畫
   C1 §c 的決策表(儲存/遷移層那一列)與修訂紀錄。

② **判讀依據:兩條寫入路徑皆為 UTC**(2026-07-26 複驗;此清單原是實作者的
   反對意見證據,2026-07-27 改判後成為本判讀的依據):

       services/csp/app/  datetime.now(timezone.utc)  → 210 處
                          datetime.utcnow()           →   1 處(同為 UTC 牆鐘)
                          裸 datetime.now()           →   0 處
                          datetime.now(ZoneInfo/pytz) →   0 處
       容器:platform.yml 僅 n8n / gitlab 設 TZ,csp 與 csp-db 皆 UTC
       DB 層:classification_events / declassification_requests /
              classification_authority_assignments 的 created_at 是
              server_default CURRENT_TIMESTAMP、policy_decisions 是 now();
              而 DB session TimeZone = Etc/UTC → 這條路徑存進 naive 欄的也是
              UTC 牆鐘。

   也就是說**兩條獨立寫入路徑(Python 211 處 + DB server_default)都是 UTC**,
   沒有第三條路徑。既有 naive 值即 UTC 牆鐘,``AT TIME ZONE 'UTC'`` 的轉換
   保持每一列的絕對時點不變。
   ``app/time_utils.py:as_utc()`` 的 docstring 寫「naive → 視同 UTC」(W1-4
   的邊界止血),與本判讀**一致** —— 07-26 版本曾刻意記錄兩者矛盾,07-27 改判
   後矛盾消失,repo 內只剩一種判讀。

③ **本判讀下,遷移前後的紀錄在絕對時點上連續、無平移。**
   naive 值是 UTC 牆鐘、按 UTC 判讀 → 每一列的絕對時點在轉換前後相同
   (例如 naive ``2026-07-26 03:00:00`` → ``2026-07-26T03:00:00+00:00``);
   本 migration 之**後**由 ``datetime.now(timezone.utc)`` 寫入的是真 UTC,
   同一語意。顯示層(W1-4④ 共用 formatter)再以 ``Asia/Taipei`` 呈現,
   使用者看到的一律是正確的台灣時間。

   因此不產生時點失真的治理帳(法律證據性質):
     * ``classification_events``                  分類異動 ledger
     * ``declassification_requests``              雙人降密核准(申請/決議時點)
     * ``classification_authority_assignments``   公文文號權責指派
     * ``policy_decisions``                       政策裁決 ledger
     * ``export_records``                         匯出紀錄(外流證據)

   若後續要改採台北判讀(不建議 —— 會讓上述治理帳的歷史時點往前平移 8 小時,
   顯示時間比事件實際發生時刻早 8 小時),把本檔的 ``_INTERPRETATION_TZ`` 改成
   ``'Asia/Taipei'`` 即可 —— 但**必須在本 migration 於該部署執行之前**;執行後
   要改需另寫補償 migration(雙重 ``AT TIME ZONE`` 轉換),而那會在同一欄裡
   堆出不連續。

════════════════════════════════════════════════════════════════════════════
範圍:批次 1(治理帳 + api_keys)
════════════════════════════════════════════════════════════════════════════

C1 §a 的分批原則是「小表、法律證據優先」。本批共 12 欄,分成兩組,**兩組的處理
方式不同**:

**(A) 乾淨 alembic 鏈上是 ``timestamp without time zone`` 的 9 欄** —— 由本
migration 轉型,是唯一會發生 8 小時重新判讀的一組:

    api_keys.expires_at
    classification_authority_assignments.created_at
    classification_authority_assignments.revoked_at
    classification_events.created_at
    declassification_requests.created_at
    declassification_requests.decided_at
    export_records.classification_latched_at
    export_records.created_at
    policy_decisions.created_at

**(B) 乾淨 alembic 鏈上**已經**是 ``timestamptz`` 的 3 欄** —— DB 側早就正確
(``0001`` 建成 timestamptz),錯的是 ORM 宣告(``Column(DateTime)``):

    api_keys.created_at
    api_keys.last_used_at
    audit_logs.created_at

    這 3 欄的既有值是 aware ``datetime.now(timezone.utc)`` 寫進 timestamptz,
    **絕對時點本來就是對的,不做也不需要任何轉換**。本 migration 只在某個部署
    的實庫發現它們是 naive 時(歷史 startup DDL 曾以 ``TIMESTAMP`` 建欄)才
    順手收斂,並留 WARNING。``downgrade()`` **刻意不動這 3 欄**:乾淨鏈上它們
    在本 migration 之前就是 timestamptz,把它們降成 naive 是製造新缺陷,而不是
    還原。這個不對稱是刻意的,也是 ``pg_dump -s`` 能逐字元還原的前提。

**批次 2 不在本包**:``messages`` / ``document_chunks`` / ``ingestion_*`` 等大表
的 naive 欄另開維護窗 —— ``ALTER TYPE ... USING`` 會**全表重寫並持有 ACCESS
EXCLUSIVE 鎖**,鎖時長 ∝ 表大小,必須先在還原副本上量測再排窗(C1 §a 批次 2)。

════════════════════════════════════════════════════════════════════════════
⛔ 連帶鐵則:csp-db 容器 TZ 在本批欄轉完前必須維持 UTC
════════════════════════════════════════════════════════════════════════════

``classification_events`` / ``declassification_requests`` /
``classification_authority_assignments`` 的 ``created_at`` 有
``server_default CURRENT_TIMESTAMP``、``policy_decisions.created_at`` 是
``now()``。``CURRENT_TIMESTAMP`` 本身是 timestamptz,寫進 ``timestamp without
time zone`` 欄時**按 session TZ 轉換**。所以只要 DB session TZ 從 ``Etc/UTC``
變成 ``Asia/Taipei``,這條路徑就開始存本地時間,而同一欄的舊值是 UTC ——
**同一個欄位混兩種語意,且資料裡沒有任何標記能區分**。

這條鐵則同時寫進 ``.env.example`` 與 ``infra/compose/platform.yml`` /
``infra/compose/dev.yml`` 的 csp-db 區塊(W2-10 加的註解與 ``TZ: Etc/UTC``)。
本批 12 欄轉成 timestamptz 之後,對這 12 欄而言鐵則自然解除(timestamptz 欄的
``CURRENT_TIMESTAMP`` 存的是絕對時點,與 session TZ 無關);但**批次 2 的欄還
沒轉**,所以鐵則整體仍然有效,直到 C1 全部批次做完。

════════════════════════════════════════════════════════════════════════════
鎖與耗時
════════════════════════════════════════════════════════════════════════════

``ALTER COLUMN ... TYPE ... USING`` 對每個被改的表取 ACCESS EXCLUSIVE 鎖並重寫
全表。本批全是小表。實測環境:throwaway ``pgvector/pgvector:pg16``、WSL2、
乾淨 alembic 鏈(2026-07-26):

    空表(9 欄 / 6 張表)                          整支 upgrade      0.319 s
    audit_logs 20,000 列 + 其餘六表各 5,000 列     整支 upgrade      0.456 s
                                                  整支 downgrade    0.141 s
    逐條 ALTER(同資料量,psql \\timing)          7.7 ms – 19.0 ms
      最慢:api_keys.expires_at 18.96 ms / declassification_requests 17.4 ms

    → 全部遠低於 C1 §g ④ 的 5 s 門檻,批次 1 的組成不需調整。
    ⚠ 這是 dev 級資料量。``.15`` 生產庫的 ``audit_logs`` 快照是 17,178 列
      (同一量級),其餘治理帳在 dev 是 ``reltuples = -1``(未 ANALYZE 或空表)
      → 生產受影響筆數報表仍須另外對 ``.15`` 產出(C1「執行前置」第 1 項)。

``lock_timeout`` 在本 migration 內明示設為 15 s:alembic 的
``MIGRATION_DATABASE_URL`` **不繼承** runtime engine 的 ``lock_timeout=5000ms``
(C1 §d,``app/config.py`` 只掛 app engine),不設的話這支會無限期排在某個長
交易後面,而它一旦排隊就把治理帳整表的讀寫全部擋住。設了之後撞鎖是**立刻失敗
並整支回滾**,由人改排維護窗 —— 那是刻意選的行為。

════════════════════════════════════════════════════════════════════════════
例外掃描(C1 §c:不受判讀選擇影響,一律要做)
════════════════════════════════════════════════════════════════════════════

``upgrade()`` 在轉型**之前**跑 ``_exception_scan()``,對本批的 naive 欄找兩種
可疑值並以 WARNING 記錄(**不阻斷**,C1 §c 的原話是「標記人工審」):

  * 值 > ``now() + 1h``:治理帳的 ``created_at`` / ``decided_at`` /
    ``revoked_at`` / ``classification_latched_at`` 不該有未來時點。
  * ``api_keys.expires_at`` 逐筆列出:它是**唯一來自 client request body** 的
    欄(``schemas/api_key.py:13``),若曾有 client 送裸台北本地時間,``UTC``
    判讀對那些列會偏 8 小時(其餘伺服器寫入的列則正確)。C1 §c 要求它在生產
    執行前**逐筆列出人工審**,而那份清單只能對 ``.15`` 生產庫產出 —— 本
    migration 只負責把它印出來,不代替那份簽核(見 PR 說明的「未確認」欄)。
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0040"
# ⚠ 合併時改成 "r1_0039"(見檔頭)。
down_revision: Union[str, None] = "r1_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


logger = logging.getLogger("alembic.runtime.migration")

# 既有 naive 值的判讀時區 —— 2026-07-27 user 拍板改為 UTC(決策軌跡見檔頭 ①)。
# upgrade 與 downgrade **共用同一個常數**,所以兩向一定對稱:
#   naive --AT TIME ZONE 'UTC'--> timestamptz
#   timestamptz --AT TIME ZONE 'UTC'--> naive(取 UTC 牆鐘)
# 這組互為反函式,資訊無損。
_INTERPRETATION_TZ = "UTC"

# 撞鎖就立刻失敗,不要排隊擋住治理帳整表(見檔頭「鎖與耗時」)。
_LOCK_TIMEOUT = "15s"

# (A) 乾淨鏈上是 naive、由本 migration 轉型的 9 欄。downgrade 逐一還原。
_CONVERT: tuple[tuple[str, str], ...] = (
    ("api_keys", "expires_at"),
    ("classification_authority_assignments", "created_at"),
    ("classification_authority_assignments", "revoked_at"),
    ("classification_events", "created_at"),
    ("declassification_requests", "created_at"),
    ("declassification_requests", "decided_at"),
    ("export_records", "classification_latched_at"),
    ("export_records", "created_at"),
    ("policy_decisions", "created_at"),
)

# (B) 乾淨鏈上已是 timestamptz、只有 ORM 宣告要修的 3 欄。
# upgrade 只在實庫發現它們是 naive 時收斂(留 WARNING);downgrade 不動。
_ALREADY_TZ: tuple[tuple[str, str], ...] = (
    ("api_keys", "created_at"),
    ("api_keys", "last_used_at"),
    ("audit_logs", "created_at"),
)

# 例外掃描:治理帳裡「不該出現未來時點」的欄。
_NO_FUTURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("classification_authority_assignments", "created_at"),
    ("classification_authority_assignments", "revoked_at"),
    ("classification_events", "created_at"),
    ("declassification_requests", "created_at"),
    ("declassification_requests", "decided_at"),
    ("export_records", "classification_latched_at"),
    ("export_records", "created_at"),
    ("policy_decisions", "created_at"),
    ("audit_logs", "created_at"),
)


def _column_tz_state(bind, table: str, column: str) -> str | None:
    """回傳 ``"naive"`` / ``"aware"`` / ``None``(表或欄不存在)。

    刻意查 ``information_schema`` 而不是 SQLAlchemy 的 reflection:
    ``str(reflected_type)`` 在預設 dialect 下對兩種 timestamp 都輸出
    ``'TIMESTAMP'``,分不出時區屬性(這個陷阱同時是 ``check_orm_pg_drift.py``
    的 tz 比對失真的原因,見本 PR 對 drift baseline 的說明)。
    """
    row = bind.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            "   AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    if row is None:
        return None
    return "aware" if row == "timestamp with time zone" else "naive"


def _exception_scan(bind) -> None:
    """C1 §c 的例外掃描 —— 只記錄,不阻斷(原話是「標記人工審」)。"""
    for table, column in _NO_FUTURE_COLUMNS:
        state = _column_tz_state(bind, table, column)
        if state is None:
            continue
        # 門檻要跟欄位型別同型,否則 PG 會拿 session TZ 去隱式轉換,掃描本身就
        # 帶上了它想找的那種模糊。
        threshold = (
            "now() + interval '1 hour'"
            if state == "aware"
            else "(now() AT TIME ZONE 'UTC') + interval '1 hour'"
        )
        suspicious = bind.execute(
            sa.text(
                f'SELECT count(*) FROM "{table}"'  # noqa: S608 — 表/欄名來自本檔常數
                f' WHERE "{column}" > {threshold}'
            )
        ).scalar_one()
        if suspicious:
            logger.warning(
                "W2-10 例外掃描:%s.%s 有 %d 列的時點在 now()+1h 之後 —— "
                "治理帳不該有未來時點,請人工審(C1 §c)。本 migration 不阻斷。",
                table,
                column,
                suspicious,
            )

    # api_keys.expires_at:唯一來自 client request body 的欄,C1 §c 要求逐筆列出。
    if _column_tz_state(bind, "api_keys", "expires_at") is not None:
        rows = bind.execute(
            sa.text(
                "SELECT id, key_prefix, expires_at, created_at FROM api_keys"
                " WHERE expires_at IS NOT NULL ORDER BY id"
            )
        ).all()
        if rows:
            logger.warning(
                "W2-10 例外掃描:api_keys.expires_at 有 %d 列非 NULL,逐筆列出"
                "(C1 §c —— 這個欄的值來自 client,若曾有 client 送裸本地時間,"
                "'UTC' 判讀對那些列會偏 8 小時):",
                len(rows),
            )
            for row in rows:
                logger.warning(
                    "  api_keys id=%s prefix=%s expires_at=%s created_at=%s",
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                )
        else:
            logger.info("W2-10 例外掃描:api_keys.expires_at 無非 NULL 列。")


def _alter(table: str, column: str, *, to_aware: bool) -> None:
    """兩向都用同一個 ``AT TIME ZONE`` 常數 —— 對稱、資訊無損。"""
    if to_aware:
        # naive 牆鐘 → 絕對時點(把牆鐘當 UTC 解讀)
        new_type = "timestamptz"
    else:
        # 絕對時點 → naive 牆鐘(取 UTC 牆鐘)
        new_type = "timestamp"
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}"'  # noqa: S608 — 常數
        f" TYPE {new_type}"
        f" USING \"{column}\" AT TIME ZONE '{_INTERPRETATION_TZ}'"
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    _exception_scan(bind)

    for table, column in _CONVERT:
        state = _column_tz_state(bind, table, column)
        if state is None:
            # 表不存在 = 這個部署還沒跑到建表的 migration,不該發生;留紀錄後跳過,
            # 不要讓 DR 還原因為一個欄位型別停在半路。
            logger.warning("W2-10:%s.%s 不存在,跳過。", table, column)
            continue
        if state == "aware":
            logger.info("W2-10:%s.%s 已是 timestamptz,跳過(冪等)。", table, column)
            continue
        _alter(table, column, to_aware=True)

    for table, column in _ALREADY_TZ:
        state = _column_tz_state(bind, table, column)
        if state is None:
            logger.warning("W2-10:%s.%s 不存在,跳過。", table, column)
            continue
        if state == "aware":
            # 乾淨鏈上的正常路徑。
            continue
        # 這個部署的欄被歷史 startup DDL 建成 TIMESTAMP。順手收斂,但要留痕:
        # 它的既有值也會按 _INTERPRETATION_TZ 重新判讀,而檔頭 (B) 那段說的
        # 「不需要轉換」對這個部署不成立。
        logger.warning(
            "W2-10:%s.%s 在本部署是 naive(乾淨 alembic 鏈上是 timestamptz)"
            " —— 一併轉型,其既有值同樣按 '%s' 重新判讀。",
            table,
            column,
            _INTERPRETATION_TZ,
        )
        _alter(table, column, to_aware=True)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")

    # 只還原 (A) 那 9 欄。(B) 的 3 欄在本 migration 之前(乾淨鏈上)就是
    # timestamptz,降成 naive 是製造新缺陷而不是還原 —— 也是 `pg_dump -s`
    # 能逐字元對回 upgrade 前的前提。
    for table, column in _CONVERT:
        state = _column_tz_state(bind, table, column)
        if state is None:
            logger.warning("W2-10 downgrade:%s.%s 不存在,跳過。", table, column)
            continue
        if state == "naive":
            logger.info(
                "W2-10 downgrade:%s.%s 已是 naive,跳過(冪等)。", table, column
            )
            continue
        _alter(table, column, to_aware=False)
