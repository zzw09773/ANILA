# -*- coding: utf-8 -*-
"""設定登錄表＋泛化存取層 —— 把「畫面上的值」與「真正生效的值」釘成同一個。

這一支測的不是某一顆設定，是**設定頁那整面牆的地基**。門檻
（``institutional_kb.score_threshold``）已經證明過一顆設定該長什麼樣；本檔要
證明的是「那個形狀被泛化之後沒有在路上掉東西」：

1. **登錄表沒有孤兒**：csp 這個行程讀得到的 95 個環境變數，一顆不漏地在登錄表
   裡被宣告過類別。少宣告一顆 = 設定頁上少一列 = 管理員又多一個看不見的開關，
   而那正是本包存在的理由（57 顆今天是隱形的）。
2. **收得下來的 ＝ 算得出來的**：每一顆的值域函式**寫入端與解析端是同一個函式
   物件**。`6666fbc8` 的教訓是兩端各寫一份時 19 支測試全綠、而管理員存的值被
   靜默丟掉。這裡用「換掉登錄表那一顆的 domain_fn，兩端都要跟著變」來釘。
3. **回退鏈三態**：DB 有列 → env（若設）→ 程式預設，而 ``source`` 必須誠實。
   壞值（繞過 API 寫進 DB 的）退回下一層時，``source`` **絕不可以**還說 ``db``。
4. **env 字串的解讀規則逐顆不同**：url_guard 的旗標是 `== "1"`、
   ``ANILA_ZH_NORMALIZE`` 是 `!= "0"`、``PDF_OCR_FALLBACK`` 是
   `.lower() == "true"`、config.py 那 53 顆走 pydantic。用一套通用 bool 解析
   會讓 ``ANILA_ALLOW_HTTP_ENDPOINT=true`` 在畫面上顯示「已開啟」而 SSRF guard
   實際上是關的 —— 那就是本包要消滅的形狀，出現在最不該出現的地方。
5. **內插值來回釘**：不是只測邊界拒絕，是 ``set → DB 那一列 → get`` 走下界／
   中間值（0.375 型）／上界三點。

⚠ **期望名單刻意寫死在本檔**，不是去讀 `env-recon.md`：那份盤點刻意不進 repo
（內含尚未修補的弱點位置，repo 是 PUBLIC），測試不能依賴一個不會被 checkout
出來的檔案。本檔的名單來源是該份盤點的 §2 逐變數表，數字對得上 §0 的計數規則。
"""

from __future__ import annotations

import ast
import dataclasses
import os
import pathlib
import re

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from anila_core.ingestion.ocr import _DEFAULT_VISION_PROMPT
from app.config import Settings
from app.models.platform_setting import (
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_KEY,
    PlatformSetting,
    SOURCE_DB,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    _is_usable_kb_threshold,
    get_setting,
    resolve_kb_threshold,
    resolve_setting,
    set_setting,
)
from app.services import settings_registry as reg
from app.services.settings_registry import (
    EDITABLE_CLASSES,
    REGISTRY,
    SETTINGS,
    SettingClass,
    UnknownSettingError,
    require_spec,
)

# ── 期望名單（來源：2026-08-08 env 盤點 §2，逐變數表） ──────────────────────

# §2.1 — app/config.py 的 pydantic 欄位，讀取點一律 config.py:215。
_TIER_1A_CONFIG_FIELDS = frozenset({
    "APP_NAME", "APP_VERSION", "DEBUG", "DATABASE_URL", "SECRET_KEY", "ALGORITHM",
    "ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS",
    "JWT_PRIVATE_KEY_PATH", "JWT_PUBLIC_KEY_PATH", "JWT_KID", "ALLOW_AUTO_KEYGEN",
    "ADMIN_USERNAME", "ADMIN_PASSWORD", "EMBEDDING_TIMEOUT", "LLM_TIMEOUT",
    "MODEL_GATEWAY_API_KEY", "PROXY_MAX_RETRIES", "PROXY_RETRY_BASE_DELAY",
    "HEALTH_CHECK_INTERVAL", "ALERT_CHECK_INTERVAL",
    "ANILA_ALERT_SMTP_ENABLED", "ANILA_ALERT_SMTP_HOST", "ANILA_ALERT_SMTP_PORT",
    "ANILA_ALERT_SMTP_USER", "ANILA_ALERT_SMTP_PASSWORD", "ANILA_ALERT_SMTP_FROM",
    "ANILA_ALERT_SMTP_TO", "ANILA_ALERT_SMTP_USE_TLS",
    "USAGE_BATCH_SIZE", "USAGE_FLUSH_INTERVAL", "CSP_SERVICE_TOKEN", "SITE_URL",
    "ALLOWED_ORIGINS", "ALLOWED_HOSTS", "COOKIE_SECURE", "STATIC_DIR",
    "AUTO_REGISTER_MODELS", "AUTO_REGISTER_AGENTS", "AUTO_SEED_API_KEYS",
    "ATTACHMENT_STORAGE_PATH", "ANILA_DEPARTMENT_MAX_DEPTH",
    "ANILA_MESSAGE_MAX_SIBLINGS", "ANILA_ACTION_INVOKE_PER_MIN",
    "ANILA_ACTION_MAX_BODY_CHARS", "ANILA_DEFAULT_CONTEXT_WINDOW",
    "ANILA_ATTACHMENT_BUDGET_RATIO", "ANILA_ATTACHMENT_TOKEN_SAFETY",
    "ANILA_ATTACHMENT_MAX_STORED_TOKENS", "AUTO_REGISTER_LINKS",
    "ENABLE_CARD_LOGIN", "REQUIRE_CARD_LOGIN_ONLY", "CARD_INITIAL_OWNERS",
})

# §2.2 — app/ 內原生 os.environ 讀取（扣掉與 §2.1 重疊的三個）。
_TIER_1B_NATIVE_ENVIRON = frozenset({
    "ANILA_ZH_NORMALIZE", "ANILA_QUERY_EXPANSION", "ANILA_ZIP_FILENAME_ENC",
    "ANILA_ALLOW_DEV_SECRET", "ANILA_TRUSTED_HOSTS", "ANILA_TEMPLATE_DIR",
    "CARD_CA_BUNDLE_PATH", "CARD_DEV_TRUST_TEST_CA", "CARD_DEV_SKIP_NONCE_BINDING",
    "MIGRATION_DATABASE_URL", "INGESTION_UPLOAD_DIR", "REDIS_URL",
    "TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS",
    "MEMORY_RETRIEVE_TOP_K", "MEMORY_RETRIEVE_MIN_COSINE", "MEMORY_MAX_CHUNK_CHARS",
    "MEMORY_LLM_MODEL", "MEMORY_HTTP_TIMEOUT",
})

# §2.3 — grep 名稱抓不到的間接讀取（泛型 helper／常數轉指／CPython runtime）。
_TIER_1C_INDIRECT = frozenset({
    "INTERNAL_PLATFORM_API_KEY", "CODESERVER_PASSWORD", "ANILA_HOST",
    "LEGACY_SQLITE_PATH", "CSP_APP_DB_PASSWORD", "SSL_CERT_FILE", "PYTHONUNBUFFERED",
})

# §2.4 — anila_core 在 csp 行程內讀的（ANILA_TRUSTED_HOSTS 已計於 §2.2，不重複）。
_TIER_2_ANILA_CORE = frozenset({
    "ANILA_ALLOW_HTTP_ENDPOINT", "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
    "ANILA_ALLOW_GRPC_ENDPOINT", "ANILA_ALLOW_PRIVATE_ENDPOINT", "ANILA_ENV",
    "CSP_SECRET_KEY", "PDF_OCR_FALLBACK", "VISION_URL", "VISION_MODEL",
    "VISION_API_KEY", "PDF_OCR_VISION_PROMPT", "PDF_OCR_DPI", "PDF_OCR_CONCURRENCY",
    "PDF_OCR_MAX_PAGES", "VISION_VERIFY_SSL", "DOC_PARSER", "DOCLING_OCR_LANGS",
})

EXPECTED_ENV_NAMES = (
    _TIER_1A_CONFIG_FIELDS
    | _TIER_1B_NATIVE_ENVIRON
    | _TIER_1C_INDIRECT
    | _TIER_2_ANILA_CORE
)

# §2.6 — 進了容器 env 但 csp 全樹零讀取點的假控制項。設定頁**永不**顯示它們，
# 所以它們也不可以出現在登錄表裡（Task 7 會把它們從 compose 拿掉）。
DEAD_ENV_NAMES = frozenset({
    "FLUX_BACKEND_URL", "FLUX_MAX_CONCURRENT", "FLUX_TIMEOUT_SECONDS",
})

# 設計 §3.1 逐字的 19 顆 C。
EXPECTED_C_ENV_NAMES = frozenset({
    "ANILA_ZH_NORMALIZE", "ANILA_QUERY_EXPANSION", "ANILA_ZIP_FILENAME_ENC",
    "ANILA_MESSAGE_MAX_SIBLINGS", "ANILA_DEPARTMENT_MAX_DEPTH",
    "ANILA_ATTACHMENT_BUDGET_RATIO", "ANILA_ATTACHMENT_TOKEN_SAFETY",
    "ANILA_DEFAULT_CONTEXT_WINDOW", "ANILA_ATTACHMENT_MAX_STORED_TOKENS",
    "ANILA_ACTION_MAX_BODY_CHARS",
    "ANILA_ACTION_INVOKE_PER_MIN", "LLM_TIMEOUT", "EMBEDDING_TIMEOUT",
    "PROXY_MAX_RETRIES", "PROXY_RETRY_BASE_DELAY",
    "MEMORY_RETRIEVE_TOP_K", "MEMORY_RETRIEVE_MIN_COSINE", "MEMORY_MAX_CHUNK_CHARS",
    "MEMORY_HTTP_TIMEOUT",
})

#: fix round 1（I1，控制方裁定）：這三顆**不在**設計 §3.2 的具名清單裡，但它們與
#: 那六顆一起被讀在 ``build_ocr_backend_from_env()``（ocr.py:367-387）**同一個函式**
#: 內，主要消費者同樣是 ingestion-worker（另一行程，讀不到 csp DB）。留在 B-可編輯
#: 等於在一個「其餘六個輸入全鎖住」的建構器上開放三個輸入可改 —— 改了不會生效，
#: 正是設計 §6.7 列的第一種形狀。
#: ⚠ env 名是**不帶前綴**的 ``VISION_URL``／``VISION_MODEL``，不是 ``PDF_OCR_VISION_*``。
_OCR_TRIO = frozenset({"PDF_OCR_FALLBACK", "VISION_URL", "VISION_MODEL"})

# 設計 §3.2 具名排除（＋ I1 的三顆）→ B-鎖定。
EXPECTED_B_LOCKED_ENV_NAMES = frozenset({
    "MEMORY_LLM_MODEL",
    "ANILA_ALERT_SMTP_ENABLED", "ANILA_ALERT_SMTP_HOST", "ANILA_ALERT_SMTP_PORT",
    "ANILA_ALERT_SMTP_USER", "ANILA_ALERT_SMTP_FROM", "ANILA_ALERT_SMTP_TO",
    "ANILA_ALERT_SMTP_USE_TLS",
    "PDF_OCR_DPI", "PDF_OCR_CONCURRENCY", "PDF_OCR_MAX_PAGES",
    "PDF_OCR_VISION_PROMPT", "DOC_PARSER", "DOCLING_OCR_LANGS",
    "INGESTION_UPLOAD_DIR", "REDIS_URL",
} | _OCR_TRIO)

# 盤點 §1 的 13 顆 SECRET。
EXPECTED_A_ENV_NAMES = frozenset({
    "DATABASE_URL", "MIGRATION_DATABASE_URL", "CSP_APP_DB_PASSWORD", "SECRET_KEY",
    "CSP_SECRET_KEY", "ADMIN_PASSWORD", "CSP_SERVICE_TOKEN", "MODEL_GATEWAY_API_KEY",
    "AUTO_SEED_API_KEYS", "INTERNAL_PLATFORM_API_KEY", "CODESERVER_PASSWORD",
    "ANILA_ALERT_SMTP_PASSWORD", "VISION_API_KEY",
})

# 盤點 §3 的 20 條 ＋ 加映 3 條 ＋ 設計 §3.2 把票期兩顆歸進來 = 25。
EXPECTED_SEC_ENV_NAMES = frozenset({
    "ALGORITHM", "JWT_PRIVATE_KEY_PATH", "JWT_PUBLIC_KEY_PATH", "JWT_KID",
    "ALLOW_AUTO_KEYGEN", "ALLOWED_ORIGINS", "ALLOWED_HOSTS", "COOKIE_SECURE",
    "ENABLE_CARD_LOGIN", "REQUIRE_CARD_LOGIN_ONLY", "CARD_INITIAL_OWNERS",
    "CARD_CA_BUNDLE_PATH", "CARD_DEV_TRUST_TEST_CA", "CARD_DEV_SKIP_NONCE_BINDING",
    "ANILA_TRUSTED_HOSTS", "ANILA_ALLOW_HTTP_ENDPOINT",
    "ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "ANILA_ALLOW_GRPC_ENDPOINT",
    "ANILA_ALLOW_PRIVATE_ENDPOINT", "ANILA_ENV",
    "SSL_CERT_FILE", "VISION_VERIFY_SSL", "ANILA_ALLOW_DEV_SECRET",
    "ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS",
})

