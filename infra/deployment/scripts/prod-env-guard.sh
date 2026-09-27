#!/usr/bin/env bash
# 正式部署環境檢查。由 deploy-prod.sh 與 intranet-deploy.sh source。
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

_prod_env_load_file() {
  local file="$1"
  local -n _dest="$2"
  local line key val
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[2]}"
      val="${BASH_REMATCH[3]}"
      _prod_env_trim val
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
  return 0
}
