
---

## 10. 前置備貨:opencc(prompt-wire 合併後新增,2026-08-03)

`opencc-python-reimplemented==0.1.7` 成為 **anila-core 的基礎依賴**
(`packages/anila-core/pyproject.toml:44`),用於 assistant 訊息落庫前的 zh-TW 正規化。

**傳遞影響三個映像**(它們的 Dockerfile 都 COPY anila-core):
`anila-agent`、`ingestion-worker`、`anila-core-router`。

⚠ **這三個映像若在內網重建,會卡在 `pip install opencc-python-reimplemented`。**
氣隙沒有 PyPI,症狀是 build 停在下載、operator 拿到一個跟語音／中文完全無關的錯誤。

**兩條路,擇一:**

1. **帶映像進去,不要在內網 build**(推薦,也是本 runbook 的主線)。
   映像裡已經裝好,不需要 wheel。起棧務必帶 `--no-build`。
2. 若真的需要在內網重建,先在有網路的機器備好 wheel:
   ```bash
   pip download opencc-python-reimplemented==0.1.7 -d /tmp/anila-wheelhouse
   ```
   連同 bundle 一起帶進去,build 時指向該目錄。

**驗證有沒有裝到**(在內網起棧後):
```bash
docker compose -p anila-restart exec -T csp python3 -c "import opencc; print('opencc ok')"
```

### 順帶:zh-TW 正規化刻意不改的字

`packages/anila-core/src/anila_core/text/domain_terms.py:12-14` 明確**不**把
「質量」改成「品質」——國防／工程語境的「質量」多半是 mass(質量守恆、彈頭質量、
質量流率),無條件改寫會污染物理術語。簡體「质量」仍會經 s2twp 轉成「質量」
(字形轉換,非詞替換)。這是刻意的,不要當成漏字補上去。
