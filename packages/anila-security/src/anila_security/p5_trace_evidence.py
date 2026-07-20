"""Gate 6 P5 six-hop trace evidence shape and consistency verifier.

This module is code-complete tooling, not production acceptance.  It validates
one internally consistent sampled chain across the repository's six P5 hops:
upload, chunk, retrieval, citation, artifact, and trace.  It does not select a
sample, enumerate a workflow matrix, verify a signed P0 profile, or approve
Gate 6.

The consistency rules mirror the current data flow:

* classification may stay equal or rise through max/latch propagation, but
  never fall;
* required compartments are the collection/document requirement union and
  therefore remain identical across the sampled chain;
* owner, source snapshot, sample/workflow/chain identities, and each hop's row
  binding remain stable.  A legitimate re-binding requires a new evidence
  sample rather than an in-place identity change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anila_contracts import Classification

from anila_security.production_acceptance_profile import canonical_json


P5_TRACE_EVIDENCE_SCHEMA = "anila.gate6.p5.trace-evidence.v1"
P5_TRACE_VERIFICATION_SCHEMA = "anila.gate6.p5.trace-verification.v1"
P5_TRACE_EVIDENCE_STATUS = "VERIFIED_NON_ACCEPTANCE"
P5_TRACE_ACCEPTANCE_STATUS = "NOT_ACCEPTANCE"
P5_TRACE_MAX_EVIDENCE_BYTES = 1_048_576
P5_TRACE_MAX_JSON_DEPTH = 32
P5_TRACE_MAX_SAMPLES = 256
P5_TRACE_MAX_COMPARTMENTS = 64
P5_TRACE_MAX_CONTAINER_ITEMS = 1_024
P5_TRACE_MAX_STRING_LENGTH = 4_096
P5_TRACE_MAX_TOTAL_NODES = 100_000
P5_TRACE_MAX_TOTAL_STRING_BYTES = P5_TRACE_MAX_EVIDENCE_BYTES
P5_TRACE_MIN_INTEGER = -9_223_372_036_854_775_808
P5_TRACE_MAX_INTEGER = 9_223_372_036_854_775_807
P5_TRACE_ERROR_DUPLICATE_JSON_KEY = "P5_TRACE_DUPLICATE_JSON_KEY"
P5_TRACE_ERROR_INVALID_EVIDENCE = "P5_TRACE_INVALID_EVIDENCE"
P5_TRACE_ERROR_INVALID_JSON = "P5_TRACE_INVALID_JSON"
P5_TRACE_ERROR_RESOURCE_LIMIT = "P5_TRACE_RESOURCE_LIMIT"
P5_TRACE_HOPS = (
    "upload",
    "chunk",
    "retrieval",
    "citation",
    "artifact",
    "trace",
)

_ROOT_FIELDS = frozenset({"schema_version", "evidence_id", "samples"})
_SAMPLE_FIELDS = frozenset(
    {
        "sample_id",
        "workflow_id",
        "chain_id",
        "owner_id",
        "snapshot_id",
        "required_compartments",
        "classification_path",
        "row_ids",
        "hops",
    }
)
_HOP_FIELDS = frozenset(
    {
        "row_id",
        "hop",
        "classification",
        "owner_id",
        "required_compartments",
        "snapshot_id",
        "sample_id",
        "workflow_id",
        "chain_id",
    }
)
_HOP_SET = frozenset(P5_TRACE_HOPS)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_COMPARTMENT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")


class P5TraceEvidenceError(ValueError):
    """Raised when P5 evidence is malformed, incomplete, or inconsistent."""


class _P5TraceDuplicateKeyError(P5TraceEvidenceError):
    pass


class _P5TraceInvalidJsonError(P5TraceEvidenceError):
    pass


class _P5TraceResourceLimitError(P5TraceEvidenceError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _P5TraceDuplicateKeyError
        value[key] = item
    return value


def _reject_nonstandard_json_constant(_value: str) -> None:
    """Reject JSON extensions such as NaN and Infinity before validation."""

    raise _P5TraceInvalidJsonError


def _check_encoded_json_depth(raw: bytes) -> None:
    """Reject excessive JSON container nesting before invoking the recursive decoder."""

    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            if depth > P5_TRACE_MAX_JSON_DEPTH:
                raise _P5TraceResourceLimitError(
                    f"P5 trace evidence JSON exceeds maximum depth {P5_TRACE_MAX_JSON_DEPTH}"
                )
        elif byte in (0x7D, 0x5D):  # } or ]
            depth -= 1


def _check_value_limits(value: Any) -> None:
    """Bound direct JSON-like inputs without recursively walking attacker data."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    total_nodes = 0
    total_string_bytes = 0
    while stack:
        item, parent_depth = stack.pop()
        total_nodes += 1
        if total_nodes > P5_TRACE_MAX_TOTAL_NODES:
            raise _P5TraceResourceLimitError(
                f"P5 trace evidence exceeds maximum node count {P5_TRACE_MAX_TOTAL_NODES}"
            )
        if isinstance(item, str):
            if len(item) > P5_TRACE_MAX_STRING_LENGTH:
                raise _P5TraceResourceLimitError(
                    "P5 trace evidence contains a string longer than "
                    f"{P5_TRACE_MAX_STRING_LENGTH} characters"
                )
            total_string_bytes += len(item.encode("utf-8"))
            if total_string_bytes > P5_TRACE_MAX_TOTAL_STRING_BYTES:
                raise _P5TraceResourceLimitError(
                    "P5 trace evidence exceeds maximum cumulative string bytes "
                    f"{P5_TRACE_MAX_TOTAL_STRING_BYTES}"
                )
            continue
        if isinstance(item, Mapping):
            depth = parent_depth + 1
            if depth > P5_TRACE_MAX_JSON_DEPTH:
                raise _P5TraceResourceLimitError(
                    f"P5 trace evidence exceeds maximum depth {P5_TRACE_MAX_JSON_DEPTH}"
                )
            if len(item) > P5_TRACE_MAX_CONTAINER_ITEMS:
                raise _P5TraceResourceLimitError(
                    "P5 trace evidence object exceeds maximum item count "
                    f"{P5_TRACE_MAX_CONTAINER_ITEMS}"
                )
            for key, child in item.items():
                stack.append((child, depth))
                stack.append((key, depth))
            continue
        if isinstance(item, (list, tuple)):
            depth = parent_depth + 1
            if depth > P5_TRACE_MAX_JSON_DEPTH:
                raise _P5TraceResourceLimitError(
                    f"P5 trace evidence exceeds maximum depth {P5_TRACE_MAX_JSON_DEPTH}"
                )
            if len(item) > P5_TRACE_MAX_CONTAINER_ITEMS:
                raise _P5TraceResourceLimitError(
                    "P5 trace evidence array exceeds maximum item count "
                    f"{P5_TRACE_MAX_CONTAINER_ITEMS}"
                )
            stack.extend((child, depth) for child in item)


