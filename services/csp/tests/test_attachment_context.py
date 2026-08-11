"""P1.5 attachment context: extraction, derived admission, injection, auth.

Fixtures use .txt/.md (or monkeypatched extract_text) rather than real PDFs —
the parser stack is present but PDF bytes would pull pymupdf and obscure the
budget/injection assertions. Plan verification 「10 頁 vs 500 頁」is modelled
as two text bodies whose estimated token counts straddle the configured budget.

Admission is derived by admit() at use time; extract_status never stores
budget fit (no 'over_budget' value).
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.proxy import (
    _inject_attachments,
    _require_conversation_access,
    _sse_with_attachment_trace,
)
from app.middleware.caller import Caller
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.services.attachment_context import (
    AttachmentInjectResult,
    admit,
    attachment_budget_tokens,
    effective_cost,
    get_context_window,
    get_conversation_attachment_usage,
)
from app.services import attachment_context as attachment_context_module
from app.services import attachment_service
from app.services.attachment_service import (
    TEXT_CLASS_EXTENSIONS,
    delete_attachment,
    extract_attachment_text,
)
from app.services.proxy import _estimate_token_count
from tests.conftest import login, make_model, make_user


# Shrink the budget so fixtures stay small (bytes, not multi-MB PDFs).
_TEST_WINDOW = 1000
_TEST_RATIO = 0.5  # budget = 500
_TEST_SAFETY = 1.15
_TEST_STORE_CAP = 1500  # absolute store cap (raw tokens), model-independent


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


@pytest.fixture(autouse=True)
def _budget_knobs(db, monkeypatch):
    """Use explicit test seams for fixed constants and the retained ratio."""
    from app.models.platform_setting import set_setting

    set_setting(db, "limits.attachment_budget_ratio", _TEST_RATIO)
    db.commit()

    real_window = attachment_context_module.get_context_window

    def test_window(current_db, model_name):
        if model_name in {None, "gpt-test"}:
            return _TEST_WINDOW
        return real_window(current_db, model_name)

    monkeypatch.setattr(attachment_context_module, "get_context_window", test_window)
    monkeypatch.setattr(sys.modules[__name__], "get_context_window", test_window)
    monkeypatch.setattr(attachment_service, "get_context_window", test_window)
    monkeypatch.setattr(
        attachment_context_module,
        "max_stored_tokens",
        lambda current_db: _TEST_STORE_CAP,
    )
    monkeypatch.setattr(
        attachment_service,
        "max_stored_tokens",
        lambda current_db: _TEST_STORE_CAP,
    )


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachment_service, "ATTACHMENT_STORAGE_ROOT", root)
    return root


def _make_conv(db, user, title="att-test") -> Conversation:
    conv = Conversation(user_id=user.id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _write_and_row(
    db,
    *,
    user,
    conv,
    storage_root: Path,
    filename: str,
    content: bytes,
    extract_status: str = "pending",
    created_at: datetime | None = None,
    content_type: str = "text/plain",
) -> Attachment:
    ref = f"ref-{filename}-{id(content) % 10_000_000}"
    rel = f"{user.id}/{ref}{Path(filename).suffix}"
    path = storage_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    att = Attachment(
        reference_id=ref,
        conversation_id=conv.id if conv else None,
        uploaded_by=user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        storage_path=rel,
        extract_status=extract_status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _text_of_tokens(target_raw: int, seed: str = "alpha ") -> str:
    """Build ASCII text whose heuristic estimate is ~target_raw tokens."""
    # Heuristic ≈ max(wordish, ascii/4). Prefer wordish via repeated tokens.
    unit = seed
    text = unit
    while _estimate_token_count(None, text) < target_raw:
        text += unit
    # Trim back if we overshot a lot
    while len(text) > len(unit) and _estimate_token_count(None, text) > target_raw + 5:
        text = text[: -len(unit)]
    return text


def _ordered_ok(db, conv_id: int):
    return (
        db.query(Attachment)
        .filter(Attachment.conversation_id == conv_id)
        .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        .all()
    )


# ── a. Plan verification: small admitted / large excluded + injection ──────


def test_small_ok_large_excluded_injection(db, storage_root, monkeypatch):
    """「丟 10 頁與 500 頁」：用 token 數跨預算的兩份 .txt 模擬（非真 PDF）。

    Both extract as ok; admit() excludes the large one for the default budget.
    """
    user = make_user(db, username="att-a")
    conv = _make_conv(db, user)

    # budget = 500; safety 1.15 → raw ok ≤ floor(500/1.15) ≈ 434
    small_text = _text_of_tokens(80, seed="small ")
    large_text = _text_of_tokens(600, seed="largeword ")

    def fake_extract(filename, content, mime_type=None):
        body = content.decode("utf-8")
        pages = 10 if "small" in filename else 500
        return body, {"page_count": pages}, {}

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text", fake_extract,
    )

    small = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="small-10p.txt", content=small_text.encode(),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    large = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="large-500p.txt", content=large_text.encode(),
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    extract_attachment_text(small.id, db=db)
    extract_attachment_text(large.id, db=db)
    db.refresh(small)
    db.refresh(large)

    assert small.extract_status == "ok"
    assert large.extract_status == "ok"  # stored; admission is derived
    assert large.extracted_text
    assert large.token_count is not None

    budget = attachment_budget_tokens(db, _TEST_WINDOW)
    admitted, excluded = admit(db, _ordered_ok(db, conv.id), budget)
    assert small.id in admitted
    assert large.id in excluded

    body = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "系統提示"},
            {"role": "user", "content": "請分析附件"},
        ],
    }
    result = _inject_attachments(db, conv.id, body, "gpt-test")
    assert result is not None
    sys_content = body["messages"][0]["content"]
    assert "small-10p.txt" in sys_content
    assert small_text[:40] in sys_content
    assert large_text[:40] not in sys_content
    assert "large-500p.txt" in sys_content
    assert "超出附件 token 預算" in sys_content


# ── b. Budget accumulation oldest-first ───────────────────────────────────


def test_budget_accumulation_oldest_first(db, storage_root, monkeypatch):
    user = make_user(db, username="att-b")
    conv = _make_conv(db, user)

    # Each ~200 raw → effective ~230; two fit in 500, third does not.
    chunk = _text_of_tokens(200, seed="chunk ")

    def fake_extract(filename, content, mime_type=None):
        return content.decode("utf-8"), {"page_count": 1}, {}

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text", fake_extract,
    )

    atts = []
    for i in range(3):
        att = _write_and_row(
            db, user=user, conv=conv, storage_root=storage_root,
            filename=f"part{i}.txt", content=chunk.encode(),
            created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc),
        )
        extract_attachment_text(att.id, db=db)
        db.refresh(att)
        atts.append(att)

    assert all(a.extract_status == "ok" for a in atts)
    budget = attachment_budget_tokens(db, _TEST_WINDOW)
    admitted, excluded = admit(db, _ordered_ok(db, conv.id), budget)
    assert admitted == [atts[0].id, atts[1].id]
    assert excluded == [atts[2].id]


# ── c. Delete promotes without recheck ────────────────────────────────────


def test_delete_promotes_without_recheck(db, storage_root, monkeypatch):
    """Freeing space re-derives admission on next read — no recheck call."""
    user = make_user(db, username="att-c")
    conv = _make_conv(db, user)
    chunk = _text_of_tokens(200, seed="free ")

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {"page_count": 1}, {},
        ),
    )

    atts = []
    for i in range(3):
        att = _write_and_row(
            db, user=user, conv=conv, storage_root=storage_root,
            filename=f"free{i}.txt", content=chunk.encode(),
            created_at=datetime(2026, 2, i + 1, tzinfo=timezone.utc),
        )
        extract_attachment_text(att.id, db=db)
        db.refresh(att)
        atts.append(att)

    budget = attachment_budget_tokens(db, _TEST_WINDOW)
    admitted, excluded = admit(db, _ordered_ok(db, conv.id), budget)
    assert atts[2].id in excluded
    assert all(a.extract_status == "ok" for a in atts)

    # Delete via service (no recheck inside).
    delete_attachment(db, atts[0].reference_id, user)

    remaining = _ordered_ok(db, conv.id)
    assert len(remaining) == 2
    admitted2, excluded2 = admit(db, remaining, budget)
    assert set(admitted2) == {atts[1].id, atts[2].id}
    assert excluded2 == []

    # Inject also re-derives — both texts present, no status flip needed.
    body = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q"},
        ],
    }
    _inject_attachments(db, conv.id, body, None)
    content = body["messages"][0]["content"]
    assert "free1.txt" in content
    assert "free2.txt" in content
    assert "超出附件 token 預算" not in content

    for a in (atts[1], atts[2]):
        db.refresh(a)
        assert a.extract_status == "ok"


# ── d. Capacity object consistency ────────────────────────────────────────


def test_capacity_object_consistent(db, storage_root, monkeypatch):
    user = make_user(db, username="att-d")
    conv = _make_conv(db, user)
    text = _text_of_tokens(100, seed="cap ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="cap.txt", content=text.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)

    window = get_context_window(db, None)
    usage = get_conversation_attachment_usage(db, conv.id, window)
    assert usage["budget_tokens"] == int(_TEST_WINDOW * _TEST_RATIO)
    assert usage["used_tokens"] == effective_cost(db, att.token_count)
    assert usage["remaining_tokens"] == usage["budget_tokens"] - usage["used_tokens"]
    assert usage["over_budget_tokens"] == 0
    assert 0 <= usage["percent"] <= 100
    assert usage["used_tokens"] + usage["remaining_tokens"] == usage["budget_tokens"]
    assert usage["attachment_count"] == 1
    assert usage["admitted_ids"] == [att.id]


def test_capacity_used_remaining_invariant_with_overflow(db):
    """used + remaining == budget always; excluded cost rides over_budget_tokens."""
    user = make_user(db, username="att-d2")
    conv = _make_conv(db, user)
    # Fabricate an 'ok' row whose effective cost exceeds the budget —
    # admit excludes it; meter must still reconcile.
    att = Attachment(
        reference_id="overflow-cap",
        conversation_id=conv.id,
        uploaded_by=user.id,
        filename="overflow.txt",
        content_type="text/plain",
        size_bytes=10,
        storage_path="x/overflow.txt",
        extract_status="ok",
        token_count=2_000_000,  # effective >> budget 500
    )
    db.add(att)
    db.commit()

    window = get_context_window(db, None)
    usage = get_conversation_attachment_usage(db, conv.id, window)
    budget = usage["budget_tokens"]
    assert usage["used_tokens"] + usage["remaining_tokens"] == budget
    assert usage["used_tokens"] == 0  # nothing admitted
    assert usage["remaining_tokens"] == budget
    assert usage["over_budget_tokens"] == effective_cost(db, 2_000_000)
    assert usage["excluded_ids"] == [att.id]


# ── e. Pending not injected ───────────────────────────────────────────────


def test_pending_not_injected(db, storage_root):
    user = make_user(db, username="att-e")
    conv = _make_conv(db, user)
    _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="pending.txt", content=b"hello pending",
        extract_status="pending",
    )
    body = {
        "messages": [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "hi"},
        ],
    }
    _inject_attachments(db, conv.id, body, None)
    content = body["messages"][0]["content"]
    assert "仍在處理中" in content
    assert "hello pending" not in content


# ── f. Extraction failure ─────────────────────────────────────────────────


def test_extraction_failure_named_in_block(db, storage_root, monkeypatch):
    from anila_core.ingestion.errors import ParseError

    user = make_user(db, username="att-f")
    conv = _make_conv(db, user)

    def boom(filename, content, mime_type=None):
        raise ParseError.corrupt(user_message="檔案損毀無法解析")

    monkeypatch.setattr("anila_core.ingestion.parsers.extract_text", boom)

    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="bad.txt", content=b"xxx",
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "failed"
    assert att.extract_error
    assert "損毀" in att.extract_error or "解析" in att.extract_error

    body = {"messages": [{"role": "user", "content": "q"}]}
    _inject_attachments(db, conv.id, body, None)
    sys_content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "system"
    assert "bad.txt" in sys_content
    assert "解析失敗" in sys_content


# ── g. Safety multiplier ──────────────────────────────────────────────────


def test_safety_multiplier_on_known_string(db):
    text = "hello world token safety check 12345"
    raw = _estimate_token_count(None, text)
    assert effective_cost(db, raw) == int(raw * _TEST_SAFETY)


# ── h. No attachments → body unchanged ────────────────────────────────────


def test_injection_noop_without_attachments(db):
    user = make_user(db, username="att-h")
    conv = _make_conv(db, user)
    body = {
        "messages": [
            {"role": "system", "content": "only-system"},
            {"role": "user", "content": "hi"},
        ],
    }
    before = json.dumps(body["messages"], ensure_ascii=False, sort_keys=True)
    result = _inject_attachments(db, conv.id, body, None)
    after = json.dumps(body["messages"], ensure_ascii=False, sort_keys=True)
    assert before == after
    assert result is None


# ── i. Conversation attachments endpoint auth ─────────────────────────────


def test_conversation_attachments_rejects_non_owner(client: TestClient, db):
    owner = make_user(db, username="att-owner")
    attacker = make_user(db, username="att-attacker")
    conv = _make_conv(db, owner)

    # Same rule as chat path.
    with pytest.raises(HTTPException) as exc:
        _require_conversation_access(
            db, Caller(user=attacker, api_key_id=None), conv.id,
        )
    assert exc.value.status_code == 404

    # HTTP endpoint must refuse too.
    token = login(client, username="att-attacker")
    resp = client.get(
        f"/api/conversations/{conv.id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ── FIX 1: upload refuses foreign conversation_id ─────────────────────────


def test_upload_rejects_foreign_conversation(
    client: TestClient, db, storage_root,
):
    owner = make_user(db, username="att-up-owner")
    attacker = make_user(db, username="att-up-attacker")
    conv = _make_conv(db, owner)

    token = login(client, username="att-up-attacker")
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.txt", io.BytesIO(b"CROSS_USER_MARKER_AAA"), "text/plain")},
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 404
    assert db.query(Attachment).filter(
        Attachment.conversation_id == conv.id,
    ).count() == 0

    body = {
        "messages": [
            {"role": "system", "content": "owner-base"},
            {"role": "user", "content": "q"},
        ],
    }
    _inject_attachments(db, conv.id, body, None)
    assert "CROSS_USER_MARKER_AAA" not in json.dumps(body)


# ── FIX 1: no byte-size precheck; admission is post-extraction only ────────


def test_http_text_300kib_uploads_and_classifies_ok(
    client: TestClient, db, storage_root, monkeypatch,
):
    """~300 KiB ASCII: old size//2 guard returned 400; now 201 → ok."""
    # Production-like window so 300 KiB ASCII (~76.8k raw, ~88k effective)
    # fits the 89.6k budget (was falsely refused by bytes//2 ≈ 150k).
    from app.models.platform_setting import set_setting

    set_setting(db, "limits.attachment_budget_ratio", 0.7)
    db.commit()
    monkeypatch.setattr(
        attachment_context_module,
        "max_stored_tokens",
        lambda current_db: 800_000,
    )
    monkeypatch.setattr(
        attachment_service,
        "max_stored_tokens",
        lambda current_db: 800_000,
    )
    user = make_user(db, username="att-300k")
    conv = _make_conv(db, user)
    token = login(client, username="att-300k")
    blob = b"a" * (300 * 1024)

    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("big.txt", io.BytesIO(blob), "text/plain")},
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extract_status"] == "pending"

    att = (
        db.query(Attachment)
        .filter(Attachment.reference_id == body["reference_id"])
        .one()
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_text is not None
    assert att.token_count is not None
    assert effective_cost(db, att.token_count) <= int(128_000 * 0.7)


def test_http_text_excluded_after_extract_not_upload_400(
    client: TestClient, db, storage_root, monkeypatch,
):
    """Plain text that exceeds budget: 201 at upload, ok after extract, excluded by admit."""
    user = make_user(db, username="att-http-ob")
    conv = _make_conv(db, user)
    token = login(client, username="att-http-ob")
    # Test budget = 500; ~600 raw → effective ~690 → excluded by admit.
    large_text = _text_of_tokens(600, seed="httpob ")

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )

    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": (
                "over.txt",
                io.BytesIO(large_text.encode()),
                "text/plain",
            ),
        },
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["extract_status"] == "pending"

    att = (
        db.query(Attachment)
        .filter(Attachment.reference_id == resp.json()["reference_id"])
        .one()
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_text

    budget = attachment_budget_tokens(db, _TEST_WINDOW)
    admitted, excluded = admit(db, _ordered_ok(db, conv.id), budget)
    assert att.id in excluded
    assert admitted == []


def test_binary_upload_not_refused_by_byte_precheck(
    client: TestClient, db, storage_root, monkeypatch,
):
    user = make_user(db, username="att-bin")
    conv = _make_conv(db, user)
    token = login(client, username="att-bin")

    blob = b"%PDF-1.4 " + (b"x" * (300 * 1024))
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("manual.pdf", io.BytesIO(blob), "application/pdf")},
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["extract_status"] == "pending"


