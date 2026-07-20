/**
 * 串流語音輸入的核心:麥克風 → Int16 PCM @16kHz → WS `/asr/stream`。
 *
 * ⚠⚠ VENDORED —— 本檔在 `apps/anilalm/src/asr/` 與 `apps/anila-shell/src/asr/`
 * 各有一份**完全相同**的副本。改一邊就要同步另一邊。
 *
 * 為什麼是副本而不是共用套件:`packages/` 底下四個套件全是 Python,整個 repo
 * 沒有任何前端共用包(沒有 npm workspace / pnpm / turbo,兩個 app 是各自獨立的
 * vite build,而且 anilalm 有 TypeScript、anila-shell 沒有)。要共用就得先引入
 * 前端 monorepo 工具鏈 —— 那是基礎建設改動,不該夾在語音輸入這個功能裡。
 * 這是刻意的取捨,不是疏忽。(規劃書 §2.5 要求實作者查既有慣例後決定;查了,
 * 沒有前例。)
 *
 * 刻意寫成純 JS、零框架相依:anila-shell 沒有 TypeScript,同一份 .js 兩邊都能
 * 直接 import;anilalm 那邊靠旁邊的 asrStream.d.ts 拿型別。
 *
 * 錄音那段移植自內網已驗證的實作
 * (語料庫/ASR_intranet/code_for_mlsteam/streaming_asr/templates/index.html:629+),
 * 幾個看起來多餘、其實是踩過坑的地方都保留了 —— 見各處註解。
 *
 * 協定見 docs/planning/asr-voice-input-plan.md §2.2。
 */

const TARGET_SR = 16000;
const SEND_SAMPLES = 1440; // ~90ms per packet

// close code(與 services/asr-gateway/app/main.py 對齊)
export const CLOSE_AUTH_FAILED = 4401;
export const CLOSE_SESSION_TIMEOUT = 4408;
export const CLOSE_CONCURRENCY = 4409;
export const CLOSE_AUTH_UNAVAILABLE = 4503;

/** close code → 給使用者看的訊息。不認得的 code 回 null(= 靜默,通常是正常關閉)。 */
export function describeClose(code) {
  switch (code) {
    case CLOSE_AUTH_FAILED:
      return '登入狀態已失效,請重新登入後再使用語音輸入。';
    case CLOSE_SESSION_TIMEOUT:
      return '語音輸入已達單次時間上限,請再按一次麥克風繼續。';
    case CLOSE_CONCURRENCY:
      return '語音輸入已被另一個分頁或裝置接手。';
    case CLOSE_AUTH_UNAVAILABLE:
      return '語音服務暫時不可用(認證服務異常),請稍後再試。';
    default:
      return null;
  }
}

/**
 * 線性內插重採樣,跨次呼叫保留尾巴與小數位置。
 *
 * 為什麼要 carry:每次 callback 拿到的樣本數不保證是 ratio 的整數倍,不保留
 * 尾巴的話每個 buffer 邊界都會掉幾個樣本 —— 累積起來就是講話變快、字被吃掉。
 */
export function makeResampler(fromRate, toRate) {
  if (fromRate === toRate) return (input) => input;
  const ratio = fromRate / toRate;
  let tail = new Float32Array(0);
  let pos = 0;
  return (input) => {
    const data = new Float32Array(tail.length + input.length);
    data.set(tail);
    data.set(input, tail.length);
    const outLen = Math.max(0, Math.floor((data.length - 1 - pos) / ratio) + 1);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const idx = Math.floor(pos);
      const frac = pos - idx;
      out[i] = data[idx] * (1 - frac) + data[idx + 1] * frac;
      pos += ratio;
    }
    const consumed = Math.floor(pos);
    tail = data.slice(consumed);
    pos -= consumed;
    return out;
  };
}

/** Float32 [-1,1] → Int16。負值除以 0x8000、正值除以 0x7FFF 是為了對稱地用滿範圍。 */
export function floatToInt16(float32) {
  const pcm = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return pcm;
}

const WORKLET_CODE = `
class PCMForwarder extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('pcm-forwarder', PCMForwarder);
`;

/**
 * 追蹤協定不變式:final/discard 之後,同 id 的 partial 一律無效。
 *
 * 伺服器已經擋了一層(gateway 的 _terminal),這裡再擋是規劃書 §2.2 明文要求的
 * 雙保險 —— 少了它,一個遲到的 partial 就會讓預覽文字在定稿後復活,而且永遠
 * 清不掉(下一個 final 是別的 id,蓋不到它)。
 */
class UtteranceTracker {
  constructor() {
    this.terminal = new Set();
  }
  markTerminal(id) {
    this.terminal.add(id);
  }
  acceptsPartial(id) {
    return !this.terminal.has(id);
  }
  reset() {
    this.terminal.clear();
  }
}