def _read_p5_trace_evidence(path: str | Path) -> dict[str, Any]:
    """Read strict UTF-8 JSON while rejecting BOM and duplicate object keys."""

    evidence_path = Path(path)
    try:
        if evidence_path.stat().st_size > P5_TRACE_MAX_EVIDENCE_BYTES:
            raise _P5TraceResourceLimitError(
                f"P5 trace evidence exceeds maximum file size {P5_TRACE_MAX_EVIDENCE_BYTES} bytes"
            )
        with evidence_path.open("rb") as stream:
            raw = stream.read(P5_TRACE_MAX_EVIDENCE_BYTES + 1)
    except P5TraceEvidenceError:
        raise
    except OSError as exc:
        raise _P5TraceInvalidJsonError from exc
    except MemoryError as exc:
        raise _P5TraceResourceLimitError from exc
    if len(raw) > P5_TRACE_MAX_EVIDENCE_BYTES:
        raise _P5TraceResourceLimitError(
            f"P5 trace evidence exceeds maximum file size {P5_TRACE_MAX_EVIDENCE_BYTES} bytes"
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _P5TraceInvalidJsonError
    try:
        _check_encoded_json_depth(raw)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except P5TraceEvidenceError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError) as exc:
        raise _P5TraceInvalidJsonError from exc
    if not isinstance(value, dict):
        raise P5TraceEvidenceError("P5 trace evidence root must be an object")
    try:
        _check_value_limits(value)
    except P5TraceEvidenceError:
        raise
    except (UnicodeError, RecursionError, MemoryError) as exc:
        raise _P5TraceResourceLimitError from exc
    return value