def test_text_budget_excluded_via_admit_not_stored_status(
    db, storage_root, monkeypatch,
):
    """Text that exceeds budget after extraction stays ok; admit excludes it."""
    user = make_user(db, username="att-txt-ob")
    conv = _make_conv(db, user)
    large_text = _text_of_tokens(600, seed="ob ")

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="big.txt", content=large_text.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_text  # kept for promotion when budget frees

    budget = attachment_budget_tokens(db, _TEST_WINDOW)
    _, excluded = admit(db, _ordered_ok(db, conv.id), budget)
    assert att.id in excluded


# ── Staleness class gone: same row, two models, no write ───────────────────


def test_admission_derived_per_model_no_staleness(
    client: TestClient, db, storage_root, monkeypatch,
):
    """One attachment, two ?model= queries, no intervening write.

    Persisted extract_status stays 'ok'; budget_admitted flips with the
    requested model's window — the stale-status class is gone.
    """
    user = make_user(db, username="att-stale")
    conv = _make_conv(db, user)
    small = make_model(db, name="tiny-ctx")
    small.context_window = 200
    db.commit()

    body_text = _text_of_tokens(150, seed="stale ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="mid.txt", content=body_text.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"

    token = login(client, username="att-stale")

    # Default window budget 500 → admitted
    resp_default = client.get(
        f"/api/conversations/{conv.id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_default.status_code == 200, resp_default.text
    rows_default = resp_default.json()["attachments"]
    assert len(rows_default) == 1
    assert rows_default[0]["extract_status"] == "ok"
    assert rows_default[0]["budget_admitted"] is True
    assert resp_default.json()["conversation_capacity"]["budget_tokens"] == int(
        _TEST_WINDOW * _TEST_RATIO,
    )

    # Tiny window budget 100 → excluded; NO write between the two GETs
    resp_tiny = client.get(
        f"/api/conversations/{conv.id}/attachments",
        params={"model": "tiny-ctx"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_tiny.status_code == 200, resp_tiny.text
    rows_tiny = resp_tiny.json()["attachments"]
    assert len(rows_tiny) == 1
    assert rows_tiny[0]["extract_status"] == "ok"
    assert rows_tiny[0]["budget_admitted"] is False
    assert resp_tiny.json()["conversation_capacity"]["budget_tokens"] == int(
        200 * _TEST_RATIO,
    )
    # Meter shows overflow for the excluded cost under the tiny budget.
    cap_tiny = resp_tiny.json()["conversation_capacity"]
    assert cap_tiny["used_tokens"] == 0
    assert cap_tiny["remaining_tokens"] == cap_tiny["budget_tokens"]
    assert cap_tiny["over_budget_tokens"] > 0

    # Persisted row unchanged
    db.refresh(att)
    assert att.extract_status == "ok"


# ── FIX 2: capacity endpoints honour ?model= ───────────────────────────────


def test_capacity_endpoint_uses_named_model(
    client: TestClient, db, storage_root, monkeypatch,
):
    """Meter with ?model=tiny-ctx matches admit() for that window."""
    user = make_user(db, username="att-cap-model")
    conv = _make_conv(db, user)
    small = make_model(db, name="tiny-ctx")
    small.context_window = 200
    db.commit()

    body_text = _text_of_tokens(150, seed="capmodel ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="mid.txt", content=body_text.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"

    expected_budget = int(200 * _TEST_RATIO)  # 100
    token = login(client, username="att-cap-model")
    resp = client.get(
        f"/api/conversations/{conv.id}/attachments",
        params={"model": "tiny-ctx"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    cap = resp.json()["conversation_capacity"]
    assert cap["budget_tokens"] == expected_budget
    assert resp.json()["attachments"][0]["budget_admitted"] is False

    resp_default = client.get(
        f"/api/conversations/{conv.id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_default.status_code == 200
    assert resp_default.json()["conversation_capacity"]["budget_tokens"] == int(
        _TEST_WINDOW * _TEST_RATIO,
    )
    assert resp_default.json()["attachments"][0]["budget_admitted"] is True

    meta = client.get(
        f"/api/attachments/{att.reference_id}/meta",
        params={"model": "tiny-ctx"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert meta.status_code == 200, meta.text
    assert meta.json()["conversation_capacity"]["budget_tokens"] == expected_budget
    assert meta.json()["budget_admitted"] is False
    assert meta.json()["extract_status"] == "ok"


# ── FIX 4: injection uses selected model's context window ─────────────────


def test_admission_uses_model_context_window(db, storage_root, monkeypatch):
    user = make_user(db, username="att-fix4")
    conv = _make_conv(db, user)
    # Small model: window 200 → budget 100; default window 1000 → budget 500.
    small = make_model(db, name="tiny-ctx")
    small.context_window = 200
    db.commit()

    # ~150 raw → effective ~172: fits default budget 500, not tiny budget 100.
    body_text = _text_of_tokens(150, seed="modelwin ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="mid.txt", content=body_text.encode(),
    )
    extract_attachment_text(att.id, db=db)  # default window store-cap → ok
    db.refresh(att)
    assert att.extract_status == "ok"

    window = get_context_window(db, "tiny-ctx")
    assert window == 200
    usage = get_conversation_attachment_usage(db, conv.id, window)
    assert usage["budget_tokens"] == int(200 * _TEST_RATIO)
    assert att.id in usage["excluded_ids"]
    assert usage["admitted_ids"] == []

    # Inject with the small model withholds text; status stays ok.
    chat_body = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q"},
        ],
    }
    _inject_attachments(db, conv.id, chat_body, "tiny-ctx")
    assert body_text[:30] not in chat_body["messages"][0]["content"]
    assert "mid.txt" in chat_body["messages"][0]["content"]
    assert "超出附件 token 預算" in chat_body["messages"][0]["content"]
    db.refresh(att)
    assert att.extract_status == "ok"


# ── FIX 5: injection does not load excluded text; store cap → too_large ────


def test_inject_skips_loading_excluded_text(db, storage_root, monkeypatch):
    user = make_user(db, username="att-fix5a")
    conv = _make_conv(db, user)
    marker = "EXCLUDED_TEXT_SHOULD_NOT_LOAD_" + ("Z" * 200)
    large = _text_of_tokens(600, seed="nobloat ")
    large = marker + large

    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="bloated.txt", content=large.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_text and marker in att.extracted_text

    queried_text_ids: list[int] = []
    real_query = db.query

    def tracking_query(*entities, **kwargs):
        if (
            len(entities) == 2
            and getattr(entities[0], "key", None) == "id"
            and getattr(entities[1], "key", None) == "extracted_text"
        ):
            q = real_query(*entities, **kwargs)
            orig_all = q.all

            def all_and_track():
                rows = orig_all()
                for row in rows:
                    queried_text_ids.append(row[0])
                return rows

            q.all = all_and_track
            return q
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db, "query", tracking_query)

    body = {"messages": [{"role": "user", "content": "q"}]}
    _inject_attachments(db, conv.id, body, None)
    assert att.id not in queried_text_ids
    assert marker not in body["messages"][0]["content"]
    assert "bloated.txt" in body["messages"][0]["content"]


def test_storage_ratio_cap_withholds_text_as_too_large(
    db, storage_root, monkeypatch,
):
    user = make_user(db, username="att-fix5b")
    conv = _make_conv(db, user)
    # store cap = budget 500 * 3.0 = 1500 raw tokens
    huge = _text_of_tokens(1600, seed="storecap ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="huge.txt", content=huge.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "too_large"
    assert att.extracted_text is None
    assert att.token_count is not None and att.token_count > 1500
    assert att.extract_error and "儲存上限" in att.extract_error

    body = {"messages": [{"role": "user", "content": "q"}]}
    _inject_attachments(db, conv.id, body, None)
    sys_content = body["messages"][0]["content"]
    assert "huge.txt" in sys_content
    assert "抽取文字超過儲存上限" in sys_content


def test_inject_does_not_commit_or_mutate_status(db, storage_root, monkeypatch):
    """Per-turn inject must not write extract_status (no recheck path)."""
    user = make_user(db, username="att-nocommit")
    conv = _make_conv(db, user)
    small = make_model(db, name="tiny-ctx")
    small.context_window = 200
    db.commit()

    body_text = _text_of_tokens(150, seed="nocommit ")
    monkeypatch.setattr(
        "anila_core.ingestion.parsers.extract_text",
        lambda fn, content, mime_type=None: (
            content.decode("utf-8"), {}, {},
        ),
    )
    att = _write_and_row(
        db, user=user, conv=conv, storage_root=storage_root,
        filename="mid.txt", content=body_text.encode(),
    )
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    before_updated = att.extracted_at

    commits: list[str] = []
    real_commit = db.commit

    def tracking_commit():
        commits.append("commit")
        return real_commit()

    monkeypatch.setattr(db, "commit", tracking_commit)

    chat_body = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q"},
        ],
    }
    _inject_attachments(db, conv.id, chat_body, "tiny-ctx")
    assert commits == []
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_at == before_updated


# ── FIX 7: streaming terminal meta carries attachment trace ───────────────


def test_sse_attachment_trace_merged_into_anila_meta():
    import asyncio

    inject = AttachmentInjectResult(
        status="partial",
        label="附件注入",
        detail="納入 1 份；未納入 1 份",
        injected_count=1,
    )

    async def fake_upstream():
        # Split the way proxy_stream synthesises the fallback meta.
        yield "event: anila.meta\n"
        yield (
            "data: "
            + json.dumps(
                {
                    "trace_id": "t1",
                    "trace": [{"kind": "call", "label": "x", "status": "ok"}],
                    "citations": [],
                    "confidence": None,
                    "handoff_chain": [],
                    "follow_ups": [],
                    "latency_ms": 1,
                    "classified": False,
                    "usage": None,
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def run():
        chunks = []
        async for c in _sse_with_attachment_trace(fake_upstream(), inject):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    joined = "".join(chunks)
    assert "event: anila.meta" in joined
    meta = None
    for block in joined.split("\n\n"):
        if "event: anila.meta" in block:
            for line in block.split("\n"):
                if line.startswith("data:"):
                    meta = json.loads(line[5:].strip())
    assert meta is not None
    kinds = [t.get("kind") for t in meta["trace"]]
    assert "attachment" in kinds
    att_entry = next(t for t in meta["trace"] if t["kind"] == "attachment")
    assert att_entry["label"] == "附件注入"
    assert att_entry["status"] == "partial"


# ── text-class formats: .py reaches extract ok + injected context ─────────


def test_py_attachment_extracts_ok_and_injects_source(
    client: TestClient, db, storage_root,
):
    """Owner 2026-08-01: .py is uploadable; parser must extract and inject body."""
    user = make_user(db, username="att-py-src")
    conv = _make_conv(db, user)
    token = login(client, username="att-py-src")
    py_src = (
        "def answer():\n"
        "    return 'PY_ATTACH_MARKER_42'\n"
        "\n"
        "print(answer())\n"
    )

    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": ("snippet.py", io.BytesIO(py_src.encode()), "text/x-python"),
        },
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["extract_status"] == "pending"

    att = (
        db.query(Attachment)
        .filter(Attachment.reference_id == resp.json()["reference_id"])
        .one()
    )
    # Real extract_text / PlainTextParser — no monkeypatch.
    extract_attachment_text(att.id, db=db)
    db.refresh(att)
    assert att.extract_status == "ok"
    assert att.extracted_text is not None
    assert "PY_ATTACH_MARKER_42" in att.extracted_text
    assert "def answer()" in att.extracted_text

    body = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "系統提示"},
            {"role": "user", "content": "讀這個 .py"},
        ],
    }
    result = _inject_attachments(db, conv.id, body, "gpt-test")
    assert result is not None
    sys_content = body["messages"][0]["content"]
    assert "snippet.py" in sys_content
    assert "PY_ATTACH_MARKER_42" in sys_content
    assert "def answer()" in sys_content


# ── F1/F4: text-class extensions upload under browser / Python / empty MIME ──

# Extensions newly added to ALLOWED_EXTENSIONS in this package (mutation target).
_NEWLY_ALLOWED_EXTENSIONS = (
    ".sh", ".bash", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".r", ".m", ".tex",
    ".dcm", ".dat", ".inp", ".out",
)

# Reviewer-verified Linux browser MIMEs (xdg-mime / shared-mime-info) that
# previously 400'd; others use text/plain as a safe browser-ish default.
_BROWSER_MIME_BY_EXT: dict[str, str] = {
    ".sh": "application/x-shellscript",
    ".bash": "application/octet-stream",
    ".rb": "application/x-ruby",
    ".php": "application/x-php",
    ".r": "application/octet-stream",
    ".toml": "application/octet-stream",
    ".ini": "application/octet-stream",
    ".xml": "application/xml",
    ".sql": "application/sql",
    ".js": "application/javascript",
    ".ts": "video/mp2t",
    ".tsx": "application/octet-stream",
    ".jsx": "application/octet-stream",
    ".py": "text/x-python",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".log": "text/x-log",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".java": "text/x-java",
    ".go": "text/x-go",
    ".rs": "text/rust",
    ".c": "text/x-csrc",
    ".cpp": "text/x-c++src",
    ".h": "text/x-chdr",
    ".hpp": "text/x-c++hdr",
    ".m": "text/x-objcsrc",
    ".tex": "text/x-tex",
    ".dcm": "text/plain",
    ".dat": "application/octet-stream",
    ".inp": "text/plain",
    ".out": "text/plain",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
}


def _python_mime(ext: str) -> str | None:
    import mimetypes

    return mimetypes.guess_type(f"sample{ext}")[0]


@pytest.mark.parametrize("ext", _NEWLY_ALLOWED_EXTENSIONS)
@pytest.mark.parametrize(
    "mime_kind",
    ("browser", "python", "empty"),
    ids=("browser-mime", "python-mime", "empty-mime"),
)
def test_newly_allowed_text_ext_uploads_201(
    client: TestClient, db, storage_root, ext: str, mime_kind: str,
):
    """F4: every newly-allowed ext must 201; F1: MIME must not reject them."""
    assert ext in TEXT_CLASS_EXTENSIONS
    user = make_user(db, username=f"n{ext[1:]}{mime_kind[0]}")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    body_bytes = f"# sample{ext}\nprint('ok')\n".encode()

    if mime_kind == "browser":
        mime = _BROWSER_MIME_BY_EXT[ext]
    elif mime_kind == "python":
        mime = _python_mime(ext)  # may be None → omit Content-Type
    else:
        mime = ""

    if mime:
        files = {"file": (f"sample{ext}", io.BytesIO(body_bytes), mime)}
    else:
        files = {"file": (f"sample{ext}", io.BytesIO(body_bytes))}

    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, (
        f"{ext} mime_kind={mime_kind!r} mime={mime!r} → {resp.status_code}: {resp.text}"
    )


@pytest.mark.parametrize("ext", sorted(TEXT_CLASS_EXTENSIONS))
@pytest.mark.parametrize(
    "mime_kind",
    ("browser", "python", "empty"),
    ids=("browser-mime", "python-mime", "empty-mime"),
)
def test_text_class_ext_uploads_under_common_mimes(
    client: TestClient, db, storage_root, ext: str, mime_kind: str,
):
    """Acceptance: full TEXT_CLASS_EXTENSIONS × {browser, python, empty} → 201."""
    user = make_user(db, username=f"t{ext[1:]}{mime_kind[0]}")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    body_bytes = f"sample content for {ext}\n".encode()

    if mime_kind == "browser":
        mime = _BROWSER_MIME_BY_EXT.get(ext, "text/plain")
    elif mime_kind == "python":
        mime = _python_mime(ext)
    else:
        mime = ""

    if mime:
        files = {"file": (f"doc{ext}", io.BytesIO(body_bytes), mime)}
    else:
        files = {"file": (f"doc{ext}", io.BytesIO(body_bytes))}

    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, (
        f"{ext} mime_kind={mime_kind!r} mime={mime!r} → {resp.status_code}: {resp.text}"
    )


def test_rtf_application_rtf_uploads_201(client: TestClient, db, storage_root):
    """L1: .rtf + application/rtf must not 400 (MIME was missing from allow-list)."""
    user = make_user(db, username="rtf_mime")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    body = (
        b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Times;}}\\f0\\fs24 hello rtf}"
    )
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("note.rtf", io.BytesIO(body), "application/rtf")},
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, resp.text


# ── Extensionless: upload reaches extract; content gates decide ───────────


_DATCOM_FOR00X = (
    b"CASEID AlbatrossII\n"
    b" $FLTCON NALPHA=  10.0,\n"
    b"         ALSCHD= -4.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0,\n"
    b"                  12.0, 14.0,\n"
    b"         NMACH=  1.0,\n"
    b"         MACH= 0.102,\n"
    b"         NALT=  1.0,\n"
    b"         ALT= 6000.0,\n"
    b"         WT=  1200.0$\n"
    b"NACA-W-4-7418-33\n"
)


@pytest.mark.parametrize("filename", ["for005", "for006", "README", "Makefile"])
@pytest.mark.parametrize(
    "mime",
    ["application/octet-stream", "text/plain", ""],
)
def test_extensionless_upload_201_under_common_mimes(
    client: TestClient, db, storage_root, filename: str, mime: str,
):
    """Extensionless must not be blocked by MIME — extract decides by content."""
    mime_tag = (mime or "nil").replace("/", "_").replace("-", "")[:12]
    user = make_user(db, username=f"xl_{filename}_{mime_tag}")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    body = _DATCOM_FOR00X if filename.startswith("for") else b"# sample\nline\n"
    if mime:
        files = {"file": (filename, io.BytesIO(body), mime)}
    else:
        files = {"file": (filename, io.BytesIO(body))}
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
        data={"conversation_id": str(conv.id)},
    )
    assert resp.status_code == 201, (
        f"{filename} mime={mime!r} → {resp.status_code}: {resp.text}"
    )


