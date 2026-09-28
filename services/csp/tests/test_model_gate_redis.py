"""A multi-worker process must not invent its own model-admission board."""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import model_gate as gate


@pytest.fixture
def fresh_board():
    gate.reset_board_for_tests()
    yield
    gate.reset_board_for_tests()


def _parent_uvicorn(workers: str | None):
    def argv(pid: int) -> list[str]:
        if pid == os.getpid():
            return ["python", "-c", "spawn_main"]
        if workers is None:
            return ["uvicorn", "app.main:app", "--port", "8000"]
        return ["uvicorn", "app.main:app", "--port", "8000", "--workers", workers]

    return argv


def test_multi_worker_without_redis_refuses_at_the_board(monkeypatch, fresh_board, caplog):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if hasattr(gate, "_argv"):
        monkeypatch.setattr(gate, "_argv", _parent_uvicorn("32"))
    with caplog.at_level(logging.ERROR, logger="app.services.model_gate"):
        with pytest.raises(RuntimeError) as caught:
            gate.get_board()
    assert "REDIS_URL" in str(caught.value)
    assert "REDIS_URL" in caplog.text
    assert gate._board is None


def test_single_worker_without_redis_uses_a_process_local_board(monkeypatch, fresh_board, caplog):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if hasattr(gate, "_argv"):
        monkeypatch.setattr(gate, "_argv", _parent_uvicorn(None))
    with caplog.at_level(logging.ERROR, logger="app.services.model_gate"):
        board = gate.get_board()
    assert isinstance(board, gate.MemoryBoard)
    assert not caplog.records


def test_pytest_keeps_a_process_local_board_even_with_redis_and_many_workers(
    monkeypatch, fresh_board
):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    assert os.environ.get("PYTEST_CURRENT_TEST")
    if hasattr(gate, "_argv"):
        monkeypatch.setattr(gate, "_argv", _parent_uvicorn("32"))
    assert isinstance(gate.get_board(), gate.MemoryBoard)


def test_startup_refuses_when_the_model_gate_refuses(monkeypatch):
    def refuse() -> None:
        raise RuntimeError("沒有 REDIS_URL，多個 worker 不能啟動")

    monkeypatch.setattr(gate, "require_model_gate", refuse, raising=False)
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        with TestClient(app):
            pass
