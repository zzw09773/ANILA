"""asr-decoder 的 HTTP 契約測試。

decoder 以假件注入 → 不載入 faster-whisper 權重,測試在 CPU-only、無 GPU、
無網路的機器上也能跑(CI 與 air-gap build 都是這種環境)。
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import SAMPLE_RATE, _resolve_model_ref

TOKEN = "unit-test-token"


class FakeDecoder:
    """記下最後一次 transcribe 的參數,讓測試能斷言 partial/final 分流。"""

    def __init__(self, *, ready: bool = True) -> None:
        self._ready = ready
        self.calls: list[dict] = []

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def info(self) -> dict:
        return {
            "model": "fake",
            "device": "cpu",
            "compute_type": "int8",
            "ready": self._ready,
        }

    def transcribe(self, samples, *, kind, prompt, beam, language) -> dict:
        self.calls.append(
            {
                "samples": samples,
                "kind": kind,
                "prompt": prompt,
                "beam": beam,
                "language": language,
            }
        )
        return {
            "text": "測試文字",
            "no_speech_prob": 0.01,
            "avg_logprob": -0.2,
            "decode_seconds": 0.123,
        }


def build_client(decoder: FakeDecoder | None = None) -> tuple[TestClient, FakeDecoder]:
    decoder = decoder or FakeDecoder()
    app = create_app(
        decoder=decoder,
        app_settings=Settings(ASR_DECODER_TOKEN=TOKEN),
    )
    return TestClient(app), decoder


def pcm(seconds: float = 1.0, value: int = 1000) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    return np.full(n, value, dtype=np.int16).tobytes()


# ── auth ────────────────────────────────────────────────────────────────


def test_transcribe_rejects_missing_token():
    client, _ = build_client()
    resp = client.post("/transcribe", content=pcm(0.5))
    assert resp.status_code == 401


def test_transcribe_rejects_wrong_token():
    client, _ = build_client()
    resp = client.post(
        "/transcribe", content=pcm(0.5), headers={"X-Token": "wrong"}
    )
    assert resp.status_code == 401


def test_transcribe_accepts_correct_token():
    client, _ = build_client()
    resp = client.post(
        "/transcribe", content=pcm(0.5), headers={"X-Token": TOKEN}
    )
    assert resp.status_code == 200


def test_missing_token_fails_loud_at_startup():
    with pytest.raises(RuntimeError, match="ASR_DECODER_TOKEN"):
        create_app(decoder=FakeDecoder(), app_settings=Settings(ASR_DECODER_TOKEN=""))


# ── 契約 ─────────────────────────────────────────────────────────────────


def test_transcribe_returns_contract_fields():
    client, _ = build_client()
    resp = client.post(
        "/transcribe?kind=final&beam=5&prompt=以下是繁體中文。&language=zh",
        content=pcm(1.0),
        headers={"X-Token": TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"text", "no_speech_prob", "avg_logprob", "decode_seconds"}
    assert body["text"] == "測試文字"


def test_query_params_reach_the_decoder():
    client, decoder = build_client()
    client.post(
        "/transcribe?kind=partial&beam=3&prompt=提示&language=zh",
        content=pcm(0.5),
        headers={"X-Token": TOKEN},
    )
    call = decoder.calls[-1]
    assert call["kind"] == "partial"
    assert call["beam"] == 3
    assert call["prompt"] == "提示"
    assert call["language"] == "zh"


def test_empty_prompt_becomes_none():
    """空字串當 initial_prompt 會讓 whisper 收到一個空的 prompt token 序列;
    gateway 沒設 prompt 時應該是「不帶」而不是「帶空的」。"""
    client, decoder = build_client()
    client.post("/transcribe", content=pcm(0.5), headers={"X-Token": TOKEN})
    assert decoder.calls[-1]["prompt"] is None


def test_pcm_is_decoded_to_normalised_float32():
    client, decoder = build_client()
    client.post(
        "/transcribe", content=pcm(0.1, value=16384), headers={"X-Token": TOKEN}
    )
    samples = decoder.calls[-1]["samples"]
    assert samples.dtype == np.float32
    assert len(samples) == int(0.1 * SAMPLE_RATE)
    np.testing.assert_allclose(samples[0], 0.5, atol=1e-4)


def test_defaults_are_final_and_beam_5():
    client, decoder = build_client()
    client.post("/transcribe", content=pcm(0.5), headers={"X-Token": TOKEN})
    assert decoder.calls[-1]["kind"] == "final"
    assert decoder.calls[-1]["beam"] == 5


# ── 輸入驗證 ─────────────────────────────────────────────────────────────


def test_rejects_unknown_kind():
    client, _ = build_client()
    resp = client.post(
        "/transcribe?kind=bogus", content=pcm(0.5), headers={"X-Token": TOKEN}
    )
    assert resp.status_code == 400


def test_rejects_empty_body():
    client, _ = build_client()
    resp = client.post("/transcribe", content=b"", headers={"X-Token": TOKEN})
    assert resp.status_code == 400


def test_rejects_odd_length_body():
    """Int16 樣本必須成對;奇數長度代表 gateway 送壞了,寧可 400 也不要
    np.frombuffer 直接丟 ValueError 變成 500。"""
    client, _ = build_client()
    resp = client.post("/transcribe", content=b"\x01\x02\x03", headers={"X-Token": TOKEN})
    assert resp.status_code == 400


def test_rejects_audio_over_the_cap():
    client, decoder = build_client()
    app = create_app(
        decoder=decoder,
        app_settings=Settings(ASR_DECODER_TOKEN=TOKEN, ASR_MAX_AUDIO_SECONDS=1.0),
    )
    resp = TestClient(app).post(
        "/transcribe", content=pcm(2.0), headers={"X-Token": TOKEN}
    )
    assert resp.status_code == 413


def test_rejects_out_of_range_beam():
    client, _ = build_client()
    resp = client.post(
        "/transcribe?beam=99", content=pcm(0.5), headers={"X-Token": TOKEN}
    )
    assert resp.status_code == 422


# ── health / readiness ──────────────────────────────────────────────────


def test_health_ok_when_ready():
    client, _ = build_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["ready"] is True


def test_health_503_while_loading():
    client, _ = build_client(FakeDecoder(ready=False))
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False


def test_health_needs_no_token():
    """compose healthcheck 不該需要密鑰。"""
    client, _ = build_client()
    assert client.get("/health").status_code == 200


def test_transcribe_503_while_loading():
    client, decoder = build_client(FakeDecoder(ready=False))
    resp = client.post("/transcribe", content=pcm(0.5), headers={"X-Token": TOKEN})
    assert resp.status_code == 503
    assert decoder.calls == []


# ── 模型路徑解析 ─────────────────────────────────────────────────────────


def test_model_ref_plain_size_when_no_dir():
    assert _resolve_model_ref(Settings(ASR_MODEL_SIZE="large-v3")) == ("large-v3", None)


def test_model_dir_with_weights_is_used_as_the_model_path(tmp_path):
    """air-gap bundle 直接把轉檔好的模型掛進來 → 目錄本身就是模型路徑。"""
    (tmp_path / "model.bin").write_bytes(b"stub")
    ref, root = _resolve_model_ref(
        Settings(ASR_MODEL_SIZE="large-v3", ASR_MODEL_DIR=str(tmp_path))
    )
    assert ref == str(tmp_path)
    assert root is None


def test_model_dir_without_weights_is_a_download_root(tmp_path):
    ref, root = _resolve_model_ref(
        Settings(ASR_MODEL_SIZE="medium", ASR_MODEL_DIR=str(tmp_path))
    )
    assert ref == "medium"
    assert root == str(tmp_path)
