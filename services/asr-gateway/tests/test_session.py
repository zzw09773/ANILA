"""async 編排層測試 —— 驗規劃書 §2.2 的七條必守規則。

解碼以假件注入(可控延遲、可控回傳),不需要 decoder 服務、不需要 GPU。
VAD 也注入,理由同 test_transcriber.py。
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.decode_client import DecodeError
from app.session import AsrSession, make_text_filter
from app.transcriber import FRAME_SAMPLES, SAMPLE_RATE, SPEECH_START_FRAMES, VadSegmenter

from tests.test_transcriber import AlwaysVad, frames


class FakeDecode:
    """記錄呼叫並回傳腳本化結果;可注入延遲以製造並行競態。"""

    def __init__(self, *, text="辨識結果", delay=0.0, fail_final=False):
        self.calls: list[dict] = []
        self.text = text
        self.delay = delay
        self.fail_final = fail_final
        self.started = asyncio.Event()

    async def __call__(self, samples, *, kind, prompt, beam, language="zh"):
        self.calls.append({"kind": kind, "prompt": prompt, "beam": beam,
                           "n": len(samples)})
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if kind == "final" and self.fail_final:
            raise DecodeError("GPU 解碼無回應(測試)")
        return {"text": self.text, "no_speech_prob": 0.01,
                "avg_logprob": -0.2, "decode_seconds": 0.1}


class Recorder:
    def __init__(self):
        self.sent: list[dict] = []

    async def __call__(self, msg: dict) -> None:
        self.sent.append(msg)

    def types(self, t):
        return [m for m in self.sent if m["type"] == t]


def build(decode=None, *, partials_enabled=True, **kw):
    decode = decode or FakeDecode()
    rec = Recorder()
    seg = VadSegmenter(vad=AlwaysVad(True), partials_enabled=partials_enabled)
    s = AsrSession(decode=decode, send=rec, segmenter=seg,
                   partials_enabled=partials_enabled, **kw)
    return s, rec, decode, seg


async def settle(n=8):
    """讓 worker task 跑完手上的事。"""
    for _ in range(n):
        await asyncio.sleep(0)


# ── 基本流程 ─────────────────────────────────────────────────────────────


async def test_speech_start_and_end_emit_status():
    s, rec, _, _ = build(partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.flush()
    await settle()
    states = [m["state"] for m in rec.types("status")]
    assert "recording" in states and "listening" in states
    await s.aclose()


async def test_final_is_decoded_and_sent():
    s, rec, dec, _ = build(partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await s.drain(1.0)
    await settle()
    finals = rec.types("final")
    assert len(finals) == 1
    assert finals[0]["text"] == "辨識結果"
    assert finals[0]["latency_seconds"] >= 0
    assert dec.calls[-1]["kind"] == "final"
    await s.aclose()


async def test_final_uses_beam_and_prompt():
    s, _, dec, _ = build(partials_enabled=False, initial_prompt="以下是繁體中文。",
                         beam_size=5)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await s.drain(1.0)
    await settle()
    call = [c for c in dec.calls if c["kind"] == "final"][-1]
    assert call["beam"] == 5
    assert call["prompt"] == "以下是繁體中文。"
    await s.aclose()


# ── 規則 1:final 優先 ───────────────────────────────────────────────────


async def test_final_is_decoded_before_pending_partial():
    """佇列裡有 final 待解時不送 partial。"""
    dec = FakeDecode()
    s, rec, _, _ = build(dec)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    # 一次餵進足以同時產生 partial 與 final 的音訊,再立刻 flush
    await s.feed(frames(60))
    await s.flush()
    await settle(20)
    kinds = [c["kind"] for c in dec.calls]
    assert "final" in kinds
    assert kinds[0] == "final", f"final 應該先解,實際順序:{kinds}"
    await s.aclose()


# ── 規則 2:單 in-flight partial + 塌縮 ──────────────────────────────────


async def test_multiple_partial_events_collapse_into_one_decode():
    """partial 事件連發時不該一次次排隊解碼 —— 只解最新的一次。"""
    dec = FakeDecode(delay=0.05)
    s, _, _, _ = build(dec, partials_enabled=True)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    for _ in range(5):                      # 連續產生多次 PartialReady
        await s.feed(frames(20))
    await asyncio.sleep(0.2)
    partials = [c for c in dec.calls if c["kind"] == "partial"]
    assert len(partials) <= 2, f"partial 應塌縮,實際解了 {len(partials)} 次"
    await s.aclose()


async def test_partial_is_skipped_when_snapshot_is_empty():
    """語段已收尾 → snapshot 空 → 放棄該次 partial(不要拿空音訊去解)。"""
    dec = FakeDecode()
    s, _, _, seg = build(dec)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(60))       # 觸發 PartialReady
    seg.flush()                    # 直接讓 segmenter 收尾 → snapshot 變空
    await settle(10)
    assert [c for c in dec.calls if c["kind"] == "partial"] == []
    await s.aclose()


# ── 規則 3:stale partial 必須丟棄 ───────────────────────────────────────


async def test_partial_arriving_after_discard_is_dropped():
    """核心競態:partial 解碼途中該 utt 被 discard → 結果不得推送,否則
    預覽文字會在定稿後復活且永遠清不掉。"""
    dec = FakeDecode(delay=0.1)
    s, rec, _, seg = build(dec)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(60))                     # 觸發 partial 解碼
    await dec.started.wait()                     # 確定 partial 已在飛
    await s._mark_discarded(seg.current_utt_id)  # 解碼途中該 utt 被判掉
    await asyncio.sleep(0.2)                     # 等 partial 回來
    assert rec.types("partial") == [], "stale partial 洩漏到前端了"
    assert len(rec.types("discard")) == 1
    await s.aclose()


async def test_partial_not_decoded_for_already_terminal_utt():
    dec = FakeDecode()
    s, rec, _, seg = build(dec)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s._mark_discarded(seg.current_utt_id)   # 先判掉
    await s.feed(frames(60))                     # 之後才觸發 partial
    await settle(10)
    assert [c for c in dec.calls if c["kind"] == "partial"] == []
    await s.aclose()


# ── 規則 7:decoder 掛掉不 crash ─────────────────────────────────────────


async def test_final_decode_failure_reports_error_and_discards():
    dec = FakeDecode(fail_final=True)
    s, rec, _, _ = build(dec, partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await settle(20)
    assert len(rec.types("error")) == 1
    assert len(rec.types("discard")) == 1        # 前端要能清掉該 id
    assert rec.types("final") == []
    await s.aclose()


async def test_session_survives_final_failure_and_keeps_working():
    """一句失敗不該讓整條 session 死掉。"""
    dec = FakeDecode(fail_final=True)
    s, rec, _, seg = build(dec, partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await settle(20)
    dec.fail_final = False                       # 下一句 decoder 恢復
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await s.drain(1.0)
    await settle(20)
    assert len(rec.types("final")) == 1
    await s.aclose()


async def test_partial_failure_is_silent():
    """partial 掉了不該吵使用者 —— 下一個 partial 馬上就來。"""
    class PartialFails(FakeDecode):
        async def __call__(self, samples, *, kind, prompt, beam, language="zh"):
            if kind == "partial":
                return {"text": ""}              # decode_client 對 partial 失敗的回傳
            return await super().__call__(samples, kind=kind, prompt=prompt,
                                          beam=beam, language=language)

    s, rec, _, _ = build(PartialFails())
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(60))
    await settle(10)
    assert rec.types("error") == []
    assert rec.types("partial") == []
    await s.aclose()


# ── 幻覺過濾 ─────────────────────────────────────────────────────────────


async def test_hallucinated_final_becomes_discard_not_text():
    s, rec, _, _ = build(FakeDecode(text="請訂閱我的頻道"), partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await settle(20)
    assert rec.types("final") == []
    assert len(rec.types("discard")) == 1
    await s.aclose()


async def test_empty_final_becomes_discard():
    s, rec, _, _ = build(FakeDecode(text="   "), partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    await settle(20)
    assert rec.types("final") == []
    assert len(rec.types("discard")) == 1
    await s.aclose()


async def test_hallucinated_partial_is_dropped_silently():
    s, rec, _, _ = build(FakeDecode(text="音樂"))
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(60))
    await settle(10)
    assert rec.types("partial") == []
    assert rec.types("discard") == []      # partial 幻覺不送 discard
    await s.aclose()


# ── 規則 6:drain ────────────────────────────────────────────────────────


async def test_drain_waits_for_pending_final():
    dec = FakeDecode(delay=0.1)
    s, rec, _, _ = build(dec, partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    assert s.busy
    await s.drain(2.0)
    await settle()
    assert len(rec.types("final")) == 1
    await s.aclose()


async def test_drain_gives_up_after_timeout():
    """等太久等於沒有逾時 —— drain 必須有上限。"""
    dec = FakeDecode(delay=5.0)
    s, _, _, _ = build(dec, partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(40))
    await s.flush()
    start = asyncio.get_event_loop().time()
    await s.drain(0.2)
    assert asyncio.get_event_loop().time() - start < 1.0
    await s.aclose()


# ── partials 關閉(負載旋鈕)──────────────────────────────────────────────


async def test_partials_disabled_never_decodes_partial():
    dec = FakeDecode()
    s, rec, _, _ = build(dec, partials_enabled=False)
    await s.start()
    await s.feed(frames(SPEECH_START_FRAMES))
    await s.feed(frames(120))
    await settle(10)
    assert [c for c in dec.calls if c["kind"] == "partial"] == []
    assert rec.types("partial") == []
    await s.aclose()


# ── 繁體後處理旗標 ───────────────────────────────────────────────────────


def test_text_filter_off_is_identity():
    f = make_text_filter("off")
    assert f("機庫內有50架無人機") == "機庫內有50架無人機"


def test_s2twp_is_refused():
    """實測對正確繁體誤傷 10.58%(「類型」→「型別」)→ 明令禁止。"""
    with pytest.raises(ValueError, match="s2twp"):
        make_text_filter("s2twp")
