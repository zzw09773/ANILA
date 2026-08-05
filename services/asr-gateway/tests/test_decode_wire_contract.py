"""離開 process 的位元組:欄位名、檔案型別、認證 header、音訊 framing。

這一份存在的理由是一次量測結果:在既有的 155 條測試底下,**九個**針對
「送出去的東西長什麼樣」的突變一條都不會紅 —— 因為 HTTP 邊界一律是 respx,
而 respx 什麼都收。其中兩個實測是活體致命的:

  * `decode_client` 的檔案欄位名 `file` → `audio`:每一句話 422,麥克風看起來
    正常,測試全綠。
  * `decode_probe` 的探針不送 `Authorization`:`/asr/health` 拿著一把**正確
    的**金鑰回 `decoder_unauthorized` —— 綠燈的測試、藏起來的麥克風、被派去
    輪替憑證的 operator。這正是這一批改動存在的目的(誤診)本身。

所以這裡改用 `tests/strict_decoder_stub.py`:真的開 socket、真的檢查、而且
把每一次請求記下來。斷言分兩層,兩層都要:

  1. **結果層** —— 挑剔的端點拒絕了,呼叫端就該壞掉(decode 丟 DecodeError、
     探針回非 ok)。
  2. **記錄層** —— 探針刻意把 400/415/422 視為「認證過了、只是嫌這段 100 ms
     靜音」(`decode_probe._probe_openai` 的註解說明了為什麼),所以探針那條
     路上「被拒絕」不會反映在結論裡,只能斷言記錄。少了這一層,欄位名與音訊
     framing 的突變在探針側依然是隱形的。
"""

from __future__ import annotations

import io
import logging
import wave

import httpx
import numpy as np
import pytest

from app.config import Settings
from app.decode_client import DecodeClient, DecodeError, OpenAIDecodeClient
from app.decode_probe import (
    REASON_DECODER_UNAUTHORIZED,
    REASON_OK,
    probe_decode_target,
)
from app.main import _configure_logging
from app.redaction import UserinfoRedactingFilter
from app.wav import silence_wav
from tests.strict_decoder_stub import StrictDecoderStub, wav_facts

KEY = "sk-strict-stub-key-do-not-log"
SHARED = "native-shared-secret"
MODEL = "whisper-large-v3"


