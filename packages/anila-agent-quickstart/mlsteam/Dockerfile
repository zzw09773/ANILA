# MLSteam lab image. Install is offline: mlsteam/build.sh passes
# --network=none, and the pip steps below also set network none.
# Digest is the local python:3.13-slim RepoDigest. Do not float the tag.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS install

RUN useradd --create-home --uid 10001 --user-group --shell /bin/bash lab \
    && mkdir -p /etc/anila /app \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app

COPY IMAGE_VERSION /etc/anila/lab-image-version
COPY agent.py server.py platform_io.py llm.py README.md /app/

RUN --mount=type=bind,source=requirements.lock,target=/tmp/requirements.lock,readonly \
    --mount=type=bind,source=mlsteam/requirements-jupyter.lock,target=/tmp/requirements-jupyter.lock,readonly \
    --mount=type=bind,source=mlsteam/build/wheelhouse,target=/wheelhouse,readonly \
    --network=none \
    python -m pip install --no-index --find-links=/wheelhouse \
        --require-hashes --only-binary=:all: --no-cache-dir \
        -r /tmp/requirements.lock \
    && python -m pip install --no-index --find-links=/wheelhouse \
        --require-hashes --only-binary=:all: --no-cache-dir \
        -r /tmp/requirements-jupyter.lock \
    && python -m pip check \
    && chown -R lab:lab /app \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

ARG LAB_IMAGE_VERSION

ENV ANILA_LAB_IMAGE_VERSION=${LAB_IMAGE_VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENT_PORT=8200

LABEL org.anila.lab-image-version="${LAB_IMAGE_VERSION}"

# Symantec DCS can inject /var/lib/sdcssagent and /run/sisidsdaemon.pid into
# any RUN layer, so every RUN ends with that cleanup (later layers cannot fix it).
RUN useradd --create-home --uid 10001 --user-group --shell /bin/bash lab \
    && mkdir -p /etc/anila /app \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY --from=install /usr/local /usr/local
COPY --from=install /etc/anila/lab-image-version /etc/anila/lab-image-version
COPY --from=install /app /app
COPY run.sh /app/run.sh

RUN test "$(tr -d '[:space:]' < /etc/anila/lab-image-version)" = "${ANILA_LAB_IMAGE_VERSION}" \
    && chmod 755 /app/run.sh \
    && chown -R lab:lab /app \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app
USER lab
EXPOSE 8200
CMD ["sleep", "infinity"]
