#!/usr/bin/env python3
"""Verify and replay captured provider output through the formal R3 runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from infra.ci import run_gate5_routing_contract as runner
from infra.ci.check_gate5_routing_eval import DEFAULT_DATASET_PATH
from infra.ci.gate5_r3_eval_adapter import FormalR3EvalAdapter
from infra.ci.gate5_routing_provider_capture import (
    DEFAULT_FIXTURE_PATH,
    EXPECTED_PROVIDER_MODEL_ID,
    FIXTURE_SCHEMA_VERSION,
    PROMPT_SHA256,
    PROMPT_VERSION,
    canonical_json_bytes,
    load_projected_requests,
    request_sha256,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "dataset",
        "model_id",
        "prompt_version",
        "prompt_sha256",
        "captured_at",
        "outputs",
        "fixture_sha256",
    }
)
_DATASET_FIELDS = frozenset(
    {"dataset_id", "dataset_version", "dataset_sha256", "case_count"}
)
_OUTPUT_FIELDS = frozenset(
    {
        "request_sha256",
        "provider_output",
        "response_model_id",
        "output_sha256",
        "finish_reason",
        "usage",
        "elapsed_ms",
    }
)
_USAGE_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens"})


class RecordedProviderError(ValueError):
    """Raised when a recorded-provider fixture cannot be trusted."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecordedProviderError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_object(
    value: object, *, fields: frozenset[str], path: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordedProviderError(f"{path} must be an object")
    actual = set(value)
    if actual != fields:
        raise RecordedProviderError(f"{path} fields do not match the fixture contract")
    return value


