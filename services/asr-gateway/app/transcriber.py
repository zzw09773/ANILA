"""VAD 切句狀態機 —— 純同步、無 I/O、無執行緒。

移植自 `語料庫/ASR_intranet/code_for_mlsteam/streaming_asr/transcriber.py`
(內網實測過的版本)。**常數與判斷邏輯逐行照抄,不要憑感覺調** —— 那些數字
是在真實內網環境用出來的。

與原版的差異只有一處,且是刻意的:原版把「解碼」也扛在自己身上(每個 session
一條 worker thread + `threading.Condition`),因為它跑在 Flask-SocketIO 的
threading 模型下。本專案的 gateway 是 async FastAPI,所以這裡只留**純粹的狀態
機**:餵 bytes 進來、吐 event 出去,解碼與 WS 推送由 `session.py` 的 async 層
負責。好處是這一層完全可測(不需要 event loop、不需要 mock 執行緒),而 async
單執行緒也讓原版用來保護共享狀態的鎖全部消失。

用法:

    seg = VadSegmenter()
    for event in seg.feed(pcm_bytes):   # Int16 mono PCM @16 kHz
        ...
    for event in seg.flush():           # 使用者按停 → 收尾當前語段
        ...
"""

from __future__ import annotations

import re
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import webrtcvad


class Vad(Protocol):
    """webrtcvad.Vad 的介面。抽出來是為了測試能注入腳本化的 VAD:VAD 的判定
    與音量天生相關(實測:合成訊號振幅一降,voiced 比例跟著掉),用真 VAD 很難
    單獨驗「VAD 說有聲但音量不足」這條路徑。注入假 VAD 才測得到自己的邏輯,
    而不是在測 webrtcvad 的統計行為。"""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...
    def set_mode(self, mode: int) -> None: ...

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000       # 480
FRAME_BYTES = FRAME_SAMPLES * 2                      # Int16 mono

SPEECH_START_FRAMES = 3      # ~90 ms voiced to enter an utterance
SPEECH_END_FRAMES = 17       # ~510 ms silence to close it
PREROLL_FRAMES = 10          # ~300 ms kept from before the detected onset
MAX_UTTERANCE_SEC = 30.0
PARTIAL_INTERVAL_SEC = 0.5
PARTIAL_MIN_SPEECH_SEC = 0.8
PARTIAL_WINDOW_SEC = 15.0
MIN_UTTERANCE_SEC = 0.3
MIN_RMS = 0.008
START_RMS = MIN_RMS          # voiced trigger frames need real energy too
PARTIAL_NO_SPEECH_PROB = 0.6
PARTIAL_MIN_AVG_LOGPROB = -1.0

# Spam-only markers that are never real speech content (incl. echo of the
# generic traditional prompt) — drop unconditionally.
HALLUCINATION_ALWAYS_RE = re.compile(
    r"ming pao|明報|www\.|請訂閱|\bsubscribe\b|copyright|版權所有|"
    r"thank you for watching|以下是繁體中文",
    re.IGNORECASE,
)
# Everyday words Whisper emits for music/silence; only damning when they are
# essentially the whole (short) output, so real sentences containing them survive.
HALLUCINATION_SHORT_RE = re.compile(r"音樂|music|字幕|subtitle", re.IGNORECASE)


def is_hallucination(text: str) -> bool:
    if HALLUCINATION_ALWAYS_RE.search(text):
        return True
    return len(text) <= 8 and bool(HALLUCINATION_SHORT_RE.search(text))


def looks_degenerate(text: str) -> bool:
    """Repetition loops (common in greedy partial decodes of cut-off audio)
    compress too well — same idea as Whisper's compression_ratio check.

    ⚠ 已知缺口:40-byte 下限讓短重複漏網(例:`詞曲 曲曲 曲曲 曲曲 曲曲` 只有
    31 bytes)。**先不要動這個數字** —— 那是對合成正弦波過擬合;真實輸入還有
    webrtcvad + RMS 兩道閘門在前面。要改請先用真實語音量測。
    見 docs/planning/asr-voice-input-plan.md §11。
    """
    data = text.encode("utf-8")
    if len(data) < 40:
        return False
    return len(data) / len(zlib.compress(data)) > 2.4


# ── events ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpeechStart:
    utt_id: int


@dataclass(frozen=True)
class SpeechEnd:
    utt_id: int


@dataclass(frozen=True)
class Discard:
    """太短 / 太小聲 → 根本沒送去解碼。前端要把該 id 的 partial 清掉。"""
    utt_id: int


@dataclass(frozen=True)
class FinalReady:
    utt_id: int
    audio: bytes
    ended_at: float


@dataclass(frozen=True)
class PartialReady:
    """刻意不帶 audio:snapshot 由 session 在**真正要解碼的那一刻**才向
    `partial_snapshot()` 拿,這樣解碼前多講的那段也會被納入(對齊原版 worker
    在 decode 時才 snapshot 的行為)。async 單執行緒,中間沒有 await,不會有
    資料競爭。"""
    utt_id: int


Event = SpeechStart | SpeechEnd | Discard | FinalReady | PartialReady


