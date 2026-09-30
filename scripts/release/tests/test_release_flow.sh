#!/usr/bin/env bash
# 出貨與內網更新的純 bash 測試。不連真實 docker、不打包線上那套。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
# shellcheck source=../anila-update.sh
source "$ROOT/scripts/release/anila-update.sh"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# 測試用的 compose 專案。不能叫 anila：那是這台正在跑的平台。
TEST_PROJECT=anila-release-test

# /tmp 常掛 noexec。替身放那邊時，command -v 會跳過它、解析到 /usr/bin/docker。
# 替身一律放在家目錄，執行前再確認解析結果就是這支。
install_release_stubs() {
  local bindir="$1"
  mkdir -p "$bindir"
  cat > "$bindir/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${DOCKER_LOG:?}"
joined="$*"
if [[ "$joined" == 'compose version --short' ]]; then
  printf '%s\n' "${STUB_COMPOSE_VERSION:-2.36.2}"
  exit 0
fi
# 假的映像封存：docker save 與 buildx type=docker 都帶 manifest.json 與 index.json。
stub_image_tar() {
  local d
  d="$(mktemp -d "${HOME}/.anila-stub-img.XXXXXX")"
  printf '[{"Config":"blobs/sha256/%s","RepoTags":["stub:1"],"Layers":[]}]' \
    "${STUB_CONFIG_HEX:-deadbeef}" > "$d/manifest.json"
  printf '{"schemaVersion":2,"manifests":[{"digest":"sha256:%s"}]}' \
    "${STUB_MANIFEST_HEX:-cafe0001}" > "$d/index.json"
  head -c 2048 /dev/zero > "$d/pad"
  tar -cf - -C "$d" manifest.json index.json pad
  rm -rf "$d"
}
if [[ "$joined" == *' build --print '* ]]; then
  if [[ -n "${STUB_BAKE_JSON:-}" && -f "$STUB_BAKE_JSON" ]]; then
    cat "$STUB_BAKE_JSON"
  else
    printf '{"target":{}}\n'
  fi
  exit 0
fi
if [[ "$joined" == 'buildx inspect'* ]]; then
  printf 'Name: stub\nDriver: docker-container\n'
  exit 0
fi
if [[ "$joined" == 'buildx bake'* ]]; then
  for arg in "$@"; do
    case "$arg" in
      *.output=type=docker,dest=*) stub_image_tar > "${arg#*,dest=}" ;;
    esac
  done
  exit 0
fi
if [[ "${1:-}" == save ]]; then
  stub_image_tar
  exit 0
fi
if [[ "${1:-}" == load ]]; then
  cat >/dev/null
  exit 0
fi
if [[ "$joined" == *' ps -a '* ]]; then
  if [[ "${STUB_HEALTH_FAIL:-0}" == 1 ]]; then
    printf 'csp Up (unhealthy)\n'
    exit 0
  fi
  if [[ -n "${DOCKER_PS_FILE:-}" && -f "$DOCKER_PS_FILE" ]]; then
    cat "$DOCKER_PS_FILE"
  fi
  exit 0
fi
if [[ "$joined" == *version_num* ]]; then
  if [[ "$joined" == *csp_restore* ]]; then
    printf '%s\n' "${STUB_RESTORE_ALEMBIC:-r1_0062}"
  else
    printf '%s\n' "${STUB_LIVE_ALEMBIC:-r1_0062}"
  fi
  exit 0
fi
if [[ "$joined" == *'INSERT INTO audit_logs'* ]]; then
  if [[ "${STUB_AUDIT_FAIL:-0}" == 1 ]]; then
    exit 1
  fi
  exit 0
fi
if [[ "$joined" == *pg_isready* && "${STUB_DB_DOWN:-0}" == 1 ]]; then
  exit 1
fi
if [[ "$joined" == *'FROM banners'* ]]; then
  printf '%s\n' "${STUB_BANNER_COUNT:-0}"
  exit 0
fi
if [[ "$joined" == *pg_dump* ]]; then
  if [[ "${STUB_DUMP_FAIL:-0}" == 1 ]]; then
    exit 1
  fi
  printf 'PGDMP'
  exit 0
fi
if [[ "$joined" == *created_at* ]]; then
  printf '%s\n' "${STUB_COUNTS:-0|0|0}"
  exit 0
fi
if [[ "$joined" == *'compose ls'* ]]; then
  if [[ "${STUB_EXISTING_STACK:-0}" == 1 ]]; then
    printf '%s\n' "${COMPOSE_PROJECT_NAME:-anila-release-test}"
  fi
  exit 0
fi
if [[ "$joined" == *'volume ls'* ]]; then
  if [[ "$joined" == *anila-studio-artifacts* ]]; then
    printf '%s_anila-studio-artifacts\n' "${COMPOSE_PROJECT_NAME:-anila-release-test}"
    exit 0
  fi
  if [[ "${STUB_EXISTING_STACK:-0}" == 1 || "${STUB_EXISTING_VOLUME:-0}" == 1 ]]; then
    printf '%s_csp-pgdata\n' "${COMPOSE_PROJECT_NAME:-anila-release-test}"
  fi
  exit 0
fi
if [[ "$joined" == *pg_restore* && "${STUB_RESTORE_FAIL:-0}" == 1 ]]; then
  exit 1
fi
if [[ "$joined" == *anila-restore-staging* && "${STUB_STUDIO_RESTORE_FAIL:-0}" == 1 ]]; then
  exit 1
fi
if [[ "$joined" == *'--entrypoint'* && "$joined" == *' tar '* ]]; then
  host=""
  for arg in "$@"; do
    case "$arg" in
      *:/backup|*:/backup:ro) host="${arg%%:/backup*}" ;;
    esac
  done
  if [[ -n "$host" && "$joined" == *'-cf'* ]]; then
    mkdir -p "$host"
    printf 'studio-snapshot\n' > "$host/studio-artifacts.tar"
  fi
  exit 0
fi
if [[ "$joined" == *'image inspect'* ]]; then
  ref="${*: -1}"
  # 模擬 containerd 儲存：設定檔雜湊查不到。
  if [[ -n "${STUB_MISSING_IDS:-}" && " $STUB_MISSING_IDS " == *" $ref "* ]]; then
    printf 'Error: No such image: %s\n' "$ref" >&2
    exit 1
  fi
  if [[ "$ref" == *@sha256:* ]]; then
    printf 'Error: No such image: %s\n' "$ref" >&2
    exit 1
  fi
  if [[ "$joined" == *'{{.Id}}'* ]]; then
    if [[ "$ref" == sha256:* ]]; then
      printf '%s\n' "$ref"
    else
      printf '%s\n' "${STUB_IMAGE_ID:-sha256:deadbeef}"
    fi
  fi
  exit 0
fi
if [[ "$joined" == *'image ls'* ]]; then
  if [[ -n "${DOCKER_IMAGE_LS_FILE:-}" && -f "$DOCKER_IMAGE_LS_FILE" ]]; then
    cat "$DOCKER_IMAGE_LS_FILE"
  fi
  exit 0
fi
exit 0
EOF
  cat > "$bindir/pg_dump" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "pg_dump $*" >> "${DOCKER_LOG:?}"
printf 'PGDMP'
exit 0
EOF
  cat > "$bindir/psql" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "psql $*" >> "${DOCKER_LOG:?}"
exit 0
EOF
  cat > "$bindir/curl" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *http_code* ]]; then
  printf '200'
fi
exit 0
EOF
  chmod +x "$bindir/docker" "$bindir/pg_dump" "$bindir/psql" "$bindir/curl"
}

# 解析到的 docker / pg_dump / psql 必須是替身，否則中止整份測試。
# 直接執行替身的絕對路徑做探針，不會沿著 PATH 落到真的 docker。
assert_release_stubs() {
  local bindir="$1" tool resolved probe_log
  hash -r
  case ":$PATH:" in
    *":$bindir:"*) ;;
    *) export PATH="$bindir:$PATH" ;;
  esac
  for tool in docker pg_dump psql; do
    resolved="$(command -v "$tool" 2>/dev/null || true)"
    if [[ "$resolved" != "$bindir/$tool" ]]; then
      printf '測試中止：解析到的 %s 是「%s」，不是替身 %s。\n這會打到這台正在跑的平台，測試不再繼續。\n' \
        "$tool" "${resolved:-找不到}" "$bindir/$tool" >&2
      exit 1
    fi
  done
  probe_log="$(mktemp "${HOME}/.anila-stub-probe.XXXXXX")"
  if ! DOCKER_LOG="$probe_log" "$bindir/docker" __anila_stub_probe__; then
    rm -f "$probe_log"
    printf '測試中止：docker 替身無法執行。不要把替身放在 /tmp（noexec 會讓 bash 改跑 /usr/bin/docker）。\n' >&2
    exit 1
  fi
  if ! grep -q '__anila_stub_probe__' "$probe_log"; then
    rm -f "$probe_log"
    printf '測試中止：docker 替身沒有留下探針紀錄。\n' >&2
    exit 1
  fi
  rm -f "$probe_log"
}

# 模擬「初次安裝已記錄」：根目錄與 compose 專案都寫進 state。專案不得是 anila。
arm_test_install() {
  local root="$1" canon
  [[ "$TEST_PROJECT" != "anila" ]] || fail "測試專案不能叫 anila"
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  mkdir -p "$root/state"
  canon="$(cd "$root" && pwd -P)"
  state_set install_root "$canon"
  state_set compose_project "$TEST_PROJECT"
}

# compose 目錄必須是安裝根目錄的 versions/<目前版本>。
use_version_tree() {
  local root="$1" ver="${2:-2026.09.29-1}" dest
  dest="$root/versions/$ver"
  mkdir -p "$dest"
  if [[ ! -f "$dest/compose.yaml" ]]; then
    printf 'name: anila\n' > "$dest/compose.yaml"
  fi
  ln -sfn "versions/$ver" "$root/current"
  state_set current "$ver"
  printf '%s' "$dest"
}

tmp="$(mktemp -d "${HOME}/.anila-release-test.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
STUB_BIN="$tmp/bin"
install_release_stubs "$STUB_BIN"
export DOCKER_LOG="$tmp/docker.log"
: > "$DOCKER_LOG"
assert_release_stubs "$STUB_BIN"

# ── 版本號：同一天加 1，隔天從 1 開始，已有的包也算 ────────────────────────
out="$tmp/releases"
v1="$(release_next_version "$out" "2026.09.29")"
[[ "$v1" == "2026.09.29-1" ]] || fail "第一版應為 2026.09.29-1，得到 $v1"
v2="$(release_next_version "$out" "2026.09.29")"
[[ "$v2" == "2026.09.29-2" ]] || fail "第二版應為 2026.09.29-2，得到 $v2"
v3="$(release_next_version "$out" "2026.09.30")"
[[ "$v3" == "2026.09.30-1" ]] || fail "隔天應從 1 開始，得到 $v3"
touch "$out/anila-2026.09.30-4.tar.gz"
v4="$(release_next_version "$out" "2026.09.30")"
[[ "$v4" == "2026.09.30-5" ]] || fail "已有 -4 時下一版應為 -5，得到 $v4"

# ── 工作目錄不乾淨就拒絕打包 ──────────────────────────────────────────────
repo="$tmp/repo"
git init -q "$repo"
printf 'a\n' > "$repo/a"
git -C "$repo" add a
git -C "$repo" -c user.email=test@example.com -c user.name=test commit -q -m init
release_assert_clean "$repo" || fail "乾淨的樹不該被拒絕"
printf 'b\n' >> "$repo/a"
if release_assert_clean "$repo"; then
  fail "有變更的樹應該拒絕打包"
fi

# ── 清單 SHA256：改一個位元組就拒絕 ───────────────────────────────────────
bundle="$tmp/bundle"
mkdir -p "$bundle/images" "$bundle/compose"
printf 'source\n' > "$bundle/source.tar.gz"
printf 'img\n' > "$bundle/images/csp.tar.gz"
printf 'compose\n' > "$bundle/compose/compose.yaml"
lines="$tmp/images.txt"
printf 'image csp anila-csp:latest sha256:abc images/csp.tar.gz\n' > "$lines"
release_seal_manifest "$bundle" "2026.09.29-1" "abc123" "$lines"
release_verify_bundle "$bundle" || fail "剛封好的清單應該通過"
printf 'x\n' >> "$bundle/source.tar.gz"
if release_verify_bundle "$bundle"; then
  fail "檔案被改過仍通過清單核對"
fi
# 只改清單、不改 checksum，也要拒絕
release_seal_manifest "$bundle" "2026.09.29-1" "abc123" "$lines"
printf 'version 2026.09.29-9\n' >> "$bundle/manifest.txt"
if release_verify_bundle "$bundle"; then
  fail "清單被改過仍通過核對"
fi

# ── 回復必須打對版本 ──────────────────────────────────────────────────────
confirm_rollback_version "2026.09.29-1" "2026.09.29-1" || fail "打對版本不該取消"
if confirm_rollback_version "2026.09.29-1" "2026.09.29-2"; then
  fail "打錯版本不該繼續"
fi
if confirm_rollback_version "2026.09.29-1" "no"; then
  fail "隨意輸入不該繼續"
fi
rollback_needs_db_restore "r1_0062" "r1_0062" && fail "遷移沒變不該還原資料庫"
rollback_needs_db_restore "r1_0062" "r1_0063" || fail "遷移變了應該還原資料庫"
rollback_needs_db_restore "" "r1_0062" && fail "沒有更新前的版本號時不該還原"

# ── 內網腳本不得建置或拉取；codeserver / n8n 不在預設啟動清單 ─────────────
code="$(sed -E 's/(^|[[:space:]])#.*$//' \
  "$ROOT/scripts/release/anila-update.sh" \
  "$ROOT/scripts/release/release-lib.sh")"
if printf '%s\n' "$code" | grep -E 'docker[[:space:]]+pull|docker[[:space:]]+build|compose[[:space:]]+(build|pull)([[:space:]]|$)'; then
  fail "內網腳本含有建置或拉取"
fi
grep -q -- '--no-build' <<<"$code" || fail "更新沒有 --no-build"
grep -q -- '--pull never' <<<"$code" || fail "更新沒有 --pull never"
# docker run 若出現，必須帶 --pull never，避免映像名字打錯時去外網拉
while IFS= read -r line; do
  [[ "$line" == *docker\ run* ]] || continue
  [[ "$line" == *--pull\ never* ]] || fail "docker run 沒有 --pull never：$line"
done < <(printf '%s\n' "$code")
# 把 --pull never / --no-build 拿掉之後，不該再出現 pull 或 build 當指令
cleaned="$(printf '%s\n' "$code" | sed 's/--pull never//g; s/--no-build//g')"
if printf '%s\n' "$cleaned" | grep -E '(^|[[:space:]])(pull|build)([[:space:]]|$)'; then
  fail "拿掉旗標後仍有 pull 或 build"
fi

started="$(services_to_start)"
printf '%s\n' "$started" | grep -qx csp || fail "預設應該啟動 csp"
if printf '%s\n' "$started" | grep -Eq '^(codeserver|codeserver-init|n8n)$'; then
  fail "codeserver 或 n8n 被預設啟動"
fi
# 不要用 grep -q 接管道：pipefail 下，對到就關管道會讓左邊收到 SIGPIPE，整段被算失敗。
catalog="$(release_catalog)"
grep -q '^codeserver ' <<<"$catalog" || fail "出貨清單沒有 codeserver"
grep -q '^n8n ' <<<"$catalog" || fail "出貨清單沒有 n8n"

log="$tmp/docker.log"
: > "$log"
# 子 shell 裡登記測試專案，避免把 COMPOSE_PROJECT_NAME 留在這份測試的外層。
(
  trap - EXIT
  root="$tmp/up-root"
  mkdir -p "$root"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  DOCKER_LOG="$log" compose_up_no_build "$tree"
) || fail "compose up 被拒絕或失敗"
grep -q -- '--no-build' "$log" || fail "實際呼叫沒有 --no-build"
grep -q -- '--pull never' "$log" || fail "實際呼叫沒有 --pull never"
if grep -E '(^| )pull($| )|(^| )build($| )' "$log"; then
  fail "實際呼叫含有 pull 或 build：$log"
fi

# 沒有公告時預設取消；有公告則不詢問
banner_gate 2 "" || fail "有公告不該擋住"
if banner_gate 0 n; then
  fail "沒有公告又回答 n 不該繼續"
fi
banner_gate 0 y || fail "明確回答 y 應該繼續"

