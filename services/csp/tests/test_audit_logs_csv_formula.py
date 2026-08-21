"""Site 1 — audit CSV export already has ``_csv_safe``; lock it with tests.

Behaviour of ``_csv_safe`` is not expanded here (4-char historical set).
Removing the call around ``writer.writerow`` must turn these tests red.
"""
from __future__ import annotations

import csv
import io
import os

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.api.audit_logs import _csv_safe
from app.models.audit_log import AuditLog
from tests.conftest import login, make_user


_SITE1_TRIGGERS = ("=", "+", "-", "@")


def _export_rows(client) -> list[list[str]]:
    resp = client.get("/api/audit-logs/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    assert text[:1] == "\ufeff"
    body_lines = [ln for ln in text[1:].splitlines() if not ln.startswith("#")]
    return list(csv.reader(io.StringIO("\n".join(body_lines))))


def _detail_cell(rows: list[list[str]]) -> str:
    header = rows[0]
    idx = header.index("detail")
    assert len(rows) >= 2, rows
    return rows[1][idx]


@pytest.mark.parametrize("prefix", _SITE1_TRIGGERS)
def test_csv_safe_prefixes_each_historical_trigger(prefix: str):
    payload = f"{prefix}1+1"
    out = _csv_safe(payload)
    assert out[:1] == "'"
    assert out[1:] == payload


def test_csv_safe_leaves_plain_text_alone():
    assert _csv_safe("login") == "login"
    assert _csv_safe(None) == ""


@pytest.mark.parametrize("prefix", _SITE1_TRIGGERS)
def test_audit_export_prefixes_formula_in_detail(
    client, db: Session, prefix: str
):
    """Call-site lock: writerow must keep wrapping values with ``_csv_safe``.

    A unit test of ``_csv_safe`` alone would stay green if the export stopped
    calling it.
    """
    make_user(db, username="audit-formula-admin", role="admin")
    payload = f"{prefix}cmd|'/c calc'!A0"
    db.add(
        AuditLog(
            actor_username="attacker",
            action="test-action",
            resource_type="test",
            status="success",
            detail=payload,
        )
    )
    db.commit()
    login(client, "audit-formula-admin")
    cell = _detail_cell(_export_rows(client))
    assert cell[:1] == "'"
    assert cell[1:] == payload


def test_export_header_collapses_newline_and_neutralizes_formula_username(
    client, db: Session
):
    """Export-time only: a stored admin username with a leading '=' and a
    newline cannot forge a second header line or a formula row.

    Registration / username charset are not changed; the user is created
    with a plain name, then the stored value is mutated.
    """
    user = make_user(db, username="header-nl-admin", role="admin")
    login(client, "header-nl-admin")
    user.username = "=1+1\n# 稽核鏈鏈頭: forged"
    db.add(user)
    db.commit()

    resp = client.get("/api/audit-logs/export")
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert text[:1] == "\ufeff"
    body = text[1:]
    lines = body.splitlines()
    exporter_lines = [ln for ln in lines if ln.startswith("# 匯出者:")]
    assert len(exporter_lines) == 1, lines[:20]
    line = exporter_lines[0]
    rest = line[len("# 匯出者: ") :]
    assert rest[:1] == "'"
    assert rest[1:].startswith("=1+1")
    assert not any(ln.strip() == "# 稽核鏈鏈頭: forged" for ln in lines)
