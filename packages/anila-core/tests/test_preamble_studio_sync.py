"""Studio generated_preamble.py 與共同前導 SSOT 的 sync guard。

``services/anila-studio`` 未依賴 anila-core，國家用語／紀年段經
``packages/anila-core/scripts/gen_preamble_studio.py`` 產成
``services/anila-studio/app/generated_preamble.py``。SSOT 改了沒重跑
產生器 → 這裡紅。

比對的是 import 後的常數值（雙向相等），不是檔案內 substring——
避免 SSOT 縮短後舊產物仍含新字串而誤過。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from anila_core.prompts import ERA_RULES, NATIONAL_TERMINOLOGY

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GEN_SCRIPT = _REPO_ROOT / "packages" / "anila-core" / "scripts" / "gen_preamble_studio.py"
_GENERATED = _REPO_ROOT / "services" / "anila-studio" / "app" / "generated_preamble.py"


def _load_module(path: Path, name: str):
    """Load a .py file by path (studio app package is not importable here)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses / relative patterns behave if needed.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_generator():
    return _load_module(_GEN_SCRIPT, "gen_preamble_studio")


def _load_generated():
    assert _GENERATED.exists(), (
        "Studio generated_preamble.py 不存在——跑 gen_preamble_studio.py"
    )
    return _load_module(_GENERATED, "studio_generated_preamble")


def test_studio_generated_preamble_in_sync():
    generated = _load_generated()
    assert generated.NATIONAL_TERMINOLOGY == NATIONAL_TERMINOLOGY, (
        "Studio NATIONAL_TERMINOLOGY 與 SSOT 不同步——重跑 gen_preamble_studio.py"
    )
    assert generated.ERA_RULES == ERA_RULES, (
        "Studio ERA_RULES 與 SSOT 不同步——重跑 gen_preamble_studio.py"
    )
    # Explicit both-direction equality (same as ==, documents the contract).
    assert NATIONAL_TERMINOLOGY == generated.NATIONAL_TERMINOLOGY
    assert ERA_RULES == generated.ERA_RULES


def test_emit_str_round_trips_arbitrary_content(tmp_path):
    """Generator escaping must preserve backslashes, triple quotes, and ${}."""
    gen = _load_generator()
    samples = [
        r"C:\new\test",
        'literal """ quotes',
        "has ${interpolation} marker",
        'combo C:\\new\\test with """ and ${x}',
        "tabs\tand\nnewlines\\u0041",
    ]
    for i, original in enumerate(samples):
        emitted = gen.emit_str(original)
        mod_path = tmp_path / f"roundtrip_{i}.py"
        mod_path.write_text(f"VALUE = {emitted}\n", encoding="utf-8")
        loaded = _load_module(mod_path, f"preamble_roundtrip_{i}")
        assert loaded.VALUE == original, (
            f"emit_str round-trip failed for {original!r}: got {loaded.VALUE!r}"
        )
