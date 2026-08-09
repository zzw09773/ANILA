"""Keep every delivery-image build layer saveable on a DCS-protected host."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


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

_DOCKERFILE_NAME = re.compile(
    r"^(?:Dockerfile|Containerfile)(?:\..*)?$|"
    r"^.+\.(?:Dockerfile|Containerfile)$"
)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _tracked_files() -> tuple[Path, ...]:
    """Read the repository index instead of inspecting the filesystem."""

    command = ["git", "ls-files", "-z", "--"]
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AssertionError(
            f"git ls-files unavailable: git executable not found while checking {REPO_ROOT}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = os.fsdecode(exc.stderr or b"").strip()
        raise AssertionError(
            f"git ls-files unavailable for {REPO_ROOT}: "
            f"repository index unavailable (exit status {exc.returncode}); "
            f"{detail or 'git command failed without stderr'}"
        ) from exc
    except OSError as exc:
        raise AssertionError(
            f"git ls-files unavailable for {REPO_ROOT}: {type(exc).__name__}: {exc}"
        ) from exc

    return tuple(
        REPO_ROOT / Path(os.fsdecode(raw_path))
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    )


def _all_dockerfiles() -> tuple[Path, ...]:
    """Find tracked Dockerfiles by the naming conventions used in this repo."""

    return tuple(
        sorted(
            path.resolve()
            for path in _tracked_files()
            if _DOCKERFILE_NAME.fullmatch(path.name) is not None
        )
    )


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


def _delivery_dockerfiles() -> tuple[Path, ...]:
    """Return all tracked delivery Dockerfiles with explicit exceptions."""

    dockerfiles = set(_all_dockerfiles())
    assert dockerfiles, "repo has no Dockerfiles to protect"

    excluded = _excluded_paths()
    assert excluded <= dockerfiles, (
        "Dockerfile exclusion paths are not discovered by the default scan: "
        + ", ".join(sorted(_display_path(path) for path in excluded - dockerfiles))
    )
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
    """Split only top-level sequential shell command separators."""

    command = re.sub(r"^\s*RUN(?:\s+|$)", "", command, count=1, flags=re.IGNORECASE)
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    parentheses = 0
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
        if char == "(":
            parentheses += 1
            index += 1
            continue
        if char == ")" and parentheses:
            parentheses -= 1
            index += 1
            continue
        if parentheses == 0 and command.startswith("&&", index):
            segments.append(command[start:index].strip())
            start = index + 2
            index += 2
            continue
        if parentheses == 0 and char == ";":
            segments.append(command[start:index].strip())
            start = index + 1
            index += 1
            continue
        index += 1

    segments.append(command[start:].strip())
    return [segment for segment in segments if segment]


def _has_unsafe_shell_operator(command: str) -> bool:
    """Reject top-level conditionals, pipelines, and background commands."""

    quote: str | None = None
    escaped = False
    parentheses = 0
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
        if char == "(":
            parentheses += 1
            index += 1
            continue
        if char == ")":
            if parentheses == 0:
                return True
            parentheses -= 1
            index += 1
            continue
        if parentheses:
            index += 1
            continue
        if command.startswith("||", index) or char == "|":
            return True
        if command.startswith("&&", index):
            index += 2
            continue
        if char == "&":
            if command.startswith("&>", index) or (index > 0 and command[index - 1] == ">"):
                index += 2 if command.startswith("&>", index) else 1
                continue
            return True
        index += 1

    return quote is not None or parentheses != 0


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
    if any(_has_unsafe_shell_operator(segment) for segment in segments[cleanup_index:]):
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


def test_tracked_discovery_covers_delivery_paths_without_python_content_scan() -> None:
    discovered = {_display_path(path) for path in _all_dockerfiles()}

    assert "infra/codeserver/Dockerfile" in discovered
    assert "infra/docker/csp.Dockerfile" in discovered
    assert all(_DOCKERFILE_NAME.fullmatch(Path(path).name) for path in discovered)
    assert not any(path.endswith(".py") for path in discovered)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Dockerfile", True),
        ("Dockerfile.dev", True),
        ("image.Dockerfile", True),
        ("Containerfile", True),
        ("Containerfile.dev", True),
        ("image.Containerfile", True),
        ("Dockerfile~", False),
        ("Dockerfile-dev", False),
        ("notes.md", False),
    ],
)
def test_dockerfile_filename_conventions(filename: str, expected: bool) -> None:
    assert (_DOCKERFILE_NAME.fullmatch(filename) is not None) is expected


def test_missing_git_executable_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    def missing_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", missing_git)

    with pytest.raises(AssertionError, match="git ls-files unavailable: git executable not found"):
        _all_dockerfiles()


def test_export_without_git_index_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError, match="repository index unavailable"):
        _all_dockerfiles()


def test_exclusion_list_cannot_name_a_missing_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXCLUDED_DOCKERFILES", {"gone/Dockerfile": "removed"})
    present = tmp_path / "present" / "Dockerfile"
    present.parent.mkdir(parents=True)
    present.write_text("FROM alpine\n", encoding="utf-8")
    monkeypatch.setattr(module, "_all_dockerfiles", lambda: (present.resolve(),))

    with pytest.raises(AssertionError, match="gone/Dockerfile"):
        _delivery_dockerfiles()


def test_empty_dockerfile_root_fails_with_vacuous_pass_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "_all_dockerfiles", lambda: ())

    with pytest.raises(AssertionError, match="repo has no Dockerfiles to protect"):
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
        (f"RUN work || {DCS_CLEANUP}", False),
        (f"RUN work | {DCS_CLEANUP}", False),
        (f"RUN work & {DCS_CLEANUP}", False),
        (f"RUN (a || b) && {DCS_CLEANUP}", True),
        (f"RUN pip install x && {DCS_CLEANUP} &> /dev/null", True),
        (f"RUN pip install x && {DCS_CLEANUP} 2>&1", True),
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
