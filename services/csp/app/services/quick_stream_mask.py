"""Monotonic bounded masking for terminal quick-builder output streams.

Only confirmed plaintext leaves ``feed``. ASCII atoms and Chinese key
prefixes remain pending until their role is known. Structured/quoted data
and a confirmed credential assignment quarantine the complete remaining
suffix: finishing replaces it, and never releases any prefix of its value.
This deliberately masks more than the batch formatter; it is not a JSON
incremental parser or a fixed-size look-behind window.
"""
from __future__ import annotations

import re

MAX_BUFFER_CHARS = 32 * 1024
MAX_TOTAL_CHARS = 32 * 1024
# The existing batch quick_execution.mask(str) only exposes 20000 chars.
# Do not stream extra plaintext it would later discard; fail explicitly.
MAX_VISIBLE_CHARS = 20_000
REDACTED = "[已遮罩]"
STRUCTURED_REDACTED = "[結構化內容已遮罩]"

_ATOM = re.compile(r"[a-z\d_.-]+", re.I)
_KEY = re.compile(
    r"password|passwd|secret|token|authorization|credential|cookie|api[_-]?key|"
    r"employee[_-]?id|密碼|憑證|員工編號|身分證|身份證",
    re.I,
)
_CHINESE_KEYS = ("密碼", "憑證", "員工編號", "身分證", "身份證")
_IDENTITY = re.compile(r"[A-Z][12]\d{8}", re.I)
_PHONE = re.compile(r"09\d{8}")
_STRUCTURED_START = frozenset("{[\"'\\")


class OutputMaskError(ValueError):
    """Stable, non-sensitive failure for the outer SSE error handler."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class OutputMaskLimitError(OutputMaskError):
    def __init__(self):
        super().__init__("OUTPUT_TOO_LARGE", "回答超過串流安全處理上限，請縮小問題範圍")


class OutputMasker:
    """``feed(str) -> str`` and one terminal ``finish() -> str``.

    Total input and pending buffer are bounded at 32768 Unicode characters.
    Visible output is bounded at 20000 characters to preserve the existing
    batch mask's disclosure ceiling. Overflow fails closed, clears pending
    content, and the instance cannot emit again. These are explicit errors,
    never silent truncation. ``finish`` is idempotent; ``feed`` after finish
    is a typed error. Call ``finish`` only after upstream completed normally.
    """

    def __init__(self):
        self._pending = ""
        self._total = 0
        self._visible = 0
        self._quarantine = None
        self._key_length = 0
        self._closed = False
        self._failed = False
        self._atom_pending = False
        self._atom_chunks: list[str] = []
        self._atom_chars = 0

    @property
    def buffered_chars(self) -> int:
        return len(self._pending) + self._atom_chars

    @property
    def total_chars(self) -> int:
        return self._total

    def _fail(self, exc):
        self._failed = True
        self._pending = ""
        self._atom_chunks = []
        self._atom_chars = 0
        self._quarantine = None
        raise exc

    def _output(self, text: str) -> str:
        if self._visible + len(text) > MAX_VISIBLE_CHARS:
            self._fail(OutputMaskLimitError())
        self._visible += len(text)
        return text

    def feed(self, text: str) -> str:
        if self._failed or self._closed:
            raise OutputMaskError("OUTPUT_MASK_CLOSED", "串流遮罩已結束，不能再加入內容")
        if type(text) is not str:
            self._fail(OutputMaskError("OUTPUT_MASK_INVALID_INPUT", "串流回答必須是文字"))
        if self._total + len(text) > MAX_TOTAL_CHARS:
            self._fail(OutputMaskLimitError())
        if self.buffered_chars + len(text) > MAX_BUFFER_CHARS:
            self._fail(OutputMaskLimitError())
        self._total += len(text)
        if self._atom_pending and (not text or _ATOM.fullmatch(text)):
            self._atom_chunks.append(text)
            self._atom_chars += len(text)
            return ""
        if self._atom_pending:
            self._pending = "".join(self._atom_chunks) + self._pending
            self._atom_chunks = []
            self._atom_chars = 0
        self._pending += text
        self._atom_pending = False
        result = self._drain(final=False)
        if self._atom_pending:
            self._atom_chunks = [self._pending]
            self._atom_chars = len(self._pending)
            self._pending = ""
        return self._output(result)

    def finish(self) -> str:
        if self._failed:
            raise OutputMaskError("OUTPUT_MASK_CLOSED", "串流遮罩已因安全限制停止")
        if self._closed:
            return ""
        if self._atom_pending:
            self._pending = "".join(self._atom_chunks) + self._pending
            self._atom_chunks = []
            self._atom_chars = 0
            self._atom_pending = False
        result = self._output(self._drain(final=True))
        self._closed = True
        return result

    def _key_end(self, length: int, *, final: bool):
        """Find assignment separator without disclosing its future value."""
        rest = self._pending[length:]
        stripped = rest.lstrip()
        if not stripped:
            return "plain" if final else "wait"
        if stripped[0] in ":=：":
            return "assignment"
        if stripped[0] in _STRUCTURED_START:
            return "structured"
        return "plain"

    @staticmethod
    def _mask_atom(atom: str) -> str:
        consecutive = 0
        for part in atom.split("."):
            consecutive = consecutive + 1 if part else 0
            if consecutive >= 3:
                break
        if atom.startswith("eyJ") or consecutive >= 3:
            return "[權杖已遮罩]"
        if _IDENTITY.search(atom):
            return "[身分證字號已遮罩]"
        if _PHONE.search(atom):
            return "[手機已遮罩]"
        return atom

    def _drain(self, *, final: bool) -> str:
        pieces = []
        while self._pending:
            if self._quarantine is not None:
                if final:
                    if self._quarantine == "assignment":
                        rest = self._pending[self._key_length :]
                        end = self._key_length + len(rest) - len(rest.lstrip()) + 1
                        while end < len(self._pending) and self._pending[end].isspace():
                            end += 1
                        pieces.append(self._pending[:end] + REDACTED)
                    elif self._quarantine == "bearer":
                        pieces.append("Bearer " + REDACTED)
                    else:
                        pieces.append(STRUCTURED_REDACTED)
                    self._pending = ""
                break
            first = self._pending[0]
            if first in _STRUCTURED_START:
                self._quarantine = "structured"
                continue
            atom_match = _ATOM.match(self._pending)
            if atom_match:
                atom = atom_match[0]
                if len(atom) == len(self._pending) and not final:
                    self._atom_pending = True
                    break
                # Every Bearer form is held conservatively, including an
                # incomplete or malformed scheme. Its token never leaves.
                if "bearer" in atom.lower():
                    self._quarantine = "bearer"
                    continue
                if _KEY.search(atom):
                    decision = self._key_end(len(atom), final=final)
                    if decision == "wait":
                        break
                    if decision != "plain":
                        self._quarantine = decision
                        self._key_length = len(atom)
                        continue
                pieces.append(self._mask_atom(atom))
                self._pending = self._pending[len(atom) :]
                continue
            full_key = next(
                (key for key in _CHINESE_KEYS if self._pending.startswith(key)), None
            )
            if full_key:
                decision = self._key_end(len(full_key), final=final)
                if decision == "wait":
                    break
                if decision != "plain":
                    self._quarantine = decision
                    self._key_length = len(full_key)
                    continue
                pieces.append(full_key)
                self._pending = self._pending[len(full_key) :]
                continue
            if not final and any(
                key.startswith(self._pending) for key in _CHINESE_KEYS
            ):
                break
            pieces.append(first)
            self._pending = self._pending[1:]
        return "".join(pieces)
