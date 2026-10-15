"""Studio 是獨立服務，映像裡沒有 anila_core。

2026-09-26 新增的 CSRF 中介層引用了 anila_core，測試因為 PYTHONPATH 帶了
anila-core 而通過，上線後 studio 起不來。這裡直接掃原始碼擋住。
"""
from pathlib import Path
import ast


def test_app_never_imports_anila_core():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import anila_core", "from anila_core")):
                offenders.append(f"{path.relative_to(app_dir)}: {stripped}")
    assert offenders == []


def test_app_has_no_opencc_runtime_import_or_dynamic_loader():
    """字元清理只使用服務內固定詞表，不能重新帶入外部轉換詞典。"""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                loader = (
                    isinstance(node.func, ast.Name) and node.func.id in {"__import__", "import_module"}
                ) or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "importlib"
                )
                names = [node.args[0].value] if loader and isinstance(node.args[0].value, str) else []
            else:
                names = []
            if any(name == "opencc" or name.startswith("opencc.") for name in names):
                offenders.append(f"{path.relative_to(app_dir)}:{node.lineno}")
    assert offenders == []
