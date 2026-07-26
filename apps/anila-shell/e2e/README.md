# E2E — 目前不存在

**本目錄沒有任何 E2E 測試。** `@playwright/test` 也不是本 app 的依賴。

## 為什麼留這份 README 而不是直接刪掉

先前這裡寫著:

> The scaffold in `functions.spec.js` covers the spec §8.4 must-have
> scenarios (happy path, RBAC, verb whitelist injection rejection, ownership 403)

而 **`functions.spec.js` 從來不存在**。任何讀到那句的人會以為 RBAC 與注入拒絕
已有端到端覆蓋 —— 這比誠實地說「沒有 E2E」危險得多,因為它會讓人**跳過**該做
的驗證。W0-6 移除了那個宣稱。

這類「文件字面與實作相反」在本 codebase 是一級缺陷類別(補救計畫 §5 S5 列了
多個實例:UI 的「加密模式」實際零加密、runbook 說備份含 JWT 私鑰而實作明確
不含、治理文件宣稱的個資法更正權連 admin 都讀不到)。留下這份記錄,是為了讓
下一個人知道**這裡是空的,而且是刻意記錄成空的**。

## 補 E2E 屬於哪個階段

不在補救計畫 Wave 0–3 範圍(見計畫 §6.2:補 E2E 本體是 Gate 4+ 工程)。前置:

1. **樣式策略遷移期間不宜寫選擇器** —— D3 已定案 CSS Modules + design tokens,
   遷移會動 DOM 結構,現在寫的選擇器會作廢。
2. **先讓 Wave 0 的 CI 護欄到位**:`apps/anilalm` 才剛有測試設施(W0-6①)、
   全案 ESLint 尚未接上。E2E 是最貴的一層,不該當第一層。
3. 需要可重複的 seed 資料與**專用**測試 stack —— 現行鐵則禁止對
   `anila-platform-*`(user 的 dev 環境)或 `.15` 生產跑破壞性測試。

## 舊 README 的架構描述已失效

它列的前置服務是 `worker-api`、`sandbox-exec`、`sandbox-extract`、egress proxy、
`anila-ui`,並硬寫 seed 密碼 `dev-password`。這些在現行
`infra/compose/platform.yml` 的服務清單裡都找不到。若日後要補 E2E,**不要拿舊
README 當規格**,以當時的 compose 與 `AGENTS.md` 為準。
