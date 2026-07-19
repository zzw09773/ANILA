from __future__ import annotations

import copy
import json
import traceback
from pathlib import Path

import pytest

from anila_security.p5_trace_evidence import (
    P5_TRACE_ERROR_DUPLICATE_JSON_KEY,
    P5_TRACE_ERROR_INVALID_EVIDENCE,
    P5_TRACE_ERROR_INVALID_JSON,
    P5_TRACE_ERROR_RESOURCE_LIMIT,
    P5_TRACE_EVIDENCE_SCHEMA,
    P5_TRACE_EVIDENCE_STATUS,
    P5_TRACE_HOPS,
    P5_TRACE_MAX_COMPARTMENTS,
    P5_TRACE_MAX_CONTAINER_ITEMS,
    P5_TRACE_MAX_EVIDENCE_BYTES,
    P5_TRACE_MAX_INTEGER,
    P5_TRACE_MAX_JSON_DEPTH,
    P5_TRACE_MAX_SAMPLES,
    P5_TRACE_MAX_STRING_LENGTH,
    P5_TRACE_MAX_TOTAL_NODES,
    P5_TRACE_MAX_TOTAL_STRING_BYTES,
    P5_TRACE_MIN_INTEGER,
    P5TraceEvidenceError,
    read_p5_trace_evidence,
    verify_p5_trace_evidence,
    verify_p5_trace_evidence_file,
)


LEVELS = (
    "無機密",
    "營業秘密",
    "營業秘密",
    "機密",
    "機密",
    "機密",
)


def _sample(suffix: str, *, owner_id: int = 7, snapshot_id: int = 11) -> dict:
    sample_id = f"sample-{suffix}"
    workflow_id = f"workflow-{suffix}"
    chain_id = f"chain-{suffix}"
    compartments = ["ALPHA", "PROJECT_X"]
    classification_path = dict(zip(P5_TRACE_HOPS, LEVELS, strict=True))
    row_ids = {hop: f"{hop}-row-{suffix}" for hop in P5_TRACE_HOPS}
    return {
        "sample_id": sample_id,
        "workflow_id": workflow_id,
        "chain_id": chain_id,
        "owner_id": owner_id,
        "snapshot_id": snapshot_id,
        "required_compartments": compartments,
        "classification_path": classification_path,
        "row_ids": row_ids,
        "hops": [
            {
                "row_id": row_ids[hop],
                "hop": hop,
                "classification": classification_path[hop],
                "owner_id": owner_id,
                "required_compartments": compartments.copy(),
                "snapshot_id": snapshot_id,
                "sample_id": sample_id,
                "workflow_id": workflow_id,
                "chain_id": chain_id,
            }
            for hop in P5_TRACE_HOPS
        ],
    }


def _evidence(*samples: dict) -> dict:
    return {
        "schema_version": P5_TRACE_EVIDENCE_SCHEMA,
        "evidence_id": "p5.synthetic.trace-shape",
        "samples": list(samples or (_sample("one"),)),
    }


def _hop(sample: dict, hop: str) -> dict:
    return next(item for item in sample["hops"] if item["hop"] == hop)


def test_valid_six_hop_evidence_is_deterministic_non_acceptance() -> None:
    evidence = _evidence(_sample("b", owner_id=8, snapshot_id=12), _sample("a"))
    verified = verify_p5_trace_evidence(evidence)
    report = verified.as_dict()

    assert verified.status == P5_TRACE_EVIDENCE_STATUS
    assert verified.acceptance_status == "NOT_ACCEPTANCE"
    assert verified.gate6_pass is False
    assert report["gate6_pass"] is False
    assert report["production_approval"] is False
    assert report["sample_count"] == 2
    assert [row["sample_id"] for row in report["samples"]] == ["sample-a", "sample-b"]

    reordered = copy.deepcopy(evidence)
    reordered["samples"].reverse()
    for sample in reordered["samples"]:
        sample["hops"].reverse()
    assert verify_p5_trace_evidence(reordered).as_dict() == report


