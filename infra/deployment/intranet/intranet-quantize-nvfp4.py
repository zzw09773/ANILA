#!/usr/bin/env python3
"""內網離線 NVFP4 量化 (B200 換裝後用;H100 跑不了 NVFP4 不要在上面試)。

前置 (一次性,在帶進去的 vLLM 容器或同版 python 環境):
    pip install --no-index --find-links=/path/to/toolkit/wheelhouse llmcompressor datasets

用法:
    python3 intranet-quantize-nvfp4.py \
        --model  $ANILA_HF_DIR/Mistral-Medium-3.5-128B \
        --out    $ANILA_HF_DIR/Mistral-Medium-3.5-128B-NVFP4 \
        --calib  /path/to/toolkit/calib-datasets/Open-Platypus

資源需求 (rule of thumb):
    CPU RAM ≳ 模型 bf16 大小 × 1.2 (llm-compressor 逐層 onload 到 GPU 校準,
    模型本體留在 CPU RAM)。Maverick 748G 需要 ~1TB RAM 的主機才壓得動;
    Mistral 兩隻 (~250G) 一般 1TB RAM 主機沒問題。
    GPU: 1 張即可 (校準 forward 逐層跑),時間 = 大模型數小時級。

⚠ 進場前先在外網用小模型 (gemma-4-E4B,15G) 把「wheelhouse 安裝 → 本腳本
   → vLLM 載入量化結果」整條 rehearsal 一遍 — llm-compressor 的 API 在不同
   版本間有小幅調整,別讓第一次執行發生在沒有網路救援的內網。
"""
import argparse
import os

# 離線三開關 — 一定要在 import transformers/datasets 之前設好,
# 不然它們還是會嘗試連 huggingface.co (連不到就是長 timeout 或直接炸)。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

from datasets import load_dataset                          # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402
from llmcompressor import oneshot                          # noqa: E402
from llmcompressor.modifiers.quantization import QuantizationModifier  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, help="bf16 母本目錄 (本地路徑)")
    p.add_argument("--out", required=True, help="NVFP4 輸出目錄")
    p.add_argument("--calib", required=True,
                   help="本地校準資料集目錄 (toolkit/calib-datasets/Open-Platypus)")
    p.add_argument("--num-samples", type=int, default=64,
                   help="校準樣本數 (官方 NVFP4 範例用 20-512,64 是穩健中間值)")
    p.add_argument("--max-seq-len", type=int, default=2048)
    args = p.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    # torch_dtype auto + device_map 交給 llm-compressor 的 sequential
    # pipeline 處理 onloading;不要自己 .to("cuda") — 大模型會直接 OOM。
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype="auto")

    # Open-Platypus 是 parquet repo,本地路徑直接 load。欄位:
    # instruction / output。轉成單一 text 欄給 calibration 用。
    ds = load_dataset(args.calib, split=f"train[:{args.num_samples}]")
    ds = ds.map(lambda ex: {
        "text": (ex.get("instruction") or "") + "\n" + (ex.get("output") or "")
    })

    # NVFP4 = W4A4 microscaling (Blackwell 硬體格式)。lm_head 照慣例不量化。
    recipe = QuantizationModifier(targets="Linear", scheme="NVFP4",
                                  ignore=["lm_head"])

    oneshot(
        model=model,
        processor=tokenizer,
        dataset=ds,
        recipe=recipe,
        max_seq_length=args.max_seq_len,
        num_calibration_samples=args.num_samples,
        output_dir=args.out,
    )
    print(f"✓ NVFP4 checkpoint 已輸出: {args.out}")
    print("  vLLM (Blackwell) 驗證:")
    print(f"  vllm serve {args.out} --max-model-len 8192  # 起得來 + 對話一輪即可")


if __name__ == "__main__":
    main()