# 盤點 §6 逐組列出的「真正歧義」變數。每一顆都必須被設計 §3.1／§3.2 具名裁定過，
# 沒有一顆可以留在「未裁定」狀態偷偷變成可編輯。
AMBIGUOUS_ENV_NAMES = (
    EXPECTED_C_ENV_NAMES
    | (EXPECTED_B_LOCKED_ENV_NAMES - _OCR_TRIO)
    | {"ANILA_TRUSTED_HOSTS", "ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS"}
)

# 讀取點是 per-call（改了不必重啟就會被下一次呼叫看到）的那些。其餘一律 boot 讀取。
# 逐顆對盤點 §2 的 when-read 欄；兩處刻意與該欄不同，理由寫在登錄表的說明欄：
#   * CARD_CA_BUNDLE_PATH —— card_auth.py 的 ``_ca_anchor_cache`` 是行程級快取，
#     第一次驗章之後改 env 不再生效 → 需重啟。
#   * ANILA_TRUSTED_HOSTS —— 兩個讀取點（開機 backfill ＋ url_guard per-call），
#     取保守值 → 需重啟。
EXPECTED_NO_RESTART_KEYS = frozenset({
    # C 類：搬進 platform_settings 之後每請求讀 DB。
    "intl.zh_normalize", "intl.query_expansion", "intl.zip_filename_encoding",
    "limits.message_max_siblings", "limits.department_max_depth",
    "limits.attachment_budget_ratio", "limits.attachment_token_safety",
    "limits.default_context_window", "limits.attachment_max_stored_tokens",
    "limits.action_max_body_chars", "limits.action_invoke_per_min",
    "proxy.llm_timeout", "proxy.embedding_timeout", "proxy.max_retries",
    "proxy.retry_base_delay",
    "memory.retrieve_top_k", "memory.retrieve_min_cosine", "memory.max_chunk_chars",
    "memory.http_timeout",
    "institutional_kb.score_threshold",
    # 非 C 但讀取點本來就是 per-call 的。
    "network.allow_http_model_endpoint", "network.allow_http_agent_endpoint",
    "network.allow_grpc_endpoint", "network.allow_private_endpoint",
    "network.environment",
    "card.dev_trust_test_ca",
    "auth.secret_key_fallback",
    "ingestion.pdf_ocr_fallback", "ingestion.vision_url", "ingestion.vision_model",
    "ingestion.vision_api_key", "ingestion.vision_verify_ssl",
    "ingestion.pdf_ocr_vision_prompt", "ingestion.pdf_ocr_dpi",
    "ingestion.pdf_ocr_concurrency", "ingestion.pdf_ocr_max_pages",
    "ingestion.doc_parser", "ingestion.docling_ocr_langs",
})

# 內插值來回釘的三點（下界／中間值／上界）＋ 兩個必須被拒的界外值。
# 「0.375 型」不是裝飾：只測邊界的測試對「解析端把上界改成開區間」是全綠的。
INTERIOR_ROUND_TRIP = {
    "limits.attachment_budget_ratio": (0.0, 0.375, 1.0, -0.1, 1.1),
    "limits.attachment_token_safety": (1.0, 1.375, 4.0, 0.9, 4.1),
    "limits.message_max_siblings": (1, 37, 1000, 0, 1001),
    "limits.department_max_depth": (1, 4, 10, 0, 11),
    "limits.default_context_window": (256, 65537, 10_000_000, 255, 10_000_001),
    "limits.attachment_max_stored_tokens": (1, 123_457, 100_000_000, 0, 100_000_001),
    "limits.action_max_body_chars": (1, 4097, 1_000_000, 0, 1_000_001),
    "limits.action_invoke_per_min": (1, 37, 10_000, 0, 10_001),
    "proxy.llm_timeout": (1, 137, 3600, 0, 3601),
    "proxy.embedding_timeout": (1, 45, 3600, 0, 3601),
    "proxy.max_retries": (0, 4, 10, -1, 11),
    "proxy.retry_base_delay": (0.0, 0.375, 60.0, -0.1, 60.1),
    "memory.retrieve_top_k": (1, 7, 100, 0, 101),
    "memory.retrieve_min_cosine": (0.0, 0.375, 1.0, -0.1, 1.1),
    "memory.max_chunk_chars": (1, 1201, 100_000, 0, 100_001),
    "memory.http_timeout": (1.0, 30.5, 3600.0, 0.9, 3600.1),
    "institutional_kb.score_threshold": (0.0, 0.375, 1.0, -0.1, 1.1),
}

# C 類裡不是數值鈕的那三顆，各自的合法值與必須被拒的值。
NON_NUMERIC_C_CASES = {
    "intl.zh_normalize": ([True, False], []),
    "intl.query_expansion": ([True, False], []),
    "intl.zip_filename_encoding": (["", "cp950", "gbk", "utf-8"], ["not-a-codepage"]),
}


def _env_backed() -> list:
    return [s for s in SETTINGS if s.env_name is not None]


def _by_env_name(name: str):
    matches = [s for s in SETTINGS if s.env_name == name]
    assert len(matches) == 1, f"{name} 在登錄表出現 {len(matches)} 次"
    return matches[0]


# ── 1. 登錄表完整性：對盤點做集合運算，不是逐條看 ──────────────────────────


def test_registry_declares_every_variable_the_csp_process_reads():
    declared = {s.env_name for s in _env_backed()}
    assert declared - EXPECTED_ENV_NAMES == set(), "登錄表宣告了 csp 讀不到的變數"
    assert EXPECTED_ENV_NAMES - declared == set(), "有變數沒被宣告 —— 設定頁上會少一列"
    assert len(declared) == 95


def test_dead_variables_are_not_in_the_registry():
    declared = {s.env_name for s in _env_backed()}
    assert declared & DEAD_ENV_NAMES == set(), "零讀取點的假控制項不可以被畫成活的"


def test_class_census_matches_the_rulings():
    by_class = {}
    for spec in _env_backed():
        by_class.setdefault(spec.setting_class, set()).add(spec.env_name)

    assert by_class[SettingClass.C] == EXPECTED_C_ENV_NAMES
    assert by_class[SettingClass.B_LOCKED] == EXPECTED_B_LOCKED_ENV_NAMES
    assert by_class[SettingClass.A] == EXPECTED_A_ENV_NAMES
    assert by_class[SettingClass.SEC] == EXPECTED_SEC_ENV_NAMES

    # B-可編輯 = 剩下的全部（設計 §3.2：「其餘 B 全數可編輯」）。
    assert by_class[SettingClass.B_EDIT] == (
        EXPECTED_ENV_NAMES
        - EXPECTED_C_ENV_NAMES
        - EXPECTED_B_LOCKED_ENV_NAMES
        - EXPECTED_A_ENV_NAMES
        - EXPECTED_SEC_ENV_NAMES
    )
    assert (
        len(by_class[SettingClass.C]),
        len(by_class[SettingClass.B_EDIT]),
        len(by_class[SettingClass.B_LOCKED]),
        len(by_class[SettingClass.SEC]),
        len(by_class[SettingClass.A]),
    ) == (19, 19, 19, 25, 13)