def _sha256(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RecordedProviderError(f"{path} must be a lowercase SHA-256 digest")
    return value


def verify_fixture_document(
    document: object,
    *,
    dataset_identity: Mapping[str, Any],
    requests: Sequence[runner.RoutingRequest],
) -> dict[str, str]:
    root = _exact_object(document, fields=_ROOT_FIELDS, path="fixture")
    if root["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise RecordedProviderError("fixture schema version mismatch")
    dataset = _exact_object(
        root["dataset"], fields=_DATASET_FIELDS, path="fixture.dataset"
    )
    if dict(dataset) != dict(dataset_identity):
        raise RecordedProviderError("fixture dataset identity mismatch")
    if root["model_id"] != EXPECTED_PROVIDER_MODEL_ID:
        raise RecordedProviderError(
            "fixture model id does not match the approved provider model"
        )
    if (
        root["prompt_version"] != PROMPT_VERSION
        or root["prompt_sha256"] != PROMPT_SHA256
    ):
        raise RecordedProviderError("fixture prompt identity mismatch")
    if not isinstance(root["captured_at"], str):
        raise RecordedProviderError("fixture capture time is invalid")
    try:
        captured_at = datetime.fromisoformat(root["captured_at"])
    except ValueError as exc:
        raise RecordedProviderError("fixture capture time is invalid") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RecordedProviderError("fixture capture time must include a timezone")

    stored_digest = _sha256(root["fixture_sha256"], path="fixture.fixture_sha256")
    unsigned = dict(root)
    unsigned.pop("fixture_sha256")
    calculated_digest = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if stored_digest != calculated_digest:
        raise RecordedProviderError("fixture canonical digest mismatch")

    outputs = root["outputs"]
    if not isinstance(outputs, list):
        raise RecordedProviderError("fixture outputs must be an array")
    if len(outputs) != len(requests) or len(outputs) != dataset_identity["case_count"]:
        raise RecordedProviderError("fixture output coverage is incomplete")
    indexed: dict[str, str] = {}
    for index, value in enumerate(outputs):
        output = _exact_object(
            value, fields=_OUTPUT_FIELDS, path=f"fixture.outputs[{index}]"
        )
        fingerprint = _sha256(
            output["request_sha256"],
            path=f"fixture.outputs[{index}].request_sha256",
        )
        if fingerprint in indexed:
            raise RecordedProviderError(
                "fixture contains a duplicate request fingerprint"
            )
        response_model_id = output["response_model_id"]
        if (
            not isinstance(response_model_id, str)
            or not response_model_id
            or response_model_id.strip() != response_model_id
            or response_model_id != EXPECTED_PROVIDER_MODEL_ID
        ):
            raise RecordedProviderError(
                "fixture response model does not match the requested model"
            )
        raw = output["provider_output"]
        if not isinstance(raw, str):
            raise RecordedProviderError("fixture provider output must be a string")
        if len(raw.encode("utf-8")) > 1_048_576:
            raise RecordedProviderError("fixture provider output exceeds 1 MiB")
        finish_reason = output["finish_reason"]
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            raise RecordedProviderError(
                "fixture output finish_reason must be a non-empty string"
            )
        if finish_reason != "stop":
            raise RecordedProviderError("fixture output finish_reason must be stop")
        usage = output["usage"]
        if not isinstance(usage, Mapping) or set(usage) != _USAGE_FIELDS:
            raise RecordedProviderError(
                "fixture output usage fields do not match the schema"
            )
        for field_name in _USAGE_FIELDS:
            value = usage[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RecordedProviderError(
                    f"fixture output usage.{field_name} must be a non-negative integer"
                )
        elapsed_ms = output["elapsed_ms"]
        if (
            isinstance(elapsed_ms, bool)
            or not isinstance(elapsed_ms, int)
            or elapsed_ms < 0
        ):
            raise RecordedProviderError(
                "fixture output elapsed_ms must be a non-negative integer"
            )
        output_digest = _sha256(
            output["output_sha256"],
            path=f"fixture.outputs[{index}].output_sha256",
        )
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != output_digest:
            raise RecordedProviderError("fixture provider output digest mismatch")
        indexed[fingerprint] = raw

    expected = {request_sha256(request) for request in requests}
    if len(expected) != len(requests):
        raise RecordedProviderError("verified dataset has duplicate visible requests")
    if set(indexed) != expected:
        raise RecordedProviderError(
            "fixture request coverage does not match the dataset"
        )
    return indexed


def load_fixture_file(
    fixture_path: Path,
    *,
    dataset_identity: Mapping[str, Any],
    requests: Sequence[runner.RoutingRequest],
) -> dict[str, str]:
    try:
        raw = fixture_path.read_bytes()
    except OSError as exc:
        raise RecordedProviderError(
            f"cannot read provider fixture {fixture_path}"
        ) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RecordedProviderError("provider fixture must not contain a UTF-8 BOM")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordedProviderError(
            "provider fixture is not canonical UTF-8 JSON"
        ) from exc
    return verify_fixture_document(
        document, dataset_identity=dataset_identity, requests=requests
    )


class RecordedProviderAdapter:
    """Lookup raw output by request fingerprint and run the production pipeline."""

    def __init__(
        self,
        fixture_path: Path = DEFAULT_FIXTURE_PATH,
        *,
        dataset_path: Path = DEFAULT_DATASET_PATH,
    ) -> None:
        identity, requests = load_projected_requests(dataset_path)
        self._outputs = load_fixture_file(
            fixture_path, dataset_identity=identity, requests=requests
        )
        self.formal = FormalR3EvalAdapter()

    def evaluate(self, request: runner.RoutingRequest) -> Mapping[str, Any]:
        fingerprint = request_sha256(request)
        try:
            raw_output = self._outputs[fingerprint]
        except KeyError as exc:
            raise RecordedProviderError(
                "recorded provider output is missing for request fingerprint"
            ) from exc
        return self.formal.evaluate_provider_output(request, raw_output)


def build_adapter() -> RecordedProviderAdapter:
    fixture = Path(
        os.environ.get("ANILA_GATE5_ROUTING_PROVIDER_FIXTURE", DEFAULT_FIXTURE_PATH)
    )
    return RecordedProviderAdapter(fixture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    args = parser.parse_args(argv)
    try:
        identity, requests = load_projected_requests(args.dataset)
        outputs = load_fixture_file(
            args.fixture, dataset_identity=identity, requests=requests
        )
    except RecordedProviderError as exc:
        print(f"recorded provider fixture failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                **identity,
                "expected_provider_model_id": EXPECTED_PROVIDER_MODEL_ID,
                "fixture": str(args.fixture),
                "verified_output_count": len(outputs),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RecordedProviderAdapter",
    "RecordedProviderError",
    "EXPECTED_PROVIDER_MODEL_ID",
    "build_adapter",
    "load_fixture_file",
    "verify_fixture_document",
]
