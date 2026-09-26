"""Studio 是獨立服務，映像裡沒有 anila_core。

2026-09-26 新增的 CSRF 中介層引用了 anila_core，測試因為 PYTHONPATH 帶了
anila-core 而通過，上線後 studio 起不來。這裡直接掃原始碼擋住。
"""
from pathlib import Path


def test_app_never_imports_anila_core():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import anila_core", "from anila_core")):
                offenders.append(f"{path.relative_to(app_dir)}: {stripped}")
    assert offenders == []
