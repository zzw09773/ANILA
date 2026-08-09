"""Keep every delivery-image build layer saveable on a DCS-protected host."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
DCS_CLEANUP = "rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid"
PID_PATH = "/run/sisidsdaemon.pid"

# This is an allowlist of things that are deliberately not delivery images.  It
# is intentionally small and path-specific: a new Dockerfile anywhere else in
# the tree is covered without another list update.  Each path is checked below
# so an old exception cannot quietly become a false sense of coverage.
EXCLUDED_DOCKERFILES: dict[str, str] = {
    "cht/Dockerfile": "development-only mock card reader",
    "infra/loadtest/stub/Dockerfile": "load-test-only embedding stub",
    "packages/anila-core/src/anila_core/cli/templates/agent-template/Dockerfile": (
        "template source, not a delivered image"
    ),
    "scraps/ANILA_UI/scraps/anila-ui-backup-2026-04-21/Dockerfile": "dead backup",
}


class _ComposeLoader(yaml.SafeLoader):
    """Read Compose extension tags without needing to evaluate their semantics."""


def _construct_compose_tag(loader: yaml.SafeLoader, _tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_compose_tag)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _all_dockerfiles() -> tuple[Path, ...]:
    """Find Dockerfiles physically present in the repo, including untracked ones."""

    paths: set[Path] = set()
    for pattern in ("Dockerfile*", "*.Dockerfile"):
        paths.update(
            path.resolve()
            for path in REPO_ROOT.rglob(pattern)
            if path.is_file() and ".git" not in path.parts
        )
    return tuple(sorted(paths))


def _excluded_paths() -> set[Path]:
    missing = sorted(
        relative
        for relative in EXCLUDED_DOCKERFILES
        if not (REPO_ROOT / relative).is_file()
    )
    assert not missing, (
        "Dockerfile exclusion list contains missing paths; review the exception instead of "
        f"letting it rot: {', '.join(missing)}"
    )
    return {(REPO_ROOT / relative).resolve() for relative in EXCLUDED_DOCKERFILES}


def _read_compose_document(path: Path) -> dict[str, object]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}
    except yaml.YAMLError as exc:
        raise AssertionError(f"{_display_path(path)} is not valid YAML: {exc}") from exc
    return document if isinstance(document, dict) else {}


def _repo_compose_files() -> tuple[Path, ...]:
    """Find Compose-shaped YAML files without maintaining a compose allowlist."""

    compose_files: set[Path] = set()
    for pattern in ("*.yml", "*.yaml"):
        for path in REPO_ROOT.rglob(pattern):
            if not path.is_file() or ".git" in path.parts:
                continue
            document = _read_compose_document(path)
            if "services" in document or "include" in document:
                compose_files.add(path.resolve())
    return tuple(sorted(compose_files))


def _compose_files_to_check() -> tuple[tuple[Path, Path], ...]:
    """Return compose files and the base used to resolve their build paths."""

    pending = [(path, path.parent) for path in _repo_compose_files()]
    for raw_path in os.environ.get("COMPOSE_EXTRA_FILES", "").split():
        compose_file = Path(raw_path)
        if not compose_file.is_absolute():
            compose_file = REPO_ROOT / compose_file
        compose_file = compose_file.resolve()
        assert compose_file.is_file(), f"COMPOSE_EXTRA_FILES references missing file {compose_file}"
        # The exporter documents extra compose paths as relative to the repo
        # root, so preserve that resolution base for the top-level overlay.
        pending.append((compose_file, REPO_ROOT))

    files: list[tuple[Path, Path]] = []
    seen: set[tuple[Path, Path]] = set()
    while pending:
        compose_file, build_base = pending.pop()
        key = (compose_file, build_base)
        if key in seen:
            continue
        seen.add(key)
        assert compose_file.is_file(), f"compose include references missing file {compose_file}"
        document = _read_compose_document(compose_file)
        files.append((compose_file, build_base))

        includes = document.get("include", [])
        if isinstance(includes, (str, Path)):
            includes = [includes]
        if not isinstance(includes, list):
            raise AssertionError(f"{_display_path(compose_file)} has an unsupported include shape")
        for include in includes:
            include_paths = include.get("path", []) if isinstance(include, dict) else include
            if isinstance(include_paths, (str, Path)):
                include_paths = [include_paths]
            if not isinstance(include_paths, list):
                raise AssertionError(
                    f"{_display_path(compose_file)} has an unsupported include path shape"
                )
            for include_path in include_paths:
                included_file = (compose_file.parent / str(include_path)).resolve()
                pending.append((included_file, included_file.parent))

    return tuple(files)


def _compose_build_dockerfiles() -> set[Path]:
    """Derive Dockerfiles from every discovered Compose build definition."""

    dockerfiles: set[Path] = set()
    for compose_file, build_base in _compose_files_to_check():
        document = _read_compose_document(compose_file)
        services = document.get("services") or {}
        if not isinstance(services, dict):
            raise AssertionError(f"{_display_path(compose_file)} has an unsupported services shape")
        for service_name, service in services.items():
            if not isinstance(service, dict):
                continue
            build = service.get("build")
            if build is None:
                continue
            if isinstance(build, str):
                context, dockerfile = build, "Dockerfile"
            elif isinstance(build, dict):
                context = build.get("context", ".")
                dockerfile = build.get("dockerfile", "Dockerfile")
            else:
                raise AssertionError(
                    f"{_display_path(compose_file)} service {service_name!r} has an unsupported "
                    "build shape"
                )
            if "$" in str(context) or "$" in str(dockerfile):
                raise AssertionError(
                    f"{_display_path(compose_file)} service {service_name!r} has an unresolved "
                    "variable in its build path; make coverage explicit before exporting"
                )

            dockerfile_path = (build_base / str(context) / str(dockerfile)).resolve()
            assert dockerfile_path.is_relative_to(REPO_ROOT), (
                f"{_display_path(compose_file)} service {service_name!r} builds outside the "
                f"repo and is not covered by the save-cleanup guard: {dockerfile_path}"
            )
            assert dockerfile_path.is_file(), (
                f"{_display_path(compose_file)} service {service_name!r} references missing "
                f"Dockerfile {dockerfile_path}"
            )
            dockerfiles.add(dockerfile_path)
    return dockerfiles


def _assert_compose_builds_are_discovered(discovered: set[Path]) -> None:
    """Make a shrinking filesystem coverage set fail loudly."""

    missing = sorted(_compose_build_dockerfiles() - discovered)
    assert not missing, (
        "Compose build coverage shrank: these shipped Dockerfiles are not in the filesystem "
        "discovery set: "
        + ", ".join(_display_path(path) for path in missing)
    )


def _delivery_dockerfiles() -> tuple[Path, ...]:
    """Return all in-repo delivery Dockerfiles with explicit non-delivery exceptions.

    The filesystem is the coverage source rather than a compose-derived list.
    Therefore a service added through a ``docker compose -f`` overlay is still
    covered as long as its Dockerfile is in this repo; there is no second list
    for an overlay to evade.  An out-of-repo build is outside this guard's
    delivery contract and must be brought into the repo or given a reviewed,
    path-specific exception here.
    """

    dockerfiles = set(_all_dockerfiles())
    assert dockerfiles, "repo has no Dockerfiles to protect"

    excluded = _excluded_paths()
    assert excluded <= dockerfiles, (
        "Dockerfile exclusion paths are not discovered by the default scan: "
        + ", ".join(sorted(_display_path(path) for path in excluded - dockerfiles))
    )
    _assert_compose_builds_are_discovered(dockerfiles)
    return tuple(sorted(dockerfiles - excluded))


def _run_instructions(path: Path) -> list[tuple[int, str]]:
    """Return (start line, raw instruction) pairs for Dockerfile RUN commands."""

    instructions: list[tuple[int, str]] = []
    start_line: int | None = None
    lines: list[str] = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            if start_line is not None:
                lines.append(line)
            continue

        # ONBUILD RUN executes later as a build layer, but the simple parser
        # cannot model that trigger.  It must fail loudly, never look like a
        # Dockerfile with zero RUNs.
        if re.match(r"ONBUILD(?:\s|$)", stripped, re.IGNORECASE):
            raise AssertionError(
                f"{_display_path(path)}:{line_number}: ONBUILD instructions are unsupported by "
                "the save-cleanup guard; add an explicit parser rule before using one"
            )

        if start_line is None:
            if re.match(r"RUN(?:\s|$)", stripped, re.IGNORECASE):
                start_line = line_number
                lines = [line]
            else:
                continue
        else:
            lines.append(line)

        # Dockerfile permits comments inside a continued instruction.  They
        # do not end the RUN, even though the comment line itself has no '\\'.
        if not line.rstrip().endswith("\\"):
            instructions.append((start_line, "\n".join(lines)))
            start_line = None
            lines = []

    if start_line is not None:
        instructions.append((start_line, "\n".join(lines)))
    return instructions


def _shell_segments(command: str) -> list[str]:
    """Split simple shell command separators while preserving quoted strings."""

    command = re.sub(r"^\s*RUN(?:\s+|$)", "", command, count=1, flags=re.IGNORECASE)
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0

    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if command.startswith(("&&", "||"), index):
            segments.append(command[start:index].strip())
            start = index + 2
            index += 2
            continue
        if char in ";|&":
            segments.append(command[start:index].strip())
            start = index + 1
            index += 1
            continue
        index += 1

    segments.append(command[start:].strip())
    return [segment for segment in segments if segment]


def _is_dcs_cleanup_segment(segment: str) -> bool:
    pid_index = segment.rfind(PID_PATH)
    if pid_index < 0:
        return False

    rm_match = re.match(r"rm\s+-rf\b", segment)
    if rm_match is None:
        return False

    # Keep the canonical order.  The apt-cache form used by existing images is
    # allowed because it has the same rm command and puts both DCS paths after
    # the apt path.
    cleanup_prefix = segment[rm_match.end() :pid_index]
    return "/var/lib/sdcssagent" in cleanup_prefix


def _is_trailing_rm_f(segment: str) -> bool:
    if re.fullmatch(r"rm\s+-f\s+[^|&;]+", segment) is None:
        return False
    return "$(" not in segment and "`" not in segment


def _ends_with_dcs_cleanup(instruction: str) -> bool:
    """Require DCS cleanup after the work, with only harmless rm -f after it."""

    command = " ".join(
        part
        for line in instruction.splitlines()
        if not line.lstrip().startswith("#")
        for part in [line.strip().removesuffix("\\").strip()]
        if part
    )
    segments = _shell_segments(command)
    cleanup_indices = [
        index for index, segment in enumerate(segments) if _is_dcs_cleanup_segment(segment)
    ]
    if not cleanup_indices:
        return False

    cleanup_index = cleanup_indices[-1]
    # A cleanup-only RUN, or a RUN that cleans before doing its real work, is
    # not safe: the injected layer is committed at the end of this RUN.
    if cleanup_index == 0:
        return False

    # services/asr-decoder already removes one harmless base-image log after
    # its DCS cleanup.  Preserve that form, but reject any later install/build
    # or other substantive shell command.
    return all(_is_trailing_rm_f(segment) for segment in segments[cleanup_index + 1 :])


def test_delivery_dockerfiles_clean_dcs_injection_before_each_layer_commit() -> None:
    missing: list[str] = []
    for dockerfile in _delivery_dockerfiles():
        for line_number, instruction in _run_instructions(dockerfile):
            if not _ends_with_dcs_cleanup(instruction):
                missing.append(f"{_display_path(dockerfile)}:{line_number}")

    assert not missing, (
        "Every RUN in a Dockerfile shipped in the intranet delivery bundle must "
        f"end with `{DCS_CLEANUP}`. Add that cleanup to the end of each listed RUN "
        "because Symantec DCS can inject /var/lib/sdcssagent and "
        "/run/sisidsdaemon.pid into the build layer, and a later cleanup layer "
        "cannot repair docker save metadata. Missing cleanup at: "
        + ", ".join(missing)
    )


def test_default_discovery_covers_a_new_dockerfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dockerfiles = (
        tmp_path / "services" / "newthing" / "Dockerfile",
        tmp_path / "services" / "newthing" / "Dockerfile.dev",
        tmp_path / "infra" / "docker" / "csp.Dockerfile",
    )
    for dockerfile in dockerfiles:
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text("FROM alpine\n", encoding="utf-8")
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXCLUDED_DOCKERFILES", {})

    assert _delivery_dockerfiles() == tuple(sorted(dockerfile.resolve() for dockerfile in dockerfiles))


def test_compose_build_cannot_disappear_from_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules[__name__]
    monkeypatch.delenv("COMPOSE_EXTRA_FILES", raising=False)
    csp_dockerfile = REPO_ROOT / "infra/docker/csp.Dockerfile"
    assert csp_dockerfile.resolve() in _compose_build_dockerfiles()

    discovered_without_csp = tuple(
        path for path in _all_dockerfiles() if path != csp_dockerfile.resolve()
    )
    monkeypatch.setattr(module, "_all_dockerfiles", lambda: discovered_without_csp)

    with pytest.raises(AssertionError, match="infra/docker/csp.Dockerfile"):
        _delivery_dockerfiles()


def test_exclusion_list_cannot_name_a_missing_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXCLUDED_DOCKERFILES", {"gone/Dockerfile": "removed"})
    present = tmp_path / "present" / "Dockerfile"
    present.parent.mkdir(parents=True)
    present.write_text("FROM alpine\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="gone/Dockerfile"):
        _delivery_dockerfiles()


def test_empty_dockerfile_root_fails_with_vacuous_pass_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXCLUDED_DOCKERFILES", {"gone/Dockerfile": "removed"})

    with pytest.raises(AssertionError, match="repo has no Dockerfiles to protect"):
        _delivery_dockerfiles()


def test_overlay_build_outside_repo_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yml"
    overlay.write_text(
        "services:\n  external:\n    build:\n      context: /tmp/outside-delivery\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPOSE_EXTRA_FILES", str(overlay))

    with pytest.raises(AssertionError, match="outside the repo"):
        _delivery_dockerfiles()


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        (f"RUN pip install x && {DCS_CLEANUP}", True),
        (f"RUN pip install x && {DCS_CLEANUP} && rm -f /var/log/bootstrap.log", True),
        (f"RUN pip install x && {DCS_CLEANUP} && rm -f /tmp/y && pip install evil", False),
        (f"RUN {DCS_CLEANUP} && rm -f /tmp/y", False),
        (f"RUN pip install x && {DCS_CLEANUP} || pip install evil", False),
        (f"RUN pip install x && {DCS_CLEANUP} | tee /tmp/output", False),
        (f"RUN pip install x && {DCS_CLEANUP} & pip install evil", False),
        (f"RUN pip install x && {DCS_CLEANUP} && rm -f $(pip install evil)", False),
    ],
)
def test_dcs_cleanup_is_the_last_substantive_shell_command(instruction: str, expected: bool) -> None:
    assert _ends_with_dcs_cleanup(instruction) is expected


def test_onbuild_run_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\nONBUILD RUN echo later\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="ONBUILD"):
        _run_instructions(dockerfile)
