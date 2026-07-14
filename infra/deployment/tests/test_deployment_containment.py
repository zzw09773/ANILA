"""Static regression checks for Gate 0 deployment containment.

These checks intentionally use only the Python standard library so they can
run before Docker images or application dependencies are installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def compose_service_block(document: str, service: str) -> str:
    lines = document.splitlines()
    marker = f"  {service}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"compose service not found: {service}") from exc

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


class DeploymentContainmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = read("infra/compose/platform.yml")

    def test_codeserver_is_opt_in_and_has_only_the_sandbox_mount(self) -> None:
        block = compose_service_block(self.platform, "codeserver")
        self.assertIn('profiles: ["developer-tools"]', block)
        self.assertIn(
            "../../share/codeserver-sandbox:/home/coder/workspace", block
        )
        self.assertNotIn("CODESERVER_WORKSPACE", block)
        self.assertNotIn("../..:/home/coder/workspace", block)

    def test_compose_default_excludes_codeserver_but_profile_includes_it(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker compose is not installed")

        probe = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
        )
        if probe.returncode != 0:
            self.skipTest("docker compose is not usable in this environment")

        env = os.environ.copy()
        for variable in (
            "COMPOSE_FILE",
            "COMPOSE_PROFILES",
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_ENV_FILES",
            "COMPOSE_DISABLE_ENV_FILE",
            "COMPOSE_PATH_SEPARATOR",
        ):
            env.pop(variable, None)
        env.update(
            {
                "ADMIN_PASSWORD": "test-admin-0123456789012345",
                "CARD_INITIAL_OWNERS": "990000001",
                "CARD_CRL_BUNDLE_PATH": "/etc/anila/pki/card-crl-bundle.pem",
                "CARD_CRL_SOURCE": "synthetic-inventory-feed",
                "CARD_REQUIRED_CERT_POLICY_OIDS": "1.3.6.1.4.1.55555.1.1",
                "CODESERVER_PASSWORD": "test-code-0123456789012345",
                "CSP_APP_DB_PASSWORD": "0123456789abcdef0123456789abcdef",
                "CSP_DB_PASSWORD": "abcdef0123456789abcdef0123456789",
                "CSP_SECRET_KEY": "0123456789abcdef0123456789abcdef",
                "CSP_SERVICE_TOKEN": "abcdef0123456789abcdef0123456789",
                "STUDIO_ARTIFACT_SERVICE_TOKEN": (
                    "csk-studio-artifact-containment-test"
                ),
                "STUDIO_RUNTIME_SERVICE_TOKEN": (
                    "csk-studio-runtime-containment-test"
                ),
                "STUDIO_JOB_ENVELOPE_HMAC_KEY": (
                    "studio-envelope-containment-test-0123456789abcdef"
                ),
                "INGESTION_QUEUE_HMAC_KEY": (
                    "ingestion-queue-containment-test-0123456789abcdef"
                ),
                "FLUX_AGENT_SERVICE_TOKEN": "csk-image-generator-synthetic-test",
                "INTERNAL_PLATFORM_API_KEY": (
                    "sk-internal-0123456789abcdef0123456789abcdef"
                ),
                "SITE_URL": "https://anila.inventory-check.invalid",
                "ANILA_STATE_DIR": str(ROOT.parent / ".anila-test-state"),
                "ANILA_SECRETS_DIR": str(ROOT.parent / ".anila-test-state" / "secrets"),
                "ANILA_TLS_CERTS_DIR": str(ROOT.parent / ".anila-test-state" / "tls"),
                "ANILA_DEV_TLS_CERTS_DIR": str(ROOT.parent / ".anila-test-state" / "dev-tls"),
                "CARD_CA_BUNDLE_PATH": "/etc/anila/pki/cht-synthetic-ca.pem",
                "N8N_HOST": "n8n.ai.ncsist.org.tw",
                "N8N_EDITOR_BASE_URL": "https://n8n.ai.ncsist.org.tw/",
                "N8N_WEBHOOK_URL": "https://n8n.ai.ncsist.org.tw/",
                "N8N_OWNER_EMAIL": "n8n-owner@example.invalid",
                "N8N_OWNER_PASSWORD_HASH": (
                    "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
                ),
                "N8N_ENCRYPTION_KEY": "0123456789abcdef0123456789abcdef",
                "GITLAB_HOST": "gitlab.ai.ncsist.org.tw",
                "GITLAB_ROOT_PASSWORD": "gitlab-root-test-password-0123456789",
                "CODESERVER_HOST": "code.ai.ncsist.org.tw",
                "GITLAB_SSH_BIND_IP": "10.53.100.15",
                "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
                "EMBEDDING_MODEL_FINGERPRINT": "0" * 64,
                "EMBEDDING_MODEL_FINGERPRINT_DEV": "1" * 64,
            }
        )
        inventory = read(
            "infra/deployment/intranet/platform-image-inventory.tsv"
        )
        for raw in inventory.splitlines():
            if not raw or raw.startswith("#"):
                continue
            service, image, *_ = raw.split("\t")
            variable = "ANILA_IMAGE_" + service.upper().replace("-", "_")
            env[variable] = image

        def compose_config(*args: str) -> dict[str, object]:
            result = subprocess.run(
                ["docker", "compose", *args, "config", "--format", "json"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        default = compose_config()
        default_services = default["services"]
        self.assertNotIn("codeserver", default_services)
        self.assertIn("n8n", default_services)
        self.assertIn("gitlab", default_services)
        dedicated = "csk-studio-artifact-containment-test"
        self.assertEqual(
            default_services["csp"]["environment"]["STUDIO_ARTIFACT_SERVICE_TOKEN"],
            dedicated,
        )
        self.assertEqual(
            default_services["anila-studio"]["environment"][
                "STUDIO_ARTIFACT_SERVICE_TOKEN"
            ],
            dedicated,
        )
        self.assertNotIn(
            "STUDIO_ARTIFACT_SERVICE_TOKEN",
            default_services["router"]["environment"],
        )
        n8n = default_services["n8n"]
        self.assertEqual(n8n["environment"]["N8N_HOST"], "n8n.ai.ncsist.org.tw")
        self.assertEqual(n8n["environment"]["N8N_PATH"], "/")
        self.assertEqual(n8n["image"], "n8nio/n8n:2.29.10")
        self.assertEqual(
            n8n["environment"]["N8N_INSTANCE_OWNER_MANAGED_BY_ENV"],
            "true",
        )
        self.assertEqual(
            n8n["environment"]["N8N_INSTANCE_OWNER_EMAIL"],
            "n8n-owner@example.invalid",
        )
        self.assertEqual(
            n8n["environment"]["N8N_EDITOR_BASE_URL"],
            "https://n8n.ai.ncsist.org.tw/",
        )
        gitlab = default_services["gitlab"]
        self.assertEqual(gitlab["image"], "gitlab/gitlab-ce:19.1.1-ce.0")
        self.assertIn(
            {
                "mode": "ingress",
                "host_ip": "10.53.100.15",
                "target": 22,
                "published": "2222",
                "protocol": "tcp",
            },
            gitlab["ports"],
        )
        self.assertIn(
            "gitlab_rails['gitlab_shell_ssh_port'] = 2222",
            gitlab["environment"]["GITLAB_OMNIBUS_CONFIG"],
        )
        self.assertIn(
            "external_url 'https://gitlab.ai.ncsist.org.tw'",
            gitlab["environment"]["GITLAB_OMNIBUS_CONFIG"],
        )
        self.assertNotIn(
            "gitlab.ai.ncsist.org.tw/gitlab",
            gitlab["environment"]["GITLAB_OMNIBUS_CONFIG"],
        )
        self.assertEqual(
            default_services["csp"]["environment"]["CARD_CA_BUNDLE_PATH"],
            "/etc/anila/pki/cht-synthetic-ca.pem",
        )
        csp_pki = next(
            volume
            for volume in default_services["csp"]["volumes"]
            if volume["target"] == "/etc/anila/pki"
        )
        self.assertTrue(csp_pki["read_only"])

        csp_secrets = next(
            volume
            for volume in default_services["csp"]["volumes"]
            if volume["target"] == "/app/secrets"
        )
        nginx_tls = next(
            volume
            for volume in default_services["nginx"]["volumes"]
            if volume["target"] == "/etc/nginx/certs"
        )
        self.assertEqual(
            Path(csp_secrets["source"]).resolve(),
            (ROOT.parent / ".anila-test-state" / "secrets").resolve(),
        )
        self.assertEqual(
            Path(nginx_tls["source"]).resolve(),
            (ROOT.parent / ".anila-test-state" / "tls").resolve(),
        )

        links = json.loads(default_services["csp"]["environment"]["AUTO_REGISTER_LINKS"])
        urls = {link["name"]: link["url"] for link in links}
        self.assertEqual(urls["n8n 工作流程"], "https://n8n.ai.ncsist.org.tw/")
        self.assertEqual(urls["GitLab"], "https://gitlab.ai.ncsist.org.tw/")
        self.assertEqual(
            urls["Code Server (按需)"], "https://code.ai.ncsist.org.tw/"
        )

        developer = compose_config("--profile", "developer-tools")
        codeserver = developer["services"]["codeserver"]
        self.assertEqual(codeserver["profiles"], ["developer-tools"])
        workspace = next(
            volume
            for volume in codeserver["volumes"]
            if volume["target"] == "/home/coder/workspace"
        )
        self.assertEqual(
            Path(workspace["source"]).resolve(),
            (ROOT / "share/codeserver-sandbox").resolve(),
        )

        dev = compose_config(
            "-f",
            "infra/compose/platform.yml",
            "-f",
            "infra/compose/dev.yml",
        )
        dev_links = json.loads(dev["services"]["csp"]["environment"]["AUTO_REGISTER_LINKS"])
        dev_urls = {link["name"]: link["url"] for link in dev_links}
        self.assertEqual(dev_urls["n8n 工作流程"], "https://n8n.ai.ncsist.org.tw/")
        self.assertEqual(dev_urls["GitLab"], "https://gitlab.ai.ncsist.org.tw/")
        self.assertEqual(
            dev_urls["Code Server (按需)"], "https://code.ai.ncsist.org.tw/"
        )

    def test_n8n_and_gitlab_remain_default_intranet_services(self) -> None:
        for service in ("n8n", "gitlab"):
            with self.subTest(service=service):
                block = compose_service_block(self.platform, service)
                self.assertNotIn("profiles:", block)

    def test_active_tools_are_isolated_from_core_network(self) -> None:
        nginx = compose_service_block(self.platform, "nginx")
        self.assertIn("- default", nginx)
        expected_networks = {
            "n8n": "n8n-tools",
            "gitlab": "gitlab-tools",
            "codeserver": "codeserver-tools",
        }
        for network in expected_networks.values():
            self.assertIn(f"- {network}", nginx)
        for service, network in expected_networks.items():
            with self.subTest(service=service):
                block = compose_service_block(self.platform, service)
                self.assertIn(f"networks:\n      - {network}", block)
                self.assertNotIn("networks:\n      - default", block)
                for other in expected_networks.values():
                    if other != network:
                        self.assertNotIn(f"- {other}", block)
        for service in ("csp", "redis", "ingestion-worker"):
            block = compose_service_block(self.platform, service)
            for network in expected_networks.values():
                self.assertNotIn(f"- {network}", block)

    def test_n8n_owner_bootstrap_and_unauthenticated_inputs_fail_closed(self) -> None:
        block = compose_service_block(self.platform, "n8n")
        for setting in (
            "N8N_INSTANCE_OWNER_MANAGED_BY_ENV",
            "N8N_INSTANCE_OWNER_EMAIL",
            "N8N_INSTANCE_OWNER_PASSWORD_HASH",
            "N8N_ENCRYPTION_KEY",
            "N8N_MCP_MANAGED_BY_ENV",
            "N8N_MCP_ACCESS_ENABLED",
            "N8N_BLOCK_ENV_ACCESS_IN_NODE",
            "N8N_RESTRICT_FILE_ACCESS_TO",
        ):
            self.assertIn(setting, block)
        nginx = read("infra/nginx/anila.conf")
        for route in (
            "webhook",
            "webhook-test",
            "webhook-waiting",
            "form",
            "form-test",
            "form-waiting",
            "mcp-server",
            "mcp-oauth",
            "oauth-protected-resource",
        ):
            self.assertIn(route, nginx)

    def test_tool_upgrade_and_formal_verification_are_fail_closed(self) -> None:
        script = read("infra/deployment/scripts/deploy-prod.sh")
        for token in (
            "check_tool_upgrade_safety",
            "tool-preflight",
            "unverified-volume:",
            'N8N_RUNTIME_IMAGE="${ANILA_IMAGE_N8N:-$(env_file_value ANILA_IMAGE_N8N)}"',
            'GITLAB_RUNTIME_IMAGE="${ANILA_IMAGE_GITLAB:-$(env_file_value ANILA_IMAGE_GITLAB)}"',
            "showSetupOnFirstLoad!==false",
            "GitLab main/ci background 與 schema migrations 全數完成",
            "mark_verified_tool_versions",
            "flux2-dev-agent nginx n8n gitlab",
            "running_services=(ingestion-worker)",
            "cmd_wait_healthy",
            "remaining=$(( deadline - $(date +%s) ))",
            "cmd_postconfigure",
            "ANILA_VERIFY_CA_FILE",
            '"$rc" == "52" || "$rc" == "56"',
            "cmd_verify",
        ):
            self.assertIn(token, script)
        formal_up = script[
            script.index("validate_formal_up() {") :
            script.index("# ── Subcommand: deploy")
        ]
        restart = script[
            script.index("cmd_restart() {") :
            script.index("# ── Opt-in developer tool")
        ]
        postconfigure = script[
            script.index("cmd_postconfigure() {") : script.index("cmd_verify() {")
        ]
        verify = script[script.index("cmd_verify() {") : script.index("# ── 顯示說明")]
        self.assertLess(
            formal_up.index("check_tool_upgrade_safety"),
            formal_up.index("ensure_jwt_keypair"),
        )
        self.assertLess(restart.index("validate_formal_up"), restart.index("cmd_down"))
        self.assertIn("cmd_postconfigure --prevalidated", script)
        self.assertIn("cmd_verify --prevalidated", script)
        self.assertIn("check_running_container_image_lock", postconfigure)
        self.assertIn("check_running_container_image_lock", verify)
        self.assertIn('"(unhealthy)"', script)
        self.assertIn('"(healthy)"', script)

    def test_formal_private_keys_are_external_to_repo(self) -> None:
        self.assertIn("${ANILA_SECRETS_DIR:?", self.platform)
        self.assertIn("${ANILA_TLS_CERTS_DIR:?", self.platform)
        self.assertNotIn("../../secrets:/app/secrets", self.platform)

        self.assertNotIn("../../infra/nginx/certs:/etc/nginx/certs", self.platform)
        dev_compose = read("infra/compose/dev.yml")
        self.assertIn("${ANILA_DEV_TLS_CERTS_DIR:?", dev_compose)
        self.assertIn("csp-secrets-dev:/app/secrets", dev_compose)
        self.assertIn('ALLOW_AUTO_KEYGEN: "true"', dev_compose)
        self.assertNotIn("../../infra/nginx/certs:/etc/nginx/certs", dev_compose)

        intranet = read("infra/deployment/intranet/intranet-deploy.sh")
        generic = read("infra/deployment/scripts/deploy-prod.sh")
        reissue = read("infra/deployment/scripts/reissue-tls-cert.sh")
        ops = read("infra/deployment/scripts/anila-ops.sh")
        for script in (intranet, generic, reissue, ops):
            self.assertIn("ANILA_TLS_CERTS_DIR", script)
            self.assertIn("repo", script.lower())
        self.assertIn("ANILA_SECRETS_DIR", intranet)
        self.assertIn("ANILA_SECRETS_DIR", generic)
        backup_profile = read(
            "infra/deployment/backup/production-backup-profile.v1.json"
        )
        backup_tool = read("infra/deployment/backup/production_backup.py")
        self.assertIn("production-backup.py", ops)
        self.assertIn("ANILA_BACKUP_JWT_KEY_REFERENCE", backup_profile)
        self.assertIn("ANILA_BACKUP_TLS_KEY_REFERENCE", backup_profile)
        self.assertNotIn("jwt-private.pem", backup_tool)
        self.assertNotIn("server.key", backup_tool)
        self.assertNotIn("local certs=infra/nginx/certs", ops)
        self.assertNotIn('$PWD/secrets:/out', intranet)
        self.assertNotIn('$REPO_ROOT/secrets:/out', generic)

    def test_tls_history_runbook_matches_audited_paths_and_rotates_first(self) -> None:
        audit = read("docs/security/2026-07-10-card-material-audit.md")
        runbook = read("docs/runbooks/rotate-tls-cert.md")
        for path in (
            "myCSPPlatform/docker/certs/server.key",
            "myCSPPlatform/docker/certs/server.key.bak",
        ):
            self.assertIn(path, audit)
            self.assertIn(f"--path {path}", runbook)
        self.assertLess(
            runbook.index("reissue-tls-cert.sh"),
            runbook.index("git filter-repo --invert-paths"),
        )
        self.assertIn("prod-intranet-card` 維持 deployment No-Go", runbook)
    def test_gitlab_ssh_defaults_bind_only_the_formal_lan_interface(self) -> None:
        env_example = read(".env.example")
        self.assertIn("N8N_HOST=n8n.ai.ncsist.org.tw", env_example)
        self.assertIn("GITLAB_HOST=gitlab.ai.ncsist.org.tw", env_example)
        self.assertIn("CODESERVER_HOST=code.ai.ncsist.org.tw", env_example)
        self.assertIn("GITLAB_SSH_BIND_IP=10.53.100.15", env_example)
        self.assertIn("GITLAB_SSH_PORT=2222", env_example)

    def test_intranet_deploy_pins_tool_dns_and_tls_contract(self) -> None:
        intranet = read("infra/deployment/intranet/intranet-deploy.sh")
        generic = read("infra/deployment/scripts/deploy-prod.sh")
        for name, hostname in (
            ("N8N_HOST", "n8n.ai.ncsist.org.tw"),
            ("GITLAB_HOST", "gitlab.ai.ncsist.org.tw"),
            ("CODESERVER_HOST", "code.ai.ncsist.org.tw"),
        ):
            self.assertIn(f"set_env {name}", intranet)
            self.assertIn(hostname, intranet)
            self.assertIn(hostname, generic)
        self.assertIn('openssl x509 -in "$CRT" -noout -checkhost', intranet)
        self.assertIn("N8N_EDITOR_BASE_URL", generic)
        self.assertIn("N8N_WEBHOOK_URL", generic)

    def test_codeserver_workspace_cannot_be_overridden_from_env(self) -> None:
        env_example = read(".env.example")
        self.assertNotIn("CODESERVER_WORKSPACE=", env_example)
        self.assertNotIn("${CODESERVER_WORKSPACE", self.platform)

    def test_operational_backup_default_and_override_are_outside_repo(self) -> None:
        script = read("infra/deployment/scripts/anila-ops.sh")
        self.assertNotIn('$REPO_ROOT/backups', script)
        self.assertIn("ANILA_STATE_DIR", script)
        self.assertIn("$ANILA_STATE_DIR/backups", script)
        self.assertIn(
            'assert_outside_repo "$BACKUP_DIR" ANILA_BACKUP_DIR', script
        )

    def test_intranet_env_backup_is_external_and_legacy_mount_is_removed(self) -> None:
        script = read("infra/deployment/intranet/intranet-deploy.sh")
        self.assertNotIn('cp .env ".env.bak.', script)
        self.assertIn("ANILA_ENV_BACKUP_DIR", script)
        self.assertIn("backup_existing_env", script)
        self.assertIn(
            'assert_outside_repo "$ENV_BACKUP_DIR" ANILA_ENV_BACKUP_DIR', script
        )
        self.assertIn("unset_env CODESERVER_WORKSPACE", script)

    def test_tls_archives_are_outside_the_nginx_live_mount(self) -> None:
        intranet = read("infra/deployment/intranet/intranet-deploy.sh")
        ops = read("infra/deployment/scripts/anila-ops.sh")
        reissue = read("infra/deployment/scripts/reissue-tls-cert.sh")
        for script in (intranet, ops, reissue):
            self.assertIn("tls-archive", script)
            self.assertNotIn("$TLS_CERTS_DIR/archive", script)
            self.assertNotIn("$CERTS_DIR/archive", script)
            self.assertNotIn("$certs/archive", script)

    def test_intranet_secrets_are_serialized_as_literal_dotenv_values(self) -> None:
        script = read("infra/deployment/intranet/intranet-deploy.sh")
        self.assertIn("set_env_single_quoted()", script)
        self.assertIn("get_env_unquoted()", script)
        for name in (
            "CSP_SECRET_KEY",
            "CSP_SERVICE_TOKEN",
            "STUDIO_ARTIFACT_SERVICE_TOKEN",
            "INTERNAL_PLATFORM_API_KEY",
            "ADMIN_PASSWORD",
            "CSP_DB_PASSWORD",
            "CSP_APP_DB_PASSWORD",
            "CODESERVER_PASSWORD",
            "N8N_OWNER_PASSWORD_HASH",
            "N8N_ENCRYPTION_KEY",
            "GITLAB_ROOT_PASSWORD",
            "CARD_INITIAL_OWNERS",
            "MODEL_GATEWAY_API_KEY",
        ):
            self.assertIn(f"set_env_single_quoted {name}", script)
        self.assertIn(
            "STUDIO_ARTIFACT_SERVICE_TOKEN 必須是專用 csk- Service Client token",
            script,
        )
        self.assertIn(
            "STUDIO_ARTIFACT_SERVICE_TOKEN 必須是專用 csk- Service Client token",
            read("infra/deployment/scripts/deploy-prod.sh"),
        )
        self.assertIn(
            'DEF_CIO="${CARD_INITIAL_OWNERS:-$(get_env_unquoted CARD_INITIAL_OWNERS)}"',
            script,
        )

        ops = read("infra/deployment/scripts/anila-ops.sh")
        self.assertIn("set_env_single_quoted()", ops)
        self.assertIn('set_env_single_quoted MODEL_GATEWAY_API_KEY "$key"', ops)

    def test_intranet_card_profile_requires_offline_crl_evidence(self) -> None:
        script = read("infra/deployment/intranet/intranet-deploy.sh")
        self.assertIn("share/pki/card-crl-bundle.pem", script)
        self.assertIn("BEGIN X509 CRL", script)
        self.assertIn("set_env CARD_CRL_REQUIRED true", script)
        self.assertIn(
            "set_env CARD_CRL_BUNDLE_PATH /etc/anila/pki/card-crl-bundle.pem",
            script,
        )
        self.assertIn(
            'set_env_single_quoted CARD_CRL_SOURCE "$_card_crl_source"',
            script,
        )
        self.assertIn(
            'set_env_single_quoted CARD_REQUIRED_CERT_POLICY_OIDS "$_card_policy_oids"',
            script,
        )
        self.assertIn('_defaults_card_crl_source="$_v"', script)
        self.assertIn('_defaults_card_crl_max_age="$_v"', script)
        self.assertIn('_defaults_card_policy_oids="$_v"', script)
        self.assertIn("APPROVE-PKI-PROFILE", script)
        self.assertIn("未明確核准 bundle 提供的 PKI 信任姿態", script)

    def test_developer_lifecycle_explicitly_enables_the_profile(self) -> None:
        script = read("infra/deployment/scripts/deploy-prod.sh")
        self.assertIn(
            "compose --profile developer-tools up -d --no-build --pull never codeserver",
            script,
        )
        self.assertIn(
            "compose --profile developer-tools stop codeserver", script
        )
        self.assertIn("Gate 2 pilot active set 禁止啟用", script)
        self.assertIn("codeserver-up)   cmd_codeserver_up", script)
        self.assertIn("codeserver-down) cmd_codeserver_down", script)

    def test_gate2_pilot_uses_one_authoritative_formal_compose_lifecycle(self) -> None:
        deploy = read("infra/deployment/scripts/deploy-prod.sh")
        intranet = read("infra/deployment/intranet/intranet-deploy.sh")

        self.assertIn(
            "FORMAL_COMPOSE_ARGS=(--project-name anila-platform -f infra/compose/platform.yml)",
            deploy,
        )
        self.assertIn(
            "FORMAL_COMPOSE_ARGS+=(-f infra/compose/gate2-pilot.yml)", deploy
        )
        self.assertIn('IMAGE_LOCK_POSTURE_ARGS=(--posture gate2-pilot)', deploy)
        self.assertIn('docker compose "${FORMAL_COMPOSE_ARGS[@]}" "$@"', deploy)
        self.assertIn('compose up -d --no-build --pull never', deploy)
        self.assertIn('verify_gate2_pilot_runtime_posture "$main_host"', deploy)
        for service in (
            "ingestion-worker",
            "pptx-renderer",
            "anila-studio",
            "flux2-dev-agent",
            "codeserver",
        ):
            self.assertIn(service, deploy)
        self.assertIn("Gate 2 pilot 443 Router deny", deploy)
        self.assertIn("Gate 2 pilot 4443 Router deny", deploy)
        self.assertIn("bash infra/deployment/scripts/deploy-prod.sh up", intranet)
        self.assertNotIn("\ndocker compose up -d", intranet)

    def test_e2e_does_not_assume_codeserver_is_in_default_stack(self) -> None:
        script = read("infra/deployment/scripts/phase1-e2e.sh")
        self.assertIn('ANILA_E2E_CODESERVER:-0', script)
        self.assertNotIn("ANILA_E2E_SESSION_COOKIE", script)
        self.assertIn(
            'for path in "/n8n" "/n8n/e2e" "/gitlab" "/gitlab/e2e" '
            '"/codeserver" "/codeserver/e2e"',
            script,
        )
        self.assertIn('--resolve "$host:443:127.0.0.1"', script)
        self.assertIn('if [ "$HTTP" = 404 ]', script)
        self.assertIn('"/webhook-waiting/e2e"', script)


if __name__ == "__main__":
    unittest.main()
