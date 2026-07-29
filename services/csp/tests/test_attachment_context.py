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
from app.config import settings
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
from app.services.attachment_service import (
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
def _budget_knobs(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_DEFAULT_CONTEXT_WINDOW", _TEST_WINDOW)
    monkeypatch.setattr(settings, "ANILA_ATTACHMENT_BUDGET_RATIO", _TEST_RATIO)
    monkeypatch.setattr(settings, "ANILA_ATTACHMENT_TOKEN_SAFETY", _TEST_SAFETY)
    monkeypatch.setattr(
        settings, "ANILA_ATTACHMENT_MAX_STORED_TOKENS", _TEST_STORE_CAP,
    )


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(settings, "ATTACHMENT_STORAGE_PATH", str(root))
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

    budget = attachment_budget_tokens(_TEST_WINDOW)
    admitted, excluded = admit(_ordered_ok(db, conv.id), budget)
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
    budget = attachment_budget_tokens(_TEST_WINDOW)
    admitted, excluded = admit(_ordered_ok(db, conv.id), budget)
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

    budget = attachment_budget_tokens(_TEST_WINDOW)
    admitted, excluded = admit(_ordered_ok(db, conv.id), budget)
    assert atts[2].id in excluded
    assert all(a.extract_status == "ok" for a in atts)

    # Delete via service (no recheck inside).
    delete_attachment(db, atts[0].reference_id, user)

    remaining = _ordered_ok(db, conv.id)
    assert len(remaining) == 2
    admitted2, excluded2 = admit(remaining, budget)
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
    assert usage["used_tokens"] == effective_cost(att.token_count)
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
    assert usage["over_budget_tokens"] == effective_cost(2_000_000)
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
    assert effective_cost(raw) == int(raw * _TEST_SAFETY)


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
    assert exc.value.status_code == 403

    # HTTP endpoint must refuse too.
    token = login(client, username="att-attacker")
    resp = client.get(
        f"/api/conversations/{conv.id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


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
    assert resp.status_code == 403
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
    monkeypatch.setattr(settings, "ANILA_DEFAULT_CONTEXT_WINDOW", 128_000)
    monkeypatch.setattr(settings, "ANILA_ATTACHMENT_BUDGET_RATIO", 0.7)
    monkeypatch.setattr(settings, "ANILA_ATTACHMENT_TOKEN_SAFETY", 1.15)
    monkeypatch.setattr(
        settings, "ANILA_ATTACHMENT_MAX_STORED_TOKENS", 800_000,
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
    assert effective_cost(att.token_count) <= int(128_000 * 0.7)


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

    budget = attachment_budget_tokens(_TEST_WINDOW)
    admitted, excluded = admit(_ordered_ok(db, conv.id), budget)
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

    budget = attachment_budget_tokens(_TEST_WINDOW)
    _, excluded = admit(_ordered_ok(db, conv.id), budget)
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
