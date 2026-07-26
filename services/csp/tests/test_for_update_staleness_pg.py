# -*- coding: utf-8 -*-
"""`SELECT ... FOR UPDATE` 沒有 `populate_existing()` 就讀到舊值 —— 真 PG 守門。

## 這支證明什麼

`retention_reaper` 在**不可逆刪檔之前**會重讀一次列並檢查 `legal_hold`。那次重讀
的全部意義就是「拿到最新值再決定」。但 SQLAlchemy 的 Query 若命中 identity map
且物件未過期,**會丟棄剛 SELECT 回來的列值,回傳記憶體舊值** —— 鎖還在,讀到的
是舊值。`populate_existing()` 是唯一的強制覆寫。

## 為什麼既有的守門測試抓不到

`test_retention_lifecycle.py::test_hold_created_after_erase_due_marker_wins_before_unlink`
看起來守的正是這件事,但它的 `establish_hold(session)` 用的是**同一個 session**
—— 同 session 的寫入本來就在 identity map 裡,所以它在有沒有 `populate_existing()`
的兩種情況下都會通過。**「全套件 FAILED 清單逐字相同」因此不構成安全證據。**

而且 SQLite + StaticPool 的測試套件在**物理上**測不到跨 session 的列鎖 TOCTOU:
所有 session 共用同一條連線,沒有兩個獨立交易可言。所以這支只在真 PG 下跑。

## 一個比先前理解更嚴重的事實(2026-07-26 實測修正)

先前的判讀是「`expire_on_commit=False`(工單 G / PR #51)會擊穿這條防線」,言下
之意是本分支(未設該旗標 → 預設 `True`)是安全的。**實測證明那個判讀太窄。**

`expire_on_commit=True` 只在**自己 commit** 時讓物件過期。若 session A 先讀過列、
session B 改了並 commit、A 在**沒有自己 commit** 的情況下重讀 —— A 照樣拿到舊值。
reaper 的形狀正是這樣(先掃出候選、再逐筆加鎖重讀)。

實測(真 PG、兩個獨立 session、`expire_on_commit=True`):

    不加 populate_existing()   legal_hold_seen=False → 照樣刪除(BUG)
    加   populate_existing()   legal_hold_seen=True  → 擋下刪除(正確)

所以那個旗標不是啟用條件,只是**放大器**。`populate_existing()` 在任何「重讀以
做決定」的地方都是必要的,與旗標無關。
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "ANILA_TEST_PG_DSN / TEST_POSTGRES_URL 未設 —— 這條 TOCTOU 需要兩個獨立"
        "交易,SQLite + StaticPool 在物理上測不到"
    ),
)


@pytest.fixture(scope="module")
def pg_engine():
    from app.database import Base
    import app.models  # noqa: F401 — 註冊所有 mapper

    engine = create_engine(_DSN)
    # 只建這支需要的表會撞 FK,所以整套建起來;module scope 攤平成本。
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seed_version(engine):
    """建一份 erase_due 且 legal_hold=False 的 ArtifactVersion,回傳 id。"""
    from app.models.artifact import Artifact, ArtifactVersion
    from app.models.user import User
    from app.utils.security import hash_password

    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        owner = User(
            # 每次呼叫都要唯一 —— parametrize 會跑兩輪,固定名字第二輪撞 unique
            username=f"toctou-{uuid.uuid4().hex[:12]}",
            hashed_password=hash_password("x"),
            role="user",
            is_active=True,
            is_approved=True,
        )
        s.add(owner)
        s.flush()
        art = Artifact(
            title="toctou",
            owner_user_id=owner.id,
            classification_level="無機密",
            artifact_type="document",
        )
        s.add(art)
        s.flush()
        ver = ArtifactVersion(
            artifact_id=art.id,
            version=1,
            classification_level="無機密",
            legal_hold=False,
        )
        s.add(ver)
        s.commit()
        return ver.id
    finally:
        s.close()


@pytest.mark.parametrize("use_populate", [False, True])
def test_reread_sees_other_session_hold_only_with_populate_existing(
    pg_engine, use_populate
):
    """兩個獨立 session:B 建立 legal hold 後,A 的加鎖重讀看不看得到。

    這條**刻意**同時斷言「不加會看不到」與「加了才看得到」—— 只斷言後者的話,
    測試無法證明它真的在測那個機制(可能只是 DB 剛好回對的值)。
    """
    from app.models.artifact import ArtifactVersion

    version_id = _seed_version(pg_engine)
    SessionA = sessionmaker(bind=pg_engine)
    SessionB = sessionmaker(bind=pg_engine)
    a, b = SessionA(), SessionB()
    try:
        # A 先讀一次(reaper 先掃出候選列 → 物件進 identity map)
        first = a.query(ArtifactVersion).filter(
            ArtifactVersion.id == version_id
        ).one()
        assert first.legal_hold is False

        # B 在另一個交易裡建立 legal hold 並 commit
        row_b = b.query(ArtifactVersion).filter(
            ArtifactVersion.id == version_id
        ).one()
        row_b.legal_hold = True
        row_b.legal_hold_reason = "TOCTOU-GUARD"
        b.commit()

        # A 加鎖重讀,準備決定要不要不可逆刪檔。A **沒有**自己 commit 過。
        query = a.query(ArtifactVersion).filter(
            ArtifactVersion.id == version_id
        ).with_for_update()
        if use_populate:
            query = query.populate_existing()
        again = query.one()

        if use_populate:
            assert again.legal_hold is True, (
                "加了 populate_existing() 還是讀到舊值 —— 這條防線整個失效了"
            )
        else:
            assert again.legal_hold is False, (
                "不加 populate_existing() 竟然讀到新值。若 SQLAlchemy 的語意變了,"
                "這是好消息,但要重新評估 infra/ci/check_for_update_populate.py "
                "這個 gate 還有沒有必要 —— 不要直接刪掉它,先確認新語意在所有"
                "支援版本上都成立。"
            )
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()


def test_retention_reaper_erase_path_rereads_with_populate_existing():
    """靜態守門:reaper 的刪檔路徑上每個 with_for_update 都要有 populate_existing。

    行為測試需要真 PG 才跑得動,而這條不需要 —— 它讀原始碼。兩者互補:
    上面那條證明**機制**,這條保證**這個檔案**沒有漏網的站點。
    """
    import ast
    from pathlib import Path

    target = Path(__file__).resolve().parents[1] / "app" / "services" / "retention_reaper.py"
    tree = ast.parse(target.read_text(encoding="utf-8"))
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "with_for_update"):
            continue
        outer = node
        while True:
            par = parents.get(id(outer))
            if isinstance(par, ast.Attribute) and par.value is outer:
                outer = par
                continue
            if isinstance(par, ast.Call) and par.func is outer:
                outer = par
                continue
            break
        names = []
        cur = outer
        while True:
            if isinstance(cur, ast.Call):
                cur = cur.func
            elif isinstance(cur, ast.Attribute):
                names.append(cur.attr)
                cur = cur.value
            elif isinstance(cur, ast.Name):
                names.append(cur.id)
                break
            else:
                break
        if "populate_existing" not in names:
            missing.append(node.lineno)

    assert missing == [], (
        f"retention_reaper.py 有 {len(missing)} 個 with_for_update 缺 "
        f"populate_existing(行 {missing})—— 那是不可逆刪檔的路徑"
    )
