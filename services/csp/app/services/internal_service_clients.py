"""CSP-owned credentials for internal services.

Humans do not copy these tokens. On startup and on a timer, CSP makes
sure each configured internal client has one active credential and
writes the current plaintext to
``<ANILA_SERVICE_CLIENT_DIR>/<client_name>.token``. 有指定消費者群組時，明文寫在
``<目錄>/<client_name>/token``，子目錄必須已存在（啟動腳本以 uid 10005
建好）。沒有群組時寫 ``<目錄>/<client_name>.token``（測試用）。
已吊銷的列不重新核發，
並刪掉憑證檔，該服務因此失敗即關閉。

內建名單是 ``router-primary``（服務憑證）、``anila-studio``（服務憑證）
與 ``ingestion-worker``（使用者 API key，不是 csk-）。
``ANILA_INTERNAL_SERVICE_CLIENTS`` 可再加客戶端。``router-primary`` 與
``anila-studio`` 一律會核發，類型被寫錯時改回內建值。JSON 若已明確列出
``ingestion-worker``，就照那一筆（舊的服務憑證測試仍可這樣寫）；沒列才
補上內建的 API key。

每個消費者一個子目錄（擁有者 uid 10005，mode 2770，群組才進得去）。
CSP、studio、worker 都是 uid 10001；檔案若放在同一個 0640 目錄，
它們會互讀。子目錄的擁有者不是 10001，同 uid 的其他行程進不去。
CSP 必須加入每一個消費者群組才能在目錄裡建檔。::

    [{"client_name": "ingestion-worker", "client_type": "worker", "credential": "api_key", "file_gid": 10004}]
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.service_client import ServiceClient
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.service_token_envelope import (
    compute_lookup_hash,
    decode_service_token_envelope,
    encode_service_token_envelope,
    generate_service_token,
)
from app.time_utils import as_utc

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
# Last startup/periodic pass. None means it has not failed. Client names
# only — never a token. /health reads this so a failed required publish
# cannot look the same as a healthy boot.
_provision_failed: tuple[str, ...] | None = None
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
_CLIENT_TYPES = frozenset({"router", "worker", "admin_tool", "studio"})
_CREDENTIALS = frozenset({"service_token", "api_key"})
_UNSET = object()

# 憑證檔維持 0640。隔離不靠拿掉擁有者位元：CSP 與 studio／worker
# 同是 uid 10001，擁有者讀不到就連寫入行程也讀不回。
# 每個客戶端一個子目錄，擁有者 uid 10005（沒有服務以這個 uid 跑），
# mode 2770，群組才進得去。見 infra/docker/csp-credential-dirs.sh。
TOKEN_FILE_MODE = 0o640
# 升級腳本清掉根目錄的扁平憑證後留下這個檔。只處理一次。
_FORCE_ROTATE_MARKER = ".force-rotate-router-primary"
_FORCE_ROTATE_DETAIL = "升級時清掉可能被複製的 router 權杖，不留寬限"
DIRECTORY_MODE = 0o2750
TOKEN_DIR_OWNER_UID = 10005
ROUTER_FILE_GID = 10002
STUDIO_FILE_GID = 10003
WORKER_FILE_GID = 10004
DEFAULT_ROTATE_AFTER = timedelta(days=30)
DEFAULT_GRACE = timedelta(hours=24)
_MIN_PROVISION_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class InternalServiceClientSpec:
    client_name: str
    client_type: str
    description: str = ""
    file_gid: int | None = None
    # service_token：csk-，寫進 service_clients。
    # api_key：sk-，雜湊後放 api_keys，明文只進憑證檔。
    credential: str = "service_token"


@dataclass(frozen=True)
class ProvisionOutcome:
    client_name: str
    action: str


DEFAULT_INTERNAL_SERVICE_CLIENTS: tuple[InternalServiceClientSpec, ...] = (
    InternalServiceClientSpec(
        client_name="router-primary",
        client_type="router",
        description="anila-core-router；憑證由 CSP 核發",
        file_gid=ROUTER_FILE_GID,
    ),
    InternalServiceClientSpec(
        client_name="anila-studio",
        client_type="studio",
        description="anila-studio；憑證由 CSP 核發",
        file_gid=STUDIO_FILE_GID,
    ),
    InternalServiceClientSpec(
        client_name="ingestion-worker",
        client_type="worker",
        description="ingestion-worker 系統帳號的 API key；明文只寫憑證檔",
        file_gid=WORKER_FILE_GID,
        credential="api_key",
    ),
)


def auto_provision_enabled() -> bool:
    """True unless the process explicitly turns provisioning off.

    Read at call time so tests can force it off after import. Unset
    means on: a deployment that does not mention the variable still
    provisions.
    """
    raw = os.environ.get("ANILA_SERVICE_CLIENT_AUTO_PROVISION")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def parse_internal_service_clients(raw: str | None) -> tuple[InternalServiceClientSpec, ...]:
    """Parse the JSON client list. Invalid input falls back to the built-in list.

    ``router-primary`` is merged in when the list omits it, and a
    non-router ``client_type`` for that name is replaced by the built-in
    router client. The raw string is never logged: an operator mistake
    could paste a secret into the variable.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_INTERNAL_SERVICE_CLIENTS
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error(
            "ANILA_INTERNAL_SERVICE_CLIENTS is not valid JSON; using the built-in list"
        )
        return DEFAULT_INTERNAL_SERVICE_CLIENTS
    if not isinstance(data, list) or not data:
        logger.error(
            "ANILA_INTERNAL_SERVICE_CLIENTS must be a non-empty JSON list; "
            "using the built-in list"
        )
        return DEFAULT_INTERNAL_SERVICE_CLIENTS
    specs: list[InternalServiceClientSpec] = []
    seen: set[str] = set()
    for item in data:
        spec = _spec_from_json(item)
        if spec is None:
            continue
        if spec.client_name in seen:
            logger.error(
                "duplicate internal service client %s ignored", spec.client_name
            )
            continue
        seen.add(spec.client_name)
        specs.append(spec)
    if not specs:
        logger.error(
            "ANILA_INTERNAL_SERVICE_CLIENTS produced no valid entries; "
            "using the built-in list"
        )
        return DEFAULT_INTERNAL_SERVICE_CLIENTS
    return _with_required_clients(tuple(specs))