def wrong_rate_wav(rate: int) -> bytes:
    """合法 WAV,但取樣率不是 16 kHz —— 用來證明 stub 真的讀了檔頭。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


@pytest.fixture
def openai_stub():
    with StrictDecoderStub(credential=KEY, model=MODEL) as stub:
        yield stub


@pytest.fixture
def native_stub():
    with StrictDecoderStub(credential=SHARED) as stub:
        yield stub


# ── 先驗那個「挑剔的端點」自己真的挑剔 ─────────────────────────────────────
#
# ⚠ 這一節是**驗證器的驗證器**。下面所有測試的價值都建立在「stub 會拒絕」上;
#   stub 哪天悄悄變寬鬆(改壞了、或有人為了讓某條測試過而放行),整份就會變回
#   respx 那種「什麼都收」的假綠燈,而且不會有任何一條測試紅。所以直接對它送
#   壞掉的請求,斷言它擋得住。


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,expected,why",
    [
        (lambda kw: kw.update(headers={}), 401, "沒有 Authorization"),
        (
            lambda kw: kw.update(headers={"Authorization": f"Token {KEY}"}),
            401,
            "scheme 不是 Bearer",
        ),
        (
            lambda kw: kw.update(headers={"Authorization": "Bearer sk-wrong"}),
            401,
            "金鑰不對",
        ),
        (
            lambda kw: kw.update(files={"audio": ("a.wav", silence_wav(100), "audio/wav")}),
            422,
            "欄位名不是 file",
        ),
        (
            lambda kw: kw.update(
                files={"file": ("a.wav", silence_wav(100), "application/octet-stream")}
            ),
            415,
            "檔案部件的 Content-Type 不是 audio/wav",
        ),
        (
            lambda kw: kw.update(files={"file": ("a.wav", b"\x00\x00" * 1600, "audio/wav")}),
            415,
            "送的是裸 PCM,不是 WAV",
        ),
        (
            lambda kw: kw.update(files={"file": ("a.wav", wrong_rate_wav(8000), "audio/wav")}),
            415,
            "WAV 但取樣率不是 16 kHz",
        ),
        (lambda kw: kw.update(data={"response_format": "json"}), 422, "缺 model 欄位"),
    ],
)
async def test_the_strict_stub_actually_rejects(openai_stub, mutate, expected, why):
    kwargs = {
        "headers": {"Authorization": f"Bearer {KEY}"},
        "files": {"file": ("a.wav", silence_wav(100), "audio/wav")},
        "data": {"model": MODEL, "response_format": "json"},
    }
    mutate(kwargs)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{openai_stub.url}/v1/audio/transcriptions", timeout=10.0, **kwargs
        )
    assert resp.status_code == expected, f"{why}:收到 {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_the_strict_stub_accepts_a_correct_request(openai_stub):
    """反面:形狀全對就要 200 —— 否則上面那些拒絕證明不了「挑剔」,只證明「壞掉」。"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{openai_stub.url}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {KEY}"},
            files={"file": ("a.wav", silence_wav(100), "audio/wav")},
            data={"model": MODEL, "response_format": "json"},
            timeout=10.0,
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,expected,why",
    [
        ({"Content-Type": "application/octet-stream"}, 401, "沒有 X-Token"),
        (
            {"Content-Type": "application/octet-stream", "X-Token": "wrong"},
            401,
            "X-Token 不對",
        ),
        (
            {"Content-Type": "text/plain", "X-Token": SHARED},
            415,
            "body 的 Content-Type 不是 octet-stream",
        ),
    ],
)
async def test_the_strict_native_stub_actually_rejects(
    native_stub, headers, expected, why
):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{native_stub.url}/transcribe",
            params={"kind": "final", "beam": 1, "prompt": "", "language": "zh"},
            content=b"\x00\x00" * 100,
            headers=headers,
            timeout=10.0,
        )
    assert resp.status_code == expected, f"{why}:收到 {resp.status_code} {resp.text}"


# ── 解碼路徑:OpenAI 相容 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_decode_satisfies_a_strict_endpoint(openai_stub):
    """PROVE RED(逐一施加,每次一個):

      * `files={"file": …}` → `files={"audio": …}`   → 端點 422,decode 丟錯
      * `("audio.wav", wav, "audio/wav")` → `"application/octet-stream"`
                                                     → 端點 415,decode 丟錯
      * `data` 拿掉 `"language"`                      → 記錄層斷言紅
      * `"temperature": "0"` → `"1"`                  → 記錄層斷言紅
      * `pcm16_to_wav(pcm)` → 裸 `pcm`                → 端點 415,decode 丟錯
    """
    client = OpenAIDecodeClient(openai_stub.url, KEY, model=MODEL)
    samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    result = await client.decode(
        samples, kind="final", prompt="以下是繁體中文。", beam=5, language="zh"
    )
    await client.aclose()

    assert result["text"] == "測試語音"
    assert openai_stub.rejections == []

    call = openai_stub.last_to("/v1/audio/transcriptions")
    # 認證:scheme 與值都要對(挑剔端點對兩者都回 401)。
    assert call.header("authorization") == f"Bearer {KEY}"
    # 檔案部件:名字、檔名、Content-Type、以及檔案本體真的是 16k mono 16-bit。
    audio = call.part("file")
    assert audio is not None, f"送出去的部件是 {call.part_names}"
    assert audio.filename == "audio.wav"
    assert audio.content_type == "audio/wav"
    assert wav_facts(audio.content) == {
        "channels": 1,
        "sampwidth": 2,
        "framerate": 16_000,
        "frames": 1600,
    }
    # 表單欄位:少一個就是換一種行為,而不是「少一個欄位」。
    assert call.form("model") == MODEL
    assert call.form("response_format") == "json"
    assert call.form("language") == "zh"
    # temperature=0 → 不做 temperature fallback。串流逐字稿要穩定不要多樣性。
    assert call.form("temperature") == "0"
    assert call.form("prompt") == "以下是繁體中文。"


