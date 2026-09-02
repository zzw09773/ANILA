"""The audit export's ``# 匯出者:`` header line is written with ``buf.write``,
not ``csv.writer`` — so ``,`` and ``"`` inside the admin's username carry CSV
meaning (MEDIUM finding, materials 2026-08-24).

Two damages, two assertions:

1. structure — a ``"`` opens a quoted field that swallows the column header
   and every data row after it; the export stops being readable as an audit
   record. Assert: the exporter line is one cell, the column header row is
   found intact, and the number of data rows equals the rows in the DB.
2. formula — ``evil,=HYPERLINK(...)`` puts ``=HYPERLINK`` at the start of a
   *second* cell, where first-character neutralisation of the username never
   looks. Assert over the parsed CSV (every cell of every row, header lines
   included), not over string prefixes.

Both must be red on the unfixed export.
"""

from __future__ import annotations

import csv
import io
import itertools
import os

from sqlalchemy.orm import Session

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.api.audit_logs import _EXPORT_COLUMNS
from app.models.audit_log import AuditLog
from app.utils.csv_formula import FORMULA_TRIGGER_PREFIXES
from tests.conftest import login, make_user

_PAYLOAD_USERNAME = 'evil,=HYPERLINK("http://x","PWNED")'


def _all_rows(client) -> list[list[str]]:
    """Parse the *whole* export with ``csv.reader`` — header lines included.

    Filtering ``#`` lines first (as the older helper does) would hide the
    exact place the defect lives.
    """
    resp = client.get("/api/audit-logs/export")
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert text[:1] == "﻿"
    return list(csv.reader(io.StringIO(text[1:])))


def _login_then_rename(client, db: Session, username: str):
    user = make_user(db, username="structure-admin", role="admin")
    login(client, "structure-admin")
    user.username = username
    db.add(user)
    db.commit()
    return user


def test_exporter_username_cannot_open_a_second_column_or_swallow_rows(
    client, db: Session
):
    _login_then_rename(client, db, _PAYLOAD_USERNAME)
    rows = _all_rows(client)

    exporter_rows = [r for r in rows if r and r[0].startswith("# 匯出者:")]
    assert len(exporter_rows) == 1, rows[:8]
    assert len(exporter_rows[0]) == 1, exporter_rows[0]

    assert list(_EXPORT_COLUMNS) in rows, rows[:12]
    columns_at = rows.index(list(_EXPORT_COLUMNS))
    data_rows = rows[columns_at + 1 :]
    assert len(data_rows) == db.query(AuditLog).count()
    assert all(len(r) == len(_EXPORT_COLUMNS) for r in data_rows)


def test_no_parsed_cell_starts_with_a_formula_trigger(client, db: Session):
    _login_then_rename(client, db, _PAYLOAD_USERNAME)
    rows = _all_rows(client)
    offenders = [
        cell
        for cell in itertools.chain.from_iterable(rows)
        if cell[:1] in FORMULA_TRIGGER_PREFIXES
    ]
    assert offenders == [], offenders


def test_header_shape_is_independent_of_the_exporter_name(client, db: Session):
    """Acceptance ②: row count and per-row column count do not change with
    the username. Login events are audit rows themselves, so the two exports
    are taken by the *same* session, renamed in between."""
    user = _login_then_rename(client, db, "plain-admin")
    before = _all_rows(client)
    user.username = _PAYLOAD_USERNAME
    db.add(user)
    db.commit()
    after = _all_rows(client)
    assert len(after) == len(before)
    assert [len(r) for r in after] == [len(r) for r in before]