# 新的斷言用 check：印出每一項失敗，最後一次退出。
# 生產碼的 die 會 exit，所以會失敗的呼叫都放在子 shell。
_fail_count=0
check() {
  local name="$1"
  shift
  # 每一項開始前再確認一次。解析到真的 docker 就中止整份測試，不是只記一項失敗。
  assert_release_stubs "$STUB_BIN"
  set +e
  # 子 shell 裡清掉本檔的 EXIT，避免測完一項就把暫存目錄刪掉。
  # 子 shell 自己開 set -e，失敗的 exit 不會把整份測試一起帶走。
  (
    trap - EXIT
    set -euo pipefail
    "$@"
  )
  local rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    printf 'FAIL %s\n' "$name" >&2
    _fail_count=$((_fail_count + 1))
  fi
}

first_line() {
  awk -v re="$2" '$0 ~ re { print NR; exit }' "$1"
}

write_ps_file() {
  local dest="$1" svc
  : > "$dest"
  while IFS= read -r svc; do
    [[ -n "$svc" ]] || continue
    if [[ "$svc" == "csp-credential-dirs" ]]; then
      printf '%s Exited (0)\n' "$svc"
    else
      printf '%s Up (healthy)\n' "$svc"
    fi
  done < <(services_to_start) >> "$dest"
}

# set_env 改寫時若用 mv 蓋掉 .env，版本目錄裡的 symlink 會變成一般檔，
# 密鑰離開 state/，下一版會重新產生 SECRET_KEY。
test_set_env_keeps_symlink_and_mode() {
  local envtree="$tmp/envtree" state="$tmp/envstate" mode oldmask
  oldmask="$(umask)"
  umask 022
  mkdir -p "$envtree" "$state"
  printf 'SECRET_KEY=keep\nANILA_HOST=lab\n' > "$state/.env"
  chmod 644 "$state/.env"
  ln -sfn "$state/.env" "$envtree/.env"
  (
    cd "$envtree"
    set_env ADMIN_PASSWORD 's3cret'
    set_env SECRET_KEY 'rotated'
  ) || return 1
  [[ -L "$envtree/.env" ]] || { echo "版本目錄的 .env 不再是 symlink" >&2; umask "$oldmask"; return 1; }
  if find "$envtree" -maxdepth 1 -name '.env' ! -type l | grep -q .; then
    echo "密鑰寫進版本目錄" >&2
    umask "$oldmask"
    return 1
  fi
  grep -qx 'ANILA_HOST=lab' "$state/.env" || { echo "其他鍵消失" >&2; umask "$oldmask"; return 1; }
  grep -qx 'ADMIN_PASSWORD=s3cret' "$state/.env" || { echo "新鍵沒寫進 state/.env" >&2; umask "$oldmask"; return 1; }
  grep -qx 'SECRET_KEY=rotated' "$state/.env" || { echo "舊鍵沒有改到 state/.env" >&2; umask "$oldmask"; return 1; }
  mode="$(stat -c %a "$state/.env")"
  umask "$oldmask"
  [[ "$mode" == "600" ]] || { echo "state/.env 權限是 $mode" >&2; return 1; }
  [[ ! -e "$envtree/.env.tmp" ]] || { echo "留下 .env.tmp" >&2; return 1; }
}

# 更新前備份在 umask 022 的環境也必須是目錄 700、檔案 600。
test_preupdate_dump_is_private() {
  local oldmask tree dest dmode fmode pmode dumpcmd
  oldmask="$(umask)"
  umask 022
  tree="$tmp/dumptree"
  mkdir -p "$tree/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" "$tree/infra/deployment/scripts/backup-lib.sh"
  printf 'name: anila\n' > "$tree/compose.yaml"
  dumpcmd="$tmp/pgdump.sh"
  cat > "$dumpcmd" <<'EOF'
#!/bin/sh
printf 'PGDMP-data\n' > "$1"
EOF
  chmod +x "$dumpcmd"
  dest="$tmp/backups/pre-update/2026.09.29-1/db.dump"
  ANILA_PG_DUMP_CMD="$dumpcmd" preupdate_dump "$tree" "$dest" || { umask "$oldmask"; return 1; }
  dmode="$(stat -c %a "$(dirname "$dest")")"
  pmode="$(stat -c %a "$(dirname "$(dirname "$dest")")")"
  fmode="$(stat -c %a "$dest")"
  umask "$oldmask"
  unset ANILA_PG_DUMP_CMD
  [[ "$dmode" == "700" ]] || { echo "備份目錄權限 $dmode" >&2; return 1; }
  [[ "$pmode" == "700" ]] || { echo "pre-update 目錄權限 $pmode" >&2; return 1; }
  [[ "$fmode" == "600" ]] || { echo "備份檔權限 $fmode" >&2; return 1; }
}