@pytest.mark.asyncio
async def test_openai_decode_language_follows_the_caller(openai_stub):
    """`language` 是**參數**不是常數 —— 寫死成 "zh" 的話這條紅。"""
    client = OpenAIDecodeClient(openai_stub.url, KEY, model=MODEL)
    await client.decode(
        np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1, language="ja"
    )
    await client.aclose()
    assert openai_stub.last_to("/v1/audio/transcriptions").form("language") == "ja"


@pytest.mark.asyncio
async def test_openai_decode_dies_loudly_when_the_endpoint_rejects_the_shape(openai_stub):
    """挑剔端點拒絕 = final 一定要 raise(使用者看得到),不是靜默成功。

    這條把「結果層」釘住:上面那條若只剩記錄層斷言,欄位名突變仍會在這裡紅。
    """
    openai_stub.forced_status = 422
    openai_stub.forced_body = "missing form field 'file'"
    client = OpenAIDecodeClient(openai_stub.url, KEY, model=MODEL)
    with pytest.raises(DecodeError) as excinfo:
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    assert "422" in str(excinfo.value)


@pytest.mark.asyncio
async def test_openai_decode_with_a_wrong_key_is_rejected_by_the_stub(openai_stub):
    """假端點本身要真的擋得住錯的金鑰,否則上面的綠燈不值錢。"""
    client = OpenAIDecodeClient(openai_stub.url, "sk-wrong", model=MODEL)
    with pytest.raises(DecodeError):
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    assert openai_stub.rejections, "挑剔端點沒有擋下錯的金鑰 —— 這個 stub 是壞的"


# ── 解碼路徑:原生契約 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_decode_satisfies_a_strict_endpoint(native_stub):
    """PROVE RED:`"Content-Type": "application/octet-stream"` → `"text/plain"`
    → 端點 415,decode 丟錯;`"X-Token"` 拿掉 → 401。
    """
    client = DecodeClient(native_stub.url, SHARED)
    samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    result = await client.decode(
        samples, kind="final", prompt="以下是繁體中文。", beam=5, language="zh"
    )
    await client.aclose()

    assert result["text"] == "測試語音"
    assert native_stub.rejections == []

    call = native_stub.last_to("/transcribe")
    assert call.header("x-token") == SHARED
    assert (call.header("content-type") or "").split(";")[0] == "application/octet-stream"
    # 裸 PCM:無檔頭、int16、長度等於取樣數 × 2。
    assert len(call.body) == 1600 * 2
    assert call.query["kind"] == ["final"]
    assert call.query["beam"] == ["5"]
    assert call.query["language"] == ["zh"]
    assert call.query["prompt"] == ["以下是繁體中文。"]


@pytest.mark.asyncio
async def test_native_decode_with_a_wrong_shared_secret_is_rejected(native_stub):
    client = DecodeClient(native_stub.url, "wrong-secret")
    with pytest.raises(DecodeError):
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    assert native_stub.rejections, "挑剔端點沒有擋下錯的共享祕密"


# ── 探針:OpenAI 相容 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_probe_presents_the_full_request_shape(openai_stub):
    """探針送出去的東西跟真正的辨識請求是**同一種**請求,不是「差不多」。

    PROVE RED(逐一施加):
      * `headers = {}`(不送 Authorization)          → 端點 401 → 結論不是 ok
      * `f"Bearer {credential}"` → `f"Token {…}"`     → 端點 401 → 結論不是 ok
      * `files={"file": …}` → `files={"audio": …}`    → 記錄層斷言紅
      * `silence_wav(100)` → 裸 PCM                   → 記錄層斷言紅
      * `data` 拿掉 `"model"`                          → 記錄層斷言紅

    ⚠ 後三個**不會**讓結論變成非 ok,那是刻意的:`_probe_openai` 把
    400/415/422 當成「認證過了、只是嫌這段 100 ms 靜音」,因為真實端點確實會
    這樣回。所以它們只有記錄層擋得住。
    """
    probe = await probe_decode_target(
        openai_stub.url, KEY, protocol="openai", openai_model=MODEL
    )

    assert probe == {"ok": True, "reason": REASON_OK, "detail": None}
    assert openai_stub.rejections == []

    call = openai_stub.last_to("/v1/audio/transcriptions")
    assert call.header("authorization") == f"Bearer {KEY}"
    audio = call.part("file")
    assert audio is not None, f"探針送出去的部件是 {call.part_names}"
    assert audio.content_type == "audio/wav"
    # 100 ms @ 16 kHz = 1600 frames。裸 PCM 在這裡解不開。
    assert wav_facts(audio.content) == {
        "channels": 1,
        "sampwidth": 2,
        "framerate": 16_000,
        "frames": 1600,
    }
    assert call.form("model") == MODEL
    assert call.form("response_format") == "json"


