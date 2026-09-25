"""CSP-owned credentials for internal services.

Humans do not copy these tokens. On startup and on a timer, CSP makes
sure each configured internal client has one active credential and
writes the current plaintext to
``<ANILA_SERVICE_CLIENT_DIR>/<client_name>.token``. The file is mode
0640 and, when the process may change groups, owned by the consumer
gid (default 10002, the ``anila-svc-tokens`` group shared with the
router uid). A revoked row is not re-issued: the file is removed so
the consumer fails closed.

The client list is data. The built-in default is ``router-primary``
(``client_type=router``). ``ANILA_INTERNAL_SERVICE_CLIENTS`` adds to
that list with a JSON array so ``ingestion-worker`` can be added
without a code change. ``router-primary`` is always provisioned as a
router client, including when the JSON omits it or names it with
another ``client_type``::

    [{"client_name": "ingestion-worker", "client_type": "worker", "file_gid": 10001}]
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
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
_CLIENT_TYPES = frozenset({"router", "worker", "admin_tool"})
_UNSET = object()

TOKEN_FILE_MODE = 0o640
DIRECTORY_MODE = 0o2750
DEFAULT_ROTATE_AFTER = timedelta(days=30)
DEFAULT_GRACE = timedelta(hours=24)
_MIN_PROVISION_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class InternalServiceClientSpec:
    client_name: str
    client_type: str
    description: str = ""
    file_gid: int | None = None


@dataclass(frozen=True)
class ProvisionOutcome:
    client_name: str
    action: str


DEFAULT_INTERNAL_SERVICE_CLIENTS: tuple[InternalServiceClientSpec, ...] = (
    InternalServiceClientSpec(
        client_name="router-primary",
        client_type="router",
        description="anila-core-router; credential provisioned by CSP",
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
    return _with_required_router(tuple(specs))


def _with_required_router(
    specs: tuple[InternalServiceClientSpec, ...],
) -> tuple[InternalServiceClientSpec, ...]:
    """Keep ``router-primary`` as a router client in every configured list."""
    kept: list[InternalServiceClientSpec] = []
    saw_router = False
    for spec in specs:
        if spec.client_name != "router-primary":
            kept.append(spec)
            continue
        if spec.client_type != "router":
            logger.error(
                "internal service client router-primary must be client_type "
                "router; the built-in router client is used instead"
            )
            continue
        if saw_router:
            logger.error("duplicate internal service client router-primary ignored")
            continue
        saw_router = True
        kept.append(spec)
    if saw_router:
        return tuple(kept)
    return (DEFAULT_INTERNAL_SERVICE_CLIENTS[0], *kept)


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
    mode: int = TOKEN_FILE_MODE,
    gid: int | None = None,
) -> None:
    """Atomically publish ``plaintext``. No-op when the bytes already match.

    The temporary file is created in ``directory`` and renamed over the
    destination so readers never see a partial token. Mode is forced to
    0640 and, when ``gid`` is set, the group is changed. A failed
    chmod or chown, or a resulting mode or gid that does not match,
    raises. The caller must not report the publication as successful.
    """
    payload = _payload(plaintext)
    directory.mkdir(parents=True, exist_ok=True)
    _harden_directory(directory, gid)
    dest = _token_path(directory, client_name)
    if dest.is_file() and _file_matches(dest, payload):
        _apply_mode_and_group(dest, mode, gid)
        return
    tmp = directory / f".{client_name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
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
    _fsync_directory(directory)


def remove_token_file(directory: Path, client_name: str) -> None:
    if not _NAME_RE.fullmatch(client_name):
        logger.error("refusing to remove a token file for an unsafe client name")
        return
    try:
        _token_path(directory, client_name).unlink()
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
    if not _NAME_RE.fullmatch(name) or spec.client_type not in _CLIENT_TYPES:
        logger.error("skipping internal service client with an invalid name or type")
        return ProvisionOutcome(name, "error")
    gid = _gid_for(spec, file_gid)
    try:
        row = _lock_named(db, name)
        if row is not None and (not row.is_active or row.revoked_at is not None):
            logger.error(
                "internal service client %s is revoked; refusing to re-issue; "
                "token file removed so the service fails closed",
                name,
            )
            db.rollback()
            remove_token_file(directory, name)
            return ProvisionOutcome(name, "revoked")
        if row is not None and row.client_type != spec.client_type:
            logger.error(
                "internal service client %s has client_type %s; configuration says %s; "
                "not publishing a credential",
                name,
                row.client_type,
                spec.client_type,
            )
            db.rollback()
            return ProvisionOutcome(name, "error")

        if row is None:
            action = _insert_client(db, spec, now)
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
) -> str:
    """Install a new active token if the row still has the hash we read.

    ``previous_grace is None`` drops the old token immediately. That is
    required when the old token is the fleet-shared legacy secret.
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
        detail = "replaced legacy shared credential; previous token invalidated"
    else:
        values["service_token_previous_envelope"] = previous_envelope
        values["service_token_previous_lookup_hash"] = expected
        values["service_token_previous_expires_at"] = now + previous_grace
        detail = f"service_client rotated (grace={int(previous_grace.total_seconds())}s)"
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
            remove_token_file(directory, name)
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
    return InternalServiceClientSpec(
        client_name=name,
        client_type=client_type,
        description=description,
        file_gid=_coerce_gid(item.get("file_gid")),
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
    if spec.file_gid is not None:
        return spec.file_gid
    if override is not _UNSET:
        return _coerce_gid(override)
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


def _token_path(directory: Path, client_name: str) -> Path:
    if not _NAME_RE.fullmatch(client_name):
        raise ValueError("unsafe service client name")
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


def _harden_directory(directory: Path, gid: int | None) -> None:
    try:
        os.chmod(directory, DIRECTORY_MODE)
    except OSError as exc:
        logger.warning(
            "service client directory mode not set on %s: %s",
            directory,
            exc.__class__.__name__,
        )
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
