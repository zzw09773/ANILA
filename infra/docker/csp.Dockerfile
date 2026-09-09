# Build context is repo root (set in compose). Dockerfile paths are
# repo-relative — services/csp + apps/csp-governance-ui for the app,
# packages/anila-core for the central SDK the inspector endpoints need.
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
#
# Stage 1: Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /build
# 鎖檔是必要輸入,不是可選的:原本寫 `package-lock.json*`,那個 `*` 讓「鎖檔
# 不在建置脈絡裡」變成靜默成功(照樣 build,直接跟 registry 要當下最新版)。
COPY apps/csp-governance-ui/package.json apps/csp-governance-ui/package-lock.json ./
# `npm ci`(不是 `npm install`):install 在 package.json 與鎖檔不一致時會**重新
# 解析並改寫鎖檔**,凍結後那等於映像裡裝了什麼沒有人決定過;ci 則直接失敗。
# 代價是改 package.json 之後要在 host 端跑一次 `npm install` 更新鎖檔再 build。
RUN npm ci && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
COPY apps/csp-governance-ui/ ./
RUN npm run build && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Stage 2: Production
FROM python:3.13-slim
WORKDIR /app

# graphviz + fonts-noto-cjk: Studio Fix 2 (2026-05-18). CSP shells out
# to `dot -Tpng` from app.services.diagram_renderer to render
# Slide.diagram_dot when image_kind='diagram'. Noto CJK is required
# so DOT graphs with Traditional Chinese node labels render glyphs
# instead of tofu boxes.
#
# Round 2 fix-up (2026-05-19): originally landed in the wrong
# Dockerfile — there used to be a second, dead `services/csp/Dockerfile`
# that compose never built. Deleted 2026-08-06 (FAKE-CONTROLS #50) so the
# "fixed the file nobody builds" mistake cannot recur. **This** file is
# the CSP image; compose points here via
# `dockerfile: infra/docker/csp.Dockerfile`.
# curl: deploy-prod.sh cmd_verify runs `docker compose exec -T csp curl`
# against http://localhost:8000/health. F6 now fail-closes on a missing
# probe binary; studio already ships curl, CSP did not.
RUN apt-get update && apt-get install -y --no-install-recommends \
    graphviz \
    fonts-noto-cjk \
    curl \
    && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