def test_every_ambiguous_variable_was_explicitly_ruled_on():
    """盤點 §6 列為歧義的每一顆，都必須落在 C／B-鎖定／SEC 其中之一。

    留在 B-可編輯 = 沒有人裁定過卻預設可改，那是「保守鎖定」規則要擋的狀態。
    """
    for name in AMBIGUOUS_ENV_NAMES:
        spec = _by_env_name(name)
        assert spec.setting_class is not SettingClass.B_EDIT, (
            f"{name} 是盤點列為歧義的變數，卻落在 B-可編輯"
        )


def test_every_input_of_the_ocr_builder_is_locked_together():
    """``build_ocr_backend_from_env()`` 讀的九顆，一顆都不可編輯（fix round 1 I1）。

    在一個「六個輸入鎖住、三個輸入可改」的建構器上開放編輯，等於在畫面上承諾一件
    改了不會生效的事 —— 那個函式的主要消費者是 ingestion-worker，另一個行程，
    讀不到 csp 的 ``platform_settings``。這一支釘的是**整組**，不是那三顆，
    所以往後往那個函式加一顆新的 env 也逃不掉。
    """
    ocr_builder_inputs = {
        "PDF_OCR_FALLBACK", "VISION_URL", "VISION_MODEL", "VISION_API_KEY",
        "PDF_OCR_VISION_PROMPT", "PDF_OCR_DPI", "PDF_OCR_CONCURRENCY",
        "PDF_OCR_MAX_PAGES", "VISION_VERIFY_SSL",
    }
    for name in sorted(ocr_builder_inputs):
        spec = _by_env_name(name)
        assert spec.setting_class not in EDITABLE_CLASSES, (
            f"{name} 被 build_ocr_backend_from_env() 讀，卻宣告成可編輯"
        )


def test_entry_shape_is_complete():
    for spec in SETTINGS:
        assert spec.key and isinstance(spec.key, str)
        assert isinstance(spec.setting_class, SettingClass)
        assert callable(spec.domain_fn)
        assert isinstance(spec.restart_required, bool)
        assert spec.description.strip(), f"{spec.key} 沒有說明文字（要上畫面）"
        if spec.setting_class in (SettingClass.B_LOCKED, SettingClass.SEC, SettingClass.A):
            assert spec.locked_reason.strip(), f"{spec.key} 是不可編輯類別卻沒有鎖定理由"
        else:
            assert spec.locked_reason == "", f"{spec.key} 可編輯卻帶著鎖定理由"


def test_keys_and_env_names_are_unique_and_namespaced():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys))
    env_names = [s.env_name for s in _env_backed()]
    assert len(env_names) == len(set(env_names))
    for spec in SETTINGS:
        assert spec.key == spec.key.lower()
        assert "." in spec.key, f"{spec.key} 不是點號命名空間（設定頁靠前綴分群）"
        assert len(spec.key) <= 120, f"{spec.key} 超過 platform_settings.key 的欄寬"


def test_every_default_passes_its_own_domain_fn():
    for spec in SETTINGS:
        assert spec.domain_fn(spec.default) is True, (
            f"{spec.key} 的預設值 {spec.default!r} 過不了自己的值域函式"
        )


def test_every_default_survives_a_format_parse_round_trip():
    """預設值寫進 DB 再讀回來必須是同一個值。

    format 與 parse 對不上（例如旗標是 `== "1"` 卻存成 "true"）時，管理員存的
    「開啟」會被讀成「關閉」，而且不會有錯誤訊息。
    """
    for spec in SETTINGS:
        rendered = spec.value_type.format(spec.default)
        assert isinstance(rendered, str)
        assert spec.value_type.parse(rendered) == spec.default, f"{spec.key} 來回不一致"


def test_both_truth_values_survive_the_round_trip_for_every_flag():
    """布林設定要**兩個值都**來回得了，不是只有預設那一個。

    ⚠ 只測預設值是漏得掉的：旗標族的預設清一色是 False，而
    ``format(False)`` 在幾乎每一種寫法下都會被讀回 False。真正會出事的是
    ``format(True)`` —— 存成 "true" 而讀取規則是 ``== "1"`` 時，管理員按下「開啟」
    存進 DB，下一次讀出來是「關閉」，而且沒有任何錯誤訊息。
    """
    for spec in SETTINGS:
        if spec.value_type.py_type is not bool:
            continue
        for value in (True, False):
            rendered = spec.value_type.format(value)
            assert spec.value_type.parse(rendered) is value, (
                f"{spec.key}：{value} 存成 {rendered!r} 之後讀回來不是同一個值"
            )


def test_restart_required_matches_the_read_sites():
    actual = {s.key for s in SETTINGS if not s.restart_required}
    assert actual == EXPECTED_NO_RESTART_KEYS


def test_c_class_never_requires_a_restart():
    for spec in SETTINGS:
        if spec.setting_class is SettingClass.C:
            assert spec.restart_required is False, f"{spec.key} 是 C 類卻標了需重啟"


# ── 2. env 字串的解讀規則逐顆對得上真正的讀取點 ────────────────────────────


@pytest.mark.parametrize(
    "env_name, raw, expected",
    [
        # url_guard._env_flag：strip() == "1"。"true" 不算開啟。
        ("ANILA_ALLOW_HTTP_ENDPOINT", "1", True),
        ("ANILA_ALLOW_HTTP_ENDPOINT", "true", False),
        ("ANILA_ALLOW_PRIVATE_ENDPOINT", "true", False),
        ("ANILA_ALLOW_DEV_SECRET", "yes", False),
        # zh_normalize / query_expansion：!= "0"。任何非 "0" 都算開啟。
        ("ANILA_ZH_NORMALIZE", "0", False),
        ("ANILA_ZH_NORMALIZE", "false", True),
        ("ANILA_QUERY_EXPANSION", "0", False),
        # ocr.py：.lower() == "true"。"1" 不算開啟。
        ("PDF_OCR_FALLBACK", "true", True),
        ("PDF_OCR_FALLBACK", "1", False),
        ("VISION_VERIFY_SSL", "false", False),
        # card_auth:109 —— strip().lower() in ("1", "true", "yes")。
        ("CARD_DEV_TRUST_TEST_CA", "yes", True),
        ("CARD_DEV_TRUST_TEST_CA", "on", False),
        ("CARD_DEV_TRUST_TEST_CA", " true ", True),
        # card_auth:121 —— **同一族但沒有 strip**（`.lower() in (...)`）。
        # 兩顆共用一個「差不多」的規則，會讓 " true " 在畫面上是開、在模組層是關。
        ("CARD_DEV_SKIP_NONCE_BINDING", "true", True),
        ("CARD_DEV_SKIP_NONCE_BINDING", " true ", False),
        # config.py 的 53 顆走 pydantic 的 bool 解析。
        ("DEBUG", "true", True),
        ("DEBUG", "on", True),
        ("COOKIE_SECURE", "0", False),
    ],
)
def test_env_string_is_read_by_this_variables_own_rule(env_name, raw, expected):
    spec = _by_env_name(env_name)
    assert spec.value_type.parse(raw) is expected


