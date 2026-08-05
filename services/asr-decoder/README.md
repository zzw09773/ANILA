# asr-decoder

無狀態的 faster-whisper 解碼服務。收 PCM、回文字,**沒有 session 概念** ——
切句、partial/final 的節奏、繁體轉換全在 `asr-gateway`,這裡只做解碼。

整體架構、WS 協定、前端規劃見 `docs/planning/asr-voice-input-plan.md`。

## 契約

```
POST /transcribe?kind=partial|final&beam=<1-10>&prompt=<s>&language=zh
  header: X-Token: <ASR_DECODER_TOKEN>
  body:   application/octet-stream — Int16 mono PCM @16 kHz
  200:    {"text", "no_speech_prob", "avg_logprob", "decode_seconds"}
  400:    body 空 / 長度非 Int16 對齊 / kind 不合法
  401:    X-Token 不符
  413:    音訊超過 ASR_MAX_AUDIO_SECONDS(預設 60s)
  503:    模型還在載入

GET /health           (免 token —— compose healthcheck 要用)
  200 {"status":"ok",      model, device, compute_type, ready:true}
  503 {"status":"loading", ...,                          ready:false}
```

`kind` 決定解碼參數,**兩組都是內網實測過的,不要憑感覺改**:

| | beam | 品質門檻 | 理由 |
|---|---|---|---|
| `partial` | 1 (greedy) | 全關 | 每 ~0.5s 重出一次、壽命極短。半句話長得像雜訊,開門檻會讓 whisper 判定「非語音」而把正在講的句子整個吃掉。過濾交給 gateway。 |
| `final` | 參數帶入(預設 5) | `no_speech<0.5`、壓縮比 `<2.2`、`logprob>-1.0` | 會留在畫面上 → 花得起 beam search,也該擋幻覺。 |

## 兩種部署模式

同一份程式碼、同一個 image,**只差設定**。

### 內部版 — decoder 與平台同機

decoder 定義在 `infra/models/docker-compose.yml`(隨 `anila-models` stack 起),
掛 `anila-models-net`,**不開 host port**。gateway 設:

```
ASR_DECODE_URL=http://asr-decoder:9000
```

走內部 network,本機 dev 也用這版。⚠ 這個位址一樣要過 gateway 的 SSRF guard:
`asr-decoder` 是**單標籤** docker 服務名 → 靠 `ANILA_TRUSTED_HOSTS` 點名;
`http://` 這個 scheme → 靠 `ANILA_ALLOW_HTTP_ENDPOINT=1` 放行。兩者缺一
asr-gateway **啟動時就會停**(見 `docs/runbooks/asr-voice-input.md` §3c)。

### 外部版 — decoder 在獨立 GPU 主機(MLSteam / aiops 模式)

用本目錄的 `docker-compose.standalone.yml`:

```bash
cp .env.example .env      # 填 ASR_DECODER_TOKEN(openssl rand -hex 32)、卡號、模型尺寸
docker compose -f docker-compose.standalone.yml -p asr-decoder up -d
curl -sf http://localhost:9000/health
```

gateway 端設:

```
ASR_DECODE_URL=http://<gpu-host>:9000
ANILA_ALLOW_HTTP_ENDPOINT=1  # 純 http 必須顯式放行(跟其他 model endpoint 同一道門)
```

