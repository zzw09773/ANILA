"""The one-shot deploy must not hard-code a csp image name from another project.

Fresh-install rehearsal 2026-09-02 stopped at [4b/7]: intranet-deploy.sh ran
``anila-platform-csp:latest`` to generate the JWT keypair, but the bundle (and
compose, project ``anila-restart``) ships ``anila-restart-csp``. The pull was
refused and the whole install died. fix-runtime-ownership.sh had the same
default. Both must derive the image from COMPOSE_PROJECT_NAME.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = (
    REPO / "infra/deployment/intranet/intranet-deploy.sh",
    REPO / "infra/deployment/scripts/fix-runtime-ownership.sh",
)


def test_no_stale_project_image_name_in_deploy_scripts():
    offenders = []
    for path in SCRIPTS:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "anila-platform-csp" in code:
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_csp_image_derives_from_compose_project_name():
    for path in SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "${COMPOSE_PROJECT_NAME" in text and "-csp:latest" in text, path.name


# ── 2026-09-02 fresh-install rehearsal, two more stops on a CPU host ─────────

CPU_OVERLAY = REPO / "infra/compose/asr-cpu.yml"


def test_gitlab_is_absent_while_n8n_and_codeserver_stay():
    """2026-09-26 擁有者裁定先拿掉 GitLab。服務、volume、GITLAB_* 與 /gitlab
    location 都不再宣告；n8n 與 codeserver 維持原樣。主機上的舊 volume 不在
    這份測試裡刪。"""
    import re

    import yaml

    platform = (REPO / "infra/compose/platform.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(platform)
    services = set((doc.get("services") or {}))
    volumes = set((doc.get("volumes") or {}))
    assert "gitlab" not in services
    assert "n8n" in services
    assert "codeserver" in services
    assert not any(name.startswith("gitlab") for name in volumes)
    assert "n8n_data" in volumes
    assert "codeserver_config" in volumes
    assert "GITLAB_" not in platform

    deploy = (REPO / "infra/deployment/intranet/intranet-deploy.sh").read_text(encoding="utf-8")
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "GITLAB_" not in deploy
    assert "GITLAB_" not in example

    nginx = (REPO / "infra/nginx/anila.conf").read_text(encoding="utf-8")
    assert not re.search(r"location\s+/gitlab/?", nginx)
    assert "gitlab:8181" not in nginx
    assert nginx.count("location /n8n") >= 1
    assert nginx.count("location /codeserver") >= 1
    # 兩條 TLS listener 都要留 n8n／codeserver，且都不再有 /gitlab。
    assert nginx.count("location /n8n") == 2
    assert nginx.count("location /codeserver") == 2


def test_cpu_overlay_forces_a_cpu_compute_type():
    """asr-decoder crash-looped: .env.example ships ASR_COMPUTE_TYPE=float16 (for
    the GPU hosts) and the CPU overlay only defaulted when unset, so float16 won
    and ctranslate2 refused. On the CPU host the overlay must decide."""
    text = CPU_OVERLAY.read_text(encoding="utf-8")
    assert "ASR_COMPUTE_TYPE: int8" in text
    assert "${ASR_COMPUTE_TYPE" not in text