def test_a_flag_read_as_eq_one_never_reports_a_lie_on_the_overview(db, monkeypatch):
    """SSRF 旗標設成 "true" 時，畫面必須跟 guard 一樣認定它是關的。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "true")
    value, source = resolve_setting(db, "network.allow_http_model_endpoint")
    assert (value, source) == (False, SOURCE_ENV)


# ── 3. 回退鏈三態與壞值誠實 ────────────────────────────────────────────────


def test_fallback_uses_program_default_when_neither_db_nor_env_has_it(db, monkeypatch):
    monkeypatch.delenv("MEMORY_RETRIEVE_TOP_K", raising=False)
    assert resolve_setting(db, "memory.retrieve_top_k") == (3, SOURCE_DEFAULT)
    assert get_setting(db, "memory.retrieve_top_k") == 3


def test_fallback_uses_env_when_there_is_no_db_row(db, monkeypatch):
    monkeypatch.setenv("MEMORY_RETRIEVE_TOP_K", "9")
    assert resolve_setting(db, "memory.retrieve_top_k") == (9, SOURCE_ENV)


def test_an_explicitly_empty_env_value_is_still_an_env_value(db, monkeypatch):
    """``FOO=""`` 是「有人設過，設成空的」，不是「沒有人設過」。

    值相同、來源不同：把它算成程式預設，畫面上「來源」那一欄就會告訴管理員
    沒有人動過這顆 —— 而他明明在 `.env` 裡寫了一行。
    """
    monkeypatch.setenv("ANILA_ZIP_FILENAME_ENC", "")
    spec = REGISTRY["intl.zip_filename_encoding"]
    assert spec.default == ""
    assert resolve_setting(db, "intl.zip_filename_encoding") == ("", SOURCE_ENV)


def test_db_row_wins_over_env(db, monkeypatch):
    monkeypatch.setenv("MEMORY_RETRIEVE_TOP_K", "9")
    set_setting(db, "memory.retrieve_top_k", 11)
    assert resolve_setting(db, "memory.retrieve_top_k") == (11, SOURCE_DB)


def test_unparsable_db_value_falls_back_to_env_and_says_so(db, monkeypatch):
    monkeypatch.setenv("MEMORY_RETRIEVE_TOP_K", "9")
    db.add(PlatformSetting(key="memory.retrieve_top_k", value="不是數字"))
    db.flush()
    value, source = resolve_setting(db, "memory.retrieve_top_k")
    assert (value, source) == (9, SOURCE_ENV), "壞值退回後 source 不可以還說 db"


def test_out_of_domain_db_value_falls_back_and_never_claims_db(db, monkeypatch):
    monkeypatch.delenv("MEMORY_RETRIEVE_MIN_COSINE", raising=False)
    db.add(PlatformSetting(key="memory.retrieve_min_cosine", value="1.7"))
    db.flush()
    value, source = resolve_setting(db, "memory.retrieve_min_cosine")
    assert (value, source) == (0.4, SOURCE_DEFAULT)


def test_unparsable_env_value_falls_back_to_the_program_default(db, monkeypatch):
    monkeypatch.setenv("MEMORY_RETRIEVE_TOP_K", "abc")
    assert resolve_setting(db, "memory.retrieve_top_k") == (3, SOURCE_DEFAULT)


def test_out_of_domain_env_value_falls_back_to_the_program_default(db, monkeypatch):
    monkeypatch.setenv("PROXY_MAX_RETRIES", "99")
    assert resolve_setting(db, "proxy.max_retries") == (3, SOURCE_DEFAULT)


@pytest.mark.parametrize("stored", ["-4", "不是數字"])
def test_bad_value_is_reported_out_loud(db, monkeypatch, caplog, stored):
    """兩條壞值路徑各自要有 warning：解不開的，與解得開但落在值域外的。

    只測其中一條會漏掉另一條被降成 debug —— 那就是「靜默退回」重新長回來。
    """
    monkeypatch.delenv("MEMORY_RETRIEVE_TOP_K", raising=False)
    db.add(PlatformSetting(key="memory.retrieve_top_k", value=stored))
    db.flush()
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert resolve_setting(db, "memory.retrieve_top_k") == (3, SOURCE_DEFAULT)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("memory.retrieve_top_k" in r.getMessage() for r in warnings), (
        "靜默退回比報錯危險 —— 壞值一定要留下 warning"
    )


def test_get_and_resolve_are_the_same_resolution(db, monkeypatch):
    monkeypatch.setenv("PROXY_MAX_RETRIES", "7")
    assert get_setting(db, "proxy.max_retries") == resolve_setting(db, "proxy.max_retries")[0]


def test_unknown_key_is_refused_not_guessed(db):
    with pytest.raises(UnknownSettingError):
        resolve_setting(db, "limits.no_such_knob")
    with pytest.raises(UnknownSettingError):
        require_spec("limits.no_such_knob")


# ── 4. 讀不快取（不變式 1）：同一個行程內改完就生效 ────────────────────────


def test_no_process_lifetime_cache_on_db_values(db, monkeypatch):
    monkeypatch.delenv("ANILA_MESSAGE_MAX_SIBLINGS", raising=False)
    assert get_setting(db, "limits.message_max_siblings") == 20
    set_setting(db, "limits.message_max_siblings", 5)
    assert get_setting(db, "limits.message_max_siblings") == 5, "同一行程內改完沒生效"
    set_setting(db, "limits.message_max_siblings", 6)
    assert get_setting(db, "limits.message_max_siblings") == 6


def test_no_process_lifetime_cache_on_env_values(db, monkeypatch):
    monkeypatch.setenv("ANILA_MESSAGE_MAX_SIBLINGS", "8")
    assert get_setting(db, "limits.message_max_siblings") == 8
    monkeypatch.setenv("ANILA_MESSAGE_MAX_SIBLINGS", "12")
    assert get_setting(db, "limits.message_max_siblings") == 12


# ── 5. 值域函式是同一個物件（不變式 2） ────────────────────────────────────


def test_writer_and_resolver_share_one_domain_fn_object(db, monkeypatch):
    """把某一顆的 domain_fn 換掉，寫入端與解析端必須同時跟著變。

    只有其中一端改變 = 兩端各自帶著一份規則 = `6666fbc8` 那個「存得進去、算不
    出來」的狀態。這一支就是為了讓那種寫法當場死掉。
    """
    key = "limits.department_max_depth"
    spec = REGISTRY[key]
    rejecting = dataclasses.replace(spec, domain_fn=lambda value: False)
    monkeypatch.setitem(REGISTRY, key, rejecting)

    with pytest.raises(ValueError):
        set_setting(db, key, 3)

    db.add(PlatformSetting(key=key, value="3"))
    db.flush()
    monkeypatch.delenv("ANILA_DEPARTMENT_MAX_DEPTH", raising=False)
    value, source = resolve_setting(db, key)
    assert source == SOURCE_DEFAULT, "解析端沒有走登錄表那一份值域函式"


def test_domain_fn_is_consulted_with_the_parsed_value_not_the_raw_string(db, monkeypatch):
    key = "limits.department_max_depth"
    seen: list = []
    spec = REGISTRY[key]
    watching = dataclasses.replace(
        spec, domain_fn=lambda value: (seen.append(value), spec.domain_fn(value))[1]
    )
    monkeypatch.setitem(REGISTRY, key, watching)
    monkeypatch.setenv("ANILA_DEPARTMENT_MAX_DEPTH", "4")
    resolve_setting(db, key)
    assert seen == [4]


# ── 6. 門檻那一顆：別名指向已關板的範本，不是複製一份 ──────────────────────


def test_kb_threshold_alias_reuses_the_closed_templates_domain_fn():
    spec = REGISTRY[KB_THRESHOLD_KEY]
    assert spec.domain_fn is _is_usable_kb_threshold, (
        "門檻的值域函式必須是範本那一個物件本身，不可以另外抄一份"
    )
    assert spec.default == KB_THRESHOLD_DEFAULT
    assert spec.env_name is None, "門檻沒有 env 回退層（它本來就只住在 DB）"
    assert spec.setting_class is SettingClass.C


@pytest.mark.parametrize(
    "stored, expected_value, expected_source",
    [
        (None, KB_THRESHOLD_DEFAULT, SOURCE_DEFAULT),
        ("0.55", 0.55, SOURCE_DB),
        ("1.7", KB_THRESHOLD_DEFAULT, SOURCE_DEFAULT),
        ("蘋果", KB_THRESHOLD_DEFAULT, SOURCE_DEFAULT),
    ],
)
def test_generic_resolution_agrees_with_the_threshold_template(
    db, stored, expected_value, expected_source
):
    """三態下，泛化層與已關板的 ``resolve_kb_threshold`` 必須給同一個答案。

    「有人量過」＝ source 是 db；退回預設 ＝ 不可以宣稱有人量過。
    """
    if stored is not None:
        db.add(PlatformSetting(key=KB_THRESHOLD_KEY, value=stored))
        db.flush()

    value, source = resolve_setting(db, KB_THRESHOLD_KEY)
    assert (value, source) == (expected_value, expected_source)

    template_value, calibrated = resolve_kb_threshold(db)
    assert value == template_value
    assert (source == SOURCE_DB) is calibrated


# ── 7. 寫入端：只 flush 不 commit、只有可編輯類別收得下來 ─────────────────


def test_set_setting_flushes_but_never_commits(db):
    set_setting(db, "limits.department_max_depth", 5)
    assert db.get(PlatformSetting, "limits.department_max_depth") is not None
    db.rollback()
    assert db.get(PlatformSetting, "limits.department_max_depth") is None, (
        "set_setting 自己 commit 了 —— 設定與稽核事件就不在同一個交易裡"
    )


def test_set_setting_records_the_actor(db):
    from tests.conftest import make_user

    user = make_user(db, username="settings-admin", role="admin")
    set_setting(db, "limits.department_max_depth", 5, actor=user)
    row = db.get(PlatformSetting, "limits.department_max_depth")
    assert row.updated_by_user_id == user.id


def test_set_setting_refuses_every_non_editable_key(db):
    """全類別掃過，不是抽一顆。"""
    refused = [s for s in SETTINGS if s.setting_class not in EDITABLE_CLASSES]
    # 96 條目 − 可編輯 39（C 19 ＋ 門檻別名 1 ＋ B-可編輯 19）= 57。
    assert len(refused) == 57
    for spec in refused:
        with pytest.raises(ValueError):
            set_setting(db, spec.key, spec.default)
        assert db.get(PlatformSetting, spec.key) is None, f"{spec.key} 竟然被寫進去了"


def test_set_setting_refuses_an_unknown_key(db):
    with pytest.raises(UnknownSettingError):
        set_setting(db, "limits.no_such_knob", 1)


def test_set_setting_rejects_a_bool_for_a_numeric_setting(db):
    with pytest.raises((TypeError, ValueError)):
        set_setting(db, "limits.department_max_depth", True)


# ── 8. 內插值來回釘（下界／中間值／上界） ──────────────────────────────────


def test_every_c_setting_has_an_interior_round_trip_pin():
    c_keys = {s.key for s in SETTINGS if s.setting_class is SettingClass.C}
    pinned = set(INTERIOR_ROUND_TRIP) | set(NON_NUMERIC_C_CASES)
    assert c_keys == pinned, "有 C 類設定沒有來回釘 —— 值域是防呆生命線"


@pytest.mark.parametrize("key", sorted(INTERIOR_ROUND_TRIP))
def test_interior_values_round_trip_through_the_stored_row(db, key, monkeypatch):
    low, mid, high, below, above = INTERIOR_ROUND_TRIP[key]
    spec = REGISTRY[key]
    if spec.env_name:
        monkeypatch.delenv(spec.env_name, raising=False)

    for value in (low, mid, high):
        set_setting(db, key, value)
        row = db.get(PlatformSetting, key)
        assert row is not None
        assert spec.value_type.parse(row.value) == value
        assert resolve_setting(db, key) == (value, SOURCE_DB)

    for value in (below, above):
        with pytest.raises(ValueError):
            set_setting(db, key, value)


@pytest.mark.parametrize("key", sorted(NON_NUMERIC_C_CASES))
def test_non_numeric_c_settings_round_trip(db, key, monkeypatch):
    accepted, rejected = NON_NUMERIC_C_CASES[key]
    spec = REGISTRY[key]
    if spec.env_name:
        monkeypatch.delenv(spec.env_name, raising=False)

    for value in accepted:
        set_setting(db, key, value)
        assert resolve_setting(db, key) == (value, SOURCE_DB)

    for value in rejected:
        with pytest.raises(ValueError):
            set_setting(db, key, value)


def test_rejection_message_carries_the_range_in_plain_words(db):
    """錯誤 detail 會原樣送到畫面上，所以它必須說得出「什麼值收得下來」。"""
    with pytest.raises(ValueError) as excinfo:
        set_setting(db, "proxy.max_retries", 99)
    message = str(excinfo.value)
    assert "proxy.max_retries" in message
    assert "99" in message
    assert REGISTRY["proxy.max_retries"].description[:6] in message


# ── 9. 祕密：登錄表本身不可以夾帶祕密值 ────────────────────────────────────


# ── 10. 宣告 vs 現實（fix round 1，I3：三個活下來的探針就在這一段） ────────
#
# 前面每一支測試都在問「登錄表**內部**自洽嗎」——類別集合、值域、來回、回退鏈。
# 內部自洽擋不住的是**配對漂掉**：某一顆的 env 名換成另一顆的、某一顆的預設值
# 跟真正的讀取點對不上、說明文字寫的值域跟 domain_fn 收的不是同一段。三者都
# 讓整套測試全綠，而畫面上會印出一個這個行程從來沒有用過的值。
#
# 所以這一段刻意**不**再嵌一份名單去比對自己，而是去讀**現實**：
#   * pydantic 那 53 顆 → `Settings.model_fields[...].default`
#   * 原生讀取點 → 掃原始碼裡 `os.environ.get("NAME", "字面值")` 的字面值
#   * 值域 → 拿說明文字裡印出來的那兩個數字**去試** domain_fn 的邊界

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SOURCE_ROOTS = (
    _REPO_ROOT / "services" / "csp" / "app",
    _REPO_ROOT / "packages" / "anila-core" / "src" / "anila_core",
)

# `os.environ.get("NAME", "literal")` / `os.getenv("NAME", "literal")`。
# 只認**兩個引數都是字面值**的形式 —— 名字是變數（startup_security 的泛型 helper、
# url_guard 的 `_env_flag(name)`）或預設值是常數的，這裡看不到，由下面的
# `_DEFAULT_NOT_MECHANICALLY_PINNABLE` 具名交代。
_ENV_LITERAL_DEFAULT = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z0-9_]+)["']\s*,\s*(["'][^"']*["'])\s*\)"""
)

