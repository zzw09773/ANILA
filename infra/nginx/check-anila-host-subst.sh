#!/bin/sh
# Render infra/nginx/anila.conf the way the official nginx image does.
#
# The image entrypoint (20-envsubst-on-templates.sh) runs envsubst on
# /etc/nginx/templates/*.template and substitutes only variables that
# exist in the container environment. Compose mounts anila.conf at
# /etc/nginx/templates/default.conf.template and passes ANILA_HOST.
# $host is not ${HOST}, so it stays an nginx variable.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
TEMPLATE="$ROOT/infra/nginx/anila.conf"
SAMPLE=${1:-anila.example.test}

if ! command -v envsubst >/dev/null 2>&1; then
  echo "envsubst is required (gettext)" >&2
  exit 2
fi

export ANILA_HOST="$SAMPLE"
defined=$(awk 'BEGIN { for (name in ENVIRON) printf "${%s} ", name }')
rendered=$(envsubst "$defined" < "$TEMPLATE")

printf '%s\n' "$rendered" | awk '
  $0 ~ /map \$host \$is_anila_host \{/ { grab=1 }
  grab { print }
  grab && $0 ~ /^\}/ { exit }
' > /tmp/anila-host-map-check.txt

map=$(cat /tmp/anila-host-map-check.txt)
rm -f /tmp/anila-host-map-check.txt

case "$map" in
  *'${ANILA_HOST}'*) echo "ANILA_HOST was not substituted" >&2; exit 1 ;;
esac
case "$map" in
  *"$SAMPLE"*) ;;
  *) echo "sample host missing from rendered map" >&2; exit 1 ;;
esac
case "$map" in
  *'map $host $is_anila_host'*) ;;
  *) echo "nginx \$host variable was rewritten" >&2; exit 1 ;;
esac
for token in '"localhost"' '"127.0.0.1"' '"::1"' '"csp"' '"router"'; do
  case "$map" in
    *"$token"*) ;;
    *) echo "map missing $token" >&2; exit 1 ;;
  esac
done
case "$map" in
  *'25[0-5]'*) ;;
  *) echo "IPv4 literal rule missing" >&2; exit 1 ;;
esac
case "$map" in
  *'[0-9a-f:.]'*) ;;
  *) echo "IPv6 literal rule missing" >&2; exit 1 ;;
esac
case "$map" in
  *'10.53.'*|*'172.16.'*|*'ncsist'*) echo "lab host leaked into the map" >&2; exit 1 ;;
esac

echo "ok ${SAMPLE}"
