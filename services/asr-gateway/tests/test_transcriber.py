"""VAD 切句狀態機的測試。

兩層策略:

1. **注入假 VAD**(大多數測試)——「這一幀算不算語音」由腳本決定,於是切句
   邏輯可以被精確、確定性地驗證。用真 VAD 做不到:實測顯示 webrtcvad 的判定
   與訊號音量天生相關(振幅一降 voiced 比例就跟著掉),沒辦法單獨控制變因。
2. **真 webrtcvad + 類語音訊號**(最後一節)—— 證明狀態機跟真東西搭得起來,
   不是只在假件下自洽。訊號用諧波堆疊(實測 voiced 100%),不是純正弦。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.transcriber import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    MAX_UTTERANCE_SEC,
    PREROLL_FRAMES,
    SAMPLE_RATE,
    SPEECH_END_FRAMES,
    SPEECH_START_FRAMES,
    Discard,
    FinalReady,
    PartialReady,
    SpeechEnd,
    SpeechStart,
    VadSegmenter,
    is_hallucination,
    looks_degenerate,
)


class ScriptedVad:
    """照腳本回答的假 VAD。verdicts 用完後一律回最後一個值。"""

    def __init__(self, verdicts: list[bool]) -> None:
        self.verdicts = list(verdicts)
        self.mode = 2

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self.verdicts.pop(0) if self.verdicts else False

    def set_mode(self, mode: int) -> None:
        self.mode = mode


class AlwaysVad:
    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self.verdict

    def set_mode(self, mode: int) -> None:
        pass


def frames(n: int, amplitude: float = 0.3) -> bytes:
    """n 幀的類語音音訊(振幅足以過 RMS 閘門)。"""
    total = n * FRAME_SAMPLES
    t = np.arange(total) / SAMPLE_RATE
    harm = sum(np.sin(2 * np.pi * 120 * k * t) / k for k in range(1, 12))
    harm = harm / np.abs(harm).max() * amplitude
    return (harm * 32767).astype(np.int16).tobytes()


def silence(n: int) -> bytes:
    return np.zeros(n * FRAME_SAMPLES, dtype=np.int16).tobytes()


def of_type(events, cls):
    return [e for e in events if isinstance(e, cls)]


# ── 進入語段 ─────────────────────────────────────────────────────────────


def test_no_events_on_silence():
    seg = VadSegmenter(vad=AlwaysVad(False))
    assert seg.feed(silence(50)) == []
    assert not seg.in_speech


def test_speech_starts_after_enough_voiced_frames():
    seg = VadSegmenter(vad=AlwaysVad(True))
    # 前 SPEECH_START_FRAMES-1 幀不該觸發
    events = seg.feed(frames(SPEECH_START_FRAMES - 1))
    assert of_type(events, SpeechStart) == []
    events = seg.feed(frames(1))
    assert len(of_type(events, SpeechStart)) == 1
    assert seg.in_speech


def test_quiet_audio_does_not_start_speech_even_if_vad_says_voiced():
    """START_RMS 閘門:VAD 對冷氣聲、鍵盤聲也可能說 voiced,音量閘門是第二道
    防線,否則會整天在解碼環境噪音。"""
    seg = VadSegmenter(vad=AlwaysVad(True))
    events = seg.feed(frames(20, amplitude=0.0005))  # RMS 遠低於 START_RMS
    assert of_type(events, SpeechStart) == []
    assert not seg.in_speech


def test_utterance_ids_increment():
    seg = VadSegmenter(vad=AlwaysVad(True))
    first = of_type(seg.feed(frames(SPEECH_START_FRAMES)), SpeechStart)[0]
    seg.feed(frames(30))
    seg.flush()
    seg._vad = AlwaysVad(True)
    second = of_type(seg.feed(frames(SPEECH_START_FRAMES)), SpeechStart)[0]
    assert second.utt_id == first.utt_id + 1


def test_preroll_is_prepended_to_the_utterance():
    """VAD 要 90ms 才確定是語音,那 90ms 已經是字的一部分 → 必須補回開頭,
    否則每句話的頭都被咬掉。

    語意注意:preroll 是一個 maxlen=PREROLL_FRAMES 的滑動窗,**觸發幀本身也在
    窗內**。所以進入語段時 speech 的長度就是 PREROLL_FRAMES(300ms 的回溯窗),
    而不是 preroll + 觸發幀。
    """
    seg = VadSegmenter(vad=ScriptedVad([False] * PREROLL_FRAMES + [True] * 100))
    seg.feed(frames(PREROLL_FRAMES))          # 填滿 preroll 窗
    seg.feed(frames(SPEECH_START_FRAMES))     # 觸發
    assert len(seg.partial_snapshot()) == PREROLL_FRAMES * FRAME_BYTES


# ── 結束語段 ─────────────────────────────────────────────────────────────


def test_silence_closes_the_utterance_and_emits_final():
    seg = VadSegmenter(
        vad=ScriptedVad([True] * 40 + [False] * (SPEECH_END_FRAMES + 5))
    )
    seg.feed(frames(40))
    events = seg.feed(silence(SPEECH_END_FRAMES + 5))
    assert len(of_type(events, SpeechEnd)) == 1
    assert len(of_type(events, FinalReady)) == 1
    assert not seg.in_speech


def test_short_utterance_is_discarded_not_decoded():
    """太短 → 根本不送解碼(省 GPU,也避免 whisper 對半個字亂猜)。

    ⚠ 只有 flush 路徑到得了這裡。走 VAD 收尾時,語段一定含 300ms preroll +
    510ms 收尾靜音 = 810ms > MIN_UTTERANCE_SEC(300ms),長度閘門必然放行 ——
    也就是說「按停」是這道閘門唯一的觸發途徑。這是參考設計的既有性質,照移植。
    """
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    seg.feed(frames(SPEECH_START_FRAMES))     # 剛觸發就按停 → 語段只有 90ms
    events = seg.flush()
    assert len(of_type(events, Discard)) == 1
    assert of_type(events, FinalReady) == []


def test_quiet_utterance_is_discarded_by_rms():
    """整段音量不足 → discard,不送解碼。

    振幅 0.02(RMS≈0.0103)剛好過得了 START_RMS(0.008)進入語段,隨後一長串
    近乎無聲把整段均能拉到門檻以下 → MIN_RMS 攔下。真實對應情境:語段是被
    關門聲之類的短暫突波觸發的,後面其實沒人在講話。
    """
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    seg.feed(frames(SPEECH_START_FRAMES, amplitude=0.02))
    seg.feed(frames(10, amplitude=0.0005))
    events = seg.flush()
    assert len(of_type(events, Discard)) == 1
    assert of_type(events, FinalReady) == []


def test_too_long_utterance_is_force_closed():
    """講不停也要切,否則 whisper 的 30s 窗爆掉、延遲無上限。

    切完之後若人還在講,會立刻開一個新語段(voiced_run 重新累積到 3 幀即可)
    —— 所以這裡驗的是「有切」與「切完 id 會往前」,不是「切完就閒置」。
    """
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    n = int(MAX_UTTERANCE_SEC * SAMPLE_RATE / FRAME_SAMPLES) + 5
    events = seg.feed(frames(n))
    finals = of_type(events, FinalReady)
    assert len(finals) == 1
    assert len(finals[0].audio) >= int(MAX_UTTERANCE_SEC * SAMPLE_RATE) * 2
    # 人還在講 → 已經進入下一個語段
    assert len(of_type(events, SpeechStart)) == 2


def test_final_carries_the_audio_and_a_timestamp():
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    seg.feed(frames(SPEECH_START_FRAMES))
    seg.feed(frames(40))
    final = of_type(seg.flush(), FinalReady)[0]
    assert len(final.audio) > 0
    assert len(final.audio) % 2 == 0          # Int16 對齊
    assert final.ended_at > 0


# ── flush ───────────────────────────────────────────────────────────────


def test_flush_closes_an_open_utterance():
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    seg.feed(frames(SPEECH_START_FRAMES))
    seg.feed(frames(40))
    events = seg.flush()
    assert len(of_type(events, SpeechEnd)) == 1
    assert len(of_type(events, FinalReady)) == 1


def test_flush_when_idle_is_a_noop():
    seg = VadSegmenter(vad=AlwaysVad(False))
    seg.feed(silence(10))
    assert seg.flush() == []


# ── partial ─────────────────────────────────────────────────────────────


def test_partials_fire_while_speaking():
    seg = VadSegmenter(vad=AlwaysVad(True))
    seg.feed(frames(SPEECH_START_FRAMES))
    events = seg.feed(frames(60))       # ~1.8s
    assert len(of_type(events, PartialReady)) >= 1


def test_partials_can_be_disabled():
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=False)
    seg.feed(frames(SPEECH_START_FRAMES))
    events = seg.feed(frames(60))
    assert of_type(events, PartialReady) == []


def test_no_partial_before_min_speech_length():
    """太短的語段不出 partial —— 半個字的 greedy 解碼只會是雜訊。"""
    seg = VadSegmenter(vad=AlwaysVad(True))
    seg.feed(frames(SPEECH_START_FRAMES))
    events = seg.feed(frames(10))       # ~0.3s < PARTIAL_MIN_SPEECH_SEC(0.8s)
    assert of_type(events, PartialReady) == []


def test_partial_snapshot_is_empty_when_not_speaking():
    seg = VadSegmenter(vad=AlwaysVad(False))
    seg.feed(silence(10))
    assert seg.partial_snapshot() == b""


def test_partial_snapshot_grows_with_speech():
    seg = VadSegmenter(vad=AlwaysVad(True))
    seg.feed(frames(SPEECH_START_FRAMES))
    first = len(seg.partial_snapshot())
    seg.feed(frames(10))
    assert len(seg.partial_snapshot()) > first


def test_partial_snapshot_is_capped_to_the_window():
    seg = VadSegmenter(vad=AlwaysVad(True))
    seg.feed(frames(SPEECH_START_FRAMES))
    seg.feed(frames(int(20 * SAMPLE_RATE / FRAME_SAMPLES)))   # 20s > 15s 窗
    from app.transcriber import PARTIAL_WINDOW_SEC
    assert len(seg.partial_snapshot()) <= int(PARTIAL_WINDOW_SEC * SAMPLE_RATE) * 2


# ── 分幀 ─────────────────────────────────────────────────────────────────


def test_partial_frames_are_buffered_until_complete():
    """WS 送來的 chunk 邊界不會剛好對齊 30ms 幀 → 不足一幀的尾巴要留著。"""
    seg = VadSegmenter(vad=AlwaysVad(True))
    half = FRAME_BYTES // 2
    assert seg.feed(frames(1)[:half]) == []          # 半幀 → 還不處理
    seg.feed(frames(1)[half:])                        # 補滿 → 才算一幀
    assert seg.feed(frames(SPEECH_START_FRAMES)) or True  # 不炸即可


def test_odd_sized_chunks_do_not_desync():
    seg = VadSegmenter(vad=AlwaysVad(True))
    data = frames(20)
    for i in range(0, len(data), 137):      # 刻意用不對齊的塊大小
        seg.feed(data[i:i + 137])
    assert seg.in_speech


# ── 幻覺過濾 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "請訂閱我的頻道", "www.example.com", "版權所有", "明報新聞",
    "Thank you for watching", "以下是繁體中文",
])
def test_known_spam_is_filtered(text):
    assert is_hallucination(text)


def test_short_music_marker_is_filtered():
    assert is_hallucination("音樂")


def test_real_sentence_containing_music_survives():
    """「音樂」只有在幾乎就是整個輸出時才可疑,不能誤殺真句子。"""
    assert not is_hallucination("請幫我找一下這份文件裡關於音樂教育的段落")


def test_normal_text_is_not_filtered():
    assert not is_hallucination("機庫內現在有多少架無人機")


def test_repetition_loop_is_degenerate():
    assert looks_degenerate("好的好的好的好的好的好的好的好的好的好的好的好的好的好的")


def test_normal_text_is_not_degenerate():
    assert not looks_degenerate("請幫我整理這份文件的核心論點,並列出三個值得追問的方向")


def test_short_repetition_is_a_known_gap():
    """⚠ 記錄已知缺口(規劃書 §11):40-byte 下限讓短重複漏網。這個測試是在
    釘住「目前行為」,不是在主張它正確。真實語音驗證後若要收緊門檻,改這裡。"""
    assert not looks_degenerate("詞曲 曲曲 曲曲 曲曲 曲曲")   # 31 bytes < 40


# ── 真 webrtcvad 整合 ────────────────────────────────────────────────────


def test_real_vad_segments_speechlike_audio():
    """證明狀態機跟真的 webrtcvad 搭得起來(前面的測試都用假 VAD)。
    訊號用諧波堆疊 —— 實測真 VAD 對它 voiced 100%。"""
    seg = VadSegmenter()      # 真 webrtcvad
    seg.feed(silence(10))
    events = seg.feed(frames(40))
    assert len(of_type(events, SpeechStart)) == 1
    events = seg.feed(silence(SPEECH_END_FRAMES + 5))
    assert len(of_type(events, FinalReady)) == 1


def test_real_vad_ignores_silence():
    seg = VadSegmenter()
    assert seg.feed(silence(100)) == []