def _builtin(name: str) -> InternalServiceClientSpec:
    for spec in DEFAULT_INTERNAL_SERVICE_CLIENTS:
        if spec.client_name == name:
            return spec
    raise KeyError(name)


def _with_required_clients(
    specs: tuple[InternalServiceClientSpec, ...],
) -> tuple[InternalServiceClientSpec, ...]:
    """router-primary 與 anila-studio 一律留在名單裡，類型不對就改回內建。"""
    kept: list[InternalServiceClientSpec] = []
    saw: set[str] = set()
    for spec in specs:
        if spec.client_name == "router-primary":
            if spec.client_type != "router" or spec.credential != "service_token":
                logger.error(
                    "internal service client router-primary must be a router "
                    "service token; the built-in router client is used instead"
                )
                continue
            if "router-primary" in saw:
                logger.error("duplicate internal service client router-primary ignored")
                continue
            saw.add("router-primary")
            kept.append(_with_default_gid(spec, ROUTER_FILE_GID))
            continue
        if spec.client_name == "anila-studio":
            if spec.client_type != "studio" or spec.credential != "service_token":
                logger.error(
                    "internal service client anila-studio must be a studio "
                    "service token; the built-in studio client is used instead"
                )
                continue
            if "anila-studio" in saw:
                logger.error("duplicate internal service client anila-studio ignored")
                continue
            saw.add("anila-studio")
            kept.append(_with_default_gid(spec, STUDIO_FILE_GID))
            continue
        if spec.client_name in saw:
            logger.error(
                "duplicate internal service client %s ignored", spec.client_name
            )
            continue
        saw.add(spec.client_name)
        kept.append(spec)
    if "router-primary" not in saw:
        kept.insert(0, _builtin("router-primary"))
    if "anila-studio" not in saw:
        kept.append(_builtin("anila-studio"))
    if "ingestion-worker" not in saw:
        kept.append(_builtin("ingestion-worker"))
    return tuple(kept)


def _with_default_gid(
    spec: InternalServiceClientSpec, gid: int
) -> InternalServiceClientSpec:
    if spec.file_gid is not None:
        return spec
    return replace(spec, file_gid=gid)


def ensure_internal_service_clients(
    db: Session,
    *,
    specs: Sequence[InternalServiceClientSpec] | None = None,
    directory: Path | str | None = None,
    now: datetime | None = None,
    rotate_after: timedelta | None = None,
    grace: timedelta | None = None,
    file_gid: int | None | object = _UNSET,
) -> list[ProvisionOutcome]:
    """Create, refresh, or rotate each configured client and publish its file.

    Commits per client. A revoked client is left revoked and its file
    is removed. Plaintext is never logged.
    """
    with _LOCK:
        chosen = tuple(specs) if specs is not None else _configured_specs()
        root = Path(directory) if directory is not None else _directory()
        moment = as_utc(now) or datetime.now(timezone.utc)
        interval = rotate_after if rotate_after is not None else _rotate_after()
        grace_window = grace if grace is not None else _grace()
        outcomes: list[ProvisionOutcome] = []
        for spec in chosen:
            outcomes.append(
                _ensure_one(
                    db,
                    spec,
                    root,
                    moment,
                    interval,
                    grace_window,
                    file_gid,
                )
            )
        return outcomes


def sync_configured_client_token(
    db: Session,
    client: ServiceClient,
    plaintext: str | None = None,
) -> None:
    """Publish the committed credential after an admin mutation.

    ``plaintext`` is ignored for the file bytes. The file is written from
    the row as committed, so a replica that lost a race cannot put its
    own token on disk. Clients outside the configured list have no file.
    A revoked client loses its file.
    """
    del plaintext
    with _LOCK:
        spec = _spec_named(client.client_name)
        if spec is None:
            return
        _publish_committed(
            db,
            client.client_name,
            _directory(),
            _gid_for(spec, _UNSET),
        )