@pytest.mark.asyncio
async def test_openai_probe_with_a_wrong_key_says_unauthorized(openai_stub):
    """挑剔端點回 401 → 必須是 decoder_unauthorized,不是 unreachable。"""
    probe = await probe_decode_target(
        openai_stub.url, "sk-wrong", protocol="openai", openai_model=MODEL
    )
    assert probe["ok"] is False
    assert probe["reason"] == REASON_DECODER_UNAUTHORIZED
    assert "sk-wrong" not in (probe["detail"] or "")


@pytest.mark.asyncio
async def test_openai_probe_with_the_right_key_never_says_unauthorized(openai_stub):
    """最貴的那個誤診,正面釘住:**正確的**金鑰不可以被說成沒授權。

    探針少送 `Authorization`(或 scheme 打錯)時,這條會抓到 —— 而它描述的
    正是活體症狀:麥克風被藏起來、operator 被派去輪替一把本來就對的金鑰。
    """
    probe = await probe_decode_target(
        openai_stub.url, KEY, protocol="openai", openai_model=MODEL
    )
    assert probe["reason"] != REASON_DECODER_UNAUTHORIZED
    assert probe["ok"] is True


# ── 探針:原生契約 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_probe_presents_the_shared_secret_on_the_transcribe_leg(native_stub):
    """PROVE RED:`"X-Token": credential or ""` 那行拿掉 → 端點 401 →
    探針回 decoder_unauthorized,兩層都紅。

    `/health` 是不認證的,所以「共享祕密錯了」只有在 `/transcribe` 那一腿看得
    出來 —— 那條腿的 header 掉了,health 就會對著一支能用的麥克風說沒授權。
    """
    probe = await probe_decode_target(native_stub.url, SHARED, protocol="native")

    assert probe == {"ok": True, "reason": REASON_OK, "detail": None}
    assert native_stub.rejections == []

    call = native_stub.last_to("/transcribe")
    assert call.header("x-token") == SHARED
    assert (call.header("content-type") or "").split(";")[0] == "application/octet-stream"
    assert call.query["kind"] == ["final"]


@pytest.mark.asyncio
async def test_native_probe_with_a_wrong_secret_says_unauthorized(native_stub):
    probe = await probe_decode_target(native_stub.url, "wrong-secret", protocol="native")
    assert probe["ok"] is False
    assert probe["reason"] == REASON_DECODER_UNAUTHORIZED


# ── §3:憑證不准出現在錯誤訊息或 log 行 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_native_error_message_drops_url_userinfo(native_stub):
    """operator 把憑證貼進位址 → httpx 的 HTTPStatusError 訊息會帶著**完整
    URL**,而那串字經 session.py 直接送進使用者的瀏覽器。

    PROVE RED:把 `_redact` 換回「只 replace self._credential」→ 這條紅
    (訊息裡會有 `user:sk-URL-CANARY-…@`)。
    """
    secret = "sk-URL-CANARY-abcdef"
    native_stub.forced_status = 500
    client = DecodeClient(native_stub.url_with_userinfo("operator", secret), SHARED)
    with pytest.raises(DecodeError) as excinfo:
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()

    message = str(excinfo.value)
    assert secret not in message
    assert "operator" not in message
    assert "@" not in message
    # 位址本身仍要看得見,否則 operator 不知道是打去哪裡壞的。
    assert "127.0.0.1" in message