#: 兩種比對都構不到的 15 顆，逐顆有理由。名單本身被 `test_no_entry_escapes_both_default_pins`
#: 釘成相等，所以將來新增的條目不可能默默掉進這個豁免區。
_DEFAULT_NOT_MECHANICALLY_PINNABLE = {
    # url_guard 的 `_env_flag(name)`：名字是參數，字面上看不到。
    "ANILA_ALLOW_HTTP_ENDPOINT", "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
    "ANILA_ALLOW_GRPC_ENDPOINT", "ANILA_ALLOW_PRIVATE_ENDPOINT",
    # startup_security 的泛型 helper：名字來自 `_KNOWN_DEFAULTS` 的鍵。
    "ANILA_HOST", "INTERNAL_PLATFORM_API_KEY", "CODESERVER_PASSWORD",
    # 讀取點沒有給預設值（None／`or` 後備／常數轉指）。
    "ANILA_TEMPLATE_DIR", "ANILA_ZIP_FILENAME_ENC", "CSP_APP_DB_PASSWORD",
    "CSP_SECRET_KEY", "LEGACY_SQLITE_PATH",
    # Task 2 之後這兩顆的讀取點是 ``get_setting``，字面預設值只剩登錄表這一份
    # （原本 ``os.environ.get(name, "1")`` 那個重複的 "1" 已經消失）。拿登錄表
    # 去比登錄表沒有意義，所以改由 ``test_settings_takes_effect`` 的「兩層都沒有
    # 就回退到宣告的預設值」那一輪守著 —— 那是**行為**上的核對，比字面掃描強。
    "ANILA_ZH_NORMALIZE", "ANILA_QUERY_EXPANSION",
    # Task 3 同理：memory 那四顆的讀取點原本是 ``memory_service.py:103-107`` 的
    # **模組層常數**（import 期讀一次 env、整個行程都用那一份），現在改成用到它
    # 的那個函式裡走 ``get_setting``。字面預設值因此只剩登錄表這一份 —— 這正是
    # 本包要的終點：**預設值只能有一個來源**。守它的是
    # ``test_settings_takes_effect_ops`` 的「兩層都沒有就回退到宣告的預設值」，
    # 那是行為上的核對，比字面掃描強。
    "MEMORY_RETRIEVE_TOP_K", "MEMORY_RETRIEVE_MIN_COSINE",
    "MEMORY_MAX_CHUNK_CHARS", "MEMORY_HTTP_TIMEOUT",
    # 預設值是一個常數而不是字面值 —— 改用同一性比對，見下一支測試。
    "PDF_OCR_VISION_PROMPT",
    # 沒有任何 app 讀取點：CPython／httpx runtime 自己消費。
    "PYTHONUNBUFFERED", "SSL_CERT_FILE",
}

