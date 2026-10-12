"""切句 / 噪音門檻可由 env 調整(ASR_SPEECH_END_MS / ASR_MIN_RMS / ASR_START_RMS)。"""

from __future__ import annotations

import inspect

import numpy as np

from app import main as main_mod
from app.config import Settings
from app.transcriber import (
    MIN_RMS,
    SPEECH_END_FRAMES,
    START_RMS,
    FinalReady,
    SpeechEnd,
    SpeechStart,
    VadSegmenter,
)
from tests.test_transcriber import AlwaysVad, ScriptedVad, frames, of_type, silence


def _rms(pcm: bytes) -> float:
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(s**2)))


def test_settings_defaults_match_legacy_constants(monkeypatch):
    for k in ("ASR_SPEECH_END_MS", "ASR_MIN_RMS", "ASR_START_RMS", "ASR_VAD_AGGRESSIVENESS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.ASR_SPEECH_END_MS == 510 and -(-510 // 30) == SPEECH_END_FRAMES
    assert s.ASR_MIN_RMS == MIN_RMS
    assert s.ASR_START_RMS == START_RMS
    assert s.ASR_VAD_AGGRESSIVENESS == 2


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("ASR_SPEECH_END_MS", "900")
    monkeypatch.setenv("ASR_MIN_RMS", "0.012")
    monkeypatch.setenv("ASR_START_RMS", "0.015")
    monkeypatch.setenv("ASR_VAD_AGGRESSIVENESS", "3")
    s = Settings()
    assert (s.ASR_SPEECH_END_MS, s.ASR_MIN_RMS, s.ASR_START_RMS, s.ASR_VAD_AGGRESSIVENESS) == (
        900, 0.012, 0.015, 3)


def test_segmenter_defaults_unchanged():
    seg = VadSegmenter(vad=AlwaysVad(False))
    assert (seg.speech_end_frames, seg.min_rms, seg.start_rms) == (
        SPEECH_END_FRAMES, MIN_RMS, START_RMS)


def test_speech_end_ms_rounds_up_to_frames():
    assert VadSegmenter(vad=AlwaysVad(False), speech_end_ms=900).speech_end_frames == 30
    assert VadSegmenter(vad=AlwaysVad(False), speech_end_ms=901).speech_end_frames == 31


def test_longer_end_silence_keeps_short_pause_in_one_utterance():
    pause = 20  # 600 ms:預設(17 幀)會切句,900ms(30 幀)不會
    script = [True] * 40 + [False] * pause + [True] * 40 + [False] * 40
    audio = frames(40) + silence(pause) + frames(40) + silence(40)

    default = VadSegmenter(vad=ScriptedVad(script))
    assert len(of_type(default.feed(audio), SpeechStart)) == 2

    tuned = VadSegmenter(vad=ScriptedVad(script), speech_end_ms=900)
    ev = tuned.feed(audio)
    assert len(of_type(ev, SpeechStart)) == 1
    assert len(of_type(ev, SpeechEnd)) == 1


def test_start_rms_blocks_quiet_noise():
    quiet = frames(40, amplitude=0.03)
    level = _rms(quiet)
    loose = VadSegmenter(vad=AlwaysVad(True), start_rms=level * 0.5)
    assert of_type(loose.feed(quiet), SpeechStart)
    strict = VadSegmenter(vad=AlwaysVad(True), start_rms=level * 2)
    assert not of_type(strict.feed(quiet), SpeechStart)


def test_min_rms_discards_quiet_utterance():
    quiet = frames(40, amplitude=0.03)
    level = _rms(quiet)
    script = [True] * 40 + [False] * 40
    ok = VadSegmenter(vad=ScriptedVad(script), start_rms=0, min_rms=level * 0.5)
    assert of_type(ok.feed(quiet + silence(40)), FinalReady)
    drop = VadSegmenter(vad=ScriptedVad(script), start_rms=0, min_rms=level * 2)
    assert not of_type(drop.feed(quiet + silence(40)), FinalReady)


def test_ws_handler_wires_settings_into_segmenter():
    src = inspect.getsource(main_mod)
    for kw in ("vad_aggressiveness=s.ASR_VAD_AGGRESSIVENESS",
               "speech_end_ms=s.ASR_SPEECH_END_MS",
               "min_rms=s.ASR_MIN_RMS",
               "start_rms=s.ASR_START_RMS"):
        assert kw in src
