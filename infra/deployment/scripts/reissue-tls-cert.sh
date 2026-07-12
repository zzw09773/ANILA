#!/bin/bash
# Reissue ANILA self-signed TLS cert (Sprint 6 X / A4).
#
# Generates server.{key,crt} into the external ANILA TLS state directory,
# replacing the existing pair. Old key is moved to server.key.revoked-<timestamp> rather
# than deleted so ops can grep the host for any service that still has it
# pinned. Production deployments should swap in a CA-signed cert at this
# point — this script is the on-prem / dev fallback.
#
# Usage:
#   bash infra/deployment/scripts/reissue-tls-cert.sh
#
# Optional env overrides:
#   ANILA_STATE_DIR       external state root (default: XDG/HOME state dir)
#   ANILA_TLS_CERTS_DIR   external cert directory (default: $ANILA_STATE_DIR/tls)
#   ANILA_CERT_CN          common name (default: 172.16.120.35)
#   ANILA_CERT_SAN         comma-separated SANs (default sensible LAN list)
#   ANILA_CERT_DAYS        validity in days (default: 365 — short on purpose)

set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
env_value() {
  grep -E "^$1=" "$REPO_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2- || true
}
if [ -n "${XDG_STATE_HOME:-}" ]; then
  DEFAULT_STATE_DIR="$XDG_STATE_HOME/anila"
elif [ -n "${HOME:-}" ]; then
  DEFAULT_STATE_DIR="$HOME/.local/state/anila"
else
  DEFAULT_STATE_DIR="/var/lib/anila"