def read_p5_trace_evidence(path: str | Path) -> dict[str, Any]:
    """Read strict JSON while exposing only stable, content-free error codes."""

    try:
        return _read_p5_trace_evidence(path)
    except _P5TraceDuplicateKeyError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_DUPLICATE_JSON_KEY) from None
    except _P5TraceResourceLimitError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_RESOURCE_LIMIT) from None
    except _P5TraceInvalidJsonError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_INVALID_JSON) from None
    except P5TraceEvidenceError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_INVALID_EVIDENCE) from None


def _exact(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise P5TraceEvidenceError(f"{field} has unknown or missing fields")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise P5TraceEvidenceError(f"{field} must be a non-empty identifier")
    return value


def _positive_int(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > P5_TRACE_MAX_INTEGER
    ):
        raise P5TraceEvidenceError(f"{field} must be a positive integer")
    return value


def _row_id(value: Any, field: str) -> str:
    if isinstance(value, bool):
        raise P5TraceEvidenceError(f"{field} must be a non-empty row identity")
    if isinstance(value, int):
        if value < P5_TRACE_MIN_INTEGER or value > P5_TRACE_MAX_INTEGER:
            raise P5TraceEvidenceError(f"{field} must be a non-empty row identity")
        return str(value)
    return _identifier(value, field)


def _classification(value: Any, field: str) -> Classification:
    if not isinstance(value, str):
        raise P5TraceEvidenceError(f"{field} must be a canonical classification")
    try:
        return Classification.from_storage(value)
    except ValueError as exc:
        raise P5TraceEvidenceError(f"{field} is an unknown classification") from exc


def _compartments(value: Any, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > P5_TRACE_MAX_COMPARTMENTS
        or any(
            not isinstance(item, str) or _COMPARTMENT_RE.fullmatch(item) is None for item in value
        )
    ):
        raise P5TraceEvidenceError(f"{field} must contain canonical compartment codes")
    if value != sorted(set(value)):
        raise P5TraceEvidenceError(f"{field} must be sorted and unique")
    return tuple(value)


def _hop_mapping(value: Any, field: str) -> Mapping[str, Any]:
    mapping = _exact(value, _HOP_SET, field)
    return mapping


@dataclass(frozen=True, slots=True)
class _VerifiedSample:
    sample_id: str
    workflow_id: str
    chain_id: str
    normalized: Mapping[str, Any]


def _verify_sample(value: Any, index: int) -> _VerifiedSample:
    field = f"samples[{index}]"
    sample = _exact(value, _SAMPLE_FIELDS, field)
    sample_id = _identifier(sample["sample_id"], f"{field}.sample_id")
    workflow_id = _identifier(sample["workflow_id"], f"{field}.workflow_id")
    chain_id = _identifier(sample["chain_id"], f"{field}.chain_id")
    owner_id = _positive_int(sample["owner_id"], f"{field}.owner_id")
    snapshot_id = _positive_int(sample["snapshot_id"], f"{field}.snapshot_id")
    compartments = _compartments(sample["required_compartments"], f"{field}.required_compartments")

    raw_path = _hop_mapping(sample["classification_path"], f"{field}.classification_path")
    classifications = {
        hop: _classification(raw_path[hop], f"{field}.classification_path.{hop}")
        for hop in P5_TRACE_HOPS
    }
    previous: Classification | None = None
    for hop in P5_TRACE_HOPS:
        current = classifications[hop]
        if previous is not None and current < previous:
            raise P5TraceEvidenceError(f"{field}.classification_path decreases at {hop}")
        previous = current

    raw_row_ids = _hop_mapping(sample["row_ids"], f"{field}.row_ids")
    row_ids = {hop: _row_id(raw_row_ids[hop], f"{field}.row_ids.{hop}") for hop in P5_TRACE_HOPS}

    raw_hops = sample["hops"]
    if not isinstance(raw_hops, list) or len(raw_hops) != len(P5_TRACE_HOPS):
        raise P5TraceEvidenceError(f"{field}.hops must contain exactly six rows")
    by_hop: dict[str, Mapping[str, Any]] = {}
    for hop_index, raw_hop in enumerate(raw_hops):
        hop_field = f"{field}.hops[{hop_index}]"
        item = _exact(raw_hop, _HOP_FIELDS, hop_field)
        hop = item["hop"]
        if not isinstance(hop, str) or hop not in _HOP_SET:
            raise P5TraceEvidenceError(f"{hop_field}.hop is invalid")
        if hop in by_hop:
            raise P5TraceEvidenceError(f"{field}.hops contains duplicate hop {hop}")
        if _row_id(item["row_id"], f"{hop_field}.row_id") != row_ids[hop]:
            raise P5TraceEvidenceError(f"{hop_field}.row_id breaks the row binding")
        if (
            _classification(item["classification"], f"{hop_field}.classification")
            != (classifications[hop])
        ):
            raise P5TraceEvidenceError(
                f"{hop_field}.classification breaks the declared propagation path"
            )
        if _positive_int(item["owner_id"], f"{hop_field}.owner_id") != owner_id:
            raise P5TraceEvidenceError(f"{hop_field}.owner_id breaks the owner binding")
        if _positive_int(item["snapshot_id"], f"{hop_field}.snapshot_id") != snapshot_id:
            raise P5TraceEvidenceError(f"{hop_field}.snapshot_id breaks the snapshot binding")
        if (
            _compartments(item["required_compartments"], f"{hop_field}.required_compartments")
            != compartments
        ):
            raise P5TraceEvidenceError(
                f"{hop_field}.required_compartments breaks compartment propagation"
            )
        for identity in ("sample_id", "workflow_id", "chain_id"):
            actual = _identifier(item[identity], f"{hop_field}.{identity}")
            if actual != sample[identity]:
                raise P5TraceEvidenceError(
                    f"{hop_field}.{identity} breaks the sample chain binding"
                )
        by_hop[hop] = item
    if set(by_hop) != _HOP_SET:
        raise P5TraceEvidenceError(f"{field}.hops is missing a required hop")

    normalized_hops = [
        {
            "row_id": row_ids[hop],
            "hop": hop,
            "classification": classifications[hop].to_storage(),
            "owner_id": owner_id,
            "required_compartments": list(compartments),
            "snapshot_id": snapshot_id,
            "sample_id": sample_id,
            "workflow_id": workflow_id,
            "chain_id": chain_id,
        }
        for hop in P5_TRACE_HOPS
    ]
    normalized = {
        "sample_id": sample_id,
        "workflow_id": workflow_id,
        "chain_id": chain_id,
        "owner_id": owner_id,
        "snapshot_id": snapshot_id,
        "required_compartments": list(compartments),
        "classification_path": {hop: classifications[hop].to_storage() for hop in P5_TRACE_HOPS},
        "row_ids": {hop: row_ids[hop] for hop in P5_TRACE_HOPS},
        "hops": normalized_hops,
    }
    return _VerifiedSample(sample_id, workflow_id, chain_id, normalized)


@dataclass(frozen=True, slots=True)
class VerifiedP5TraceEvidence:
    """Deterministic non-acceptance result for shape-consistent P5 evidence."""

    evidence_id: str
    evidence_sha256: str
    samples: tuple[tuple[str, str, str], ...]

    @property
    def status(self) -> str:
        return P5_TRACE_EVIDENCE_STATUS

    @property
    def acceptance_status(self) -> str:
        return P5_TRACE_ACCEPTANCE_STATUS

    @property
    def gate6_pass(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": P5_TRACE_VERIFICATION_SCHEMA,
            "status": P5_TRACE_EVIDENCE_STATUS,
            "acceptance_status": P5_TRACE_ACCEPTANCE_STATUS,
            "gate6_pass": False,
            "production_approval": False,
            "evidence_class": "non-acceptance-p5-trace-shape-evidence",
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "sample_count": len(self.samples),
            "samples": [
                {
                    "sample_id": sample_id,
                    "workflow_id": workflow_id,
                    "chain_id": chain_id,
                }
                for sample_id, workflow_id, chain_id in self.samples
            ],
        }


def _verify_p5_trace_evidence(value: Any) -> VerifiedP5TraceEvidence:
    """Validate exact six-hop evidence without making an acceptance decision."""

    _check_value_limits(value)
    root = _exact(value, _ROOT_FIELDS, "evidence")
    if root["schema_version"] != P5_TRACE_EVIDENCE_SCHEMA:
        raise P5TraceEvidenceError("unknown P5 trace evidence schema")
    evidence_id = _identifier(root["evidence_id"], "evidence.evidence_id")
    raw_samples = root["samples"]
    if (
        not isinstance(raw_samples, list)
        or not raw_samples
        or len(raw_samples) > P5_TRACE_MAX_SAMPLES
    ):
        raise P5TraceEvidenceError(
            f"evidence.samples must contain between 1 and {P5_TRACE_MAX_SAMPLES} samples"
        )
    verified = [_verify_sample(sample, index) for index, sample in enumerate(raw_samples)]
    sample_ids = [sample.sample_id for sample in verified]
    chain_ids = [sample.chain_id for sample in verified]
    if len(sample_ids) != len(set(sample_ids)):
        raise P5TraceEvidenceError("evidence contains duplicate sample_id")
    if len(chain_ids) != len(set(chain_ids)):
        raise P5TraceEvidenceError("evidence contains duplicate chain_id")

    seen_rows: set[tuple[str, str]] = set()
    for sample in verified:
        row_ids = sample.normalized["row_ids"]
        if not isinstance(row_ids, Mapping):  # defensive; constructed above
            raise P5TraceEvidenceError("normalized row binding is invalid")
        for hop in P5_TRACE_HOPS:
            row_key = (hop, str(row_ids[hop]))
            if row_key in seen_rows:
                raise P5TraceEvidenceError(
                    f"evidence reuses {hop} row identity {row_key[1]} across samples"
                )
            seen_rows.add(row_key)

    ordered = sorted(verified, key=lambda sample: sample.sample_id)
    normalized_evidence = {
        "schema_version": P5_TRACE_EVIDENCE_SCHEMA,
        "evidence_id": evidence_id,
        "samples": [sample.normalized for sample in ordered],
    }
    evidence_sha256 = hashlib.sha256(canonical_json(normalized_evidence)).hexdigest()
    return VerifiedP5TraceEvidence(
        evidence_id=evidence_id,
        evidence_sha256=evidence_sha256,
        samples=tuple(
            (sample.sample_id, sample.workflow_id, sample.chain_id) for sample in ordered
        ),
    )


def verify_p5_trace_evidence(value: Any) -> VerifiedP5TraceEvidence:
    """Validate exact six-hop evidence without making an acceptance decision."""

    try:
        return _verify_p5_trace_evidence(value)
    except _P5TraceResourceLimitError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_RESOURCE_LIMIT) from None
    except P5TraceEvidenceError:
        raise P5TraceEvidenceError(P5_TRACE_ERROR_INVALID_EVIDENCE) from None
    except (UnicodeError, RecursionError, MemoryError):
        raise P5TraceEvidenceError(P5_TRACE_ERROR_RESOURCE_LIMIT) from None


