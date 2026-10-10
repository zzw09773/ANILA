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

export ANILA_HOST="$SAMPLE"

# nginx 映像的 20-envsubst-on-templates.sh 只代「環境裡存在的」變數。
# 有 gettext 時走同一個 envsubst。沒有時用同等規則：只換
# $NAME／${NAME}，而且 NAME 必須在環境裡。$host 因此留著。
if command -v envsubst >/dev/null 2>&1; then
  defined=$(awk 'BEGIN { for (name in ENVIRON) printf "${%s} ", name }')
  rendered=$(envsubst "$defined" < "$TEMPLATE")
else
  rendered=$(python3 - "$TEMPLATE" <<'PY'
import os, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

def repl(match):
    name = match.group(1) or match.group(2)
    if name in os.environ:
        return os.environ[name]
    return match.group(0)

sys.stdout.write(pattern.sub(repl, text))
PY
)
fi

printf '%s\n' "$rendered" | awk '
  $0 ~ /map \$host \$anila_named_host \{/ { grab=1 }
  grab { print }
  grab && seen && $0 ~ /^\}/ { exit }
  grab && $0 ~ /\$is_anila_host \{/ { seen=1 }
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
  *'map $host $anila_named_host'*) ;;
  *) echo "nginx \$host variable was rewritten" >&2; exit 1 ;;
esac
case "$map" in
  *'$is_anila_host'*) ;;
  *) echo "combined host map missing" >&2; exit 1 ;;
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
  *'([0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}'*) ;;
  *) echo "IPv6 literal rule missing" >&2; exit 1 ;;
esac
case "$map" in
  *'::ffff:'*) ;;
  *) echo "IPv4-mapped IPv6 rule missing" >&2; exit 1 ;;
esac
case "$map" in
  *'10.53.'*|*'172.16.'*|*'ncsist'*) echo "lab host leaked into the map" >&2; exit 1 ;;
esac

echo "ok ${SAMPLE}"
