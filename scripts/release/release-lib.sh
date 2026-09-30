#!/usr/bin/env bash
# 出貨與內網更新共用的函式。給 build-release.sh 與 anila-update.sh source。
# 沒有安裝記錄時，第一次安裝的預設位置是 /opt/anila。記錄一旦存在，根目錄與專案
# 都要跟記錄相同。ANILA_INSTALL_ROOT 只給測試把目錄指到暫存，不能繞過記錄，
# 不要寫進 .env，也不要在正式機設定。
set -euo pipefail

die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

# compose 檔的 depends_on 用到 restart: true，舊版 compose 會整份拒讀。
compose_version_ok() {
  local v="$1"
  v="${v#v}"
  [[ "$v" =~ ^([0-9]+)\.([0-9]+) ]] || return 1
  (( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 17) ))
}

require_compose_version() {
  local v
  v="$(docker compose version --short 2>/dev/null || true)"
  compose_version_ok "$v" || die "docker compose 版本 ${v:-讀不到}，需要 2.17 以上"
}
warn() { printf '⚠ %s\n' "$*" >&2; }
info() { printf '▶ %s\n' "$*" >&2; }
ok()   { printf '✓ %s\n' "$*" >&2; }

# ── .env 存取：腳本眼中的「已設」必須等於 compose 眼中的「已設」 ─────────────
# 舊版用 `^KEY=` 認鍵，compose 不是這樣解的。實測 v2.36.2，下面每一種寫法
# compose 都讀成同一個值，而 `^KEY=` 一種都認不出來：
#     ` KEY=1`（行首空白）  `\tKEY=1`  `export KEY=1`  `KEY =1`（= 前空白）
#     `KEY="1"` / `KEY='1'`（引號）  `KEY=1 `（行尾空白）  `KEY=1\r\n`（CRLF）
# 差別會直接吃掉操作者的設定：手寫一行之後重跑腳本，grep 看不見，檔尾又
# append 一行，compose 取最後一筆，剛開起來的旗標就被靜默關掉。
_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}
_env_key_re() { printf '^[[:space:]]*(export[[:space:]]+)?%s[[:space:]]*=' "$1"; }
env_has_key() { grep -qE "$(_env_key_re "$1")" .env 2>/dev/null; }
# 空白、只有空白、或空引號都算沒設。UID/GID/DOCKER_GID 用這個判斷，
# 再交給 set_env 換掉那一行，避免檔尾再追加一筆。
env_has_value() {
  local val
  val="$(_trim "$(get_env "$1")")"
  [ -n "$val" ]
}

# .env 在正式機是指向 state/.env 的 symlink。直接 mv 到 .env 會把連結
# 換成一般檔，密鑰落在版本目錄，下一版又會重新產生 SECRET_KEY。
# 寫到連結的目標，連結本身留著，並把目標檔收成 0600。
set_env() {
  local key="$1" val="$2" dest dir tmp oldmask
  [ -e .env ] || [ -L .env ] || die ".env 不存在"
  dest="$(readlink -f -- .env 2>/dev/null || true)"
  [ -n "$dest" ] || die ".env 指不到實際檔案"
  dir="$(dirname -- "$dest")"
  [ -d "$dir" ] || die ".env 的目錄不存在"
  oldmask="$(umask)"
  umask 077
  tmp="$(mktemp "${dir}/.env.XXXXXX")"
  if [ -f "$dest" ]; then
    grep -vE "$(_env_key_re "$key")" "$dest" > "$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$dest"
  chmod 600 "$dest"
  umask "$oldmask"
}
get_env() {
  local line val
  line="$(grep -E "$(_env_key_re "$1")" .env 2>/dev/null | tail -1)" || true
  [ -n "$line" ] || return 0
  val="${line%$'\r'}"
  val="$(_trim "${val#*=}")"
  case "$val" in
    '"'*) val="${val#\"}"; case "$val" in *'"'*) val="${val%%\"*}" ;; esac ;;
    "'"*) val="${val#\'}"; case "$val" in *"'"*) val="${val%%\'*}" ;; esac ;;
    *)    val="$(_trim "${val%% #*}")" ;;
  esac
  printf '%s' "$val"
}
preserve_flag() {
  local key="$1" note="$2"
  if env_has_key "$key"; then
    [ "$(_trim "$(get_env "$key")")" = "1" ] && warn "$key=1 — $note"
  else
    set_env "$key" 0
  fi
  return 0
}
# END ENV HELPERS