def provisioning_health() -> dict:
    """Readiness of the last required-client pass.

    ``disabled`` — auto-provision is off. Readiness is not healthy:
    the router credential is not being published.
    ``ok`` — the last pass published every required client.
    ``degraded`` — a required client hit a write or database error.
    ``failed`` lists client names only.
    """
    if not auto_provision_enabled():
        return {"ok": False, "state": "disabled", "failed": []}
    if _provision_failed:
        return {"ok": False, "state": "degraded", "failed": list(_provision_failed)}
    return {"ok": True, "state": "ok", "failed": []}


def note_provision_outcomes(outcomes: Sequence[ProvisionOutcome]) -> None:
    """Record whether required clients published. Clears a previous failure."""
    global _provision_failed
    failed = tuple(item.client_name for item in outcomes if item.action == "error")
    if failed:
        _provision_failed = failed
        logger.error(
            "required internal service client provisioning failed for %s; "
            "health is degraded",
            ", ".join(failed),
        )
        return
    _provision_failed = None


def note_provision_failure() -> None:
    """A pass threw before it could name a client. Still not healthy."""
    global _provision_failed
    _provision_failed = ("*",)
    logger.error(
        "required internal service client provisioning failed; health is degraded"
    )


def provision_internal_service_clients_once() -> list[ProvisionOutcome]:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            outcomes = ensure_internal_service_clients(db)
        except Exception:
            logger.exception(
                "internal service client provisioning failed; health is degraded"
            )
            note_provision_failure()
            return []
    finally:
        db.close()
    note_provision_outcomes(outcomes)
    return outcomes


async def start_internal_service_client_provisioner() -> asyncio.Task:
    """Periodic ensure. The caller runs one pass before this task sleeps."""

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_provision_interval_seconds())
            try:
                provision_internal_service_clients_once()
            except Exception:
                logger.exception("internal service client provisioner failed")

    logger.info(
        "internal service client provisioner scheduled every %ss",
        _provision_interval_seconds(),
    )
    return asyncio.create_task(_loop())


def write_token_file(
    directory: Path,
    client_name: str,
    plaintext: str,
    *,
    mode: int | None = None,
    gid: int | None = None,
) -> None:
    """Atomically publish ``plaintext``. No-op when the bytes already match.

    暫存檔寫在同一目錄再改名，讀者不會看到半截憑證。有 ``gid`` 時檔案在
    ``<directory>/<client_name>/token``（子目錄必須已存在）。mode 是
    0640。chmod／chown 失敗，或寫完之後的 mode／gid 不符，就拋出。
    呼叫端不得把這次發布當成成功。

    有 gid 卻沒有專屬目錄時拒絕發布，不退回根目錄的扁平檔。同 uid 的
    其他服務讀得到那個扁平檔，這次發布必須失敗，readiness 才會降級。
    """
    if mode is None:
        mode = TOKEN_FILE_MODE
    payload = _payload(plaintext)
    directory.mkdir(parents=True, exist_ok=True)
    _harden_directory(directory)
    if gid is not None and not (directory / client_name).is_dir():
        logger.error(
            "private credential directory for %s is missing; refusing to "
            "publish. csp-credential-dirs must create it so uid 10001 "
            "services cannot read each other's tokens. health is degraded",
            client_name,
        )
        raise FileNotFoundError(
            f"private credential directory for {client_name} is missing"
        )
    dest = _token_path(directory, client_name, gid=gid)
    dest_parent = dest.parent
    if dest.is_file() and _file_matches(dest, payload):
        _apply_mode_and_group(dest, mode, gid)
        return
    tmp = dest_parent / f".{client_name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        mode,
    )
    try:
        _write_all(fd, payload)
        os.fchmod(fd, mode)
        _fchown_group(fd, client_name, gid)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _apply_mode_and_group(dest, mode, gid)
    _fsync_directory(dest_parent)


def remove_token_file(
    directory: Path, client_name: str, *, gid: int | None = None
) -> None:
    if not _NAME_RE.fullmatch(client_name):
        logger.error("refusing to remove a token file for an unsafe client name")
        return
    try:
        _token_path(directory, client_name, gid=gid).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.error(
            "could not remove token file for %s: %s",
            client_name,
            exc.__class__.__name__,
        )


