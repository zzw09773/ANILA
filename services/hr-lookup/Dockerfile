# 人資查詢。只裝資安放行的那五個 wheel，thin mode，沒有 Oracle client。
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
COPY wheelhouse ./wheelhouse

# wheelhouse 多一個檔、雜湊不合、或 requirements 沒有對上的 hash，建置就停。
RUN python -c "from pathlib import Path; \
sums=Path('wheelhouse/SHA256SUMS').read_text(encoding='utf-8').splitlines(); \
names={line.split()[1] for line in sums if line.strip() and not line.startswith('#')}; \
files={p.name for p in Path('wheelhouse').glob('*') if p.name != 'SHA256SUMS'}; \
assert files == names, (files, names)" \
 && (cd wheelhouse && sha256sum -c SHA256SUMS) \
 && pip install --no-index --find-links wheelhouse --require-hashes -r requirements.txt \
 && rm -rf wheelhouse /root/.cache /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY server.py ./

RUN groupadd --gid 10007 hrlookup \
 && useradd --uid 10007 --gid 10007 --create-home --shell /usr/sbin/nologin hrlookup \
 && chown hrlookup:hrlookup /app/server.py \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

USER hrlookup

EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health')"

CMD ["python", "server.py"]