@pytest.mark.parametrize(
    ("_name", "mutate"),
    (
        ("wrong-version", lambda value: value.update(schema_version="wrong")),
        ("root-extra", lambda value: value.update(extra=True)),
        ("empty-samples", lambda value: value.update(samples=[])),
        ("sample-extra", lambda value: value["samples"][0].update(extra=True)),
        ("hop-extra", lambda value: value["samples"][0]["hops"][0].update(extra=True)),
        (
            "unknown-classification",
            lambda value: value["samples"][0]["classification_path"].update(trace="未知"),
        ),
        (
            "unsorted-compartments",
            lambda value: value["samples"][0].update(required_compartments=["PROJECT_X", "ALPHA"]),
        ),
        (
            "duplicate-compartments",
            lambda value: value["samples"][0].update(required_compartments=["ALPHA", "ALPHA"]),
        ),
        ("empty-owner", lambda value: value["samples"][0].update(owner_id=0)),
        ("empty-snapshot", lambda value: value["samples"][0].update(snapshot_id=0)),
    ),
)
def test_exact_schema_and_canonical_values_fail_closed(_name: str, mutate) -> None:
    evidence = _evidence()
    mutate(evidence)
    with pytest.raises(P5TraceEvidenceError):
        verify_p5_trace_evidence(evidence)


def test_duplicate_missing_and_extra_hops_are_rejected() -> None:
    mutations = {
        "missing": lambda sample: sample["hops"].pop(),
        "duplicate": lambda sample: sample["hops"].__setitem__(
            -1, copy.deepcopy(sample["hops"][0])
        ),
        "extra": lambda sample: sample["hops"].append(copy.deepcopy(sample["hops"][0])),
        "unknown": lambda sample: sample["hops"][0].update(hop="unknown"),
    }
    for _name, mutate in mutations.items():
        evidence = _evidence()
        mutate(evidence["samples"][0])
        with pytest.raises(P5TraceEvidenceError):
            verify_p5_trace_evidence(evidence)


def test_each_hop_field_is_bound_to_the_declared_sample_chain() -> None:
    mutations = {
        "classification": lambda row: row.update(classification="絕對機密"),
        "compartments": lambda row: row.update(required_compartments=["ALPHA"]),
        "owner": lambda row: row.update(owner_id=999),
        "snapshot": lambda row: row.update(snapshot_id=999),
        "row": lambda row: row.update(row_id="different-row"),
        "sample": lambda row: row.update(sample_id="sample-other"),
        "workflow": lambda row: row.update(workflow_id="workflow-other"),
        "chain": lambda row: row.update(chain_id="chain-other"),
    }
    for hop in P5_TRACE_HOPS:
        for _field, mutate in mutations.items():
            evidence = _evidence()
            mutate(_hop(evidence["samples"][0], hop))
            with pytest.raises(P5TraceEvidenceError):
                verify_p5_trace_evidence(evidence)


def test_declared_classification_path_may_rise_but_never_fall() -> None:
    evidence = _evidence()
    sample = evidence["samples"][0]
    sample["classification_path"]["trace"] = "無機密"
    _hop(sample, "trace")["classification"] = "無機密"

    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(evidence)


def test_cross_sample_binding_and_row_reuse_are_rejected() -> None:
    first = _sample("first", owner_id=7, snapshot_id=11)
    second = _sample("second", owner_id=8, snapshot_id=12)

    mixed = _evidence(copy.deepcopy(first), copy.deepcopy(second))
    mixed["samples"][1]["hops"][0] = copy.deepcopy(mixed["samples"][0]["hops"][0])
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(mixed)

    reused = _evidence(copy.deepcopy(first), copy.deepcopy(second))
    reused_id = reused["samples"][0]["row_ids"]["upload"]
    reused["samples"][1]["row_ids"]["upload"] = reused_id
    _hop(reused["samples"][1], "upload")["row_id"] = reused_id
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(reused)

    duplicate_sample = _evidence(copy.deepcopy(first), copy.deepcopy(second))
    duplicate_sample["samples"][1]["sample_id"] = first["sample_id"]
    for row in duplicate_sample["samples"][1]["hops"]:
        row["sample_id"] = first["sample_id"]
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(duplicate_sample)