def test_extensionless_datcom_extracts_ok(
    client: TestClient, db, storage_root,
):
    """for006 DATCOM deck: upload + real extract → ok, text present, tokens > 0."""
    user = make_user(db, username="for006ok")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": (
                "for006",
                io.BytesIO(_DATCOM_FOR00X),
                "application/octet-stream",
            ),
        },
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
    assert att.extract_status == "ok"
    assert att.extracted_text is not None
    assert "CASEID AlbatrossII" in att.extracted_text
    assert "\x00" not in att.extracted_text
    assert att.token_count is not None and att.token_count > 0


def test_extensionless_binary_lands_unsupported_no_tokens(
    client: TestClient, db, storage_root,
):
    """Extensionless /bin/ls head: unsupported, no tokens, no NULs stored."""
    try:
        with open("/bin/ls", "rb") as fh:
            blob = fh.read(20 * 1024)
    except OSError:
        blob = bytes(range(256)) * 80
    user = make_user(db, username="forbin")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": ("for005", io.BytesIO(blob), "application/octet-stream"),
        },
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
    assert att.extract_status == "unsupported"
    assert att.extracted_text is None
    assert att.token_count is None
    assert att.extract_error is None or "\x00" not in att.extract_error


def test_low_entropy_uint16_results_dat_unsupported_no_tokens(
    client: TestClient, db, storage_root,
):
    """F5: uint16 CJK-band instrument blob must not bill as ok at CSP layer.

    /bin/ls is high-entropy and dies to several gates; seven of nine
    content-gate mutations still leave all CSP tests green. This fixture
    is the exact F1 false-accept shape (LE uint16 in [20000, 40959]).
    """
    import struct

    values = [20000 + (i % 500) for i in range(4000)]
    blob = struct.pack("<" + "H" * len(values), *values)
    user = make_user(db, username="u16dat")
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": ("results.dat", io.BytesIO(blob), "application/octet-stream"),
        },
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
    assert att.extract_status == "unsupported"
    assert att.extracted_text is None
    assert att.token_count is None
    assert att.extract_error is None or "\x00" not in att.extract_error


