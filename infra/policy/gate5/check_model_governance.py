#!/usr/bin/env python3
"""Fail-closed static verifier for Gate 5 production model governance.

This verifier is intentionally an authority *foundation*, not runtime
admission.  It binds a signed profile to a source inventory, immutable model
artifacts and deployments, and a single CSP model gateway.  Runtime egress,
admission, health probes and usage reconciliation remain follow-up work.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import tokenize
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from anila_security.model_governance import (
    GATEWAY_ID as AUTHORITY_GATEWAY_ID,
    InferenceCallsite,
    ModelGovernanceError,
    VerifiedModelGovernanceAuthority,
    inventory_content_sha256,
    validate_inventory_payload,
)

GATEWAY_ID = AUTHORITY_GATEWAY_ID
_ENDPOINT_RE = re.compile(
    r"/(?:v[12]/)?(?:chat/completions|embeddings|images/generations)\b"
)
_KNOWN_GATEWAY_CALLS = {
    "_call_llm_chat",
    "call_llm_chat",
    "proxy_chat_completions",
    "embed_query",
    "proxy_request",
    "proxy_stream",
    "proxy_model_request",
    "score_one",
    "_call_llm",
}
_SDK_CALLS = {
    "AsyncOpenAI",
    "OpenAIChatCompletionsModel",
    "OpenAIEmbeddings",
}
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "examples",
    "example",
    "templates",
    "test",
    "tests",
}
_SOURCE_ROOTS = ("packages", "services", "infra/models/src")
# Python 3.12 (PEP 701) exposes f-string literal segments as dedicated
# token kinds instead of ``tokenize.STRING``.  Keep the scanner's endpoint
# detection semantic, while remaining compatible with Python 3.11 where
# those names do not exist.
_STRING_TOKEN_TYPES = {
    tokenize.STRING,
    *(getattr(tokenize, name) for name in (
        "FSTRING_START",
        "FSTRING_MIDDLE",
        "FSTRING_END",
    ) if hasattr(tokenize, name)),
}


class ModelGovernancePolicyError(RuntimeError):
    """Raised when static model-governance authority is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class SourceFinding:
    source: str
    sink_kinds: tuple[str, ...]