ensure_env() {
  local key="$1" val="$2"
  if env_has_key "$key"; then
    return 0
  fi
  set_env "$key" "$val"
}

sha256_file() {
  sha256sum -- "$1" | awk '{print $1}'
}

RELEASE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

release_catalog() {
  local line
  [[ -f "$RELEASE_LIB_DIR/images.tsv" ]] || die "找不到映像清單 images.tsv"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    printf '%s\n' "$line"
  done < "$RELEASE_LIB_DIR/images.tsv"
}

services_to_start() {
  local svc image archive start
  while read -r svc image archive start; do
    [[ "$start" == "yes" ]] || continue
    printf '%s\n' "$svc"
  done < <(release_catalog)
}

install_root() {
  if [[ -n "${ANILA_INSTALL_ROOT:-}" ]]; then
    printf '%s' "$ANILA_INSTALL_ROOT"
  else
    printf '%s' /opt/anila
  fi
}

compose_project() {
  printf '%s' "${COMPOSE_PROJECT_NAME:-anila}"
}

# 專案 anila 的許可不能放在出貨包或版本目錄裡。操作者自己建的樹可以偽造
# state/release.state。這份檔在安裝包外面，權限 0600；root 執行時擁有者是 root。
_install_anchor_file() {
  printf '%s\n' /var/lib/anila/install-anchor
}

_anchor_value() {
  local key="$1" f line
  f="$(_install_anchor_file)"
  [[ -f "$f" ]] || return 0
  line="$(grep -E "^${key}=" "$f" | tail -1 || true)"
  printf '%s' "${line#*=}"
}

install_root_phys() {
  local root
  root="$(install_root)"
  if [[ -d "$root" ]]; then
    (cd "$root" && pwd -P)
  else
    printf '%s' "$root"
  fi
}

# 記錄裡的根目錄。目錄還在就取實體路徑，否則用檔裡的字。
_anchor_root_phys() {
  local recorded
  recorded="$(_anchor_value install_root)"
  [[ -n "$recorded" ]] || return 1
  if [[ -d "$recorded" ]]; then
    (cd "$recorded" && pwd -P)
  else
    printf '%s' "$recorded"
  fi
}

# 記錄一旦存在，根目錄與專案都要跟現在這次相同。
anchor_binds_here() {
  local root_phys project recorded_root recorded_project
  [[ -f "$(_install_anchor_file)" ]] || return 1
  recorded_root="$(_anchor_root_phys)" || return 1
  recorded_project="$(_anchor_value compose_project)"
  [[ -n "$recorded_project" ]] || return 1
  root_phys="$(install_root_phys)"
  project="$(compose_project)"
  [[ "$root_phys" == "$recorded_root" && "$project" == "$recorded_project" ]]
}

install_root_is_real_anchor() {
  local root_phys anchor
  if [[ -f "$(_install_anchor_file)" ]]; then
    anchor_binds_here
    return
  fi
  root_phys="$(install_root_phys)"
  anchor="/opt/anila"
  if [[ -d /opt/anila ]]; then
    anchor="$(cd /opt/anila && pwd -P)"
  fi
  [[ "$root_phys" == "/opt/anila" || "$root_phys" == "$anchor" ]]
}

refuse_if_anchor_mismatch() {
  [[ -f "$(_install_anchor_file)" ]] || return 0
  if anchor_binds_here; then
    return 0
  fi
  die "拒絕操作：安裝根目錄或 compose 專案與安裝記錄不符。記錄是 $(_anchor_value install_root) / $(_anchor_value compose_project)，現在是 $(install_root_phys) / $(compose_project)。不會改寫既有記錄。"
}

