#!/bin/sh
# Generate squid.conf from ANILA_FUNCTIONS_EGRESS_ALLOWLIST and exec squid.
#
# ALLOWLIST format: comma-separated `host:port` entries.
# Examples: "csp:8000,router:9000,internal-lint.intra:443"
#
# Output: one ACL block per entry plus an http_access allow rule.

set -e

CONF=/etc/squid/squid.conf
TMPL=/etc/squid/squid.conf.template
ALLOW="${ANILA_FUNCTIONS_EGRESS_ALLOWLIST:-}"

ACL_LINES=""
HTTP_RULES=""
i=0

# Split on commas
OLD_IFS="$IFS"
IFS=,
for entry in $ALLOW; do
    IFS="$OLD_IFS"
    host="${entry%:*}"
    port="${entry##*:}"
    [ -z "$host" ] && continue
    [ -z "$port" ] && continue
    label="entry${i}"
    ACL_LINES="${ACL_LINES}acl ${label}_host dstdomain ${host}\n"
    ACL_LINES="${ACL_LINES}acl ${label}_port port ${port}\n"
    HTTP_RULES="${HTTP_RULES}http_access allow ${label}_host ${label}_port\n"
    i=$((i + 1))
    IFS=,
done
IFS="$OLD_IFS"

awk -v acls="$ACL_LINES" -v rules="$HTTP_RULES" '
    /__ALLOWLIST_PLACEHOLDER__/ {
        gsub(/\\n/, "\n", acls)
        gsub(/\\n/, "\n", rules)
        printf "%s\n%s", acls, rules
        next
    }
    { print }
' "$TMPL" > "$CONF"

echo "[anila-egress] Generated squid.conf with $i allowlisted entries" >&2

exec squid -N -f "$CONF"
