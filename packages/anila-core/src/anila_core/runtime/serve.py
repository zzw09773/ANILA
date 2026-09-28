"""Replace the process with uvicorn sized from the container CPU quota.

    python -m anila_core.runtime.serve --role csp --app app.main:app --port 8000
    python -m anila_core.runtime.serve --role router --app main:app --port 9000
"""

from __future__ import annotations

import argparse
import os

from anila_core.runtime.cpu_budget import csp_worker_count, router_worker_count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start uvicorn with a CPU-sized worker count")
    parser.add_argument("--role", choices=("csp", "router"), required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)
    workers = csp_worker_count() if args.role == "csp" else router_worker_count()
    print(
        f"anila-serve role={args.role} workers={workers}",
        flush=True,
    )
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            args.app,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--workers",
            str(workers),
            "--timeout-keep-alive",
            "75",
            "--backlog",
            "2048",
            "--log-level",
            "info",
        ],
    )


if __name__ == "__main__":
    main()