def _upload_and_extract(
    client: TestClient, db, *, username: str, filename: str, blob: bytes,
    mime: str = "text/plain",
) -> Attachment:
    user = make_user(db, username=username)
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    resp = client.post(
        "/api/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, io.BytesIO(blob), mime)},
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


def _assert_unsupported_no_tokens(att: Attachment) -> None:
    assert att.extract_status == "unsupported"
    assert att.extracted_text is None
    assert att.token_count is None
    assert att.extract_error is None or "\x00" not in att.extract_error


def test_csp_pure_esc_unsupported_no_tokens(client: TestClient, db, storage_root):
    """I4: 4000×ESC must not bill tokens at the CSP layer."""
    att = _upload_and_extract(
        client, db, username="esc4000", filename="spool.log", blob=b"\x1b" * 4000,
    )
    _assert_unsupported_no_tokens(att)


def test_csp_esc_ff_vt_mix_100kb_unsupported_no_tokens(
    client: TestClient, db, storage_root,
):
    blob = b"\x1b\x0c\x0b" * 40_000
    assert len(blob) >= 100_000
    att = _upload_and_extract(
        client, db, username="esc100k", filename="spool.log", blob=blob,
    )
    _assert_unsupported_no_tokens(att)


@pytest.mark.parametrize("esc_pct,tag", [(0.20, "e20"), (0.50, "e50")])
def test_csp_esc_diluted_printable_unsupported_no_tokens(
    client: TestClient, db, storage_root, esc_pct: float, tag: str,
):
    n = 10_000
    n_esc = int(n * esc_pct)
    body = (b"INFO service ok\n" * 800)[: n - n_esc] + (b"\x1b" * n_esc)
    att = _upload_and_extract(
        client, db, username=f"escdil_{tag}", filename="spool.log", blob=body,
    )
    _assert_unsupported_no_tokens(att)


@pytest.mark.parametrize("ctrl_pct,tag", [(0.30, "c30"), (0.90, "c90")])
def test_csp_exempt_c0_mix_diluted_unsupported_no_tokens(
    client: TestClient, db, storage_root, ctrl_pct: float, tag: str,
):
    n = 10_000
    n_ctrl = int(n * ctrl_pct)
    pads = [0x1B, 0x0C, 0x0B]
    ctrl = bytes(pads[i % 3] for i in range(n_ctrl))
    body = (b"A" * (n - n_ctrl)) + ctrl
    att = _upload_and_extract(
        client, db, username=f"c0mix_{tag}", filename="spool.log", blob=body,
    )
    _assert_unsupported_no_tokens(att)


def _esc_interleave(payload: bytes, k: int = 1) -> bytes:
    out = bytearray()
    for i, b in enumerate(payload):
        out.append(b)
        if (i + 1) % k == 0:
            out.append(0x1B)
    return bytes(out)


@pytest.mark.parametrize(
    "label,factory",
    [
        ("elf", lambda: open("/bin/ls", "rb").read(20_000)),
        ("int32", lambda: __import__("struct").pack(
            "<" + "i" * 4000, *range(1000, 5000),
        )),
        ("tecplot", lambda: b"#!TDV112" + __import__("struct").pack(
            "<" + "f" * 1500, *[float(i) * 0.1 for i in range(1500)],
        )),
    ],
)
def test_csp_esc_interleave_binary_unsupported_no_tokens(
    client: TestClient, db, storage_root, label: str, factory,
):
    """I4: 1:1 ESC interleave must not flip binaries to ok at CSP layer."""
    try:
        blob = factory()
    except OSError:
        if label == "elf":
            blob = bytes(range(256)) * 80
        else:
            raise
    raw = _esc_interleave(blob, k=1)
    att = _upload_and_extract(
        client, db,
        username=f"ileav_{label}",
        filename=f"{label}.dat",
        blob=raw,
        mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att)


@pytest.mark.parametrize(
    "line,tag",
    [
        ("\x1b[32mINFO\x1b[0m  service started ok\n", "a59"),
        ("\x1b[32mINFO\x1b[0m \x1b[36mmod\x1b[0m service started ok\n", "a93"),
    ],
)
def test_csp_ansi_coloured_log_extracts_ok_byte_exact(
    client: TestClient, db, storage_root, line: str, tag: str,
):
    """I3: ANSI CSI logs still extract ok at CSP (byte-exact)."""
    blob = (line * 40).encode("ascii")
    att = _upload_and_extract(
        client, db, username=f"ansi_{tag}", filename="app.log", blob=blob,
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == blob.decode("ascii")
    assert att.token_count is not None and att.token_count > 0


def test_csp_for006_ff_stray_byte_exact(client: TestClient, db, storage_root):
    """A4: Fortran FF page break + one stray \\xff → ok, sole U+FFFD."""
    body = (
        "TITLE DATCOM FOR006 LISTING\n"
        + ("CL = 0.42  CD = 0.031  XCP = -0.12\n" * 8)
        + "\x0c"
        + ("PAGE 2 CONTINUED\n" * 4)
    )
    blob = body.encode("ascii") + b"\xff"
    att = _upload_and_extract(
        client, db, username="for006ff", filename="for006", blob=blob,
        mime="application/octet-stream",
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == body + "\ufffd"
    assert att.extracted_text.count("\ufffd") == 1
    assert "\x0c" in att.extracted_text


@pytest.mark.parametrize("ctrl,tag", [("\x0c", "ff"), ("\x0b", "vt")])
def test_csp_ff_or_vt_alone_byte_exact(
    client: TestClient, db, storage_root, ctrl: str, tag: str,
):
    sample = ("LINE OF OUTPUT DATA FOLLOWS\n" * 10) + ctrl + ("MORE\n" * 5)
    att = _upload_and_extract(
        client, db, username=f"pg_{tag}", filename="listing.out",
        blob=sample.encode("ascii"),
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == sample


def test_csp_csv_crlf_byte_exact(client: TestClient, db, storage_root):
    sample = "id,name\r\n1,alpha\r\n2,beta\r\n"
    att = _upload_and_extract(
        client, db, username="csvcrlf", filename="scores.csv",
        blob=sample.encode("ascii"), mime="text/csv",
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == sample


def test_csp_python_escaped_and_literal_esc_byte_exact(
    client: TestClient, db, storage_root,
):
    escaped = (
        "#!/usr/bin/env python3\n"
        "reset = \"\\x1b[0m\"\n"
        + ("print(reset)\n" * 20)
    )
    literal = (
        "#!/usr/bin/env python3\n"
        "reset = \"\x1b[0m\"\n"
        + ("print(reset)\n" * 20)
    )
    att_e = _upload_and_extract(
        client, db, username="pyesc", filename="esc_escaped.py",
        blob=escaped.encode("utf-8"), mime="text/x-python",
    )
    att_l = _upload_and_extract(
        client, db, username="pylit", filename="esc_literal.py",
        blob=literal.encode("utf-8"), mime="text/x-python",
    )
    assert att_e.extract_status == "ok" and att_e.extracted_text == escaped
    assert att_l.extract_status == "ok" and att_l.extracted_text == literal


@pytest.mark.parametrize(
    "filename",
    [
        "test.dcm", "for005", "for006", "results.dat",
        "test.out", "test.inp", "test.txt", "deck",
    ],
)
@pytest.mark.parametrize(
    "source",
    ["/home/c1147259/下載/test.dcm", "/home/c1147259/下載/test.out"],
)
def test_csp_owner_datcom_byte_exact_under_every_name(
    client: TestClient, db, storage_root, filename: str, source: str,
):
    """A4: owner DATCOM fixtures stay byte-exact under every text-class name.

    C4 decision: host-conditional on purpose — the owner's own two files
    cannot be synthesised. The always-running CSP twin is
    ``test_extensionless_datcom_extracts_ok`` (inline deck).
    """
    from pathlib import Path

    path = Path(source)
    if not path.is_file():
        pytest.skip(f"owner fixture missing: {source}")
    blob = path.read_bytes()
    tag = f"{path.stem}_{filename}".replace(".", "")[:20]
    att = _upload_and_extract(
        client, db, username=f"od_{tag}", filename=filename, blob=blob,
        mime="application/octet-stream",
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == blob.decode("utf-8")


def test_csp_cjk_utf8_ok_utf16_refused_with_actionable_notice(
    client: TestClient, db, storage_root,
):
    """Contract at the CSP layer: UTF-8 CJK bills; UTF-16 CJK does not.

    The refusal must arrive in the chat notice as an instruction the user
    can act on in three seconds — a stated limit, not a mystery failure.
    """
    from app.services.attachment_context import build_attachment_prompt_block

    sample = "姓名,單位,代號\n王小明,資訊室,A01\n" * 10
    att8 = _upload_and_extract(
        client, db, username="cjk8", filename="roster.txt",
        blob=sample.encode("utf-8"),
    )
    att16 = _upload_and_extract(
        client, db, username="cjk16", filename="roster.txt",
        blob=sample.encode("utf-16"),
    )
    assert att8.extract_status == "ok" and att8.extracted_text == sample
    assert att8.token_count is not None and att8.token_count > 0
    assert att16.extract_status == "unsupported"
    assert att16.extracted_text is None
    assert att16.token_count is None
    notice = build_attachment_prompt_block([att16], admitted_ids=set())
    assert notice is not None
    assert "roster.txt" in notice
    assert "非 ASCII" in notice
    assert "UTF-8" in notice and "另存" in notice
    for leak in ("parser_registry", "anila_core", "/home/", "\x00"):
        assert leak not in notice


def test_csp_bomless_utf16_mixed_ascii_cjk_refused_actionable(
    client: TestClient, db, storage_root,
):
    """BOM-less UTF-16 is unsupported; extract_error carries re-save guidance."""
    from app.services.attachment_context import build_attachment_prompt_block

    sample = ("id,name,score\n" * 5) + ("姓名,王小明,A01\n" * 10)
    att = _upload_and_extract(
        client, db, username="cjkmix", filename="roster.txt",
        blob=sample.encode("utf-16-le"),
    )
    assert att.extract_status == "unsupported"
    assert att.token_count is None
    assert att.extracted_text is None
    assert att.extract_error is not None
    assert "UTF-8" in att.extract_error
    assert "另存" in att.extract_error
    # T4: the actionable reason reaches the chat notice, not only the DB.
    notice = build_attachment_prompt_block([att], admitted_ids=set())
    assert notice is not None
    assert "roster.txt" in notice
    assert "另存" in notice
    assert "UTF-8" in notice
    assert "parser_registry" not in notice
    assert "/home/" not in notice
    assert "anila_core" not in notice


def test_csp_sparse_latin1_lossy_ok(client: TestClient, db, storage_root):
    """A4: sparse latin-1 (~1.5% U+FFFD) stays owner-accepted lossy ok."""
    blob = b"".join(
        b"status update line %02d system ok cafe\xe9 resume notes\n" % i
        for i in range(40)
    )
    att = _upload_and_extract(
        client, db, username="lat1", filename="notes.txt", blob=blob,
    )
    assert att.extract_status == "ok"
    assert att.extracted_text is not None
    ratio = att.extracted_text.count("\ufffd") / len(att.extracted_text)
    assert 0.01 < ratio < 0.05


@pytest.mark.parametrize(
    "label,blob",
    [
        ("del", b"\x7f" * 4000),
        ("c1", "\x85".encode("utf-8") * 4000),
        ("iaa", "\ufff9".encode("utf-8") * 2000),
        ("zwsp", "\u200b".encode("utf-8") * 4000),
    ],
)
def test_csp_non_graphic_scalars_unsupported_no_tokens(
    client: TestClient, db, storage_root, label: str, blob: bytes,
):
    """A3 at CSP: non-graphic-only blobs must not bill tokens."""
    att = _upload_and_extract(
        client, db, username=f"ng_{label}", filename=f"{label}.txt", blob=blob,
    )
    _assert_unsupported_no_tokens(att)


def test_csp_space_interleave_elf_unsupported_no_tokens(
    client: TestClient, db, storage_root,
):
    """Round-10 headline: ELF interleaved with 0x20 must not extract ok."""
    try:
        elf = open("/bin/true", "rb").read(4096)
    except OSError:
        elf = bytes(range(256)) * 16
    raw = bytearray()
    for b in elf:
        raw.append(b)
        raw.append(0x20)
    att = _upload_and_extract(
        client, db, username="elfsp", filename="evil.txt",
        blob=bytes(raw), mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att)


# ── Round-12 CSP pins: each of F1–F4 is load-bearing at the attachment path ──


def test_csp_f1_diluted_latin1_filler_elf_unsupported(
    client: TestClient, db, storage_root,
):
    """F1 pin: Latin-1-diluted filler + ELF slice must not bill tokens.

    High-NUL BOM-less refusal catches this weave (no UTF-16 speculation).
    """
    import random as _rnd

    try:
        elf = open("/bin/ls", "rb").read()
        payload = elf[12849 : 12849 + 4000]
        if len(payload) < 500:
            payload = elf[:4000]
    except OSError:
        payload = bytes(range(256)) * 16
    alphabet = list(range(0x41, 0x49))
    latin = [0xA1, 0xAB, 0xBB, 0xA0]
    _rnd.seed(42)
    fs = [
        _rnd.choice(latin) if _rnd.random() < 0.30 else alphabet[i % len(alphabet)]
        for i in range(len(payload))
    ]
    raw = bytearray()
    for i, b in enumerate(payload):
        raw.append(fs[i])
        raw.append(b)
    att = _upload_and_extract(
        client, db, username="f1dil", filename="evil.dat",
        blob=bytes(raw), mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att)


def test_csp_bom_prefixed_binary_slice_unsupported(
    client: TestClient, db, storage_root,
):
    """I2: a BOM in front of a binary slice must not buy acceptance.

    C4: self-generating. The old version skipped whenever ``/bin/ls`` was
    absent or its layout shifted, so on most hosts this pin silently did
    nothing. The synthetic slice always runs; the real ``/bin/ls`` offset
    is an EXTRA case when the host still has it.
    """
    import struct

    cases: list[tuple[str, bytes]] = []
    synth = b"\xfe\xff" + struct.pack(
        ">" + "H" * 2000, *[(0x4E00 + (i * 7919) % 0x4000) for i in range(2000)],
    )
    cases.append(("synth", synth))
    try:
        blob = open("/bin/ls", "rb").read()[94336:98336]
        if len(blob) == 4000:
            cases.append(("ls94336", blob))
            if not blob.startswith(b"\xfe\xff"):
                cases.append(("ls94336_bom", b"\xfe\xff" + blob))
    except OSError:
        pass
    assert len(cases) >= 1
    for tag, body in cases:
        att = _upload_and_extract(
            client, db, username=f"bomslice_{tag}"[:24], filename="slice.dat",
            blob=body, mime="application/octet-stream",
        )
        assert att.extract_status != "ok", tag
        assert att.token_count is None, tag
        assert att.extracted_text is None, tag


def test_csp_requirements_txt_ascii_byte_exact(
    client: TestClient, db, storage_root,
):
    """T1/T3: clean ASCII UTF-8 is never re-decoded as UTF-16.

    Re-introducing ``_prefer_utf16_over_utf8_mojibake`` turns this RED.
    """
    blob = b"anila-core>=0.1.0\nuvicorn[standard]>=0.29\n"
    att = _upload_and_extract(
        client, db, username="reqtxt", filename="requirements.txt",
        blob=blob,
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == blob.decode("ascii")
    assert att.token_count is not None and att.token_count > 0


def test_csp_parity_smuggle_low_nul_unsupported(
    client: TestClient, db, storage_root,
):
    """T3: tiny-filler parity weave stays unsupported via parity-smuggle.

    Diverse ASCII on one lane + constant ``a`` on the other is clean UTF-8
    (no FFFD / low C0) but one-sided — only ``_parity_lanes_show_filler_smuggle``
    blocks the clean accept. Reverting that check turns this RED.
    """
    alphabet = bytes(range(32, 127))
    payload = alphabet * 50
    blob = bytearray()
    for b in payload:
        blob.append(ord("a"))
        blob.append(b)
    att = _upload_and_extract(
        client, db, username="psmug", filename="evil.txt", blob=bytes(blob),
    )
    _assert_unsupported_no_tokens(att)


def test_csp_ascii_dominant_utf16_ok_byte_exact(
    client: TestClient, db, storage_root,
):
    """Accept side of the contract at CSP: ASCII-dominant UTF-16 bills.

    Real mixed document shape (English body with a Chinese minority), not a
    repeated phrase — C3. Punctuation and vocabulary spread are irrelevant.
    """
    sample = (
        "deployment checklist\n"
        "1. rebuild csp image\n"
        "2. reload nginx after compose up\n"
        "owner 王小明 資訊室\n"
    ) * 25
    assert sum(1 for c in sample if ord(c) < 0x80) * 2 > len(sample)
    att = _upload_and_extract(
        client, db, username="u16ascii", filename="report.txt",
        blob=b"\xff\xfe" + sample.encode("utf-16-le"),
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == sample
    assert att.token_count is not None and att.token_count > 0


def test_csp_han_utf16_refused_utf8_twin_ok(
    client: TestClient, db, storage_root,
):
    """Reject side: the same Han body bills in UTF-8 and not in UTF-16.

    Fixture is real prose from ``SYSTEM-MAP.md`` (uniq_ratio ≈ 0.53), the
    population this path serves — a repeated phrase (uniq_ratio ≈ 0.01)
    hid the round-13 defect.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "SYSTEM-MAP.md"
    assert src.is_file(), f"prose fixture source missing: {src}"
    han = [c for c in src.read_text(encoding="utf-8") if 0x4E00 <= ord(c) <= 0x9FFF]
    sample = "".join(han[:400])
    assert 0.40 <= len(set(sample)) / len(sample) <= 0.70

    att16 = _upload_and_extract(
        client, db, username="hanu16", filename="prose.txt",
        blob=sample.encode("utf-16"),
    )
    _assert_unsupported_no_tokens(att16)
    assert "另存" in (att16.extract_error or "")
    att8 = _upload_and_extract(
        client, db, username="hanu8", filename="prose.txt",
        blob=sample.encode("utf-8"),
    )
    assert att8.extract_status == "ok"
    assert att8.extracted_text == sample
    assert att8.token_count is not None and att8.token_count > 0


def test_csp_f2_uint16_band_still_unsupported(
    client: TestClient, db, storage_root,
):
    """Reject side: BOM'd uint16 ramp must stay unsupported at CSP.

    BOM-less bands are refused by the no-speculation path; this pin uses a
    BOM so ``_is_ascii_dominant`` is the load-bearing check. Neutering it
    turns this RED.
    """
    import struct

    blob = b"\xff\xfe" + struct.pack(
        "<" + "H" * 4000, *([(20000 + i) & 0xFFFF for i in range(4000)]),
    )
    att = _upload_and_extract(
        client, db, username="f2band", filename="results.dat",
        blob=blob, mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att)


def test_csp_f3_scattered_strays_ok_not_absolute_budget(
    client: TestClient, db, storage_root,
):
    """F3 pin: absolute residual budget must not reject scattered strays.

    ~5 KB listing + 10 uniform 0x1A: density ≤0.2% and gaps ≫64 so clustering
    accepts; the old ``count > 2`` absolute budget rejects. Sized under the
    test store-cap (1500 tokens) so ``too_large`` cannot mask the pin.
    Reverting to abs>2 turns this RED.
    """
    listing = ("      SUBROUTINE FOO(X)\n      X = X + 1.0\n" * 200)[:5000]
    body = bytearray(listing.encode("ascii"))
    assert len(body) == 5000
    step = len(body) // 10
    for i in range(10):
        body[i * step] = 0x1A
    att = _upload_and_extract(
        client, db, username="f3stray", filename="for006.out",
        blob=bytes(body), mime="application/octet-stream",
    )
    assert att.extract_status == "ok"
    assert att.extracted_text is not None and "SUBROUTINE FOO" in att.extracted_text
    assert att.token_count is not None and att.token_count > 0


def test_csp_f4_fffd_short_binary_unsupported(
    client: TestClient, db, storage_root,
):
    """F4/I-3' pin: U+FFFD must not satisfy the short-file readable floor.

    ``abc\\xff``: ASCII ratio 0.75, replacement 0.25 — inside short-floor
    budgets — but readable_ratio is 0.75 when U+FFFD is excluded (<0.85),
    so unsupported. Counting U+FFFD as readable lifts it to 1.0 → ok.
    """
    att = _upload_and_extract(
        client, db, username="f4fffd", filename="junk.dat",
        blob=b"abc\xff", mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att)
    # Pure FFFD blob must also stay unsupported at any short length.
    att2 = _upload_and_extract(
        client, db, username="f4fffd2", filename="junk2.dat",
        blob=b"\xff" * 199, mime="application/octet-stream",
    )
    _assert_unsupported_no_tokens(att2)


def test_csp_f4_diagram_box_drawing_ok(
    client: TestClient, db, storage_root,
):
    """F4 pin (accept): So box-drawing stays readable at the CSP layer."""
    box = (
        "┌──────────┬──────────┐\n"
        "│  CSP     │  Router  │\n"
        "├──────────┼──────────┤\n"
        "│  Redis   │  Postgres│\n"
        "└──────────┴──────────┘\n"
    ) * 5
    att = _upload_and_extract(
        client, db, username="f4box", filename="arch.txt",
        blob=box.encode("utf-8"),
    )
    assert att.extract_status == "ok"
    assert att.extracted_text == box


# ── Round-14: I2 minimal pairs end-to-end, and message routing ─────────────


def _extract_many(client: TestClient, db, *, username: str,
                  items: list[tuple[str, bytes]]) -> dict[str, Attachment]:
    """Upload + extract several bodies under one user/conversation."""
    user = make_user(db, username=username)
    conv = _make_conv(db, user)
    token = login(client, username=user.username)
    out: dict[str, Attachment] = {}
    for tag, blob in items:
        resp = client.post(
            "/api/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("probe.dat", io.BytesIO(blob),
                            "application/octet-stream")},
            data={"conversation_id": str(conv.id)},
        )
        assert resp.status_code == 201, f"{tag}: {resp.status_code} {resp.text}"
        att = (
            db.query(Attachment)
            .filter(Attachment.reference_id == resp.json()["reference_id"])
            .one()
        )
        extract_attachment_text(att.id, db=db)
        db.refresh(att)
        out[tag] = att
    return out


def _a1_minimal_pair_bodies() -> dict[str, bytes]:
    """The bodies that round 13 billed once a BOM was prefixed."""
    import struct

    bodies: dict[str, bytes] = {}
    for fill in (0xCCCC, 0x4E00, 0xAAAA, 0x8080, 0x1234, 0x30A0, 0xAC00,
                 0x2500, 0xE000, 0xFFFF):
        bodies[f"fill_{fill:04x}"] = struct.pack("<" + "H" * 1500,
                                                 *([fill] * 1500))
    try:
        bodies["dircolors_27315"] = open("/bin/dircolors", "rb").read()[27315:31315]
    except OSError:
        pass
    try:
        ls = open("/bin/ls", "rb").read()
        for off in (94336, 12849, 128021, 133825, 135689):
            chunk = ls[off:off + 4000]
            if len(chunk) == 4000:
                bodies[f"ls_{off}"] = chunk
    except OSError:
        pass
    bodies["han1500_utf16le"] = ("中" * 1500).encode("utf-16-le")
    return bodies


def test_csp_i2_bom_prefix_never_creates_tokens(
    client: TestClient, db, storage_root,
):
    """I2 end-to-end: every listed body refuses WITH and WITHOUT a BOM.

    ``han1500_utf16le`` without a BOM is the T1 exception and is asserted
    separately below: those bytes are valid ASCII UTF-8 (``-N-N…``), so the
    UTF-8 reading wins — the file is never re-decoded as UTF-16 mojibake.
    """
    bodies = _a1_minimal_pair_bodies()
    assert len(bodies) >= 12, f"too few bodies exercised: {len(bodies)}"
    items: list[tuple[str, bytes]] = []
    for tag, body in bodies.items():
        items.append((f"{tag}|bom", b"\xff\xfe" + body))
        if tag != "han1500_utf16le":
            items.append((f"{tag}|raw", body))
    atts = _extract_many(client, db, username="i2pairs", items=items)
    billed = [
        tag for tag, att in atts.items()
        if att.extract_status == "ok" or att.token_count is not None
    ]
    assert billed == [], f"BOM/raw bodies billed tokens: {billed}"
    for tag, att in atts.items():
        assert att.extracted_text is None, tag

    # T1 exception, stated explicitly rather than skipped. Sized under the
    # test store cap (1500 tokens) so ``too_large`` cannot mask the pin.
    han_raw = ("中" * 300).encode("utf-16-le")
    t1 = _extract_many(
        client, db, username="i2t1", items=[("han_raw", han_raw)],
    )["han_raw"]
    assert t1.extract_status == "ok"
    assert t1.extracted_text == han_raw.decode("ascii")
    assert "中" not in (t1.extracted_text or "")


def test_csp_refusal_notice_advice_matches_cause(
    client: TestClient, db, storage_root,
):
    """I3/I4 at the chat notice: five causes, five different sentences.

    Round 16 added the fifth: a body that IS valid UTF-8 but whose scalars
    the platform does not accept (a whitespace-free run of >= 24 distinct
    two-byte letters; prose that is mostly combining marks). Those two used
    to accept, via the exemptions removed this round, and must now be
    refused by NAME of the limit — telling a user that a perfectly valid
    UTF-8 file "is not a readable text file" is the failure I3 forbids.
    """
    import struct
    import zlib

    from app.services.attachment_context import build_attachment_prompt_block

    def _png() -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
        raw = b"".join(
            b"\x00" + bytes([(x * 7 + y * 13) % 256 for x in range(120)])
            for y in range(40)
        )
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 40, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    try:
        elf = open("/bin/ls", "rb").read()
    except OSError:
        elf = bytes(range(256)) * 500
    items = [
        ("zh_utf16", ("這是一段以中文為主的內部報告內容。\n" * 40).encode("utf-16")),
        ("elf", elf),
        ("big5", ("姓名,單位,代號\n王小明,資訊室,A01\n" * 40).encode("big5")),
        ("png", _png()),
        # Valid UTF-8, refused on content: a space-free Cyrillic run and
        # Devanagari prose (66% combining marks).
        ("utf8_run",
         ("абвгдежзийклмнопрстуфхцчшщъыьэюя" * 13)[:400].encode("utf-8")),
        ("utf8_marks",
         (("यह एक आंतरिक नेटवर्क दस्तावेज़ है जिसमें विवरण दिया गया है " * 4)[:200]
          ).encode("utf-8")),
    ]
    atts = _extract_many(client, db, username="advice", items=items)
    notices = {}
    for tag, att in atts.items():
        _assert_unsupported_no_tokens(att)
        notice = build_attachment_prompt_block([att], admitted_ids=set())
        assert notice is not None, tag
        notices[tag] = notice
        for leak in ("parser_registry", "anila_core", "/home/", "Traceback",
                     "Supported:", "\x00"):
            assert leak not in notice, f"{tag} leaked {leak!r}: {notice}"

    assert "非 ASCII" in notices["zh_utf16"] and "另存" in notices["zh_utf16"]
    assert "Big5" in notices["big5"] and "另存" in notices["big5"]
    for tag in ("elf", "png"):
        assert "不是可讀的文字檔" in notices[tag], notices[tag]
        assert "另存" not in notices[tag], (
            f"{tag} must not be told to re-save as UTF-8: {notices[tag]}"
        )
    # I3: a valid UTF-8 body must never be called "not a readable text file".
    for tag in ("utf8_run", "utf8_marks"):
        assert "不是可讀的文字檔" not in notices[tag], notices[tag]
        assert "可解讀為 UTF-8" in notices[tag], notices[tag]
        assert "支援" in notices[tag] and "中文" in notices[tag], notices[tag]
    assert len({notices[k] for k in ("big5", "zh_utf16", "png", "utf8_run")}) == 4
    assert notices["utf8_run"].split("：", 1)[-1] == (
        notices["utf8_marks"].split("：", 1)[-1]
    ), "one limit, one sentence"
