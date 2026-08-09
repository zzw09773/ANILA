"""Keep every delivery-image build layer saveable on a DCS-protected host."""

from __future__ import annotations

import os
import re
import stat
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

_SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "dist", "build"})
_MAX_CLASSIFICATION_BYTES = 64 * 1024
# Content remains authoritative; the basename is only a secondary signal when
# content cannot be classified, so unreadable Dockerfile-looking paths fail loud.
_DOCKERFILE_NAME = re.compile(
    r"^(?:dockerfile|containerfile)(?:[._-].*)?$|"
    r"^.+\.(?:dockerfile|containerfile)$",
    re.IGNORECASE,
)


class _UnclassifiableFileError(Exception):
    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(reason)


class _NonRegularFileError(_UnclassifiableFileError):
    """A filesystem entry that must never be opened by content discovery."""


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _repo_files() -> list[Path]:
    """Walk repository-owned files while pruning generated and nested repos."""

    files: list[Path] = []
    for root, directories, filenames in os.walk(REPO_ROOT, topdown=True, followlinks=False):
        root_path = Path(root)
        if root_path != REPO_ROOT and (".git" in directories or ".git" in filenames):
            directories[:] = []
            continue

        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRECTORIES and not (root_path / name).is_symlink()
        )
        for name in filenames:
            if name == ".git":
                continue
            path = root_path / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                # Keep a raced-away or otherwise unstatable path long enough
                # for _all_dockerfiles to apply the basename fail-loud rule.
                files.append(path)
                continue
            # os.walk reports FIFOs and sockets as filenames.  Never pass them
            # to a content reader: opening a FIFO can block forever.
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                continue
            files.append(path)
    return files


def _format_os_error(operation: str, exc: OSError) -> str:
    detail = str(exc) or "no additional details"
    return f"{operation} failed with {type(exc).__name__}: {detail}"


def _read_classification_text(path: Path) -> str:
    """Read a bounded, regular-file UTF-8 snapshot for content classification."""

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        raise _UnclassifiableFileError(path, _format_os_error("open", exc)) from exc

    try:
        try:
            mode = os.fstat(file_descriptor).st_mode
        except OSError as exc:
            raise _UnclassifiableFileError(path, _format_os_error("stat", exc)) from exc
        if not stat.S_ISREG(mode):
            raise _NonRegularFileError(
                path, f"not a regular file ({stat.filemode(mode)})"
            )

        content = bytearray()
        while True:
            try:
                chunk = os.read(
                    file_descriptor, _MAX_CLASSIFICATION_BYTES + 1 - len(content)
                )
            except OSError as exc:
                raise _UnclassifiableFileError(path, _format_os_error("read", exc)) from exc
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > _MAX_CLASSIFICATION_BYTES:
                raise _UnclassifiableFileError(
                    path,
                    "content exceeds the safe classification limit of "
                    f"{_MAX_CLASSIFICATION_BYTES} bytes",
                )
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            # A close race must not turn a completed classification into a
            # guard crash.  The descriptor is no longer usable here anyway.
            pass

    raw_content = bytes(content)
    if b"\x00" in raw_content:
        raise _UnclassifiableFileError(path, "binary content contains a NUL byte")
    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _UnclassifiableFileError(
            path, f"content is not valid UTF-8: {exc}"
        ) from exc


def _is_delivery_dockerfile(path: Path) -> bool:
    """Classify a delivery Dockerfile by its first meaningful instruction."""

    for line in _read_classification_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return re.match(r"FROM(?:\s|$)", stripped, re.IGNORECASE) is not None
    return False


def _looks_like_dockerfile_name(path: Path) -> bool:
    return _DOCKERFILE_NAME.fullmatch(path.name) is not None


def _all_dockerfiles() -> tuple[Path, ...]:
    """Find repository-owned delivery Dockerfiles regardless of their names."""

    dockerfiles: set[Path] = set()
    for path in _repo_files():
        try:
            is_delivery_dockerfile = _is_delivery_dockerfile(path)
        except _NonRegularFileError:
            # A path can change type after os.walk.  The fstat check in the
            # reader makes that race safe and keeps the non-regular file out.
            continue
        except _UnclassifiableFileError as exc:
            if _looks_like_dockerfile_name(path):
                raise AssertionError(
                    f"{path}: cannot classify Dockerfile content: {exc.reason}"
                ) from exc
            continue

        if is_delivery_dockerfile:
            try:
                dockerfiles.add(path.resolve())
            except OSError as exc:
                failure = _UnclassifiableFileError(
                    path, _format_os_error("resolve", exc)
                )
                if _looks_like_dockerfile_name(path):
                    raise AssertionError(
                        f"{path}: cannot classify Dockerfile path: {failure.reason}"
                    ) from failure
    return tuple(sorted(dockerfiles))


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
    """Return all repository-owned delivery Dockerfiles with explicit exceptions."""

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

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AssertionError(
            f"{_display_path(path)}: classified Dockerfile became unreadable: "
            f"{_format_os_error('read', exc) if isinstance(exc, OSError) else exc}"
        ) from exc

    for line_number, line in enumerate(source.splitlines(), 1):
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


