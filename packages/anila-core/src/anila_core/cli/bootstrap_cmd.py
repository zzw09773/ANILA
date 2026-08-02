"""anila-core agent bootstrap — RETIRED (P2.1).

The ``bsk-`` → ``csk-`` exchange and long-lived agent service tokens
are gone. Dispatch identity is the platform-minted 5-minute RS256 JWT
verified via JWKS. See ``docs/guides/developer-guide.md``.
"""

from __future__ import annotations

import sys

_RETIRED = (
    "error: `anila-core agent bootstrap` 已廢止 (P2.1)。\n"
    "agent 不再領取長效 csk-/bsk-；派工身分改走 5 分鐘 RS256 JWT＋JWKS。\n"
    "請見 docs/guides/developer-guide.md。"
)


def run(args: list[str]) -> None:
    """Refuse every ``anila-core agent …`` invocation (including --help)."""
    if args and args[0] in ("-h", "--help"):
        print(
            "anila-core agent — RETIRED (P2.1)\n"
            "Former subcommand `bootstrap` (bsk- → csk-) is gone.\n"
            "Use dispatch JWT + JWKS; see docs/guides/developer-guide.md."
        )
        sys.exit(0)
    print(_RETIRED, file=sys.stderr)
    sys.exit(1)
