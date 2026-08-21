"""Remote docling parser tests (2026-08-17 owner decision: GPU workloads are
reached as an external HTTP endpoint, never run in-process).

Covers the invariants the brief names plus reconstruction:
1. The outbound guard actually rejects an unsafe URL.
2. A non-2xx response surfaces a loud error — never a native-parser fallback.
3. The token never appears in any raised exception text.
4. A 2xx docling payload reconstructs ParsedDocument / ImageRef back.

http 端點(如 http://docling:9100)在 guard 裡需要 ANILA_ALLOW_HTTP_ENDPOINT=1
——scheme 門在 trusted-host 之前,這是 P0.2 起就存在的語意。測試一律設它,
才測得到「單標籤 host 是否被 trusted-hosts 門擋下」那一層。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import respx

from anila_core.ingestion.docling_parser import (
    DOCLING_SUPPORTED_EXTS,
    RemoteDoclingError,
    RemoteDoclingParser,
    _normalize_title,
    build_docling_parser_from_env,
)
from anila_core.ingestion.errors import ParseError

DOCLING_URL = "http://docling:9100"
TOKEN = "docling-token-abc123"

_ALLOW_HTTP = "ANILA_ALLOW_HTTP_ENDPOINT"


def _allow_http(monkeypatch) -> None:
    # http scheme gate 在 trusted-host 之前(guard 的文件語意);docling 端點在
    # 內網走 http,同 ASR,需要這個旗標。
    monkeypatch.setenv(_ALLOW_HTTP, "1")


def _write_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def _ok_payload(markdown: str = "# Hello") -> dict:
    return {
        "markdown": markdown,
        "title": "sample",
        "page_count": 3,
        "ocr_applied": False,
        "images": [],
    }


# ── 1. guard rejects an unsafe URL ────────────────────────────────────────

def test_guard_rejects_loopback_url(tmp_path):
    # 127.0.0.1 是結構性不安全,連 trusted host 都救不回來——guard 必須擋。
    # M1:這是**設定錯**(DOCLING_URL),走 bad_config(不可重試),不是基礎設施。
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url="http://127.0.0.1:9100", token=TOKEN)
    with pytest.raises(ParseError) as excinfo:
        parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert excinfo.value.retryable is False


def test_guard_rejects_single_label_host_without_trust(tmp_path, monkeypatch):
    # 單標籤 docker 服務名(docling)在被 ANILA_TRUSTED_HOSTS 點名前,guard 該擋。
    # M1:設定錯 → bad_config;維運線索(含 hint)進 details。
    _allow_http(monkeypatch)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with pytest.raises(ParseError) as excinfo:
        parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    # trust-host 可修 → details 要指到那個 operator 機制。
    assert "ANILA_TRUSTED_HOSTS" in str(excinfo.value.details)


def test_guard_accepts_trusted_single_label(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=_ok_payload())
        )
        result = parser.parse(str(path))
    assert result.content == "# Hello"


# ── 2. non-2xx → 三類分域(文件／組態／基礎設施)── 每一格一個測試 ──────────

@pytest.mark.parametrize("status", [400, 422])
def test_client_4xx_file_error_maps_to_corrupt(tmp_path, monkeypatch, status):
    """服務端說「這份文件有問題」(400/422)→ 文件問題、不可重試。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(status, json={"detail": "file problem"})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_CORRUPT"
    assert excinfo.value.retryable is False


def test_client_413_maps_to_too_large(tmp_path, monkeypatch):
    """413 = 檔案過大,文案不能叫人猜「純圖片/損毀/密碼保護」。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(413, json={"detail": "too large"})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_TOO_LARGE"
    assert excinfo.value.retryable is False
    assert "損毀" not in excinfo.value.user_message


@pytest.mark.parametrize("status", [401, 403, 404, 405, 407])
def test_client_4xx_config_maps_to_bad_config(tmp_path, monkeypatch, status):
    """401/403(token)、404/405(URL 路徑錯)、407(proxy auth)= 組態,不可重試。
    HIGH 第四類:404/405/407 過去被判成「檔案壞了」。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(status, json={"detail": "config issue"})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert excinfo.value.retryable is False
    # 設定名只進 details 不進 body(MEDIUM-A)。
    setting = str(excinfo.value.details.get("setting", ""))
    assert setting in ("DOCLING_SERVICE_TOKEN", "DOCLING_URL")
    assert setting not in excinfo.value.user_message