# Install anila-core first (changes less often than backend code, so
# layer caching survives most builds). The package brings asyncpg +
# pgvector + the AgentScopedPgVectorStore the inspector uses, and the
# ``[rag]`` extra adds the parser stack (pymupdf4llm / python-docx /
# odfpy / striprtf / Pillow) needed by
# ``anila_core.ingestion.parsers.extract_text``, which CSP calls
# directly from POST /api/ingestion/chunking-preview.
#
# AgenticRAG/ in this branch is a pure starter template — production
# code (CSP, ingestion-worker, evaluator) imports parsers/vision from
# anila_core.* directly and does NOT install AgenticRAG at runtime.
COPY packages/anila-core /tmp/anila-core
RUN pip install --no-cache-dir '/tmp/anila-core[rag]' \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Install Python dependencies (CSP-specific)
COPY services/csp/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Copy backend code (includes scripts/: generate-jwt-keypair.py + init_db.py
# land in /app/scripts — no separate scripts COPY needed since §17.1 folded
# myCSPPlatform/scripts/ into services/csp/scripts/)
COPY services/csp/ ./

# Copy built frontend
COPY --from=frontend-build /build/dist /app/frontend-dist

# ── Swagger UI 靜態檔:vendored,不在 build 時抓 ────────────────────────────
# 2026-08-08 以前這裡是「build 時去 cdn.jsdelivr.net 抓兩個檔,失敗就
# `|| echo "Swagger UI download skipped (offline build)"`」。氣隙內重建映像時
# 那一定失敗,而 build **照樣成功**,只是 /docs 變成一個壞掉的頁面,沒有任何
# 錯誤訊息 —— 正是「靜默成功比報錯危險」那條教訓的形狀。
#
# 現在兩個檔直接進版控(`services/csp/app/static/`,由上面的
# `COPY services/csp/ ./` 帶進來),來源:swagger-ui-dist@5.18.2(Apache-2.0),
# 同時自 cdn.jsdelivr.net 與 unpkg.com 取得、逐位元組相同。
#
# ⚠ 2026-08-08 修訂(審查 I1):**這一段原本只驗檔案大小與 placeholder 標記**,
# 雜湊只寫在註解裡、build 從來沒算過。審查者用三個突變打穿它,三個全過:
# (a) 把 css 的位元組放進 js 的路徑、(b) 150KB 隨機位元組、(c) 真檔截斷到 120KB。
# 也就是「/docs 壞掉」只要換一種壞法,守衛照樣說 ok —— **一道不守的守衛,正是
# 本包第 0 條為 starlette 修掉的同一個形狀**。現在改成驗**內容**:雜湊是唯一
# 判準(順帶把大小與 placeholder 兩種情況也一起蓋住,錯了就是雜湊對不上)。
#
# 換版本時:換檔 → 重算 `sha256sum services/csp/app/static/*` → 改下面兩行。
# 對不上就當場 build 失敗,不會生出一個「看起來好了」的映像。
RUN set -eu; \
    printf '%s  %s\n' \
      c50b94bbc4f02394326fb7aed1f4fb693b3677f4b3d3344e0d6131808cbf281f app/static/swagger-ui-bundle.js \
      8f33d996025317049d4a9864f421eab2b2a247872f388026fa94c654913259e7 app/static/swagger-ui.css \
      > /tmp/swagger-ui.sha256; \
    if ! sha256sum -c --strict /tmp/swagger-ui.sha256; then \
      echo "FATAL: Swagger UI 靜態檔與 swagger-ui-dist@5.18.2 的雜湊對不上。" >&2; \
      echo "       檔案不見／是 placeholder／被截斷／換了版本都會走到這裡。" >&2; \
      echo "       修法:把 swagger-ui-dist@5.18.2 的真檔放回 services/csp/app/static/," >&2; \
      echo "       或換版本後同步更新本 Dockerfile 裡的兩個 sha256。" >&2; \
      exit 1; \
    fi; \
    rm -f /tmp/swagger-ui.sha256; \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

ENV DATABASE_URL=postgresql://csp:csp_password@postgres:5432/csp

# ── 降權:非 root 跑 (FAKE-CONTROLS #50) ────────────────────────────────────
# UID/GID **釘死成數字**,而且要滿足一條不變式:
#
#   這裡的 uid/gid  ==  services/ingestion-worker/Dockerfile 的 uid/gid
#                   ==  infra/deployment/scripts/fix-runtime-ownership.sh 的
#                       ANILA_RUNTIME_UID / ANILA_RUNTIME_GID
#
# 三者共用 share/uploads/ingestion 這顆 inode(csp 寫上傳原始檔、worker 寫回
# 影像;host 端所有權由那支腳本設定)。任一邊漂開就是單向壞掉,而且**沒有任何
# 機器檢查會告訴你** —— 兩個容器都會照常 healthy。改號碼時 grep 10001。
# (刻意不寫「要改幾個地方」:那種計數自己就會過時,而過時的計數比沒有更糟。)
#
# 用 groupadd 明確給 gid,而不是只靠 `useradd --uid`:useradd 的 gid 是
# 「挑一個還沒被佔的」。本樹既有的 anila-studio / asr-gateway 實測出來**確實**
# 也是 10001,但那是巧合不是宣告 —— base image 換一版、多裝一個會建群組的
# 套件,它就會變,而且不會有人發現。這裡要的是宣告。
#
# /app/logs 是**唯一**需要在映像裡就可寫的路徑:app/main.py 的 setup_logging()
# 在 lifespan 啟動時對 logs/csp.log 開 RotatingFileHandler,不可寫 = 服務起不來。
# 只處理這一個目錄,不做 `chown -R /app` (那會整包複製一層,映像肥一倍)。
# 其餘寫入路徑都在容器外:/var/anila/ingestion-uploads、/var/anila/attachments
# (bind mount,host 端所有權由上面那支腳本處理) 與 /tmp (world-writable)。
#
# ⚠ `chown -R` 的 `-R` 是必要的,不是順手加的。上一版寫 `chown anila:anila /app/logs`
# 只改目錄本身,而「目錄可寫」**不等於**「裡面那個檔可寫」:在 services/csp 底下跑過
# pytest / uvicorn 的樹會留下 services/csp/logs/csp.log,`COPY services/csp/ ./` 把它
# 以 root 搬進來,RotatingFileHandler 對既有檔案是 append 開啟 → PermissionError,
# **容器直接起不來**(2026-08-06 驗收實測)。根因由 .dockerignore 的 `**/logs/` 擋掉,
# 這裡是第二道:即使哪天 context 又漏進什麼,服務仍然起得來。
# 目錄照理是空的,`-R` 的成本是零。
RUN groupadd --gid 10001 anila \
 && useradd --create-home --uid 10001 --gid 10001 anila \
 && mkdir -p /app/logs \
 && chown -R anila:anila /app/logs \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
