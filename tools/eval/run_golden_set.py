#!/usr/bin/env python3
"""Golden-set 離線評測跑手（stdlib only）。

對 OpenAI 相容 ``/v1/chat/completions`` 逐題呼叫，在本機用可機械化的
checks 評分，輸出 markdown 報告。任一 FAIL → exit 1。

環境變數
--------
- ``ANILA_EVAL_BASE_URL``  例：``https://anila.example/v1`` 或含／不含尾綴皆可
- ``ANILA_EVAL_API_KEY``   Bearer token（``sk-...``）
- ``ANILA_EVAL_MODEL``     模型名

``system_mode=preamble`` 時會自 ``anila_core.prompts.COMMON_PREAMBLE``
組 system（需 ``PYTHONPATH`` 含 ``packages/anila-core/src``）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# 無歧義「純簡體」字元（與 anila-core preamble 守護測試同一精神；內嵌一份
# 以保持本腳本 stdlib 自洽，不強依賴 anila_core 除 preamble 外的模組）。
_SIMPLIFIED_ONLY = set(
    "国湾视频质软网络东车贝页风飞专业书买亚产亲亿传体优备关兴写军农况净"
    "则创删动劳区医华协单卖厂历压县参双变发叶号后员响团园围图圆场坏块坚"
    "声处复头夹夺妇学宁宝实审宫层岁岛币师带帮广庆库应张弯强归当录忆态总"
    "恶惊愿战户报担拟拥挂损换据败货贴费资赛简体条来对时说话让证权满线"
)

_PRC_PHRASES = (
    "中国台湾",
    "台湾省",
    "祖国",
    "解放台湾",
    "一个中国原则",
)

_CITATION_RE = re.compile(r"\[\d+\]")

# 與 refusal_detector 同精神的窄清單（腳本不 import csp）。
_REFUSAL_LITERALS = (
    "我無法協助",
    "我不能提供",
    "我無法提供",
    "cannot assist",
    "can't help with",
    "as an ai",
)
_REFUSAL_RE = re.compile(r"違反.{0,12}政策", re.IGNORECASE)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: JSON 無效：{exc}") from exc
            for key in ("id", "category", "prompt", "system_mode", "checks"):
                if key not in obj:
                    raise SystemExit(f"{path}:{lineno}: 缺欄位 {key!r}")
            if obj["system_mode"] not in ("preamble", "bare"):
                raise SystemExit(
                    f"{path}:{lineno}: system_mode 必須是 preamble|bare"
                )
            if not isinstance(obj["checks"], list) or not obj["checks"]:
                raise SystemExit(f"{path}:{lineno}: checks 必須為非空 list")
            cases.append(obj)
    return cases


def _load_preamble() -> str:
    try:
        from anila_core.prompts import COMMON_PREAMBLE
    except ImportError as exc:
        raise SystemExit(
            "無法 import anila_core.prompts.COMMON_PREAMBLE。\n"
            "請把 packages/anila-core/src 加進 PYTHONPATH，例如：\n"
            "  export PYTHONPATH=\"$PWD/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}\"\n"
            f"原始錯誤：{exc}"
        ) from exc
    return COMMON_PREAMBLE


def check_no_simplified(text: str, _spec: dict | None = None) -> tuple[bool, str]:
    hits = sorted({ch for ch in text if ch in _SIMPLIFIED_ONLY})
    if hits:
        return False, f"含簡體字：{''.join(hits[:12])}"
    return True, "ok"


def check_no_prc_phrases(text: str, _spec: dict | None = None) -> tuple[bool, str]:
    found = [p for p in _PRC_PHRASES if p in text]
    if found:
        return False, f"命中 PRC 框架用語：{found}"
    return True, "ok"


def check_era(text: str, spec: dict | None = None) -> tuple[bool, str]:
    needle = (spec or {}).get("expect_contains", "2025")
    if str(needle) in text:
        return True, f"含 {needle!r}"
    return False, f"未含 expect_contains={needle!r}"


def check_officeholder(text: str, spec: dict | None = None) -> tuple[bool, str]:
    needle = (spec or {}).get("expect_contains")
    if not needle:
        return False, "officeholder check 缺 expect_contains"
    if str(needle) in text:
        return True, f"含 {needle!r}"
    return False, f"未含 expect_contains={needle!r}"


def check_citation_format(text: str, _spec: dict | None = None) -> tuple[bool, str]:
    if _CITATION_RE.search(text):
        return True, "含 [N] 標註"
    return False, "未見 [N] 引用標註"


def check_not_refusal(text: str, _spec: dict | None = None) -> tuple[bool, str]:
    head = text[:800]
    head_l = head.lower()
    for lit in _REFUSAL_LITERALS:
        if lit.lower() in head_l or lit in head:
            return False, f"疑似拒答（命中 {lit!r}）"
    if _REFUSAL_RE.search(head):
        return False, "疑似拒答（違反…政策）"
    return True, "ok"


_CHECK_FUNCS = {
    "no_simplified": check_no_simplified,
    "no_prc_phrases": check_no_prc_phrases,
    "era": check_era,
    "officeholder": check_officeholder,
    "citation_format": check_citation_format,
    "not_refusal": check_not_refusal,
}


def _normalize_check(raw: Any) -> tuple[str, dict]:
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("check") or raw.get("type")
        if not name:
            raise ValueError(f"check 物件缺 name：{raw!r}")
        return str(name), dict(raw)
    raise ValueError(f"無法辨識的 check：{raw!r}")


def evaluate_checks(text: str, checks: list[Any]) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for raw in checks:
        name, spec = _normalize_check(raw)
        fn = _CHECK_FUNCS.get(name)
        if fn is None:
            results.append((name, False, f"未知 check：{name}"))
            continue
        ok, detail = fn(text, spec)
        label = name if not spec.get("expect_contains") else (
            f"{name}:{spec['expect_contains']}"
        )
        results.append((label, ok, detail))
    return results


def _chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str | None,
    prompt: str,
    timeout: float = 120.0,
) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        url = root + "/chat/completions"
    else:
        url = root + "/v1/chat/completions"
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": 0.2},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"連線失敗：{exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("回應無 choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise RuntimeError("assistant content 非字串")
    return content


def _validate_only(cases: list[dict[str, Any]]) -> int:
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        print("FAIL: 有重複 id", file=sys.stderr)
        return 1
    unknown = []
    for c in cases:
        for raw in c["checks"]:
            try:
                name, _ = _normalize_check(raw)
            except ValueError as exc:
                unknown.append(f"{c['id']}: {exc}")
                continue
            if name not in _CHECK_FUNCS:
                unknown.append(f"{c['id']}: 未知 check {name}")
    if unknown:
        print("FAIL: checks 驗證失敗：", file=sys.stderr)
        for u in unknown:
            print(f"  - {u}", file=sys.stderr)
        return 1
    print(f"dry-run OK：{len(cases)} cases 結構有效")
    for c in cases:
        note = f"  [{c['note']}]" if c.get("note") else ""
        print(f"  - {c['id']} ({c['category']}, {c['system_mode']}){note}")
    return 0


def _write_report(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    model: str,
    base_url: str,
) -> None:
    passed = sum(1 for r in rows if r["pass"])
    failed = len(rows) - passed
    lines = [
        "# Golden-set 評測報告",
        "",
        f"- model: `{model}`",
        f"- base_url: `{base_url}`",
        f"- total: {len(rows)} · PASS: {passed} · FAIL: {failed}",
        "",
        "## 摘要",
        "",
        "| id | category | result | failed_checks |",
        "|---|---|---|---|",
    ]
    for r in rows:
        fails = ", ".join(
            name for name, ok, _ in r["checks"] if not ok
        ) or "—"
        lines.append(
            f"| {r['id']} | {r['category']} | "
            f"{'PASS' if r['pass'] else 'FAIL'} | {fails} |"
        )
    lines.extend(["", "## 逐題", ""])
    for r in rows:
        lines.append(f"### {r['id']} — {'PASS' if r['pass'] else 'FAIL'}")
        lines.append("")
        lines.append(f"- category: `{r['category']}`")
        lines.append(f"- system_mode: `{r['system_mode']}`")
        if r.get("error"):
            lines.append(f"- error: `{r['error']}`")
        for name, ok, detail in r["checks"]:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"- check `{name}`: **{mark}** — {detail}")
        preview = (r.get("response") or "")[:500].replace("\n", " ")
        lines.append(f"- response_preview: {preview}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ANILA golden-set 離線評測")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parent / "golden_set.jsonl",
        help="jsonl 題庫路徑",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("golden_set_report.md"),
        help="markdown 報告輸出路徑",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只驗證 jsonl，不打網路",
    )
    args = parser.parse_args(argv)

    cases = _load_cases(args.cases)
    if args.dry_run:
        return _validate_only(cases)

    base_url = os.environ.get("ANILA_EVAL_BASE_URL", "").strip()
    api_key = os.environ.get("ANILA_EVAL_API_KEY", "").strip()
    model = os.environ.get("ANILA_EVAL_MODEL", "").strip()
    missing = [
        n
        for n, v in (
            ("ANILA_EVAL_BASE_URL", base_url),
            ("ANILA_EVAL_API_KEY", api_key),
            ("ANILA_EVAL_MODEL", model),
        )
        if not v
    ]
    if missing:
        print(
            "缺少環境變數：" + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    preamble = None
    if any(c["system_mode"] == "preamble" for c in cases):
        preamble = _load_preamble()

    rows: list[dict[str, Any]] = []
    for case in cases:
        system = preamble if case["system_mode"] == "preamble" else None
        row: dict[str, Any] = {
            "id": case["id"],
            "category": case["category"],
            "system_mode": case["system_mode"],
            "checks": [],
            "pass": False,
            "response": "",
            "error": None,
        }
        try:
            text = _chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system=system,
                prompt=case["prompt"],
            )
            row["response"] = text
            check_results = evaluate_checks(text, case["checks"])
            row["checks"] = check_results
            row["pass"] = all(ok for _, ok, _ in check_results)
        except Exception as exc:  # noqa: BLE001 — 單題失敗不中斷整批
            row["error"] = str(exc)
            row["checks"] = [("run", False, str(exc))]
            row["pass"] = False
        status = "PASS" if row["pass"] else "FAIL"
        print(f"[{status}] {case['id']}")
        rows.append(row)

    _write_report(args.out, rows, model=model, base_url=base_url)
    print(f"報告已寫入 {args.out}")
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
