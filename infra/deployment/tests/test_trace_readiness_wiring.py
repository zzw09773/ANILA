"""Static deployment contract for Router Full Trace readiness."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


class TraceReadinessWiringTests(unittest.TestCase):
    def test_formal_compose_injects_trace_endpoint_and_checks_ready(self) -> None:
        compose = (REPO_ROOT / "infra" / "compose" / "platform.yml").read_text(
            encoding="utf-8"
        )
        router_start = compose.index("  router:\n")
        nginx_start = compose.index("  nginx:\n", router_start)
        router = compose[router_start:nginx_start]

        self.assertIn("ANILA_ENV:", router)
        self.assertIn("ANILA_TRACE_ENDPOINT:", router)
        self.assertIn("http://localhost:9000/ready", router)


if __name__ == "__main__":
    unittest.main()
