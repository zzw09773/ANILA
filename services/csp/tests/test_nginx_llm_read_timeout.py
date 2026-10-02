"""會同步等模型的 /api/ 路徑，各自的 location 要是 300 秒，不能退回 120 秒。"""
from __future__ import annotations

import re
from pathlib import Path

_CONF = Path(__file__).resolve().parents[3] / "infra" / "nginx" / "anila.conf"

_LONG_PATHS = (
    "/api/thinking/summarize",
    "/api/agents/system-prompt/suggest",
    "/api/skills/assist",
    "/api/institutional-kb/preview",
    "/api/ingestion/collections/12/search",
    "/api/ingestion/collections/+12/search",
    "/api/memory/recall",
    "/api/ingestion/collections/12/images/search",
    "/api/ingestion/collections/+12/images/search",
    "/api/models/12/set-platform-embedding",
    "/api/models/+12/set-platform-embedding",
)
_SHORT_PATH = "/api/users"


def _server_blocks(text: str) -> list[str]:
    starts = [m.start() for m in re.finditer(r"(?m)^server \{", text)]
    blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        blocks.append(text[start:end])
    selected = [
        block for block in blocks
        if re.search(r"(?m)^\s*listen\s+443\s+ssl", block)
        or re.search(r"(?m)^\s*listen\s+4443\s+ssl", block)
    ]
    assert len(selected) == 2
    return selected


def _locations(block: str) -> list[tuple[str, str]]:
    found = []
    for match in re.finditer(r"(?m)^    location\s+([^\{]+)\{", block):
        start = match.end()
        depth = 1
        cursor = start
        while cursor < len(block) and depth:
            if block[cursor] == "{":
                depth += 1
            elif block[cursor] == "}":
                depth -= 1
            cursor += 1
        found.append((match.group(1).strip(), block[start:cursor]))
    return found


def _winning_timeout(block: str, path: str) -> str:
    regex_hit = None
    prefix_hit = None
    prefix_len = -1
    for modifier, body in _locations(block):
        if modifier.startswith("~"):
            pattern = modifier.split(None, 1)[1].strip()
            if regex_hit is None and re.search(pattern, path):
                regex_hit = body
            continue
        prefix = modifier
        if path == prefix or path.startswith(prefix):
            if len(prefix) > prefix_len:
                prefix_len = len(prefix)
                prefix_hit = body
    body = regex_hit if regex_hit is not None else prefix_hit
    assert body is not None, path
    match = re.search(r"proxy_read_timeout\s+(\d+s)", body)
    assert match is not None, path
    return match.group(1)


def test_each_long_path_has_its_own_300s_read_timeout_in_both_servers():
    blocks = _server_blocks(_CONF.read_text(encoding="utf-8"))
    assert len(blocks) == 2
    for block in blocks:
        for path in _LONG_PATHS:
            assert _winning_timeout(block, path) == "300s", path
        assert _winning_timeout(block, _SHORT_PATH) == "120s"
