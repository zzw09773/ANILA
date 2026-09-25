# ANILA LM 暫時關閉閘門 — 重開手順

> 本 release（高層預覽）ANILA LM **尚未就緒**，對外的門暫時關上。
> `anilalm` 容器**繼續跑**（compose 不動、不 stop），只關入口。
> 重開不必考古：改三個旗標／一段 nginx，再 rebuild + recreate。

## 為什麼是 503

對外 `/anilalm/` 回 **503 + 短 zh-TW 維護頁**，不用 404。
404 會讓好奇的人以為「功能不存在」；503 表示「尚未開放」。

## 四個開關（全部打開才算重開）

| # | 表面 | 檔案 | 現況（關閉） | 重開改成 |
|---|---|---|---|---|
| 1 | nginx 對外路徑 | `infra/nginx/anila.conf` | `ANILA_LM_NGINX_GATE = closed`：`location ~* ^/anilalm` → 503 | 刪掉兩個 server block（443／4443）裡的 `~* ^/anilalm` 503 區塊，還原檔內註解的 `location = /anilalm` + `location /anilalm` proxy_pass 對 |
| 2 | ANILA shell 導覽 | `apps/anila-shell/src/anilalmReleaseGate.js` | `ANILA_LM_ENTRY_ENABLED = false`（列還在，標「即將推出」、不可點） | `ANILA_LM_ENTRY_ENABLED = true` |
| 3 | 治理中心儀表板卡片 | `apps/csp-governance-ui/src/utils/anilalmReleaseGate.js` | `ANILA_LM_LINK_VISIBLE = false`（儀表板「平台 · 外部工具」不渲染；**服務登記／服務存取照列，只標「未開放」**） | `ANILA_LM_LINK_VISIBLE = true` |
| 4 | **API 回應本身** | `services/csp/app/services/anilalm_release_gate.py` | `ANILA_LM_RELEASED = False`：`GET /api/services`、`/api/platform-links` 的**使用者面**清單不列 ANILA LM；`POST /api/services/{id}/launch` 回 503 | `ANILA_LM_RELEASED = True` |

### 為什麼需要第 4 道（2026-08-02 補上）

1、2、3 擋的都是「瀏覽得到的門」。shell 的「專案入口」抽屜是照
`GET /api/services` 畫的 —— 種子在**全新資料庫**會把 ANILA LM 建成
`is_active=True`，於是抽屜裡會出現一張看起來完全正常、按下去卻走到 503 維護頁
的卡片；而且任何人只要知道 service id 就打得到 `POST …/launch`。
第 4 道關的就是這一面。

⚠ **第 4 道不關管理面。** `include_inactive=true` 的管理清單（治理中心「服務登記」
與「服務存取」用的就是它）照常回傳 ANILA LM，單筆 CRUD 也照常。
閘門關的是「可用」，不是「可管理」——把列從管理員眼前拿掉，他就沒辦法停用／
編輯／刪除它（08-02 踩過：當時服務登記在前端濾掉整列，要停用只能手打 API）。

## 操作步驟（本機 `-p anila`）

```bash
cd ~/桌面/ANILA/anila-restart-20260729/ANILA   # 或對應 worktree

# 1) 還原 nginx 區塊（見上表 #1）後強制 recreate —— reload 不夠
#    （bind-mount 綁 inode；git 改檔＝新 inode，容器仍握舊檔）
docker compose -p anila up -d --force-recreate nginx
# ⚠ 不要 docker cp 進 conf.d/，會變成第二份設定 → resolver 重複宣告。

# 2) shell / 治理中心 / 後端旗標改 true 後重建映像
#    （治理中心與後端閘門都在 csp 映像裡；shell 在 anila-ui）
docker compose -p anila build anila-ui csp
docker compose -p anila up -d anila-ui csp
docker compose -p anila up -d --force-recreate nginx
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

```bash
# API 面：使用者面清單應重新列出 ANILA LM，launch 不再 503
#（$TOK = 任一已登入使用者的 access token）
curl -sk -H "Authorization: Bearer $TOK" https://127.0.0.1/api/services \
  | python3 -c 'import json,sys;print([r["name"] for r in json.load(sys.stdin)])'
```

- Shell：側欄「我的知識庫」恢復為可點連結（無「即將推出」）。
- Shell：「專案入口」抽屜重新出現 ANILA LM 卡片，按下去真的開得起來。
- 治理中心：儀表板「平台 · 外部工具」再次出現 ANILA LM 卡片。
- 治理中心：服務登記／服務存取那一列的「未開放」標籤消失（列本來就一直在）。

## 關閉時不該做的事

- 不要 `docker compose stop anilalm` 或從 compose 刪服務。
- 不要只 `nginx -s reload` 就以為 anila.conf 已生效。
- 不要刪掉 shell 的「我的知識庫」列——只切旗標。
- **不要靠手動把 `is_active` 設成 false 當閘門。** 那是資料不是程式碼：
  全新資料庫種子一跑就回到 `is_active=True`，閘門等於沒關。
- **不要在治理中心的管理頁把整列濾掉。** 管理員看不到那一列，就管不動它。
