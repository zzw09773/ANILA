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


def test_deploy_generates_gitlab_root_password_like_every_other_secret():
    """gitlab crash-looped on the fresh install: 'initial_root_password: Length
    is too short' — the one-shot script generated every secret except this one."""
    text = (REPO / "infra/deployment/intranet/intranet-deploy.sh").read_text(encoding="utf-8")
    assert 'set_env GITLAB_ROOT_PASSWORD       "${GITLAB_ROOT_PASSWORD:-$(openssl rand -base64 24)}"' in text


def test_cpu_overlay_forces_a_cpu_compute_type():
    """asr-decoder crash-looped: .env.example ships ASR_COMPUTE_TYPE=float16 (for
    the GPU hosts) and the CPU overlay only defaulted when unset, so float16 won
    and ctranslate2 refused. On the CPU host the overlay must decide."""
    text = CPU_OVERLAY.read_text(encoding="utf-8")
    assert "ASR_COMPUTE_TYPE: int8" in text
    assert "${ASR_COMPUTE_TYPE" not in text
