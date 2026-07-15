from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
PLATFORM = ROOT / "infra" / "compose" / "platform.yml"
DEV = ROOT / "infra" / "compose" / "dev.yml"
DOCKERFILE = ROOT / "packages" / "anila-agent" / "Dockerfile"
E2E = ROOT / "infra" / "deployment" / "scripts" / "gate5-silver-e2e.sh"
SEED = ROOT / "infra" / "deployment" / "scripts" / "gate5-silver-seed.py"


class Gate5AgentComposeTests(unittest.TestCase):
    def _service(self, path: Path, name: str = "anila-agent") -> dict:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return document["services"][name]

    def test_platform_agent_is_explicitly_profiled_and_image_locked(self) -> None:
        service = self._service(PLATFORM)
        self.assertEqual(service["profiles"], ["gate5-silver"])
        self.assertEqual(
            service["image"],
            "${ANILA_IMAGE_ANILA_AGENT:?ANILA_IMAGE_ANILA_AGENT must be a locked image content ID}",
        )
        self.assertNotIn("ports", service)
        self.assertEqual(service["expose"], ["8200"])
        self.assertEqual(service["networks"], ["default"])
        self.assertEqual(service["volumes"], ["anila-agent-state:/var/lib/anila-agent"])
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertEqual(service["build"]["target"], "silver-runtime")
        self.assertEqual(service["environment"]["ANILA_BASE_URL"], "http://csp:8000/v1")

    def test_dev_agent_and_disposable_model_are_isolated(self) -> None:
        service = self._service(DEV)
        self.assertEqual(service["profiles"], ["gate5-silver"])
        self.assertNotIn("ports", service)
        self.assertEqual(service["networks"], ["default"])
        self.assertEqual(service["volumes"], ["anila-agent-state-dev:/var/lib/anila-agent"])
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        model = self._service(DEV, "gate5-e2e-model")
        self.assertEqual(model["profiles"], ["gate5-silver"])
        self.assertNotIn("ports", model)
        self.assertEqual(model["networks"], ["default"])
        self.assertEqual(model["volumes"], ["../../infra/deployment/scripts/gate5-e2e-model.py:/opt/gate5/gate5-e2e-model.py:ro"])

    def test_csp_healthchecks_bound_readiness_probe_time(self) -> None:
        for path in (DEV, PLATFORM):
            csp = self._service(path, "csp")
            test = csp["healthcheck"]["test"]
            self.assertIn("timeout=2", " ".join(test))

    def test_dev_profile_secret_is_optional_at_interpolation_but_startup_is_fail_closed(self) -> None:
        service = self._service(DEV)
        self.assertEqual(
            service["environment"]["CSP_SERVICE_TOKEN"],
            "${ANILA_AGENT_SERVICE_TOKEN_DEV:-}",
        )
        self.assertEqual(
            service["environment"]["CSP_SEARCH_TOKEN"],
            "${ANILA_AGENT_SERVICE_TOKEN_DEV:-}",
        )
        self.assertEqual(service["environment"]["ANILA_ALLOW_NO_SERVICE_TOKEN"], "0")

    def test_platform_profile_secret_is_optional_at_interpolation_but_startup_is_fail_closed(self) -> None:
        service = self._service(PLATFORM)
        self.assertEqual(
            service["environment"]["CSP_SERVICE_TOKEN"],
            "${ANILA_AGENT_SERVICE_TOKEN:-}",
        )
        self.assertEqual(
            service["environment"]["CSP_SEARCH_TOKEN"],
            "${ANILA_AGENT_SERVICE_TOKEN:-}",
        )
        self.assertEqual(service["environment"]["ANILA_ALLOW_NO_SERVICE_TOKEN"], "0")

    def test_router_restart_state_is_named_and_formal_resume_reaches_public_router(self) -> None:
        router = self._service(DEV, "router")
        self.assertEqual(router["volumes"], ["router-state-dev:/var/lib/anila-router"])
        script = E2E.read_text(encoding="utf-8")
        self.assertNotIn('export CSP_SERVICE_TOKEN="$ROUTER_TOKEN"', script)
        for marker in (
            "export ANILA_CSP_REGISTRY_SERVICE_TOKEN=\"$ROUTER_TOKEN\"",
            "compose up -d --build csp-db redis csp router gate5-e2e-model",
            "compose kill router csp anila-agent",
            "compose up -d --force-recreate --no-deps csp router anila-agent",
            "MSYS_NO_PATHCONV=1",
            "-e PYTHONPATH=/app",
            "/v1/sessions/$SESSION_ID/answer",
            "resume-by-session",
            "PUBLIC_ACCESS_TOKEN",
        ):
            self.assertIn(marker, script)

    def test_silver_target_is_non_root_service_wrapper(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim AS silver-runtime", dockerfile)
        self.assertIn("USER anila", dockerfile)
        self.assertIn('EXPOSE 8200', dockerfile)
        self.assertIn(
            'CMD ["uvicorn", "anila_agent.serving.service_wrapper:app", "--host", "0.0.0.0", "--port", "8200"]',
            dockerfile,
        )

    def test_seed_flushes_auth_session_before_refresh_fk(self) -> None:
        source = SEED.read_text(encoding="utf-8")
        parent = source.index("auth_session = AuthSession(")
        refresh = source.index("AuthRefreshToken(", parent)
        block = source[parent:refresh]
        self.assertIn("db.add(auth_session)", block)
        self.assertIn("db.flush()", block)
        self.assertLess(block.index("db.add(auth_session)"), block.index("db.flush()"))

    def test_seed_uses_unsealed_explicit_none_source_snapshot(self) -> None:
        source = SEED.read_text(encoding="utf-8")
        start = source.index("source = SourceSnapshot(")
        block = source[start:source.index("db.add(source)", start)]
        self.assertIn('origin="none"', block)
        self.assertIn('source_scope="none"', block)
        self.assertNotIn("content_hash=", block)
        self.assertNotIn("payload_ref=", block)

    def test_seed_grants_the_disposable_user_access_to_the_disposable_model(self) -> None:
        source = SEED.read_text(encoding="utf-8")
        self.assertIn("UserModelPermission", source)
        self.assertIn("UserModelPermission(user_id=user.id, model_id=model.id)", source)

    def test_e2e_harness_is_disposable_and_fail_closed(self) -> None:
        script = E2E.read_text(encoding="utf-8")
        self.assertNotIn("${3:-{}}", script)
        self.assertIn("extra_headers='{}'", script)
        self.assertEqual(
            script.count("if exc.code in {400, 403, 409, 422, 500, 502, 503}:"),
            2,
        )
        self.assertEqual(script.count("def safe_text(value):"), 2)
        self.assertEqual(script.count("if not isinstance(value, str):"), 2)
        self.assertIn("return value[:256] or None", script)
        self.assertIn("if exc.code == 422 and isinstance(detail, list):", script)
        self.assertEqual(
            script.count('safe_detail = safe_text(detail) or "detail unavailable"'), 2
        )
        self.assertIn('("loc", "type", "msg")', script)
        self.assertIn('parsed = json.loads(exc.read().decode("utf-8"))', script)
        self.assertNotIn('print(parsed', script)
        self.assertNotIn('safe_errors.append(item)', script)
        self.assertNotIn('"input"', script)
        self.assertNotIn("ensure_ascii=False", script)
        self.assertIn("ensure_ascii=True,separators=(\",\",\":\")", script)
        self.assertIn("print(json.dumps(payload, ensure_ascii=True", script)
        seed = SEED.read_text(encoding="utf-8")
        self.assertIn("print(json.dumps(context, ensure_ascii=True", seed)
        for marker in (
            "set -euo pipefail",
            "gate5-silver",
            "docker compose",
            "execution-grants/mint",
            "/internal/v1/agents/dispatch",
            "/v1/sessions/$SESSION_ID/answer",
            "docker compose kill anila-agent",
            "docker compose up -d --force-recreate --no-deps anila-agent",
            "session_events",
            "resume_attempts",
            "GATE5_E2E_KEEP",
        ):
            self.assertIn(marker, script)

    def test_e2e_harness_posts_and_reads_full_trace_over_csp_http(self) -> None:
        script = E2E.read_text(encoding="utf-8")
        for marker in (
            '"/v1/traces/$TRACE_ID/spans"',
            'csp_http_get "/api/traces/$TRACE_ID" "$PUBLIC_ACCESS_TOKEN"',
            'FULL_TRACE_ACCEPTED',
            'FULL_TRACE_DUPLICATES',
            'application/json',
            'event_binding',
            'source_snapshot_id',
            'content_sha256',
            'classification_level',
            'span.get("producer") != "proxy"',
            'parent_span_id',
        ):
            self.assertIn(marker, script)
        self.assertIn('Bearer " + os.environ["E2E_ACCESS_TOKEN"]', script)
        self.assertNotIn('trace readback via db', script.lower())


if __name__ == "__main__":
    unittest.main()