def verify_p5_trace_evidence_file(path: str | Path) -> VerifiedP5TraceEvidence:
    """Read and verify one P5 evidence file without mutating it."""

    return verify_p5_trace_evidence(read_p5_trace_evidence(path))


__all__ = [
    "P5_TRACE_ACCEPTANCE_STATUS",
    "P5_TRACE_ERROR_DUPLICATE_JSON_KEY",
    "P5_TRACE_ERROR_INVALID_EVIDENCE",
    "P5_TRACE_ERROR_INVALID_JSON",
    "P5_TRACE_ERROR_RESOURCE_LIMIT",
    "P5_TRACE_EVIDENCE_SCHEMA",
    "P5_TRACE_EVIDENCE_STATUS",
    "P5_TRACE_HOPS",
    "P5_TRACE_MAX_COMPARTMENTS",
    "P5_TRACE_MAX_CONTAINER_ITEMS",
    "P5_TRACE_MAX_EVIDENCE_BYTES",
    "P5_TRACE_MAX_INTEGER",
    "P5_TRACE_MIN_INTEGER",
    "P5_TRACE_MAX_JSON_DEPTH",
    "P5_TRACE_MAX_SAMPLES",
    "P5_TRACE_MAX_STRING_LENGTH",
    "P5_TRACE_MAX_TOTAL_NODES",
    "P5_TRACE_MAX_TOTAL_STRING_BYTES",
    "P5_TRACE_VERIFICATION_SCHEMA",
    "P5TraceEvidenceError",
    "VerifiedP5TraceEvidence",
    "read_p5_trace_evidence",
    "verify_p5_trace_evidence",
    "verify_p5_trace_evidence_file",
]