/**
 * 建立一條語音輸入 session。
 *
 * @param {object} opts
 * @param {(text: string) => void}   opts.onFinal    定稿(要 append 進輸入框)
 * @param {(text: string) => void}   opts.onPartial  預覽(整段取代,不進輸入框)
 * @param {() => void}               opts.onPartialClear  清掉預覽
 * @param {(state: string) => void}  opts.onState    idle|requesting|listening|recording
 * @param {(msg: string) => void}    opts.onError    非阻斷提示
 * @param {string}                   [opts.url]      預設同源 /asr/stream
 */
export function createAsrSession({
  onFinal,
  onPartial,
  onPartialClear,
  onState,
  onError,
  url,
}) {
  let ws = null;
  let audioCtx = null;
  let micStream = null;
  let captureNode = null;
  let recording = false;
  let sendBuf = new Int16Array(0);
  let closedByUs = false;
  const tracker = new UtteranceTracker();

  const setState = (s) => onState && onState(s);
  const fail = (msg) => onError && onError(msg);

  function wsUrl() {
    if (url) return url;
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/asr/stream`;
  }

  function pushSamples(float32) {
    const pcm = floatToInt16(float32);
    const merged = new Int16Array(sendBuf.length + pcm.length);
    merged.set(sendBuf);
    merged.set(pcm, sendBuf.length);
    sendBuf = merged;
    while (sendBuf.length >= SEND_SAMPLES) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(sendBuf.slice(0, SEND_SAMPLES).buffer);
      }
      sendBuf = sendBuf.subarray(SEND_SAMPLES);
    }
    // subarray 是 view,長期累積會抓著整個舊 buffer 不放 → 複製一份切斷參照。
    sendBuf = new Int16Array(sendBuf);
  }

  async function buildAudioGraph(preferredRate) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    const ctx = preferredRate ? new Ctor({ sampleRate: preferredRate }) : new Ctor();
    try {
      if (ctx.state === 'suspended') await ctx.resume();
      const source = ctx.createMediaStreamSource(micStream);
      const resample = makeResampler(ctx.sampleRate, TARGET_SR);

      if (ctx.audioWorklet) {
        const blobUrl = URL.createObjectURL(
          new Blob([WORKLET_CODE], { type: 'application/javascript' })
        );
        try {
          await ctx.audioWorklet.addModule(blobUrl);
        } finally {
          URL.revokeObjectURL(blobUrl);
        }
        captureNode = new AudioWorkletNode(ctx, 'pcm-forwarder');
        captureNode.port.onmessage = (e) => {
          if (recording) pushSamples(resample(e.data));
        };
        source.connect(captureNode);
        // 輸出是靜音的,但不接 destination 的話節點會被當成沒用而停止運作。
        captureNode.connect(ctx.destination);
      } else {
        captureNode = ctx.createScriptProcessor(4096, 1, 1);
        captureNode.onaudioprocess = (e) => {
          if (recording) pushSamples(resample(e.inputBuffer.getChannelData(0)));
        };
        source.connect(captureNode);
        captureNode.connect(ctx.destination);
      }
      return ctx;
    } catch (e) {
      captureNode = null;
      await ctx.close().catch(() => {});
      throw e;
    }
  }

  function handleMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return; // 伺服器不該送壞 JSON;真送了也不值得打斷使用者錄音
    }
    switch (msg.type) {
      case 'partial':
        // 協定不變式:已定稿/已捨棄的 utt,遲到的 partial 一律忽略。
        if (tracker.acceptsPartial(msg.id)) onPartial && onPartial(msg.text);
        break;
      case 'final':
        tracker.markTerminal(msg.id);
        onPartialClear && onPartialClear();
        if (msg.text) onFinal && onFinal(msg.text);
        break;
      case 'discard':
        tracker.markTerminal(msg.id);
        onPartialClear && onPartialClear();
        break;
      case 'status':
        // recording = VAD 偵測到有人在講;listening = 在等聲音。
        if (msg.state === 'recording' || msg.state === 'listening') setState(msg.state);
        break;
      case 'error':
        fail(msg.msg || '語音辨識發生錯誤');
        break;
      default:
        break;
    }
  }

  async function start() {
    if (recording) return;
    closedByUs = false;
    tracker.reset();
    setState('requesting');

    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        video: false,
      });
    } catch (e) {
      setState('idle');
      // ⚠ 這裡的 NotAllowedError 有三種可能,錯誤物件分不出來:
      //   1. 使用者真的按了拒絕
      //   2. 頁面不是 secure context(http 直連,非 localhost)
      //   3. nginx 的 Permissions-Policy 沒放行 microphone=(self)
      // 所以訊息必須把三種都講出來,否則使用者(和維護的人)會鬼打牆。
      fail(
        `無法取得麥克風(${e.name})。請確認:① 已允許本站使用麥克風;` +
          `② 網址是 https(http 直連拿不到麥克風);③ 伺服器已放行麥克風權限。`
      );
      return;
    }

    sendBuf = new Int16Array(0);
    try {
      try {
        audioCtx = await buildAudioGraph(TARGET_SR);
      } catch {
        // Firefox 舊版無法把 48kHz 的麥克風接到 16kHz 的 context;退回預設
        // 取樣率,重採樣交給 makeResampler 做。
        audioCtx = await buildAudioGraph(null);
      }
    } catch (e) {
      cleanupAudio();
      setState('idle');
      fail(`無法啟動音訊處理:${e.message}`);
      return;
    }

    try {
      ws = new WebSocket(wsUrl());
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      cleanupAudio();
      setState('idle');
      fail(`無法連線語音服務:${e.message}`);
      return;
    }

    ws.onmessage = (e) => handleMessage(e.data);
    ws.onerror = () => {
      // onerror 之後一定會有 onclose,訊息在那邊統一處理,這裡不重複打擾。
    };
    ws.onclose = (e) => {
      const wasRecording = recording;
      recording = false;
      cleanupAudio();
      onPartialClear && onPartialClear();
      setState('idle');
      if (closedByUs) return;
      const reason = describeClose(e.code);
      if (reason) fail(reason);
      else if (wasRecording) fail('語音連線中斷,請再試一次。');
    };
    ws.onopen = () => {
      recording = true;
      setState('listening');
    };
  }

  function cleanupAudio() {
    if (captureNode) {
      try {
        captureNode.disconnect();
      } catch {
        /* 已經斷開就算了 */
      }
      captureNode = null;
    }
    if (micStream) {
      micStream.getTracks().forEach((t) => t.stop());
      micStream = null;
    }
    if (audioCtx) {
      audioCtx.close().catch(() => {});
      audioCtx = null;
    }
  }

  function stop() {
    if (!recording && !ws) return;
    recording = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // 尾巴不足一個封包也要送 —— 不送的話最後那 90ms 會被切掉,常常正好是
      // 句尾的字。
      if (sendBuf.length) {
        ws.send(sendBuf.buffer.slice(0, sendBuf.length * 2));
        sendBuf = new Int16Array(0);
      }
      ws.send(JSON.stringify({ type: 'flush' }));
      // 不立刻關 —— flush 後的 final 還在路上。等伺服器把定稿送回來、
      // 或 3 秒後放棄。closedByUs 讓 onclose 不要跳「連線中斷」的錯誤。
      closedByUs = true;
      setTimeout(() => {
        if (ws) {
          ws.close(1000);
          ws = null;
        }
      }, 3000);
    } else if (ws) {
      closedByUs = true;
      ws.close(1000);
      ws = null;
    }
    cleanupAudio();
    setState('idle');
  }

  function dispose() {
    closedByUs = true;
    recording = false;
    if (ws) {
      try {
        ws.close(1000);
      } catch {
        /* 已關就算了 */
      }
      ws = null;
    }
    cleanupAudio();
  }

  return { start, stop, dispose, isRecording: () => recording };
}

/**
 * 把一句定稿接到既有草稿後面,自動決定接縫要不要加空格。
 *
 * ⚠ 這是**句與句**的接縫,不是排版:whisper 一句話的輸出本來就自帶內部空格
 * (「報告 GNSS 干擾」),本函式只處理連續兩次定稿的交界。所以規則以「會不會
 * 黏成看不出斷句」為準,不是英文排版慣例:
 *   - 只要交界任一邊是 CJK,就不加空格 —— 中文不用空格斷詞,加了反而怪
 *     (「你好」+「世界」→「你好世界」;「報告」+「GNSS」→「報告GNSS」,
 *      GNSS 前的空格若需要,會在同一句的 whisper 輸出裡自帶)。
 *   - 兩邊都是非 CJK(純英數句子接純英數句子)才加,免得 "use"+"drone" 黏成
 *     "usedrone"。
 *   - 草稿尾端已是空白就不再加。
 */
export function appendTranscript(draft, addition) {
  const add = (addition || '').trim();
  if (!add) return draft;
  if (!draft) return add;
  const prev = draft[draft.length - 1];
  const next = add[0];
  const isCJK = (ch) => /[　-〿㐀-鿿＀-￯]/.test(ch);
  if (/\s/.test(prev)) return draft + add;
  if (isCJK(prev) || isCJK(next)) return draft + add;
  return `${draft} ${add}`;
}

/**
 * ASR 有沒有部署。
 *
 * 不用 build-time 旗標(VITE_ASR_ENABLED 之類):那會讓「有/無 ASR」變成兩顆
 * 內容不同的前端 image,跟 platform.yml 鎖 image content ID 的走向打架。
 * asr-gateway 是 profile-gated 的 —— 沒開時 nginx 打不到它,直接 502/404,
 * probe 一下就知道,連旗標都不需要。
 */
export async function probeAsrAvailable() {
  try {
    const resp = await fetch('/asr/health', { credentials: 'same-origin' });
    // 200 = 可用;503 = gateway 活著但 revocation cache 沒 ready(fail-closed,
    // 此時 WS 一律被拒)→ 兩者都不該顯示按鈕以外的樣子。只有 200 才顯示。
    return resp.ok;
  } catch {
    return false;
  }
}
