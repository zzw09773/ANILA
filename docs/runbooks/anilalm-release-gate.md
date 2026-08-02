# ANILA LM 暫時關閉閘門 — 重開手順

> 本 release（高層預覽）ANILA LM **尚未就緒**，對外三個門暫時關上。
> `anilalm` 容器**繼續跑**（compose 不動、不 stop），只關入口。
> 重開不必考古：改三個旗標／一段 nginx，再 recreate nginx。

## 為什麼是 503

對外 `/anilalm/` 回 **503 + 短 zh-TW 維護頁**，不用 404。
404 會讓好奇的人以為「功能不存在」；503 表示「尚未開放」。

## 三個開關（全部打開才算重開）

| # | 表面 | 檔案 | 現況（關閉） | 重開改成 |
|---|---|---|---|---|
| 1 | nginx 對外路徑 | `infra/nginx/anila.conf` | `ANILA_LM_NGINX_GATE = closed`：`location ~* ^/anilalm` → 503 | 刪掉兩個 server block（443／4443）裡的 `~* ^/anilalm` 503 區塊，還原檔內註解的 `location = /anilalm` + `location /anilalm` proxy_pass 對 |
| 2 | ANILA shell 導覽 | `apps/anila-shell/src/anilalmReleaseGate.js` | `ANILA_LM_ENTRY_ENABLED = false`（列還在，標「即將推出」、不可點） | `ANILA_LM_ENTRY_ENABLED = true` |
| 3 | 治理中心平台連結 | `apps/csp-governance-ui/src/utils/anilalmReleaseGate.js` | `ANILA_LM_LINK_VISIBLE = false`（儀表板／服務登記／服務存取都不渲染） | `ANILA_LM_LINK_VISIBLE = true` |

## 操作步驟（本機 `-p anila-restart`）

```bash
cd ~/桌面/ANILA/anila-restart-20260729/ANILA   # 或對應 worktree

# 1) 還原 nginx 區塊（見上表 #1）後強制 recreate —— reload 不夠
#    （bind-mount 綁 inode；git 改檔＝新 inode，容器仍握舊檔）
docker compose -p anila-restart up -d --force-recreate nginx
# ⚠ 不要 docker cp 進 conf.d/，會變成第二份設定 → resolver 重複宣告。

# 2) shell / 治理中心旗標改 true 後重建映像（治理中心在 csp 映像裡）
docker compose -p anila-restart build anila-ui csp
docker compose -p anila-restart up -d anila-ui csp
docker compose -p anila-restart up -d --force-recreate nginx
```

## 驗收

```bash
# ANILA LM 應回到 200 text/html（或 301→/anilalm/）
curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1/anilalm/
curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1/anilalm

# 其他四個入口不得被波及
for p in / /anila/ /asr/health /router/health; do
  printf '%-16s ' "$p"
  curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' "https://127.0.0.1$p"
done
```

- Shell：側欄「我的知識庫」恢復為可點連結（無「即將推出」）。
- 治理中心：儀表板「平台 · 外部工具」再次出現 ANILA LM 卡片。

## 關閉時不該做的事

- 不要 `docker compose stop anilalm` 或從 compose 刪服務。
- 不要只 `nginx -s reload` 就以為 anila.conf 已生效。
- 不要刪掉 shell 的「我的知識庫」列——只切旗標。
