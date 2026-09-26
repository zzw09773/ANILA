"""Keep dependency lower bounds that protect a known compatibility or CVE invariant."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[3]

# These floors are deliberately documented beside their manifest entries.  The
# test keeps the reason here as well so a future failure explains the contract,
# rather than merely reporting that a number changed.
# ⚠ 涵蓋聲明：FLOOR_RULES 是手寫清單、只管 Python 下限。2026-08-21 釘的四顆
# （react-router／echarts／js-yaml override／pytest==8.4.2）**不在本守衛涵蓋內**——
# 有人拔掉 package.json 的上限，這裡不會紅。這份清單不會自己長大。
FLOOR_RULES = (
    (
        "packages/anila-core/pyproject.toml",
        "starlette",
        "1.3.1",
        "CVE-2026-54283 remains in every Starlette release below 1.3.1.",
    ),
    (
        "services/anila-studio/pyproject.toml",
        "starlette",
        "1.3.1",
        "CVE-2026-54283 remains in every Starlette release below 1.3.1.",
    ),
    (
        "services/asr-decoder/pyproject.toml",
        "starlette",
        "1.3.1",
        "CVE-2026-54283 remains in every Starlette release below 1.3.1.",
    ),
    (
        "services/asr-gateway/pyproject.toml",
        "starlette",
        "1.3.1",
        "CVE-2026-54283 remains in every Starlette release below 1.3.1.",
    ),
    (
        "packages/anila-agent/pyproject.toml",
        "anila-core",
        "0.14",
        "the agent imports the JWKS/RS256 verifier introduced by anila-core 0.14.",
    ),
    (
        "services/ingestion-worker/pyproject.toml",
        "anila-core",
        "0.14.0",
        "the worker's parser and security imports are provided by anila-core 0.14.",
    ),
    (
        "services/ingestion-worker/pyproject.toml",
        "arq",
        "0.26",
        "Arq 0.26 is the first release with the Pydantic v2-compatible JobResult model.",
    ),
    (
        "services/asr-decoder/pyproject.toml",
        "faster-whisper",
        "1.0",
        "the decoder contract is tied to the CTranslate2-backed faster-whisper runtime.",
    ),
    (
        "services/asr-gateway/pyproject.toml",
        "webrtcvad-wheels",
        "2.0.14",
        "the maintained prebuilt fork avoids the original webrtcvad pkg_resources failure.",
    ),
)

_REQUIREMENT_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?(?P<spec>(?:[<>=!~].*)?)$"
)


def _parse_requirement(line: str) -> tuple[str, str] | None:
    candidate = line.split("#", 1)[0].strip().rstrip(",").strip()
    if not candidate or candidate.startswith(("-", "[", "}")):
        return None
    candidate = candidate.strip("\"'")
    match = _REQUIREMENT_LINE.fullmatch(candidate)
    if not match:
        return None
    return match.group("name"), match.group("spec")


def _find_requirement(path: Path, package: str) -> tuple[int, str, str] | None:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parsed = _parse_requirement(line)
        if parsed and parsed[0].casefold() == package.casefold():
            return line_number, line, parsed[1]
    return None


def _declared_lower_bound(spec: str) -> Version | None:
    minimums = []
    for item in SpecifierSet(spec):
        if item.operator in {">=", ">", "==", "~="}:
            minimums.append(Version(item.version))
    return max(minimums) if minimums else None


@pytest.mark.parametrize("manifest, package, floor, reason", FLOOR_RULES)
def test_documented_dependency_floors_are_not_lowered(
    manifest: str, package: str, floor: str, reason: str
) -> None:
    path = REPO_ROOT / manifest
    found = _find_requirement(path, package)
    if found is None:
        pytest.fail(
            f"{manifest}:1: missing {package} dependency; required floor is {package}>={floor} "
            f"because {reason}"
        )

    line_number, raw_line, spec = found
    declared = _declared_lower_bound(spec)
    required = Version(floor)
    if declared is None or declared < required:
        pytest.fail(
            f"{manifest}:{line_number}: {raw_line.strip()} allows {package} below {floor}; "
            f"the floor exists because {reason}"
        )