@pytest.mark.parametrize("status", [301, 302, 307])
def test_client_3xx_maps_to_bad_config(tmp_path, monkeypatch, status):
    """3xx = 重導向(永久組態錯),不可重試。httpx follow_redirects 預設 False,
    所以 3xx 真的會到這(M4)。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(status, json={}, headers={"location": "/v1/parse"})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert excinfo.value.retryable is False
    assert excinfo.value.details.get("location") == "/v1/parse"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_client_5xx_408_429_maps_to_infrastructure(tmp_path, monkeypatch, status):
    """5xx、408、429 → 基礎設施、可重試、訊息指端點(通用句,不帶主機名)。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(status, json={"detail": "server failure"})
        )
        with pytest.raises(RemoteDoclingError) as excinfo:
            parser.parse(str(path))
    # 使用者面通用句(經 parsers.py 會轉成 RemoteParseError,此處是 client 層)。
    assert "稍後重試" in str(excinfo.value)


def test_network_failure_is_loud_not_silent(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(RemoteDoclingError) as excinfo:
            parser.parse(str(path))
    assert "稍後重試" in str(excinfo.value)


# ── 3. token never appears in exception text ───────────────────────────────

def test_token_redacted_in_http_error(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    # 上游把 token 回音在 body(實際見過的形狀):必須抹掉。5xx 走
    # RemoteDoclingError,上游片段進 details(=維運面,也 redact)、usersafe 是
    # 通用句——兩個字串都不得含 token。
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(
                500, json={"detail": f"invalid token {TOKEN}"}
            )
        )
        with pytest.raises(RemoteDoclingError) as excinfo:
            parser.parse(str(path))
    assert TOKEN not in str(excinfo.value)  # usersafe
    assert TOKEN not in str(excinfo.value.details)  # 維運面也 redact 過
    assert "<redacted>" in str(excinfo.value.details)


def test_token_redacted_in_bad_config_details(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    # 401 走 bad_config 分支,detail 進 details——也抹。
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(
                401, json={"detail": f"invalid token {TOKEN}"}
            )
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in str(excinfo.value.details)


def test_token_redacted_in_network_error(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    # httpx 例外訊息可能把 URL(userinfo 帶 token)貼上;redact 要擋住。
    parser = RemoteDoclingParser(
        base_url=f"http://user:{TOKEN}@docling:9100",
        token=TOKEN,
        client=httpx.Client(timeout=5.0),
    )
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            side_effect=httpx.ConnectError(f"connect to user:{TOKEN}@docling")
        )
        with pytest.raises(RemoteDoclingError) as excinfo:
            parser.parse(str(path))
    assert TOKEN not in str(excinfo.value)


# ── 4. reconstruction ──────────────────────────────────────────────────────

def test_reconstructs_document_with_images(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    payload = {
        "markdown": "# Title\n\nbody text",
        "title": "My Doc",
        "page_count": 5,
        "ocr_applied": True,
        "images": [
            {
                "id": "img1",
                "page": 2,
                "caption": "a chart",
                "png_b64": base64.b64encode(png_bytes).decode(),
            }
        ],
    }
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = parser.parse(str(path))

    assert result.content == "# Title\n\nbody text\n\n[[IMAGE:img1]]"
    assert result.metadata["parser"] == "docling-remote"
    assert result.metadata["pages"] == 5
    assert result.metadata["ocr_used"] is True
    assert result.metadata["embedded_images"] == 1
    assert result.images["img1"].image_bytes == png_bytes
    assert result.images["img1"].page == 2
    assert result.images["img1"].caption == "a chart"


# ── 5. env factory wiring ──────────────────────────────────────────────────

def test_factory_returns_none_by_default(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "native")
    assert build_docling_parser_from_env() is None


def test_factory_returns_remote_when_docling(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", DOCLING_URL)
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", TOKEN)
    parser = build_docling_parser_from_env()
    assert parser is not None
    assert isinstance(parser, RemoteDoclingParser)


def test_factory_bad_timeout_raises_not_falls_back(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", DOCLING_URL)
    monkeypatch.setenv("DOCLING_TIMEOUT_SECONDS", "not-a-number")
    # 不再是裸 ValueError——結構化 ParseError.bad_config。(裸 ValueError 會
    # 掉進 parsers.py 的「不支援副檔名」分支,歸錯因。)設定名進 details,
    # 不進 body(MEDIUM-A)。
    with pytest.raises(ParseError) as excinfo:
        build_docling_parser_from_env()
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert "DOCLING_TIMEOUT_SECONDS" in str(excinfo.value.details)
    assert "DOCLING_TIMEOUT_SECONDS" not in excinfo.value.user_message


def test_supported_exts_unchanged():
    assert DOCLING_SUPPORTED_EXTS == frozenset(
        {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"}
    )


def test_normalize_title_strips_control_chars_and_caps():
    # client 側鏡射 service 端邊界:title 由 uploader 控制,控制字元/超長
    # 字串不假設遠端清乾淨(2026-08-20 revision)。
    assert _normalize_title("clean") == "clean"
    assert _normalize_title("a\r\nb\tc") == "abc"
    assert _normalize_title("a\x7fb") == "ab"
    assert _normalize_title("  padded \t") == "padded"
    assert len(_normalize_title("x" * 300)) == 255
    assert _normalize_title(None) == ""
    assert _normalize_title(123) == ""


def test_reconstruct_normalizes_title_from_payload(tmp_path, monkeypatch):
    """client 補強:即使遠端回來的 title 帶控制字元/超長,_reconstruct 仍守
    下游(不假設遠端一定清乾淨——另一版 service 或少跑一層時,這裡照樣守)。
    """
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    payload = _ok_payload()
    payload["title"] = "a\r\nb\t" + "x" * 300
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = parser.parse(str(path))
    assert result.metadata["title"] == "ab" + "x" * 253
    assert len(result.metadata["title"]) == 255
    assert "\r" not in result.metadata["title"]
    assert "\n" not in result.metadata["title"]
    assert "\t" not in result.metadata["title"]


# ── 6. client reuse (LOW #1):一個 parser 一個 client,不每份文件重交握 ──────

def test_client_is_reused_across_parses(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=_ok_payload())
        )
        parser.parse(str(path))
        first_client = parser._client
        parser.parse(str(path))
        assert parser._client is first_client


def test_non_ascii_token_points_at_the_setting(tmp_path, monkeypatch):
    """非 ASCII token → 組態問題(bad_config),不可指向網路。

    過去它會在 httpx 組 X-Token header 時拋 UnicodeEncodeError,然後被包成
    「遠端無回應…請檢查端點是否可達」——100% 解析失敗,方向卻指錯。
    R5 HIGH-A 後:組態問題 = ParseError.bad_config,設定名進 details(不再
    用 RemoteDoclingError 也不把設定名放 body)。
    """
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token="祕密-非-ASCII")
    with pytest.raises(ParseError) as excinfo:
        parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert "DOCLING_SERVICE_TOKEN" in str(excinfo.value.details)
    assert "DOCLING_SERVICE_TOKEN" not in excinfo.value.user_message
    # 不可指向網路 / 端點 / 無回應。
    assert "無回應" not in excinfo.value.user_message
    assert "可達" not in excinfo.value.user_message


def test_injected_client_takes_precedence(tmp_path, monkeypatch):
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    injected = httpx.Client()
    parser = RemoteDoclingParser(
        base_url=DOCLING_URL, token=TOKEN, client=injected
    )
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=_ok_payload())
        )
        parser.parse(str(path))
        assert parser._client is injected


# ── 7. _reconstruct shape guard (LOW#2) ─────────────────────────────────────

def test_200_wrong_shape_maps_to_bad_config(tmp_path, monkeypatch):
    """200 + 合法 JSON 但沒有 markdown 鍵 = 端點指到別的服務(不是 docling)。

    「別人回 200 不代表別人收下了」——缺 markdown 鍵要歸 bad_config(組態),
    不是「檔案抽取後沒有可用文字」。
    """
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json={"hello": "world", "id": 1})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_BAD_CONFIG"
    assert "markdown" in str(excinfo.value.details)


def test_400_unsupported_extension_maps_to_format_unsupported(tmp_path, monkeypatch):
    """service 回 400 "unsupported extension" = 兩端 extension 集合漂移 → format_unsupported。"""
    _allow_http(monkeypatch)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")
    path = _write_pdf(tmp_path)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(400, json={"detail": "unsupported extension: 'x.zzz'"})
        )
        with pytest.raises(ParseError) as excinfo:
            parser.parse(str(path))
    assert excinfo.value.code == "E_PARSE_FORMAT_UNSUPPORTED"
    assert excinfo.value.retryable is False
