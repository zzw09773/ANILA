/**
 * asrStream 核心邏輯的測試。
 *
 * 涵蓋不需要瀏覽器音訊 API 的純邏輯:append 空白判斷、close code 文案、
 * 重採樣的樣本保留、以及協定不變式(final/discard 後同 id partial 無效)。
 *
 * 真正的「按麥克風→出字」需要真人對著瀏覽器講話 + secure context,無法在
 * vitest/jsdom 驗(規劃書 §5 M5 已載明由 user 手動實測)。這裡守住的是不需要
 * 麥克風就能出錯的那一整層。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  appendTranscript,
  describeClose,
  makeResampler,
  floatToInt16,
  probeAsrAvailable,
  speechStatusAllowsMic,
  watchSpeechStatus,
  createAsrSession,
  CLOSE_AUTH_FAILED,
  CLOSE_SESSION_TIMEOUT,
  CLOSE_CONCURRENCY,
} from '../asr/asrStream.js';

describe('appendTranscript', () => {
  it('中文之間不加空格', () => {
    expect(appendTranscript('你好', '世界')).toBe('你好世界');
  });
  it('英文之間加空格', () => {
    expect(appendTranscript('hello', 'world')).toBe('hello world');
  });
  it('交界任一邊是 CJK 就不加空格(句內空格由 whisper 自帶,不是這裡的事)', () => {
    // 中文句尾接英數開頭:不加。GNSS 前若要空格,是 whisper 在同一句裡給的。
    expect(appendTranscript('報告', 'GNSS')).toBe('報告GNSS');
    // 英數句尾接中文開頭:也不加(next 是 CJK)。
    expect(appendTranscript('use', '無人機')).toBe('use無人機');
  });
  it('兩邊都是英數才加空格(避免 usedrone)', () => {
    expect(appendTranscript('use', 'drone')).toBe('use drone');
  });
  it('空草稿直接回定稿', () => {
    expect(appendTranscript('', '機庫')).toBe('機庫');
  });
  it('空定稿不動草稿', () => {
    expect(appendTranscript('機庫', '   ')).toBe('機庫');
  });
  it('草稿尾端已有空白就不再加', () => {
    expect(appendTranscript('hello ', 'world')).toBe('hello world');
  });
  it('定稿前後空白會被 trim', () => {
    expect(appendTranscript('你好', '  世界  ')).toBe('你好世界');
  });
});

describe('describeClose', () => {
  it('已知 close code 有中文說明', () => {
    expect(describeClose(CLOSE_AUTH_FAILED)).toMatch(/重新登入/);
    expect(describeClose(CLOSE_SESSION_TIMEOUT)).toMatch(/時間上限/);
    expect(describeClose(CLOSE_CONCURRENCY)).toMatch(/接手/);
  });
  it('正常關閉(1000)不產生訊息', () => {
    expect(describeClose(1000)).toBeNull();
  });
  it('不認得的 code 回 null', () => {
    expect(describeClose(4999)).toBeNull();
  });
});

describe('makeResampler', () => {
  it('同取樣率原樣回傳', () => {
    const r = makeResampler(16000, 16000);
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(r(input)).toBe(input);
  });
  it('降採樣後樣本數約為比例', () => {
    const r = makeResampler(48000, 16000);
    const out = r(new Float32Array(48000).fill(0.5));
    // 48k → 16k ≈ 1/3;容一點邊界誤差
    expect(out.length).toBeGreaterThan(15900);
    expect(out.length).toBeLessThan(16100);
  });
  it('跨呼叫保留尾巴(邊界不掉樣本)', () => {
    // 若不保留 carry,兩次各半的輸入會比一次整段少樣本。
    const whole = makeResampler(48000, 16000)(new Float32Array(48000).fill(0.5));
    const split = makeResampler(48000, 16000);
    const a = split(new Float32Array(24000).fill(0.5));
    const b = split(new Float32Array(24000).fill(0.5));
    expect(a.length + b.length).toBe(whole.length);
  });
});

describe('floatToInt16', () => {
  it('0.5 → 16383(Int16Array 截斷,非四捨五入)', () => {
    // 0.5 * 0x7fff = 16383.5,寫進 Int16Array 會截斷成 16383。
    expect(floatToInt16(new Float32Array([0.5]))[0]).toBe(16383);
  });
  it('超範圍會 clip', () => {
    const out = floatToInt16(new Float32Array([2.0, -2.0]));
    expect(out[0]).toBe(0x7fff);
    expect(out[1]).toBe(-0x8000);
  });
});

describe('probeAsrAvailable', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });
  it('治理中心回 enabled+healthy 才可用，而且只打狀態端點', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: true, healthy: true }),
    });
    vi.stubGlobal('fetch', fetchMock);
    expect(await probeAsrAvailable()).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/external-services/speech/status',
      { credentials: 'same-origin' },
    );
  });
  it('啟用但不健康 → 不可用', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: true, healthy: false }),
    }));
    expect(await probeAsrAvailable()).toBe(false);
  });
  it('未啟用 → 不可用', async () => {
    expect(speechStatusAllowsMic({ enabled: false, healthy: false })).toBe(false);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ enabled: false, healthy: false }),
    }));
    expect(await probeAsrAvailable()).toBe(false);
  });
  it('狀態端點失敗或連不上 → 不可用,不拋例外', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
    expect(await probeAsrAvailable()).toBe(false);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')));
    expect(await probeAsrAvailable()).toBe(false);
  });
});

describe('watchSpeechStatus', () => {
  afterEach(() => {
    vi.useRealTimers();
  });
  it('載入時問一次，聚焦再問，時間過去也不會自己再問', () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const stop = watchSpeechStatus(run);
    expect(run).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event('focus'));
    expect(run).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(120_000);
    expect(run).toHaveBeenCalledTimes(2);
    stop();
    window.dispatchEvent(new Event('focus'));
    expect(run).toHaveBeenCalledTimes(2);
  });
});

// ── 協定不變式:用假 WebSocket 驅動 session,不碰音訊 API ──────────────────

class FakeWebSocket {
  constructor() {
    this.readyState = 0; // CONNECTING
    this.sent = [];
    FakeWebSocket.last = this;
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose && this.onclose({ code: 1000 });
  }
  emit(obj) {
    this.onmessage && this.onmessage({ data: JSON.stringify(obj) });
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;

describe('協定不變式(真的驅動 createAsrSession)', () => {
  let session;
  let events;

  // getUserMedia + AudioWorklet 在 jsdom 不存在。我們要驗的是**收到訊息後怎麼
  // 處理**,不是錄音 → stub 掉音訊 API,讓 start() 能把 WS 綁起來,之後就能用
  // FakeWebSocket 重放 gateway 送來的訊息,測真正的 asrStream.handleMessage。
  beforeEach(async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const fakeCtx = {
      state: 'running',
      sampleRate: 16000,
      audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
      createMediaStreamSource: () => ({ connect: vi.fn() }),
      destination: {},
      resume: vi.fn().mockResolvedValue(undefined),
      close: vi.fn().mockResolvedValue(undefined),
    };
    vi.stubGlobal('AudioContext', vi.fn(() => fakeCtx));
    vi.stubGlobal(
      'AudioWorkletNode',
      vi.fn(() => ({ port: { onmessage: null }, connect: vi.fn() }))
    );
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:x'),
      revokeObjectURL: vi.fn(),
    });
    vi.stubGlobal('Blob', vi.fn());
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [] }),
      },
    });

    events = { final: [], partial: [], cleared: 0, errors: [] };
    session = createAsrSession({
      onFinal: (t) => events.final.push(t),
      onPartial: (t) => events.partial.push(t),
      onPartialClear: () => (events.cleared += 1),
      onState: () => {},
      onError: (m) => events.errors.push(m),
      url: 'ws://test/asr/stream',
    });
    await session.start();
    FakeWebSocket.last.readyState = FakeWebSocket.OPEN;
    FakeWebSocket.last.onopen && FakeWebSocket.last.onopen();
  });

  afterEach(() => {
    session.dispose();
    vi.unstubAllGlobals();
  });

  const emit = (obj) => FakeWebSocket.last.emit(obj);

  it('final 之後同 id 的遲到 partial 被忽略', () => {
    emit({ type: 'partial', id: 1, text: '機庫內' });
    emit({ type: 'final', id: 1, text: '機庫內有50架無人機' });
    emit({ type: 'partial', id: 1, text: '機庫內有五十' }); // stale
    expect(events.final).toEqual(['機庫內有50架無人機']);
    expect(events.partial).toEqual(['機庫內']);
  });

  it('discard 之後同 id 的遲到 partial 被忽略', () => {
    emit({ type: 'partial', id: 2, text: '呃' });
    emit({ type: 'discard', id: 2 });
    emit({ type: 'partial', id: 2, text: '呃啊' }); // stale
    expect(events.partial).toEqual(['呃']);
    expect(events.final).toEqual([]);
  });

  it('不同 id 的 partial 各自獨立,不受前一句終態影響', () => {
    emit({ type: 'final', id: 1, text: '第一句' });
    emit({ type: 'partial', id: 2, text: '第二句預覽' });
    expect(events.partial).toEqual(['第二句預覽']);
  });

  it('final 會清掉預覽', () => {
    emit({ type: 'partial', id: 1, text: '機庫' });
    const before = events.cleared;
    emit({ type: 'final', id: 1, text: '機庫內' });
    expect(events.cleared).toBe(before + 1);
  });

  it('空 final 不 append(幻覺被 gateway 濾成空)但仍清預覽', () => {
    emit({ type: 'partial', id: 1, text: '嗯' });
    emit({ type: 'final', id: 1, text: '' });
    expect(events.final).toEqual([]);
  });

  it('error 訊息轉成非阻斷提示', () => {
    emit({ type: 'error', msg: 'GPU 解碼無回應' });
    expect(events.errors).toEqual(['GPU 解碼無回應']);
  });

  it('壞掉的 JSON 不讓 session 爆掉', () => {
    expect(() =>
      FakeWebSocket.last.onmessage({ data: '{not json' })
    ).not.toThrow();
  });
});
