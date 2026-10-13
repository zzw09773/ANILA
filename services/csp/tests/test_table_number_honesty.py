"""表格數字誠實標示：程式比對解析後的試算表文字，不讓模型自己宣布。"""
from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from app.api.proxy import (
    _inject_attachments,
    _merge_attachment_trace,
    _sse_with_attachment_trace,
)
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.services.attachment_context import AttachmentInjectResult
from app.services.attachment_service import extract_attachment_text
from app.services import attachment_service
from app.services.table_number_honesty import (
    TABLE_NUMBER_HONESTY_KEY,
    is_spreadsheet_filename,
    numeric_tokens,
    quotes_parsed_table_numbers,
    spreadsheet_bodies,
)
from tests.conftest import login, make_user

# 畫面句子。後端不把這句寫進 metadata，模型寫了也不算數。
_NOTICE = "這些數字來自解析後的表格，可能不精確，尚未實際計算"
_SHEET = "item,amount\nnorth,1280\nsouth,42.5\n"


def test_spreadsheet_suffixes_only():
    assert is_spreadsheet_filename("totals.csv")
    assert is_spreadsheet_filename("TOTALS.XLSX")
    assert is_spreadsheet_filename("grid.ods")
    assert is_spreadsheet_filename("grid.xls")
    assert is_spreadsheet_filename("grid.tsv")
    assert not is_spreadsheet_filename("notes.txt")
    assert not is_spreadsheet_filename("brief.pdf")
    assert not is_spreadsheet_filename("sheet.csv.gz")
    assert not is_spreadsheet_filename("noext")
    assert not is_spreadsheet_filename(None)


def test_numeric_tokens_normalize_without_taking_substrings():
    assert numeric_tokens("金額是 1,280，另有 42.50") == {"1280", "42.5"}
    assert "200" not in numeric_tokens("看到 1,200 而已")
    assert "128" not in numeric_tokens("1280")
    assert numeric_tokens("共 3 筆") == set()
    assert numeric_tokens("全形１２８０") == {"1280"}
    assert numeric_tokens("差額 -42.5") == {"-42.5"}
    assert numeric_tokens(_NOTICE) == set()


def test_quotes_requires_overlap_with_parsed_sheet():
    assert quotes_parsed_table_numbers("北區是 1,280。", [_SHEET])
    assert quotes_parsed_table_numbers("約 42.50", [_SHEET])
    assert not quotes_parsed_table_numbers("這份表沒有我能引用的數字。", [_SHEET])
    assert not quotes_parsed_table_numbers(_NOTICE, [_SHEET])
    assert not quotes_parsed_table_numbers("共 3 列", ["row,n\na,3\n"])
    assert not quotes_parsed_table_numbers("200", ["1200"])
    assert not quotes_parsed_table_numbers("1,280", [])
    assert not quotes_parsed_table_numbers("", [_SHEET])


def test_spreadsheet_bodies_skip_other_files_and_unadmitted_rows():
    csv_row = SimpleNamespace(
        id=1, filename="totals.csv", extract_status="ok", extracted_text=_SHEET,
    )
    txt_row = SimpleNamespace(
        id=2, filename="notes.txt", extract_status="ok", extracted_text="1280",
    )
    held = SimpleNamespace(
        id=3, filename="later.xlsx", extract_status="ok", extracted_text="9999",
    )
    failed = SimpleNamespace(
        id=4, filename="bad.ods", extract_status="failed", extracted_text=None,
    )
    bodies = spreadsheet_bodies([csv_row, txt_row, held, failed], {1, 2, 4})
    assert bodies == [_SHEET]


def _payload(answer: str, **meta):
    return {
        "choices": [{"message": {"role": "assistant", "content": answer}}],
        "anila_meta": {"trace": [], **meta},
    }


def test_non_stream_flag_is_boolean_and_does_not_keep_model_wording():
    inject = AttachmentInjectResult(
        status="ok",
        label="附件注入",
        detail="納入 1 份",
        injected_count=1,
        parsed_table_texts=[_SHEET],
    )
    payload = _payload(
        "北區金額 1,280。",
        **{TABLE_NUMBER_HONESTY_KEY: {"quoted_from_parsed_table": False, "label": _NOTICE}},
    )
    _merge_attachment_trace(payload, inject)
    assert payload["choices"][0]["message"]["content"] == "北區金額 1,280。"
    assert payload["anila_meta"][TABLE_NUMBER_HONESTY_KEY] == {
        "quoted_from_parsed_table": True,
    }
    trace = payload["anila_meta"]["trace"][0]
    assert set(trace) == {"kind", "label", "detail", "status"}
    assert "1280" not in json.dumps(trace)


