"""Unit tests for ingestion_worker.settings.WorkerSettings.

Covers:
- Default values for every field (pydantic-settings BaseSettings).
- Env-var override via monkeypatch.setenv + a fresh WorkerSettings().
- embedding_dim default + override + type coercion.
- Type coercion for int / float / bool fields from env strings.
- case_sensitive=False → uppercase env vars map onto lowercase fields.
- The module-level ``settings`` singleton is a WorkerSettings instance.

Determinism notes
-----------------
The process may carry real env vars (DATABASE_URL, REDIS_URL, …) that would
shadow field defaults. Every default-asserting test clears the relevant vars
with monkeypatch.delenv(..., raising=False) first. We also pass
``_env_file=None`` when constructing so a stray repo ``.env`` can never leak
into the assertions. No network, time, or randomness is involved.
"""
from __future__ import annotations

import pytest

from ingestion_worker.settings import WorkerSettings, settings
from ingestion_worker.main import WorkerSettings as ArqWorkerSettings

# Every settable field and the ENV VAR that pydantic-settings maps onto it
# (case_sensitive=False → uppercased field name).
_ENV_VARS = [
    "ANILA_DEPLOYMENT_PROFILE",
    "INGESTION_QUEUE_HMAC_KEY",
    "ANILA_PILOT_MODE",
    "GATE2_ALLOW_UNCONVERGED_INFERENCE",
    "DATABASE_URL",
    "REDIS_URL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_DIM",
    "EMBEDDING_TIMEOUT_SECONDS",
    "PARSE_TIMEOUT_SECONDS",
    "INDEX_TIMEOUT_SECONDS",
    "JOB_TIMEOUT_SECONDS",
    "UPLOAD_DIR",
    "PG_POOL_MIN",
    "PG_POOL_MAX",
    "JOB_LEASE_SECONDS",
    "JOB_HEARTBEAT_SECONDS",
    "JOB_RETRY_BACKOFF_SECONDS",
    "JOB_REAPER_INTERVAL_SECONDS",
    "JOB_REAPER_BATCH_SIZE",
    "JOB_LEASE_GRACE_SECONDS",
    "ENABLE_IMAGE_CAPTIONS",
    "VISION_URL",
    "VISION_MODEL",
    "VISION_API_KEY",
    "VISION_VERIFY_SSL",
    "VISION_CONCURRENCY",
    "VISION_TIMEOUT_SECONDS",
    "VISION_MAX_IMAGE_BYTES",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every WorkerSettings-related env var so defaults are observable."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _fresh(**kwargs) -> WorkerSettings:
    """Construct WorkerSettings ignoring any on-disk .env file."""
    return WorkerSettings(_env_file=None, **kwargs)


# ── Defaults ───────────────────────────────────────────────────────────────


def test_database_url_default(clean_env):
    assert _fresh().database_url == "postgresql://csp_app:csp@csp-db:5432/csp"


def test_redis_url_default(clean_env):
    assert _fresh().redis_url == "redis://redis:6379"


def test_embedding_base_url_default(clean_env):
    assert _fresh().embedding_base_url == "http://host.docker.internal:7011/v1"


def test_embedding_model_default(clean_env):
    assert _fresh().embedding_model == "nvidia/NV-embed-V2"


def test_embedding_api_key_default(clean_env):
    assert _fresh().embedding_api_key == "not-set"


def test_embedding_dim_default(clean_env):
    s = _fresh()
    assert s.embedding_dim == 4000
    assert isinstance(s.embedding_dim, int)


def test_embedding_timeout_seconds_default(clean_env):
    s = _fresh()
    assert s.embedding_timeout_seconds == 30.0
    assert isinstance(s.embedding_timeout_seconds, float)


def test_upload_dir_default(clean_env):
    assert _fresh().upload_dir == "/var/anila/ingestion-uploads"


def test_pg_pool_defaults(clean_env):
    s = _fresh()
    assert s.pg_pool_min == 1
    assert s.pg_pool_max == 5


def test_enable_image_captions_default_true(clean_env):
    assert _fresh().enable_image_captions is True


def test_vision_url_default_empty(clean_env):
    # Empty default is the safety gate that disables captioning.
    assert _fresh().vision_url == ""


def test_vision_model_default(clean_env):
    assert _fresh().vision_model == "gemma4"


def test_vision_api_key_default(clean_env):
    assert _fresh().vision_api_key == "not-set"


def test_vision_verify_ssl_default_false(clean_env):
    assert _fresh().vision_verify_ssl is False


def test_vision_concurrency_default(clean_env):
    assert _fresh().vision_concurrency == 4


def test_vision_timeout_seconds_default(clean_env):
    s = _fresh()
    assert s.vision_timeout_seconds == 60.0
    assert isinstance(s.vision_timeout_seconds, float)


def test_vision_max_image_bytes_default(clean_env):
    # 8 MiB computed expression in the source.
    assert _fresh().vision_max_image_bytes == 8 * 1024 * 1024
    assert _fresh().vision_max_image_bytes == 8388608


def test_all_defaults_at_once(clean_env):
    """Snapshot of the full default config in one shot."""
    s = _fresh()
    assert s.model_dump() == {
        "anila_deployment_profile": "development",
        "ingestion_queue_hmac_key": "",
        "anila_pilot_mode": False,
        "gate2_allow_unconverged_inference": False,
        "database_url": "postgresql://csp_app:csp@csp-db:5432/csp",
        "redis_url": "redis://redis:6379",
        "embedding_base_url": "http://host.docker.internal:7011/v1",
        "embedding_model": "nvidia/NV-embed-V2",
        "embedding_model_fingerprint": "",
        "embedding_api_key": "not-set",
        "embedding_dim": 4000,
        "embedding_timeout_seconds": 30.0,
        "parse_timeout_seconds": 120.0,
        "index_timeout_seconds": 120.0,
        "job_timeout_seconds": 900,
        "upload_dir": "/var/anila/ingestion-uploads",
        "pg_pool_min": 1,
        "pg_pool_max": 5,
        "job_lease_seconds": 90,
        "job_heartbeat_seconds": 20,
        "job_retry_backoff_seconds": 15,
        "job_reaper_interval_seconds": 15,
        "job_reaper_batch_size": 25,
        "job_lease_grace_seconds": 15,
        "metrics_port": 8081,
        "health_probe_timeout_seconds": 2.0,
        "enable_image_captions": True,
        "vision_url": "",
        "vision_model": "gemma4",
        "vision_api_key": "not-set",
        "vision_verify_ssl": False,
        "vision_concurrency": 4,
        "vision_timeout_seconds": 60.0,
        "vision_max_image_bytes": 8 * 1024 * 1024,
        "enable_relation_llm": True,
        "relation_llm_url": "",
        "relation_llm_model": "gemma4",
        "relation_llm_api_key": "not-set",
        "relation_llm_verify_ssl": False,
        "relation_llm_timeout_seconds": 120.0,
        "relation_llm_max_chars": 12000,
        "relation_llm_max_candidates": 200,
        "enable_similarity_edges": True,
        "similarity_top_k": 3,
        "similarity_min": 0.75,
        "similarity_max_docs": 500,
        "similarity_debounce_seconds": 10.0,
        "similarity_job_poll_seconds": 1.0,
        "similarity_job_lease_seconds": 300,
        "similarity_job_heartbeat_seconds": 30,
        "similarity_job_retry_backoff_seconds": 30,
    }


# ── Env-var overrides ────────────────────────────────────────────────────────


def test_str_override_via_setenv(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
    clean_env.setenv("REDIS_URL", "redis://other:6380/2")
    clean_env.setenv("EMBEDDING_MODEL", "custom/model")
    clean_env.setenv("UPLOAD_DIR", "/tmp/uploads")
    s = _fresh()
    assert s.database_url == "postgresql://u:p@host:5432/db"
    assert s.redis_url == "redis://other:6380/2"
    assert s.embedding_model == "custom/model"
    assert s.upload_dir == "/tmp/uploads"


def test_embedding_dim_override_and_coercion(clean_env):
    clean_env.setenv("EMBEDDING_DIM", "1536")
    s = _fresh()
    assert s.embedding_dim == 1536
    assert isinstance(s.embedding_dim, int)


def test_float_override_and_coercion(clean_env):
    clean_env.setenv("EMBEDDING_TIMEOUT_SECONDS", "12.5")
    clean_env.setenv("VISION_TIMEOUT_SECONDS", "90")
    s = _fresh()
    assert s.embedding_timeout_seconds == 12.5
    assert isinstance(s.embedding_timeout_seconds, float)
    # int-looking string still coerces to float.
    assert s.vision_timeout_seconds == 90.0
    assert isinstance(s.vision_timeout_seconds, float)


def test_int_pool_override(clean_env):
    clean_env.setenv("PG_POOL_MIN", "3")
    clean_env.setenv("PG_POOL_MAX", "20")
    clean_env.setenv("VISION_CONCURRENCY", "8")
    clean_env.setenv("VISION_MAX_IMAGE_BYTES", "1048576")
    s = _fresh()
    assert s.pg_pool_min == 3
    assert s.pg_pool_max == 20
    assert s.vision_concurrency == 8
    assert s.vision_max_image_bytes == 1048576


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
    ],
)
def test_bool_override_parsing(clean_env, raw, expected):
    clean_env.setenv("ENABLE_IMAGE_CAPTIONS", raw)
    clean_env.setenv("VISION_VERIFY_SSL", raw)
    s = _fresh()
    assert s.enable_image_captions is expected
    assert s.vision_verify_ssl is expected


