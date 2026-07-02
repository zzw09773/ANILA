# 離線安裝（air-gapped 內網）

`openai-agents==0.17.5` 會拉一整棵相依樹（openai / httpx / pydantic / mcp …）。
真正的 air-gapped 部署需把完整相依閉包預先打包，帶進內網以 `--no-index` 安裝。

## 步驟

1. **有網路的機器**（與內網相同的 OS/Python 版本）：

   ```bash
   bash offline/build-wheelhouse.sh
   # 預設 extras=serving,pgvector,csp（不含 litellm）。
   # 要 litellm 時：bash offline/build-wheelhouse.sh serving,pgvector,csp,litellm
   ```

   產生 `offline/wheelhouse/*.whl`。

2. 把整個專案（含 `offline/wheelhouse/`）帶進 air-gapped 內網。

3. **內網機器**（全程不連網）：

   ```bash
   bash offline/install.sh serving
   .venv/bin/anila
   ```

> 注意：`pip download` 解析的 wheel 與目標機器的 OS / Python ABI 綁定。請在
> **與內網一致**的環境打包（建議同一台或同映像）。
