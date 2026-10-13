"""兩版 Word 的條號比對。內文是虛構的社團規定，不是真實規章。"""

from __future__ import annotations

import io
import os
import zipfile
from types import SimpleNamespace
from xml.sax.saxutils import escape

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from tests.conftest import make_user

_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(client, username="alice"):
    resp = client.post("/api/auth/login", json={"username": username, "password": "password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _docx(paragraphs: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", rels)
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def _files(before: bytes, after: bytes, before_name="old.docx", after_name="new.docx"):
    return {
        "before": (before_name, before, _DOCX_TYPE),
        "after": (after_name, after, _DOCX_TYPE),
    }


def test_compare_requires_login(client):
    resp = client.post(
        "/api/document-diffs",
        files=_files(_docx(["第一條", "茶會在週三。"]), _docx(["第一條", "茶會在週四。"])),
    )
    assert resp.status_code == 401


def test_compare_rejects_non_docx(client, db):
    make_user(db, "alice")
    resp = client.post(
        "/api/document-diffs",
        files=_files(b"hello", b"hello", "notes.txt", "other.txt"),
        headers=_auth(client),
    )
    assert resp.status_code == 400
    assert "docx" in resp.json()["detail"]


def test_compare_returns_article_changes_and_full_original(client, db):
    make_user(db, "alice")
    before = _docx(["第一條", "圖書館週一休息。", "第二條", "茶會在週三。自備杯子。"])
    after = _docx(["第一條", "圖書館週一休息。", "第二條", "茶會在週四。自備杯子。"])
    resp = client.post(
        "/api/document-diffs",
        files=_files(before, after),
        headers=_auth(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_complete"] is True
    assert body["model_status"] == "not_requested"
    assert body["explanations"] == []
    assert [change["id"] for change in body["changes"]] == ["diff:2:modified"]
    assert "茶會在週三。自備杯子。" in body["changes"][0]["before"]
    assert "茶會在週四。自備杯子。" in body["changes"][0]["after"]


def test_unparsed_docx_does_not_claim_no_other_differences(client, db):
    make_user(db, "alice")
    resp = client.post(
        "/api/document-diffs",
        files=_files(_docx(["今天茶會改到週四。"]), _docx(["今天茶會仍在週三。"])),
        headers=_auth(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 沒有條號時改依段落比對：有差異卡片，但仍不宣稱完整。
    assert [change["id"] for change in body["changes"]] == ["diff:1:modified"]
    assert body["parse_complete"] is False
    assert "沒有找到差異" not in body["summary"]
    assert "不能據此" in body["summary"]


def test_explain_keeps_only_ids_the_program_emitted(client, db, monkeypatch):
    make_user(db, "alice")
    captured: list[dict] = []

    def fake_resolve(_db, role):
        assert role == "summary"
        return SimpleNamespace(status="ok", model=SimpleNamespace(name="summary-test", id=1))

    async def fake_complete(_db, _model, body, **_kwargs):
        captured.append(body)
        return (
            '[{"id":"diff:9:added","text":"第九條是捏造的。"},'
            '{"id":"diff:2:modified","text":"茶會從週三改到週四。"}]'
        )

    monkeypatch.setattr("app.services.model_roles.resolve_role", fake_resolve)
    monkeypatch.setattr("app.services.internal_llm.complete_chat", fake_complete)

    before = _docx(["第一條", "圖書館週一休息。UNCHANGED_MARK", "第二條", "茶會在週三。"])
    after = _docx(["第一條", "圖書館週一休息。UNCHANGED_MARK", "第二條", "茶會在週四。"])
    resp = client.post(
        "/api/document-diffs/explain",
        files=_files(before, after),
        headers=_auth(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [change["id"] for change in body["changes"]] == ["diff:2:modified"]
    assert body["explanations"] == [
        {"id": "diff:2:modified", "text": "茶會從週三改到週四。"}
    ]
    assert body["model_status"] == "filtered"
    prompt = captured[0]["messages"][1]["content"]
    assert "UNCHANGED_MARK" not in prompt
    assert "diff:2:modified" in prompt
    assert "diff:9:added" not in prompt
