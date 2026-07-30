# 手動三檢查 — 何時跑、怎麼跑

擁有者裁定：**不要 CI**（一人維運太辛苦）。本樹沒有 GitHub workflows；
`infra/ci/` 與 `infra/checks/` 裡的腳本是**你覺得該看的時候才跑**的工具，
不擋 commit、不擋 merge、不擋部署。

入口：

```bash
# 全部（結尾有三行摘要）
bash infra/checks/run-all.sh

# 單項
bash infra/checks/run-all.sh orm
bash infra/checks/run-all.sh contrast
bash infra/checks/run-all.sh zh
```

Exit code：`0` 乾淨、`3` 有發現（自己決定要不要修）、`1` 檢查壞了、`2` 用法錯。

依賴：只用專案／本機既有的 Python（標準庫 + 既有 venv 裡的
SQLAlchemy／alembic／psycopg2）。**不新增**要在內網主機另裝的套件。
ORM 檢查會借舊樹 `services/csp/.venv`（或設 `CHECKS_PYTHON`）；
scratch DB 預設連本機已在跑的 throwaway Postgres（`127.0.0.1:55441`，
可用 `SCRATCH_*` 覆寫）。**絕不**對 `anila-restart-csp-db-1` 跑 alembic。

---

## 1. ORM ↔ 真 Postgres schema

**何時跑**

- 改了 `services/csp/app/models/**`
- 新增／改寫 alembic migration（尤其 `r1_*`）
- 懷疑「程式以為有欄、庫沒有」或反過來
- 發版前若本月動過 schema，跑一次就夠

**做什麼**

對 scratch DB（`alembic upgrade head` 乾淨庫）比對 ORM metadata：
缺表／缺欄／型別明顯不符／可空性分歧／UNIQUE 背後缺索引／缺 FK；
另有 policy：`DateTime` 未宣告 `timezone=True`。

**不修什麼**

有發現 → 記進 PLAN，不要在檢查當下順手改 production schema。

---

## 2. 對比色（WCAG AA）

**何時跑**

- 動了任一前端的色票／theme token（`anila-shell/index.html`、
  `csp-governance-ui/.../tokens.css`、`anilalm/.../tokens.ts`）
- 改了預設淺／深主題
- 發版前若本月動過顏色

**做什麼**

兩主題各算常見「文字色 × 背景色」對比，報 < 4.5:1 的配對。

**抓不到什麼（腳本自己會印）**

浮水印、半透明 overlay、漸層上的字、canvas／執行期改色——
今晚那種「淺底正常、深底淹死對話」只能渲染目視。靜態通過 ≠ 畫面安全。

---

## 3. 簡體／大陸用語（zh-TW）

**何時跑**

- 發版前（固定）
- 大範圍改前端文案或 API `detail=`／使用者可見錯誤字串之後

**做什麼**

`infra/ci/lint-zh-tw.sh` → 掃三前端 + `services/csp/app` +
`services/anila-studio/app`。命中可改用語，或（僅裝飾性）加 `zh-exempt`。

注意：「登錄」在台灣也可當「登記／註冊」用，詞表會把它當「登入」的
大陸用語打——若語境是登錄表／登記，屬誤報，自行判斷。
