# anila-agent / templete

agent runtime 設計參考用的 **upstream SDK source snapshots**,以 zip 形式提供。
這些不是 anila-agent 執行碼,只是讓開發者本機 unzip 後對照學習 / 借鑑設計模式。

| 檔案 | upstream | 用途 |
|---|---|---|
| `claude-code-src.zip` | Anthropic Claude Code CLI | turn-loop、3-tier compact、memdir、hook surface、slash-command |
| `openai-agents-python.zip` | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | anila-agent runtime 的 base(handoffs / guardrails / tracing / session) |
| `antigravity-sdk-python.zip` | [google-antigravity/antigravity-sdk-python](https://github.com/google-antigravity/antigravity-sdk-python) | Google Antigravity 平台 SDK,參考其 agent SDK pattern |

unzip 後**勿提交解出來的源碼**到 anila-agent;只追蹤這幾份 zip + 本 README。