write_install_anchor() {
  local root="$1" project="$2" f dir tmp have_root have_project
  if [[ -d "$root" ]]; then
    root="$(cd "$root" && pwd -P)"
  fi
  f="$(_install_anchor_file)"
  if [[ -f "$f" ]]; then
    have_root="$(_anchor_value install_root)"
    have_project="$(_anchor_value compose_project)"
    if [[ -n "$have_root" && -d "$have_root" ]]; then
      have_root="$(cd "$have_root" && pwd -P)"
    fi
    if [[ "$have_root" != "$root" || "$have_project" != "$project" ]]; then
      die "拒絕操作：已有安裝記錄（根目錄 ${have_root}，專案 ${have_project}）。不會改寫成 ${root} / ${project}。"
    fi
    return 0
  fi
  dir="$(dirname "$f")"
  mkdir -p "$dir" || die "寫不了安裝記錄目錄 ${dir}"
  chmod 700 "$dir" || true
  tmp="$(mktemp "$dir/.anchor.XXXXXX")"
  printf 'install_root=%s\ncompose_project=%s\n' "$root" "$project" > "$tmp"
  chmod 600 "$tmp"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown root:root "$dir" "$tmp" || true
  fi
  mv -f "$tmp" "$f"
  chmod 600 "$f"
}

# 沒有安裝記錄時，先看這台是不是已經有同名的 compose 專案或 volume。
# 有的話不能當成第一次安裝。
existing_compose_stack() {
  local project line
  project="$(compose_project)"
  while IFS= read -r line; do
    [[ "$line" == "$project" ]] && return 0
  done < <(docker compose ls --all --format '{{.Name}}' 2>/dev/null || true)
  while IFS= read -r line; do
    [[ -n "$line" ]] && return 0
  done < <(docker volume ls --filter "label=com.docker.compose.project=${project}" --format '{{.Name}}' 2>/dev/null || true)
  return 1
}

refuse_if_existing_stack() {
  existing_compose_stack || return 0
  die "拒絕操作：這不是第一次安裝。這台已有名稱像 $(compose_project) 的 compose 專案或 volume。請先備份再認領：
bash anila-update.sh adopt $(install_root)/versions/<目前版本>"
}

# 停服務、還原、刪除或改名資料庫、載入或標記映像：
# 安裝記錄一旦存在，根目錄與專案都要跟記錄相同，不能改寫，ANILA_INSTALL_ROOT 也不能繞過。
# 沒有記錄時，/opt/anila 只是第一次安裝的預設。
# 版本目錄裡的 state 不能當成專案 anila 的憑證。
# 從 git 工作樹或 repo 的 scripts/release 對專案 anila 一律拒絕。
# 沒有記錄的其他專案（測試）仍用安裝根目錄裡的 state，而且專案名必須相符。
assert_destructive_allowed() {
  local root_phys recorded_root recorded_project project script_dir anchor_phys
  local inside=0 from_repo=0 in_git=0
  project="$(compose_project)"
  root_phys="$(install_root_phys)"
  anchor_phys="/opt/anila"
  if [[ -d /opt/anila ]]; then
    anchor_phys="$(cd /opt/anila && pwd -P)"
  fi
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  case "$script_dir" in
    "$root_phys"|"$root_phys"/*) inside=1 ;;
  esac
  case "$script_dir" in
    */scripts/release) from_repo=1 ;;
  esac
  if git -C "$script_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    in_git=1
  fi
  if [[ "$project" == "anila" ]]; then
    if [[ "$from_repo" -eq 1 || "$in_git" -eq 1 || "$inside" -ne 1 ]]; then
      die "拒絕操作：從工作樹對 compose 專案 anila 停服務或動資料庫。這會打到這台正在跑的平台。"
    fi
  fi
  # 記錄存在就以它為準，認領與第一次安裝也不能換根目錄。
  if [[ -f "$(_install_anchor_file)" ]]; then
    refuse_if_anchor_mismatch
    return 0
  fi
  # 認領進行中、還沒有記錄：備份必須先於寫入 state。
  if [[ "${_adopting:-0}" == 1 ]]; then
    if [[ "$project" == "anila" ]] && ! install_root_is_real_anchor; then
      die "拒絕操作：compose 專案 anila 只允許裝在 /opt/anila，或腳本寫在安裝包外面的記錄。版本目錄裡的 state 不能當成憑證。"
    fi
    return 0
  fi
  if [[ "$project" == "anila" ]]; then
    if ! install_root_is_real_anchor; then
      die "拒絕操作：compose 專案 anila 只允許裝在 /opt/anila，或腳本寫在安裝包外面的記錄。版本目錄裡的 state 不能當成憑證。"
    fi
    recorded_project="$(_anchor_value compose_project)"
    if [[ -z "$recorded_project" ]]; then
      if [[ "$inside" -eq 1 && ( "$root_phys" == "/opt/anila" || "$root_phys" == "$anchor_phys" ) ]]; then
        refuse_if_existing_stack
        write_install_anchor "$root_phys" "$project"
        state_set install_root "$root_phys"
        state_set compose_project "$project"
        return 0
      fi
      die "拒絕操作：還沒有初次安裝記錄，不能停服務或動資料庫。"
    fi
    if [[ "$project" != "$recorded_project" ]]; then
      die "拒絕操作：compose 專案與初次安裝記錄不符，不能停服務或動資料庫。"
    fi
    return 0
  fi
  recorded_root="$(state_get install_root)"
  recorded_project="$(state_get compose_project)"
  if [[ -n "$recorded_root" && -d "$recorded_root" ]]; then
    recorded_root="$(cd "$recorded_root" && pwd -P)"
  fi
  if [[ "$root_phys" != "/opt/anila" && "$root_phys" != "$anchor_phys" && "$root_phys" != "$recorded_root" ]]; then
    die "拒絕操作：安裝根目錄不是 /opt/anila，也不是初次安裝記錄的路徑，不能停服務或動資料庫。"
  fi
  if [[ -z "$recorded_project" ]]; then
    if [[ "$inside" -eq 1 && "$from_repo" -eq 0 && "$in_git" -eq 0 \
      && ( "$root_phys" == "/opt/anila" || "$root_phys" == "$anchor_phys" ) ]]; then
      refuse_if_existing_stack
      state_set install_root "$root_phys"
      state_set compose_project "$project"
      return 0
    fi
    die "拒絕操作：還沒有初次安裝記錄，不能停服務或動資料庫。"
  fi
  if [[ "$project" != "$recorded_project" ]]; then
    die "拒絕操作：compose 專案與初次安裝記錄不符，不能停服務或動資料庫。"
  fi
}

