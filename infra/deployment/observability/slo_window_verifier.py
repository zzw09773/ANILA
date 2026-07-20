#!/usr/bin/env python3
"""Strict Gate 6 P3 SLO-window evidence verifier.

This module evaluates already-exported raw samples with a conservative window
aggregate: upper-bound SLOs use the maximum sample and lower-bound success SLOs
use the minimum sample.  It neither interpolates gaps nor produces metrics,
incidents, signatures, profiles, or production approval.  Even a perfect
seven-day export returns explicit non-acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SECURITY_SRC = _REPO_ROOT / "packages" / "anila-security" / "src"
if _SECURITY_SRC.is_dir() and str(_SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(_SECURITY_SRC))

from anila_security.production_acceptance_profile import (  # noqa: E402
    ProductionAcceptanceProfileError,
    VerifiedProductionAcceptanceProfile,
    canonical_json,
    production_profile_content_sha256,
    verify_signed_production_acceptance_profile,
)


P3_METRICS_EXPORT_SCHEMA = "anila.gate6.p3.slo-metrics-export.v1"
P3_INCIDENT_LIST_SCHEMA = "anila.gate6.p3.incident-list.v1"
P3_VERIFICATION_SCHEMA = "anila.gate6.p3.slo-window-verification.v1"
P3_VERIFICATION_STATUS = "VERIFIED_NON_ACCEPTANCE"
P3_ACCEPTANCE_STATUS = "NOT_ACCEPTANCE"
P3_ERROR_INVALID_EVIDENCE = "P3_INVALID_EVIDENCE"
P3_ERROR_INVALID_JSON = "P3_INVALID_JSON"
P3_ERROR_INVALID_PROFILE = "P3_INVALID_PROFILE"
P3_ERROR_RESOURCE_LIMIT = "P3_RESOURCE_LIMIT"

P3_MIN_WINDOW_SECONDS = 7 * 24 * 60 * 60
P3_MAX_FILE_BYTES = 16 * 1024 * 1024
P3_MAX_JSON_DEPTH = 32
P3_MAX_TOTAL_NODES = 2_000_000
P3_MAX_TOTAL_STRING_BYTES = P3_MAX_FILE_BYTES
P3_MAX_STRING_LENGTH = 4_096
P3_MAX_CONTAINER_ITEMS = 100_000
P3_MAX_CONTAINERS = 100_000
P3_MAX_SAMPLES = 100_000
P3_MAX_INCIDENTS = 10_000
P3_MAX_CADENCE_SECONDS = 86_400

# The verifier can validate the shape and timing of rows supplied in an
# incident export, but it cannot prove that the export is complete without
# independent operational-system evidence.  Keep this list fixed and
# attacker-independent so a clean result never implies that completeness was
# established locally.
P3_MISSING_EXTERNAL_EVIDENCE = (
    "time_locked_metrics_source_provenance",
    "incident_system_complete_export_watermark_provenance",
    "independent_operational_approval",
)
P3_MISSING_EXTERNAL_P3_EVIDENCE = P3_MISSING_EXTERNAL_EVIDENCE

P3_SLO_FIELDS = (
    "ingestion_p99_ms",
    "dispatch_success_rate",
    "queue_age_seconds",
    "stuck_job_count",
    "artifact_download_success_rate",
    "auth_error_rate",
)
_SLO_FIELD_SET = frozenset(P3_SLO_FIELDS)
_UPPER_BOUND_SLOS = frozenset(
    {
        "ingestion_p99_ms",
        "queue_age_seconds",
        "stuck_job_count",
        "auth_error_rate",
    }
)
_LOWER_BOUND_SLOS = frozenset(
    {"dispatch_success_rate", "artifact_download_success_rate"}
)

_METRICS_FIELDS = frozenset(
    {
        "schema_version",
        "export_id",
        "profile_binding",
        "load_profile",
        "workflow_ids",
        "observation_window",
        "cadence",
        "samples",
    }
)
_INCIDENT_LIST_FIELDS = frozenset(
    {
        "schema_version",
        "incident_list_id",
        "profile_binding",
        "observation_window",
        "incidents",
    }
)
_PROFILE_BINDING_FIELDS = frozenset(
    {"profile_id", "profile_version", "profile_content_sha256"}
)
_OBSERVATION_FIELDS = frozenset({"start", "end", "minimum_duration_seconds", "cadence"})
_CADENCE_FIELDS = frozenset({"interval_seconds", "tolerance_seconds"})
_SAMPLE_FIELDS = frozenset({"timestamp", "values"})
_INCIDENT_FIELDS = frozenset(
    {
        "incident_id",
        "severity",
        "taxonomy_definition",
        "started_at",
        "acknowledged_at",
        "mitigated_at",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
_METRIC_MAXIMUMS = MappingProxyType(
    {
        "ingestion_p99_ms": 86_400_000.0,
        "dispatch_success_rate": 1.0,
        "queue_age_seconds": 365 * 86_400.0,
        "stuck_job_count": 100_000_000.0,
        "artifact_download_success_rate": 1.0,
        "auth_error_rate": 1.0,
    }
)
_MAX_JSON_NUMBER_TOKEN_LENGTH = 128


class SloWindowEvidenceError(ValueError):
    """Raised when P3 exports are malformed, unbound, gapped, or outside SLO."""


class _InvalidJsonError(SloWindowEvidenceError):
    pass


class _InvalidProfileError(SloWindowEvidenceError):
    pass


class _ResourceLimitError(SloWindowEvidenceError):
    pass


def _require_path(value: Any, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise SloWindowEvidenceError(f"{field} must be a filesystem path")
    return Path(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJsonError
        result[key] = value
    return result


def _parse_json_integer(value: str) -> int:
    if len(value) > _MAX_JSON_NUMBER_TOKEN_LENGTH:
        raise _ResourceLimitError
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _InvalidJsonError from exc


def _parse_json_float(value: str) -> float:
    if len(value) > _MAX_JSON_NUMBER_TOKEN_LENGTH:
        raise _ResourceLimitError
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _InvalidJsonError from exc
    if not math.isfinite(result):
        raise _ResourceLimitError
    return result


def _reject_nonstandard_number(_value: str) -> None:
    raise _InvalidJsonError


class _JsonStructureScanner:
    """Bounded byte-level JSON lexer used before ``json.loads``.

    The scanner deliberately does not build a Python object graph.  It walks
    JSON values with a depth-bounded state machine and counts nodes,
    containers, and members before the materialising parser runs.  Syntax
    errors are reported as invalid JSON; only an exceeded resource budget is
    reported as a resource limit.
    """

    __slots__ = ("raw", "index", "nodes", "containers", "string_bytes")

    _HEX = frozenset(b"0123456789abcdefABCDEF")
    _WHITESPACE = frozenset((0x20, 0x09, 0x0A, 0x0D))

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.index = 0
        self.nodes = 0
        self.containers = 0
        self.string_bytes = 0

    def scan(self) -> None:
        self._skip_whitespace()
        if self.index >= len(self.raw):
            raise _InvalidJsonError
        self._value(depth=0)
        self._skip_whitespace()
        if self.index != len(self.raw):
            raise _InvalidJsonError

    def _skip_whitespace(self) -> None:
        raw = self.raw
        size = len(raw)
        while self.index < size and raw[self.index] in self._WHITESPACE:
            self.index += 1

    def _node(self) -> None:
        self.nodes += 1
        if self.nodes > P3_MAX_TOTAL_NODES:
            raise _ResourceLimitError

    def _container(self, depth: int) -> None:
        self.containers += 1
        if self.containers > P3_MAX_CONTAINERS:
            raise _ResourceLimitError
        if depth + 1 > P3_MAX_JSON_DEPTH:
            raise _ResourceLimitError

    def _value(self, *, depth: int) -> None:
        self._skip_whitespace()
        if self.index >= len(self.raw):
            raise _InvalidJsonError
        byte = self.raw[self.index]
        self._node()
        if byte == 0x22:  # string
            self._string()
        elif byte == 0x7B:  # object
            self._object(depth)
        elif byte == 0x5B:  # array
            self._array(depth)
        elif byte == 0x2D or 0x30 <= byte <= 0x39:
            self._number()
        elif byte == 0x74:  # true
            self._literal(b"true")
        elif byte == 0x66:  # false
            self._literal(b"false")
        elif byte == 0x6E:  # null
            self._literal(b"null")
        else:
            raise _InvalidJsonError

    def _member_count(self, count: int) -> int:
        count += 1
        if count > P3_MAX_CONTAINER_ITEMS:
            raise _ResourceLimitError
        return count

    def _object(self, depth: int) -> None:
        self._container(depth)
        self.index += 1  # {
        self._skip_whitespace()
        if self.index < len(self.raw) and self.raw[self.index] == 0x7D:
            self.index += 1
            return
        count = 0
        while True:
            self._skip_whitespace()
            if self.index >= len(self.raw) or self.raw[self.index] != 0x22:
                raise _InvalidJsonError
            self._node()  # object key string
            self._string()
            self._skip_whitespace()
            if self.index >= len(self.raw) or self.raw[self.index] != 0x3A:
                raise _InvalidJsonError
            self.index += 1  # :
            self._value(depth=depth + 1)
            count = self._member_count(count)
            self._skip_whitespace()
            if self.index >= len(self.raw):
                raise _InvalidJsonError
            byte = self.raw[self.index]
            if byte == 0x7D:
                self.index += 1
                return
            if byte != 0x2C:
                raise _InvalidJsonError
            self.index += 1  # ,

    def _array(self, depth: int) -> None:
        self._container(depth)
        self.index += 1  # [
        self._skip_whitespace()
        if self.index < len(self.raw) and self.raw[self.index] == 0x5D:
            self.index += 1
            return
        count = 0
        while True:
            self._value(depth=depth + 1)
            count = self._member_count(count)
            self._skip_whitespace()
            if self.index >= len(self.raw):
                raise _InvalidJsonError
            byte = self.raw[self.index]
            if byte == 0x5D:
                self.index += 1
                return
            if byte != 0x2C:
                raise _InvalidJsonError
            self.index += 1  # ,
            self._skip_whitespace()

    def _literal(self, literal: bytes) -> None:
        end = self.index + len(literal)
        if self.raw[self.index : end] != literal:
            raise _InvalidJsonError
        self.index = end

    def _number(self) -> None:
        start = self.index
        raw = self.raw
        size = len(raw)
        if raw[self.index] == 0x2D:  # -
            self.index += 1
            if self.index >= size:
                raise _InvalidJsonError
        if raw[self.index] == 0x30:  # 0
            self.index += 1
            if self.index < size and 0x30 <= raw[self.index] <= 0x39:
                raise _InvalidJsonError
        elif 0x31 <= raw[self.index] <= 0x39:
            self.index += 1
            while self.index < size and 0x30 <= raw[self.index] <= 0x39:
                self.index += 1
        else:
            raise _InvalidJsonError
        if self.index < size and raw[self.index] == 0x2E:  # .
            self.index += 1
            fraction_start = self.index
            while self.index < size and 0x30 <= raw[self.index] <= 0x39:
                self.index += 1
            if self.index == fraction_start:
                raise _InvalidJsonError
        if self.index < size and raw[self.index] in (0x65, 0x45):  # e/E
            self.index += 1
            if self.index < size and raw[self.index] in (0x2B, 0x2D):
                self.index += 1
            exponent_start = self.index
            while self.index < size and 0x30 <= raw[self.index] <= 0x39:
                self.index += 1
            if self.index == exponent_start:
                raise _InvalidJsonError
        if self.index < size and raw[self.index] not in (
            *self._WHITESPACE,
            0x2C,  # ,
            0x5D,  # ]
            0x7D,  # }
        ):
            raise _InvalidJsonError
        if self.index - start > _MAX_JSON_NUMBER_TOKEN_LENGTH:
            raise _ResourceLimitError

    def _string(self) -> None:
        if self.index >= len(self.raw) or self.raw[self.index] != 0x22:
            raise _InvalidJsonError
        self.index += 1  # opening quote
        chars = 0
        encoded_bytes = 0
        limit_exceeded = False
        raw = self.raw
        size = len(raw)
        while self.index < size:
            byte = raw[self.index]
            if byte == 0x22:
                self.index += 1
                self.string_bytes += encoded_bytes
                if self.string_bytes > P3_MAX_TOTAL_STRING_BYTES:
                    limit_exceeded = True
                if limit_exceeded:
                    raise _ResourceLimitError
                return
            if byte == 0x5C:  # backslash
                self.index += 1
                if self.index >= size:
                    raise _InvalidJsonError
                escaped = raw[self.index]
                if escaped == 0x75:  # uXXXX
                    if self.index + 4 >= size:
                        raise _InvalidJsonError
                    digits = raw[self.index + 1 : self.index + 5]
                    if any(digit not in self._HEX for digit in digits):
                        raise _InvalidJsonError
                    code_unit = int(digits, 16)
                    self.index += 5
                    if 0xD800 <= code_unit <= 0xDBFF:
                        # A valid surrogate pair is one Python code point.  A
                        # lone surrogate is left for json.loads/post-check to
                        # reject consistently as an encoding/resource error.
                        if (
                            self.index + 5 < size
                            and raw[self.index] == 0x5C
                            and raw[self.index + 1] == 0x75
                            and all(
                                digit in self._HEX
                                for digit in raw[self.index + 2 : self.index + 6]
                            )
                        ):
                            low = int(raw[self.index + 2 : self.index + 6], 16)
                            if 0xDC00 <= low <= 0xDFFF:
                                self.index += 6
                                chars += 1
                                encoded_bytes += 4
                                if chars > P3_MAX_STRING_LENGTH:
                                    limit_exceeded = True
                                continue
                    chars += 1
                    if 0xD800 <= code_unit <= 0xDFFF:
                        encoded_bytes += 3
                    elif code_unit <= 0x7F:
                        encoded_bytes += 1
                    elif code_unit <= 0x7FF:
                        encoded_bytes += 2
                    else:
                        encoded_bytes += 3
                elif escaped in b'"\\/':
                    self.index += 1
                    chars += 1
                    encoded_bytes += 1
                elif escaped in b"bfnrt":
                    self.index += 1
                    chars += 1
                    encoded_bytes += 1
                else:
                    raise _InvalidJsonError
            elif byte < 0x20:
                raise _InvalidJsonError
            elif byte < 0x80:
                self.index += 1
                chars += 1
                encoded_bytes += 1
            else:
                consumed, utf8_bytes = self._utf8_scalar(self.index)
                self.index += consumed
                chars += 1
                encoded_bytes += utf8_bytes
            if chars > P3_MAX_STRING_LENGTH:
                limit_exceeded = True
            if self.string_bytes + encoded_bytes > P3_MAX_TOTAL_STRING_BYTES:
                limit_exceeded = True
        raise _InvalidJsonError

    def _utf8_scalar(self, index: int) -> tuple[int, int]:
        raw = self.raw
        size = len(raw)
        first = raw[index]

        def continuation(position: int) -> int:
            if position >= size or not 0x80 <= raw[position] <= 0xBF:
                raise _InvalidJsonError
            return raw[position]

        if 0xC2 <= first <= 0xDF:
            continuation(index + 1)
            return 2, 2
        if first == 0xE0:
            second = continuation(index + 1)
            if second < 0xA0:
                raise _InvalidJsonError
            continuation(index + 2)
            return 3, 3
        if 0xE1 <= first <= 0xEC or 0xEE <= first <= 0xEF:
            continuation(index + 1)
            continuation(index + 2)
            return 3, 3
        if first == 0xED:
            second = continuation(index + 1)
            if second > 0x9F:
                raise _InvalidJsonError
            continuation(index + 2)
            return 3, 3
        if first == 0xF0:
            second = continuation(index + 1)
            if second < 0x90:
                raise _InvalidJsonError
            continuation(index + 2)
            continuation(index + 3)
            return 4, 4
        if 0xF1 <= first <= 0xF3:
            continuation(index + 1)
            continuation(index + 2)
            continuation(index + 3)
            return 4, 4
        if first == 0xF4:
            second = continuation(index + 1)
            if second > 0x8F:
                raise _InvalidJsonError
            continuation(index + 2)
            continuation(index + 3)
            return 4, 4
        raise _InvalidJsonError


def _check_encoded_depth(raw: bytes) -> None:
    _JsonStructureScanner(raw).scan()


def _check_value_limits(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while stack:
        item, parent_depth = stack.pop()
        nodes += 1
        if nodes > P3_MAX_TOTAL_NODES:
            raise _ResourceLimitError
        if isinstance(item, str):
            if len(item) > P3_MAX_STRING_LENGTH:
                raise _ResourceLimitError
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > P3_MAX_TOTAL_STRING_BYTES:
                raise _ResourceLimitError
            continue
        if isinstance(item, Mapping):
            depth = parent_depth + 1
            if depth > P3_MAX_JSON_DEPTH:
                raise _ResourceLimitError
            if len(item) > P3_MAX_CONTAINER_ITEMS:
                raise _ResourceLimitError
            for key, child in item.items():
                stack.append((child, depth))
                stack.append((key, depth))
            continue
        if isinstance(item, list):
            depth = parent_depth + 1
            if depth > P3_MAX_JSON_DEPTH:
                raise _ResourceLimitError
            if len(item) > P3_MAX_CONTAINER_ITEMS:
                raise _ResourceLimitError
            stack.extend((child, depth) for child in item)


def _read_regular_file(path: str | Path, *, label: str) -> bytes:
    source = _require_path(path, label)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    before: os.stat_result | None = None
    try:
        if not nofollow:
            before = os.lstat(source)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise _InvalidJsonError
        descriptor: int | None = os.open(source, flags)
    except SloWindowEvidenceError:
        raise
    except OSError:
        raise _InvalidJsonError from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _InvalidJsonError
        if before is not None:
            current = os.lstat(source)
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise _InvalidJsonError
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise _InvalidJsonError
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise _InvalidJsonError
        if opened.st_size > P3_MAX_FILE_BYTES:
            raise _ResourceLimitError
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            raw = stream.read(P3_MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
    except SloWindowEvidenceError:
        raise
    except OSError:
        raise _InvalidJsonError from None
    except (MemoryError, OverflowError):
        raise _ResourceLimitError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > P3_MAX_FILE_BYTES:
        raise _ResourceLimitError
    if (
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise _InvalidJsonError
    return raw


def _decode_json(raw: bytes, *, label: str) -> dict[str, Any]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _InvalidJsonError
    try:
        _check_encoded_depth(raw)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_int=_parse_json_integer,
            parse_float=_parse_json_float,
            parse_constant=_reject_nonstandard_number,
        )
    except SloWindowEvidenceError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        raise _InvalidJsonError from None
    except (RecursionError, MemoryError, OverflowError):
        raise _ResourceLimitError from None
    if not isinstance(value, dict):
        raise SloWindowEvidenceError(f"{label} root must be an object")
    try:
        _check_value_limits(value)
    except (UnicodeError, RecursionError, MemoryError, OverflowError):
        raise _ResourceLimitError from None
    return value


def _read_json_with_raw(
    path: str | Path,
    *,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_file(path, label=label)
    return _decode_json(raw, label=label), raw


def _read_json(path: str | Path, *, label: str) -> dict[str, Any]:
    value, _raw = _read_json_with_raw(path, label=label)
    return value


def read_slo_metrics_export(path: str | Path) -> dict[str, Any]:
    try:
        return _read_json(path, label="metrics export")
    except _ResourceLimitError:
        raise SloWindowEvidenceError(P3_ERROR_RESOURCE_LIMIT) from None
    except (_InvalidJsonError, SloWindowEvidenceError):
        raise SloWindowEvidenceError(P3_ERROR_INVALID_JSON) from None
    except Exception:  # noqa: BLE001 - public evidence boundary is fail-closed
        raise SloWindowEvidenceError(P3_ERROR_INVALID_JSON) from None


def read_incident_list(path: str | Path) -> dict[str, Any]:
    try:
        return _read_json(path, label="incident list")
    except _ResourceLimitError:
        raise SloWindowEvidenceError(P3_ERROR_RESOURCE_LIMIT) from None
    except (_InvalidJsonError, SloWindowEvidenceError):
        raise SloWindowEvidenceError(P3_ERROR_INVALID_JSON) from None
    except Exception:  # noqa: BLE001 - public evidence boundary is fail-closed
        raise SloWindowEvidenceError(P3_ERROR_INVALID_JSON) from None


def _exact(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SloWindowEvidenceError(f"{field} has unknown or missing fields")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise SloWindowEvidenceError(f"{field} must be an identifier")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SloWindowEvidenceError(f"{field} must be lowercase SHA-256")
    return value


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise SloWindowEvidenceError(f"{field} must be an integer in range")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SloWindowEvidenceError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SloWindowEvidenceError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise SloWindowEvidenceError(f"{field} must be a finite number")
    return result


def _timestamp(value: Any, field: str) -> datetime:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _RFC3339_RE.fullmatch(value) is None
    ):
        raise SloWindowEvidenceError(f"{field} must be RFC3339 with timezone")
    match = _RFC3339_RE.fullmatch(value)
    if match is None:  # defensive; guarded above
        raise SloWindowEvidenceError(f"{field} must be RFC3339 with timezone")
    offset = match.group("offset")
    if offset == "-00:00":
        raise SloWindowEvidenceError(f"{field} must use a known timezone offset")
    if offset != "Z":
        hours, minutes = (int(part) for part in offset[1:].split(":"))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
            raise SloWindowEvidenceError(f"{field} has an invalid timezone offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise SloWindowEvidenceError(f"{field} must be RFC3339 with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SloWindowEvidenceError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalise_as_of(value: Any) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SloWindowEvidenceError("as_of must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _profile_binding(authority: VerifiedProductionAcceptanceProfile) -> dict[str, str]:
    return {
        "profile_id": authority.profile_id,
        "profile_version": authority.profile_version,
        "profile_content_sha256": authority.profile_content_sha256,
    }


def _verify_authority(
    signed_profile_path: str | Path,
    trust_store_path: str | Path,
    inventory_path: str | Path,
    *,
    as_of: datetime,
) -> VerifiedProductionAcceptanceProfile:
    for value, field in (
        (signed_profile_path, "signed_profile_path"),
        (trust_store_path, "trust_store_path"),
        (inventory_path, "inventory_path"),
    ):
        _require_path(value, field)
    effective_at = _normalise_as_of(as_of)
    try:
        authority = verify_signed_production_acceptance_profile(
            signed_profile_path,
            trust_store_path,
            inventory_path=inventory_path,
            now=effective_at,
        )
    except ProductionAcceptanceProfileError:
        raise _InvalidProfileError from None
    if type(authority) is not VerifiedProductionAcceptanceProfile:
        raise SloWindowEvidenceError(
            "signed P0 verifier returned an invalid authority type"
        )
    if effective_at != authority.observation_start:
        raise SloWindowEvidenceError(
            "as_of must equal the signed P0 observation_window.start"
        )
    try:
        content_hash = production_profile_content_sha256(authority.profile)
    except Exception as exc:  # noqa: BLE001 - immutable authority boundary
        raise SloWindowEvidenceError(
            "signed P0 profile hash cannot be recomputed"
        ) from exc
    if content_hash != authority.profile_content_sha256:
        raise SloWindowEvidenceError("signed P0 profile content hash mismatch")
    return authority


def _verify_binding(
    value: Any,
    authority: VerifiedProductionAcceptanceProfile,
    *,
    field: str,
) -> None:
    binding = _exact(value, _PROFILE_BINDING_FIELDS, field)
    _identifier(binding["profile_id"], f"{field}.profile_id")
    if (
        not isinstance(binding["profile_version"], str)
        or not binding["profile_version"]
    ):
        raise SloWindowEvidenceError(f"{field}.profile_version must be non-empty text")
    _sha256(binding["profile_content_sha256"], f"{field}.profile_content_sha256")
    if dict(binding) != _profile_binding(authority):
        raise SloWindowEvidenceError(f"{field} does not match the signed P0 profile")


def _verify_observation_binding(
    value: Any,
    authority: VerifiedProductionAcceptanceProfile,
    *,
    field: str,
) -> tuple[datetime, datetime, int]:
    observation = _exact(value, _OBSERVATION_FIELDS, field)
    cadence = _exact(observation["cadence"], _CADENCE_FIELDS, f"{field}.cadence")
    interval = _bounded_int(
        cadence["interval_seconds"],
        f"{field}.cadence.interval_seconds",
        minimum=1,
        maximum=P3_MAX_CADENCE_SECONDS,
    )
    _bounded_int(
        cadence["tolerance_seconds"],
        f"{field}.cadence.tolerance_seconds",
        minimum=0,
        maximum=interval,
    )
    signed_observation = authority.profile.get("observation_window")
    if not isinstance(signed_observation, Mapping) or dict(observation) != dict(
        signed_observation
    ):
        raise SloWindowEvidenceError(f"{field} does not match the signed P0 profile")
    start = _timestamp(observation["start"], f"{field}.start")
    end = _timestamp(observation["end"], f"{field}.end")
    minimum = _bounded_int(
        observation["minimum_duration_seconds"],
        f"{field}.minimum_duration_seconds",
        minimum=P3_MIN_WINDOW_SECONDS,
        maximum=365 * 86_400,
    )
    elapsed = (end - start).total_seconds()
    if elapsed < P3_MIN_WINDOW_SECONDS or elapsed < minimum:
        raise SloWindowEvidenceError("observation window is shorter than required")
    if start != authority.observation_start or end != authority.observation_end:
        raise SloWindowEvidenceError("observation window differs from signed authority")
    return start, end, minimum


def _verify_metric_value(metric: str, value: Any, field: str) -> float:
    if metric == "stuck_job_count" and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        raise SloWindowEvidenceError(f"{field} must be an integer count")
    number = _finite_number(value, field)
    if not 0 <= number <= _METRIC_MAXIMUMS[metric]:
        raise SloWindowEvidenceError(f"{field} is outside source-controlled bounds")
    return number


def _verify_metrics(
    value: Any,
    authority: VerifiedProductionAcceptanceProfile,
) -> tuple[str, int, int, int, dict[str, float], dict[str, float], datetime, datetime]:
    metrics = _exact(value, _METRICS_FIELDS, "metrics export")
    if metrics["schema_version"] != P3_METRICS_EXPORT_SCHEMA:
        raise SloWindowEvidenceError("unknown P3 metrics export schema")
    export_id = _identifier(metrics["export_id"], "metrics export.export_id")
    _verify_binding(
        metrics["profile_binding"], authority, field="metrics profile_binding"
    )
    profile_load = authority.profile.get("load_profile")
    if not isinstance(profile_load, Mapping) or metrics["load_profile"] != profile_load:
        raise SloWindowEvidenceError("metrics load_profile does not match signed P0")
    workflow_ids = metrics["workflow_ids"]
    if not isinstance(workflow_ids, list) or workflow_ids != profile_load.get(
        "workflow_ids"
    ):
        raise SloWindowEvidenceError("metrics workflow_ids do not match signed P0")
    start, end, _minimum = _verify_observation_binding(
        metrics["observation_window"], authority, field="metrics observation_window"
    )
    cadence = _exact(metrics["cadence"], _CADENCE_FIELDS, "metrics cadence")
    export_interval = _bounded_int(
        cadence["interval_seconds"],
        "metrics cadence.interval_seconds",
        minimum=1,
        maximum=P3_MAX_CADENCE_SECONDS,
    )
    _bounded_int(
        cadence["tolerance_seconds"],
        "metrics cadence.tolerance_seconds",
        minimum=0,
        maximum=export_interval,
    )
    signed_observation = authority.profile.get("observation_window")
    signed_cadence = (
        signed_observation.get("cadence")
        if isinstance(signed_observation, Mapping)
        else None
    )
    if not isinstance(signed_cadence, Mapping) or dict(cadence) != dict(signed_cadence):
        raise SloWindowEvidenceError("metrics cadence does not match signed P0")
    interval = _bounded_int(
        signed_cadence["interval_seconds"],
        "signed P0 observation_window.cadence.interval_seconds",
        minimum=1,
        maximum=P3_MAX_CADENCE_SECONDS,
    )
    tolerance = _bounded_int(
        signed_cadence["tolerance_seconds"],
        "signed P0 observation_window.cadence.tolerance_seconds",
        minimum=0,
        maximum=interval,
    )
    maximum_gap = interval + tolerance
    samples = metrics["samples"]
    if not isinstance(samples, list) or not 2 <= len(samples) <= P3_MAX_SAMPLES:
        raise SloWindowEvidenceError("metrics samples count is outside allowed range")
    minimum_samples = math.ceil((end - start).total_seconds() / maximum_gap) + 1
    if len(samples) < minimum_samples:
        raise SloWindowEvidenceError(
            "metrics samples are insufficient for declared cadence"
        )

    timestamps: list[datetime] = []
    observed: dict[str, list[float]] = {metric: [] for metric in P3_SLO_FIELDS}
    for index, raw_sample in enumerate(samples):
        sample = _exact(raw_sample, _SAMPLE_FIELDS, f"metrics samples[{index}]")
        timestamp = _timestamp(
            sample["timestamp"], f"metrics samples[{index}].timestamp"
        )
        if timestamp < start or timestamp > end:
            raise SloWindowEvidenceError(
                "metrics sample timestamp is outside observation window"
            )
        if timestamps and timestamp <= timestamps[-1]:
            raise SloWindowEvidenceError(
                "metrics sample timestamps must be strictly increasing and unique"
            )
        if timestamps and (timestamp - timestamps[-1]).total_seconds() > maximum_gap:
            raise SloWindowEvidenceError(
                "metrics sample gap exceeds declared cadence tolerance"
            )
        timestamps.append(timestamp)
        values = _exact(
            sample["values"], _SLO_FIELD_SET, f"metrics samples[{index}].values"
        )
        for metric in P3_SLO_FIELDS:
            observed[metric].append(
                _verify_metric_value(
                    metric,
                    values[metric],
                    f"metrics samples[{index}].values.{metric}",
                )
            )
    if timestamps[0] != start or timestamps[-1] != end:
        raise SloWindowEvidenceError(
            "metrics first and last samples must align with observation window"
        )

    raw_thresholds = authority.profile.get("slo_thresholds")
    thresholds = _exact(raw_thresholds, _SLO_FIELD_SET, "signed P0 slo_thresholds")
    normalized_thresholds = {
        metric: _finite_number(thresholds[metric], f"signed P0 slo_thresholds.{metric}")
        for metric in P3_SLO_FIELDS
    }
    aggregates = {
        metric: max(observed[metric])
        if metric in _UPPER_BOUND_SLOS
        else min(observed[metric])
        for metric in P3_SLO_FIELDS
    }
    for metric in P3_SLO_FIELDS:
        aggregate = aggregates[metric]
        threshold = normalized_thresholds[metric]
        if metric in _UPPER_BOUND_SLOS and aggregate > threshold:
            raise SloWindowEvidenceError(f"{metric} exceeds signed P0 threshold")
        if metric in _LOWER_BOUND_SLOS and aggregate < threshold:
            raise SloWindowEvidenceError(f"{metric} is below signed P0 threshold")
    return (
        export_id,
        interval,
        tolerance,
        len(samples),
        aggregates,
        normalized_thresholds,
        start,
        end,
    )


def _verify_incidents(
    value: Any,
    authority: VerifiedProductionAcceptanceProfile,
    *,
    expected_start: datetime,
    expected_end: datetime,
) -> tuple[str, int]:
    """Validate only the incident rows supplied by the export.

    Completeness, source provenance, and truthful severity classification are
    external operational facts; this local verifier intentionally does not
    claim to establish them.
    """
    incident_list = _exact(value, _INCIDENT_LIST_FIELDS, "incident list")
    if incident_list["schema_version"] != P3_INCIDENT_LIST_SCHEMA:
        raise SloWindowEvidenceError("unknown P3 incident list schema")
    list_id = _identifier(
        incident_list["incident_list_id"], "incident list.incident_list_id"
    )
    _verify_binding(
        incident_list["profile_binding"], authority, field="incident profile_binding"
    )
    start, end, _minimum = _verify_observation_binding(
        incident_list["observation_window"],
        authority,
        field="incident observation_window",
    )
    if start != expected_start or end != expected_end:
        raise SloWindowEvidenceError("metrics and incident observation windows differ")
    incidents = incident_list["incidents"]
    if not isinstance(incidents, list) or len(incidents) > P3_MAX_INCIDENTS:
        raise SloWindowEvidenceError("incidents must be an explicit bounded list")
    taxonomy = authority.profile.get("severity_taxonomy")
    if not isinstance(taxonomy, Mapping) or set(taxonomy) != {"sev1", "sev2"}:
        raise SloWindowEvidenceError("signed P0 severity taxonomy is invalid")
    seen: set[str] = set()
    for index, raw_incident in enumerate(incidents):
        incident = _exact(
            raw_incident, _INCIDENT_FIELDS, f"incident list.incidents[{index}]"
        )
        incident_id = _identifier(
            incident["incident_id"], f"incident list.incidents[{index}].incident_id"
        )
        if incident_id in seen:
            raise SloWindowEvidenceError("incident list contains duplicate incident_id")
        seen.add(incident_id)
        severity = incident["severity"]
        if severity not in {"sev1", "sev2"}:
            raise SloWindowEvidenceError("incident severity must be sev1 or sev2")
        rule = _exact(
            taxonomy[severity],
            frozenset({"definition", "ack_seconds", "mitigate_seconds"}),
            f"signed P0 severity_taxonomy.{severity}",
        )
        definition = incident["taxonomy_definition"]
        if not isinstance(definition, str) or definition not in rule["definition"]:
            raise SloWindowEvidenceError(
                "incident taxonomy_definition does not match signed P0 taxonomy"
            )
        started = _timestamp(
            incident["started_at"], f"incident list.incidents[{index}].started_at"
        )
        acknowledged = _timestamp(
            incident["acknowledged_at"],
            f"incident list.incidents[{index}].acknowledged_at",
        )
        mitigated = _timestamp(
            incident["mitigated_at"], f"incident list.incidents[{index}].mitigated_at"
        )
        if not start <= started <= acknowledged <= mitigated <= end:
            raise SloWindowEvidenceError(
                "incident timestamps must be ordered inside observation window"
            )
        ack_limit = _bounded_int(
            rule["ack_seconds"],
            f"signed P0 severity_taxonomy.{severity}.ack_seconds",
            minimum=1,
            maximum=365 * 86_400,
        )
        mitigation_limit = _bounded_int(
            rule["mitigate_seconds"],
            f"signed P0 severity_taxonomy.{severity}.mitigate_seconds",
            minimum=1,
            maximum=365 * 86_400,
        )
        if (acknowledged - started).total_seconds() > ack_limit:
            raise SloWindowEvidenceError(
                "incident acknowledgement exceeds signed P0 threshold"
            )
        if (mitigated - started).total_seconds() > mitigation_limit:
            raise SloWindowEvidenceError(
                "incident mitigation exceeds signed P0 threshold"
            )
    return list_id, len(incidents)


@dataclass(frozen=True, slots=True)
class VerifiedSloWindowEvidence:
    """Read-only P3 evaluation result; never Gate 6 acceptance or approval."""

    metrics_export_id: str
    incident_list_id: str
    metrics_sha256: str
    incident_list_sha256: str
    profile_id: str
    profile_version: str
    profile_content_sha256: str
    observation_start: datetime
    observation_end: datetime
    cadence_interval_seconds: int
    cadence_tolerance_seconds: int
    sample_count: int
    incident_count: int
    aggregates: Mapping[str, float]
    thresholds: Mapping[str, float]
    missing_external_p3_evidence: tuple[str, ...] = dataclass_field(
        init=False,
        default=P3_MISSING_EXTERNAL_P3_EVIDENCE,
    )

    @property
    def status(self) -> str:
        return P3_VERIFICATION_STATUS

    @property
    def acceptance_status(self) -> str:
        return P3_ACCEPTANCE_STATUS

    @property
    def gate6_pass(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": P3_VERIFICATION_SCHEMA,
            "status": P3_VERIFICATION_STATUS,
            "acceptance_status": P3_ACCEPTANCE_STATUS,
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-acceptance-p3-slo-window-evidence",
            "profile_binding": {
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "profile_content_sha256": self.profile_content_sha256,
            },
            "metrics_export_id": self.metrics_export_id,
            "incident_list_id": self.incident_list_id,
            "metrics_sha256": self.metrics_sha256,
            "incident_list_sha256": self.incident_list_sha256,
            "observation_window": {
                "start": self.observation_start.isoformat(),
                "end": self.observation_end.isoformat(),
            },
            "cadence": {
                "interval_seconds": self.cadence_interval_seconds,
                "tolerance_seconds": self.cadence_tolerance_seconds,
            },
            "sample_count": self.sample_count,
            "incident_count": self.incident_count,
            "missing_external_p3_evidence": list(self.missing_external_p3_evidence),
            "aggregate_method": {
                metric: "window_max" if metric in _UPPER_BOUND_SLOS else "window_min"
                for metric in P3_SLO_FIELDS
            },
            "observed_aggregates": dict(self.aggregates),
            "signed_thresholds": dict(self.thresholds),
        }


def _verify_slo_window_files(
    metrics_export_path: str | Path,
    incident_list_path: str | Path,
    signed_profile_path: str | Path,
    trust_store_path: str | Path,
    *,
    inventory_path: str | Path,
    as_of: datetime,
) -> VerifiedSloWindowEvidence:
    """Verify raw P3 exports and the raw signed P0 authority in one operation."""

    metrics, _metrics_raw = _read_json_with_raw(
        metrics_export_path,
        label="metrics export",
    )
    incidents, _incidents_raw = _read_json_with_raw(
        incident_list_path,
        label="incident list",
    )
    _profile, profile_raw = _read_json_with_raw(
        signed_profile_path,
        label="signed profile",
    )
    _trust_store, trust_store_raw = _read_json_with_raw(
        trust_store_path,
        label="trust store",
    )
    _inventory, inventory_raw = _read_json_with_raw(
        inventory_path,
        label="inventory",
    )
    with tempfile.TemporaryDirectory(prefix="anila-p3-authority-") as snapshot_dir:
        snapshot_root = Path(snapshot_dir)
        profile_snapshot = snapshot_root / "profile.json"
        trust_store_snapshot = snapshot_root / "trust.json"
        inventory_snapshot = snapshot_root / "inventory.json"
        for snapshot, raw in (
            (profile_snapshot, profile_raw),
            (trust_store_snapshot, trust_store_raw),
            (inventory_snapshot, inventory_raw),
        ):
            snapshot.write_bytes(raw)
            snapshot.chmod(0o600)
        authority = _verify_authority(
            profile_snapshot,
            trust_store_snapshot,
            inventory_snapshot,
            as_of=as_of,
        )
    (
        metrics_id,
        interval,
        tolerance,
        sample_count,
        aggregates,
        thresholds,
        start,
        end,
    ) = _verify_metrics(metrics, authority)
    incident_list_id, incident_count = _verify_incidents(
        incidents,
        authority,
        expected_start=start,
        expected_end=end,
    )
    return VerifiedSloWindowEvidence(
        metrics_export_id=metrics_id,
        incident_list_id=incident_list_id,
        metrics_sha256=hashlib.sha256(canonical_json(metrics)).hexdigest(),
        incident_list_sha256=hashlib.sha256(canonical_json(incidents)).hexdigest(),
        profile_id=authority.profile_id,
        profile_version=authority.profile_version,
        profile_content_sha256=authority.profile_content_sha256,
        observation_start=start,
        observation_end=end,
        cadence_interval_seconds=interval,
        cadence_tolerance_seconds=tolerance,
        sample_count=sample_count,
        incident_count=incident_count,
        aggregates=MappingProxyType(aggregates),
        thresholds=MappingProxyType(thresholds),
    )


def verify_slo_window_files(
    metrics_export_path: str | Path,
    incident_list_path: str | Path,
    signed_profile_path: str | Path,
    trust_store_path: str | Path,
    *,
    inventory_path: str | Path,
    as_of: datetime,
) -> VerifiedSloWindowEvidence:
    """Public fail-closed verifier exposing only stable, content-free errors."""

    try:
        return _verify_slo_window_files(
            metrics_export_path,
            incident_list_path,
            signed_profile_path,
            trust_store_path,
            inventory_path=inventory_path,
            as_of=as_of,
        )
    except _ResourceLimitError:
        raise SloWindowEvidenceError(P3_ERROR_RESOURCE_LIMIT) from None
    except _InvalidJsonError:
        raise SloWindowEvidenceError(P3_ERROR_INVALID_JSON) from None
    except _InvalidProfileError:
        raise SloWindowEvidenceError(P3_ERROR_INVALID_PROFILE) from None
    except SloWindowEvidenceError:
        raise SloWindowEvidenceError(P3_ERROR_INVALID_EVIDENCE) from None
    except Exception:  # noqa: BLE001 - public verifier exposes fixed codes only
        raise SloWindowEvidenceError(P3_ERROR_INVALID_EVIDENCE) from None


verify_slo_window = verify_slo_window_files


__all__ = [
    "P3_ACCEPTANCE_STATUS",
    "P3_ERROR_INVALID_EVIDENCE",
    "P3_ERROR_INVALID_JSON",
    "P3_ERROR_INVALID_PROFILE",
    "P3_ERROR_RESOURCE_LIMIT",
    "P3_INCIDENT_LIST_SCHEMA",
    "P3_METRICS_EXPORT_SCHEMA",
    "P3_MIN_WINDOW_SECONDS",
    "P3_MAX_CONTAINERS",
    "P3_MISSING_EXTERNAL_EVIDENCE",
    "P3_MISSING_EXTERNAL_P3_EVIDENCE",
    "P3_SLO_FIELDS",
    "P3_VERIFICATION_SCHEMA",
    "P3_VERIFICATION_STATUS",
    "SloWindowEvidenceError",
    "VerifiedSloWindowEvidence",
    "read_incident_list",
    "read_slo_metrics_export",
    "verify_slo_window",
    "verify_slo_window_files",
]
