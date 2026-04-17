"""Memory Consolidation (AutoDream) — cross-session background memory cleanup.

Ported from Claude Code autoDream.ts and consolidationLock.ts.

Trigger conditions (cheapest first):
  1. Time gate: hours since last consolidation >= min_hours (default 24)
  2. Session gate: sessions since last consolidation >= min_sessions (default 5)
  3. Lock: no other process is consolidating

Consolidation 4-phase flow:
  orient   -> scan existing memories
  gather   -> search transcripts for new signals
  consolidate -> merge duplicates, remove contradictions, fix outdated info
  prune    -> trim MEMORY.md index to 200 lines / 25KB

The consolidation lock uses a file whose mtime IS the lastConsolidatedAt
timestamp. PID is stored in the file body for dead-process detection.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = ".consolidate-lock"
HOLDER_STALE_SECONDS = 3600  # 1 hour
DEFAULT_MIN_HOURS = 24.0
DEFAULT_MIN_SESSIONS = 5


@dataclass
class ConsolidationConfig:
    """Configuration for auto-dream consolidation."""

    min_hours: float = DEFAULT_MIN_HOURS
    min_sessions: int = DEFAULT_MIN_SESSIONS
    lock_file_name: str = LOCK_FILE_NAME
    holder_stale_seconds: int = HOLDER_STALE_SECONDS


@dataclass
class ConsolidationLockState:
    """State captured during lock acquisition."""

    prior_mtime: float  # mtime before we acquired (for rollback)
    pid: int


class ConsolidationLockManager:
    """File-based mutex for consolidation.

    Lock file location: {memory_dir}/.consolidate-lock
    Lock file mtime = lastConsolidatedAt
    Lock file body = holder PID
    """

    def __init__(self, memory_dir: str, config: Optional[ConsolidationConfig] = None) -> None:
        self._memory_dir = memory_dir
        self._config = config or ConsolidationConfig()

    @property
    def _lock_path(self) -> Path:
        return Path(self._memory_dir) / self._config.lock_file_name

    async def read_last_consolidated_at(self) -> float:
        """Return the mtime of the lock file (= lastConsolidatedAt), or 0."""
        try:
            return self._lock_path.stat().st_mtime_ns / 1_000_000
        except FileNotFoundError:
            return 0.0

    def _is_process_running(self, pid: int) -> bool:
        """Return True if the given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def try_acquire(self) -> Optional[ConsolidationLockState]:
        """Attempt to acquire the consolidation lock.

        Returns ConsolidationLockState on success, None if blocked.
        """
        lock_path = self._lock_path
        prior_mtime: float = 0.0
        holder_pid: Optional[int] = None

        try:
            stat = lock_path.stat()
            prior_mtime = stat.st_mtime_ns / 1_000_000
            raw = lock_path.read_text().strip()
            parsed = int(raw)
            holder_pid = parsed if parsed > 0 else None
        except (FileNotFoundError, ValueError):
            pass

        # If a recent lock exists and holder is alive, yield
        age_ms = time.time() * 1000 - prior_mtime
        if prior_mtime > 0 and age_ms < self._config.holder_stale_seconds * 1000:
            if holder_pid is not None and self._is_process_running(holder_pid):
                logger.debug("[ConsolidationLock] blocked by PID %d", holder_pid)
                return None

        # Acquire: write our PID
        os.makedirs(self._memory_dir, exist_ok=True)
        lock_path.write_text(str(os.getpid()))

        # Verify we won (prevent race)
        try:
            written = lock_path.read_text().strip()
            if int(written) != os.getpid():
                return None
        except (FileNotFoundError, ValueError):
            return None

        return ConsolidationLockState(prior_mtime=prior_mtime, pid=os.getpid())

    async def rollback(self, prior_state: ConsolidationLockState) -> None:
        """Rewind the lock to pre-acquisition state after a failed run."""
        lock_path = self._lock_path
        try:
            if prior_state.prior_mtime == 0:
                lock_path.unlink(missing_ok=True)
                return
            lock_path.write_text("")
            t = prior_state.prior_mtime / 1000
            os.utime(lock_path, (t, t))
        except Exception as exc:
            logger.warning("[ConsolidationLock] rollback failed: %s", exc)

    async def list_sessions_since(self, since_ms: float, sessions_dir: str) -> list[str]:
        """Return session IDs whose transcript files were modified after since_ms."""
        sessions_path = Path(sessions_dir)
        if not sessions_path.is_dir():
            return []

        result: list[str] = []
        try:
            for entry in sessions_path.iterdir():
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    mtime_ms = entry.stat().st_mtime_ns / 1_000_000
                    if mtime_ms > since_ms:
                        session_id = entry.stem
                        result.append(session_id)
                except OSError:
                    continue
        except OSError:
            pass

        return result