state_file() {
  printf '%s/state/release.state' "$(install_root)"
}

state_get() {
  local key="$1" file line
  file="$(state_file)"
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -1 || true)"
  printf '%s' "${line#*=}"
}

state_set() {
  local key="$1" val="$2" file tmp
  file="$(state_file)"
  mkdir -p "$(dirname "$file")"
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    grep -vE "^${key}=" "$file" > "$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

release_assert_clean() {
  local repo="${1:-.}"
  local dirty
  dirty="$(git -C "$repo" status --porcelain)"
  if [[ -n "$dirty" ]]; then
    printf '工作目錄有未提交的變更，拒絕打包。\n' >&2
    return 1
  fi
}

release_next_version() {
  local out_dir="$1" day="$2"
  local seq_file n=0 num base prefix name
  mkdir -p "$out_dir/seq"
  seq_file="$out_dir/seq/$day"
  if [[ -f "$seq_file" ]]; then
    n="$(tr -cd '0-9' < "$seq_file")"
    [[ -n "$n" ]] || n=0
  fi
  prefix="anila-${day}-"
  shopt -s nullglob
  for name in "$out_dir"/"${prefix}"*; do
    base="$(basename "$name")"
    base="${base%.tar.gz}"
    num="${base#"$prefix"}"
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    if (( num > n )); then
      n=$num
    fi
  done
  shopt -u nullglob
  n=$((n + 1))
  printf '%s\n' "$n" > "$seq_file"
  printf '%s-%s\n' "$day" "$n"
}

release_seal_manifest() {
  local root="$1" version="$2" commit="$3" image_lines="${4:-}"
  local manifest tmp f rel hash
  manifest="$root/manifest.txt"
  tmp="$(mktemp)"
  {
    printf 'ANILA-MANIFEST 1\n'
    printf 'version %s\n' "$version"
    printf 'commit %s\n' "$commit"
    if [[ -n "$image_lines" && -f "$image_lines" ]]; then
      cat "$image_lines"
    fi
    while IFS= read -r f; do
      rel="${f#"$root"/}"
      case "$rel" in
        manifest.txt|manifest.sha256) continue ;;
      esac
      hash="$(sha256_file "$f")"
      printf 'file %s %s\n' "$hash" "$rel"
    done < <(find "$root" -type f | sort)
  } > "$tmp"
  mv -f "$tmp" "$manifest"
  (
    cd "$root"
    sha256sum manifest.txt > manifest.sha256
    chmod 644 manifest.sha256
  )
}

