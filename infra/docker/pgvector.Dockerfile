# Self-built pgvector on current postgres:16-alpine.
# Do not FROM pgvector/pgvector:pg16 — that tag is the live stack DB image.
FROM postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685 AS build
USER root
ARG PGVECTOR_VERSION=0.8.1
RUN apk upgrade --no-cache libuuid libcrypto3 libssl3 \
 && apk add --no-cache su-exec \
 && rm -f /usr/local/bin/gosu \
 && ln -s /sbin/su-exec /usr/local/bin/gosu \
 && apk add --no-cache --virtual .build-deps git build-base \
 && git clone --branch "v${PGVECTOR_VERSION}" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
 && make -C /tmp/pgvector install with_llvm=no \
 && rm -rf /tmp/pgvector \
 && apk del .build-deps \
 && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
COPY --from=build / /
ENV LANG=en_US.utf8 \
    PG_MAJOR=16 \
    PG_VERSION=16.15 \
    PGDATA=/var/lib/postgresql/data \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EXPOSE 5432
STOPSIGNAL SIGINT
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
