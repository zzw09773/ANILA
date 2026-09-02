"""Every file a delivery Dockerfile COPYs must be readable by the non-root user.

2026-08-24 incident (queued-fix-file-mode-shipped-into-image): a 0640 file in
``services/csp/app/`` was COPY'd into the csp image as ``root:root 0640``; the
container runs as uid 10001, alembic died with ``PermissionError`` while csp
stayed healthy. It bit twice in one week. git tracks only the exec bit, so a
clean clone hides it and ``git diff`` never shows it — the working tree we
actually build from is the only place it can be seen.

Invariant: **every file the image will read, the runtime identity can read.**
Scope is deliberately narrow (the work order says so): only paths that a
delivery Dockerfile's ``COPY`` reaches, resolved against the build contexts
platform.yml uses (repo root, or the Dockerfile's own directory). Files
outside those paths may be any mode.

Acceptance (from the work order): chmod 0600 a tracked file under a COPY'd
path → this test goes red; restore → green. The guard reads the git index for
the file list and the filesystem for the mode, so it sees exactly what
``docker build`` would ship.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from tests.test_dockerfile_save_cleanup import (
    REPO_ROOT,
    _delivery_dockerfiles,
    _display_path,
    _tracked_files,
)

_COPY_LINE = re.compile(r"^\s*(?:COPY|ADD)\b(?P<rest>.*)$", re.IGNORECASE)
_FLAG = re.compile(r"^--[a-z-]+(?:=\S*)?$")


def _copy_sources(dockerfile: Path) -> list[str]:
    """Source operands of every ``COPY``/``ADD`` that reads the build context.

    ``--from=`` stages copy from another image, not from the tree; skipped.
    Line continuations are joined first so a multi-line COPY is one statement.
    """
    text = dockerfile.read_text(encoding="utf-8")
    text = re.sub(r"\\\n", " ", text)
    sources: list[str] = []
    for line in text.splitlines():
        match = _COPY_LINE.match(line)
        if not match:
            continue
        tokens = match.group("rest").split()
        if any(t.startswith("--from") for t in tokens):
            continue
        operands = [t for t in tokens if not _FLAG.match(t)]
        if len(operands) < 2:
            continue
        sources.extend(operands[:-1])
    return sources


def _context_candidates(dockerfile: Path) -> tuple[Path, ...]:
    """platform.yml builds either from the Dockerfile's own directory or from
    the repo root (``context: ../..``). The own directory is tried first and
    wins if any COPY source resolves there: ``COPY . ./`` exists under both
    candidates, and reading it as "the whole repo" would drag every tracked
    file into a front-end image's scope."""
    return (dockerfile.parent, REPO_ROOT)


def _copied_roots(dockerfile: Path) -> set[Path]:
    sources = _copy_sources(dockerfile)
    for context in _context_candidates(dockerfile):
        roots: set[Path] = set()
        for source in sources:
            pattern = source.strip("/")
            if pattern.startswith("./"):
                pattern = pattern[2:]
            if pattern in ("", "."):
                # ``COPY . ./`` — the whole build context.
                roots.add(context.resolve())
                continue
            for hit in context.glob(pattern):
                roots.add(hit.resolve())
        if roots:
            return roots
    return set()


def _unreadable_by_others(paths: list[Path], roots: set[Path]) -> list[str]:
    """Files without ``o+r``, or ancestor directories (inside a COPY root)
    without ``o+rx``. Reported as ``mode path`` so the fix is one chmod."""
    problems: list[str] = []
    seen_dirs: set[Path] = set()
    for path in paths:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if not mode & stat.S_IROTH:
            problems.append(f"{stat.S_IMODE(mode):04o} {_display_path(path)}")
        for parent in path.parents:
            if parent in seen_dirs:
                break
            seen_dirs.add(parent)
            if parent not in roots and not any(
                root in parent.parents for root in roots
            ):
                break
            pmode = parent.stat().st_mode
            if not (pmode & stat.S_IROTH and pmode & stat.S_IXOTH):
                problems.append(f"{stat.S_IMODE(pmode):04o} {_display_path(parent)}/")
    return sorted(set(problems))


def _tracked_under(roots: set[Path]) -> list[Path]:
    tracked = [p.resolve() for p in _tracked_files()]
    return [
        p for p in tracked
        if p in roots or any(root in p.parents for root in roots)
    ]


@pytest.mark.parametrize(
    "dockerfile",
    _delivery_dockerfiles(),
    ids=lambda p: _display_path(p),
)
def test_every_file_the_image_copies_is_readable_by_the_runtime_user(dockerfile: Path):
    if not _copy_sources(dockerfile):
        # Images built purely from upstream layers (codeserver, the two model
        # servers) ship nothing from the tree; there is nothing to check.
        pytest.skip(f"{_display_path(dockerfile)}: no COPY/ADD from the build context")
    roots = _copied_roots(dockerfile)
    assert roots, f"{_display_path(dockerfile)}: no COPY source resolved against any build context"
    files = _tracked_under(roots)
    assert files, f"{_display_path(dockerfile)}: COPY roots contain no tracked files"
    problems = _unreadable_by_others(files, roots)
    assert not problems, (
        f"{_display_path(dockerfile)} would ship files its non-root user cannot read "
        f"(git only tracks the exec bit, so this is invisible in diff). "
        f"Fix: chmod a+rX the paths below.\n  " + "\n  ".join(problems)
    )


# ── self-tests: the checker itself can go red ─────────────────────────────


def test_checker_flags_a_0600_file(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    secret = root / "guard.py"
    secret.write_text("x")
    os.chmod(secret, 0o600)
    problems = _unreadable_by_others([secret], {root.resolve()})
    assert problems == [f"0600 {_display_path(secret.resolve())}"]


def test_checker_flags_a_directory_without_o_rx(tmp_path: Path):
    root = tmp_path / "app"
    inner = root / "pkg"
    inner.mkdir(parents=True)
    f = inner / "m.py"
    f.write_text("x")
    os.chmod(f, 0o644)
    os.chmod(inner, 0o750)
    try:
        problems = _unreadable_by_others([f], {root.resolve()})
    finally:
        os.chmod(inner, 0o755)
    assert problems == [f"0750 {_display_path(inner.resolve())}/"]


def test_copy_sources_skip_stage_copies_and_keep_context_copies(tmp_path: Path):
    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM x\n"
        "COPY --from=builder /opt/venv /opt/venv\n"
        "COPY --chown=anila:anila services/csp/ ./\n"
        "COPY packages/anila-core \\\n    /tmp/anila-core\n"
        "ADD a.txt b.txt /dst/\n"
    )
    assert _copy_sources(df) == ["services/csp/", "packages/anila-core", "a.txt", "b.txt"]