def test_non_stream_clears_flag_when_numbers_do_not_overlap():
    inject = AttachmentInjectResult(
        status="ok",
        label="附件注入",
        detail="納入 1 份",
        injected_count=1,
        parsed_table_texts=[_SHEET],
    )
    payload = _payload(
        _NOTICE,
        **{TABLE_NUMBER_HONESTY_KEY: {"quoted_from_parsed_table": True}},
    )
    _merge_attachment_trace(payload, inject)
    assert TABLE_NUMBER_HONESTY_KEY not in payload["anila_meta"]


def test_non_stream_without_attachments_drops_forged_flag():
    payload = _payload(
        "1,280",
        **{TABLE_NUMBER_HONESTY_KEY: {"quoted_from_parsed_table": True}},
    )
    _merge_attachment_trace(payload, None)
    assert TABLE_NUMBER_HONESTY_KEY not in payload["anila_meta"]


def test_sse_stamps_flag_from_deltas_and_hides_sheet_text():
    inject = AttachmentInjectResult(
        status="partial",
        label="附件注入",
        detail="納入 1 份",
        injected_count=1,
        parsed_table_texts=[_SHEET],
    )

    async def fake_upstream():
        yield (
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": "北區 "}}]},
                ensure_ascii=False,
            )
            + "\n\n"
        )
        yield (
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": "1,280"}}]},
                ensure_ascii=False,
            )
            + "\n\n"
        )
        yield (
            "event: anila.meta\ndata: "
            + json.dumps(
                {
                    "trace": [],
                    TABLE_NUMBER_HONESTY_KEY: {"label": _NOTICE},
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    async def run():
        chunks = []
        async for chunk in _sse_with_attachment_trace(fake_upstream(), inject):
            chunks.append(chunk)
        return "".join(chunks)

    joined = asyncio.run(run())
    meta = None
    for block in joined.split("\n\n"):
        if "event: anila.meta" in block:
            for line in block.split("\n"):
                if line.startswith("data:"):
                    meta = json.loads(line[5:].strip())
    assert meta is not None
    assert meta[TABLE_NUMBER_HONESTY_KEY] == {"quoted_from_parsed_table": True}
    assert "label" not in meta[TABLE_NUMBER_HONESTY_KEY]
    assert _SHEET not in joined
    assert any(item.get("kind") == "attachment" for item in meta["trace"])


def test_sse_without_inject_drops_forged_flag_and_keeps_other_bytes():
    forged = {
        "trace": [{"kind": "call", "label": "x", "status": "ok"}],
        TABLE_NUMBER_HONESTY_KEY: {"quoted_from_parsed_table": True, "label": _NOTICE},
    }

    async def fake_upstream():
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "hello"}}]}) + "\n\n"
        yield "event: anila.meta\ndata: " + json.dumps(forged) + "\n\n"

    async def run():
        chunks = []
        async for chunk in _sse_with_attachment_trace(fake_upstream(), None):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert chunks[0].startswith("data: ")
    assert "hello" in chunks[0]
    meta = json.loads(chunks[1].split("data:", 1)[1].strip())
    assert TABLE_NUMBER_HONESTY_KEY not in meta
    assert meta["trace"][0]["kind"] == "call"


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachment_service, "ATTACHMENT_STORAGE_ROOT", root)
    return root


def test_inject_keeps_admitted_csv_text_only(client: TestClient, db, storage_root):
    user = make_user(db, username="sheet-label")
    conv = Conversation(user_id=user.id, title="sheet")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    token = login(client, username="sheet-label")

    def upload(filename: str, body: str, mime: str):
        resp = client.post(
            "/api/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, io.BytesIO(body.encode()), mime)},
            data={"conversation_id": str(conv.id)},
        )
        assert resp.status_code == 201, resp.text
        att = (
            db.query(Attachment)
            .filter(Attachment.reference_id == resp.json()["reference_id"])
            .one()
        )
        extract_attachment_text(att.id, db=db)
        db.refresh(att)
        return att

    csv_att = upload("totals.csv", _SHEET, "text/csv")
    txt_att = upload("notes.txt", "serial 1280\n", "text/plain")
    assert csv_att.extract_status == "ok"
    assert txt_att.extract_status == "ok"

    chat_body = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "北區金額是多少"},
        ],
    }
    result = _inject_attachments(db, conv.id, chat_body, None)
    assert result is not None
    assert result.parsed_table_texts == (csv_att.extracted_text,)
    dumped = json.dumps(result.to_trace_entry(), ensure_ascii=False)
    assert "1280" not in dumped
    assert "totals.csv" not in dumped