fi
SAVED_STATE_DIR="$(env_value ANILA_STATE_DIR)"
SAVED_CERTS_DIR="$(env_value ANILA_TLS_CERTS_DIR)"
[ -z "${ANILA_STATE_DIR:-}" ] || [ -z "$SAVED_STATE_DIR" ] || [ "$ANILA_STATE_DIR" = "$SAVED_STATE_DIR" ] || {
  echo "ANILA_STATE_DIR conflicts with existing .env" >&2; exit 1;
}
[ -z "${ANILA_TLS_CERTS_DIR:-}" ] || [ -z "$SAVED_CERTS_DIR" ] || [ "$ANILA_TLS_CERTS_DIR" = "$SAVED_CERTS_DIR" ] || {
  echo "ANILA_TLS_CERTS_DIR conflicts with existing .env" >&2; exit 1;
}
ANILA_STATE_DIR="${ANILA_STATE_DIR:-$SAVED_STATE_DIR}"
ANILA_STATE_DIR="${ANILA_STATE_DIR:-$DEFAULT_STATE_DIR}"
CERTS_DIR="${ANILA_TLS_CERTS_DIR:-$SAVED_CERTS_DIR}"
CERTS_DIR="${CERTS_DIR:-$ANILA_STATE_DIR/tls}"
command -v realpath >/dev/null 2>&1 || {
  echo "realpath is required to enforce the external TLS-key boundary" >&2
  exit 1
}
REPO_REAL="$(realpath -m -- "$REPO_ROOT")"
STATE_LEX="$(realpath -ms -- "$ANILA_STATE_DIR")"
STATE_REAL="$(realpath -m -- "$ANILA_STATE_DIR")"
[ "$STATE_LEX" = "$STATE_REAL" ] || { echo "ANILA_STATE_DIR must not contain symlinks" >&2; exit 1; }
case "$STATE_REAL" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
    echo "unsafe ANILA_STATE_DIR: $STATE_REAL" >&2; exit 1 ;;
  "$REPO_REAL"|"$REPO_REAL"/*) echo "ANILA_STATE_DIR must be outside the repository" >&2; exit 1 ;;
esac
CERTS_LEX="$(realpath -ms -- "$CERTS_DIR")"
CERTS_DIR="$(realpath -m -- "$CERTS_DIR")"
[ "$CERTS_LEX" = "$CERTS_DIR" ] || { echo "ANILA_TLS_CERTS_DIR must not contain symlinks" >&2; exit 1; }
[ "$CERTS_DIR" = "$STATE_REAL/tls" ] || { echo "ANILA_TLS_CERTS_DIR must equal $STATE_REAL/tls" >&2; exit 1; }
KEY_PATH="$CERTS_DIR/server.key"
CRT_PATH="$CERTS_DIR/server.crt"

CN="${ANILA_CERT_CN:-172.16.120.35}"
DAYS="${ANILA_CERT_DAYS:-365}"
SAN="${ANILA_CERT_SAN:-DNS:localhost,DNS:anila.local,DNS:anila.ai.ncsist.org.tw,DNS:n8n.ai.ncsist.org.tw,DNS:gitlab.ai.ncsist.org.tw,DNS:code.ai.ncsist.org.tw,IP:127.0.0.1,IP:172.16.120.35}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

mkdir -p "$CERTS_DIR"
chmod 700 "$CERTS_DIR"
STAGING="$(mktemp -d "$CERTS_DIR/.reissue.XXXXXX")"
trap 'rm -rf -- "${STAGING:-}"' EXIT
NEW_KEY="$STAGING/server.key"
NEW_CRT="$STAGING/server.crt"

# 用 openssl req with -addext 直接寫 SAN，避免額外 .cnf 檔。
green "[*] Generating new self-signed cert"
green "    CN  = $CN"
green "    SAN = $SAN"
green "    days = $DAYS"

openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
  -keyout "$NEW_KEY" -out "$NEW_CRT" \
  -subj "/C=TW/ST=Taiwan/L=Taipei/O=ANILA Platform/CN=$CN" \
  -addext "subjectAltName=$SAN" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" \
  >/dev/null 2>&1

chmod 600 "$NEW_KEY"; chmod 644 "$NEW_CRT"
openssl x509 -in "$NEW_CRT" -noout -checkend 86400 >/dev/null 2>&1 \
  || { red "new certificate expires within 24 hours"; exit 1; }
for HOST in anila.ai.ncsist.org.tw n8n.ai.ncsist.org.tw gitlab.ai.ncsist.org.tw code.ai.ncsist.org.tw; do
  openssl x509 -in "$NEW_CRT" -noout -checkhost "$HOST" >/dev/null 2>&1 \
    || { red "new certificate SAN is missing $HOST"; exit 1; }
done
CERT_PUB="$(openssl x509 -in "$NEW_CRT" -noout -pubkey | sha256sum | cut -d' ' -f1)"
KEY_PUB="$(openssl pkey -in "$NEW_KEY" -pubout | sha256sum | cut -d' ' -f1)"
[ "$CERT_PUB" = "$KEY_PUB" ] || { red "new certificate and key do not match"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$STATE_REAL/tls-archive/$STAMP"
mkdir -p "$ARCHIVE"
chmod 700 "$STATE_REAL/tls-archive" "$ARCHIVE"
[ -f "$KEY_PATH" ] && { cp -- "$KEY_PATH" "$ARCHIVE/server.key"; chmod 600 "$ARCHIVE/server.key"; }
[ -f "$CRT_PATH" ] && cp -- "$CRT_PATH" "$ARCHIVE/server.crt"
install -m 600 "$NEW_KEY" "$CERTS_DIR/server.key.next"
install -m 644 "$NEW_CRT" "$CERTS_DIR/server.crt.next"
if ! mv -f -- "$CERTS_DIR/server.key.next" "$KEY_PATH" || \
   ! mv -f -- "$CERTS_DIR/server.crt.next" "$CRT_PATH"; then
  [ -f "$ARCHIVE/server.key" ] && cp -- "$ARCHIVE/server.key" "$KEY_PATH" || rm -f -- "$KEY_PATH"
  [ -f "$ARCHIVE/server.crt" ] && cp -- "$ARCHIVE/server.crt" "$CRT_PATH" || rm -f -- "$CRT_PATH"
  red "certificate install failed; previous pair restored"
  exit 1
fi
rm -rf -- "$STAGING"; STAGING=""; trap - EXIT

green "[+] 新憑證已產生:"
green "    $CRT_PATH"
green "    $KEY_PATH (mode 600)"
echo
green "[+] Fingerprint:"
openssl x509 -in "$CRT_PATH" -noout -fingerprint -sha256

cat <<'EOF'

下一步：
  1. docker compose restart nginx     # 載入新憑證
  2. curl -sk -I https://localhost/health | head -1
  3. 把新 fingerprint 公告給 team / client SDK 維護者
  4. 確認服務正常後，依保留政策處理 $ANILA_STATE_DIR/tls-archive 內舊金鑰
EOF
