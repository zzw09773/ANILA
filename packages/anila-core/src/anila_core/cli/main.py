"""anila-core CLI entry point.

Usage:
    anila-core init [name] [--description TEXT] [--endpoint URL]
    anila-core register [--csp URL] [--endpoint URL] [--username USER]
                        [--base-model NAME | --base-model-id ID]
                        [--runtime-type TYPE] [--classification-level LEVEL]
                        [--version VER]
    anila-core status [--csp URL] [--username USER] [--name NAME | --id ID | --all]
    anila-core agent bootstrap …   # RETIRED (P2.1) — exits 1
    anila-core --help
"""

from __future__ import annotations

import sys


_COMMANDS = {
    "init": "anila_core.cli.init_cmd",
    "register": "anila_core.cli.register_cmd",
    "status": "anila_core.cli.status_cmd",
    "agent": "anila_core.cli.bootstrap_cmd",
}

_HELP = """\
anila-core — ANILA platform developer CLI

Commands:
  init       Scaffold a new ANILA agent project
  register   Register an agent on the ANILA CSP platform
  status     Check agent registration / approval status
  agent      RETIRED — former csk-/bsk- bootstrap (P2.1); use dispatch JWT

Run `anila-core <command> --help` for details on each command.
"""


def main() -> None:
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP)
        sys.exit(0)

    cmd = argv[0]
    rest = argv[1:]

    if cmd not in _COMMANDS:
        print(f"error: unknown command '{cmd}'\n", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        sys.exit(1)

    import importlib
    mod = importlib.import_module(_COMMANDS[cmd])
    mod.run(rest)


if __name__ == "__main__":
    main()
