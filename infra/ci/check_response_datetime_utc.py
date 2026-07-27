#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Response datetime UTC-offset gate.

Why this exists
---------------
Naive ORM datetimes serialise to offsetless ISO-8601. Browsers parse those as
local time → Asia/Taipei UI shows UTC wall clocks eight hours off. The fix is
``ApiResponseModel`` (``services/csp/app/schemas/base.py``). This checker makes
the fix durable:

1. Walk every FastAPI route ``response_model``; any datetime-typed field
   (nested / list) that can JSON-serialise without an explicit offset → fail.
2. Assert three-input round-trip on ``ApiResponseModel`` (naive UTC, aware
   UTC, aware non-UTC) — all emit an offset denoting the same instant.
3. Flag routes that return datetimes with ``response_model=None`` or an
   untyped ``dict`` — the base class cannot reach those. Genuine exemptions
   go in ``response_datetime_utc_allowlist.json`` with a reason each.

Exit codes (same contract as ``check_orm_pg_drift.py``)
-------------------------------------------------------
    0  clean
    3  findings
    2  usage / allowlist malformed
    1  checker itself broken

Never wrap this in ``|| true``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args, get_origin

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2
EXIT_FINDINGS = 3

REPO_ROOT = Path(__file__).resolve().parents[2]
CSP_ROOT = REPO_ROOT / "services" / "csp"
ALLOWLIST_PATH = Path(__file__).resolve().parent / "response_datetime_utc_allowlist.json"

_PINNED_ENV: dict[str, str] = {
    "APP_NAME": "CSP Platform",
    "APP_VERSION": "1.0.0",
    "DATABASE_URL": "sqlite:///./response-datetime-gate.db",
    "SECRET_KEY": "response-datetime-gate-not-a-real-secret",
    "ANILA_ALLOW_DEV_SECRET": "1",
    "ANILA_DEPLOYMENT_PROFILE": "response-datetime-gate",
    "SKIP_STARTUP_MIGRATIONS": "true",
    "DEBUG": "false",
    "HEALTH_CHECK_INTERVAL": "3600",
    "AUTO_REGISTER_MODELS": "",
    "AUTO_REGISTER_AGENTS": "",
    "AUTO_SEED_API_KEYS": "",
    "AUTO_REGISTER_LINKS": "",
    "ALLOW_AUTO_KEYGEN": "true",
}


def _load_allowlist() -> dict[str, str]:
    if not ALLOWLIST_PATH.is_file():
        raise FileNotFoundError(f"allowlist missing: {ALLOWLIST_PATH}")
    raw = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = raw.get("exempt_routes")
    if entries is None:
        raise ValueError("allowlist missing 'exempt_routes'")
    out: dict[str, str] = {}
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, str):
                raise ValueError(
                    f"allowlist entry {item!r} must be "
                    "{route, reason}, not a bare string"
                )
            route = item["route"]
            reason = item["reason"]
            if not isinstance(route, str) or not isinstance(reason, str):
                raise ValueError(f"bad allowlist entry: {item!r}")
            if not reason.strip():
                raise ValueError(f"empty reason for {route}")
            out[route] = reason
    elif isinstance(entries, dict):
        for route, reason in entries.items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"empty reason for {route}")
            out[str(route)] = reason
    else:
        raise ValueError("exempt_routes must be list or dict")
    return out


def _load_app() -> Any:
    for key, value in _PINNED_ENV.items():
        os.environ[key] = value
    sandbox = Path(tempfile.mkdtemp(prefix="anila-response-dt-gate-"))
    jwt_dir = sandbox / "jwt"
    jwt_dir.mkdir()
    os.environ["JWT_PRIVATE_KEY_PATH"] = str(jwt_dir / "jwt-private.pem")
    os.environ["JWT_PUBLIC_KEY_PATH"] = str(jwt_dir / "jwt-public.pem")
    if str(CSP_ROOT) not in sys.path:
        sys.path.insert(0, str(CSP_ROOT))
    os.chdir(sandbox)
    from app.main import app  # noqa: E402

    return app


def _is_datetime_type(annotation: Any) -> bool:
    if annotation is datetime:
        return True
    origin = get_origin(annotation)
    if origin is None:
        # Pydantic AwareDatetime / NaiveDatetime are annotated aliases.
        return getattr(annotation, "__name__", "") in {
            "datetime",
            "AwareDatetime",
            "NaiveDatetime",
        }
    args = get_args(annotation)
    if origin is list or origin is tuple or origin is set:
        return any(_is_datetime_type(a) for a in args)
    # Optional / Union
    return any(_is_datetime_type(a) for a in args if a is not type(None))


def _is_pydantic_model(cls: Any) -> bool:
    try:
        from pydantic import BaseModel
    except ImportError:
        return False
    return isinstance(cls, type) and issubclass(cls, BaseModel)


