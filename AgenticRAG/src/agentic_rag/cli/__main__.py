"""``python -m agentic_rag.cli`` dispatcher.

Single subcommand for now; argparse keeps the door open for
``rotate`` / ``show`` later without breaking the CLI surface.
"""

from __future__ import annotations

import sys

from . import bootstrap as _bootstrap


def main(argv: list[str] | None = None) -> None:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        print(
            "usage: python -m agentic_rag.cli <subcommand> [args...]\n"
            "\n"
            "subcommands:\n"
            "  bootstrap   exchange a bsk- token for a csk- service token",
            file=sys.stderr,
        )
        sys.exit(2)

    sub, rest = args[0], args[1:]
    if sub == "bootstrap":
        _bootstrap.run(rest)
        return

    print(f"unknown subcommand: {sub}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