def test_json_file_entrypoint_rejects_duplicate_keys_and_bom(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    attacker_marker = "attacker\nmarker"
    duplicate.write_text('{"attacker\\nmarker":"one","attacker\\nmarker":"two"}', encoding="utf-8")
    with pytest.raises(
        P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_DUPLICATE_JSON_KEY}$"
    ) as duplicate_error:
        read_p5_trace_evidence(duplicate)
    assert attacker_marker not in str(duplicate_error.value)

    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_JSON}$"):
        verify_p5_trace_evidence_file(bom)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_evidence(), ensure_ascii=False), encoding="utf-8")
    assert verify_p5_trace_evidence_file(valid).gate6_pass is False


def test_json_file_entrypoint_rejects_nonstandard_numeric_constants(
    tmp_path: Path,
) -> None:
    encoded = json.dumps(_evidence(), ensure_ascii=False)
    for token in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / f"constant-{token.replace('-', 'negative-')}.json"
        path.write_text(encoded.replace('"owner_id": 7', f'"owner_id": {token}', 1))
        with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_JSON}$") as error:
            read_p5_trace_evidence(path)
        assert error.value.__cause__ is None
        rendered = "".join(
            traceback.format_exception(type(error.value), error.value, error.value.__traceback__)
        )
        assert token not in rendered
        assert str(path) not in rendered


def test_file_size_boundary_passes_and_oversize_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = json.dumps(_evidence(), ensure_ascii=False).encode("utf-8")
    at_limit = tmp_path / "at-limit.json"
    at_limit.write_bytes(encoded + b" " * (P5_TRACE_MAX_EVIDENCE_BYTES - len(encoded)))
    assert at_limit.stat().st_size == P5_TRACE_MAX_EVIDENCE_BYTES
    assert verify_p5_trace_evidence_file(at_limit).acceptance_status == "NOT_ACCEPTANCE"

    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (P5_TRACE_MAX_EVIDENCE_BYTES + 1))
    opened = False

    def fail_if_opened(_path: Path, *_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("oversize evidence must be rejected before opening")

    monkeypatch.setattr(Path, "open", fail_if_opened)
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        read_p5_trace_evidence(oversize)
    assert opened is False


def test_deep_unknown_nesting_fails_safely_for_file_and_direct_inputs(tmp_path: Path) -> None:
    deep_file = tmp_path / "deep.json"
    deep_file.write_text('{"unknown":' + "[" * 1_200 + "0" + "]" * 1_200 + "}")
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        read_p5_trace_evidence(deep_file)

    nested: object = 0
    for _ in range(1_200):
        nested = [nested]
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence({"unknown": nested})


def test_json_depth_exact_boundary_passes_and_plus_one_fails(tmp_path: Path) -> None:
    at_limit = tmp_path / "depth-at-limit.json"
    array_depth = P5_TRACE_MAX_JSON_DEPTH - 1  # root object consumes the first level
    at_limit.write_text(
        '{"unknown":' + "[" * array_depth + "0" + "]" * array_depth + "}",
        encoding="ascii",
    )
    assert read_p5_trace_evidence(at_limit)["unknown"] is not None

    over_limit = tmp_path / "depth-over-limit.json"
    over_limit.write_text(
        '{"unknown":' + "[" * (array_depth + 1) + "0" + "]" * (array_depth + 1) + "}",
        encoding="ascii",
    )
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        read_p5_trace_evidence(over_limit)


def test_sample_count_boundary_passes_and_plus_one_fails_closed() -> None:
    at_limit = _evidence(*(_sample(str(index)) for index in range(P5_TRACE_MAX_SAMPLES)))
    assert verify_p5_trace_evidence(at_limit).as_dict()["sample_count"] == P5_TRACE_MAX_SAMPLES

    over_limit = _evidence(*(_sample(str(index)) for index in range(P5_TRACE_MAX_SAMPLES + 1)))
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(over_limit)


def test_compartment_count_boundary_passes_and_plus_one_fails_closed() -> None:
    sample = _sample("compartments")
    at_limit = [f"C{index:03d}" for index in range(P5_TRACE_MAX_COMPARTMENTS)]
    sample["required_compartments"] = at_limit
    for row in sample["hops"]:
        row["required_compartments"] = at_limit.copy()
    assert verify_p5_trace_evidence(_evidence(sample)).gate6_pass is False

    over_limit = at_limit + ["C999"]
    sample["required_compartments"] = over_limit
    for row in sample["hops"]:
        row["required_compartments"] = over_limit.copy()
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(_evidence(sample))


def test_invalid_unicode_and_oversize_integers_use_domain_error(tmp_path: Path) -> None:
    invalid_unicode = _evidence()
    invalid_unicode["unknown"] = "\ud800"
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence(invalid_unicode)

    escaped_surrogate = tmp_path / "surrogate.json"
    escaped_surrogate.write_text('{"unknown":"\\ud800"}', encoding="ascii")
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        read_p5_trace_evidence(escaped_surrogate)

    oversize_owner = _evidence()
    oversize_owner["samples"][0]["owner_id"] = P5_TRACE_MAX_INTEGER + 1
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(oversize_owner)

    huge_integer = tmp_path / "huge-integer.json"
    huge_integer.write_text('{"unknown":' + "9" * 5_000 + "}", encoding="ascii")
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_JSON}$"):
        read_p5_trace_evidence(huge_integer)