# 壓縮包要解到安裝根目錄，而且不論成功或失敗，離開時都清掉暫存目錄。
test_open_bundle_extracts_under_install_root_and_cleans() {
  local root ship tar marker path leaks left
  root="$tmp/opt-anila"
  mkdir -p "$root"
  ship="$tmp/ship-src"
  mkdir -p "$ship"
  printf 'ANILA-MANIFEST 1\n' > "$ship/manifest.txt"
  tar -C "$tmp" -czf "$tmp/ship.tar.gz" ship-src
  marker="$tmp/opened-path"
  find /tmp -maxdepth 1 -type d -name 'anila-bundle.*' -exec rm -rf {} + 2>/dev/null || true
  (
    trap - EXIT
    set -euo pipefail
    export ANILA_INSTALL_ROOT="$root"
    _arm_exit
    path_inner="$(open_bundle "$tmp/ship.tar.gz")"
    printf '%s\n' "$path_inner" > "$marker"
    [[ -f "$path_inner/manifest.txt" ]]
  ) || { echo "解開出貨包失敗" >&2; return 1; }
  [[ -f "$marker" ]] || return 1
  path="$(cat "$marker")"
  [[ "$path" == "$root/"* ]] || { echo "解到安裝根目錄之外：$path" >&2; return 1; }
  [[ "$path" != /tmp/* ]] || { echo "解到 /tmp：$path" >&2; return 1; }
  [[ ! -e "$path" ]] || { echo "成功後暫存還在：$path" >&2; return 1; }
  printf 'not-a-tarball\n' > "$tmp/bad.tar.gz"
  if (
    trap - EXIT
    set -euo pipefail
    export ANILA_INSTALL_ROOT="$root"
    open_bundle "$tmp/bad.tar.gz" >"$tmp/bad-out.txt"
  ); then
    echo "壞掉的壓縮檔不該解開" >&2
    return 1
  fi
  leaks="$(find /tmp -maxdepth 1 -type d -name 'anila-bundle.*' 2>/dev/null || true)"
  find "$root" -maxdepth 1 -type d -name 'incoming.*' -print > "$tmp/left-incoming.txt" || true
  if [[ -n "$leaks" ]]; then
    # shellcheck disable=SC2086
    rm -rf $leaks
  fi
  [[ -z "$leaks" ]] || { echo "失敗後 /tmp 仍留著出貨包" >&2; return 1; }
  [[ ! -s "$tmp/left-incoming.txt" ]] || { echo "失敗後安裝根目錄仍留著暫存" >&2; cat "$tmp/left-incoming.txt" >&2; return 1; }
  left="$tmp/plainbundle"
  mkdir -p "$left"
  printf 'm\n' > "$left/manifest.txt"
  path="$(ANILA_INSTALL_ROOT="$root" open_bundle "$left")" || return 1
  [[ "$path" == "$(readlink -f "$left")" ]] || return 1
  [[ -d "$left" ]] || { echo "使用者給的目錄被刪了" >&2; return 1; }
}

# 還原必須進一顆空資料庫再換上，並核對備份當下記下的 alembic 版本。
test_restore_swaps_clean_database_and_checks_alembic() {
  local root tree dump log stub
  root="$tmp/restore-root"
  stub="$tmp/restore-bin"
  log="$tmp/restore.log"
  mkdir -p "$root"
  dump="$root/db.dump"
  printf 'PGDMP\n' > "$dump"
  printf 'r1_0062\n' > "${dump}.alembic"
  : > "$log"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  export DOCKER_LOG="$log"
  export STUB_RESTORE_ALEMBIC=r1_0062
  if ! ( trap - EXIT; restore_dump "$tree" "$dump" ); then
    echo "版本相符時還原失敗" >&2
    return 1
  fi
  grep -q 'CREATE DATABASE csp_restore' "$log" || { echo "沒有建乾淨資料庫" >&2; return 1; }
  if grep -q -- '--clean' "$log"; then
    echo "仍用 pg_restore --clean" >&2
    return 1
  fi
  grep -q 'RENAME TO csp' "$log" || { echo "沒有把還原庫換上" >&2; return 1; }
  local created checked swapped
  created="$(first_line "$log" 'CREATE DATABASE csp_restore')"
  checked="$(first_line "$log" 'version_num')"
  swapped="$(first_line "$log" 'ALTER DATABASE csp RENAME TO csp_previous')"
  [[ -n "$created" && -n "$checked" && -n "$swapped" ]] || { echo "還原步驟不完整" >&2; return 1; }
  [[ "$created" -lt "$checked" && "$checked" -lt "$swapped" ]] || {
    echo "核對版本之前就換掉了線上資料庫 create=$created check=$checked swap=$swapped" >&2
    return 1
  }
  log="$tmp/restore-mismatch.log"
  export DOCKER_LOG="$log"
  export STUB_RESTORE_ALEMBIC=r1_0099
  : > "$log"
  if ( trap - EXIT; restore_dump "$tree" "$dump" ); then
    echo "alembic 不符仍繼續" >&2
    return 1
  fi
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "版本不符仍刪掉線上資料庫" >&2
    return 1
  fi
}

# 備份前先停寫入，並把備份時間與 alembic 版本跟 dump 放在一起。
test_backup_stops_writers_and_records_cutoff() {
  local tree dest log stamp dump_at csp_at pgb_at svc at mode
  log="$tmp/quiet.log"
  dest="$tmp/quiet-root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  mkdir -p "$tmp/quiet-root"
  : > "$log"
  arm_test_install "$tmp/quiet-root"
  tree="$(use_version_tree "$tmp/quiet-root" "2026.09.29-1")"
  mkdir -p "$tree/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" "$tree/infra/deployment/scripts/backup-lib.sh"
  export DOCKER_LOG="$log"
  export STUB_LIVE_ALEMBIC=r1_0062
  unset ANILA_PG_DUMP_CMD || true
  stamp="$(backup_for_update "$tree" "$dest")" || { echo "backup_for_update 失敗" >&2; return 1; }
  [[ "$stamp" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || {
    echo "備份時間格式不對：$stamp" >&2
    return 1
  }
  [[ "$(tr -d '[:space:]' < "${dest}.time")" == "$stamp" ]] || { echo "時間沒有跟備份放在一起" >&2; return 1; }
  [[ "$(tr -d '[:space:]' < "${dest}.alembic")" == "r1_0062" ]] || { echo "alembic 沒有跟備份放在一起" >&2; return 1; }
  mode="$(stat -c %a "${dest}.time")"
  [[ "$mode" == "600" ]] || { echo "時間檔權限 $mode" >&2; return 1; }
  mode="$(stat -c %a "${dest}.alembic")"
  [[ "$mode" == "600" ]] || { echo "版本檔權限 $mode" >&2; return 1; }
  dump_at="$(first_line "$log" 'pg_dump')"
  [[ -n "$dump_at" ]] || { echo "沒有 pg_dump" >&2; return 1; }
  stop_line_for() {
    awk -v svc="$2" '
      {
        stopat = 0
        for (i = 1; i <= NF; i++) if ($i == "stop") stopat = i
        if (stopat == 0) next
        for (i = stopat + 1; i <= NF; i++) if ($i == svc) { print NR; exit }
      }
    ' "$1"
  }
  csp_at="$(stop_line_for "$log" csp)"
  pgb_at="$(stop_line_for "$log" pgbouncer)"
  [[ -n "$csp_at" && -n "$pgb_at" ]] || {
    echo "沒有先停寫入路徑" >&2
    cat "$log" >&2
    return 1
  }
  [[ "$csp_at" -lt "$pgb_at" && "$pgb_at" -lt "$dump_at" ]] || {
    echo "停寫入的順序不對 csp=$csp_at pgbouncer=$pgb_at dump=$dump_at" >&2
    return 1
  }
  for svc in nginx anila-ui anilalm anila-studio router ingestion-worker csp pptx-renderer backup codeserver n8n asr-gateway; do
    at="$(awk -v svc="$svc" '
      {
        stopat = 0
        for (i = 1; i <= NF; i++) if ($i == "stop") stopat = i
        if (stopat == 0) next
        for (i = stopat + 1; i <= NF; i++) if ($i == svc) { print NR; exit }
      }
    ' "$log")"
    [[ -n "$at" && "$at" -lt "$dump_at" ]] || { echo "備份前沒停 $svc" >&2; return 1; }
  done
}

layout_rollback_install() {
  local root="$1" dump
  mkdir -p "$root/versions/2026.09.29-1" "$root/versions/2026.09.29-2" \
    "$root/state/share/backups/pre-update/2026.09.29-1"
  printf 'name: anila\n' > "$root/versions/2026.09.29-1/compose.yaml"
  printf 'name: anila\n' > "$root/versions/2026.09.29-2/compose.yaml"
  ln -sfn versions/2026.09.29-2 "$root/current"
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  printf 'PGDMP\n' > "$dump"
  printf '2026-09-29T01:02:03Z\n' > "${dump}.time"
  printf 'r1_0062\n' > "${dump}.alembic"
  cat > "$root/state/release.state" <<EOF
current=2026.09.29-2
previous=2026.09.29-1
updated_at=2020-01-01T00:00:00Z
pre_update_dump=${dump}
alembic_before=r1_0062
EOF
  chmod 600 "$root/state/release.state"
}

# 手動回復要從備份時間算會消失的筆數，資料庫稽核要在換上還原庫之後才寫。
test_manual_rollback_uses_backup_time_and_audits_after_swap() {
  local root stub log ps out err drop_at audit_at
  root="$tmp/rollback-root"
  stub="$tmp/rollback-bin"
  log="$tmp/rollback.log"
  ps="$tmp/rollback-ps"
  out="$tmp/rollback.out"
  err="$tmp/rollback.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  write_ps_file "$ps"
  : > "$log"
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  export DOCKER_LOG="$log"
  export DOCKER_PS_FILE="$ps"
  export STUB_RESTORE_ALEMBIC=r1_0062
  export STUB_COUNTS='4|5|6'
  if ! ( trap - EXIT; printf '%s\n' '2026.09.29-1' | cmd_rollback >"$out" 2>"$err" ); then
    echo "手動回復失敗" >&2
    cat "$err" >&2
    return 1
  fi
  grep -q '2026-09-29T01:02:03Z' "$log" || { echo "沒有用備份時間計算" >&2; return 1; }
  if grep -q '2020-01-01T00:00:00Z' "$log"; then
    echo "仍用更新完成時間計算，備份後到更新結束之間的寫入沒被算進去" >&2
    return 1
  fi
  grep -q '目前版本是 2026.09.29-2' "$out" || { echo "畫面上沒有目前版本" >&2; return 1; }
  grep -q '對話 4' "$out" || return 1
  grep -q '訊息 5' "$out" || return 1
  grep -q '文件 6' "$out" || return 1
  local stop_at count_at
  stop_at="$(awk '
    {
      stopat = 0
      for (i = 1; i <= NF; i++) if ($i == "stop") stopat = i
      if (stopat == 0) next
      for (i = stopat + 1; i <= NF; i++) if ($i == "csp") { print NR; exit }
    }
  ' "$log")"
  count_at="$(first_line "$log" 'created_at')"
  [[ -n "$stop_at" && -n "$count_at" && "$stop_at" -lt "$count_at" ]] || {
    echo "應該先停寫入再計算會消失的筆數 stop=$stop_at count=$count_at" >&2
    return 1
  }
  drop_at="$(first_line "$log" 'ALTER DATABASE csp RENAME TO csp_previous')"
  audit_at="$(first_line "$log" 'INSERT INTO audit_logs')"
  [[ -n "$drop_at" && -n "$audit_at" && "$drop_at" -lt "$audit_at" ]] || {
    echo "資料庫稽核不在還原完成之後 drop=$drop_at audit=$audit_at" >&2
    return 1
  }
  local up1 health nginx_up up1_line
  up1="$(first_line "$log" 'up -d')"
  health="$(first_line "$log" '127.0.0.1:8000/health')"
  nginx_up="$(awk 'index($0, "up -d") && index($0, " nginx") { print NR; exit }' "$log")"
  up1_line="$(awk 'index($0, "up -d") { print; exit }' "$log")"
  [[ -n "$up1" && -n "$health" && -n "$nginx_up" && "$up1" -lt "$health" && "$health" -lt "$nginx_up" ]] || {
    echo "入口在內部健康檢查之前就開了 up=$up1 health=$health nginx=$nginx_up" >&2
    return 1
  }
  if [[ "$up1_line" == *" nginx"* || "$up1_line" == *" nginx " ]]; then
    echo "第一輪啟動包含 nginx" >&2
    return 1
  fi
  if grep -q 'exec -T csp python' "$log"; then
    echo "稽核仍透過 csp 容器，csp 沒起來就寫不進去" >&2
    return 1
  fi
  [[ -f "$root/state/operations.log" ]] || { echo "沒有 operations.log" >&2; return 1; }
  grep -E $'\trollback\t.*\tsuccess$' "$root/state/operations.log" >/dev/null || {
    echo "回復成功沒寫進 operations.log" >&2
    return 1
  }
  [[ "$(stat -c %a "$root/state/operations.log")" == "600" ]] || return 1
}

# 自動回復在還原之後才把結果寫進資料庫；檔案紀錄留在資料庫外面。
test_auto_rollback_audits_after_restore() {
  local root stub log ps dump drop_at audit_at
  root="$tmp/auto-root"
  stub="$tmp/auto-bin"
  log="$tmp/auto.log"
  ps="$tmp/auto-ps"
  layout_rollback_install "$root"
  arm_test_install "$root"
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  write_ps_file "$ps"
  : > "$log"
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  export DOCKER_LOG="$log"
  export DOCKER_PS_FILE="$ps"
  export STUB_LIVE_ALEMBIC=r1_0099
  export STUB_RESTORE_ALEMBIC=r1_0062
  if ! ( trap - EXIT; auto_rollback "$root" "2026.09.29-1" "2026.09.29-2" "r1_0062" "$dump" ); then
    echo "自動回復失敗" >&2
    return 1
  fi
  drop_at="$(first_line "$log" 'ALTER DATABASE csp RENAME TO csp_previous')"
  audit_at="$(first_line "$log" 'INSERT INTO audit_logs')"
  [[ -n "$drop_at" && -n "$audit_at" && "$drop_at" -lt "$audit_at" ]] || {
    echo "自動回復的稽核不在還原之後 drop=$drop_at audit=$audit_at" >&2
    return 1
  }
  if grep -q 'exec -T csp python' "$log"; then
    echo "稽核仍透過 csp 容器" >&2
    return 1
  fi
  # 還原後的資料庫要同時有回復結果，以及這次失敗的更新。
  grep -q 'platform_rollback' "$log" || return 1
  grep -q 'platform_update' "$log" || { echo "還原後的資料庫沒有失敗更新的稽核" >&2; return 1; }
  local update_at
  update_at="$(first_line "$log" 'platform_update')"
  [[ -n "$update_at" && "$drop_at" -lt "$update_at" ]] || return 1
  grep -E $'\trollback\t.*\tsuccess$' "$root/state/operations.log" >/dev/null || return 1
}

# 更新失敗（還沒動到資料庫）也要留下檔案紀錄。資料庫還原不會把它清掉。
test_failed_update_is_in_operations_log() {
  local root stub log
  root="$tmp/fail-root"
  stub="$tmp/fail-bin"
  log="$tmp/fail.log"
  mkdir -p "$root"
  : > "$log"
  arm_test_install "$root"
  export DOCKER_LOG="$log"
  if ( trap - EXIT; cmd_update "$tmp/does-not-exist.tar.gz" ); then
    echo "找不到出貨包卻成功" >&2
    return 1
  fi
  [[ -f "$root/state/operations.log" ]] || { echo "失敗沒寫 operations.log" >&2; return 1; }
  grep -E $'\tupdate\t.*\tfailure$' "$root/state/operations.log" >/dev/null || {
    echo "operations.log 沒有更新失敗" >&2
    cat "$root/state/operations.log" >&2
    return 1
  }
  [[ "$(stat -c %a "$root/state/operations.log")" == "600" ]] || return 1
}

# 資料庫寫入成功時也要追加檔案紀錄，不能只在寫庫失敗時才留 fallback。
test_operations_log_appends_even_when_database_accepts() {
  local oldmask root tree log mode
  oldmask="$(umask)"
  umask 022
  root="$tmp/ops-root"
  log="$tmp/ops.log"
  mkdir -p "$root/state"
  printf 'earlier\n' > "$root/state/operations.log"
  chmod 644 "$root/state/operations.log"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  : > "$log"
  export DOCKER_LOG="$log"
  record_audit "$tree" update 2026.09.29-1 2026.09.29-2 success || { umask "$oldmask"; return 1; }
  record_audit "$tree" rollback 2026.09.29-2 2026.09.29-1 failure || { umask "$oldmask"; return 1; }
  mode="$(stat -c %a "$root/state/operations.log")"
  umask "$oldmask"
  [[ "$mode" == "600" ]] || { echo "operations.log 權限 $mode" >&2; return 1; }
  [[ "$(grep -c '^earlier$' "$root/state/operations.log")" == "1" ]] || {
    echo "operations.log 被截斷" >&2
    return 1
  }
  grep -q $'update\t2026.09.29-1\t2026.09.29-2\tsuccess' "$root/state/operations.log" || return 1
  grep -q $'rollback\t2026.09.29-2\t2026.09.29-1\tfailure' "$root/state/operations.log" || return 1
  [[ "$(wc -l < "$root/state/operations.log" | tr -d '[:space:]')" == "3" ]] || return 1
}

# 2026-09-29 負責人決定：asr-gateway 跟平台一起啟動（解碼端屬模型側，
# 沒設定或不健康時麥克風自己藏起來，容器健康只看 gateway 本身）。
test_asr_gateway_shipped_and_started() {
  local line
  line="$(release_catalog | awk '$1=="asr-gateway" { print $2, $4 }')"
  [[ "$line" == "anila-asr-gateway:latest yes" ]] || {
    echo "asr-gateway 清單是 [$line]" >&2
    return 1
  }
  services_to_start | grep -qx asr-gateway || {
    echo "asr-gateway 沒有預設啟動" >&2
    return 1
  }
}

test_build_release_builds_asr_gateway() {
  local repo stage lines log compose_path
  repo="$tmp/build-repo"
  stage="$tmp/build-stage"
  lines="$tmp/build-lines"
  log="$tmp/build-docker.log"
  mkdir -p "$repo/infra/deployment/scripts" "$stage/images"
  printf 'name: anila\nservices: {}\n' > "$repo/compose.yaml"
  printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "${DOCKER_LOG:?}"\nexit 0\n' \
    > "$repo/infra/deployment/scripts/scan-image-artifacts.sh"
  chmod +x "$repo/infra/deployment/scripts/scan-image-artifacts.sh"
  git init -q "$repo"
  git -C "$repo" add compose.yaml infra/deployment/scripts/scan-image-artifacts.sh
  git -C "$repo" -c user.email=release-test@example.com -c user.name=release-test commit -q -m test
  printf 'secret\n' > "$repo/ignored-secret.txt"
  printf 'ignored-secret.txt\n' > "$repo/.gitignore"
  : > "$log"
  # shellcheck source=../build-release.sh
  source "$ROOT/scripts/release/build-release.sh"
  export DOCKER_LOG="$log"
  assert_release_stubs "$STUB_BIN"
  export STUB_LIVE_ALEMBIC=sha256:abc
  export STUB_BAKE_JSON="$tmp/build-bake.json"
  release_catalog | awk '$2 !~ /@sha256:/ { print $1, $2 }' | python3 -c '
import json, sys
targets = {}
for line in sys.stdin:
    svc, image = line.split()
    targets.setdefault(image, {"tags": [image]})
print(json.dumps({"target": {f"t{i}": t for i, t in enumerate(targets.values())}}))
' > "$STUB_BAKE_JSON"
  release_build_images "$repo" "$stage" "2026.09.29-1" "$lines" || {
    echo "release_build_images 失敗" >&2
    return 1
  }
  awk '/build/ && /asr-gateway/ { found=1 } END { exit found ? 0 : 1 }' "$log" || {
    echo "建置指令沒有 asr-gateway" >&2
    return 1
  }
  compose_path="$(awk '{
    for (i = 1; i <= NF; i++) if ($i == "-f" && $(i + 1) ~ /compose\.yaml$/) { print $(i + 1); exit }
  }' "$log")"
  [[ "$compose_path" == *"/.anila-release-src."* ]] || {
    echo "建置沒有用 HEAD 的乾淨檢出：$compose_path" >&2
    cat "$log" >&2
    return 1
  }
  [[ "$compose_path" != "$repo/compose.yaml" ]] || {
    echo "建置仍用工作目錄" >&2
    return 1
  }
  grep -q '\.tar\.gz' "$log" || {
    echo "沒有逐層掃描封存" >&2
    cat "$log" >&2
    return 1
  }
  # 自建映像走 buildx 直出 tar，不對 daemon 裡的映像 docker save（賽門鐵克 IDS）。
  grep -q '^buildx bake --builder anila-pkg .*--allow fs.read=' "$log" || {
    echo "自建映像沒有用 buildx 直出 tar" >&2
    cat "$log" >&2
    return 1
  }
  if grep -E '^save ' "$log" | grep -v 'anila-bundle/redis:' | grep -q .; then
    echo "自建映像仍用 docker save" >&2
    grep -E '^save ' "$log" >&2
    return 1
  fi
  awk '$1 == "image" && NF == 6 && $4 == "sha256:deadbeef" && $6 == "sha256:cafe0001" { n++ } END { exit n ? 0 : 1 }' "$lines" || {
    echo "映像行沒有同時記設定檔與 manifest 雜湊" >&2
    cat "$lines" >&2
    return 1
  }
  awk '$1=="asr-gateway" { print $4 }' "$ROOT/scripts/release/images.tsv" | grep -qx yes || return 1
}

check "set_env 保留 symlink 並把 state/.env 設成 600" test_set_env_keeps_symlink_and_mode
check "更新前備份目錄 700、檔案 600" test_preupdate_dump_is_private
check "出貨包解在安裝根目錄且離開時清掉" test_open_bundle_extracts_under_install_root_and_cleans
check "還原進乾淨資料庫並核對 alembic" test_restore_swaps_clean_database_and_checks_alembic
check "備份前停寫入並記下備份時間" test_backup_stops_writers_and_records_cutoff
check "手動回復從備份時間計數且稽核在還原之後" test_manual_rollback_uses_backup_time_and_audits_after_swap
check "自動回復在還原之後寫稽核" test_auto_rollback_audits_after_restore
check "更新失敗寫入 operations.log" test_failed_update_is_in_operations_log
check "operations.log 追加且在寫庫成功時也留" test_operations_log_appends_even_when_database_accepts
# 從這份工作樹對 compose 專案 anila 呼叫破壞性步驟時，必須在碰到 docker 之前拒絕。
# 就算 state 裡自己寫了 install_root 與 compose_project=anila 也一樣。
forge_live_project_state() {
  local root="$1" canon
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME=anila
  mkdir -p "$root/state"
  canon="$(cd "$root" && pwd -P)"
  state_set install_root "$canon"
  state_set compose_project anila
}

expect_no_docker() {
  local err="$1" log="$2"
  shift 2
  : > "$log"
  export DOCKER_LOG="$log"
  local rc=0
  (
    trap - EXIT
    set -euo pipefail
    "$@"
  ) >"$tmp/refuse.out" 2>"$err" || rc=$?
  if [[ -s "$log" ]]; then
    echo "拒絕之前就呼叫了 docker" >&2
    cat "$log" >&2
    return 1
  fi
  if [[ "$rc" -eq 0 ]]; then
    echo "應該拒絕，卻成功了" >&2
    return 1
  fi
  grep -q '拒絕操作' "$err" || {
    echo "拒絕訊息不對" >&2
    cat "$err" >&2
    return 1
  }
}

test_worktree_refuses_stop_on_live_project() {
  local root="$tmp/refuse-stop" tree="$tmp/refuse-stop-tree"
  mkdir -p "$root" "$tree"
  printf 'name: anila\n' > "$tree/compose.yaml"
  forge_live_project_state "$root"
  expect_no_docker "$tmp/refuse-stop.err" "$tmp/refuse-stop.log" stop_writers "$tree"
}

test_worktree_refuses_backup_on_live_project() {
  local root="$tmp/refuse-backup" tree="$tmp/refuse-backup-tree" dest
  mkdir -p "$root" "$tree/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" "$tree/infra/deployment/scripts/backup-lib.sh"
  printf 'name: anila\n' > "$tree/compose.yaml"
  dest="$tmp/refuse-backup-dest/db.dump"
  forge_live_project_state "$root"
  export STUB_LIVE_ALEMBIC=r1_0062
  expect_no_docker "$tmp/refuse-backup.err" "$tmp/refuse-backup.log" \
    backup_for_update "$tree" "$dest"
}

test_worktree_refuses_restore_on_live_project() {
  local root="$tmp/refuse-restore" tree="$tmp/refuse-restore-tree" dump
  mkdir -p "$root" "$tree"
  printf 'name: anila\n' > "$tree/compose.yaml"
  dump="$root/db.dump"
  printf 'PGDMP\n' > "$dump"
  printf 'r1_0062\n' > "${dump}.alembic"
  forge_live_project_state "$root"
  export STUB_RESTORE_ALEMBIC=r1_0062
  expect_no_docker "$tmp/refuse-restore.err" "$tmp/refuse-restore.log" \
    restore_dump "$tree" "$dump"
}

test_worktree_refuses_rollback_on_live_project() {
  local root="$tmp/refuse-rollback" ps="$tmp/refuse-rollback-ps"
  layout_rollback_install "$root"
  write_ps_file "$ps"
  forge_live_project_state "$root"
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  export DOCKER_PS_FILE="$ps"
  export STUB_RESTORE_ALEMBIC=r1_0062
  export STUB_COUNTS='4|5|6'
  expect_no_docker "$tmp/refuse-rollback.err" "$tmp/refuse-rollback.log" cmd_rollback
}

test_unset_install_root_refuses_live_project() {
  local tree="$tmp/refuse-default-tree"
  mkdir -p "$tree"
  printf 'name: anila\n' > "$tree/compose.yaml"
  unset ANILA_INSTALL_ROOT
  export COMPOSE_PROJECT_NAME=anila
  expect_no_docker "$tmp/refuse-default.err" "$tmp/refuse-default.log" stop_writers "$tree"
  [[ ! -e /opt/anila ]] || { echo "拒絕過程動到了 /opt/anila" >&2; return 1; }
}

test_unrecorded_install_refuses_stop() {
  local root="$tmp/refuse-unrecorded" tree="$tmp/refuse-unrecorded-tree"
  mkdir -p "$root" "$tree"
  printf 'name: anila\n' > "$tree/compose.yaml"
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  expect_no_docker "$tmp/refuse-unrecorded.err" "$tmp/refuse-unrecorded.log" \
    stop_writers "$tree"
}

test_project_mismatch_refuses_stop() {
  local root="$tmp/refuse-mismatch" tree="$tmp/refuse-mismatch-tree"
  mkdir -p "$root" "$tree"
  printf 'name: anila\n' > "$tree/compose.yaml"
  arm_test_install "$root"
  export COMPOSE_PROJECT_NAME=anila-other
  expect_no_docker "$tmp/refuse-mismatch.err" "$tmp/refuse-mismatch.log" \
    stop_writers "$tree"
}

test_recorded_project_is_what_docker_stops() {
  local root="$tmp/allow-root" tree log got
  mkdir -p "$root"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  log="$tmp/allow.log"
  : > "$log"
  export DOCKER_LOG="$log"
  stop_writers "$tree" || { echo "已記錄的測試專案不該被拒絕" >&2; return 1; }
  got="$(awk '{
    for (i = 1; i <= NF; i++) if ($i == "-p") { print $(i + 1); exit }
  }' "$log")"
  [[ "$got" == "$TEST_PROJECT" ]] || {
    echo "docker 收到的專案是 ${got:-（沒有）}，不是記錄的 $TEST_PROJECT" >&2
    cat "$log" >&2
    return 1
  }
}

make_min_bundle() {
  local dest="$1" ver="$2" lines svc image archive start digest
  mkdir -p "$dest/images"
  lines="$(mktemp "${HOME}/.anila-image-lines.XXXXXX")"
  while read -r svc image archive start; do
    printf 'img\n' > "$dest/images/${archive}.tar.gz"
    digest="$(printf '%s' "$svc" | sha256sum | awk '{print $1}')"
    printf 'image %s %s sha256:%s images/%s.tar.gz\n' \
      "$svc" "$image" "$digest" "$archive" >> "$lines"
  done < <(release_catalog)
  printf 'name: anila\n' > "$dest/compose.yaml"
  tar -C "$dest" -czf "$dest/source.tar.gz" compose.yaml
  rm -f "$dest/compose.yaml"
  cp "$ROOT/scripts/release/images.tsv" "$dest/images.tsv"
  release_seal_manifest "$dest" "$ver" "abc123" "$lines"
  rm -f "$lines"
}

# Docker 29 新裝預設 containerd 儲存：載入後的映像 ID 是 manifest 雜湊，不是設定檔雜湊。
# 2026-09-30 演練機 .35 實測。第四欄對不上時要改用第六欄；兩個都對不上就停。
test_load_accepts_containerd_manifest_digest() {
  local root bundle log svc image archive start cfg man missing="" want
  root="$tmp/ctrd-root"
  bundle="$tmp/ctrd-bundle"
  log="$tmp/ctrd.log"
  arm_test_install "$root"
  use_version_tree "$root" >/dev/null
  mkdir -p "$bundle/images"
  : > "$bundle/manifest.txt"
  while read -r svc image archive start; do
    printf 'img\n' | gzip -c > "$bundle/images/${archive}.tar.gz"
    cfg="sha256:$(printf 'cfg-%s' "$svc" | sha256sum | awk '{print $1}')"
    man="sha256:$(printf 'man-%s' "$svc" | sha256sum | awk '{print $1}')"
    missing+=" $cfg"
    printf 'image %s %s %s images/%s.tar.gz %s\n' "$svc" "$image" "$cfg" "$archive" "$man" \
      >> "$bundle/manifest.txt"
  done < <(release_catalog)
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_MISSING_IDS="$missing"
  if ! ( load_bundle_images "$bundle" 2026.09.30-1 ) >/dev/null 2>&1; then
    unset STUB_MISSING_IDS
    echo "containerd 儲存下 manifest 雜湊沒被接受" >&2
    return 1
  fi
  want="sha256:$(printf 'man-csp' | sha256sum | awk '{print $1}')"
  grep -q "^tag $want " "$log" || {
    unset STUB_MISSING_IDS
    echo "沒有用 manifest 雜湊上標籤" >&2
    cat "$log" >&2
    return 1
  }
  sed -i 's/ sha256:[0-9a-f]*$/ sha256:0000/' "$bundle/manifest.txt"
  export STUB_MISSING_IDS="$missing sha256:0000"
  if ( load_bundle_images "$bundle" 2026.09.30-1 ) >/dev/null 2>&1; then
    unset STUB_MISSING_IDS
    echo "兩個雜湊都對不上仍然載入" >&2
    return 1
  fi
  unset STUB_MISSING_IDS
}

# 已有上一版、資料庫沒起來：不能當成第一次安裝，也不能先改目錄或載入映像。
test_previous_version_with_db_down_aborts_before_changes() {
  local root ver bundle log err
  root="$tmp/dbdown-root"
  ver="2026.09.29-3"
  bundle="$tmp/dbdown-bundle"
  log="$tmp/dbdown.log"
  err="$tmp/dbdown.err"
  mkdir -p "$root/versions/2026.09.29-2"
  printf 'name: anila\n' > "$root/versions/2026.09.29-2/compose.yaml"
  ln -sfn versions/2026.09.29-2 "$root/current"
  mkdir -p "$root/state"
  printf 'current=2026.09.29-2\n' > "$root/state/release.state"
  arm_test_install "$root"
  make_min_bundle "$bundle" "$ver"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_DB_DOWN=1
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/dbdown.out" 2>"$err"; then
    echo "資料庫沒起來仍繼續更新" >&2
    return 1
  fi
  grep -q '資料庫' "$err" || { echo "沒有說明資料庫不可用"; cat "$err" >&2; return 1; }
  if grep -q '略過公告' "$err"; then
    echo "把已安裝、資料庫沒起來當成第一次安裝" >&2
    return 1
  fi
  [[ ! -d "$root/versions/$ver" ]] || { echo "已經建立新版本目錄"; return 1; }
  if grep -E '(^| )(load|stop)( |$)' "$log"; then
    echo "拒絕之前就載入或停服務" >&2
    cat "$log" >&2
    return 1
  fi
  grep -E $'\tupdate\t.*\tfailure$' "$root/state/operations.log" >/dev/null || {
    echo "早期失敗沒寫 operations.log" >&2
    return 1
  }
}

# 專案 anila 不能靠版本目錄裡的 state 取得許可。第一次安裝必須解在 /opt/anila。
test_first_install_outside_opt_refuses_before_docker() {
  local root="$tmp/first-root"
  mkdir -p "$root"
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME=anila
  expect_no_docker "$tmp/first.err" "$tmp/first.log" \
    cmd_update "$tmp/anila-2026.09.29-1.tar.gz"
  grep -q 'tar -xzf' "$tmp/first.err" || {
    echo "沒有給出解到 /opt/anila 的指令" >&2
    cat "$tmp/first.err" >&2
    return 1
  }
  grep -q -- '-C /opt/anila' "$tmp/first.err" || return 1
  [[ ! -e /opt/anila ]] || { echo "拒絕過程建立了 /opt/anila" >&2; return 1; }
  [[ ! -d "$root/versions" ]] || { echo "拒絕之前就建立版本目錄" >&2; return 1; }
  grep -E $'\tupdate\t.*\tfailure$' "$root/state/operations.log" >/dev/null || {
    echo "早期失敗沒寫 operations.log" >&2
    return 1
  }
}

test_fake_anila_root_refuses_before_image_tag() {
  local root="$tmp/fake-tag-root"
  mkdir -p "$root"
  forge_live_project_state "$root"
  expect_no_docker "$tmp/fake-tag.err" "$tmp/fake-tag.log" \
    tag_running_as_version "2026.09.29-1"
}

# 停寫入之後備份失敗，要把上一版拉起來，而且不能還原資料庫。
test_backup_failure_restarts_previous_version() {
  local root old bundle log err stop_at up_at
  root="$tmp/restart-root"
  old="2026.09.29-2"
  bundle="$tmp/restart-bundle"
  log="$tmp/restart.log"
  err="$tmp/restart.err"
  mkdir -p "$root/versions/$old/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" \
    "$root/versions/$old/infra/deployment/scripts/backup-lib.sh"
  printf 'name: anila\n' > "$root/versions/$old/compose.yaml"
  ln -sfn "versions/$old" "$root/current"
  mkdir -p "$root/state"
  printf 'current=%s\n' "$old" > "$root/state/release.state"
  arm_test_install "$root"
  make_min_bundle "$bundle" "2026.09.29-3"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_BANNER_COUNT=1
  export STUB_DUMP_FAIL=1
  export STUB_LIVE_ALEMBIC=r1_0062
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/restart.out" 2>"$err"; then
    echo "備份失敗仍當作更新成功" >&2
    return 1
  fi
  stop_at="$(awk '
    {
      stopat = 0
      for (i = 1; i <= NF; i++) if ($i == "stop") stopat = i
      if (stopat == 0) next
      for (i = stopat + 1; i <= NF; i++) if ($i == "csp") { print NR; exit }
    }
  ' "$log")"
  up_at="$(first_line "$log" 'up -d')"
  [[ -n "$stop_at" && -n "$up_at" && "$stop_at" -lt "$up_at" ]] || {
    echo "備份失敗後沒有把上一版拉起來 stop=$stop_at up=$up_at" >&2
    cat "$err" >&2
    cat "$log" >&2
    return 1
  }
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "備份失敗卻還原了資料庫" >&2
    return 1
  fi
  grep -q 'INSERT INTO audit_logs' "$log" || {
    echo "備份失敗沒寫資料庫稽核" >&2
    cat "$log" >&2
    return 1
  }
}

test_cancel_rollback_restarts_after_counting() {
  local root log err stop_at count_at up_at
  root="$tmp/cancel-root"
  log="$tmp/cancel.log"
  err="$tmp/cancel.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_COUNTS='4|5|6'
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; printf '%s\n' 'nope' | cmd_rollback ) >"$tmp/cancel.out" 2>"$err"; then
    echo "打錯版本仍回復" >&2
    return 1
  fi
  stop_at="$(awk '
    {
      stopat = 0
      for (i = 1; i <= NF; i++) if ($i == "stop") stopat = i
      if (stopat == 0) next
      for (i = stopat + 1; i <= NF; i++) if ($i == "csp") { print NR; exit }
    }
  ' "$log")"
  count_at="$(first_line "$log" 'created_at')"
  up_at="$(first_line "$log" 'up -d')"
  [[ -n "$stop_at" && -n "$count_at" && -n "$up_at" \
    && "$stop_at" -lt "$count_at" && "$count_at" -lt "$up_at" ]] || {
    echo "取消回復的順序不對 stop=$stop_at count=$count_at up=$up_at" >&2
    cat "$err" >&2
    return 1
  }
  if awk '$0 ~ /DROP DATABASE|RENAME TO csp/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "取消回復仍改了資料庫" >&2
    return 1
  fi
  grep -q '已取消' "$err" || { echo "沒有說已取消"; cat "$err" >&2; return 1; }
  grep -q 'INSERT INTO audit_logs' "$log" || {
    echo "打錯版本沒寫資料庫稽核" >&2
    cat "$log" >&2
    return 1
  }
}

test_rollback_confirm_is_typed_not_an_environment_variable() {
  if grep -q 'ANILA_ROLLBACK_CONFIRM' "$ROOT/scripts/release/anila-update.sh"; then
    echo "仍可用環境變數跳過打字確認" >&2
    return 1
  fi
}

test_preupdate_hardlink_snapshot_restores_with_the_database() {
  local root tree dest live snap inode_live inode_snap
  root="$tmp/snap-root"
  mkdir -p "$root/state/share/uploads" "$root/state/share/attachments" \
    "$root/state/share/static" "$root/state/share/studio-artifacts"
  printf 'SECRET\n' > "$root/state/share/uploads/kb.txt"
  printf 'ATTACH\n' > "$root/state/share/attachments/gen.md"
  printf 'ART\n' > "$root/state/share/studio-artifacts/deck.bin"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  mkdir -p "$tree/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" \
    "$tree/infra/deployment/scripts/backup-lib.sh"
  dest="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  export DOCKER_LOG="$tmp/snap.log"
  : > "$DOCKER_LOG"
  export STUB_LIVE_ALEMBIC=r1_0062
  unset ANILA_PG_DUMP_CMD || true
  backup_for_update "$tree" "$dest" >/dev/null || { echo "備份失敗" >&2; return 1; }
  snap="$(dirname "$dest")/files/uploads/kb.txt"
  [[ -f "$snap" ]] || { echo "沒有快照上傳檔"; return 1; }
  live="$root/state/share/uploads/kb.txt"
  inode_live="$(stat -c %i "$live")"
  inode_snap="$(stat -c %i "$snap")"
  [[ "$inode_live" == "$inode_snap" ]] || {
    echo "快照不是硬連結 live=$inode_live snap=$inode_snap" >&2
    return 1
  }
  rm -f "$live"
  printf 'CHANGED\n' > "$live"
  restore_share_snapshot "$dest" || { echo "還原檔案失敗" >&2; return 1; }
  [[ "$(tr -d '[:space:]' < "$live")" == "SECRET" ]] || {
    echo "回復沒有把上傳檔一起還原" >&2
    return 1
  }
  mkdir -p "$root/state/share/backups/pre-update/2026.09.29-1" \
    "$root/state/share/backups/pre-update/2026.09.29-2" \
    "$root/state/share/backups/pre-update/2026.09.29-3"
  prune_old_preupdate_backups "$root" "2026.09.29-2" "2026.09.29-3"
  [[ -d "$root/state/share/backups/pre-update/2026.09.29-2" ]] || return 1
  [[ -d "$root/state/share/backups/pre-update/2026.09.29-3" ]] || return 1
  [[ ! -d "$root/state/share/backups/pre-update/2026.09.29-1" ]] || {
    echo "預更新備份沒有只留最近兩份" >&2
    return 1
  }
}

test_db_audit_is_psql_and_pending_retries() {
  local root tree log
  root="$tmp/pending-root"
  log="$tmp/pending.log"
  mkdir -p "$root/state"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_AUDIT_FAIL=1
  record_audit "$tree" update 2026.09.29-1 2026.09.29-2 failure || true
  [[ "$(state_get db_audit_pending)" == "1" ]] || {
    echo "資料庫稽核失敗沒有標成待寫" >&2
    return 1
  }
  [[ -s "$root/state/db-audit-pending" ]] || { echo "沒有待寫檔"; return 1; }
  [[ "$(stat -c %a "$root/state/db-audit-pending")" == "600" ]] || return 1
  export STUB_AUDIT_FAIL=0
  : > "$log"
  retry_pending_db_audits "$tree" || { echo "下一輪沒有補寫稽核" >&2; return 1; }
  [[ "$(state_get db_audit_pending)" != "1" ]] || { echo "補寫成功仍標成待寫" >&2; return 1; }
  grep -q 'INSERT INTO audit_logs' "$log" || { echo "不是直接寫 audit_logs"; cat "$log" >&2; return 1; }
  grep -q 'csp-db' "$log" || { echo "沒有對 csp-db 下 psql"; return 1; }
  if grep -q 'exec -T csp python' "$log"; then
    echo "仍透過 csp 容器寫稽核" >&2
    return 1
  fi
}

test_auto_rollback_retags_before_missing_tree() {
  local root log
  root="$tmp/retag-root"
  log="$tmp/retag.log"
  mkdir -p "$root"
  arm_test_install "$root"
  : > "$log"
  export DOCKER_LOG="$log"
  if ( trap - EXIT; auto_rollback "$root" "2026.09.29-1" "2026.09.29-2" "" "" ); then
    echo "上一版目錄不在仍當作回復成功" >&2
    return 1
  fi
  grep -Eq '(^| )tag ' "$log" || {
    echo "還沒確認版本目錄就沒有把映像標回上一版" >&2
    cat "$log" >&2
    return 1
  }
  grep -q $'\trollback\t2026.09.29-2\t2026.09.29-1\tfailure' "$root/state/operations.log" || {
    echo "拒絕回復沒有寫失敗紀錄" >&2
    cat "$root/state/operations.log" >&2
    return 1
  }
}

test_optional_service_commands_name_compose_file_and_project() {
  local script="$ROOT/scripts/release/anila-update.sh"
  local doc="$ROOT/docs/deploy/UPDATE.md"
  grep -q 'current/compose.yaml' "$script" || { echo "啟動指令沒有 compose 檔"; return 1; }
  grep -q 'compose_project' "$script" || return 1
  grep -q 'current/compose.yaml' "$doc" || { echo "UPDATE.md 的指令沒有 compose 檔"; return 1; }
  grep -q '這包不含文件解析服務' "$doc" || { echo "沒有說明文件解析不在出貨包"; return 1; }
  grep -q '硬連結' "$doc" || { echo "沒有說明快照的磁碟影響"; return 1; }
  grep -q -- '--profile maint' "$script" || { echo "啟動指令沒有 maint profile"; return 1; }
  grep -q -- '--profile maint' "$doc" || { echo "UPDATE.md 沒有 maint profile"; return 1; }
}

test_dev_secret_flag_is_warned_then_refused() {
  local tree err
  tree="$tmp/envwarn"
  err="$tmp/envwarn.err"
  mkdir -p "$tree"
  cat > "$tree/.env" <<'EOF'
ANILA_HOST=lab
SECRET_KEY=k
ADMIN_PASSWORD=p
CSP_DB_PASSWORD=d
CSP_APP_DB_PASSWORD=a
CODESERVER_PASSWORD=c
ANILA_ALLOW_DEV_SECRET=1
EOF
  chmod 600 "$tree/.env"
  ensure_platform_env "$tree" >"$tmp/envwarn.out" 2>"$err" || true
  grep -q 'ANILA_ALLOW_DEV_SECRET' "$err" || {
    echo "開發旗標沒有警告" >&2
    cat "$err" >&2
    return 1
  }
  # shellcheck source=../../../infra/deployment/scripts/prod-env-guard.sh
  source "$ROOT/infra/deployment/scripts/prod-env-guard.sh"
  if prod_env_refuse "$tree/.env"; then
    echo "正式部署沒有拒絕開發旗標" >&2
    return 1
  fi
}

test_redis_image_is_pinned_by_digest() {
  local pin='redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499'
  grep -q "$pin" "$ROOT/scripts/release/build-release.sh" || {
    echo "build-release.sh 沒有把 redis 釘在 digest" >&2
    return 1
  }
  grep -q "$pin" "$ROOT/scripts/release/images.tsv" || return 1
  grep -q 'image: redis:7-alpine$' "$ROOT/infra/compose/platform.yml" || {
    echo "主機 compose 沒有用 redis:7-alpine 這個標籤" >&2
    return 1
  }
  if grep -q 'redis:7-alpine@sha256' "$ROOT/infra/compose/platform.yml"; then
    echo "主機 compose 仍用 manifest-list digest，load 之後對不到" >&2
    return 1
  fi
}

test_manifest_rejects_traversal_and_unlisted_files() {
  local bundle="$tmp/man-bundle" mode
  mkdir -p "$bundle"
  printf 'ok\n' > "$bundle/foo..bar"
  printf 'src\n' > "$bundle/source.tar.gz"
  umask 077
  release_seal_manifest "$bundle" "2026.09.29-1" "abc123"
  mode="$(stat -c %a "$bundle/manifest.sha256")"
  [[ "$mode" == "644" ]] || { echo "manifest.sha256 權限 $mode" >&2; return 1; }
  release_verify_bundle "$bundle" || { echo "foo..bar 被當成路徑穿越"; return 1; }
  printf 'extra\n' > "$bundle/extra.txt"
  if release_verify_bundle "$bundle"; then
    echo "清單沒列的檔案仍通過" >&2
    return 1
  fi
  rm -f "$bundle/extra.txt"
  release_seal_manifest "$bundle" "2026.09.29-1" "abc123"
  printf 'file deadbeef nested/../../etc/passwd\n' >> "$bundle/manifest.txt"
  ( cd "$bundle" && sha256sum manifest.txt > manifest.sha256 )
  chmod 644 "$bundle/manifest.sha256"
  if release_verify_bundle "$bundle"; then
    echo "路徑穿越仍通過" >&2
    return 1
  fi
}

test_secrets_scan_rejects_env_keys_and_private_key_blocks() {
  local root src
  root="$tmp/secrets-ok"
  mkdir -p "$root"
  printf 'X=\n' > "$root/.env.example"
  printf 'cert\n' > "$root/cspki_ca_bundle.pem"
  release_assert_no_secrets "$root" || { echo "公開範例與 CA 不該被拒絕"; return 1; }
  root="$tmp/secrets-env"
  mkdir -p "$root"
  printf 'X=1\n' > "$root/.env.local"
  if release_assert_no_secrets "$root"; then
    echo ".env.local 沒被拒絕" >&2
    return 1
  fi
  root="$tmp/secrets-pem"
  mkdir -p "$root"
  printf 'x\n' > "$root/bad.pem"
  if release_assert_no_secrets "$root"; then
    echo "bad.pem 沒被拒絕" >&2
    return 1
  fi
  root="$tmp/secrets-nested"
  mkdir -p "$root/nested/secrets"
  printf 't\n' > "$root/nested/secrets/token"
  if release_assert_no_secrets "$root"; then
    echo "巢狀 secrets 沒被拒絕" >&2
    return 1
  fi
  root="$tmp/secrets-key"
  src="$tmp/secrets-key-src"
  mkdir -p "$root" "$src"
  printf '%s\n' '-----BEGIN PRIVATE KEY-----' 'abc' > "$src/note.txt"
  tar -C "$src" -czf "$root/source.tar.gz" note.txt
  if release_assert_no_secrets "$root"; then
    echo "封存裡的 PRIVATE KEY 沒被拒絕" >&2
    return 1
  fi
  root="$tmp/secrets-placeholder"
  src="$tmp/secrets-placeholder-src"
  mkdir -p "$root/services/csp/secrets" "$src/services/csp/secrets"
  printf '*\n!.gitignore\n' > "$root/services/csp/secrets/.gitignore"
  printf 'keep\n' > "$root/services/csp/secrets/.gitkeep"
  printf 'readme\n' > "$root/services/csp/secrets/README"
  release_assert_no_secrets "$root" || { echo "services/csp/secrets/.gitignore 不該被拒絕"; return 1; }
  printf 't\n' > "$root/services/csp/secrets/token"
  if release_assert_no_secrets "$root"; then
    echo "services/csp/secrets/token 沒被拒絕" >&2
    return 1
  fi
  printf '*\n!.gitignore\n' > "$src/services/csp/secrets/.gitignore"
  mkdir -p "$tmp/secrets-tar-ok"
  tar -C "$src" -czf "$tmp/secrets-tar-ok/source.tar.gz" services/csp/secrets/.gitignore
  release_assert_no_secrets "$tmp/secrets-tar-ok" || {
    echo "封存裡的 services/csp/secrets/.gitignore 不該被拒絕" >&2
    return 1
  }
  printf 't\n' > "$src/services/csp/secrets/token"
  tar -C "$src" -czf "$tmp/secrets-tar-ok/source.tar.gz" services/csp/secrets/.gitignore services/csp/secrets/token
  if release_assert_no_secrets "$tmp/secrets-tar-ok"; then
    echo "封存裡的 services/csp/secrets/token 沒被拒絕" >&2
    return 1
  fi
}

test_existing_stack_is_not_a_first_install() {
  local root bundle log err
  root="$tmp/stack-root"
  bundle="$tmp/stack-bundle"
  log="$tmp/stack.log"
  err="$tmp/stack.err"
  mkdir -p "$root"
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  rm -f "$root/state/release.state"
  make_min_bundle "$bundle" "2026.09.29-1"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_EXISTING_STACK=1
  export STUB_EXISTING_VOLUME=0
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/stack.out" 2>"$err"; then
    echo "已有 compose 專案仍當成第一次安裝" >&2
    return 1
  fi
  grep -q '不是第一次安裝' "$err" || { echo "沒有說明這不是第一次安裝"; cat "$err" >&2; return 1; }
  grep -q 'adopt' "$err" || { echo "沒有說明怎麼認領"; cat "$err" >&2; return 1; }
  grep -q 'compose ls' "$log" || { echo "沒有查 compose 專案"; cat "$log" >&2; return 1; }
  if grep -E '(^| )(load|stop)( |$)' "$log"; then
    echo "拒絕之前就載入或停服務" >&2
    cat "$log" >&2
    return 1
  fi
  [[ ! -d "$root/versions" ]] || { echo "拒絕之前就建立版本目錄"; return 1; }
  unset STUB_EXISTING_STACK
  : > "$log"
  export STUB_EXISTING_VOLUME=1
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/stack-vol.out" 2>"$err"; then
    echo "已有 volume 仍當成第一次安裝" >&2
    return 1
  fi
  grep -q 'adopt' "$err" || { echo "volume 已存在時沒有說明認領"; cat "$err" >&2; return 1; }
  grep -q 'volume ls' "$log" || { echo "沒有查 volume"; cat "$log" >&2; return 1; }
  unset STUB_EXISTING_VOLUME
}

test_adopt_backs_up_before_writing_state() {
  local root tree log err
  root="$tmp/adopt-root"
  log="$tmp/adopt.log"
  err="$tmp/adopt.err"
  mkdir -p "$root"
  export ANILA_INSTALL_ROOT="$root"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  rm -f "$root/state/release.state"
  tree="$root/versions/2026.09.29-1"
  mkdir -p "$tree/infra/deployment/scripts"
  printf 'name: anila\n' > "$tree/compose.yaml"
  printf '%s\n' \
    'ANILA_HOST=legacy-host' \
    'SECRET_KEY=keep-me' \
    'ADMIN_PASSWORD=keep-admin' \
    'CSP_DB_PASSWORD=keep-db' \
    'CSP_APP_DB_PASSWORD=keep-app' \
    'CODESERVER_PASSWORD=keep-code' \
    > "$tree/.env"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" \
    "$tree/infra/deployment/scripts/backup-lib.sh"
  cp "$ROOT/infra/deployment/scripts/prod-env-guard.sh" \
    "$tree/infra/deployment/scripts/prod-env-guard.sh"
  cp "$ROOT/infra/deployment/scripts/fix-runtime-ownership.sh" \
    "$tree/infra/deployment/scripts/fix-runtime-ownership.sh"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_EXISTING_STACK=1
  export STUB_LIVE_ALEMBIC=r1_0062
  export STUB_DUMP_FAIL=1
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; cmd_adopt "$tree" ) >"$tmp/adopt-fail.out" 2>"$err"; then
    echo "備份失敗仍當作認領成功" >&2
    return 1
  fi
  if [[ -f "$root/state/release.state" ]] && grep -q '^compose_project=' "$root/state/release.state"; then
    echo "備份失敗仍寫了安裝記錄" >&2
    cat "$root/state/release.state" >&2
    return 1
  fi
  unset STUB_DUMP_FAIL
  write_ps_file "$tmp/adopt-ps"
  export DOCKER_PS_FILE="$tmp/adopt-ps"
  : > "$log"
  if ! ( trap - EXIT; cmd_adopt "$tree" ) >"$tmp/adopt.out" 2>"$err"; then
    echo "認領失敗" >&2
    cat "$err" >&2
    return 1
  fi
  grep -q '^compose_project='"$TEST_PROJECT"'$' "$root/state/release.state" || {
    echo "認領沒寫 compose 專案" >&2
    cat "$root/state/release.state" >&2
    return 1
  }
  grep -q '^current=2026.09.29-1$' "$root/state/release.state" || {
    echo "認領沒寫目前版本" >&2
    return 1
  }
  grep -q 'pg_dump' "$log" || { echo "認領沒有先備份"; cat "$log" >&2; return 1; }
  grep -q '^ANILA_HOST=legacy-host$' "$root/state/.env" || {
    echo "沒有把舊 .env 收進 state/.env" >&2
    cat "$root/state/.env" >&2
    return 1
  }
  grep -q '^SECRET_KEY=keep-me$' "$root/state/.env" || {
    echo "舊的密鑰被空檔蓋掉" >&2
    return 1
  }
  [[ -L "$tree/.env" ]] || { echo "版本目錄的 .env 不是連到 state"; return 1; }
  grep -q $'\tadopt\tnone\t2026.09.29-1\tsuccess' "$root/state/operations.log" || {
    echo "認領成功沒寫 operations.log" >&2
    cat "$root/state/operations.log" >&2
    return 1
  }
  grep -q 'platform_adopt' "$log" || { echo "認領成功沒寫資料庫稽核"; cat "$log" >&2; return 1; }
  local dump_at prep_at up_at
  dump_at="$(first_line "$log" 'pg_dump')"
  prep_at="$(first_line "$log" 'network ')"
  up_at="$(first_line "$log" 'up -d')"
  [[ -n "$dump_at" && -n "$prep_at" && -n "$up_at" && "$dump_at" -lt "$prep_at" && "$prep_at" -lt "$up_at" ]] || {
    echo "認領沒有先備份、再準備目錄、才啟動 dump=$dump_at prep=$prep_at up=$up_at" >&2
    cat "$log" >&2
    return 1
  }
  unset STUB_EXISTING_STACK
}

test_compose_directory_must_match_state() {
  local root foreign log
  root="$tmp/dc-root"
  foreign="$tmp/dc-foreign"
  log="$tmp/dc.log"
  mkdir -p "$root" "$foreign"
  printf 'name: anila\n' > "$foreign/compose.yaml"
  arm_test_install "$root"
  use_version_tree "$root" "2026.09.29-1" >/dev/null
  : > "$log"
  export DOCKER_LOG="$log"
  if ( trap - EXIT; stop_writers "$foreign" ) >"$tmp/dc.out" 2>"$tmp/dc.err"; then
    echo "安裝根目錄外面的 compose 目錄仍被接受" >&2
    return 1
  fi
  grep -q '拒絕操作' "$tmp/dc.err" || { echo "拒絕訊息不對"; cat "$tmp/dc.err" >&2; return 1; }
  [[ ! -s "$log" ]] || { echo "拒絕之前就呼叫了 docker"; cat "$log" >&2; return 1; }
  mkdir -p "$tmp/dc-outside"
  printf 'name: anila\n' > "$tmp/dc-outside/compose.yaml"
  ln -sfn "$tmp/dc-outside" "$root/versions/2026.09.29-9"
  state_set current "2026.09.29-9"
  if ( trap - EXIT; stop_writers "$root/versions/2026.09.29-9" ) >"$tmp/dc-link.out" 2>"$tmp/dc-link.err"; then
    echo "指到外面的版本目錄仍被接受" >&2
    return 1
  fi
  state_set current "2026.09.29-1"
  ln -sfn "versions/2026.09.29-1" "$root/current"
  mkdir -p "$root/versions/2026.09.29-2"
  printf 'name: anila\n' > "$root/versions/2026.09.29-2/compose.yaml"
  : > "$log"
  _ops_to="2026.09.29-2"
  stop_writers "$root/versions/2026.09.29-2" || {
    echo "正在安裝的版本目錄不該被拒絕" >&2
    cat "$log" >&2
    return 1
  }
  grep -q -- "-p ${TEST_PROJECT}" "$log" || { echo "允許的目錄沒有送到 docker"; return 1; }
  _ops_to=""
}

test_entry_checks_manifest_before_sourcing() {
  local bundle err
  bundle="$tmp/entry-bundle"
  err="$tmp/entry.err"
  make_min_bundle "$bundle" "2026.09.29-1"
  printf 'tamper\n' >> "$bundle/manifest.txt"
  : > "$tmp/entry-docker.log"
  export DOCKER_LOG="$tmp/entry-docker.log"
  export ANILA_INSTALL_ROOT="$tmp/entry-root"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  mkdir -p "$tmp/entry-root"
  if ( trap - EXIT; bash "$ROOT/scripts/release/anila-update.sh" "$bundle" ) >"$tmp/entry.out" 2>"$err"; then
    echo "清單被改過仍繼續" >&2
    return 1
  fi
  grep -q 'SHA256' "$err" || { echo "沒有在進入腳本前核對 SHA256"; cat "$err" >&2; return 1; }
  [[ ! -s "$tmp/entry-docker.log" ]] || {
    echo "核對清單之前就呼叫了 docker" >&2
    cat "$tmp/entry-docker.log" >&2
    return 1
  }
  awk '
    /sha256sum -c manifest.sha256/ { seen=1 }
    seen && /source "\$HERE\/release-lib.sh"/ { found=1 }
    END { exit found ? 0 : 1 }
  ' "$ROOT/scripts/release/anila-update.sh" || {
    echo "sha256sum -c 沒有排在 source release-lib.sh 之前" >&2
    return 1
  }
  awk '
    /tar -C "\$out_dir" -czf "\$archive_tar"/ { seen=1 }
    seen && /sha256sum "\$archive_tar"/ { found=1 }
    END { exit found ? 0 : 1 }
  ' "$ROOT/scripts/release/build-release.sh" || {
    echo "打包結束沒有印出壓縮檔的 SHA256" >&2
    return 1
  }
}

test_auto_rollback_restore_failure_keeps_live_db() {
  local root log err dump restore_at up_at
  root="$tmp/restore-fail-root"
  log="$tmp/restore-fail.log"
  err="$tmp/restore-fail.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_LIVE_ALEMBIC=r1_0099
  export STUB_RESTORE_FAIL=1
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; auto_rollback "$root" "2026.09.29-1" "2026.09.29-2" "r1_0062" "$dump" ) \
    >"$tmp/restore-fail.out" 2>"$err"; then
    echo "還原失敗仍當作自動回復成功" >&2
    cat "$err" >&2
    return 1
  fi
  grep -q '沒有被換掉' "$err" || { echo "沒有說明線上資料庫還在"; cat "$err" >&2; return 1; }
  grep -q '原本的資料庫還在' "$err" || { echo "還原失敗的訊息不清楚"; cat "$err" >&2; return 1; }
  restore_at="$(first_line "$log" 'pg_restore')"
  up_at="$(first_line "$log" 'up -d')"
  [[ -n "$restore_at" && -n "$up_at" && "$restore_at" -lt "$up_at" ]] || {
    echo "還原失敗後沒有把上一版拉起來 restore=$restore_at up=$up_at" >&2
    cat "$log" >&2
    return 1
  }
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "暫存還原失敗仍刪掉線上資料庫" >&2
    return 1
  fi
  grep -q 'platform_rollback' "$log" || {
    echo "還原失敗沒寫回復稽核" >&2
    cat "$log" >&2
    return 1
  }
  grep -q 'platform_update' "$log" || {
    echo "還原失敗沒有同時寫更新失敗稽核" >&2
    cat "$log" >&2
    return 1
  }
  unset STUB_RESTORE_FAIL
}

test_image_catalog_rejects_missing_and_duplicate_before_stop() {
  local root bundle log err line
  root="$tmp/catalog-root"
  bundle="$tmp/catalog-bundle"
  log="$tmp/catalog.log"
  err="$tmp/catalog.err"
  mkdir -p "$root/versions/2026.09.29-2"
  printf 'name: anila\n' > "$root/versions/2026.09.29-2/compose.yaml"
  ln -sfn versions/2026.09.29-2 "$root/current"
  arm_test_install "$root"
  state_set current "2026.09.29-2"
  make_min_bundle "$bundle" "2026.09.29-4"
  grep -v '^image redis ' "$bundle/manifest.txt" > "$bundle/manifest.txt.tmp"
  mv "$bundle/manifest.txt.tmp" "$bundle/manifest.txt"
  ( cd "$bundle" && sha256sum manifest.txt > manifest.sha256 )
  chmod 644 "$bundle/manifest.sha256"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_BANNER_COUNT=1
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/catalog.out" 2>"$err"; then
    echo "缺少映像仍繼續更新" >&2
    return 1
  fi
  grep -q '缺少 redis' "$err" || { echo "沒有拒絕缺少的映像"; cat "$err" >&2; return 1; }
  if grep -E '(^| )stop( |$)' "$log"; then
    echo "清單不完整就停了服務" >&2
    cat "$log" >&2
    return 1
  fi
  make_min_bundle "$bundle" "2026.09.29-4"
  line="$(grep '^image csp ' "$bundle/manifest.txt" | head -n 1)"
  printf '%s\n' "$line" >> "$bundle/manifest.txt"
  ( cd "$bundle" && sha256sum manifest.txt > manifest.sha256 )
  chmod 644 "$bundle/manifest.sha256"
  : > "$log"
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/catalog-dup.out" 2>"$err"; then
    echo "重複映像仍繼續更新" >&2
    return 1
  fi
  grep -q '重複' "$err" || { echo "沒有拒絕重複的映像"; cat "$err" >&2; return 1; }
  if grep -E '(^| )stop( |$)' "$log"; then
    echo "映像重複就停了服務" >&2
    return 1
  fi
}

test_cancel_marks_pending_when_db_audit_fails() {
  local root log
  root="$tmp/cancel-pending-root"
  log="$tmp/cancel-pending.log"
  layout_rollback_install "$root"
  arm_test_install "$root"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_COUNTS='1|2|3'
  export STUB_AUDIT_FAIL=1
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; printf '%s\n' 'typo' | cmd_rollback ) >"$tmp/cancel-pending.out" 2>"$tmp/cancel-pending.err"; then
    echo "打錯版本仍回復" >&2
    return 1
  fi
  [[ -s "$root/state/db-audit-pending" ]] || {
    echo "稽核寫不進資料庫時沒有待寫標記" >&2
    return 1
  }
  [[ "$(state_get db_audit_pending)" == "1" ]] || return 1
  unset STUB_AUDIT_FAIL
}

test_studio_volume_is_snapshotted_with_share_dirs() {
  local root tree dest log tar
  root="$tmp/studio-root"
  log="$tmp/studio.log"
  mkdir -p "$root"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  mkdir -p "$tree/infra/deployment/scripts"
  cp "$ROOT/infra/deployment/scripts/backup-lib.sh" \
    "$tree/infra/deployment/scripts/backup-lib.sh"
  dest="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_LIVE_ALEMBIC=r1_0062
  unset ANILA_PG_DUMP_CMD || true
  backup_for_update "$tree" "$dest" >/dev/null || { echo "備份失敗" >&2; return 1; }
  tar="$(dirname "$dest")/files/studio-artifacts.tar"
  [[ -s "$tar" ]] || { echo "沒有 studio volume 快照"; return 1; }
  [[ "$(stat -c %a "$tar")" == "600" ]] || { echo "studio 快照權限不對"; return 1; }
  grep -q "${TEST_PROJECT}_anila-studio-artifacts" "$log" || {
    echo "沒有把 studio volume 掛進輔助容器" >&2
    cat "$log" >&2
    return 1
  }
  grep -q -- '--pull never' "$log" || { echo "輔助容器沒有 --pull never"; return 1; }
  : > "$log"
  restore_share_snapshot "$dest" || { echo "還原 studio volume 失敗" >&2; return 1; }
  grep -q -- '-xf' "$log" || { echo "沒有把 studio 快照解回 volume"; cat "$log" >&2; return 1; }
  grep -q 'anila-restore-staging' "$log" || { echo "沒有先解到暫存目錄"; cat "$log" >&2; return 1; }
  grep -q 'anila-restore-previous' "$log" || {
    echo "沒有把快照之後的檔移出線上目錄" >&2
    cat "$log" >&2
    return 1
  }
  mkdir -p "$root/state/share/backups/pre-update/2026.09.29-2/files" \
    "$root/state/share/backups/pre-update/2026.09.29-3/files"
  printf 'keep\n' > "$root/state/share/backups/pre-update/2026.09.29-2/files/studio-artifacts.tar"
  printf 'keep\n' > "$root/state/share/backups/pre-update/2026.09.29-3/files/studio-artifacts.tar"
  prune_old_preupdate_backups "$root" "2026.09.29-2" "2026.09.29-3"
  [[ -f "$root/state/share/backups/pre-update/2026.09.29-2/files/studio-artifacts.tar" ]] || return 1
  [[ -f "$root/state/share/backups/pre-update/2026.09.29-3/files/studio-artifacts.tar" ]] || return 1
  [[ ! -e "$tar" ]] || { echo "studio 快照沒有只留最近兩份"; return 1; }
}

test_scan_reads_plain_files_and_redis() {
  local fixture layer bundle out err rc
  if grep -q '\[\[ "$svc" == "redis" \]\] && continue' "$ROOT/scripts/release/build-release.sh"; then
    echo "redis 仍被排除在映像掃描之外" >&2
    return 1
  fi
  fixture="$tmp/scan-fixture"
  layer="$fixture/layer"
  bundle="$fixture/bundle.tar"
  mkdir -p "$layer/opt/app" "$fixture/save/blobs/sha256"
  printf '%s\n' '-----BEGIN RSA PRIVATE KEY-----' 'MIIEowIBAAKCAQEAabcdefghijklmn' > "$layer/opt/app/notes.txt"
  printf '%s\n' '-----BEGIN CERTIFICATE-----' 'MIIB' > "$layer/etc-not-used"
  tar -cf "$fixture/layer.tar" -C "$layer" .
  gzip -c "$fixture/layer.tar" > "$fixture/save/blobs/sha256/layer1"
  printf '%s\n' \
    '[{"Config":"blobs/sha256/config","RepoTags":["scan-test:notes"],"Layers":["blobs/sha256/layer1"]}]' \
    > "$fixture/save/manifest.json"
  printf 'config\n' > "$fixture/save/blobs/sha256/config"
  tar -cf "$bundle" -C "$fixture/save" manifest.json blobs
  out="$tmp/scan.out"
  err="$tmp/scan.err"
  set +e
  bash "$ROOT/infra/deployment/scripts/scan-image-artifacts.sh" "$bundle" >"$out" 2>"$err"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || { echo "一般檔裡的私鑰仍被當成乾淨"; cat "$out" "$err" >&2; return 1; }
  grep -q 'opt/app/notes.txt' "$out" || {
    echo "沒有點名藏私鑰的一般檔" >&2
    cat "$out" >&2
    return 1
  }
}

check "已有專案或 volume 不是第一次安裝" test_existing_stack_is_not_a_first_install
check "認領先備份再寫安裝記錄" test_adopt_backs_up_before_writing_state
check "compose 目錄必須是記錄中的版本" test_compose_directory_must_match_state
check "進入腳本前先核對清單 SHA256" test_entry_checks_manifest_before_sourcing
test_anchor_is_not_overwritten_or_bypassed() {
  local anchor a b a_phys b_phys before tree log err
  a="$tmp/anchor-root-a"
  b="$tmp/anchor-root-b"
  anchor="$tmp/install-anchor"
  log="$tmp/anchor.log"
  err="$tmp/anchor.err"
  mkdir -p "$a" "$b"
  _install_anchor_file() { printf '%s\n' "$anchor"; }
  a_phys="$(cd "$a" && pwd -P)"
  b_phys="$(cd "$b" && pwd -P)"
  printf 'install_root=%s\ncompose_project=%s\n' "$a_phys" "$TEST_PROJECT" > "$anchor"
  chmod 600 "$anchor"
  before="$(cat "$anchor")"
  export ANILA_INSTALL_ROOT="$b"
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  mkdir -p "$b/state"
  state_set install_root "$b_phys"
  state_set compose_project "$TEST_PROJECT"
  : > "$log"
  export DOCKER_LOG="$log"
  if ( trap - EXIT; stop_writers "$b" ) >"$tmp/anchor.out" 2>"$err"; then
    echo "ANILA_INSTALL_ROOT 繞過了安裝記錄" >&2
    return 1
  fi
  grep -q '不會改寫既有記錄' "$err" || {
    echo "根目錄不符時沒有拒絕改寫" >&2
    cat "$err" >&2
    return 1
  }
  [[ ! -s "$log" ]] || { echo "拒絕之前就呼叫了 docker"; cat "$log" >&2; return 1; }
  [[ "$(cat "$anchor")" == "$before" ]] || { echo "拒絕時改寫了安裝記錄"; return 1; }
  if ( trap - EXIT; write_install_anchor "$b_phys" "$TEST_PROJECT" ) >"$tmp/anchor-write.out" 2>"$err"; then
    echo "把安裝記錄改寫成另一個根目錄" >&2
    return 1
  fi
  grep -q '不會改寫成' "$err" || { echo "改寫拒絕的訊息不清楚"; cat "$err" >&2; return 1; }
  [[ "$(cat "$anchor")" == "$before" ]] || { echo "改寫拒絕後記錄變了"; return 1; }
  export ANILA_INSTALL_ROOT="$a"
  export COMPOSE_PROJECT_NAME=anila-other
  mkdir -p "$a/state"
  state_set install_root "$a_phys"
  state_set compose_project anila-other
  if ( trap - EXIT; stop_writers "$a" ) >"$tmp/anchor-proj.out" 2>"$err"; then
    echo "專案不符仍放行" >&2
    return 1
  fi
  [[ "$(cat "$anchor")" == "$before" ]] || { echo "專案不符時改寫了記錄"; return 1; }
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  state_set compose_project "$TEST_PROJECT"
  tree="$(use_version_tree "$a" "2026.09.29-1")"
  : > "$log"
  stop_writers "$tree" || { echo "記錄相符的根目錄不該被拒絕"; cat "$err" >&2; return 1; }
}

test_generated_secrets_are_hex() {
  local root tree admin
  root="$tmp/hex-root"
  mkdir -p "$root"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-1")"
  printf 'ANILA_HOST=lab\n' > "$root/state/.env"
  chmod 600 "$root/state/.env"
  ln -sfn "$root/state/.env" "$tree/.env"
  ensure_platform_env "$tree" >"$tmp/hex.out" || { echo "產生密鑰失敗"; return 1; }
  admin="$(awk -F= '$1=="ADMIN_PASSWORD" { print substr($0, index($0, "=") + 1) }' "$root/state/.env")"
  [[ "$admin" =~ ^[0-9a-f]+$ ]] || { echo "新密鑰不是十六進位：$admin"; return 1; }
  if grep -q 'openssl rand -base64' "$ROOT/scripts/release/anila-update.sh"; then
    echo "仍用 base64 產生密鑰" >&2
    return 1
  fi
}

test_share_restore_replaces_directory_exactly() {
  local root dump live snap
  root="$tmp/share-exact"
  mkdir -p "$root"
  arm_test_install "$root"
  use_version_tree "$root" "2026.09.29-1" >/dev/null
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  live="$root/state/share/uploads"
  snap="$(dirname "$dump")/files/uploads"
  mkdir -p "$live" "$snap"
  printf 'after\n' > "$live/after.txt"
  printf 'before\n' > "$snap/before.txt"
  printf 'PGDMP\n' > "$dump"
  restore_share_snapshot "$dump" || { echo "檔案還原失敗"; return 1; }
  [[ ! -e "$live/after.txt" ]] || { echo "快照之後的檔還在"; return 1; }
  [[ "$(cat "$live/before.txt")" == "before" ]] || { echo "快照裡的檔沒換上"; return 1; }
  [[ -d "$root/state/restore-stage" ]] || { echo "還原暫存不在安裝根目錄"; return 1; }
  if find "$HOME" -maxdepth 1 -name '.anila-share-stage.*' | grep -q .; then
    echo "還原暫存寫到家目錄" >&2
    return 1
  fi
}

test_manual_rollback_restore_failure_restarts_running_version() {
  local root log err link last
  root="$tmp/manual-fail-root"
  log="$tmp/manual-fail.log"
  err="$tmp/manual-fail.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_RESTORE_FAIL=1
  export STUB_RESTORE_ALEMBIC=r1_0062
  export STUB_COUNTS='1|2|3'
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; printf '%s\n' '2026.09.29-1' | cmd_rollback ) >"$tmp/manual-fail.out" 2>"$err"; then
    echo "資料庫還原失敗仍當作手動回復成功" >&2
    return 1
  fi
  grep -q '沒有被換掉' "$err" || { echo "沒有說明線上資料庫還在"; cat "$err" >&2; return 1; }
  grep -q '指回 2026.09.29-2' "$err" || { echo "沒有指回正在跑的版本"; cat "$err" >&2; return 1; }
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "還原失敗仍刪掉線上資料庫" >&2
    return 1
  fi
  link="$(readlink "$root/current")"
  [[ "$link" == "versions/2026.09.29-2" ]] || { echo "current 不再指回正在跑的版本：$link"; return 1; }
  last="$(awk -v p="$TEST_PROJECT/csp:" 'index($0, "tag " p) { line=$0 } END { print line }' "$log")"
  [[ "$last" == *2026.09.29-2* ]] || { echo "沒有把映像標回正在跑的版本：$last"; return 1; }
  if awk -v p="$TEST_PROJECT/" '$1=="tag" && $NF !~ ("^" p) { bad=1 } END { exit bad ? 0 : 1 }' "$log"; then
    echo "回復過程標記了專案命名空間以外的映像" >&2
    awk '$1=="tag" { print }' "$log" >&2
    return 1
  fi
  grep -q 'up -d' "$log" || { echo "沒有把正在跑的版本拉起來"; return 1; }
  grep -q 'platform_rollback' "$log" || { echo "手動回復失敗沒寫稽核"; cat "$log" >&2; return 1; }
  unset STUB_RESTORE_FAIL
  export STUB_STUDIO_RESTORE_FAIL=1
  mkdir -p "$(dirname "$root/state/share/backups/pre-update/2026.09.29-1/db.dump")/files"
  printf 'studio\n' > "$root/state/share/backups/pre-update/2026.09.29-1/files/studio-artifacts.tar"
  : > "$log"
  if ( trap - EXIT; printf '%s\n' '2026.09.29-1' | cmd_rollback ) >"$tmp/manual-file.out" 2>"$err"; then
    echo "檔案快照失敗仍當作手動回復成功" >&2
    return 1
  fi
  grep -q '檔案快照還原失敗' "$err" || { echo "手動回復沒有報告檔案快照失敗"; cat "$err" >&2; return 1; }
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "檔案快照失敗仍刪掉線上資料庫" >&2
    return 1
  fi
  [[ "$(readlink "$root/current")" == "versions/2026.09.29-2" ]] || return 1
  unset STUB_STUDIO_RESTORE_FAIL
}

test_auto_rollback_share_failure_is_not_swallowed() {
  local root log err dump
  root="$tmp/share-fail-root"
  log="$tmp/share-fail.log"
  err="$tmp/share-fail.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  mkdir -p "$(dirname "$dump")/files"
  printf 'studio\n' > "$(dirname "$dump")/files/studio-artifacts.tar"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_LIVE_ALEMBIC=r1_0099
  export STUB_STUDIO_RESTORE_FAIL=1
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; auto_rollback "$root" "2026.09.29-1" "2026.09.29-2" "r1_0062" "$dump" ) \
    >"$tmp/share-fail.out" 2>"$err"; then
    echo "檔案快照失敗仍當作自動回復成功" >&2
    cat "$err" >&2
    return 1
  fi
  grep -q '檔案快照還原失敗' "$err" || { echo "沒有報告檔案快照失敗"; cat "$err" >&2; return 1; }
  if grep -q '已回復到' "$err"; then
    echo "檔案快照失敗仍印出回復成功" >&2
    return 1
  fi
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "檔案快照失敗仍換掉線上資料庫" >&2
    return 1
  fi
  grep -q 'platform_rollback' "$log" || { echo "檔案快照失敗沒寫回復稽核"; return 1; }
  grep -q 'platform_update' "$log" || { echo "檔案快照失敗沒寫更新失敗稽核"; return 1; }
  if grep -q '|| true' "$ROOT/scripts/release/anila-update.sh" \
    && grep -n 'restore_share_snapshot' "$ROOT/scripts/release/anila-update.sh" | grep -q '|| true'; then
    echo "自動回復仍吞掉檔案快照失敗" >&2
    return 1
  fi
  unset STUB_STUDIO_RESTORE_FAIL
}

test_image_catalog_matches_bundle_tsv_before_stop() {
  local root bundle log err
  root="$tmp/tsv-root"
  bundle="$tmp/tsv-bundle"
  log="$tmp/tsv.log"
  err="$tmp/tsv.err"
  mkdir -p "$root/versions/2026.09.29-2"
  printf 'name: anila\n' > "$root/versions/2026.09.29-2/compose.yaml"
  ln -sfn versions/2026.09.29-2 "$root/current"
  arm_test_install "$root"
  state_set current "2026.09.29-2"
  make_min_bundle "$bundle" "2026.09.29-4"
  sed -i 's/anila-csp:latest/anila-csp:tampered/' "$bundle/manifest.txt"
  ( cd "$bundle" && sha256sum manifest.txt > manifest.sha256 )
  chmod 644 "$bundle/manifest.sha256"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_BANNER_COUNT=1
  if ( trap - EXIT; cmd_update "$bundle" ) >"$tmp/tsv.out" 2>"$err"; then
    echo "映像名與 images.tsv 不符仍繼續更新" >&2
    return 1
  fi
  grep -q 'image 與出貨包 images.tsv 不符' "$err" || {
    echo "沒有拒絕欄位不符" >&2
    cat "$err" >&2
    return 1
  }
  if grep -E '(^| )stop( |$)' "$log"; then
    echo "欄位不符就停了服務" >&2
    cat "$log" >&2
    return 1
  fi
}

test_manifest_pair_is_the_same_directory() {
  local dir
  dir="$tmp/manifest-pair"
  mkdir -p "$dir/nested/extra" "$dir/anila-2026.09.29-1" "$dir/only-txt" "$dir/only-sha"
  printf 'bad\n' > "$dir/nested/extra/manifest.txt"
  printf '0000000000000000000000000000000000000000000000000000000000000000  manifest.txt\n' \
    > "$dir/nested/extra/manifest.sha256"
  printf 'good\n' > "$dir/anila-2026.09.29-1/manifest.txt"
  ( cd "$dir/anila-2026.09.29-1" && sha256sum manifest.txt > manifest.sha256 )
  tar -C "$dir" -czf "$dir/bundle.tar.gz" \
    nested/extra/manifest.txt \
    nested/extra/manifest.sha256 \
    anila-2026.09.29-1/manifest.txt \
    anila-2026.09.29-1/manifest.sha256
  _entry_manifest_sha256 "$dir/bundle.tar.gz" || {
    echo "出貨包根目錄的清單沒通過" >&2
    return 1
  }
  printf 'x\n' > "$dir/only-txt/manifest.txt"
  printf 'x\n' > "$dir/only-sha/manifest.sha256"
  tar -C "$dir" -czf "$dir/split.tar.gz" only-txt/manifest.txt only-sha/manifest.sha256
  if _entry_manifest_sha256 "$dir/split.tar.gz" >"$tmp/split.out" 2>"$tmp/split.err"; then
    echo "清單不在同一目錄仍通過" >&2
    return 1
  fi
  mkdir -p "$dir/left" "$dir/right"
  printf 'a\n' > "$dir/left/manifest.txt"
  ( cd "$dir/left" && sha256sum manifest.txt > manifest.sha256 )
  printf 'b\n' > "$dir/right/manifest.txt"
  ( cd "$dir/right" && sha256sum manifest.txt > manifest.sha256 )
  tar -C "$dir" -czf "$dir/two.tar.gz" \
    left/manifest.txt left/manifest.sha256 \
    right/manifest.txt right/manifest.sha256
  if _entry_manifest_sha256 "$dir/two.tar.gz" >"$tmp/two.out" 2>"$tmp/two.err"; then
    echo "同一層有兩份清單仍取了第一筆" >&2
    return 1
  fi
  grep -q '多份清單' "$tmp/two.err" || { echo "沒有拒絕多份清單"; cat "$tmp/two.err" >&2; return 1; }
}

test_deleted_layer_key_still_fails_scan() {
  local fixture l1 l2 bundle out err rc
  fixture="$tmp/whiteout-scan"
  l1="$fixture/layer1"
  l2="$fixture/layer2"
  bundle="$fixture/bundle.tar"
  mkdir -p "$l1/opt" "$l2/opt" "$fixture/save/blobs/sha256"
  printf '%s\n' '-----BEGIN PRIVATE KEY-----' 'MIIEvQIBADANBgkqhkiGhiddenAAA' > "$l1/opt/secret.txt"
  : > "$l2/opt/.wh.secret.txt"
  tar -cf "$fixture/layer1.tar" -C "$l1" .
  tar -cf "$fixture/layer2.tar" -C "$l2" .
  gzip -c "$fixture/layer1.tar" > "$fixture/save/blobs/sha256/layer1"
  gzip -c "$fixture/layer2.tar" > "$fixture/save/blobs/sha256/layer2"
  printf '%s\n' \
    '[{"Config":"blobs/sha256/config","RepoTags":["scan-test:whiteout"],"Layers":["blobs/sha256/layer1","blobs/sha256/layer2"]}]' \
    > "$fixture/save/manifest.json"
  printf 'config\n' > "$fixture/save/blobs/sha256/config"
  tar -cf "$bundle" -C "$fixture/save" manifest.json blobs
  out="$tmp/whiteout.out"
  err="$tmp/whiteout.err"
  set +e
  bash "$ROOT/infra/deployment/scripts/scan-image-artifacts.sh" "$bundle" >"$out" 2>"$err"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || { echo "後層刪掉的私鑰仍被當成乾淨"; cat "$out" "$err" >&2; return 1; }
  grep -q 'opt/secret.txt' "$out" || { echo "沒有點名被後層刪掉的私鑰"; cat "$out" >&2; return 1; }
  mkdir -p "$fixture/wrong/usr/lib/code-server/node_modules/httpolyglot/test/fixtures" \
    "$fixture/wrong-save/blobs/sha256"
  printf '%s\n' '-----BEGIN PRIVATE KEY-----' 'notTheFixtureCCCCCCCCCCCCCCCC' \
    > "$fixture/wrong/usr/lib/code-server/node_modules/httpolyglot/test/fixtures/server.key"
  tar -cf "$fixture/wrong.tar" -C "$fixture/wrong" .
  gzip -c "$fixture/wrong.tar" > "$fixture/wrong-save/blobs/sha256/layer1"
  printf '%s\n' \
    '[{"Config":"blobs/sha256/config","RepoTags":["scan-test:wrongkey"],"Layers":["blobs/sha256/layer1"]}]' \
    > "$fixture/wrong-save/manifest.json"
  printf 'config\n' > "$fixture/wrong-save/blobs/sha256/config"
  tar -cf "$fixture/wrong-bundle.tar" -C "$fixture/wrong-save" manifest.json blobs
  set +e
  bash "$ROOT/infra/deployment/scripts/scan-image-artifacts.sh" "$fixture/wrong-bundle.tar" \
    >"$tmp/wrongkey.out" 2>"$tmp/wrongkey.err"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || { echo "雜湊不同的 httpolyglot 私鑰被放行"; cat "$tmp/wrongkey.out" >&2; return 1; }
  grep -q 'usr/lib/code-server/node_modules/httpolyglot/test/fixtures/server.key|6bf80cc4376ae97a69b2eb95fd3e17df4614bea2fe224e5e707806ed9bf0f2c8|' \
    "$ROOT/infra/deployment/scripts/scan-image-artifacts.sh" || {
    echo "沒有釘住 httpolyglot 測試 fixture 的雜湊" >&2
    return 1
  }
}

test_update_doc_states_alembic_restore_rule() {
  grep -q '目前的 alembic 版本是否與備份記下的版本不同' "$ROOT/docs/deploy/UPDATE.md" || {
    echo "UPDATE.md 沒有寫 alembic 與備份版本不同才還原" >&2
    return 1
  }
  grep -q '動作 `adopt`' "$ROOT/docs/deploy/UPDATE.md" || {
    echo "UPDATE.md 沒有寫認領成功的稽核" >&2
    return 1
  }
}

check "自動回復還原失敗仍拉起上一版" test_auto_rollback_restore_failure_keeps_live_db
check "安裝記錄不會被改寫或繞過" test_anchor_is_not_overwritten_or_bypassed
check "新密鑰是十六進位" test_generated_secrets_are_hex
check "檔案還原會清掉快照之後的檔" test_share_restore_replaces_directory_exactly
check "手動回復還原失敗會拉回正在跑的版本" test_manual_rollback_restore_failure_restarts_running_version
check "自動回復不吞掉檔案快照失敗" test_auto_rollback_share_failure_is_not_swallowed
check "映像欄位要對上出貨包的 images.tsv" test_image_catalog_matches_bundle_tsv_before_stop
check "清單成對取同一目錄" test_manifest_pair_is_the_same_directory
check "後層刪掉的私鑰仍讓掃描失敗" test_deleted_layer_key_still_fails_scan
check "文件寫的是 alembic 不同才還原" test_update_doc_states_alembic_restore_rule
check "映像清單缺漏或重複就停" test_image_catalog_rejects_missing_and_duplicate_before_stop
check "containerd 儲存用 manifest 雜湊核對" test_load_accepts_containerd_manifest_digest
check "打錯版本且稽核失敗會標待寫" test_cancel_marks_pending_when_db_audit_fails
check "studio volume 跟檔案一起快照" test_studio_volume_is_snapshotted_with_share_dirs
check "一般檔與 redis 都掃私鑰" test_scan_reads_plain_files_and_redis
check "已安裝但資料庫沒起來就中止" test_previous_version_with_db_down_aborts_before_changes
check "第一次安裝不在 /opt/anila 就拒絕" test_first_install_outside_opt_refuses_before_docker
check "假的 anila 根目錄在標記映像前拒絕" test_fake_anila_root_refuses_before_image_tag
check "備份失敗會拉起上一版" test_backup_failure_restarts_previous_version
check "取消回復會重新啟動" test_cancel_rollback_restarts_after_counting
check "回復確認不能用環境變數" test_rollback_confirm_is_typed_not_an_environment_variable
check "更新前快照檔案並只留兩份" test_preupdate_hardlink_snapshot_restores_with_the_database
check "稽核用 psql 且失敗會補寫" test_db_audit_is_psql_and_pending_retries
check "自動回復先標回映像" test_auto_rollback_retags_before_missing_tree
check "選用服務的指令含 compose 檔與專案" test_optional_service_commands_name_compose_file_and_project
check "開發旗標會警告並被正式部署拒絕" test_dev_secret_flag_is_warned_then_refused
check "redis 以 digest 釘住" test_redis_image_is_pinned_by_digest
check "清單拒絕穿越與多出來的檔" test_manifest_rejects_traversal_and_unlisted_files
check "密鑰掃描含 env、憑證與 PRIVATE KEY" test_secrets_scan_rejects_env_keys_and_private_key_blocks
check "asr-gateway 進清單且預設啟動" test_asr_gateway_shipped_and_started
check "打包會建置 asr-gateway" test_build_release_builds_asr_gateway
check "未指定安裝根目錄時拒絕對專案 anila 停服務" test_unset_install_root_refuses_live_project
check "工作樹對專案 anila 拒絕停服務" test_worktree_refuses_stop_on_live_project
check "工作樹對專案 anila 拒絕更新前備份" test_worktree_refuses_backup_on_live_project
check "工作樹對專案 anila 拒絕還原" test_worktree_refuses_restore_on_live_project
check "工作樹對專案 anila 拒絕回復" test_worktree_refuses_rollback_on_live_project
check "沒有初次安裝記錄就拒絕停服務" test_unrecorded_install_refuses_stop
check "compose 專案與記錄不符就拒絕" test_project_mismatch_refuses_stop
check "記錄的專案才是 docker 收到的專案" test_recorded_project_is_what_docker_stops

test_redis_load_verifies_image_id_and_project_tag() {
  local root bundle tree log digest
  digest='sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
  root="$tmp/redis-root"
  bundle="$tmp/redis-bundle"
  log="$tmp/redis-load.log"
  mkdir -p "$bundle/images" "$root"
  arm_test_install "$root"
  tree="$(use_version_tree "$root" "2026.09.29-9")"
  printf 'x\n' | gzip -c > "$bundle/images/redis.tar.gz"
  printf 'version 2026.09.29-9\nimage redis redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499 %s images/redis.tar.gz\n' \
    "$digest" > "$bundle/manifest.txt"
  : > "$log"
  export DOCKER_LOG="$log"
  load_bundle_images "$bundle" "2026.09.29-9" || {
    echo "載入 redis 失敗" >&2
    cat "$log" >&2
    return 1
  }
  if grep -q '@sha256:' "$log"; then
    echo "仍用 RepoDigest 查或標記 redis" >&2
    cat "$log" >&2
    return 1
  fi
  grep -q "tag ${digest} ${TEST_PROJECT}/redis:running" "$log" || {
    echo "沒有把 redis 標成專案的 running 標籤" >&2
    cat "$log" >&2
    return 1
  }
  grep -q "tag ${digest} ${TEST_PROJECT}/redis:2026.09.29-9" "$log" || return 1
  write_image_override "$tree"
  grep -q "image: ${TEST_PROJECT}/redis:running" "$tree/.anila-images.yml" || {
    echo "compose 覆寫檔沒有用載入後的標籤" >&2
    return 1
  }
  if ( trap - EXIT; docker_tag_project "$digest" "redis:7-alpine" ) \
    >"$tmp/redis-tag.out" 2>"$tmp/redis-tag.err"; then
    echo "沒有安裝記錄仍標記 redis:7-alpine" >&2
    return 1
  fi
  grep -q '拒絕操作' "$tmp/redis-tag.err" || return 1
}

test_prune_only_removes_project_tags() {
  local root log
  root="$tmp/prune-root"
  log="$tmp/prune.log"
  mkdir -p "$root"
  arm_test_install "$root"
  cat > "$tmp/prune-images" <<EOF
${TEST_PROJECT}/csp:old
${TEST_PROJECT}/csp:2026.09.29-2
${TEST_PROJECT}/csp:running
anila/csp:old
redis:7-alpine
anila-csp:latest
EOF
  : > "$log"
  export DOCKER_LOG="$log"
  export DOCKER_IMAGE_LS_FILE="$tmp/prune-images"
  prune_old_images "2026.09.29-2" "2026.09.29-1"
  grep -q "rmi ${TEST_PROJECT}/csp:old" "$log" || {
    echo "沒有刪自己的舊標籤" >&2
    cat "$log" >&2
    return 1
  }
  if grep -E 'rmi (anila/|redis:|anila-csp:)' "$log"; then
    echo "刪到專案以外的映像" >&2
    return 1
  fi
  if grep -q "rmi ${TEST_PROJECT}/csp:running" "$log"; then
    echo "刪掉正在用的標籤" >&2
    return 1
  fi
  if grep -q "rmi ${TEST_PROJECT}/csp:2026.09.29-2" "$log"; then
    echo "刪掉要保留的版本" >&2
    return 1
  fi
}

test_manual_rollback_health_failure_restores_previous_state() {
  local root log err dump
  root="$tmp/health-fail-root"
  log="$tmp/health-fail.log"
  err="$tmp/health-fail.err"
  layout_rollback_install "$root"
  arm_test_install "$root"
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  mkdir -p "$root/state/share/uploads" "$(dirname "$dump")/files/uploads"
  printf 'NOW\n' > "$root/state/share/uploads/now.txt"
  printf 'OLD\n' > "$(dirname "$dump")/files/uploads/old.txt"
  : > "$log"
  export DOCKER_LOG="$log"
  export STUB_HEALTH_FAIL=1
  export STUB_RESTORE_ALEMBIC=r1_0062
  export STUB_COUNTS='1|2|3'
  export ANILA_HEALTH_TIMEOUT=2
  export ANILA_HEALTH_POLL=0
  if ( trap - EXIT; printf '%s\n' '2026.09.29-1' | cmd_rollback ) >"$tmp/health-fail.out" 2>"$err"; then
    echo "健康檢查沒過仍當作回復成功" >&2
    cat "$err" >&2
    return 1
  fi
  grep -q '切回 2026.09.29-2' "$err" || {
    echo "沒有切回正在跑的版本" >&2
    cat "$err" >&2
    return 1
  }
  [[ "$(cat "$root/state/share/uploads/now.txt")" == "NOW" ]] || {
    echo "檔案沒有切回" >&2
    return 1
  }
  [[ "$(readlink "$root/current")" == "versions/2026.09.29-2" ]] || return 1
  grep -q 'ALTER DATABASE csp RENAME TO csp_previous' "$log" || return 1
  grep -q 'ALTER DATABASE csp_previous RENAME TO csp' "$log" || {
    echo "沒有把資料庫改回來" >&2
    return 1
  }
  if awk '$0 ~ /DROP DATABASE csp( |$)/ { found=1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "健康檢查沒過仍刪掉線上資料庫" >&2
    return 1
  fi
  awk '
    /目前指標改為 2026.09.29-1/ { if (!a) a = NR }
    /目前指標改為 2026.09.29-2/ { b = NR }
    END { exit (a && b && a < b) ? 0 : 1 }
  ' "$err" || {
    echo "指標沒有先改再切回" >&2
    cat "$err" >&2
    return 1
  }
  if awk 'index($0, "up -d") && index($0, " nginx") { found = 1 } END { exit found ? 0 : 1 }' "$log"; then
    echo "健康檢查沒過仍開啟入口" >&2
    return 1
  fi
  unset STUB_HEALTH_FAIL
}

test_share_restore_stage_falls_back_on_exdev() {
  local root dump live snap
  root="$tmp/exdev-root"
  mkdir -p "$root" "$tmp/cpbin"
  arm_test_install "$root"
  use_version_tree "$root" "2026.09.29-1" >/dev/null
  dump="$root/state/share/backups/pre-update/2026.09.29-1/db.dump"
  live="$root/state/share/uploads"
  snap="$(dirname "$dump")/files/uploads"
  mkdir -p "$live" "$snap"
  printf 'after\n' > "$live/after.txt"
  printf 'before\n' > "$snap/before.txt"
  printf 'PGDMP\n' > "$dump"
  cat > "$tmp/cpbin/cp" <<'EOF'
#!/bin/bash
if [[ "$1" == "-al" ]]; then
  printf '%s\n' "cp: cannot create hard link 'x' to 'y': Invalid cross-device link" >&2
  exit 1
fi
exec /bin/cp "$@"
EOF
  chmod +x "$tmp/cpbin/cp"
  PATH="$tmp/cpbin:$PATH"
  restore_share_snapshot "$dump" || { echo "跨裝置還原失敗" >&2; return 1; }
  [[ "$(cat "$live/before.txt")" == "before" ]] || return 1
  [[ ! -e "$live/after.txt" ]] || return 1
  [[ "$(stat -c %i "$live/before.txt")" != "$(stat -c %i "$snap/before.txt")" ]] || {
    echo "EXDEV 仍用硬連結" >&2
    return 1
  }
  [[ -d "$root/state/restore-stage" ]] || { echo "暫存不在安裝根目錄" >&2; return 1; }
}

test_second_run_stops_without_touching_incoming() {
  local root log err
  root="$tmp/lock-root"
  log="$tmp/lock.log"
  err="$tmp/lock.err"
  mkdir -p "$root/incoming.stale" "$root/versions/2026.09.29-1"
  printf 'name: anila\n' > "$root/versions/2026.09.29-1/compose.yaml"
  arm_test_install "$root"
  printf '%s\n' "$root/incoming.stale" > "$root/.incoming-path"
  : > "$log"
  export DOCKER_LOG="$log"
  exec {hold}>"$root/state/update.lock"
  flock -n "$hold" || { echo "測試拿不到鎖" >&2; return 1; }
  if ( trap - EXIT; cmd_rollback ) >"$tmp/lock.out" 2>"$err"; then
    echo "第二個回復沒有停" >&2
    return 1
  fi
  grep -q '正在進行' "$err" || { echo "沒有說明鎖"; cat "$err" >&2; return 1; }
  [[ -d "$root/incoming.stale" ]] || { echo "別的執行把暫存清掉了" >&2; return 1; }
  [[ ! -s "$log" ]] || { echo "拿不到鎖仍呼叫了 docker"; cat "$log" >&2; return 1; }
  if ( trap - EXIT; cmd_update "$tmp/no-such-bundle" ) >"$tmp/lock-up.out" 2>"$err"; then
    echo "第二個更新沒有停" >&2
    return 1
  fi
  grep -q '正在進行' "$err" || { cat "$err" >&2; return 1; }
  [[ -d "$root/incoming.stale" ]] || return 1
  [[ ! -s "$log" ]] || { cat "$log" >&2; return 1; }
  if ( trap - EXIT; cmd_adopt "$root/versions/2026.09.29-1" ) >"$tmp/lock-ad.out" 2>"$err"; then
    echo "第二個認領沒有停" >&2
    return 1
  fi
  grep -q '正在進行' "$err" || { cat "$err" >&2; return 1; }
  [[ -d "$root/incoming.stale" ]] || return 1
  [[ ! -s "$log" ]] || { cat "$log" >&2; return 1; }
}

test_stale_incoming_removed_when_lock_is_free() {
  local root err
  root="$tmp/sweep-root"
  err="$tmp/sweep.err"
  mkdir -p "$root/incoming.stale"
  arm_test_install "$root"
  printf '%s\n' "$root/incoming.stale" > "$root/.incoming-path"
  if ( trap - EXIT; cmd_rollback ) >"$tmp/sweep.out" 2>"$err"; then
    echo "沒有上一版仍成功" >&2
    return 1
  fi
  [[ ! -d "$root/incoming.stale" ]] || { echo "沒人持有鎖時沒清暫存" >&2; return 1; }
  grep -q '沒有上一版' "$err" || { cat "$err" >&2; return 1; }
}

test_retired_scripts_refuse_when_sourced() {
  local s path err
  err="$tmp/retired.err"
  for s in anila-serve.sh intranet-deploy.sh build-and-export-for-intranet.sh; do
    path="$ROOT/infra/deployment/archive/intranet-legacy/$s"
    if bash -c 'source "$1"' bash "$path" >"$tmp/retired.out" 2>"$err"; then
      echo "source ${s} 仍繼續" >&2
      return 1
    fi
    grep -q '已退役' "$err" || {
      echo "source ${s} 沒有退役訊息" >&2
      cat "$err" >&2
      return 1
    }
    if bash "$path" >"$tmp/retired.out" 2>"$err"; then
      echo "執行 ${s} 仍繼續" >&2
      return 1
    fi
    grep -q 'docs/deploy/UPDATE.md' "$err" || return 1
  done
}

check "redis 用映像 ID 核對並標進專案" test_redis_load_verifies_image_id_and_project_tag
check "清理只刪這個專案的映像標籤" test_prune_only_removes_project_tags
check "手動回復健康檢查沒過會切回" test_manual_rollback_health_failure_restores_previous_state
check "跨裝置還原改成複製" test_share_restore_stage_falls_back_on_exdev
check "第二個更新拿不到鎖就停" test_second_run_stops_without_touching_incoming
check "沒人持有鎖才清暫存" test_stale_incoming_removed_when_lock_is_free
check "退役腳本被 source 也會停" test_retired_scripts_refuse_when_sourced

_write_platform_env() {
  local tree="$1"
  mkdir -p "$tree"
  cat > "$tree/.env" <<'EOF'
ANILA_HOST=lab
SECRET_KEY=k
ADMIN_PASSWORD=p
CSP_DB_PASSWORD=d
CSP_APP_DB_PASSWORD=a
CODESERVER_PASSWORD=c
EOF
  chmod 600 "$tree/.env"
}

test_host_account_written_from_sudo_and_docker_group() {
  local tree="$tmp/host-sudo"
  _write_platform_env "$tree"
  getent() {
    if [[ "$1" == group && "$2" == docker ]]; then
      printf 'docker:x:136:\n'
      return 0
    fi
    command getent "$@"
  }
  SUDO_UID=4242 SUDO_GID=2424 ensure_platform_env "$tree"
  (
    cd "$tree"
    [[ "$(get_env UID)" == 4242 ]]
    [[ "$(get_env GID)" == 2424 ]]
    [[ "$(get_env DOCKER_GID)" == 136 ]]
  )
}

test_host_account_fills_blank_keys() {
  local tree="$tmp/host-blank"
  _write_platform_env "$tree"
  printf '\nUID=\nGID=""\nDOCKER_GID='"'"'   '"'"'\nUID=   \n' >> "$tree/.env"
  getent() { printf 'docker:x:136:\n'; }
  SUDO_UID=4242 SUDO_GID=2424 ensure_platform_env "$tree"
  (
    cd "$tree"
    [[ "$(get_env UID)" == 4242 ]]
    [[ "$(get_env GID)" == 2424 ]]
    [[ "$(get_env DOCKER_GID)" == 136 ]]
    [[ "$(grep -cE '^[[:space:]]*(export[[:space:]]+)?UID[[:space:]]*=' .env)" == 1 ]]
    [[ "$(grep -cE '^[[:space:]]*(export[[:space:]]+)?GID[[:space:]]*=' .env)" == 1 ]]
    [[ "$(grep -cE '^[[:space:]]*(export[[:space:]]+)?DOCKER_GID[[:space:]]*=' .env)" == 1 ]]
  )
}

test_host_account_keeps_existing_values() {
  local tree="$tmp/host-keep"
  _write_platform_env "$tree"
  printf '\nUID=7\nGID=8\nDOCKER_GID=9\n' >> "$tree/.env"
  getent() { printf 'docker:x:136:\n'; }
  SUDO_UID=4242 SUDO_GID=2424 ensure_platform_env "$tree"
  (
    cd "$tree"
    [[ "$(get_env UID)" == 7 ]]
    [[ "$(get_env GID)" == 8 ]]
    [[ "$(get_env DOCKER_GID)" == 9 ]]
  )
}

test_host_account_without_sudo_uses_install_root_owner() {
  local root="$tmp/acct-root" tree="$tmp/acct-tree"
  mkdir -p "$root"
  _write_platform_env "$tree"
  export ANILA_INSTALL_ROOT="$root"
  unset SUDO_UID SUDO_GID
  getent() { printf 'docker:x:136:\n'; }
  ensure_platform_env "$tree"
  (
    cd "$tree"
    [[ "$(get_env UID)" == "$(stat -c %u "$root")" ]]
    [[ "$(get_env GID)" == "$(stat -c %g "$root")" ]]
    [[ "$(get_env DOCKER_GID)" == 136 ]]
  )
}

test_postgres_memconf_62_and_755() {
  local script="$ROOT/infra/docker/postgres-memconf.sh"
  local mem62="$tmp/meminfo-62" mem755="$tmp/meminfo-755"
  local cg="$tmp/cgroup-max" small="$tmp/cgroup-small"
  printf 'MemTotal:       65011712 kB\n' > "$mem62"
  printf 'MemTotal:       791674880 kB\n' > "$mem755"
  printf 'max\n' > "$cg"
  printf '8589934592\n' > "$small"
  sh -c '
    set -eu
    POSTGRES_MEMCONF_LIB=1
    . "$1"
    export POSTGRES_MEMINFO="$2" POSTGRES_CGROUP_MAX="$3"
    kib=$(postgres_visible_kib)
    postgres_compute "$kib"
    [ "$shared" = 3968 ] || { echo "62 shared=$shared"; exit 1; }
    [ "$cache" = 31744 ] || { echo "62 cache=$cache"; exit 1; }
    [ "$maint" = 1984 ] || { echo "62 maint=$maint"; exit 1; }
    [ "$work" = 26 ] || { echo "62 work=$work"; exit 1; }
    export POSTGRES_MEMINFO="$4"
    kib=$(postgres_visible_kib)
    postgres_compute "$kib"
    [ "$shared" = 32768 ] || { echo "755 shared=$shared"; exit 1; }
    [ "$cache" = 262144 ] || { echo "755 cache=$cache"; exit 1; }
    [ "$maint" = 2048 ] || { echo "755 maint=$maint"; exit 1; }
    [ "$work" = 64 ] || { echo "755 work=$work"; exit 1; }
    export POSTGRES_MEMINFO="$2" POSTGRES_CGROUP_MAX="$5"
    kib=$(postgres_visible_kib)
    [ "$kib" = 8388608 ] || { echo "cgroup kib=$kib"; exit 1; }
    postgres_compute "$kib"
    [ "$shared" = 512 ] || { echo "cgroup shared=$shared"; exit 1; }
  ' sh "$script" "$mem62" "$cg" "$mem755" "$small"
}

test_postgres_memconf_logs_chosen_values() {
  local bin="$tmp/pg-bin" script="$ROOT/infra/docker/postgres-memconf.sh"
  mkdir -p "$bin"
  cat > "$bin/docker-entrypoint.sh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" > "$POSTGRES_EXEC_LOG"
exit 0
EOF
  chmod +x "$bin/docker-entrypoint.sh"
  printf 'MemTotal:       65011712 kB\n' > "$tmp/meminfo-log"
  printf 'max\n' > "$tmp/cgroup-log"
  PATH="$bin:$PATH" \
    POSTGRES_MEMINFO="$tmp/meminfo-log" \
    POSTGRES_CGROUP_MAX="$tmp/cgroup-log" \
    POSTGRES_EXEC_LOG="$tmp/pg-exec.log" \
    sh "$script" >"$tmp/pg-out" 2>"$tmp/pg-err"
  grep -q 'shared_buffers=3968MB' "$tmp/pg-err"
  grep -q 'work_mem=26MB' "$tmp/pg-err"
  grep -q 'max_connections=120' "$tmp/pg-exec.log"
  grep -q 'shared_buffers=3968MB' "$tmp/pg-exec.log"
}

test_compose_version_gate() {
  (
    source "$ROOT/scripts/release/release-lib.sh"
    compose_version_ok 2.36.2 && compose_version_ok v2.17.0 && compose_version_ok 3.0.0 \
      && ! compose_version_ok 2.16.9 && ! compose_version_ok 1.29.2 && ! compose_version_ok ""
  )
}

test_postgres_memconf_unreadable_uses_defaults() {
  local bin="$tmp/pg-bin-bad" script="$ROOT/infra/docker/postgres-memconf.sh"
  mkdir -p "$bin"
  cat > "$bin/docker-entrypoint.sh" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" > "$POSTGRES_EXEC_LOG"
exit 0
EOF
  chmod +x "$bin/docker-entrypoint.sh"
  printf 'MemTotal:       0 kB\n' > "$tmp/meminfo-zero"
  printf 'MemTotal:       nope kB\n' > "$tmp/meminfo-bad"
  run_one() {
    PATH="$bin:$PATH" \
      POSTGRES_MEMINFO="$1" \
      POSTGRES_CGROUP_MAX="$2" \
      POSTGRES_EXEC_LOG="$3" \
      sh "$script" >"$tmp/pg-bad-out" 2>"$tmp/pg-bad-err"
    grep -q '警告' "$tmp/pg-bad-err"
    grep -q '^postgres -c max_connections=120$' "$3"
    ! grep -q 'shared_buffers' "$3"
  }
  run_one "$tmp/meminfo-zero" "$tmp/cgroup-missing" "$tmp/pg-zero.log"
  run_one "$tmp/meminfo-bad" "$tmp/cgroup-missing" "$tmp/pg-bad.log"
  run_one "$tmp/meminfo-absent" "$tmp/cgroup-missing" "$tmp/pg-absent.log"
}

check "安裝腳本寫入 UID GID DOCKER_GID" test_host_account_written_from_sudo_and_docker_group
check "空白的主機帳號會就地補上" test_host_account_fills_blank_keys
check "已設的主機帳號不被覆寫" test_host_account_keeps_existing_values
check "沒有 sudo 時用安裝根目錄的擁有者" test_host_account_without_sudo_uses_install_root_owner
check "compose 版本低於 2.17 就停" test_compose_version_gate
check "Postgres 記憶體計算 62GB 與 755GB" test_postgres_memconf_62_and_755
check "Postgres 啟動時記下算出的參數" test_postgres_memconf_logs_chosen_values
check "讀不到記憶體時用 Postgres 內建預設" test_postgres_memconf_unreadable_uses_defaults

if [[ "$_fail_count" -ne 0 ]]; then
  printf '%s 項失敗\n' "$_fail_count" >&2
  exit 1
fi

printf 'ok\n'