def _ensure_one(
    db: Session,
    spec: InternalServiceClientSpec,
    directory: Path,
    now: datetime,
    rotate_after: timedelta,
    grace: timedelta,
    file_gid: int | None | object,
) -> ProvisionOutcome:
    name = spec.client_name
    if (
        not _NAME_RE.fullmatch(name)
        or spec.client_type not in _CLIENT_TYPES
        or spec.credential not in _CREDENTIALS
    ):
        logger.error("skipping internal service client with an invalid name or type")
        return ProvisionOutcome(name, "error")
    gid = _gid_for(spec, file_gid)
    if spec.credential == "api_key":
        return _ensure_api_key(db, spec, directory, now, rotate_after, grace, gid)
    force = _force_rotate_requested(directory, name)
    try:
        row = _lock_named(db, name)
        if row is not None and (not row.is_active or row.revoked_at is not None):
            logger.error(
                "internal service client %s is revoked; refusing to re-issue; "
                "token file removed so the service fails closed",
                name,
            )
            db.rollback()
            remove_token_file(directory, name, gid=gid)
            if force:
                # 舊權杖已經不能用。清掉標記，避免下一輪又去輪替。
                _consume_force_rotate_marker(directory)
            return ProvisionOutcome(name, "revoked")
        if row is not None and row.client_type != spec.client_type:
            logger.error(
                "internal service client %s has client_type %s; configuration says %s; "
                "not publishing a credential",
                name,
                row.client_type,
                spec.client_type,
            )
            if force:
                claimed = _claim_new_token(
                    db,
                    row,
                    now,
                    previous_grace=None,
                    detail=_FORCE_ROTATE_DETAIL,
                )
                if claimed == "error":
                    return ProvisionOutcome(name, "error")
                if claimed != "lost":
                    db.commit()
                _consume_force_rotate_marker(directory)
            else:
                db.rollback()
            return ProvisionOutcome(name, "error")

        if row is None:
            action = _insert_client(db, spec, now)
        elif force:
            # 扁平檔可能已被複製。這一次不留寬限，換完就清掉標記。
            action = _claim_new_token(
                db,
                row,
                now,
                previous_grace=None,
                detail=_FORCE_ROTATE_DETAIL,
            )
            if action == "ok":
                action = "force_rotated"
        elif _active_is_legacy_shared(row):
            # Migration 0027 stored the fleet CSP_SERVICE_TOKEN on this row.
            # Republishing it, or keeping it for the grace window, leaves the
            # shared secret able to call router-only endpoints.
            action = _claim_new_token(db, row, now, previous_grace=None)
            if action == "ok":
                action = "replaced_legacy"
        elif _rotation_due(row, now, rotate_after):
            action = _claim_new_token(db, row, now, previous_grace=grace)
            if action == "ok":
                action = "rotated"
        elif _previous_is_legacy_shared(row):
            action = _clear_legacy_previous(db, row)
        else:
            action = "unchanged"

        if action == "lost":
            logger.info(
                "internal service client %s left the credential to the replica "
                "that committed; publishing that credential",
                name,
            )
            action = "unchanged"
        if action == "error":
            return _finish(db, name, directory, gid, "error")
        db.commit()
        if force:
            _consume_force_rotate_marker(directory)
    except IntegrityError:
        db.rollback()
        logger.info(
            "internal service client %s already committed by another replica; "
            "publishing that credential",
            name,
        )
        try:
            published = _publish_committed(db, name, directory, gid)
        except Exception as exc:
            logger.error(
                "failed to publish token file for %s: %s",
                name,
                exc.__class__.__name__,
            )
            return ProvisionOutcome(name, "error")
        if published is None:
            # The other insert is committed but not visible yet, or the
            # file write is the other replica's job. Not a failed publish.
            return ProvisionOutcome(name, "unchanged")
        logger.info(
            "internal service client %s unchanged; credential file updated",
            name,
        )
        return ProvisionOutcome(name, "unchanged")
    except Exception:
        db.rollback()
        logger.exception(
            "internal service client %s could not be provisioned", name
        )
        return ProvisionOutcome(name, "error")
    return _finish(db, name, directory, gid, action)


def _ensure_api_key(
    db: Session,
    spec: InternalServiceClientSpec,
    directory: Path,
    now: datetime,
    rotate_after: timedelta,
    grace: timedelta,
    gid: int | None,
) -> ProvisionOutcome:
    """核發 ingestion-worker 這類系統帳號的 sk-。

    雜湊方式與現有 api_keys 相同。明文只寫進憑證檔，日誌與稽核不記明文。
    檔案不見時無法從雜湊還原，只能換一把新的。已停用、沒有現用金鑰時
    不重新核發，並刪掉憑證檔。
    """
    from app.models.api_key import ApiKey
    from app.models.user import User
    from app.utils.security import hash_password

    name = spec.client_name
    key_name = f"{name}-system-key"
    try:
        user = (
            db.query(User)
            .filter(User.username == name)
            # User 有自動 join 的關聯，查詢會帶 outer join；PostgreSQL 不准對
            # outer join 的可空側下 FOR UPDATE，所以只鎖 users 這張表。
            .with_for_update(of=User)
            .first()
        )
        if user is None:
            user = User(
                username=name,
                email=f"{name}@anila.local",
                hashed_password=hash_password(secrets.token_urlsafe(24)),
                role="system",
                is_active=True,
                is_approved=True,
            )
            db.add(user)
            db.flush()
            action = "created"
        elif user.role != "system" or not user.is_active:
            logger.error(
                "internal API key %s belongs to a user that is not an active "
                "system account; not publishing a credential",
                name,
            )
            db.rollback()
            return ProvisionOutcome(name, "error")
        else:
            action = "unchanged"

        keys = (
            db.query(ApiKey)
            .filter(ApiKey.user_id == user.id, ApiKey.name == key_name)
            .all()
        )
        current = _current_api_key(keys, now)
        inactive = [row for row in keys if not row.is_active]
        if current is None and inactive:
            for row in keys:
                if row.is_active:
                    row.is_active = False
            db.commit()
            remove_token_file(directory, name, gid=gid)
            logger.error(
                "internal API key %s is revoked; refusing to re-issue; "
                "token file removed so the service fails closed",
                name,
            )
            return ProvisionOutcome(name, "revoked")

        legacy_hash = _legacy_platform_key_hash()
        replace_legacy = (
            current is not None
            and legacy_hash is not None
            and _hash_is(current.key_hash, legacy_hash)
        )
        file_ok = (
            current is not None
            and _api_key_file_matches(directory, name, current.key_hash, gid=gid)
        )
        if (
            current is not None
            and file_ok
            and not replace_legacy
            and not _api_key_rotation_due(current, now, rotate_after)
        ):
            plaintext_now = _plaintext_placeholder_skip(directory, name, gid=gid)
            db.rollback()
            write_token_file(directory, name, plaintext_now, gid=gid)
            return ProvisionOutcome(name, "unchanged")

        if current is not None and file_ok and not replace_legacy:
            action = "rotated"
        elif replace_legacy:
            action = "replaced_legacy"
        elif current is None:
            action = "created"
        else:
            # 檔案不見或內容對不上現用金鑰：雜湊還原不了明文，換一把。
            action = "rotated" if action == "unchanged" else action

        plaintext, prefix, suffix, key_hash = _mint_api_key_parts()
        new_row = ApiKey(
            user_id=user.id,
            name=key_name,
            key_prefix=prefix,
            key_suffix=suffix,
            key_hash=key_hash,
            is_active=True,
        )
        db.add(new_row)
        db.flush()
        if current is not None:
            if replace_legacy:
                current.is_active = False
                current.expires_at = now
            else:
                current.expires_at = now + grace
        audit = log_audit_event(
            db,
            actor=None,
            action=agent_credential_service.AUDIT_TOKEN_ISSUED,
            resource_type="api_key",
            resource_id=new_row.id,
            detail=f"auto-provisioned api key '{key_name}' for {name}",
            metadata={
                "client_name": name,
                "credential": "api_key",
                "source": "internal_provisioner",
            },
        )
        if audit is None:
            logger.error(
                "internal API key %s was not provisioned; audit write failed",
                name,
            )
            return ProvisionOutcome(name, "error")
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(
            "internal API key %s already committed by another replica",
            name,
        )
        return ProvisionOutcome(name, "unchanged")
    except Exception:
        db.rollback()
        logger.exception("internal API key %s could not be provisioned", name)
        return ProvisionOutcome(name, "error")
    try:
        write_token_file(directory, name, plaintext, gid=gid)
    except Exception as exc:
        logger.error(
            "failed to publish token file for %s: %s",
            name,
            exc.__class__.__name__,
        )
        return ProvisionOutcome(name, "error")
    logger.info(
        "internal API key %s %s; credential file updated",
        name,
        action,
    )
    return ProvisionOutcome(name, action)


def _current_api_key(keys, now: datetime):
    current = None
    for row in keys:
        if not row.is_active:
            continue
        if row.expires_at is not None:
            exp = as_utc(row.expires_at)
            if exp is None or exp <= now:
                continue
            continue
        if current is None or row.id > current.id:
            current = row
    return current


def _api_key_rotation_due(row, now: datetime, rotate_after: timedelta) -> bool:
    anchor = as_utc(row.created_at)
    return anchor is None or now - anchor >= rotate_after


