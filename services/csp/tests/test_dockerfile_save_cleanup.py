"""Keep every delivery-image build layer saveable on a DCS-protected host."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = REPO_ROOT / "compose.yaml"
DCS_CLEANUP = "rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid"


def _compose_include_paths(root: Path) -> tuple[Path, ...]:
    """Follow the same compose include tree used by the intranet exporter."""

    pending = [root.resolve()]
    seen: set[Path] = set()
    files: list[Path] = []
    while pending:
        compose_file = pending.pop()
        if compose_file in seen:
            continue
        seen.add(compose_file)
        files.append(compose_file)

        document = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        includes = document.get("include", [])
        if isinstance(includes, (str, Path)):
            includes = [includes]
        for include in includes:
            include_paths = include.get("path", []) if isinstance(include, dict) else include
            if isinstance(include_paths, str):
                include_paths = [include_paths]
            for include_path in include_paths:
                pending.append((compose_file.parent / include_path).resolve())

    return tuple(files)


def _delivery_dockerfiles() -> tuple[Path, ...]:
    """Derive build Dockerfiles from compose instead of maintaining a list here."""

    dockerfiles: set[Path] = set()
    for compose_file in _compose_include_paths(COMPOSE_ROOT):
        document = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        for service_name, service in (document.get("services") or {}).items():
            build = service.get("build")
            if build is None:
                continue

            if isinstance(build, str):
                context = build
                dockerfile = "Dockerfile"
            else:
                context = build.get("context", ".")
                dockerfile = build.get("dockerfile", "Dockerfile")

            path = (compose_file.parent / context / dockerfile).resolve()
            assert path.is_file(), (
                f"{compose_file.relative_to(REPO_ROOT)} service {service_name!r} "
                f"references missing delivery Dockerfile {path.relative_to(REPO_ROOT)}"
            )
            dockerfiles.add(path)

    assert dockerfiles, "compose delivery bundle has no build Dockerfiles to protect"
    return tuple(sorted(dockerfiles))


def _run_instructions(path: Path) -> list[tuple[int, str]]:
    """Return (start line, raw instruction) pairs for Dockerfile RUN commands."""

    instructions: list[tuple[int, str]] = []
    start_line: int | None = None
    lines: list[str] = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if start_line is None:
            if re.match(r"RUN(?:\s|$)", stripped):
                start_line = line_number
                lines = [line]
            else:
                continue
        else:
            lines.append(line)
        # Dockerfile permits comments inside a continued instruction. They do
        # not end the RUN, even though the comment line itself has no '\\'.
        if stripped.startswith("#") or not line.rstrip().endswith("\\"):
            if stripped.startswith("#"):
                continue
            instructions.append((start_line, "\n".join(lines)))
            start_line = None
            lines = []

    if start_line is not None:
        instructions.append((start_line, "\n".join(lines)))
    return instructions


def _ends_with_dcs_cleanup(instruction: str) -> bool:
    """Accept the existing asr-decoder form that also removes apt's cache."""

    command = " ".join(
        part
        for line in instruction.splitlines()
        if not line.lstrip().startswith("#")
        for part in [line.strip().removesuffix("\\").strip()]
        if part
    )
    pid_path = "/run/sisidsdaemon.pid"
    pid_end = command.rfind(pid_path) + len(pid_path)
    if pid_end < len(pid_path):
        return False

    cleanup_prefix = command[:pid_end]
    if (
        re.search(r"\brm\s+-rf\b", cleanup_prefix) is None
        or "/var/lib/sdcssagent" not in cleanup_prefix
    ):
        return False

    tail = command[pid_end:].strip()
    # asr-decoder already has a harmless base-image bootstrap.log removal
    # after the DCS cleanup; preserve that existing form without allowing a
    # later install/build command to hide a misplaced cleanup.
    return not tail or re.fullmatch(r"(?:&&|;)\s*rm\s+-f\s+.+", tail) is not None


def test_delivery_dockerfiles_clean_dcs_injection_before_each_layer_commit() -> None:
    missing: list[str] = []
    for dockerfile in _delivery_dockerfiles():
        for line_number, instruction in _run_instructions(dockerfile):
            if not _ends_with_dcs_cleanup(instruction):
                relative = dockerfile.relative_to(REPO_ROOT)
                missing.append(f"{relative}:{line_number}")

    assert not missing, (
        "Every RUN in a Dockerfile shipped in the intranet delivery bundle must "
        f"end with `{DCS_CLEANUP}`. Add that cleanup to the end of each listed RUN "
        "because Symantec DCS can inject /var/lib/sdcssagent and "
        "/run/sisidsdaemon.pid into the build layer, and a later cleanup layer "
        "cannot repair docker save metadata. Missing cleanup at: "
        + ", ".join(missing)
    )