def test_json_decode_errors_return_exact_codes_without_attacker_content_or_paths(
    tmp_path: Path,
) -> None:
    invalid_utf8 = tmp_path / "secret-attacker-path.json"
    invalid_utf8.write_bytes(b'{"marker":"ATTACKER-UTF8-\xff"}')
    with pytest.raises(
        P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_JSON}$"
    ) as utf8_error:
        read_p5_trace_evidence(invalid_utf8)
    assert "ATTACKER" not in str(utf8_error.value)
    assert str(invalid_utf8) not in str(utf8_error.value)

    malformed = tmp_path / "malformed-attacker.json"
    malformed.write_text('{"ATTACKER-MALFORMED":', encoding="ascii")
    with pytest.raises(
        P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_JSON}$"
    ) as malformed_error:
        read_p5_trace_evidence(malformed)
    assert "ATTACKER" not in str(malformed_error.value)
    assert str(malformed) not in str(malformed_error.value)


def test_public_file_errors_suppress_sensitive_exception_chains(tmp_path: Path) -> None:
    missing = tmp_path / "missing-secret-attacker-path.json"
    invalid_utf8 = tmp_path / "invalid-utf8-secret-attacker-path.json"
    invalid_utf8.write_bytes(b'{"marker":"ATTACKER-UTF8-\xff"}')
    malformed = tmp_path / "malformed-secret-attacker-path.json"
    malformed.write_text('{"ATTACKER-MALFORMED":', encoding="ascii")
    duplicate = tmp_path / "duplicate-secret-attacker-path.json"
    duplicate.write_text(
        '{"ATTACKER\\nDUPLICATE":"one","ATTACKER\\nDUPLICATE":"two"}',
        encoding="utf-8",
    )
    cases = (
        (missing, P5_TRACE_ERROR_INVALID_JSON, (str(missing), "No such file")),
        (
            invalid_utf8,
            P5_TRACE_ERROR_INVALID_JSON,
            (str(invalid_utf8), "ATTACKER-UTF8", "\\xff", "UnicodeDecodeError"),
        ),
        (
            malformed,
            P5_TRACE_ERROR_INVALID_JSON,
            (str(malformed), "ATTACKER-MALFORMED", "JSONDecodeError", "Expecting"),
        ),
        (
            duplicate,
            P5_TRACE_ERROR_DUPLICATE_JSON_KEY,
            (str(duplicate), "ATTACKER\nDUPLICATE", "_P5TraceDuplicateKeyError"),
        ),
    )

    for path, expected_code, forbidden in cases:
        with pytest.raises(P5TraceEvidenceError, match=f"^{expected_code}$") as error:
            read_p5_trace_evidence(path)
        assert error.value.__cause__ is None
        rendered = "".join(
            traceback.format_exception(type(error.value), error.value, error.value.__traceback__)
        )
        for marker in forbidden:
            assert marker not in rendered


