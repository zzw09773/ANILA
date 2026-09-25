# 部署清單 — 2026-08-01 收工批次(本機 `-p anila`)

> 為什麼有這張表:2026-07-31 踩過「前端合併六包、映像 17 小時沒重建」。
> 合併完 ≠ 部署完。**每次合併後回來對這張表**,不要憑印象決定要重建誰。

## 一、原始碼 → 映像 的實際對應(從 Dockerfile 的 COPY 反查,不是從直覺)

| 改到這棵樹 | 要重建的服務 |
|---|---|
| `services/csp/` | `csp` |
| `apps/csp-governance-ui/` | **`csp`**(治理中心是 csp 映像的多階段前端,**不是** anila-ui) |
| `packages/anila-core/` | **`csp`、`ingestion-worker`、`router`** ← 三個都包 |
| `apps/anila-shell/` | `anila-ui` |
| `apps/anilalm/` | `anilalm` |
| `services/anila-studio/`、`services/pptx-renderer/`、`services/asr-*/` | 同名服務 |
| `packages/anila-agent/` | **本機沒有對應容器**(agent 跑在 MLSteam),不必重建 |

⚠ `packages/anila-core` 那一列是最容易漏的:附件解析器
(`anila_core/ingestion/parser_registry.py`)同時服務「聊天室附件」與「知識庫匯入」,
只重建 csp 會出現「聊天室讀得懂、知識庫讀不懂」的分裂。

## 二、本批次(08-01)實際要重建的

08-01 08:16(csp 映像時間)之後動到:`services/csp` 29 次、`packages/anila-core` 23 次、
`apps/csp-governance-ui` 13 次、`apps/anila-shell` 3 次。

→ **`csp`、`anila-ui`、`ingestion-worker`、`router`** 四個。

(`ingestion-worker` 映像停在 07-29 12:01、`router` 停在 07-31 11:32,兩個都已落後 anila-core。)

## 三、執行順序

```bash
cd ~/桌面/ANILA/anila-restart-20260729/ANILA

# 1) 建映像。⚠ 平行建置的錯誤訊息不會說是哪個服務,一個一個建比較好抓
docker compose -p anila build csp anila-ui ingestion-worker router

# 2) 起容器。⚠ 一定要帶 ASR 的 CPU overlay,否則 nvidia driver 錯誤會中斷整批啟動
docker compose -p anila \
  -f compose.yaml -f infra/compose/asr-cpu.yml --profile asr up -d

# 3) ⚠ 重新載入 nginx。upstream 的 DNS 只在載入設定時解析一次,
#    recreate 任何服務都會讓 nginx 打到舊 IP → 全站 502 但容器全綠
docker compose -p anila exec nginx nginx -s reload
```

## 四、驗證(要驗行為,不是只看 status code)

五個入口都要**看 Content-Type**——SPA 的 catch-all 會用 `200 text/html` 假裝端點沒被保護:

```bash
for p in / /anila/ /anilalm/ /asr/health /router/health; do
  printf '%-16s ' "$p"
  curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' "https://localhost$p"
done
```

其他必驗:

- alembic head 仍是 `r1_0031`(schema 沒被意外推進)
- **csp 容器沒裝 `curl`** → 驗內部端點要用
  `docker exec <csp> python3 -c "import httpx; ..."`,用 curl 會回空,是假陰性
- 知識庫匯入實際丟一個 `.dcm` 進去,確認 ingestion-worker 用的是新解析器
  (只重建 csp 的話這一項會過不了——這就是第一節那個警告的驗收點)