def test_case_insensitive_env_mapping(clean_env):
    """case_sensitive=False → lowercase env var still maps onto the field."""
    clean_env.setenv("vision_url", "https://vlm.internal/v1")
    s = _fresh()
    assert s.vision_url == "https://vlm.internal/v1"


def test_vision_url_enables_captioning_combo(clean_env):
    """Both flags must be on for captioning — assert they are independently set."""
    clean_env.setenv("VISION_URL", "https://vlm.internal/v1")
    clean_env.setenv("ENABLE_IMAGE_CAPTIONS", "true")
    s = _fresh()
    assert s.vision_url == "https://vlm.internal/v1"
    assert s.enable_image_captions is True


def test_kwarg_override_beats_default(clean_env):
    """Direct constructor kwargs override defaults (validates field is settable)."""
    s = _fresh(embedding_dim=2048, embedding_base_url="http://x/v1")
    assert s.embedding_dim == 2048
    assert s.embedding_base_url == "http://x/v1"


# ── Invalid input ────────────────────────────────────────────────────────────


def test_invalid_int_raises(clean_env):
    from pydantic import ValidationError

    clean_env.setenv("EMBEDDING_DIM", "not-an-int")
    with pytest.raises(ValidationError):
        _fresh()


def test_job_deadline_posture_rejects_heartbeat_not_shorter_than_lease(clean_env):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="heartbeat must be shorter"):
        _fresh(job_lease_seconds=60, job_heartbeat_seconds=60)


def test_job_deadline_posture_rejects_stage_timeout_at_job_timeout(clean_env):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="parse timeout must be shorter"):
        _fresh(job_timeout_seconds=120, parse_timeout_seconds=120)


# ── Module singleton ─────────────────────────────────────────────────────────


def test_module_singleton_is_worker_settings():
    assert isinstance(settings, WorkerSettings)
    # Sanity: the singleton exposes the embedding_dim contract.
    assert isinstance(settings.embedding_dim, int)


def test_arq_delegates_retries_to_durable_db_state_machine():
    assert ArqWorkerSettings.retry_jobs is False
    assert ArqWorkerSettings.max_tries == 1
    assert ArqWorkerSettings.job_timeout == settings.job_timeout_seconds
