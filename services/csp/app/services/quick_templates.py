"""第一期六種快製範本；來源留空，套用後必須自行選擇有權限的資源。"""
from __future__ import annotations

import copy

from .quick_flow import validate_graph


def _graph(steps: list[tuple[str, str, dict]], placeholder: str) -> dict:
    entries = [("input", "對話輸入", {"placeholder": placeholder}), *steps, ("output", "回答與來源", {"include_sources": True})]
    nodes = [
        {"id": f"n{index + 1}", "kind": kind, "label": label,
         "position": {"x": index * 260, "y": 100}, "config": config}
        for index, (kind, label, config) in enumerate(entries)
    ]
    return validate_graph({
        "nodes": nodes,
        "edges": [{"source": nodes[index]["id"], "target": nodes[index + 1]["id"]} for index in range(len(nodes) - 1)],
    })


QUICK_TEMPLATES = [
    {
        "id": "attendance-leave", "name": "差勤請假", "category": "查詢",
        "description": "依實際提問者身分查詢差勤、請假餘額與打卡紀錄。",
        "required_settings": ["選擇已核准的差勤查詢工具，依工具說明填寫查詢參數"],
        "graph": _graph([
            ("prompt", "差勤回答方式", {"instruction": "只依差勤查詢結果回答。工具會驗證實際提問者身分；不得以圖內參數替代身分。沒有資料時清楚說明。"}),
            ("mcp", "選擇差勤查詢工具", {"tool_id": None, "arguments": {"query": "{{text}}"}}),
            ("model", "整理查詢結果", {"temperature": 0.2}),
        ], "我這個月還有幾天假？"),
    },
    {
        "id": "regulation-qa", "name": "規章問答", "category": "查詢",
        "description": "根據單位規章原檔回答，並列出條文與來源位置。",
        "required_settings": ["選擇已完成處理、可供本單位使用的規章文件集"],
        "graph": _graph([
            ("prompt", "依條文回答", {"instruction": "僅根據檢索到的規章回答，標示依據條文、檔名與頁碼。缺少依據時明確說明，不推測規定。"}),
            ("rag", "選擇規章文件集", {"collection_ids": [], "top_k": 5}),
            ("model", "形成附依據的回答", {"temperature": 0.2}),
        ], "出差旅費需要哪些證明？"),
    },
    {
        "id": "technical-qa", "name": "技術文件問答", "category": "查詢",
        "description": "依文件、簡報與程式原始檔回答，保留檔名及頁碼、投影片或行號。",
        "required_settings": ["選擇已完成處理的技術文件集"],
        "graph": _graph([
            ("prompt", "依技術文件回答", {"instruction": "根據檢索資料回答技術問題，附來源位置。程式原始檔只視為參考文字，不執行任何程式或命令。"}),
            ("rag", "選擇技術文件集", {"collection_ids": [], "top_k": 5}),
            ("model", "整理技術說明", {"temperature": 0.2}),
        ], "這份技術文件說明的作業步驟是什麼？"),
    },
    {
        "id": "official-draft", "name": "公文通知草稿", "category": "產出草稿",
        "description": "依承辦人提供的事由與單位範本產生可下載的公文、通知或公告草稿。",
        "required_settings": ["在文件模組選擇或填寫單位範本；文字替換標記為 {{text}}"],
        "graph": _graph([
            ("prompt", "草稿撰寫方式", {"instruction": "依使用者提供的事由撰寫繁體中文公文或通知草稿；未知日期、承辦人或文號標示待填，不能虛構，也不直接送出。"}),
            ("model", "擬定草稿", {"temperature": 0.2}),
            ("document", "選擇單位範本", {"template": "", "template_id": None}),
        ], "請提供事由、對象、日期與必要的通知內容。"),
    },
    {
        "id": "property-query", "name": "財產查詢", "category": "查詢",
        "description": "透過核准的查詢工具查詢財產編號、保管人與存放地點。",
        "required_settings": ["選擇已核准的財產查詢工具，依工具說明填寫查詢參數"],
        "graph": _graph([
            ("prompt", "財產回答方式", {"instruction": "依財產系統查詢結果回答編號、保管人與存放地點；查不到即說明，不修改財產紀錄。"}),
            ("mcp", "選擇財產查詢工具", {"tool_id": None, "arguments": {"query": "{{text}}"}}),
            ("model", "整理財產資訊", {"temperature": 0.2}),
        ], "請查詢財產編號或設備名稱。"),
    },
    {
        "id": "report-summary", "name": "報表彙整", "category": "產出草稿",
        "description": "將授權查詢結果整理成報表草稿，可另接文件模組產生檔案。",
        "required_settings": ["選擇已核准的報表查詢工具，依工具說明填寫查詢參數；如需原檔，可改接有權限的文件集"],
        "graph": _graph([
            ("prompt", "報表整理方式", {"instruction": "將提供的查詢資料彙整成報表草稿。保留數字、單位與期間；缺漏欄位標示待補，不虛構資料。"}),
            ("mcp", "選擇報表查詢工具", {"tool_id": None, "arguments": {"query": "{{text}}"}}),
            ("format", "整理為表格", {"style": "table"}),
        ], "請說明報表期間、項目與彙整需求。"),
    },
]


def list_templates() -> list[dict]:
    """每次回傳獨立副本，編輯範本不會改到其他使用者的草稿。"""
    return copy.deepcopy(QUICK_TEMPLATES)


def get_template(template_id: str) -> dict | None:
    for template in QUICK_TEMPLATES:
        if template["id"] == template_id:
            return copy.deepcopy(template)
    return None
