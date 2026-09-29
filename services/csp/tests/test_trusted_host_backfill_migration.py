"""只刪自動匯入的 compose 內部名稱，其餘信任主機留下。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.trusted_host import TrustedHost
from tests.conftest import make_user

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "r1_0065_drop_compose_internal_trusted_host_backfill.py"
)
_NOTE = "imported from ANILA_TRUSTED_HOSTS env at startup"


def _migration():
    spec = importlib.util.spec_from_file_location(_MIGRATION.stem, _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add(db, host: str, *, note: str, user_id: int | None = None) -> None:
    db.add(
        TrustedHost(
            host=host,
            note=note,
            created_by_user_id=user_id,
        )
    )


def test_drops_only_auto_imported_compose_internal_names(db):
    owner = make_user(db, username="host-owner")
    _add(db, "docling", note=_NOTE)
    _add(db, "Router", note=_NOTE)
    _add(db, "10.1.2.3", note=_NOTE)
    _add(db, "gemma4", note=_NOTE)
    _add(db, "docs.example", note=_NOTE)
    _add(db, "parser.internal", note="管理員自加", user_id=owner.id)
    db.commit()

    _migration().drop_auto_imported_compose_hosts(db)
    db.commit()
    db.expire_all()

    hosts = {row.host for row in db.query(TrustedHost).all()}
    assert hosts == {"10.1.2.3", "gemma4", "docs.example", "parser.internal"}


def test_owner_added_compose_name_stays(db):
    owner = make_user(db, username="host-owner-2")
    _add(db, "docling", note=_NOTE, user_id=owner.id)
    _add(db, "router", note="留給解析主機")
    db.commit()

    _migration().drop_auto_imported_compose_hosts(db)
    db.commit()
    db.expire_all()

    hosts = {row.host for row in db.query(TrustedHost).all()}
    assert hosts == {"docling", "router"}
