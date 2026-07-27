"""upload_zip must not freeze the single uvicorn event loop.

CSP runs one worker with no --workers. Per-member zlib / sniff / sha256 /
disk I/O used to run inline inside ``async def upload_zip`` with zero awaits,
so a large knowledge-base zip stalled every concurrent coroutine (chat SSE
included). These tests lock the off-loop contract.
"""

from __future__ import annotations

import asyncio
import time
import zipfile
from io import BytesIO

import pytest
from fastapi import UploadFile

from app.api.ingestion import documents
from app.models.ingestion import IngestionCollection, IngestionDocument
from tests.conftest import make_user


def _collection(db, user, *, name: str = "zip-off-loop") -> IngestionCollection:
    collection = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embedding",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4,
        created_by=user.id,
        classification_level="無機密",
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return archive.getvalue()


@pytest.mark.asyncio
async def test_upload_zip_keeps_event_loop_schedulable_during_member_io(
    tmp_path, db, monkeypatch
) -> None:
    """Probe must keep ticking **while a member's CPU/IO is running**.

    Inject a 350ms sync sleep into ``_persist_blob``. If staging still ran on
    the event-loop thread, every ``asyncio.sleep(0.02)`` overlapping that
    window would stall past the 100ms budget (the pre-fix red failure). With
    ``asyncio.to_thread`` the probe ticks on schedule throughout.

    ⚠ 這條刻意只量「staging 視窗內」的停頓,不是整個請求。原本寫成
    ``assert stalls == []``(全程零停頓),而那條斷言**超出 W2-2 的範圍**:
    每個 member 的 DB 階段(重複檢查 + ``db.add`` + ``db.commit``)是**刻意**
    留在 loop thread 的 —— SQLAlchemy Session 不是執行緒安全的,把它包進
    ``to_thread`` 會換成一個更難查的 bug。實測 staging 之後仍有約 110ms 的
    阻塞(SQLite 的 fsync 會放大這個數字),那是 W2-7「event-loop 同步阻塞
    批次收斂」的範圍,不是這裡能修的。

    所以下面分兩件事斷言:
    ① staging 視窗內零停頓  ← W2-2 真正修的東西,也是 1GB zip 凍結全平台的主因
    ② staging 之後的阻塞有上限 ← 已知殘量,只准降不准升,指向 W2-7
    把兩者混成一條 ``stalls == []`` 的話,這支測試會永遠是紅的,然後被人
    改成寬鬆版或直接刪掉 —— 那才是真正的損失。
    """
    user = make_user(db, username="zip-loop-probe")
    collection = _collection(db, user)
    documents._UPLOAD_DIR = str(tmp_path)

    real_persist = documents._persist_blob
    staging_window: list[float] = []  # [enter, exit]

    def slow_persist(content: bytes, sha256: str) -> str:
        staging_window.append(time.perf_counter())
        time.sleep(0.35)
        result = real_persist(content, sha256)
        staging_window.append(time.perf_counter())
        return result

    monkeypatch.setattr(documents, "_persist_blob", slow_persist)

    upload_task = asyncio.create_task(
        documents.upload_zip(
            collection.id,
            UploadFile(
                filename="batch.zip",
                file=BytesIO(_zip_bytes([("note.txt", b"hello off-loop")])),
            ),
            preserve_folder_structure=False,
            origin="csp",

            db=db,
            current_user=user,
        )
    )

    # (probe 結束時刻, 該次 await 實際耗時)
    samples: list[tuple[float, float]] = []
    while not upload_task.done():
        t0 = time.perf_counter()
        await asyncio.sleep(0.02)
        now = time.perf_counter()
        samples.append((now, now - t0))

    response = await upload_task
    assert response.enqueued == 1

    assert len(staging_window) == 2, "slow_persist 沒被呼叫 —— 這支測試什麼都沒驗到"
    enter, exit_ = staging_window
    # sanity:注入的 350ms 真的花掉了,否則下面的視窗是空的
    assert exit_ - enter >= 0.3, f"staging 視窗只有 {exit_ - enter:.3f}s"

    # ① staging 進行中,loop 必須照排程跳。取「整段 await 都落在視窗內」的樣本,
    #    跨越視窗邊界的那一次不算(它一半在視窗外,無法歸因)。
    in_window = [d for (end, d) in samples if enter <= end - d and end <= exit_]
    assert in_window, "staging 視窗內沒有取到樣本 —— probe 間隔可能太長"
    worst = max(in_window)
    assert worst < 0.1, (
        f"staging 進行中 event loop 停頓 {worst*1000:.0f}ms —— "
        f"每個 member 的 zlib/sniff/sha256/磁碟寫入必須走 asyncio.to_thread"
    )

    # ② staging 之後的殘量阻塞(sync DB on the loop)。上限 = 實測值取整後留餘裕;
    #    只准降。真的要歸零得等 W2-7,而那需要先處理 Session 的執行緒邊界。
    after = [d for (end, d) in samples if end - d >= exit_]
    residual = max(after) if after else 0.0
    assert residual < 0.35, (
        f"staging 之後的 loop 阻塞 {residual*1000:.0f}ms 超過已知殘量上限 —— "
        f"若是新增的同步 DB/IO 請比照 W2-7 處理,不要調高這個上限"
    )


