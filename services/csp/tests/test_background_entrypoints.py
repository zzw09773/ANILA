"""背景排程入口的行為測試。

``client`` fixture 為了避免與 StaticPool 連線競態，把閒置掃描、附件清除、
告警寄信重試（不帶 db）的背景入口換成 no-op。這裡不經 ``client``，直接驗：
1. 排程迴圈啟動後確實呼叫工作函式；
2. 工作函式自己開 ``SessionLocal`` 的路徑能跑完。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.orm import sessionmaker

from app.services import alert_mail, attachment_retention, inactivity_service


async def _wait_called(calls: list, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not calls and loop.time() < end:
        await asyncio.sleep(0.01)


def _run_loop_once(start, monkeypatch, module, name, result):
    calls: list = []

    def _work():
        calls.append(1)
        return result

    monkeypatch.setattr(module, name, _work)

    async def _main():
        task = start()
        try:
            await _wait_called(calls)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_main())
    return calls


def test_attachment_retention_loop_runs_purge_on_start(monkeypatch):
    calls = _run_loop_once(
        attachment_retention.start_attachment_retention,
        monkeypatch, attachment_retention, "_purge_once", 0,
    )
    assert calls, "啟動後應先跑一輪 _purge_once"


def test_inactivity_loop_runs_sweep_on_start(monkeypatch):
    calls = _run_loop_once(
        inactivity_service.start_inactivity_sweep,
        monkeypatch, inactivity_service, "_sweep_once", {"phase": None},
    )
    assert calls, "啟動後應先跑一輪 _sweep_once"


def _use_test_sessionlocal(monkeypatch, db_engine):
    monkeypatch.setattr(
        "app.database.SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )


def test_purge_once_opens_own_session(monkeypatch, db_engine):
    _use_test_sessionlocal(monkeypatch, db_engine)
    assert attachment_retention._purge_once() == 0


def test_sweep_once_opens_own_session(monkeypatch, db_engine):
    _use_test_sessionlocal(monkeypatch, db_engine)
    assert isinstance(inactivity_service._sweep_once(), dict)


def test_retry_due_alert_mail_without_db_opens_own_session(monkeypatch, db_engine):
    _use_test_sessionlocal(monkeypatch, db_engine)
    alert_mail.retry_due_alert_mail()
    # 自建 session 路徑會 ensure_settings 並 commit；之後同一個 engine 讀得到設定列。
    from app.models.alert_mail import AlertMailSettings

    db = sessionmaker(bind=db_engine)()
    try:
        # 直接查列,不用 ensure_settings(它會自己補建,斷言就驗不到 commit)。
        assert db.query(AlertMailSettings).count() >= 1
    finally:
        db.close()
