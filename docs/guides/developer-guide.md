# 開發者指南 — 用快速起步做一支助手

> 頁面：治理中心「開發指南」（`/developer/guide`），開發者側欄第一項。
> 下載與註冊在「助手」（`/developer/agents`）。
>
> 預設路徑是 `packages/anila-agent-quickstart`。`packages/anila-agent` 是進階實作範例，助手頁的第二個下載，不是這份指南的下一步。

不必先註冊。註冊要 endpoint，而 endpoint 要等服務起來、做好 port forwarding 才存在。

開發在 MLSteam lab。lab 由平台建好的映像開成一台長期開著的 Linux 虛擬機，裡面沒有 Docker。下載的 zip 只有程式與站台設定，沒有 API 金鑰。zip 根目錄是 `anila-agent-quickstart/`。

## 步驟

1. 在助手頁按「下載快速起步」，取得通用 zip。用與 `bundle.json` 的 `compatible_lab_image_version` 相同的 lab 映像開一個 lab，把檔案放到工作目錄 `/app`。`ANILA_CA_FILE` 已指到 `/app/ca.pem`。
2. 在 JupyterLab 只改 `agent.py` 的回答。`COLLECTION_ID` 選填。範例 `AGENT_NAME` 可以留著；若要改，改完再用同一個名字註冊。註冊之後不要再改。
3. 通用包的 `LLM_MODEL` 留空。註冊並選定底層模型後再下載，會預填那個模型名稱。金鑰只放環境，不要寫進這個檔、也不要提交：

```sh
export LLM_API_KEY=…
```

這把金鑰只給 lab 自己測試、請求裡沒有派工 JWT 時使用。上線後助手改帶派工 JWT 呼叫 CSP，用量記在提問者身上。

4. `./run.sh start`。服務聽埠 8200。
5. 在 MLSteam 把 port forwarding 指到 8200。
6. 回到 Console 註冊助手。endpoint 填轉出去的 http 位址。註冊要填名稱、至少 24 字的用途說明、endpoint，以及基礎模型。
7. 把註冊得到的數字 id 填進 `deployment.env` 的 `ANILA_AGENT_ID`，執行 `./run.sh restart`。

`LLM_BASE_URL` 已是 CSP 的 `/v1`。維持下載包裡的這個值。上線用量算提問者，開發者的金鑰不參與那筆計帳。

`GET /health` 回 200 才表示可以接派工。不代表模型答得通。回到 Console 完成核准，再在對話裡選這個助手。`./run.sh status` 會印出跟 `/health` 相同的 `reason`。

| reason | 下一步 |
|---|---|
| 啟動被拒，缺 CSP_BASE_URL／ANILA_CA_FILE／LLM_BASE_URL | 用下載包裡的 deployment.env，不要手填站台位址 |
| 啟動被拒，映像版本不符 | 換 `bundle.json` 寫的那版 lab 映像，或重新下載 zip |
| `not_registered` | 註冊後把數字 id 填進 ANILA_AGENT_ID，再 `./run.sh restart` |
| `llm_not_configured` | 填 LLM_MODEL。lab 測試才 `export LLM_API_KEY` 後重啟；上線用量算提問者 |
| `jwks_unavailable` | 確認 CSP_BASE_URL 與 ca.pem；不要關 TLS |
| `upstream_error` | 確認 LLM_MODEL 是註冊的底層模型。lab 測試再查金鑰是否仍有效 |
| `search_failed` | 知識庫未綁定、派工憑條過期，或搜尋被拒 |

若模型和金鑰都還沒設，`/health` 先報 `llm_not_configured`，補上之後才報 `not_registered`。

查看：`./run.sh status`、`./run.sh logs`。停機：`./run.sh stop`。重啟：`./run.sh restart`。

## 知識庫

知識庫在 Console 綁定，而且必須屬於這位助手的擁有者。`agent.py` 的 `COLLECTION_ID` 只是挑選已經綁定的其中一個；沒有要查就留 `None`。它不授予權限。搜尋仍由平台依擁有者與已綁定的集合檢查。

## 進階實作範例

助手頁的第二個下載是「下載進階範例」，對應 `packages/anila-agent`（zip 根目錄 `anila-agent-advanced-example/`）。要做工具迴圈、長期記憶、多輪或背景工作時才改讀它。它本身就是可運行的服務。

既有 Python 服務若要自己接驗簽，仍用助手頁的「下載 anila_verify.py」。快速起步 zip 裡已經有同一支檔與 `ca.pem`。
