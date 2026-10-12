#!/usr/bin/env python3
"""把一則已修好回饋的原問句印成一行回歸題。

不修改 golden_set.jsonl。題庫要不要收這一行，由操作者決定。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def load_feedback_eval():
    path = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "csp"
        / "app"
        / "services"
        / "feedback_eval.py"
    )
    spec = importlib.util.spec_from_file_location("feedback_eval_standalone", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"找不到轉換模組：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="已修好的原問句 → 一行回歸題")
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.prompt is not None and args.prompt_file is not None:
        print("prompt 與 prompt-file 只能擇一", file=sys.stderr)
        return 2
    if args.prompt_file is not None:
        text = args.prompt_file.read_text(encoding="utf-8")
    else:
        text = args.prompt or ""
    module = load_feedback_eval()
    try:
        case = module.build_regression_case(message_id=args.message_id, prompt=text)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(module.case_to_jsonl(case))
    return 0


if __name__ == "__main__":
    sys.exit(main())