CONSOLIDATION_PROMPT_TEMPLATE = """You are running a memory consolidation task (auto-dream).

Memory directory: {memory_dir}
Transcript directory: {transcript_dir}

## Phase 1: Orient
Read MEMORY.md to understand what is currently indexed. Scan the memory directory
to see what topic files exist and what they contain.

## Phase 2: Gather
Search the transcript files for new information since the last consolidation that
is not yet captured in memory files. Look for:
- New user preferences or corrections
- New project conventions established
- Debugging lessons learned
- API patterns discovered

Sessions to review ({session_count}):
{session_list}

## Phase 3: Consolidate
For each piece of new information:
- If it updates an existing memory, edit that file
- If it contradicts an existing memory, resolve the contradiction
- If it is new, create a new memory file with proper frontmatter
- Convert relative dates to absolute dates where possible
- Remove information that is no longer accurate

## Phase 4: Prune
Update MEMORY.md to reflect all changes:
- Each entry: `- [Title](file.md) - one-line hook`
- Maximum 200 lines
- Maximum 25KB
- Remove entries for deleted files
- Add entries for new files

{extra}
"""


def build_consolidation_prompt(
    memory_dir: str,
    transcript_dir: str,
    session_ids: list[str],
    extra: str = "",
) -> str:
    """Build the consolidation prompt for the forked agent."""
    session_list = "\n".join(f"- {sid}" for sid in session_ids)
    return CONSOLIDATION_PROMPT_TEMPLATE.format(
        memory_dir=memory_dir,
        transcript_dir=transcript_dir,
        session_count=len(session_ids),
        session_list=session_list or "(none)",
        extra=extra,
    )


RunConsolidationFn = Callable[
    [str, str],  # (memory_dir, prompt) -> session_ids written
    Coroutine[Any, Any, list[str]],
]


class ConsolidationService:
    """Manages the auto-dream consolidation lifecycle.

    Gates, locks, and delegates to a forked agent for actual processing.
    """

    def __init__(
        self,
        memory_dir: str,
        sessions_dir: str,
        current_session_id: str = "",
        config: Optional[ConsolidationConfig] = None,
    ) -> None:
        self._memory_dir = memory_dir
        self._sessions_dir = sessions_dir
        self._current_session_id = current_session_id
        self._config = config or ConsolidationConfig()
        self._lock_mgr = ConsolidationLockManager(memory_dir, self._config)
        self._last_session_scan: float = 0.0
        self._scan_interval_ms = 10 * 60 * 1000  # 10 minutes

    async def should_consolidate(self) -> tuple[bool, list[str]]:
        """Check all gates and return (should_run, session_ids).

        Returns (False, []) when any gate fails.
        """
        # Time gate
        last_at = await self._lock_mgr.read_last_consolidated_at()
        hours_since = (time.time() * 1000 - last_at) / 3_600_000
        if hours_since < self._config.min_hours:
            return False, []

        # Scan throttle
        scan_age_ms = time.time() * 1000 - self._last_session_scan
        if scan_age_ms < self._scan_interval_ms:
            return False, []
        self._last_session_scan = time.time() * 1000

        # Session gate
        session_ids = await self._lock_mgr.list_sessions_since(last_at, self._sessions_dir)
        session_ids = [s for s in session_ids if s != self._current_session_id]
        if len(session_ids) < self._config.min_sessions:
            logger.debug(
                "[Consolidation] skip: %d sessions, need %d",
                len(session_ids),
                self._config.min_sessions,
            )
            return False, []

        return True, session_ids

    async def run(
        self,
        run_forked: RunConsolidationFn,
        session_ids: Optional[list[str]] = None,
    ) -> list[str]:
        """Attempt to run consolidation.

        Args:
            run_forked: Async function that executes the consolidation agent.
            session_ids: If provided, skip the gate check.

        Returns:
            List of memory file paths touched.
        """
        if session_ids is None:
            should_run, session_ids = await self.should_consolidate()
            if not should_run:
                return []

        lock_state = await self._lock_mgr.try_acquire()
        if lock_state is None:
            return []

        logger.debug("[Consolidation] firing for %d sessions", len(session_ids))

        prompt = build_consolidation_prompt(
            self._memory_dir,
            self._sessions_dir,
            session_ids,
        )

        try:
            written = await run_forked(self._memory_dir, prompt)
            return written
        except Exception as exc:
            logger.warning("[Consolidation] failed: %s", exc)
            await self._lock_mgr.rollback(lock_state)
            return []
