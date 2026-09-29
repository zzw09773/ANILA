#!/usr/bin/env bash
# 正式部署環境檢查。由 deploy-prod.sh 與 anila-update.sh source。
# 只印鍵名，不印值。不要在這裡 source .env，也不要 set -x。

_prod_env_trim() {
  local -n _slot="$1"
  local raw="${_slot}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  if [[ ${#raw} -ge 2 ]]; then
    local first="${raw:0:1}" last="${raw: -1}"
    if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
      raw="${raw:1:${#raw}-2}"
      raw="${raw#"${raw%%[![:space:]]*}"}"
      raw="${raw%"${raw##*[![:space:]]}"}"
    fi
  fi
  _slot="$raw"
}

# 守門判斷不了的值放進這個標記，受保護的鍵遇到就拒絕：變數展開、引號內的
# 反斜線（compose 會把 \' 當跳脫），以及引號沒在同一行結束（compose 允許跨行）。
_PROD_ENV_DYNAMIC=$'\x01dynamic'

# 照 compose 讀 .env 的方式解析（已用 docker compose config 逐項對過）：
# - 行尾 CR 去掉；`KEY=v`、`KEY = v`、`KEY: v`、`export KEY=v` 都算
# - 值先去前導空白；引號值只取到結束引號，引號內原樣保留，後面的註解不算
# - 沒加引號的值，「空格 + #」起是註解，tab 不算（compose 把 1<tab># c 整段當值）；
#   開頭就是 # 的值照樣是值（KEY= #abc 讀成 #abc）；尾端空白去掉
# - 沒加引號或雙引號的值含 $ 時 compose 會展開；引號值含 \ 或沒在同一行結束時
#   compose 的讀法和逐行解析不同。這三種都記成 _PROD_ENV_DYNAMIC
_prod_env_load_file() {
  local file="$1"
  local -n _dest="$2"
  local line key val lead q rest
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*[=:](.*)$ ]]; then
      key="${BASH_REMATCH[2]}"
      val="${BASH_REMATCH[3]}"
      lead="${val#"${val%%[![:space:]]*}"}"
      q="${lead:0:1}"
      rest="${lead:1}"
      if [[ "$q" == '"' || "$q" == "'" ]] && [[ "$rest" != *"$q"* || "$rest" == *'\'* ]]; then
        val="$_PROD_ENV_DYNAMIC"
      elif [[ "$q" == '"' || "$q" == "'" ]]; then
        val="${rest%%"$q"*}"
        if [[ "$q" == '"' && "$val" == *'$'* ]]; then
          val="$_PROD_ENV_DYNAMIC"
        fi
      else
        val="${lead%% #*}"
        val="${val%"${val##*[![:space:]]}"}"
        if [[ "$val" == *'$'* ]]; then
          val="$_PROD_ENV_DYNAMIC"
        fi
      fi
      _dest["$key"]="$val"
    fi
  done <"$file"
}

_prod_env_is_bad() {
  local key="$1"
  local -n _val="$2"
  case "$key" in
    ANILA_ALLOW_DEV_SECRET|CARD_DEV_TRUST_TEST_CA)
      [[ "${_val}" == "1" ]]
      ;;
    CARD_CA_BUNDLE_PATH)
      [[ "${_val}" == *dev-card-ca* || "${_val}" == *dev_ca* ]]
      ;;
    ANILA_AUTH_MODE)
      [[ -n "${_val}" && "${_val}" != "card-only" ]]
      ;;
    *)
      return 1
      ;;
  esac
}

# 檔案與目前 shell 任一邊不合就拒絕。缺 ANILA_AUTH_MODE 不算（compose 預設 card-only）。
prod_env_refuse() {
  local file="$1"
  local -A from_file=()
  local key val
  local -a keys=(
    ANILA_ALLOW_DEV_SECRET
    CARD_DEV_TRUST_TEST_CA
    CARD_CA_BUNDLE_PATH
    ANILA_AUTH_MODE
  )
  _prod_env_load_file "$file" from_file
  for key in "${keys[@]}" CSP_SERVICE_TOKEN CSP_BOOTSTRAP_TOKEN; do
    if [[ -v "from_file[$key]" && "${from_file[$key]}" == "$_PROD_ENV_DYNAMIC" ]]; then
      printf '拒絕部署：%s 不可用變數展開、反斜線或跨行值，請在 .env 直接寫值\n' "$key" >&2
      return 1
    fi
  done
  for key in "${keys[@]}"; do
    if [[ -v "from_file[$key]" ]]; then
      val="${from_file[$key]}"
      if _prod_env_is_bad "$key" val; then
        printf '拒絕部署：%s 不可用於正式部署\n' "$key" >&2
        return 1
      fi
    fi
    if [[ -v "$key" ]]; then
      val="${!key}"
      _prod_env_trim val
      if _prod_env_is_bad "$key" val; then
        printf '拒絕部署：%s 不可用於正式部署\n' "$key" >&2
        return 1
      fi
    fi
  done
  local -a retired=(CSP_SERVICE_TOKEN CSP_BOOTSTRAP_TOKEN)
  for key in "${retired[@]}"; do
    if [[ -v "from_file[$key]" && -n "${from_file[$key]}" ]]; then
      printf '拒絕部署：%s 已退役，請從 .env 刪除這一行\n' "$key" >&2
      return 1
    fi
    if [[ -v "$key" ]]; then
      val="${!key}"
      _prod_env_trim val
      if [[ -n "$val" ]]; then
        printf '拒絕部署：%s 已退役，請從 .env 刪除這一行\n' "$key" >&2
        return 1
      fi
    fi
  done
  return 0
}