def _legacy_platform_key_hash() -> str | None:
    raw = os.environ.get("INTERNAL_PLATFORM_API_KEY", "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode()).hexdigest()


def _mint_api_key_parts() -> tuple[str, str, str, str]:
    from app.services.api_key_service import generate_api_key

    return generate_api_key()


def _api_key_file_matches(
    directory: Path, client_name: str, key_hash: str, *, gid: int | None = None
) -> bool:
    try:
        text = _token_path(directory, client_name, gid=gid).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not text or not key_hash:
        return False
    digest = hashlib.sha256(text.encode()).hexdigest()
    return _hash_is(digest, key_hash)


def _plaintext_placeholder_skip(
    directory: Path, client_name: str, *, gid: int | None = None
) -> str:
    """檔案已對上雜湊時，把同一份明文再交給 write_token_file 以確認 mode。"""
    return _token_path(directory, client_name, gid=gid).read_text(encoding="utf-8").strip()


def _lock_named(db: Session, name: str) -> ServiceClient | None:
    """Lock the client row so only one process rotates it.

    PostgreSQL takes ``SELECT ... FOR UPDATE``. SQLite has no row lock;
    SQLAlchemy drops the clause, and :func:`_claim_new_token` falls back
    to compare-and-swap on the lookup hash.
    """
    return (
        db.query(ServiceClient)
        .filter(ServiceClient.client_name == name)
        .with_for_update()
        .first()
    )


def _insert_client(
    db: Session,
    spec: InternalServiceClientSpec,
    now: datetime,
) -> str:
    plaintext = generate_service_token()
    row = ServiceClient(
        client_name=spec.client_name,
        client_type=spec.client_type,
        description=(spec.description or None),
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
        is_legacy=False,
        is_active=True,
        service_token_issued_at=now,
    )
    db.add(row)
    db.flush()
    audit = log_audit_event(
        db,
        actor=None,
        action=agent_credential_service.AUDIT_TOKEN_ISSUED,
        resource_type="service_client",
        resource_id=row.id,
        detail=(
            f"auto-provisioned service_client '{spec.client_name}' "
            f"({spec.client_type})"
        ),
        metadata={
            "client_name": spec.client_name,
            "client_type": spec.client_type,
            "source": "internal_provisioner",
        },
    )
    if audit is None:
        logger.error(
            "internal service client %s was not provisioned; audit write failed",
            spec.client_name,
        )
        return "error"
    return "created"


def _claim_new_token(
    db: Session,
    row: ServiceClient,
    now: datetime,
    *,
    previous_grace: timedelta | None,
    detail: str | None = None,
) -> str:
    """Install a new active token if the row still has the hash we read.

    ``previous_grace is None`` drops the old token immediately. That is
    required when the old token is the fleet-shared legacy secret, and
    for the one-time upgrade rotation of a token copied from a flat file.
    Returns ``ok``, ``lost`` (another replica committed), or ``error``.
    """
    expected = row.service_token_lookup_hash
    previous_envelope = row.service_token_envelope
    client_id = row.id
    client_name = row.client_name
    plaintext = generate_service_token()
    new_hash = compute_lookup_hash(plaintext)
    values: dict = {
        "service_token_envelope": encode_service_token_envelope(plaintext),
        "service_token_lookup_hash": new_hash,
        "service_token_rotated_at": now,
        "is_legacy": False,
    }
    if previous_grace is None:
        values["service_token_previous_envelope"] = None
        values["service_token_previous_lookup_hash"] = None
        values["service_token_previous_expires_at"] = None
        if detail is None:
            detail = "replaced legacy shared credential; previous token invalidated"
    else:
        values["service_token_previous_envelope"] = previous_envelope
        values["service_token_previous_lookup_hash"] = expected
        values["service_token_previous_expires_at"] = now + previous_grace
        if detail is None:
            detail = (
                "service_client rotated "
                f"(grace={int(previous_grace.total_seconds())}s)"
            )
    result = db.execute(
        update(ServiceClient)
        .where(
            ServiceClient.id == client_id,
            ServiceClient.service_token_lookup_hash == expected,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return "lost"
    db.expire(row)
    audit = log_audit_event(
        db,
        actor=None,
        action=agent_credential_service.AUDIT_TOKEN_ROTATED,
        resource_type="service_client",
        resource_id=client_id,
        detail=detail,
        metadata={"client_name": client_name},
    )
    if audit is None:
        return "error"
    return "ok"


def _clear_legacy_previous(db: Session, row: ServiceClient) -> str:
    """Drop a grace-window copy of the fleet secret without rotating."""
    expected = row.service_token_lookup_hash
    previous = row.service_token_previous_lookup_hash
    result = db.execute(
        update(ServiceClient)
        .where(
            ServiceClient.id == row.id,
            ServiceClient.service_token_lookup_hash == expected,
            ServiceClient.service_token_previous_lookup_hash == previous,
        )
        .values(
            service_token_previous_envelope=None,
            service_token_previous_lookup_hash=None,
            service_token_previous_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return "lost"
    client_name = row.client_name
    db.expire(row)
    logger.error(
        "internal service client %s still trusted the legacy shared token "
        "during grace; that previous token is no longer accepted",
        client_name,
    )
    return "legacy_previous_cleared"


def _finish(
    db: Session,
    name: str,
    directory: Path,
    gid: int | None,
    action: str,
) -> ProvisionOutcome:
    try:
        published = _publish_committed(db, name, directory, gid)
    except Exception as exc:
        logger.error(
            "failed to publish token file for %s: %s",
            name,
            exc.__class__.__name__,
        )
        return ProvisionOutcome(name, "error")
    if action == "error" or published is None:
        return ProvisionOutcome(name, "error")
    logger.info(
        "internal service client %s %s; credential file updated",
        name,
        action,
    )
    return ProvisionOutcome(name, action)


def _publish_committed(
    db: Session,
    name: str,
    directory: Path,
    gid: int | None,
) -> str | None:
    """Write the file from the committed row, under the row lock.

    The lock is held across the write so another replica cannot commit a
    different token and then lose the file race. The lock transaction is
    read-only and is closed before return.
    """
    db.expire_all()
    try:
        row = _lock_named(db, name)
        if row is None:
            # Do not unlink. A replica can be between its commit and its
            # publish; deleting here would remove the file it just wrote.
            return None
        if not row.is_active or row.revoked_at is not None:
            remove_token_file(directory, name, gid=gid)
            logger.error(
                "internal service client %s is revoked; credential file removed "
                "so the service fails closed",
                name,
            )
            return None
        plaintext = decode_service_token_envelope(row.service_token_envelope)
        if not plaintext:
            logger.error(
                "internal service client %s has no decodable token; file not written",
                name,
            )
            return None
        write_token_file(directory, name, plaintext, gid=gid)
        return plaintext
    finally:
        db.rollback()


def _rotation_due(row: ServiceClient, now: datetime, rotate_after: timedelta) -> bool:
    anchor = as_utc(row.service_token_rotated_at) or as_utc(row.service_token_issued_at)
    return anchor is None or now - anchor >= rotate_after


def _legacy_shared_hash() -> str | None:
    legacy = (settings.CSP_SERVICE_TOKEN or "").strip()
    if not legacy:
        return None
    return compute_lookup_hash(legacy)


def _hash_is(stored: str | None, expected: str | None) -> bool:
    if not stored or not expected or len(stored) != len(expected):
        return False
    return hmac.compare_digest(stored, expected)


def _active_is_legacy_shared(row: ServiceClient) -> bool:
    """True when this row is the migration-0027 fleet secret.

    ``is_legacy`` is the seed flag. A row whose active hash still equals
    the current ``CSP_SERVICE_TOKEN`` is the same credential even if the
    flag was cleared.
    """
    if row.is_legacy:
        return True
    return _hash_is(row.service_token_lookup_hash, _legacy_shared_hash())


def _previous_is_legacy_shared(row: ServiceClient) -> bool:
    return _hash_is(row.service_token_previous_lookup_hash, _legacy_shared_hash())


def _spec_from_json(item: object) -> InternalServiceClientSpec | None:
    if not isinstance(item, dict):
        logger.error("skipping internal service client entry that is not an object")
        return None
    name = str(item.get("client_name") or "").strip()
    client_type = str(item.get("client_type") or "").strip()
    if not _NAME_RE.fullmatch(name) or client_type not in _CLIENT_TYPES:
        logger.error("skipping internal service client with an invalid name or type")
        return None
    description = str(item.get("description") or "")[:500]
    credential = str(item.get("credential") or "service_token").strip()
    if credential not in _CREDENTIALS:
        logger.error("skipping internal service client with an invalid credential kind")
        return None
    return InternalServiceClientSpec(
        client_name=name,
        client_type=client_type,
        description=description,
        file_gid=_coerce_gid(item.get("file_gid")),
        credential=credential,
    )


def _configured_specs() -> tuple[InternalServiceClientSpec, ...]:
    return parse_internal_service_clients(settings.ANILA_INTERNAL_SERVICE_CLIENTS)


def credential_is_file_provisioned(client_name: str) -> bool:
    """True when CSP writes this client's credential to the shared directory."""
    return _spec_named(client_name) is not None


def _spec_named(client_name: str) -> InternalServiceClientSpec | None:
    for spec in _configured_specs():
        if spec.client_name == client_name:
            return spec
    return None


def _directory() -> Path:
    return Path(settings.ANILA_SERVICE_CLIENT_DIR)


def _rotate_after() -> timedelta:
    seconds = int(settings.ANILA_SERVICE_CLIENT_ROTATE_INTERVAL_SECONDS)
    if seconds <= 0:
        seconds = int(DEFAULT_ROTATE_AFTER.total_seconds())
    return timedelta(seconds=seconds)


def _grace() -> timedelta:
    seconds = int(settings.ANILA_SERVICE_CLIENT_GRACE_SECONDS)
    if seconds < 60:
        seconds = int(DEFAULT_GRACE.total_seconds())
    return timedelta(seconds=seconds)


def _provision_interval_seconds() -> int:
    try:
        seconds = int(settings.ANILA_SERVICE_CLIENT_PROVISION_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        seconds = 3600
    return max(_MIN_PROVISION_INTERVAL_SECONDS, seconds)


def _gid_for(
    spec: InternalServiceClientSpec,
    override: int | None | object,
) -> int | None:
    # 呼叫端明示的值（含 None）蓋過 spec，測試才能關掉 chown。
    # router 的群組就是 ANILA_SERVICE_CLIENT_FILE_GID（compose 的 10002）。
    # 其他客戶端用自己的 file_gid，避免共用一個群組。
    if override is not _UNSET:
        return _coerce_gid(override)
    if spec.client_name == "router-primary":
        configured = _coerce_gid(settings.ANILA_SERVICE_CLIENT_FILE_GID)
        if configured is not None:
            return configured
    if spec.file_gid is not None:
        return spec.file_gid
    return _coerce_gid(settings.ANILA_SERVICE_CLIENT_FILE_GID)


def _coerce_gid(value: object) -> int | None:
    if value is None or value is _UNSET or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        gid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.error("ignoring internal service client file_gid that is not an integer")
        return None
    if gid < 0 or gid > 2**32 - 1:
        logger.error("ignoring internal service client file_gid outside 0..2^32-1")
        return None
    return gid


def _payload(plaintext: str) -> bytes:
    text = (plaintext or "").strip()
    if not text:
        raise ValueError("refusing to write an empty service token file")
    return (text + "\n").encode("utf-8")


def _force_rotate_marker(directory: Path) -> Path:
    return directory / _FORCE_ROTATE_MARKER


def _force_rotate_requested(directory: Path, name: str) -> bool:
    """升級腳本刪過扁平憑證檔時才為真。只針對 router-primary，且只做一次。"""
    if name != "router-primary":
        return False
    marker = _force_rotate_marker(directory)
    try:
        if marker.is_symlink():
            return False
        return marker.is_file()
    except OSError:
        logger.error("could not stat router force-rotate marker")
        return False


def _consume_force_rotate_marker(directory: Path) -> None:
    marker = _force_rotate_marker(directory)
    try:
        if marker.is_symlink() or not marker.is_file():
            return
        marker.unlink()
    except OSError as exc:
        logger.error(
            "could not remove router force-rotate marker: %s",
            exc.__class__.__name__,
        )


def _token_path(
    directory: Path, client_name: str, *, gid: int | None = None
) -> Path:
    if not _NAME_RE.fullmatch(client_name):
        raise ValueError("unsafe service client name")
    private = directory / client_name
    if gid is not None:
        # 沒有專屬目錄就不要退回根目錄的 <client>.token。
        if not private.is_dir():
            raise FileNotFoundError(
                f"private credential directory for {client_name} is missing"
            )
        return private / "token"
    return directory / f"{client_name}.token"


def _file_matches(path: Path, payload: bytes) -> bool:
    try:
        current = path.read_bytes()
    except OSError as exc:
        logger.error(
            "could not read existing token file %s: %s",
            path.name,
            exc.__class__.__name__,
        )
        return False
    if len(current) != len(payload):
        return False
    return hmac.compare_digest(current, payload)


def _harden_directory(directory: Path) -> None:
    """共用目錄維持 router 那個群組，不要改成最後一個客戶端的 gid。

    各憑證檔自己 chgrp。目錄若被改成 studio 的群組，uid 1000 的 router
    就進不了目錄。
    """
    try:
        os.chmod(directory, DIRECTORY_MODE)
    except OSError as exc:
        logger.warning(
            "service client directory mode not set on %s: %s",
            directory,
            exc.__class__.__name__,
        )
    gid = _coerce_gid(settings.ANILA_SERVICE_CLIENT_FILE_GID)
    if gid is None:
        return
    try:
        os.chown(directory, -1, gid)
    except OSError as exc:
        logger.warning(
            "service client directory group not set on %s: %s",
            directory,
            exc.__class__.__name__,
        )


def _apply_mode_and_group(path: Path, mode: int, gid: int | None) -> None:
    """chmod and chown, then stat. Mismatch or OSError fails publication."""
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.error(
            "token file mode not set for %s: %s",
            path.name,
            exc.__class__.__name__,
        )
        raise
    if gid is not None:
        try:
            os.chown(path, -1, gid)
        except OSError as exc:
            logger.error(
                "token file for %s not chowned to gid %s: %s",
                path.name,
                gid,
                exc.__class__.__name__,
            )
            raise
    _verify_published_mode_and_group(path, mode, gid)


def _verify_published_mode_and_group(path: Path, mode: int, gid: int | None) -> None:
    try:
        st = path.stat()
    except OSError as exc:
        logger.error(
            "token file for %s could not be stat'ed after publish: %s",
            path.name,
            exc.__class__.__name__,
        )
        raise
    actual_mode = stat.S_IMODE(st.st_mode)
    gid_ok = gid is None or st.st_gid == gid
    if actual_mode == mode and gid_ok:
        return
    logger.error(
        "token file for %s has mode %s gid %s; required mode %s gid %s",
        path.name,
        oct(actual_mode),
        st.st_gid,
        oct(mode),
        "-" if gid is None else gid,
    )
    try:
        path.unlink()
    except OSError as exc:
        logger.error(
            "could not remove token file for %s after a failed mode or group check: %s",
            path.name,
            exc.__class__.__name__,
        )
    raise OSError("token file mode or group does not match the required publication")


def _fchown_group(fd: int, client_name: str, gid: int | None) -> None:
    if gid is None:
        return
    try:
        os.fchown(fd, -1, gid)
    except OSError as exc:
        logger.error(
            "token file for %s not chowned to gid %s: %s",
            client_name,
            gid,
            exc.__class__.__name__,
        )
        raise


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing a service token file")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)
