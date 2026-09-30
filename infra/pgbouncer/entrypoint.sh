#!/bin/sh
# Write a transaction-pool config from the password already used by CSP.
# auth_type=plain is intentional. The pooler must present this password to
# Postgres, so the userlist cannot be only an md5 hash. The listener is
# not published; it is reachable on the compose network only.
set -eu

user="${PGBOUNCER_USER:-csp_app}"
password="${PGBOUNCER_PASSWORD:?PGBOUNCER_PASSWORD is required}"
db="${PGBOUNCER_DB:-csp}"
host="${PGBOUNCER_HOST:-csp-db}"

mkdir -p /etc/pgbouncer
umask 077
printf '"%s" "%s"\n' "$user" "$password" > /etc/pgbouncer/userlist.txt

cat > /etc/pgbouncer/pgbouncer.ini <<EOF
[databases]
${db} = host=${host} port=5432 dbname=${db}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
auth_type = plain
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 40
min_pool_size = 5
reserve_pool_size = 10
server_reset_query =
ignore_startup_parameters = extra_float_digits,options
admin_users = ${user}
stats_users = ${user}
EOF

chown -R pgbouncer:pgbouncer /etc/pgbouncer
# 不寫 pidfile：前景跑、容器裡只有這一個程序。舊版寫在 /tmp，容器 restart 後
# 檔案還在，pgbouncer 以為另一份在跑而拒絕啟動，只能 recreate 才救得回來。
rm -f /tmp/pgbouncer.pid
exec su-exec pgbouncer pgbouncer /etc/pgbouncer/pgbouncer.ini
