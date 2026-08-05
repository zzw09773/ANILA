"""OpenAI 相容傳輸:WAV framing、Bearer 憑證、協定選擇、探針語意。

擁有者的要求是「不論是本地還是外部伺服器都要可以連線」—— 所以這一份的每
一條都成對:openai 那條做得到什麼,native 那條就不能因此壞掉。

HTTP 邊界一律用 respx 假,函式本身不假。
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
import respx
from httpx import Response

from app.config import Settings
from app.decode_client import (
    PROTOCOL_NATIVE,
    PROTOCOL_OPENAI,
    DecodeClient,
    DecodeError,
    OpenAIDecodeClient,
    env_credential,
    make_decode_client,
    normalise_protocol,
)
from app.decode_probe import (
    REASON_DECODER_NOT_READY,
    REASON_DECODER_UNAUTHORIZED,
    REASON_DECODER_UNREACHABLE,
    REASON_OK,
    probe_decode_target,
)
from app.main import _validate_settings
from app.wav import pcm16_to_wav, silence_wav

REMOTE = "https://compute.example.test"
KEY = "sk-remote-asr-key-do-not-log"


# ── WAV framing ─────────────────────────────────────────────────────────────


def extract_wav(body: bytes) -> bytes:
    """從 multipart body 裡把 RIFF 檔案本體切出來。

    刻意用 RIFF 自己宣告的長度來切 —— 檔頭裡的長度欄位若寫錯就切不出合法
    WAV,所以這個 helper 本身就是一道檢查。
    """
    start = body.find(b"RIFF")
    assert start >= 0, "multipart body 裡找不到 RIFF 檔頭"
    size = int.from_bytes(body[start + 4 : start + 8], "little")
    return body[start : start + 8 + size]


def test_pcm16_to_wav_declares_16k_mono_int16():
    """PROVE RED:在 wav.py 把 setframerate 改成 8000 → 這條紅。"""
    pcm = b"\x01\x02" * 1600  # 1600 個取樣 = 100 ms
    blob = pcm16_to_wav(pcm)
    with wave.open(io.BytesIO(blob), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 1600
        assert handle.readframes(1600) == pcm


def test_pcm16_to_wav_rejects_half_a_sample():
    with pytest.raises(ValueError):
        pcm16_to_wav(b"\x01\x02\x03")


def test_silence_wav_is_parseable_and_short():
    with wave.open(io.BytesIO(silence_wav(100)), "rb") as handle:
        assert handle.getnframes() == 1600
        assert handle.getframerate() == 16_000


# ── 協定選擇 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["native", "NATIVE", " openai ", "openai"])
def test_normalise_protocol_accepts_the_two_supported(raw):
    assert normalise_protocol(raw) in (PROTOCOL_NATIVE, PROTOCOL_OPENAI)


@pytest.mark.parametrize("raw", ["", None, "whisper", "grpc", "openai-compatible"])
def test_normalise_protocol_refuses_anything_else(raw):
    with pytest.raises(ValueError):
        normalise_protocol(raw)


def test_bad_protocol_fails_at_startup():
    """假控制項防線:設錯的旋鈕不准悄悄退回預設。

    PROVE RED:把 normalise_protocol 的 raise 換成 `return PROTOCOL_NATIVE`
    → 這條紅(而且 openai 端點會被當成 native 打,每句話 404)。
    """
    with pytest.raises(RuntimeError, match="ASR_DECODE_PROTOCOL"):
        _validate_settings(
            Settings(
                ASR_DECODE_URL="https://gpu.example.test:9000",
                ASR_DECODER_TOKEN="t",
                ASR_DECODE_PROTOCOL="openai-compatible",
            )
        )


def test_openai_without_api_key_fails_at_startup():
    with pytest.raises(RuntimeError, match="ASR_DECODE_API_KEY"):
        _validate_settings(
            Settings(
                ASR_DECODE_URL=REMOTE,
                ASR_DECODE_PROTOCOL="openai",
                ASR_DECODE_API_KEY="",
            )
        )


def test_openai_does_not_require_the_native_shared_secret():
    """遠端部署沒有 asr-decoder,也就沒有 ASR_DECODER_TOKEN 可設。"""
    _validate_settings(
        Settings(
            ASR_DECODE_URL=REMOTE,
            ASR_DECODER_TOKEN="",
            ASR_DECODE_PROTOCOL="openai",
            ASR_DECODE_API_KEY=KEY,
        )
    )


def test_make_decode_client_picks_the_declared_transport():
    native = make_decode_client(
        Settings(ASR_DECODE_URL="https://d.example.test", ASR_DECODER_TOKEN="t")
    )
    assert isinstance(native, DecodeClient)
    assert native.protocol == PROTOCOL_NATIVE

    remote = make_decode_client(
        Settings(
            ASR_DECODE_URL=REMOTE,
            ASR_DECODE_PROTOCOL="openai",
            ASR_DECODE_API_KEY=KEY,
        )
    )
    assert isinstance(remote, OpenAIDecodeClient)
    assert remote.protocol == PROTOCOL_OPENAI


def test_env_credential_follows_the_protocol():
    """PROVE RED:讓 env_credential 永遠回 ASR_DECODER_TOKEN → openai 那半紅。"""
    native = Settings(ASR_DECODE_URL="https://d.example.test", ASR_DECODER_TOKEN="shared")
    assert env_credential(native) == "shared"
    remote = Settings(
        ASR_DECODE_URL=REMOTE,
        ASR_DECODER_TOKEN="shared",
        ASR_DECODE_PROTOCOL="openai",
        ASR_DECODE_API_KEY=KEY,
    )
    assert env_credential(remote) == KEY


# ── 遠端解碼實際走一趟 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_remote_transcription_sends_bearer_and_framed_wav():
    """PROVE RED:在 OpenAIDecodeClient._post 把 `pcm16_to_wav(pcm)` 換成裸
    `pcm` → WAV 解析失敗,這條紅。這正是「gateway 目前送無標頭 PCM」那個缺口。
    """
    route = respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": "測試語音"})
    )
    client = OpenAIDecodeClient(REMOTE, KEY, model="whisper-large-v3")
    samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    result = await client.decode(samples, kind="final", prompt="以下是繁體中文。", beam=5)
    await client.aclose()

    assert result["text"] == "測試語音"
    # session.py 的丟棄條件要求兩個指標都難看;0.0 = 沒有訊號、不要據此丟棄。
    assert result["no_speech_prob"] == 0.0
    assert result["avg_logprob"] == 0.0

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"].startswith("multipart/form-data")

    body = request.content
    with wave.open(io.BytesIO(extract_wav(body)), "rb") as handle:
        assert handle.getframerate() == 16_000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 1600
    assert b'name="model"' in body
    assert b"whisper-large-v3" in body
    assert b"\xe4\xbb\xa5\xe4\xb8\x8b\xe6\x98\xaf" in body  # prompt 有送出去


@pytest.mark.asyncio
@respx.mock
async def test_remote_base_with_v1_does_not_double_the_version_segment():
    """登記成 `…/v1` 的端點很常見;接字串會變成 /v1/v1/audio/transcriptions。"""
    route = respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": "ok"})
    )
    client = OpenAIDecodeClient(f"{REMOTE}/v1", KEY, model="whisper-1")
    await client.decode(np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1)
    await client.aclose()
    assert route.called
    assert str(route.calls.last.request.url).endswith("/v1/audio/transcriptions")


@pytest.mark.asyncio
@respx.mock
async def test_missing_credential_sends_no_authorization_header():
    """空金鑰時不要送 `Bearer `(空字串)—— 那會把 401 偽裝成 400。

    ⚠ 誠實標註:`_validate_settings` 在 `ASR_DECODE_PROTOCOL=openai` 時要求
    `ASR_DECODE_API_KEY` 非空,而治理中心沒指派金鑰時 `current_decode_credential`
    退回的正是它 —— 所以**正式部署走不到這個狀態**。這條測的是
    `auth_headers()` 這個函式自己的契約(空值不要偽造出一個 header),
    不是一個活體情境;留著是因為那個契約是 `set_credential("")` 之類的
    未來呼叫端會依賴的東西,不是因為它擋得住什麼現行的病。
    """
    route = respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": "ok"})
    )
    client = OpenAIDecodeClient(REMOTE, "", model="whisper-1")
    await client.decode(np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1)
    await client.aclose()
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
@respx.mock
async def test_wrong_credential_raises_loudly_without_leaking_the_key():
    """金鑰錯 → final 一定要 raise(使用者會看到錯誤),而錯誤訊息不含金鑰。

    PROVE RED:把 `_redact` 拿掉、或直接把 resp.text 原樣塞進 DecodeError
    → 洩漏那半條紅。
    """
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(401, json={"error": f"invalid api key: {KEY}"})
    )
    client = OpenAIDecodeClient(REMOTE, KEY, model="whisper-1")
    with pytest.raises(DecodeError) as excinfo:
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    message = str(excinfo.value)
    assert "401" in message
    assert KEY not in message
    assert "<redacted>" in message


@pytest.mark.asyncio
@respx.mock
async def test_partial_failure_stays_silent_on_the_remote_path_too():
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(500, text="boom")
    )
    client = OpenAIDecodeClient(REMOTE, KEY, model="whisper-1")
    result = await client.decode(
        np.zeros(160, dtype=np.float32), kind="partial", prompt=None, beam=1
    )
    await client.aclose()
    assert result == {"text": ""}


@pytest.mark.asyncio
@respx.mock
async def test_credential_can_be_swapped_at_runtime():
    """治理中心換了金鑰不必重啟 gateway。"""
    route = respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": "ok"})
    )
    client = OpenAIDecodeClient(REMOTE, "old-key-value", model="whisper-1")
    client.set_credential("new-key-value")
    await client.decode(np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1)
    await client.aclose()
    assert route.calls.last.request.headers["authorization"] == "Bearer new-key-value"


# ── 探針必須跟著協定走 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_remote_probe_ok_when_endpoint_answers():
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": ""})
    )
    probe = await probe_decode_target(REMOTE, KEY, protocol="openai")
    assert probe == {"ok": True, "reason": REASON_OK, "detail": None}


@pytest.mark.asyncio
@respx.mock
async def test_remote_probe_401_is_unauthorized_not_unreachable():
    """這一條是遠端部署最貴的誤診:金鑰錯卻叫人去查網路。

    PROVE RED:把 `_probe_openai` 裡的 401 分支刪掉 → 落到最後的
    decoder_unreachable,這條紅。
    """
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(401, json={"error": "invalid api key"})
    )
    probe = await probe_decode_target(REMOTE, "wrong-key", protocol="openai")
    assert probe["ok"] is False
    assert probe["reason"] == REASON_DECODER_UNAUTHORIZED
    assert probe["reason"] != REASON_DECODER_UNREACHABLE
    assert "wrong-key" not in (probe["detail"] or "")


@pytest.mark.asyncio
@respx.mock
async def test_remote_probe_down_is_unreachable():
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        side_effect=ConnectionError("remote down")
    )
    probe = await probe_decode_target(REMOTE, KEY, protocol="openai")
    assert probe["ok"] is False
    assert probe["reason"] == REASON_DECODER_UNREACHABLE
    assert KEY not in (probe["detail"] or "")


@pytest.mark.asyncio
@respx.mock
async def test_remote_probe_503_is_not_ready():
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(503, text="model loading")
    )
    probe = await probe_decode_target(REMOTE, KEY, protocol="openai")
    assert probe["reason"] == REASON_DECODER_NOT_READY


@pytest.mark.asyncio
@respx.mock
async def test_remote_probe_404_says_the_protocol_may_be_wrong():
    """協定選錯要看得見 —— 這是「自動偵測失敗必須可見」的那個要求。"""
    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(
        return_value=Response(404, text="not found")
    )
    probe = await probe_decode_target(REMOTE, KEY, protocol="openai")
    assert probe["reason"] == REASON_DECODER_UNREACHABLE
    assert "native" in (probe["detail"] or "")


@pytest.mark.asyncio
@respx.mock
async def test_native_probe_against_an_openai_endpoint_says_so():
    """反過來也要看得見:native 探針打到只有 /v1/... 的端點會拿到 404。"""
    respx.get(f"{REMOTE}/health").mock(return_value=Response(404, text="not found"))
    probe = await probe_decode_target(REMOTE, KEY, protocol="native")
    assert probe["reason"] == REASON_DECODER_UNREACHABLE
    assert "ASR_DECODE_PROTOCOL=openai" in (probe["detail"] or "")


@pytest.mark.asyncio
@respx.mock
async def test_probe_timeouts_come_from_settings_not_the_two_second_lan_default():
    """PROVE RED:把 `_timeout()` 換回 httpx.Timeout(2.0, connect=1.0) 常數
    → 這條紅。2 秒是「decoder 在同一台」調出來的,跨 WAN 會把一支能用的
    麥克風藏起來。
    """
    captured: dict = {}

    def _capture(request):
        captured["timeout"] = request.extensions.get("timeout")
        return Response(200, json={"text": ""})

    respx.post(f"{REMOTE}/v1/audio/transcriptions").mock(side_effect=_capture)
    await probe_decode_target(
        REMOTE,
        KEY,
        protocol="openai",
        timeout_seconds=11.0,
        connect_timeout_seconds=4.0,
    )
    assert captured["timeout"]["connect"] == 4.0
    assert captured["timeout"]["read"] == 11.0


def test_settings_default_probe_timeouts_are_wan_appropriate():
    s = Settings(ASR_DECODE_URL="https://d.example.test", ASR_DECODER_TOKEN="t")
    assert s.ASR_PROBE_CONNECT_TIMEOUT_SECONDS >= 3.0
    assert s.ASR_PROBE_TIMEOUT_SECONDS >= 8.0
