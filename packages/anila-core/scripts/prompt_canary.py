#!/usr/bin/env python3
"""Prompt canary + golden-set runner (harness §6-2 / §6-8).

The Router's three system templates are live-editable in the governance
center; this script is the mechanical check to run after editing them, after
switching models, and on the first day on the intranet.

  1. canary — a system instruction 「回覆結尾加【核】」 must survive the model
     server's chat template (gemma-family gateways have dropped the system
     role before). Sent per enabled LLM straight through csp /v1.
  2. golden seed — the questions in scripts/golden/seed.json go through the
     Router (the real chain: preamble, memory, regulation retrieval). Only
     mechanical properties are checked: non-empty, zero simplified
     characters, zero PRC-framing terms, [N] citation markers when the
     question should hit the regulation KB, year conversion.

Usage:
  python prompt_canary.py --base https://172.16.120.153 --key csk-... [--insecure] [--out report.md]
Exit code 1 when any check fails. Keys never go to stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
SEED = HERE / "golden" / "seed.json"

# 常見簡體字（繁體文本裡不該出現的字形）。不是完整表，抓得到就算命中。
# 只放「繁體文本裡不會出現」的簡體字形；件／明／理／行／元／于 這種兩邊共用的字
# 不能放（2026-09-02 首跑全是這種假命中）。
SIMPLIFIED = set("这个们时间国军应对说话软质数电脑关闭开发进实现问题该当为与义务权马达学习经济际会议单产业设计则网络体验记录处术条")
PRC_TERMS = ("中国台湾", "中國台灣", "台湾省", "台灣省", "祖国", "祖國", "解放台湾", "解放台灣", "两岸同属", "兩岸同屬", "岛内", "島內")
CANARY_MARK = "【核】"


def _post(client: httpx.Client, url: str, key: str, body: dict) -> dict:
    r = client.post(url, json=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def canary(client: httpx.Client, base: str, key: str) -> list[dict]:
    models = _post_get(client, f"{base}/v1/models", key)
    # /v1/models 只回 OpenAI 形狀（沒有 model_type）；嵌入模型不會回 chat，用名稱略過。
    ids = [m["id"] for m in models.get("data", []) if "embed" not in str(m["id"]).lower()]
    out = []
    for mid in ids:
        started = time.time()
        try:
            data = _post(client, f"{base}/v1/chat/completions", key, {
                "model": mid,
                "messages": [
                    {"role": "system", "content": "你是測試助理。不論被問什麼，回覆的最後一定要加上【核】三個字元。"},
                    {"role": "user", "content": "請回一句簡短的話。"},
                ],
                "stream": False, "temperature": 0.2, "max_tokens": 1024,  # 思考變體先燒 reasoning，256 會回空
            })
            content = (data["choices"][0]["message"].get("content") or "").strip()
            ok = CANARY_MARK in content
            out.append({"model": mid, "ok": ok, "ms": int((time.time() - started) * 1000), "tail": content[-40:]})
        except Exception as exc:  # noqa: BLE001
            out.append({"model": mid, "ok": False, "ms": int((time.time() - started) * 1000), "tail": f"error: {type(exc).__name__}: {str(exc)[:80]}"})
    return out


def _post_get(client: httpx.Client, url: str, key: str) -> dict:
    r = client.get(url, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    return r.json()


def check(name: str, content: str, meta: dict) -> tuple[bool, str]:
    if name == "nonempty":
        return bool(content.strip()), f"{len(content)} 字"
    if name == "zh":
        hits = sorted({ch for ch in content if ch in SIMPLIFIED})
        return not hits, "簡體命中: " + ("".join(hits) if hits else "無")
    if name == "terms":
        hits = [t for t in PRC_TERMS if t in content]
        return not hits, "用語命中: " + (",".join(hits) if hits else "無")
    if name == "cites":
        # Router 直答：要有 [N] 或 citations。派工給 agent 的回合由 agent 自己引用，
        # 這裡只記錄「是派工」不當失敗——派工品質是 agent 的 golden set 要量的事。
        route = (meta.get("route") or {}).get("decision")
        if route == "dispatch":
            return True, f"dispatched→{meta.get('answering_agent_id')}（不驗 [N]）"
        has = bool(re.search(r"\[\d+\]", content)) or bool(meta.get("citations"))
        return has, f"kb_state={meta.get('kb_state')} citations={len(meta.get('citations') or [])}"
    if name == "no_kb":
        return not meta.get("citations"), f"kb_state={meta.get('kb_state')}"
    if name.startswith("year:"):
        want = name.split(":", 1)[1]
        return want in content, f"要含 {want}"
    return False, f"未知檢查 {name}"


def golden(client: httpx.Client, base: str, key: str) -> list[dict]:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    rows = []
    for q in seed["questions"]:
        started = time.time()
        try:
            data = _post(client, f"{base}/router/v1/chat/completions", key, {
                "model": "anila-router",
                "messages": [{"role": "user", "content": q["q"]}],
                "stream": False,
            })
            content = (data["choices"][0]["message"].get("content") or "")
            meta = data.get("anila_meta") or {}
            results = {c: check(c, content, meta) for c in q["checks"]}
            rows.append({"id": q["id"], "q": q["q"], "ms": int((time.time() - started) * 1000),
                         "ok": all(v[0] for v in results.values()), "results": results, "head": content[:80].replace("\n", " ")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"id": q["id"], "q": q["q"], "ms": int((time.time() - started) * 1000), "ok": False,
                         "results": {"error": (False, f"{type(exc).__name__}: {str(exc)[:100]}")}, "head": ""})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--skip-canary", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    with httpx.Client(timeout=180, verify=not a.insecure) as client:
        can = [] if a.skip_canary else canary(client, base, a.key)
        gold = golden(client, base, a.key)
    lines = [f"# Prompt canary + golden seed — {time.strftime('%Y-%m-%d %H:%M')} — {base}", ""]
    if can:
        lines += ["## Canary（system 指令有沒有活過 chat template）", "", "| 模型 | 結果 | ms | 回覆尾巴 |", "|---|---|---|---|"]
        lines += [f"| {c['model']} | {'✅' if c['ok'] else '❌'} | {c['ms']} | {c['tail']} |" for c in can]
        lines.append("")
    lines += ["## Golden seed（走 Router 全鏈）", "", "| # | 問題 | 結果 | ms | 檢查 |", "|---|---|---|---|---|"]
    for r in gold:
        detail = "; ".join(f"{k}{'✅' if v[0] else '❌'} {v[1]}" for k, v in r["results"].items())
        lines.append(f"| {r['id']} | {r['q']} | {'✅' if r['ok'] else '❌'} | {r['ms']} | {detail} |")
    failed = [c for c in can if not c["ok"]] + [r for r in gold if not r["ok"]]
    lines += ["", f"**{len(failed)} 項未過 / {len(can) + len(gold)} 項**"]
    report = "\n".join(lines)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