def test_default_discovery_uses_content_and_prunes_non_repo_trees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dockerfiles = (
        tmp_path / "services" / "newthing" / "Dockerfile",
        tmp_path / "services" / "newthing" / "Dockerfile.dev",
        tmp_path / "infra" / "docker" / "csp.Dockerfile",
        tmp_path / "services" / "newthing" / "Containerfile",
        tmp_path / "services" / "newthing" / "image.recipe",
    )
    for dockerfile in dockerfiles:
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text("# syntax=docker/dockerfile:1\n\nFROM alpine\n", encoding="utf-8")

    late_from = tmp_path / "services" / "newthing" / "late-from.txt"
    late_from.write_text("RUN echo not-a-dockerfile\nFROM alpine\n", encoding="utf-8")
    (tmp_path / "apps" / "ui" / "node_modules" / "@vendor" / "invalid-yaml.yaml").parent.mkdir(
        parents=True
    )
    (tmp_path / "apps" / "ui" / "node_modules" / "@vendor" / "invalid-yaml.yaml").write_text(
        "test: '\n", encoding="utf-8"
    )
    for directory in ("node_modules", ".venv", "dist", "build"):
        ignored = tmp_path / directory / "third-party" / "Containerfile"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("FROM alpine\nRUN install evil\n", encoding="utf-8")

    nested_worktree = tmp_path / "nested-worktree"
    nested_worktree.mkdir()
    (nested_worktree / ".git").write_text("gitdir: /outside\n", encoding="utf-8")
    (nested_worktree / "Containerfile").write_text("FROM alpine\nRUN install evil\n", encoding="utf-8")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "EXCLUDED_DOCKERFILES", {})

    assert _delivery_dockerfiles() == tuple(sorted(dockerfile.resolve() for dockerfile in dockerfiles))


def test_readable_content_is_primary_over_a_plausible_basename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    named_like_dockerfile = tmp_path / "Dockerfile"
    named_like_dockerfile.write_text("This is documentation\n", encoding="utf-8")
    content_like_dockerfile = tmp_path / "release.recipe"
    content_like_dockerfile.write_text("FROM alpine\n", encoding="utf-8")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    assert _all_dockerfiles() == (content_like_dockerfile.resolve(),)


def test_unreadable_plausible_name_fails_loud_with_path_and_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unreadable = tmp_path / "Dockerfile.private"
    unreadable.write_text("FROM alpine\n", encoding="utf-8")
    real_open = os.open

    def deny_open(candidate: str | os.PathLike[str], flags: int) -> int:
        if Path(candidate) == unreadable:
            raise PermissionError("permission denied during classification")
        return real_open(candidate, flags)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "open", deny_open)

    with pytest.raises(AssertionError) as excinfo:
        _all_dockerfiles()

    message = str(excinfo.value)
    assert str(unreadable) in message
    assert "cannot classify" in message
    assert "PermissionError" in message


def test_unreadable_non_docker_name_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    valid = tmp_path / "Dockerfile.valid"
    valid.write_text("FROM alpine\n", encoding="utf-8")
    unreadable = tmp_path / "release-notes.txt"
    unreadable.write_text("FROM alpine\n", encoding="utf-8")
    real_open = os.open

    def fail_raced_read(candidate: str | os.PathLike[str], flags: int) -> int:
        if Path(candidate) == unreadable:
            raise OSError("file disappeared during classification")
        return real_open(candidate, flags)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "open", fail_raced_read)

    assert _all_dockerfiles() == (valid.resolve(),)


@pytest.mark.parametrize(
    ("filename", "content", "reason"),
    [
        ("Dockerfile.binary", b"FROM alpine\n\x00", "binary"),
        ("Containerfile.invalid", b"FROM alpine\n\xff", "UTF-8"),
    ],
)
def test_binary_or_decode_failure_is_reported_for_a_plausible_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    content: bytes,
    reason: str,
) -> None:
    unreadable = tmp_path / filename
    unreadable.write_bytes(content)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError) as excinfo:
        _all_dockerfiles()

    message = str(excinfo.value)
    assert str(unreadable) in message
    assert "cannot classify" in message
    assert reason in message


def test_content_over_safe_classification_limit_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oversized = tmp_path / "Dockerfile.oversized"
    oversized.write_bytes(b"FROM alpine\n" + b"x" * _MAX_CLASSIFICATION_BYTES)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError) as excinfo:
        _all_dockerfiles()

    message = str(excinfo.value)
    assert str(oversized) in message
    assert "safe classification limit" in message


def test_read_race_is_ignored_for_a_non_docker_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raced = tmp_path / "notes-race.txt"
    raced.write_text("FROM alpine\n", encoding="utf-8")

    def raise_read_race(_file_descriptor: int, _size: int) -> bytes:
        raise OSError("file changed during classification")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "read", raise_read_race)

    assert _all_dockerfiles() == ()


def test_read_race_fails_loud_for_a_plausible_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raced = tmp_path / "Dockerfile.race"
    raced.write_text("FROM alpine\n", encoding="utf-8")

    def raise_read_race(_file_descriptor: int, _size: int) -> bytes:
        raise OSError("file changed during classification")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "read", raise_read_race)

    with pytest.raises(AssertionError) as excinfo:
        _all_dockerfiles()

    message = str(excinfo.value)
    assert str(raced) in message
    assert "read failed" in message


def test_dangling_symlink_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    valid = tmp_path / "Dockerfile.valid"
    valid.write_text("FROM alpine\n", encoding="utf-8")
    dangling = tmp_path / "Dockerfile.dangling"
    try:
        dangling.symlink_to(tmp_path / "missing-target")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

    assert _all_dockerfiles() == (valid.resolve(),)


def test_fifo_is_ignored_without_opening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    valid = tmp_path / "Dockerfile.valid"
    valid.write_text("FROM alpine\n", encoding="utf-8")
    fifo = tmp_path / "Dockerfile.fifo"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError) as exc:
        pytest.skip(f"FIFO unavailable: {exc}")

    try:
        module = sys.modules[__name__]
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)

        assert _all_dockerfiles() == (valid.resolve(),)
    finally:
        fifo.unlink(missing_ok=True)


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
