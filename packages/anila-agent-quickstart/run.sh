#!/bin/bash
# MLSteam lab 的程序管理。lab 裡沒有 Docker，也沒有 systemd。
# 用法: ./run.sh {start|stop|restart|status|logs}
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"

PIDFILE="$ROOT/.anila-agent.pid"
UV_PIDFILE="$ROOT/.anila-agent.uvicorn.pid"
PUMP_PIDFILE="$ROOT/.anila-agent.pump.pid"
LOCKFILE="$ROOT/.anila-agent.lock"
LOG="$ROOT/anila-agent.log"
FIFO="$ROOT/.anila-agent.log.fifo"
SUPERVISE_ERR="$ROOT/.anila-agent.supervise.err"
MAX_LOG_BYTES=1048576
STOP_GRACE_SECONDS=30
SUPERVISOR_GRACE_SECONDS=20

if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

die() {
  echo "拒絕啟動：$*" >&2
  exit 1
}

log_pump() {
  # 單行可能超過上限。先裁掉這一行多出來的部分，寫入後再把檔案截回上限，
  # 否則沒有下一行時檔案會一直停在超限。
  LOG_PATH="$LOG" LOG_CAP="$MAX_LOG_BYTES" exec "$PY" -c '
import fcntl, os, sys
path = os.environ["LOG_PATH"]
cap = int(os.environ["LOG_CAP"])

def clip(raw):
    if len(raw) <= cap:
        return raw
    raw = raw[-cap:]
    nl = raw.find(b"\n")
    if 0 <= nl < len(raw) - 1:
        raw = raw[nl + 1:]
    if raw and not raw.endswith(b"\n"):
        raw = (raw[: max(0, cap - 1)] + b"\n")[-cap:]
    return raw

def append(line):
    if not line.endswith("\n"):
        line += "\n"
    raw = clip(line.encode("utf-8", errors="replace"))
    with open(path, "ab+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, 2)
        handle.write(raw)
        handle.flush()
        size = handle.tell()
        if size > cap:
            handle.seek(max(0, size - cap))
            data = handle.read()
            nl = data.find(b"\n")
            if 0 <= nl < len(data) - 1:
                data = data[nl + 1:]
            if len(data) > cap:
                data = data[-cap:]
            if data and not data.endswith(b"\n"):
                data = (data[: max(0, cap - 1)] + b"\n")[-cap:]
            handle.seek(0)
            handle.truncate()
            handle.write(data)
            handle.flush()

for line in sys.stdin:
    append(line)
'
}

append_log() {
  printf '%s\n' "$1" | log_pump
}

load_deployment_env() {
  [[ -f "$ROOT/deployment.env" ]] || die "缺少 deployment.env"
  # local 和賦值要分開。local var=$(cmd) 會吞掉 cmd 的失敗，檢查就擋不住啟動。
  local rendered
  rendered=$("$PY" - "$ROOT/deployment.env" <<'PY'
import shlex, sys
path = sys.argv[1]
# 開發者稍後才填的 ANILA_AGENT_ID、LLM_MODEL 不在這份清單。留空仍啟動，
# /health 分別回 not_registered、llm_not_configured。LLM_API_KEY 不寫進
# 這個檔，由 lab 的 shell export 帶進程序。
required = (
    "CSP_BASE_URL",
    "ANILA_CA_FILE",
    "LLM_BASE_URL",
    "LLM_AUTH_REQUIRED",
)
found = {}
for raw_line in open(path, encoding="utf-8"):
    line = raw_line.rstrip("\n").rstrip("\r")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    key, sep, value = line.partition("=")
    if not sep or not key or any(ch.isspace() for ch in key):
        sys.stderr.write(f"拒絕啟動：deployment.env 有無法解析的列：{line}\n")
        sys.exit(1)
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
        value = (
            value.replace("\\`", "`")
            .replace("$$", "$")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    found[key] = value
missing = [key for key in required if not str(found.get(key, "")).strip()]
if missing:
    sys.stderr.write(
        "拒絕啟動：deployment.env 缺少必要欄位："
        + "、".join(missing)
        + "。不要帶著半套設定啟動。\n"
    )
    sys.exit(1)
for key, value in found.items():
    print(f"export {key}={shlex.quote(value)}")
PY
  ) || exit 1
  eval "$rendered"
}

check_image_version() {
  "$PY" - "$ROOT/bundle.json" <<'PY'
import json, os, pathlib, sys
bundle_path = pathlib.Path(sys.argv[1])
if not bundle_path.is_file():
    sys.stderr.write("拒絕啟動：缺少 bundle.json，無法確認 lab 映像版本。\n")
    sys.exit(1)
try:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    sys.stderr.write(f"拒絕啟動：bundle.json 不是合法 JSON：{exc}\n")
    sys.exit(1)
if not isinstance(bundle, dict):
    sys.stderr.write("拒絕啟動：bundle.json 必須是 JSON 物件。\n")
    sys.exit(1)
want = str(bundle.get("compatible_lab_image_version") or "").strip()
have = os.environ.get("ANILA_LAB_IMAGE_VERSION", "").strip()
marker = pathlib.Path("/etc/anila/lab-image-version")
if not have and marker.is_file():
    have = marker.read_text(encoding="utf-8").strip()
if not want:
    sys.stderr.write("拒絕啟動：bundle.json 沒有 compatible_lab_image_version。\n")
    sys.exit(1)
if not have:
    sys.stderr.write(
        "拒絕啟動：這個環境沒有 lab 映像版本"
        "（ANILA_LAB_IMAGE_VERSION 或 /etc/anila/lab-image-version）。\n"
    )
    sys.exit(1)
if want != have:
    sys.stderr.write(
        f"拒絕啟動：bundle.json 相容的 lab 映像版本是 {want}，"
        f"這個 lab 映像是 {have}。請改用相符的映像，或重新下載 zip。\n"
    )
    sys.exit(1)
PY
}

# lab 重開後 PID 檔還在。只認 cmdline 仍是本腳本的 _supervise；
# 不符表示 PID 已被其他程序重用，清掉檔案，不要對那個程序送訊號。
is_supervisor() {
  local pid=${1:-} part script=0 mode=0
  alive "$pid" || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  while IFS= read -r -d '' part; do
    if [[ "$part" == "run.sh" || "$part" == */run.sh ]]; then
      script=1
    elif [[ "$part" == "_supervise" ]]; then
      mode=1
    fi
  done < "/proc/${pid}/cmdline" || true
  [[ "$script" -eq 1 && "$mode" -eq 1 ]]
}

supervisor_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid=$(tr -d '[:space:]' < "$PIDFILE" 2>/dev/null || true)
  if is_supervisor "$pid"; then
    echo "$pid"
    return 0
  fi
  rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE"
  return 1
}

# kill -0 對殭屍行程仍成功。lab 的 PID 1 不一定收屍（smoke 的主程式是 sleep），
# 已退出的程序要算已停止，否則 stop 會空等到強制結束。
alive() {
  local pid=${1:-} state
  [[ -n "$pid" ]] || return 1
  if [[ -r "/proc/${pid}/status" ]]; then
    state=$(awk '/^State:/{print $2}' "/proc/${pid}/status" 2>/dev/null || true)
    if [[ -z "$state" || "$state" == "Z" || "$state" == "X" ]]; then
      return 1
    fi
    return 0
  fi
  kill -0 "$pid" 2>/dev/null
}

start_uvicorn() {
  if [[ ! -p "$FIFO" ]]; then
    rm -f "$FIFO"
    mkfifo "$FIFO"
  fi
  # Open the fifo in the children. Opening it in this shell would block, and
  # bash defers traps until that command finishes, so stop could not land.
  (
    exec <"$FIFO"
    log_pump
  ) &
  pump_pid=$!
  echo "$pump_pid" > "$PUMP_PIDFILE"
  (
    export PYTHONUNBUFFERED=1
    exec >"$FIFO" 2>&1
    exec "$PY" -m uvicorn server:app --host 0.0.0.0 --port "${AGENT_PORT:-8200}" --no-proxy-headers
  ) &
  child=$!
  echo "$child" > "$UV_PIDFILE"
}

# Sleep in short slices so a deferred SIGTERM trap runs within one slice.
sleep_slice() {
  local ticks=$1
  local i=0
  while [[ "$i" -lt "$ticks" && "$stopping" -eq 0 ]]; do
    sleep 0.2 || true
    i=$((i + 1))
  done
}

reap_pump() {
  if [[ -n "${pump_pid:-}" ]]; then
    # A child that inherited the log pipe would keep the pump blocked on read.
    # Bound the wait so an unexpected uvicorn exit still restarts.
    for _ in 1 2 3 4 5; do
      if ! alive "$pump_pid"; then
        break
      fi
      sleep 0.1
    done
    if alive "$pump_pid"; then
      kill -TERM "$pump_pid" 2>/dev/null || true
    fi
    wait "$pump_pid" 2>/dev/null || true
  fi
  rm -f "$PUMP_PIDFILE"
}

supervise() {
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    echo "已有一個 anila agent 在跑" >&2
    exit 1
  fi
  echo $$ > "$PIDFILE"
  stopping=0
  child=
  pump_pid=
  delay=1
  on_term() {
    stopping=1
    if alive "${child:-}"; then
      kill -TERM "$child" 2>/dev/null || true
    fi
  }
  trap on_term TERM INT
  append_log "supervisor $$ started"
  finish() {
    rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE" "$FIFO"
    append_log "supervisor $$ stopped"
    exit 0
  }
  while true; do
    start_uvicorn
    append_log "uvicorn pid $child"
    while alive "$child"; do
      sleep_slice 1
      if [[ "$stopping" -eq 1 ]]; then
        kill -TERM "$child" 2>/dev/null || true
        break
      fi
    done
    if [[ "$stopping" -eq 1 ]]; then
      grace=0
      while alive "$child" && [[ "$grace" -lt $((SUPERVISOR_GRACE_SECONDS * 5)) ]]; do
        sleep 0.2 || true
        grace=$((grace + 1))
      done
      if alive "$child"; then
        kill -KILL "$child" 2>/dev/null || true
      fi
      reap_pump
      finish
    fi
    reap_pump
    append_log "uvicorn exited; restart in ${delay}s"
    sleep_slice $((delay * 5))
    if [[ "$stopping" -eq 1 ]]; then
      finish
    fi
    if [[ "$delay" -lt 30 ]]; then
      delay=$((delay * 2))
    fi
    if [[ "$delay" -gt 30 ]]; then
      delay=30
    fi
  done
}

cmd_start() {
  load_deployment_env
  check_image_version
  export AGENT_PORT="${AGENT_PORT:-8200}"
  if pid=$(supervisor_pid); then
    echo "status: running supervisor $pid"
    exit 0
  fi
  rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE"
  # bash reads the script, so start still works when the file itself is not executable.
  setsid bash "$0" _supervise </dev/null >>"$SUPERVISE_ERR" 2>&1 &
  for _ in $(seq 1 50); do
    if pid=$(supervisor_pid); then
      echo "status: running supervisor $pid"
      exit 0
    fi
    sleep 0.1
  done
  die "監督程序沒有留下 pid。見 $SUPERVISE_ERR"
}

cmd_stop() {
  if ! pid=$(supervisor_pid); then
    rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE"
    echo "status: stopped"
    exit 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  ticks=$((STOP_GRACE_SECONDS * 5))
  for _ in $(seq 1 "$ticks"); do
    if ! alive "$pid"; then
      rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE"
      echo "status: stopped"
      exit 0
    fi
    sleep 0.2
  done
  kill -KILL "$pid" 2>/dev/null || true
  if [[ -f "$UV_PIDFILE" ]]; then
    kill -KILL "$(tr -d '[:space:]' < "$UV_PIDFILE")" 2>/dev/null || true
  fi
  if [[ -f "$PUMP_PIDFILE" ]]; then
    kill -KILL "$(tr -d '[:space:]' < "$PUMP_PIDFILE")" 2>/dev/null || true
  fi
  rm -f "$PIDFILE" "$UV_PIDFILE" "$PUMP_PIDFILE"
  echo "status: stopped (forced)"
  exit 0
}

# Same words as GET /health: reason plus the Chinese hint. One attempt, short
# timeout, so status stays usable while uvicorn is still binding the port.
print_health_reason() {
  local port="${AGENT_PORT:-8200}"
  AGENT_PORT="$port" "$PY" - <<'PY' || true
import json, os, urllib.error, urllib.request
port = os.environ.get("AGENT_PORT", "8200")
url = f"http://127.0.0.1:{port}/health"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        raw = response.read()
except urllib.error.HTTPError as exc:
    raw = exc.read()
except Exception:
    print("reason: unreachable")
    raise SystemExit(0)
try:
    payload = json.loads(raw.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError):
    print("reason: unreadable")
    raise SystemExit(0)
if not isinstance(payload, dict):
    print("reason: unreadable")
    raise SystemExit(0)
reason = payload.get("reason") or payload.get("status") or "unknown"
if not isinstance(reason, str) or not reason.strip():
    reason = "unknown"
print("reason: " + " ".join(reason.split()))
hint = payload.get("hint")
if isinstance(hint, str) and hint.strip():
    print("hint: " + " ".join(hint.split()))
PY
}

cmd_status() {
  if pid=$(supervisor_pid); then
    uv_state="uvicorn down"
    if [[ -f "$UV_PIDFILE" ]]; then
      upid=$(tr -d '[:space:]' < "$UV_PIDFILE" || true)
      if alive "$upid"; then
        uv_state="uvicorn $upid"
      else
        uv_state="uvicorn restarting"
      fi
    fi
    # Reason first. Callers that grep -q for "status: running" exit as soon as
    # that line appears; anything written after it can die with SIGPIPE.
    print_health_reason
    echo "status: running supervisor $pid $uv_state"
    exit 0
  fi
  echo "status: stopped"
  exit 3
}

cmd_logs() {
  if [[ ! -f "$LOG" ]]; then
    echo "尚無 $LOG" >&2
    exit 1
  fi
  tail -n "${2:-200}" "$LOG"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  # stop/start call exit, so restart runs them in a child shell.
  restart)
    bash "$0" stop
    bash "$0" start
    ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  _supervise) supervise ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac
