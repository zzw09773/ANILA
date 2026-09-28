# Transaction pool in front of Postgres. Runtime services connect here.
# Migrations and backups stay on csp-db directly.
FROM alpine:3.22
RUN apk add --no-cache pgbouncer su-exec netcat-openbsd \
    && (id pgbouncer >/dev/null 2>&1 || adduser -D -H -u 70 pgbouncer) \
    && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid
COPY infra/pgbouncer/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
EXPOSE 5432
ENTRYPOINT ["/entrypoint.sh"]