⚠ 舊名 `ASR_ALLOW_HTTP_DECODER` 已於 2026-08-05 **退役**:它從被「馴服」之後就
沒有任何程式在讀(`docs/FAKE-CONTROLS.md` #31)。照舊名去設 = 設了、沒報錯、
gateway 照樣在啟動時拒收 http 位址。

⚠ **這個模式會把 decoder 綁上 host port,`X-Token` 是唯一的防線。**
密鑰要夠長,並用防火牆把來源限縮到平台主機 IP —— 不要讓整個內網都打得到。
純 http 是 MLSteam NodePort 的現實(同 `anila-agent`;填 https 會得到
`WRONG_VERSION_NUMBER`)。

## 模型權重(air-gap)

**權重刻意不烘進 image** —— 換尺寸不必重 build,image 也才簽章得動、體積可控
(large-v3 光權重就 ~3GB)。由 volume 掛到 `ASR_MODEL_DIR`。

HuggingFace 上的 `Systran/faster-whisper-*` **已經是 CTranslate2 格式**,不需
自己轉檔。在有外網的機器上抓好、帶進內網:

```bash
pip install huggingface_hub
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download("Systran/faster-whisper-large-v3", local_dir="./models")
PY
```

`./models` 內若直接含 `model.bin`,服務會把該目錄當模型路徑;否則當成
HuggingFace 快取根目錄,仍以 `ASR_MODEL_SIZE` 的名稱解析。

`ASR_LOCAL_FILES_ONLY=1` 在 air-gap **一定要開**:否則權重缺了會卡在 DNS
timeout,`/health` 一直 503,查半天以為是 GPU 問題。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `ASR_DECODER_TOKEN` | (必填) | 與 gateway 共享的密鑰。**沒填直接拒絕啟動** —— 服務會對任何連得到 port 的人解碼。 |
| `ASR_MODEL_SIZE` | `medium` | `medium` / `large-v3` / 模型目錄絕對路徑。 |
| `ASR_MODEL_DIR` | `""` | 權重目錄。含 `model.bin` → 當模型路徑;否則當下載快取根。 |
| `ASR_LOCAL_FILES_ONLY` | `false` | air-gap 設 `1`。 |
| `ASR_DEVICE` | `cuda` | `cuda` / `cpu`。 |
| `ASR_COMPUTE_TYPE` | `float16` | 見下方硬體註記。 |
| `ASR_CPU_THREADS` | `0` | 只在 `cpu` 有意義;0 = 交給 CTranslate2 決定。 |
| `ASR_MAX_AUDIO_SECONDS` | `60` | 單次 decode 上限。gateway 切句上限是 30s,留一倍餘裕擋畸形請求。 |

## 硬體註記

**V100(Volta)請用 `float16`,不要 int8。** Volta 沒有 int8 tensor core,int8 走
一般 cuBLAS 路徑,省了 VRAM 但**速度反而更慢** —— faster-whisper 官方 V100S
benchmark(large-v2、13 分鐘音檔、beam 5)是 fp16 54s / int8 59s。int8 的價值
要 Turing(T4)以後才有。H100 怎麼跑都行,4.7GB 對 80GB 是零頭。

VRAM 概估:medium fp16 ~2.5GB、large-v3 fp16 ~4.7GB(官方實測)、
large-v3 int8 ~3.1GB(官方實測)。medium 那格是按參數量外推,**要規劃容量請
在目標硬體上實測**。

CPU 只適合 dev 與契約測試:large-v3 在 CPU 上 RTF≈1,互動場景不可行。

## 測試

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`faster_whisper` 是在 `WhisperDecoder.load()` 內才 import 的,測試以假 decoder
注入(`create_app(decoder=...)`),所以**測試不需要 GPU、不需要權重、不需要
網路**。CI 與 air-gap build 機器都跑得動。

## 安全

- **音訊零落地**:PCM 只在記憶體流轉,不寫檔、不進 log。
- **log 不記辨識文字**(等同對話內容,機敏)—— 只記 `kind` / 樣本數 / 解碼耗時。
- `X-Token` 以 `secrets.compare_digest` 比對,避免時間差洩漏密鑰前綴。
- 容器以 non-root(uid 10001)執行,權重目錄唯讀掛載。

## 疑難排解

| 症狀 | 原因 |
|---|---|
| `libcudnn_ops.so.9: cannot open shared object file` | base image 是舊的 `-cudnn8-` tag。CTranslate2 4.x 要 cuDNN 9 → tag 必須是 `-cudnn-`。 |
| `/health` 一直 503 | 模型還在載(large-v3 冷啟動分鐘級,看 log)、或權重路徑錯、或 air-gap 沒開 `ASR_LOCAL_FILES_ONLY` 卡在下載。 |
| 容器起不來,log 說 `ASR_DECODER_TOKEN must be set` | 這是刻意的 fail-loud。填密鑰。 |
| 解碼很慢且是 V100 | `ASR_COMPUTE_TYPE` 設成 int8 了。改 `float16`。 |
| gateway 連不上,錯誤是 `WRONG_VERSION_NUMBER` | `ASR_DECODE_URL` 填了 https,但外部版是純 http。 |