def _unwrap_response_model(model: Any) -> list[Any]:
    """Expand list[Model] / Union / Annotated into concrete model classes."""
    if model is None:
        return []
    if _is_pydantic_model(model):
        return [model]
    origin = get_origin(model)
    if origin is None:
        return []
    out: list[Any] = []
    for arg in get_args(model):
        out.extend(_unwrap_response_model(arg))
    return out


def _datetime_field_paths(model: type, prefix: str = "") -> list[str]:
    """Return dotted paths of datetime-typed fields, including nested models."""
    from pydantic.fields import FieldInfo

    paths: list[str] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        ann = field.annotation
        if _is_datetime_type(ann):
            paths.append(path)
            continue
        for nested in _unwrap_response_model(ann):
            paths.extend(_datetime_field_paths(nested, path))
        # list[NestedModel]
        origin = get_origin(ann)
        if origin in (list, tuple):
            for arg in get_args(ann):
                for nested in _unwrap_response_model(arg):
                    paths.extend(_datetime_field_paths(nested, f"{path}[]"))
    return paths


def _iso_has_explicit_offset(value: str) -> bool:
    """True if ISO-8601 string carries Z or ±HH:MM (not a bare local wall clock)."""
    if not isinstance(value, str):
        return False
    if value.endswith("Z") or value.endswith("z"):
        return True
    # ...+00:00 / ...-08:00 / ...+0000
    if len(value) >= 6 and (value[-6] in "+-" or value[-5] in "+-"):
        # require a digit after the sign somewhere in the tz tail
        tail = value[-6:]
        return any(ch.isdigit() for ch in tail)
    if len(value) >= 5 and value[-5] in "+-" and value[-4:].isdigit():
        return True
    return False


def _probe_instant() -> datetime:
    return datetime(2026, 7, 27, 11, 19, 42, 679819)


def _build_probe_payload(model: type, *, dt: datetime) -> dict[str, Any] | None:
    """Build a minimal kwargs dict filling required fields; datetimes get ``dt``.

    Returns None if the model is too constrained to probe (caller should
    still attempt a targeted field-level check via model_construct).
    """
    payload: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        ann = field.annotation
        if _is_datetime_type(ann):
            # Optional datetime → still set so we exercise the serializer.
            payload[name] = dt
            continue
        nested_models = _unwrap_response_model(ann)
        origin = get_origin(ann)
        if nested_models and origin not in (list, tuple):
            nested_payload = _build_probe_payload(nested_models[0], dt=dt)
            if nested_payload is None:
                return None
            payload[name] = nested_payload
            continue
        if origin in (list, tuple):
            args = get_args(ann)
            if args and _is_datetime_type(args[0]):
                payload[name] = [dt]
                continue
            nested = _unwrap_response_model(args[0]) if args else []
            if nested:
                nested_payload = _build_probe_payload(nested[0], dt=dt)
                if nested_payload is None:
                    return None
                payload[name] = [nested_payload]
                continue
            if field.is_required():
                payload[name] = []
            continue
        if not field.is_required():
            continue
        # Required non-datetime: supply a cheap placeholder.
        placeholder = _placeholder_for(ann)
        if placeholder is _MISSING:
            return None
        payload[name] = placeholder
    return payload


_MISSING = object()


def _placeholder_for(ann: Any) -> Any:
    origin = get_origin(ann)
    args = [a for a in get_args(ann) if a is not type(None)]
    if ann is str or ann is Any:
        return "x"
    if ann is int:
        return 1
    if ann is float:
        return 1.0
    if ann is bool:
        return False
    if ann is dict or origin is dict:
        return {}
    if ann is list or origin is list:
        return []
    if origin is not None and args:
        return _placeholder_for(args[0])
    # Literal
    if origin is not None and str(origin).endswith("Literal"):
        return args[0] if args else _MISSING
    try:
        from typing import Literal as _Lit  # noqa: F401

        if str(get_origin(ann)) == "typing.Literal" or getattr(
            ann, "__origin__", None
        ) is not None:
            lit_args = get_args(ann)
            if lit_args:
                return lit_args[0]
    except Exception:
        pass
    # Enum
    if isinstance(ann, type) and hasattr(ann, "__members__"):
        members = list(ann.__members__.values())  # type: ignore[attr-defined]
        if members:
            return members[0]
    return _MISSING


def _collect_iso_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        # Heuristic: look like ISO date-time
        if len(obj) >= 19 and obj[4] == "-" and "T" in obj:
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_collect_iso_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_collect_iso_strings(v))
    return found