def test_stage_zip_member_over_budget_does_not_persist(tmp_path, monkeypatch) -> None:
    """Remaining-budget check must precede ``_persist_blob`` inside the helper."""
    documents._UPLOAD_DIR = str(tmp_path)
    persist_calls: list[str] = []

    def tracking_persist(content: bytes, sha256: str) -> str:
        persist_calls.append(sha256)
        raise AssertionError("_persist_blob must not run when over remaining budget")

    monkeypatch.setattr(documents, "_persist_blob", tracking_persist)

    raw = _zip_bytes([("over.txt", b"x" * 40)])
    zf = zipfile.ZipFile(BytesIO(raw))
    member = zf.infolist()[0]

    staged = documents._stage_zip_member(
        zf,
        member,
        out_name="over.txt",
        in_zip_path="over.txt",
        remaining_budget=10,
    )

    assert staged.outcome == "over_total"
    assert staged.cumulative_delta == 40
    assert persist_calls == []
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_upload_zip_six_status_distribution_matches_pre_to_thread(
    tmp_path, db, monkeypatch
) -> None:
    """empty / too_large / skipped(total) / duplicate / error / enqueued."""
    user = make_user(db, username="zip-six-status")
    collection = _collection(db, user, name="zip-six")
    documents._UPLOAD_DIR = str(tmp_path)
    monkeypatch.setattr(documents, "_MAX_BYTES", 100)
    monkeypatch.setattr(documents, "_ZIP_MAX_TOTAL_BYTES", 50)

    payload = _zip_bytes(
        [
            ("ok.txt", b"x" * 15),
            ("dup.txt", b"x" * 15),
            ("empty.txt", b""),
            ("big.txt", b"y" * 101),
            ("over.txt", b"z" * 25),
            ("fake.docx", b"%PDF-1.7\n%%EOF"),
        ]
    )

    response = await documents.upload_zip(
        collection.id,
        UploadFile(filename="mixed.zip", file=BytesIO(payload)),
        preserve_folder_structure=False,
        origin="csp",

        db=db,
        current_user=user,
    )

    statuses = [item.status for item in response.results]
    assert statuses == [
        "enqueued",
        "duplicate",
        "skipped",
        "too_large",
        "skipped",
        "error",
    ]
    assert response.enqueued == 1
    assert response.duplicates == 1
    assert response.skipped == 3
    assert response.errors == 1
    assert response.files_in_archive == 6
    assert db.query(IngestionDocument).count() == 1
    assert "empty file" in (response.results[2].detail or "")
    assert "archive total" in (response.results[4].detail or "")
    assert "content rejected" in (response.results[5].detail or "")
