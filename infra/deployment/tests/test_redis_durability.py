"""Static Redis Compose contract; proves no runtime durability or replay property.

These tests parse YAML only.  They do not start Redis, write data, restart a
process, measure RPO, or prove queue replay without loss or duplication.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


class RedisDurabilityContractTests(unittest.TestCase):
    def test_redis_requires_aof_everysec_and_no_eviction(self) -> None:
        for compose_path in (
            ROOT / "infra" / "compose" / "platform.yml",
            ROOT / "infra" / "compose" / "dev.yml",
        ):
            with self.subTest(compose_path=compose_path):
                payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
                redis = payload["services"]["redis"]
                command = list(redis["command"])

                self.assertEqual(command[:1], ["redis-server"])
                self.assertEqual(_argument(command, "--appendonly"), "yes")
                self.assertEqual(_argument(command, "--appendfsync"), "everysec")
                self.assertEqual(_argument(command, "--aof-use-rdb-preamble"), "yes")
                self.assertEqual(_argument(command, "--maxmemory-policy"), "noeviction")
                expected_volume = (
                    "redis-data:/data"
                    if compose_path.name == "platform.yml"
                    else "redis-data-dev:/data"
                )
                self.assertEqual(redis["volumes"], [expected_volume])

                healthcheck = redis["healthcheck"]
                self.assertEqual(healthcheck["test"][0], "CMD-SHELL")
                probe = healthcheck["test"][1]
                self.assertIn("aof_enabled:1", probe)
                self.assertIn("aof_last_write_status:ok", probe)


def _argument(command: list[str], name: str) -> str:
    index = command.index(name)
    try:
        return command[index + 1]
    except IndexError as exc:  # pragma: no cover - assertion helper guard
        raise AssertionError(f"{name} is missing its value") from exc