def _check_model_serialisation(model: type) -> list[str]:
    """Return finding messages if ``model`` can emit offsetless datetimes."""
    findings: list[str] = []
    paths = _datetime_field_paths(model)
    if not paths:
        return findings

    naive = _probe_instant()  # naive = UTC wall clock per project contract
    payload = _build_probe_payload(model, dt=naive)
    instance = None
    try:
        if payload is not None:
            try:
                instance = model.model_validate(payload)
            except Exception:
                # Validators (e.g. provider_type allow-list) may reject
                # placeholders — construct bypasses them for ser-only probe.
                instance = model.model_construct(**payload)
        else:
            kwargs = {p.split(".")[0].rstrip("[]"): naive for p in paths}
            instance = model.model_construct(**kwargs)
        dumped = instance.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 — surface as finding, not crash
        findings.append(
            f"{model.__module__}.{model.__qualname__}: "
            f"could not probe serialisation ({exc})"
        )
        return findings

    for iso in _collect_iso_strings(dumped):
        if not _iso_has_explicit_offset(iso):
            findings.append(
                f"{model.__module__}.{model.__qualname__}: "
                f"serialised {iso!r} without UTC offset "
                f"(datetime fields: {', '.join(paths)})"
            )
            break
    return findings


def _route_key(methods: set[str], path: str) -> str:
    method = sorted(m for m in methods if m != "HEAD")[0]
    return f"{method} {path}"


def _annotation_mentions_datetime(annotation: Any) -> bool:
    if annotation is None or annotation is type(None):
        return False
    if annotation is datetime:
        return True
    if _is_pydantic_model(annotation):
        return bool(_datetime_field_paths(annotation))
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(_annotation_mentions_datetime(a) for a in get_args(annotation))


def _is_untyped_response_model(response_model: Any) -> bool:
    if response_model is None:
        return True
    if response_model is dict:
        return True
    origin = get_origin(response_model)
    if origin is dict:
        return True
    # dict[str, Any] etc.
    return False


def assert_round_trip() -> list[str]:
    """Three-input round-trip: naive UTC, aware UTC, aware non-UTC."""
    findings: list[str] = []
    # Import after sys.path is ready.
    from app.schemas.base import ApiResponseModel

    class _Probe(ApiResponseModel):
        created_at: datetime

    naive = _probe_instant()
    aware_utc = naive.replace(tzinfo=timezone.utc)
    aware_other = aware_utc.astimezone(timezone(timedelta(hours=8)))
    expected = aware_utc

    for label, value in (
        ("naive_utc", naive),
        ("aware_utc", aware_utc),
        ("aware_non_utc", aware_other),
    ):
        dumped = _Probe(created_at=value).model_dump(mode="json")["created_at"]
        if not _iso_has_explicit_offset(dumped):
            findings.append(f"round-trip {label}: no offset in {dumped!r}")
            continue
        parsed = datetime.fromisoformat(dumped.replace("Z", "+00:00"))
        if parsed.astimezone(timezone.utc) != expected:
            findings.append(
                f"round-trip {label}: {dumped!r} is not the same instant "
                f"as {expected.isoformat()}"
            )
    return findings


def run() -> int:
    try:
        allowlist = _load_allowlist()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"allowlist error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        app = _load_app()
    except Exception as exc:  # noqa: BLE001
        print(f"checker broken (import app): {exc}", file=sys.stderr)
        return EXIT_BROKEN

    findings: list[str] = []
    findings.extend(assert_round_trip())

    seen_models: set[type] = set()
    untyped_hits: list[str] = []
    unused_allowlist = set(allowlist)

    for route in app.routes:
        response_model = getattr(route, "response_model", None)
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        # Skip framework internals.
        if path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}:
            continue
        key = _route_key(set(methods), path)

        if _is_untyped_response_model(response_model):
            endpoint = getattr(route, "endpoint", None)
            ret = None
            if endpoint is not None:
                hints = getattr(endpoint, "__annotations__", {}) or {}
                ret = hints.get("return")
            # Flag only when the callable's return annotation involves datetime
            # (or a pydantic model that does). Streaming/File/None returns are
            # fine. Bare dict[str, Any] without datetime in the annotation is
            # outside this gate — response_model ratchet covers typing debt.
            mentions_dt = _annotation_mentions_datetime(ret) if ret else False
            if mentions_dt:
                if key in allowlist:
                    unused_allowlist.discard(key)
                else:
                    untyped_hits.append(
                        f"{key}: response_model is untyped/dict but return "
                        f"annotation involves datetime ({ret!r})"
                    )
            elif key in allowlist:
                unused_allowlist.discard(key)
            continue

        for model in _unwrap_response_model(response_model):
            if model in seen_models:
                continue
            seen_models.add(model)
            for msg in _check_model_serialisation(model):
                findings.append(f"{key} → {msg}")

    findings.extend(untyped_hits)
    for key in sorted(unused_allowlist):
        findings.append(
            f"allowlist entry unused (remove or fix route key): {key}"
        )

    if findings:
        print(f"response datetime UTC gate: {len(findings)} finding(s)")
        for msg in findings:
            print(f"  - {msg}")
        return EXIT_FINDINGS

    print(
        f"response datetime UTC gate: OK "
        f"({len(seen_models)} response models probed, "
        f"{len(allowlist)} allowlist entries)"
    )
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        return run()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"checker broken: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