@pytest.mark.asyncio
async def test_openai_error_message_carries_neither_secret(openai_stub):
    """openai 那條的錯誤訊息是自己組的(不含 URL),所以 userinfo 這半本來就
    進不去 —— 這條是把它**釘住**,免得哪天有人改成 `raise_for_status()`
    就悄悄變成跟原生那條一樣的洩漏。金鑰那半則是真的在擋 body 回音。
    """
    secret = "sk-URL-CANARY-openai"
    openai_stub.forced_status = 500
    client = OpenAIDecodeClient(
        openai_stub.url_with_userinfo("operator", secret), KEY, model=MODEL
    )
    with pytest.raises(DecodeError) as excinfo:
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    message = str(excinfo.value)
    assert secret not in message
    assert KEY not in message


@pytest.mark.asyncio
async def test_probe_detail_that_quotes_the_url_drops_userinfo(openai_stub):
    """探針的 detail 會出現在 `/asr/health` 上(目前未認證,見 runbook F10)。

    404 那條分支會把位址原樣引述出來 —— 位址要看得見(不然 operator 不知道
    打去哪裡),貼在裡面的憑證不可以。用一個不存在的子路徑逼出 404。
    """
    secret = "sk-URL-CANARY-probe"
    url = openai_stub.url_with_userinfo("operator", secret) + "/nope"
    probe = await probe_decode_target(url, KEY, protocol="openai", openai_model=MODEL)

    detail = probe["detail"] or ""
    assert probe["ok"] is False
    assert "404" in detail, detail
    assert "127.0.0.1" in detail  # 位址仍看得見
    assert secret not in detail
    assert "operator" not in detail


@pytest.mark.asyncio
async def test_a_three_character_credential_is_still_redacted(openai_stub):
    """PROVE RED:把 `redact_secrets` 的門檻換回 `len(token) >= 4` → 這條紅。

    短憑證被打成 `<redacted>` 只是訊息難讀;短憑證原樣送進瀏覽器是外洩。
    """
    short = "abc"
    openai_stub.forced_status = 401
    openai_stub.forced_body = f"invalid api key: {short}"
    client = OpenAIDecodeClient(openai_stub.url, short, model=MODEL)
    with pytest.raises(DecodeError) as excinfo:
        await client.decode(
            np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
        )
    await client.aclose()
    assert short not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


@pytest.mark.asyncio
async def test_configure_logging_stops_httpx_echoing_url_userinfo(native_stub, caplog):
    """httpx 每一次請求都印一行 INFO,`%s` 走 `URL.__str__` —— **不遮蔽密碼**
    (只有 `__repr__` 會)。所以是每 0.5 秒一次,不是只有出錯時。

    ⚠ 這條刻意呼叫**正式的** `_configure_logging`,不是自己把濾網掛上去 ——
    掛的動作在測試裡做,等於測試自己造出被測條件,production 沒接線也會綠。

    PROVE RED:把 `install_userinfo_redaction(...)` 從 `_configure_logging`
    拿掉、或把 "httpx" 從點名清單裡移掉 → 這條紅。
    """
    secret = "sk-LOG-CANARY-zyxwvu"
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    _configure_logging(
        Settings(ASR_DECODE_URL="https://d.example.test", ASR_DECODER_TOKEN="t")
    )
    try:
        assert any(
            isinstance(f, UserinfoRedactingFilter) for f in httpx_logger.filters
        ), "_configure_logging 沒有把濾網掛到 httpx 這個 logger 上"

        with caplog.at_level(logging.INFO, logger="httpx"):
            client = DecodeClient(
                native_stub.url_with_userinfo("operator", secret), SHARED
            )
            await client.decode(
                np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1
            )
            await client.aclose()
    finally:
        # 這是全域狀態 —— 還原,否則後面的測試就分不清是誰裝的。
        httpx_logger.filters = before

    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines, "httpx 沒有印出 request log —— 這條測試就白測了"
    for line in lines:
        assert secret not in line
        assert "operator@" not in line
        assert "127.0.0.1" in line  # 位址仍看得見
