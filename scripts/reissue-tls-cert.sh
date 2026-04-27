#!/bin/bash
# Reissue ANILA self-signed TLS cert (Sprint 6 X / A4).
#
# Generates server.{key,crt} into myCSPPlatform/docker/certs/, replacing the
# existing pair. Old key is moved to server.key.revoked-<timestamp> rather
# than deleted so ops can grep the host for any service that still has it
# pinned. Production deployments should swap in a CA-signed cert at this
# point — this script is the on-prem / dev fallback.
#
# Usage:
#   bash scripts/reissue-tls-cert.sh
#
# Optional env overrides:
#   ANILA_CERT_CN          common name (default: 172.16.120.35)
#   ANILA_CERT_SAN         comma-separated SANs (default sensible LAN list)
#   ANILA_CERT_DAYS        validity in days (default: 365 — short on purpose)

set -euo pipefail

CERTS_DIR="$(cd "$(dirname "$0")/.." && pwd)/myCSPPlatform/docker/certs"
KEY_PATH="$CERTS_DIR/server.key"
CRT_PATH="$CERTS_DIR/server.crt"

CN="${ANILA_CERT_CN:-172.16.120.35}"
DAYS="${ANILA_CERT_DAYS:-365}"
SAN="${ANILA_CERT_SAN:-DNS:localhost,DNS:anila.local,IP:127.0.0.1,IP:172.16.120.35}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

mkdir -p "$CERTS_DIR"

if [ -f "$KEY_PATH" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  REVOKED_PATH="$KEY_PATH.revoked-$STAMP"
  yellow "[!] Existing server.key 偵測到，搬到 $REVOKED_PATH"
  mv "$KEY_PATH" "$REVOKED_PATH"
fi
if [ -f "$CRT_PATH" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  yellow "[!] Existing server.crt 搬到 $CRT_PATH.revoked-$STAMP"
  mv "$CRT_PATH" "$CRT_PATH.revoked-$STAMP"
fi

# 用 openssl req with -addext 直接寫 SAN，避免額外 .cnf 檔。
green "[*] Generating new self-signed cert"
green "    CN  = $CN"
green "    SAN = $SAN"
green "    days = $DAYS"

openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
  -keyout "$KEY_PATH" -out "$CRT_PATH" \
  -subj "/C=TW/ST=Taiwan/L=Taipei/O=ANILA Platform/CN=$CN" \
  -addext "subjectAltName=$SAN" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" \
  >/dev/null 2>&1

chmod 600 "$KEY_PATH"
chmod 644 "$CRT_PATH"

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
  4. 確認服務正常後刪掉 *.revoked-* 備份檔
EOF