class VadSegmenter:
    """Push-audio VAD 切句器。餵 Int16 PCM,吐 event。"""

    def __init__(
        self,
        *,
        vad_aggressiveness: int = 2,
        partials_enabled: bool = True,
        vad: Vad | None = None,
    ) -> None:
        self._vad = vad if vad is not None else webrtcvad.Vad(vad_aggressiveness)
        self.partials_enabled = partials_enabled
        self._pending = bytearray()
        self._trigger_rms: deque[float] = deque(maxlen=SPEECH_START_FRAMES)
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._speech = bytearray()
        self._voiced_run = 0
        self._unvoiced_run = 0
        self._in_speech = False
        self._utt_id = 0
        self._samples_since_partial = 0

    # ── public API ──────────────────────────────────────────────────────

    def feed(self, data: bytes) -> list[Event]:
        events: list[Event] = []
        self._pending.extend(data)
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._process_frame(frame, events)
        return events

    def flush(self) -> list[Event]:
        """使用者按停:把還在講的那一段強制收尾。"""
        events: list[Event] = []
        if self._in_speech:
            self._end_utterance(events)
        return events

    def partial_snapshot(self) -> bytes:
        """當前語段的尾段(最多 PARTIAL_WINDOW_SEC)。沒在講話時回空。"""
        if not self._in_speech or not self._speech:
            return b""
        window_bytes = int(PARTIAL_WINDOW_SEC * SAMPLE_RATE) * 2
        return bytes(self._speech[-window_bytes:])

    def set_vad_aggressiveness(self, level: int) -> None:
        self._vad.set_mode(int(level))

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def current_utt_id(self) -> int:
        """當前(或最後一個)語段 id。

        partial 解碼必須在**要解的那一刻**同時取這個值與 `partial_snapshot()`,
        不能沿用 `PartialReady` 事件裡的 id:事件排隊期間語段可能已經收尾、
        下一段已經開始,那時 snapshot 回的是新語段的音訊,配舊 id 就會讓前端
        把新句子的預覽蓋到舊句子上。原版 worker 也是這樣做的。
        """
        return self._utt_id

    # ── state machine ───────────────────────────────────────────────────

    def _process_frame(self, frame: bytes, events: list[Event]) -> None:
        try:
            voiced = self._vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            voiced = False

        if voiced:
            self._voiced_run += 1
            self._unvoiced_run = 0
            if not self._in_speech:
                samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                self._trigger_rms.append(float(np.sqrt(np.mean(samples**2))))
        else:
            self._unvoiced_run += 1
            if not self._in_speech:
                self._voiced_run = 0
                self._trigger_rms.clear()

        if not self._in_speech:
            self._preroll.append(frame)
            # VAD 說「有聲」還不夠:冷氣聲、鍵盤聲都會被判 voiced。要求觸發的
            # 那幾幀有真實能量,才不會整天在解碼環境噪音。
            loud_enough = (
                len(self._trigger_rms) == SPEECH_START_FRAMES
                and sum(self._trigger_rms) / len(self._trigger_rms) >= START_RMS
            )
            if self._voiced_run >= SPEECH_START_FRAMES and loud_enough:
                self._in_speech = True
                self._utt_id += 1
                self._trigger_rms.clear()
                self._speech.clear()
                # preroll:VAD 要 90ms 才確定「這是語音」,那 90ms 已經是字的
                # 一部分了。補回前 300ms,否則每句話的頭都會被咬掉。
                for kept in self._preroll:
                    self._speech.extend(kept)
                self._preroll.clear()
                self._samples_since_partial = len(self._speech) // 2
                events.append(SpeechStart(self._utt_id))
            return

        self._speech.extend(frame)
        self._samples_since_partial += FRAME_SAMPLES
        too_long = len(self._speech) >= int(MAX_UTTERANCE_SEC * SAMPLE_RATE) * 2
        if self._unvoiced_run >= SPEECH_END_FRAMES or too_long:
            self._end_utterance(events)
        elif (
            self.partials_enabled
            and self._samples_since_partial >= int(PARTIAL_INTERVAL_SEC * SAMPLE_RATE)
            and len(self._speech) >= int(PARTIAL_MIN_SPEECH_SEC * SAMPLE_RATE) * 2
        ):
            self._samples_since_partial = 0
            events.append(PartialReady(self._utt_id))

    def _end_utterance(self, events: list[Event]) -> None:
        audio = bytes(self._speech)
        utt_id = self._utt_id
        self._speech.clear()
        self._in_speech = False
        self._voiced_run = 0
        self._unvoiced_run = 0
        events.append(SpeechEnd(utt_id))

        if len(audio) < int(MIN_UTTERANCE_SEC * SAMPLE_RATE) * 2:
            events.append(Discard(utt_id))
            return
        # 收尾的那 510ms 靜音不該算進音量判斷,否則長尾巴會把整句的 RMS 拉低。
        tail = SPEECH_END_FRAMES * FRAME_BYTES
        voiced = audio[:-tail] if len(audio) > tail + FRAME_BYTES else audio
        samples = np.frombuffer(voiced, dtype=np.int16).astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(samples**2))) < MIN_RMS:
            events.append(Discard(utt_id))
            return
        events.append(FinalReady(utt_id, audio, time.monotonic()))