#: key 的末段一律要能在 env 名裡認出來（去底線後的子字串）。以下十顆是刻意的
#: 例外 —— 登錄表是拼法的唯一權威（brief），但「刻意」必須寫下來，否則跟打錯字
#: 長得一模一樣。
_KEY_TO_ENV_EXCEPTIONS = {
    "db.migration_url": "MIGRATION_DATABASE_URL",
    "db.app_role_password": "CSP_APP_DB_PASSWORD",
    "auth.secret_key_fallback": "CSP_SECRET_KEY",
    "auth.jwt_algorithm": "ALGORITHM",
    "card.enabled": "ENABLE_CARD_LOGIN",
    "card.require_card_only": "REQUIRE_CARD_LOGIN_ONLY",
    "network.allow_http_model_endpoint": "ANILA_ALLOW_HTTP_ENDPOINT",
    "network.environment": "ANILA_ENV",
    "storage.attachment_path": "ATTACHMENT_STORAGE_PATH",
    "intl.zip_filename_encoding": "ANILA_ZIP_FILENAME_ENC",
}

#: 說明文字裡的「允許 A–B」。這句話會原樣送到管理員眼前（設計 §5）。
_DECLARED_RANGE = re.compile(r"允許\s*(-?[0-9.]+)\s*[–—~-]\s*(-?[0-9.]+)")


def _literal_defaults_from_source() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for root in _SOURCE_ROOTS:
        assert root.is_dir(), f"掃描不到 {root}"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name, literal in _ENV_LITERAL_DEFAULT.findall(text):
                found.setdefault(name, set()).add(ast.literal_eval(literal))
    return found


def test_pydantic_backed_defaults_equal_the_settings_field_default():
    """53 顆 config.py 欄位：宣告的預設值 == `Settings` 那個欄位真正的預設值。

    ⚠ 這一支就是抓出 `STATIC_DIR` 的那一支（fix round 1 的 I2）：登錄表寫
    ``"app/static"``，而 `config.py:125` 算出來的是**絕對路徑**，`main.py` 又是拿
    CWD 去解相對路徑的 —— 畫面上的「程式預設」會印一個這個行程從來沒有用過的值。
    「畫面上看到的設定與實際生效的值不一致」正是本包要消滅的形狀。
    """
    fields = Settings.model_fields
    checked = 0
    for spec in _env_backed():
        if spec.env_name not in fields:
            continue
        checked += 1
        assert spec.default == fields[spec.env_name].default, (
            f"{spec.key} 宣告 {spec.default!r}，但 Settings.{spec.env_name} 的預設是 "
            f"{fields[spec.env_name].default!r}"
        )
    assert checked == 53, f"只比到 {checked} 顆 pydantic 欄位，應該是 53"


