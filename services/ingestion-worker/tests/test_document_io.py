from __future__ import annotations

import asyncio
import threading

import pytest

from ingestion_worker import document_io


@pytest.mark.asyncio
async def test_read_and_extract_offloads_read_and_parser(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"classified content")
    event_loop_thread = threading.get_ident()
    parser_thread: int | None = None

    def blocking_parser(filename: str, blob: bytes, mime_type: str | None):
        nonlocal parser_thread
        parser_thread = threading.get_ident()
        assert filename == "sample.txt"
        assert blob == b"classified content"
        assert mime_type == "text/plain"
        return "parsed", {"source": "test"}, {}

    monkeypatch.setattr(document_io, "extract_text", blocking_parser)

    result = await document_io.read_and_extract(
        str(source),
        "sample.txt",
        "text/plain",
        timeout_seconds=1,
    )

    assert result == ("parsed", {"source": "test"}, {})
    assert parser_thread is not None
    assert parser_thread != event_loop_thread


@pytest.mark.asyncio
async def test_blocking_parser_does_not_stall_event_loop(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"content")
    parser_started = threading.Event()
    release_parser = threading.Event()
    loop_progressed = asyncio.Event()

    def blocking_parser(*_args):
        parser_started.set()
        assert release_parser.wait(timeout=2)
        return "parsed", {}, {}

    async def ticker() -> None:
        while not parser_started.is_set():
            await asyncio.sleep(0)
        loop_progressed.set()
        release_parser.set()

    monkeypatch.setattr(document_io, "extract_text", blocking_parser)
    parse_task = asyncio.create_task(
        document_io.read_and_extract(
            str(source), "sample.txt", "text/plain", timeout_seconds=1
        )
    )
    ticker_task = asyncio.create_task(ticker())

    await asyncio.wait_for(loop_progressed.wait(), timeout=1)
    assert await parse_task == ("parsed", {}, {})
    await ticker_task


@pytest.mark.asyncio
async def test_parse_timeout_discards_late_thread_result(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"content")
    parser_started = threading.Event()
    release_parser = threading.Event()
    parser_finished = threading.Event()

    def blocking_parser(*_args):
        parser_started.set()
        release_parser.wait(timeout=2)
        parser_finished.set()
        return "late result", {}, {}

    monkeypatch.setattr(document_io, "extract_text", blocking_parser)
    try:
        with pytest.raises(document_io.DocumentParseTimeout):
            await document_io.read_and_extract(
                str(source), "sample.txt", "text/plain", timeout_seconds=0.01
            )
        assert parser_started.is_set()
        assert not parser_finished.is_set()
    finally:
        release_parser.set()
        assert await asyncio.to_thread(parser_finished.wait, 1)


@pytest.mark.asyncio
async def test_cancelled_parse_never_delivers_thread_result(monkeypatch, tmp_path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"content")
    parser_started = threading.Event()
    release_parser = threading.Event()
    parser_finished = threading.Event()

    def blocking_parser(*_args):
        parser_started.set()
        release_parser.wait(timeout=2)
        parser_finished.set()
        return "late result", {}, {}

    monkeypatch.setattr(document_io, "extract_text", blocking_parser)
    task = asyncio.create_task(
        document_io.read_and_extract(
            str(source), "sample.txt", "text/plain", timeout_seconds=5
        )
    )
    try:
        assert await asyncio.to_thread(parser_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_parser.set()
        assert await asyncio.to_thread(parser_finished.wait, 1)
