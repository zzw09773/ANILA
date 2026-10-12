#!/usr/bin/env bash
# lint-boundaries — CSP module boundary gate(CI Gates)。
# 以 import-linter 檢查 services/csp/.importlinter 的邊界契約:
#   1. app.modules.{tasks,policy,launch,artifacts} 互不 import(independence)
#   2. app.modules 不得 import app.api(api → modules 單向分層)
#   3. app.services 不得 import app.api(api → services 單向分層)
#
# 用法:bash infra/ci/lint-boundaries.sh
#
# .venv 不進版控。已有 services/csp/.venv/bin/lint-imports 就直接用,
# 不重建、不重裝、不改既有 CSP 開發環境。
# 目錄完全不存在時,才讀 requirements-dev.txt 裡唯一的精確釘選
# import-linter==<版本>,建立該 venv 並只裝那一包。
# 找不到、解析不到、建不起來、裝不起來,或裝完仍沒有可執行的
# lint-imports,一律失敗。不把沒跑到的檢查當成通過。
#
# 注意:必須用 lint-imports console script。
# `python -m importlinter.cli` 沒有 __main__ 入口,會靜默 exit 0(假陰性)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${SCRIPT_DIR}" || ! -f "${SCRIPT_DIR}/lint-boundaries.sh" ]]; then
    echo "lint-boundaries: 無法定位腳本目錄,本次結果是失敗。" >&2
    exit 1
fi
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CSP_DIR="${ROOT}/services/csp"
if [[ -z "${ROOT}" || ! -d "${CSP_DIR}" || ! -f "${ROOT}/infra/ci/lint-boundaries.sh" ]]; then
    echo "lint-boundaries: 無法定位 services/csp,本次結果是失敗。" >&2
    exit 1
fi
VENV="${CSP_DIR}/.venv"
LINTER="${VENV}/bin/lint-imports"
REQ="${CSP_DIR}/requirements-dev.txt"

die() {
    echo "lint-boundaries: $*" >&2
    cat >&2 <<EOF
缺少 lint-imports,邊界檢查沒有跑,本次結果是失敗。
請在倉庫根目錄手動安裝後重跑:
  python3 -m venv services/csp/.venv
  services/csp/.venv/bin/python -m pip install -r services/csp/requirements-dev.txt
  bash infra/ci/lint-boundaries.sh
若 python3 -m venv 說沒有 ensurepip,先安裝對應的 venv 套件(Debian/Ubuntu: python3-venv)。
EOF
    exit 1
}

# 只刪本腳本剛剛建立的 venv。路徑不符或是符號連結就不刪。
remove_venv_created_here() {
    case "${VENV}" in
        */services/csp/.venv) ;;
        *)
            echo "lint-boundaries: 拒絕刪除非預期路徑 ${VENV}" >&2
            return 0
            ;;
    esac
    if [[ -L "${VENV}" ]]; then
        echo "lint-boundaries: ${VENV} 是符號連結,拒絕刪除" >&2
        return 0
    fi
    if [[ -d "${VENV}" ]]; then
        rm -rf "${VENV}"
    fi
}

# 從 requirements-dev.txt 取出唯一的 import-linter==<精確版本>。
# grep 會印出整列,行尾註解、環境標記或萬用字元會一起被裝進去,所以改由
# python3 去掉註解與行接續,只接受沒有 extras、環境標記、網址或雜湊的精確釘選。
read_import_linter_spec() {
    python3 - "${REQ}" <<'PY'
import re
import sys

path = sys.argv[1]
version = (
    r"(?:[0-9]+!)?"
    r"[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?"
    r"(?:\+[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
exact = re.compile(rf"^import-linter\s*==\s*({version})$")


def fail(msg: str) -> None:
    print(f"lint-boundaries: {msg}", file=sys.stderr)
    raise SystemExit(1)


try:
    text = open(path, encoding="utf-8-sig").read()
except OSError as exc:
    fail(f"無法讀取 {path}: {exc}")

logical: list[str] = []
buf = ""
continued = False
for raw in text.splitlines():
    if raw.endswith("\\") and not raw.endswith("\\\\"):
        buf += raw[:-1] + " "
        continued = True
        continue
    logical.append(buf + raw)
    buf = ""
    continued = False
if continued:
    fail(f"{path} 以反斜線接續結尾,無法解析")


def strip_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "#":
            break
        out.append(char)
        i += 1
    return "".join(out).strip()


def project_name(item: str) -> str:
    matched_name = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", item)
    if not matched_name:
        return ""
    return re.sub(r"[-_.]+", "-", matched_name.group(1)).lower()


pins: list[str] = []
for line in logical:
    item = strip_comment(line)
    if not item:
        continue
    token = item.split(maxsplit=1)[0]
    if token in {
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-i",
        "-f",
        "--hash",
        "-c",
        "--constraint",
    }:
        fail(f"{path} 含本腳本不會沿用的 pip 選項 {token},拒絕在忽略它的情況下安裝")
    if item.startswith("-"):
        continue
    matched = exact.match(item)
    if matched:
        pins.append(f"import-linter=={matched.group(1)}")
        continue
    if project_name(item) == "import-linter":
        fail(
            f"{path} 的 import-linter 必須是不含環境標記、extras 或萬用字元的"
            f"精確釘選(import-linter==<版本>),拒絕: {item}"
        )

unique = list(dict.fromkeys(pins))
if len(unique) != 1:
    fail(f"{path} 必須恰好有一列 import-linter==<版本>,目前有 {len(unique)} 列")

print(unique[0])
PY
}

if [[ ! -x "${LINTER}" ]]; then
    if [[ -L "${VENV}" || -e "${VENV}" ]]; then
        die "已有 ${VENV},但沒有可執行的 ${LINTER}。為避免覆蓋或改動既有 CSP 開發環境,腳本不會建立、重建、刪除或 pip install"
    fi
    if [[ ! -f "${REQ}" ]]; then
        die "找不到 ${REQ}"
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        die "找不到 python3,無法建立 .venv"
    fi
    if ! spec="$(read_import_linter_spec)"; then
        die "無法從 ${REQ} 解析出唯一的 import-linter==<精確版本>"
    fi
    if [[ "${spec}" != import-linter==* || "${spec}" == *" "* || "${spec}" == *"#"* ]]; then
        die "解析結果不是精確釘選: ${spec}"
    fi
    echo "lint-boundaries: 找不到 ${LINTER},改依 ${spec} 建立 ${VENV}" >&2
    if ! python3 -m venv "${VENV}"; then
        remove_venv_created_here
        die "建立 ${VENV} 失敗"
    fi
    if [[ ! -x "${VENV}/bin/python" ]]; then
        remove_venv_created_here
        die "建立 ${VENV} 後仍沒有 ${VENV}/bin/python"
    fi
    if ! "${VENV}/bin/python" -m pip install \
        --disable-pip-version-check \
        --no-input \
        --require-virtualenv \
        --timeout 60 \
        "${spec}"; then
        remove_venv_created_here
        die "安裝 ${spec} 失敗"
    fi
    if [[ ! -x "${LINTER}" ]]; then
        remove_venv_created_here
        die "安裝後仍找不到可執行檔 ${LINTER}"
    fi
fi

cd "${CSP_DIR}"
exec "${LINTER}" --config .importlinter
