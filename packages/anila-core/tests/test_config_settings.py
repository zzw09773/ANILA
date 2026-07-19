from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
KNOWN_SETTING_ENV_KEYS = frozenset(
    {
        "allow_legacy_agent_dispatch",
        "api_dev_mode",
        "api_key",
        "cookie_secure",
        "csp_agent_service_token",
        "csp_api_key",
        "csp_base_url",
        "csp_inference_service_token",
        "csp_jwks_url",
        "csp_registry_service_token",
        "csp_service_token",
        "llm_api_key",
        "llm_url",
        "model",
        "router_context_issuer",
        "session_db_path",
    }
)


def _run_config_code(
    tmp_path: Path,
    code: str,
    *,
    dotenv: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in KNOWN_SETTING_ENV_KEYS
    }
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(PACKAGE_SRC), existing_pythonpath) if value
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_monorepo_superset_dotenv_unknown_key_is_ignored_without_echo(
    tmp_path: Path,
) -> None:
    marker = "synthetic-a1-unknown-value-must-not-echo"

    result = _run_config_code(
        tmp_path,
        "import anila_core.config; print('IMPORT_OK')",
        dotenv=f"ANILA_SUPERSET_UNKNOWN={marker}\n",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "IMPORT_OK"
    assert marker not in result.stdout
    assert marker not in result.stderr


def test_programmatic_unknown_setting_fails_closed(tmp_path: Path) -> None:
    result = _run_config_code(
        tmp_path,
        "from anila_core.config import Settings\n"
        "from pydantic import ValidationError\n"
        "try:\n"
        "    Settings(cookie_securee=False, _env_file=None)\n"
        "except ValidationError as exc:\n"
        "    print(exc.errors()[0]['type'])\n"
        "else:\n"
        "    raise SystemExit('unknown init setting was accepted')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "extra_forbidden"


def test_known_setting_invalid_type_still_fails_closed(tmp_path: Path) -> None:
    result = _run_config_code(
        tmp_path,
        "from anila_core.config import Settings\n"
        "from pydantic import ValidationError\n"
        "try:\n"
        "    Settings(api_dev_mode='not-a-valid-boolean', _env_file=None)\n"
        "except ValidationError as exc:\n"
        "    print(exc.errors()[0]['type'])\n"
        "else:\n"
        "    raise SystemExit('invalid known setting was accepted')\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bool_parsing"


def test_known_dotenv_fields_and_case_insensitive_env_names_still_load(
    tmp_path: Path,
) -> None:
    result = _run_config_code(
        tmp_path,
        "from anila_core.config import Settings\n"
        "value = Settings()\n"
        "print(value.llm_url)\n"
        "print(value.cookie_secure)\n",
        dotenv=(
            "LLM_URL=https://synthetic-dotenv.example/v1\n"
            "COOKIE_SECURE=false\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "https://synthetic-dotenv.example/v1",
        "False",
    ]


def test_dotenv_source_customisation_uses_only_legacy_supported_attributes(
    tmp_path: Path,
) -> None:
    result = _run_config_code(
        tmp_path,
        "from anila_core.config import Settings\n"
        "from pydantic_settings import DotEnvSettingsSource\n"
        "dotenv = DotEnvSettingsSource(Settings, env_file=None)\n"
        "for name in ('env_ignore_empty', 'env_parse_none_str', 'env_parse_enums'):\n"
        "    if hasattr(dotenv, name):\n"
        "        delattr(dotenv, name)\n"
        "sentinel = lambda: {}\n"
        "sources = Settings.settings_customise_sources(\n"
        "    Settings, sentinel, sentinel, dotenv, sentinel\n"
        ")\n"
        "print(type(sources[2]).__name__)\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "_KnownFieldsDotEnvSettingsSource"