manifest_path_rejected() {
  local path="$1" seg
  [[ "$path" == /* ]] && return 0
  local IFS=/
  for seg in $path; do
    [[ "$seg" == ".." ]] && return 0
  done
  return 1
}

release_verify_bundle() {
  local root="$1"
  local manifest="$root/manifest.txt"
  local line hash path got ver f rel
  declare -A listed=()
  if [[ ! -f "$manifest" || ! -f "$root/manifest.sha256" ]]; then
    printf '出貨包缺少清單，拒絕更新。\n' >&2
    return 1
  fi
  if ! ( cd "$root" && sha256sum -c manifest.sha256 >/dev/null ); then
    printf '清單的 SHA256 不符，拒絕更新。\n' >&2
    return 1
  fi
  ver="$(awk '$1=="version" {print $2; exit}' "$manifest")"
  if [[ ! "$ver" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]+$ ]]; then
    printf '清單上的版本格式不對，拒絕更新。\n' >&2
    return 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == file\ * ]] || continue
    read -r _kind hash path <<<"$line"
    if manifest_path_rejected "$path"; then
      printf '清單路徑不合法，拒絕更新。\n' >&2
      return 1
    fi
    if [[ ! -f "$root/$path" ]]; then
      printf '清單記載的檔案不在：%s\n' "$path" >&2
      return 1
    fi
    got="$(sha256_file "$root/$path")"
    if [[ "$got" != "$hash" ]]; then
      printf '檔案 SHA256 不符：%s\n' "$path" >&2
      return 1
    fi
    listed["$path"]=1
  done < "$manifest"
  while IFS= read -r f; do
    rel="${f#"$root"/}"
    case "$rel" in
      manifest.txt|manifest.sha256) continue ;;
    esac
    if [[ -z "${listed[$rel]:-}" ]]; then
      printf '出貨包有檔案不在清單：%s\n' "$rel" >&2
      return 1
    fi
  done < <(find "$root" -type f | sort)
}

# 出貨包自己的 images.tsv（清單裡的 SHA256 已核對）與每一條 image 行逐欄比對。
# 服務、映像名、封存路徑、digest 都要對上。停寫入之前就要過；缺一條或重複都拒絕。
release_verify_image_catalog() {
  local root="$1" manifest tsv listed got line kind svc image digest archive alt extra
  local -A want_image=() want_archive=() seen=()
  manifest="$root/manifest.txt"
  tsv="$root/images.tsv"
  [[ -f "$manifest" ]] || {
    printf '出貨包缺少清單，拒絕更新。\n' >&2
    return 1
  }
  [[ -f "$tsv" ]] || {
    printf '出貨包缺少 images.tsv，拒絕更新。\n' >&2
    return 1
  }
  listed="$(awk '$1=="file" && $3=="images.tsv" { print $2; exit }' "$manifest")"
  got="$(sha256_file "$tsv")"
  if [[ -z "$listed" || "$listed" != "$got" ]]; then
    printf 'images.tsv 的 SHA256 尚未核對，拒絕更新。\n' >&2
    return 1
  fi
  while read -r svc image archive start; do
    [[ -n "$svc" ]] || continue
    want_image["$svc"]="$image"
    want_archive["$svc"]="$archive"
  done < <(
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ -z "${line//[[:space:]]/}" ]] && continue
      printf '%s\n' "$line"
    done < "$tsv"
  )
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == image\ * ]] || continue
    alt=""
    extra=""
    # 第六欄選填：manifest 雜湊。containerd 儲存載入後的映像 ID 是它。
    read -r kind svc image digest archive alt extra <<<"$line"
    if [[ -n "${extra:-}" || -z "$svc" || -z "$image" || -z "$digest" || -z "$archive" ]]; then
      printf '映像清單欄位不完整，拒絕更新。\n' >&2
      return 1
    fi
    if [[ ! "$digest" =~ ^sha256:[0-9A-Fa-f]+$ ]]; then
      printf '映像 %s 沒有 digest，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    if [[ -n "$alt" && ! "$alt" =~ ^sha256:[0-9A-Fa-f]+$ ]]; then
      printf '映像 %s 的 manifest 雜湊格式不對，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    if manifest_path_rejected "$archive"; then
      printf '映像封存路徑不合法，拒絕更新。\n' >&2
      return 1
    fi
    if [[ -z "${want_image[$svc]:-}" ]]; then
      printf '映像清單有清單外的服務 %s，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    if [[ "$image" != "${want_image[$svc]}" ]]; then
      printf '映像 %s 的 image 與出貨包 images.tsv 不符，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    if [[ "$archive" != "images/${want_archive[$svc]}.tar.gz" ]]; then
      printf '映像 %s 的封存路徑與出貨包 images.tsv 不符，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    if [[ -n "${seen[$svc]:-}" ]]; then
      printf '映像清單裡 %s 重複，拒絕更新。\n' "$svc" >&2
      return 1
    fi
    seen["$svc"]=1
  done < "$manifest"
  for svc in "${!want_image[@]}"; do
    if [[ -z "${seen[$svc]:-}" ]]; then
      printf '映像清單缺少 %s，拒絕更新。\n' "$svc" >&2
      return 1
    fi
  done
}

manifest_value() {
  awk -v k="$1" '$1==k {print $2; exit}' "$2"
}

banner_gate() {
  local count="$1" answer="${2-}"
  if [[ "$count" =~ ^[0-9]+$ ]] && (( count > 0 )); then
    return 0
  fi
  printf '目前沒有生效中的公告。請先到治理中心貼上「將於幾點更新」這類公告，讓同仁看得到。\n' >&2
  if [[ -z "$answer" ]]; then
    printf '仍要繼續嗎？[y/N] ' >&2
    read -r answer || true
  fi
  case "$answer" in
    y|Y) return 0 ;;
  esac
  printf '已取消。\n' >&2
  return 1
}

confirm_rollback_version() {
  local expected="$1" typed="${2-}"
  if [[ -z "$typed" ]]; then
    printf '請輸入要回到的版本 %s 以確認：' "$expected" >&2
    read -r typed || true
  fi
  if [[ "$typed" != "$expected" ]]; then
    printf '已取消。輸入的版本與 %s 不符。\n' "$expected" >&2
    return 1
  fi
}

rollback_needs_db_restore() {
  local before="$1" after="$2"
  [[ -n "$before" && "$before" != "$after" ]]
}

# 公開的 CSPKI CA 與 .env.example 可以進包。其餘 .env.*、密鑰、憑證私檔不行。
# secrets/ 底下只允許佔位檔（.gitignore、.gitkeep、README），其他檔一律拒絕。
_secret_placeholder() {
  case "$1" in
    .gitignore|.gitkeep|README) return 0 ;;
  esac
  return 1
}

_secret_name_rejected() {
  local path="$1" base
  base="$(basename -- "$path")"
  case "$base" in
    .env.example|cspki_ca_bundle.pem) return 1 ;;
  esac
  case "$base" in
    .env|.env.*|*.key|*.pem|*.p12|*.pfx) return 0 ;;
  esac
  case "$path" in
    */secrets/*)
      _secret_placeholder "$base" && return 1
      return 0
      ;;
    */share/backups/*) return 0 ;;
  esac
  return 1
}

release_assert_no_secrets() {
  local root="$1" hit f name
  hit=""
  while IFS= read -r f; do
    if _secret_name_rejected "$f"; then
      hit="${hit}${f}"$'\n'
    fi
  done < <(find "$root" -type f | sort)
  if [[ -n "$hit" ]]; then
    printf '打包結果含有不該帶的檔案：\n%s\n' "$hit" >&2
    return 1
  fi
  if [[ -f "$root/source.tar.gz" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      # 目錄項本身沒有位元組。secrets/ 底下只有佔位 .gitignore 時，git archive
      # 仍會列出 services/csp/secrets/ 這個目錄項；裡面的檔各自照規則判。
      [[ "$name" == */ ]] && continue
      if _secret_name_rejected "$name"; then
        printf '原始碼封存含有 .env、密鑰、憑證或備份，拒絕打包。\n' >&2
        return 1
      fi
    done < <(tar -tzf "$root/source.tar.gz")
    # grep -q 會讓 tar 收到 SIGPIPE。關 pipefail 才看得到「有找到」。
    if ( set +o pipefail
         tar -xOzf "$root/source.tar.gz" 2>/dev/null | grep -q 'PRIVATE KEY' ); then
      printf '原始碼封存含有 PRIVATE KEY，拒絕打包。\n' >&2
      return 1
    fi
  fi
}
