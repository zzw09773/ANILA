# ANILA 快速起步

只改 `agent.py` 的回答。平台接線已完成。v1 只保證單輪文字回答，以及在本輪派工 JWT 仍有效時的一次 RAG。不保證完整對話歷史。

開發在 MLSteam lab。lab 由平台建好的映像 tar 開成一台長期開著的 Linux 虛擬機，裡面沒有 Docker。Python 3.13 與相依（含 JupyterLab）已裝在映像裡。下載的 zip 只有程式與站台設定，沒有 wheelhouse，也沒有 API 金鑰。

不必先註冊。註冊要 endpoint，而 endpoint 要等服務起來、做好 port forwarding 才存在。

1. 在 Console 下載通用包。用與 `bundle.json` 的 `compatible_lab_image_version` 相同的 lab 映像開一個 lab，把 `anila-agent-quickstart/` 裡的檔案放到工作目錄 `/app`。`ANILA_CA_FILE` 已指到 `/app/ca.pem`。
2. 在 JupyterLab 只改 `agent.py` 的回答區。`COLLECTION_ID` 選填。範例 `AGENT_NAME` 可以留著；若要改，改完再用同一個名字註冊。註冊之後不要再改。
3. 通用包的 `LLM_MODEL` 留空。註冊並選定底層模型後再下載，會預填那個模型名稱（此 agent 實際呼叫的模型；更換需重新送審）。還沒註冊時，也可先填註冊時要用的名稱。金鑰只放環境，不要寫進這個檔、也不要提交：

```sh
export LLM_API_KEY=sk-…
```

上線之後，助手收到派工就用那次請求的派工 JWT 呼叫 `LLM_BASE_URL`，模型名稱是 `LLM_MODEL`。用量記在提問者身上，同時標註這個 agent。`LLM_API_KEY` 只在 lab 自己測試、請求裡沒有派工 JWT 時使用，套用開發者自己的模型權限。

4. `./run.sh start`。服務聽埠 8200。
5. 在 MLSteam 把 port forwarding 指到 8200。
6. 回到 Console 註冊助手，endpoint 填轉出去的 http 位址。
7. 把註冊得到的數字 id 填進 `deployment.env` 的 `ANILA_AGENT_ID`，執行 `./run.sh restart`。

`LLM_BASE_URL` 已是 `<CSP_BASE_URL>/v1`，也就是 CSP 自己的 OpenAI 相容端點。權限、用量與稽核都留在 CSP。不要改成內網模型位址。

`GET /health` 回 200 才表示可以接派工。不代表模型答得通。回到 Console 完成核准，再在 ANILA 對話選這個 agent 發一句話。`./run.sh status` 會印出跟 `/health` 相同的 `reason`。

| reason | 下一步 |
|---|---|
| 啟動被拒，缺 CSP_BASE_URL／ANILA_CA_FILE／LLM_BASE_URL | 用下載包裡的 deployment.env，不要手填站台位址 |
| 啟動被拒，映像版本不符 | 換 `bundle.json` 寫的那版 lab 映像，或重新下載 zip |
| `not_registered` | 註冊後把數字 id 填進 ANILA_AGENT_ID，再 `./run.sh restart` |
| `llm_not_configured` | 填 LLM_MODEL。lab 測試才要 `export LLM_API_KEY` 後重啟；上線派工不靠這把金鑰 |
| `jwks_unavailable` | 確認 CSP_BASE_URL 與 ca.pem；不要關 TLS |
| `upstream_error` | 確認 LLM_MODEL 是註冊的底層模型。lab 測試再查金鑰是否仍有效 |
| `search_failed` | collection 未綁定、JWT 過期，或搜尋被拒 |

若模型和金鑰都還沒設，`/health` 先報 `llm_not_configured`，補上之後才報 `not_registered`。

查看：`./run.sh status`、`./run.sh logs`。停機：`./run.sh stop`（先 SIGTERM，最多等 30 秒再強制結束）。重啟：`./run.sh restart`。

要做工具迴圈、長期記憶、多輪或背景工作時，改讀進階實作範例。