def test_native_read_site_defaults_equal_the_declared_default():
    """原生 ``os.environ.get`` 讀取點：字面上的預設值 == 登錄表宣告的預設值。

    比的是**解析後**的值，不是字串 —— ``"1"`` 在 ``!= "0"`` 的規則下是 True，
    在 ``lower() == "true"`` 的規則下是 False，所以這一支同時守著解讀規則。
    同一個名字有多個讀取點且預設值不同時（``REDIS_URL``），只要求宣告的那個
    真的是其中之一，並由該條目的說明文字交代分歧。
    """
    found = _literal_defaults_from_source()
    checked = 0
    for spec in _env_backed():
        literals = found.get(spec.env_name)
        if not literals:
            continue
        checked += 1
        parsed = {spec.value_type.parse(raw) for raw in literals}
        assert spec.default in parsed, (
            f"{spec.key} 宣告 {spec.default!r}，讀取點的字面預設是 {sorted(literals)!r}"
        )
    # Task 2 把 ANILA_ZH_NORMALIZE／ANILA_QUERY_EXPANSION 的讀取點搬到
    # ``get_setting``，各自帶走一個字面預設值（30 → 28）；Task 3 又帶走 memory
    # 那四顆的模組層常數（28 → 24）。⚠ 這個下限是**掃描器還活著**的煙霧測試，
    # 不是覆蓋率目標：搬遷本來就會讓它一路往下掉，真正的核對在
    # ``_DEFAULT_NOT_MECHANICALLY_PINNABLE`` 那份相等比對的名單上。
    assert checked >= 24, f"只掃到 {checked} 個原生讀取點，掃描器可能壞了"


def test_no_entry_escapes_both_default_pins():
    """每一顆要嘛被 pydantic 比對到，要嘛被原始碼掃描比對到，要嘛具名豁免。

    豁免名單是相等比對而不是包含比對：新增一顆而忘了讓它可比對時，這一支會紅，
    不會讓它默默溜進「沒有人核對過」的那一區。
    """
    found = _literal_defaults_from_source()
    fields = set(Settings.model_fields)
    unpinned = {
        spec.env_name
        for spec in _env_backed()
        if spec.env_name not in fields and spec.env_name not in found
    }
    assert unpinned == _DEFAULT_NOT_MECHANICALLY_PINNABLE


def test_the_vision_prompt_default_is_the_program_constant_itself():
    """那一顆比不了字面值的，用同一性比 —— 不是抄一份提示詞過來。"""
    spec = REGISTRY["ingestion.pdf_ocr_vision_prompt"]
    assert spec.default is _DEFAULT_VISION_PROMPT


def test_every_key_identifies_its_own_env_variable():
    """key 的末段要指得出**它自己**那個 env 名，不是隔壁那個。

    ⚠ 兩顆條目同類別、同型別、同預設值時（``VISION_URL``／``VISION_MODEL``），
    把它們的 env 名對調，名字**集合**沒變、類別統計沒變、需重啟集合沒變 ——
    前面每一支測試都還是綠的，而畫面上會把一顆變數的現行值印在另一顆的標籤底下。
    """
    for spec in _env_backed():
        expected = _KEY_TO_ENV_EXCEPTIONS.get(spec.key)
        if expected is not None:
            assert spec.env_name == expected, (
                f"{spec.key} 列在具名例外裡，卻對到 {spec.env_name}"
            )
            continue
        leaf = spec.key.split(".", 1)[1].replace("_", "")
        assert leaf in spec.env_name.lower().replace("_", ""), (
            f"{spec.key} 的末段在 {spec.env_name} 裡認不出來；"
            f"若是刻意的拼法，請加進 _KEY_TO_ENV_EXCEPTIONS 並說明"
        )
    assert set(_KEY_TO_ENV_EXCEPTIONS) <= {s.key for s in SETTINGS}


def test_the_range_in_the_description_is_the_range_the_domain_fn_enforces():
    """說明文字印出來的值域，就是 ``domain_fn`` 真正收的那一段 —— 用邊界去試。

    ⚠ 設計 §5 要求被拒時的 ``detail`` **原樣**呈現給管理員，而那句話來自
    ``description``。說明寫「允許 1–7200」而 ``domain_fn`` 只收到 3600，管理員會
    照著說明填 7200、然後被拒 —— 畫面告訴他的規則不是後端執行的規則。
    這裡不比常數、比行為：任何 ``domain_fn`` 都適用（含門檻那個已關板的函式）。
    """
    checked = 0
    for spec in SETTINGS:
        match = _DECLARED_RANGE.search(spec.description)
        if match is None:
            continue
        checked += 1
        cast = int if spec.value_type.py_type is int else float
        low, high = cast(float(match.group(1))), cast(float(match.group(2)))
        step = 1 if cast is int else 1e-9
        assert spec.domain_fn(low) is True, f"{spec.key} 說明寫下界 {low}，domain_fn 不收"
        assert spec.domain_fn(high) is True, f"{spec.key} 說明寫上界 {high}，domain_fn 不收"
        assert spec.domain_fn(cast(low - step)) is False, (
            f"{spec.key} 的 domain_fn 收得比說明寫的下界更低"
        )
        assert spec.domain_fn(high + step) is False, (
            f"{spec.key} 的 domain_fn 收得比說明寫的上界更高"
        )
    assert checked >= 21, f"只核到 {checked} 段值域說明，正規表示式可能沒對上"


def test_every_bounded_setting_prints_its_range_in_the_description():
    """有值域的條目一定要把值域寫進說明 —— 否則管理員只能用猜的去撞。"""
    for spec in SETTINGS:
        if getattr(spec.domain_fn, "bounds", None) is None:
            continue
        assert _DECLARED_RANGE.search(spec.description), (
            f"{spec.key} 的 domain_fn 有值域 {spec.domain_fn.bounds}，說明卻沒寫出來"
        )


def test_secret_entries_are_declared_but_carry_no_live_secret():
    """A 類的 default 只能是程式裡那個 dev placeholder，不可以是真值。

    登錄表是會被 commit 進 PUBLIC repo 的檔案。
    """
    for spec in SETTINGS:
        if spec.setting_class is not SettingClass.A:
            continue
        assert spec.env_name in EXPECTED_A_ENV_NAMES
        assert spec.locked_reason.strip()
        assert isinstance(spec.default, str)
