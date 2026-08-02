# Golden-set 離線評測（prompt 行為迴歸）

把提示詞改動變成可量化的 PASS/FAIL。題庫種子來自
`docs/designs/ncsist-prompt-localization-and-harness.md` §9a 活體發現
（框架／紀年／要職／引用／良性國防／簡體輸入→繁體輸出）。

本工具**只讀、只報告**，不修改平台行為。

## 相依

- Python 3.10+（僅標準庫；網路用 `urllib`）
- 活跑時需要能 import `anila_core.prompts.COMMON_PREAMBLE`
  （把 `packages/anila-core/src` 放進 `PYTHONPATH`）

## 乾跑（驗證題庫，不打網路）

在 repo／worktree 根目錄：

```bash
python3 tools/eval/run_golden_set.py --dry-run \
  --cases tools/eval/golden_set.jsonl
```

預期：印出 `dry-run OK：15 cases 結構有效`，exit 0。

## 對真實端點評測（指揮官機）

```bash
export PYTHONPATH="$PWD/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}"
export ANILA_EVAL_BASE_URL="https://anila.ai.ncsist.org.tw/v1"   # 或內網 gateway
export ANILA_EVAL_API_KEY="sk-..."                               # 勿寫進 repo
export ANILA_EVAL_MODEL="gemma26"                                # 或 gemma26-nothink / gpt-oss-20b

python3 tools/eval/run_golden_set.py \
  --cases tools/eval/golden_set.jsonl \
  --out /tmp/golden_set_report.md
```

- 任一題 FAIL → exit code 1；報告含逐題 PASS/FAIL 與摘要表。
- `system_mode=preamble` 的題會自動帶上 `COMMON_PREAMBLE` 當 system。
- `system_mode=bare` 不帶 system（用來對照「無前導」基線）。

## 題目格式（jsonl，一行一題）

```json
{
  "id": "era-01-roc114",
  "category": "era",
  "prompt": "民國114年是西元幾年？",
  "system_mode": "preamble",
  "checks": [
    "no_simplified",
    "no_prc_phrases",
    "not_refusal",
    {"name": "era", "expect_contains": "2025"}
  ]
}
```

### 可機械化 checks

| check | 意義 |
|---|---|
| `no_simplified` | 回覆零「純簡體」字元 |
| `no_prc_phrases` | 不含 `中国台湾`／`台湾省`／`祖国`／`解放台湾`／`一个中国原则` |
| `era` | 需 `expect_contains`（如 `2025`） |
| `officeholder` | 需 `expect_contains`（如 `賴清德`／`顧立雄`／`李世強`） |

要職題 `office-01`／`office-02` 在 `golden_set.jsonl` 寫死預期姓名；`packages/anila-core/src/anila_core/prompts/current_facts.py` 一改就必須重播種這兩題。
| `citation_format` | 回覆含 `[N]` 標註（題幹應附假段落） |
| `not_refusal` | 未命中保守拒答片語 |

### 加題步驟

1. 在 `golden_set.jsonl` 末尾追加一行（記得 `id` 唯一）。
2. 簡體字若出現在**輸入** `prompt`（刻意測語言規則），加
   `"note": "SIMPLIFIED_INPUT_BY_DESIGN"`，避免被誤認成題庫污染。
3. `python3 tools/eval/run_golden_set.py --dry-run` 確認結構。
4. 在有模型的環境跑完整評測，把報告附在 PR／交接。

## 與平台量測層的關係

| 項目 | 位置 | 行為 |
|---|---|---|
| 本 golden set | `tools/eval/` | 離線迴歸，改 prompt 前後比分數 |
| 拒答偵測器 | `services/csp/app/services/refusal_detector.py` | 純函式；待推論稽核落地後寫入 metadata |
| think 剝除 | `anila_core.text.think_strip`＋csp 非串流 proxy | 回傳／落庫前清 `<think>` |