class _CodeCallVisitor(ast.NodeVisitor):
    """Collect calls while ignoring route decorators and docstrings."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    @staticmethod
    def _tail(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        for item in node.body:
            self.visit(item)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        for item in node.body:
            self.visit(item)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for item in node.body:
            self.visit(item)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = self._tail(node.func)
        if name:
            self.names.add(name)
        self.generic_visit(node)


def _python_code_tokens(source: str) -> list[tokenize.TokenInfo]:
    try:
        return [
            token for token in tokenize.generate_tokens(io.StringIO(source).readline)
        ]
    except (IndentationError, tokenize.TokenError):
        return []


def _has_code_endpoint(tokens: list[tokenize.TokenInfo]) -> bool:
    return any(
        token.type in _STRING_TOKEN_TYPES and _ENDPOINT_RE.search(token.string)
        for token in tokens
    )


def _finding_for_file(path: Path, repo_root: Path) -> SourceFinding | None:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        # A source file that cannot be scanned is unsafe to treat as absent.
        raise ModelGovernancePolicyError(f"cannot parse source inventory file: {path}")
    visitor = _CodeCallVisitor()
    visitor.visit(tree)
    calls = visitor.names
    tokens = _python_code_tokens(source)
    sink_kinds: set[str] = set()
    if _has_code_endpoint(tokens) and calls.intersection({"post", "stream", "request"}):
        for kind, marker in (
            ("chat_completions", "chat/completions"),
            ("embeddings", "embeddings"),
            ("images", "images/generations"),
        ):
            if any(
                token.type in _STRING_TOKEN_TYPES and marker in token.string
                for token in tokens
            ):
                sink_kinds.add(kind)
    if calls.intersection(_SDK_CALLS):
        sink_kinds.add("model_sdk")
    if calls.intersection(_KNOWN_GATEWAY_CALLS):
        sink_kinds.add("gateway_call")
    # Local model servers and model backends are sinks even without an HTTP
    # path: they execute weights directly or proxy into Triton.
    if "Flux2Pipeline" in source or ("AutoModel" in source and ".encode(" in source):
        sink_kinds.add("local_model_runtime")
    if "infer" in calls and "TRITON_URL" in source:
        sink_kinds.add("triton_infer")
    if not sink_kinds:
        return None
    relative = path.relative_to(repo_root).as_posix()
    return SourceFinding(relative, tuple(sorted(sink_kinds)))


def scan_inference_sources(repo_root: Path) -> tuple[SourceFinding, ...]:
    """Find source-level model/inference sinks without importing services."""

    findings: list[SourceFinding] = []
    for root_name in _SOURCE_ROOTS:
        root = repo_root / Path(root_name)
        if not root.exists():
            raise ModelGovernancePolicyError(
                f"scanner source root missing: {root_name}"
            )
        for path in sorted(root.rglob("*.py")):
            if any(part in _EXCLUDED_PARTS for part in path.parts):
                continue
            finding = _finding_for_file(path, repo_root)
            if finding is not None:
                findings.append(finding)
    return tuple(findings)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ModelGovernancePolicyError(f"cannot read {path}: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ModelGovernancePolicyError(f"UTF-8 BOM is forbidden: {path}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ModelGovernancePolicyError(
                    f"duplicate JSON key {key!r} in {path}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelGovernancePolicyError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ModelGovernancePolicyError(f"{path} root must be an object")
    return value


def inventory_hash(inventory: Mapping[str, Any]) -> str:
    return inventory_content_sha256(inventory)


def _profile_digest(profile: Mapping[str, Any]) -> str:
    from anila_security.model_governance import profile_content_sha256

    return profile_content_sha256(profile)


def _verify_inventory(
    inventory: Mapping[str, Any], repo_root: Path
) -> dict[str, InferenceCallsite]:
    try:
        by_id = validate_inventory_payload(inventory)
    except ModelGovernanceError as exc:
        raise ModelGovernancePolicyError(str(exc)) from exc
    repo_root = repo_root.resolve()
    for call in by_id.values():
        source_ref = Path(call.source)
        if source_ref.is_absolute():
            raise ModelGovernancePolicyError(
                f"callsite source must be repository-relative: {call.source}"
            )
        source_path = (repo_root / source_ref).resolve()
        try:
            source_path.relative_to(repo_root)
        except ValueError as exc:
            raise ModelGovernancePolicyError(
                f"callsite source escapes repository root: {call.source}"
            ) from exc
        if not source_path.is_file():
            raise ModelGovernancePolicyError(f"callsite source missing: {call.source}")
        if call.enabled:
            raise ModelGovernancePolicyError(
                f"repository inventory cannot pre-enable callsite {call.callsite_id}"
            )
        if call.raw_endpoint and call.gateway_id == GATEWAY_ID:
            raise ModelGovernancePolicyError(
                f"raw endpoint callsite cannot claim CSP gateway: {call.callsite_id}"
            )
    findings = scan_inference_sources(repo_root)
    finding_by_source = {finding.source: finding for finding in findings}
    if len(finding_by_source) != len(findings):
        raise ModelGovernancePolicyError("scanner returned duplicate source findings")
    inventory_sources = {call.source for call in by_id.values()}
    scanner_sources = set(finding_by_source)
    if scanner_sources != inventory_sources:
        missing = sorted(scanner_sources - inventory_sources)
        stale = sorted(inventory_sources - scanner_sources)
        raise ModelGovernancePolicyError(
            f"inference inventory/source scanner mismatch; missing={missing}, stale={stale}"
        )
    for call in by_id.values():
        finding = finding_by_source[call.source]
        if not set(finding.sink_kinds) <= set(call.sink_kinds):
            raise ModelGovernancePolicyError(
                f"callsite {call.callsite_id} omits scanner sink kind(s)"
            )
    return by_id


def verify(
    *,
    inventory_path: Path,
    profile_path: Path,
    trust_store_path: Path | None = None,
    repo_root: Path | None = None,
    allow_disabled_template: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a signed production profile against the source inventory."""

    inventory = _load_json(inventory_path)
    profile = _load_json(profile_path)
    # Production governance material is deliberately mounted from outside the
    # repository.  Never infer the source tree from that external path: doing
    # so would either scan the host root or silently skip the real repository.
    # Callers may omit this only for the repository-local test fixtures, where
    # the historical path-derived default remains useful.
    source_root = (repo_root or inventory_path.parents[3]).resolve()
    calls = _verify_inventory(inventory, source_root)
    current = now or datetime.now(timezone.utc)
    trust_store = _load_json(trust_store_path) if trust_store_path is not None else None
    try:
        authority = VerifiedModelGovernanceAuthority.from_verified_payload(
            inventory=inventory,
            profile=profile,
            trust_store=trust_store,
            allow_disabled_template=allow_disabled_template,
            now=current,
        )
    except ModelGovernanceError as exc:
        raise ModelGovernancePolicyError(str(exc)) from exc
    # The source scanner remains infra-owned; all profile/signature/binding
    # semantics above are delegated to the same immutable authority consumed by
    # runtime callers.  This check makes the scanner and authority inventories
    # fail closed if either side ever drifts.
    if set(authority.callsites) != set(calls):
        raise ModelGovernancePolicyError(
            "authority/scanner callsite inventory mismatch"
        )
    return {
        "inventory_callsites": len(authority.callsites),
        "enabled_callsites": len(authority.enabled_callsites),
        "profile_enabled": authority.enabled,
        "inventory_sha256": authority.inventory_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--trust-store", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="repository root whose source tree is scanned (independent of external material)",
    )
    parser.add_argument("--allow-disabled-template", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(
            inventory_path=args.inventory,
            profile_path=args.profile,
            trust_store_path=args.trust_store,
            repo_root=args.repo_root,
            allow_disabled_template=args.allow_disabled_template,
        )
    except ModelGovernancePolicyError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
