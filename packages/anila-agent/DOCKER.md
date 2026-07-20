# anila-agent 開發環境 image

這顆 image 只是「**環境**」：anila-agent 套件 + 全部依賴 + JupyterLab + dev 工具
（pytest/ruff/mypy），**不包源碼、不包 configs**。

源碼放在你的 **MLSteam workspace**（自己 clone repo），不在 image 裡——這樣 Lab 的
檔案瀏覽器就看得到源碼，也不會有「Lab 沒開在 /app」那種問題。image 的唯一任務是
讓你在 air-gap 內網不用對付 pip。

```
┌── 外網 ──┐         ┌──────────── MLSteam ────────────┐
│ docker   │         │  Lab（用這顆 image 當環境）       │
│  build   │  搬 tar │   └ workspace: git clone repo ──┐ │
│  save ───┼────────►│       源碼 + configs 在這裡 ◄───┘ │
└──────────┘         │   環境(套件)已預裝在 image       │
                     └──────────────────────────────────┘
```

## 1. 外網：build + 存 tar

```bash
cd packages/anila-agent
make docker-build          # docker build -f Dockerfile -t anila-agent:1.0.0 ../..
make docker-save           # → anila-agent_1.0.0.tar.gz（也可用未壓縮 .tar）
```

## 2. 上傳 MLSteam → 建 Lab

把 `anila-agent_1.0.0.tar` 上傳 MLSteam image registry，用它建一個 Lab/開發環境。
MLSteam 會在這顆 image 內啟動 JupyterLab（image 已自帶 jupyter，故不會 ERRORCODE=127）。

## 3. 在 Lab 裡：clone repo 開發

開 Lab 的 Terminal：

```bash
git clone <anila-agent repo>      # 源碼 + configs 進你的 workspace（Lab 看得到）
cd anila-agent

# 環境已預裝，直接用——不用 pip install：
python -c "import anila_agent; print('env ready')"

cp .env.example .env              # 填模型端點 / CSP / collection
anila                             # 互動 CLI
# 或起服務（repo 內有 configs）：
uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200
```

> 為什麼要在 repo 內跑：框架的 `configs/*.yaml`（deny-all 政策等）以 CWD 相對載入，
> 在 cloned repo 目錄下才找得到。image 只提供「環境」，源碼/設定來自 workspace。

## 本機快速驗證（外網）

```bash
docker run --rm -p 8888:8888 anila-agent:1.0.0      # 起 JupyterLab
docker run --rm -it anila-agent:1.0.0 bash          # 進去看環境
```

## 設計備註

- **多階段**：builder 裝套件+依賴進 `/opt/venv`，runtime 只 `COPY --from=builder /opt/venv` →
  小、攻擊面小。源碼只在 builder 用來安裝，不進 runtime 檔案樹。
- **jupyter symlink 到 /usr/local/bin**：MLSteam 不論用 PATH 或絕對路徑啟動都找得到。
- **非 root**（uid 10001 anila）+ home `/home/anila` 給 jupyter 寫設定。