def _value_with_exact_node_count(target: int) -> dict:
    # root mapping + key + outer list consume three nodes.  Each bounded inner
    # list then consumes one node plus its scalar children.
    remaining = target - 3
    outer: list[list[None]] = []
    while remaining:
        child_count = min(P5_TRACE_MAX_CONTAINER_ITEMS, max(remaining - 1, 0))
        outer.append([None] * child_count)
        remaining -= child_count + 1
    return {"unknown": outer}


def _value_with_exact_utf8_string_bytes(target: int) -> dict:
    key = "unknown"
    remaining = target - len(key.encode("utf-8"))
    values: list[str] = []
    while remaining:
        size = min(P5_TRACE_MAX_STRING_LENGTH, remaining)
        values.append("a" * size)
        remaining -= size
    return {key: values}


def test_node_count_exact_boundary_passes_resource_check_and_plus_one_fails() -> None:
    at_limit = _value_with_exact_node_count(P5_TRACE_MAX_TOTAL_NODES)
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(at_limit)

    over_limit = _value_with_exact_node_count(P5_TRACE_MAX_TOTAL_NODES + 1)
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence(over_limit)


def test_container_and_string_exact_boundaries_pass_then_plus_one_fails() -> None:
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence({"unknown": [None] * P5_TRACE_MAX_CONTAINER_ITEMS})
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence({"unknown": [None] * (P5_TRACE_MAX_CONTAINER_ITEMS + 1)})

    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence({"unknown": "a" * P5_TRACE_MAX_STRING_LENGTH})
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence({"unknown": "a" * (P5_TRACE_MAX_STRING_LENGTH + 1)})


def test_cumulative_utf8_bytes_exact_boundary_passes_then_plus_one_fails() -> None:
    at_limit = _value_with_exact_utf8_string_bytes(P5_TRACE_MAX_TOTAL_STRING_BYTES)
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(at_limit)

    over_limit = copy.deepcopy(at_limit)
    over_limit["unknown"][-1] += "a"
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_RESOURCE_LIMIT}$"):
        verify_p5_trace_evidence(over_limit)


@pytest.mark.parametrize("row_id", (P5_TRACE_MIN_INTEGER, P5_TRACE_MAX_INTEGER))
def test_signed_64_bit_row_id_boundaries_are_accepted(row_id: int) -> None:
    evidence = _evidence()
    evidence["samples"][0]["row_ids"]["upload"] = row_id
    _hop(evidence["samples"][0], "upload")["row_id"] = row_id
    assert verify_p5_trace_evidence(evidence).gate6_pass is False


@pytest.mark.parametrize("row_id", (P5_TRACE_MIN_INTEGER - 1, P5_TRACE_MAX_INTEGER + 1, True))
def test_out_of_range_and_boolean_row_ids_fail_closed(row_id: int | bool) -> None:
    evidence = _evidence()
    evidence["samples"][0]["row_ids"]["upload"] = row_id
    _hop(evidence["samples"][0], "upload")["row_id"] = row_id
    with pytest.raises(P5TraceEvidenceError, match=f"^{P5_TRACE_ERROR_INVALID_EVIDENCE}$"):
        verify_p5_trace_evidence(evidence)
