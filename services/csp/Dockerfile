# myCSPPlatform backend — build context: myCSPPlatform/backend
FROM python:3.11-slim

WORKDIR /app

# graphviz + fonts-noto-cjk: Studio Fix 2 (2026-05-18) — `dot -Tpng`
# renders Slide.diagram_dot for image_kind='diagram'. Noto CJK font is
# required so DOT graphs with Traditional Chinese labels (e.g. node
# names "監督代理") render without tofu boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    graphviz \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m -u 1000 csp && \
    mkdir -p /app/logs && \
    chown -R csp:csp /app
USER csp

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
