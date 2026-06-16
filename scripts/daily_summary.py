#!/usr/bin/env python3
"""
每日工作彙整：GitHub × Gmail × Google Drive → Google 文件
排程：每日 04:00 台北時間（UTC+8）執行，彙整前一日活動
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Constants ────────────────────────────────────────────────────────────────

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

GITHUB_REPOS = [
    ("zzw09773", "anila"),
    ("zzw09773", "anila-agent"),
    ("zzw09773", "aiec_test"),
]

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Date helpers ─────────────────────────────────────────────────────────────

def get_target_range():
    """Return (start, end, date_str) for yesterday in Taipei time, or DATE_OVERRIDE."""
    override = os.environ.get("DATE_OVERRIDE", "").strip()
    if override:
        target = datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=TAIPEI_TZ)
    else:
        target = datetime.now(TAIPEI_TZ) - timedelta(days=1)

    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end, target.strftime("%Y-%m-%d")


# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=GOOGLE_SCOPES,
    )
    creds.refresh(Request())
    return creds


# ── GitHub ────────────────────────────────────────────────────────────────────

def fetch_github_activity(start: datetime, end: datetime) -> dict:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)

    result = {}
    for owner, repo in GITHUB_REPOS:
        key = f"{owner}/{repo}"
        data: dict = {"commits": [], "pull_requests": [], "issues": []}

        # Commits
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                headers=headers,
                params={
                    "since": start_utc.isoformat(),
                    "until": end_utc.isoformat(),
                    "per_page": 50,
                },
                timeout=15,
            )
            if resp.ok:
                data["commits"] = [
                    {
                        "sha": c["sha"][:7],
                        "message": c["commit"]["message"].splitlines()[0][:120],
                        "author": c["commit"]["author"]["name"],
                        "url": c["html_url"],
                    }
                    for c in resp.json()
                ]
        except Exception as exc:
            data["commits"] = [{"error": str(exc)}]

        # Pull Requests
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 30},
                timeout=15,
            )
            if resp.ok:
                for pr in resp.json():
                    updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
                    if start_utc <= updated <= end_utc:
                        data["pull_requests"].append(
                            {
                                "number": pr["number"],
                                "title": pr["title"],
                                "state": pr["state"],
                                "draft": pr.get("draft", False),
                                "url": pr["html_url"],
                            }
                        )
        except Exception as exc:
            data["pull_requests"] = [{"error": str(exc)}]

        # Issues (exclude PRs)
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                headers=headers,
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "since": start_utc.isoformat(),
                    "per_page": 30,
                },
                timeout=15,
            )
            if resp.ok:
                for issue in resp.json():
                    if "pull_request" in issue:
                        continue
                    updated = datetime.fromisoformat(issue["updated_at"].replace("Z", "+00:00"))
                    if updated <= end_utc:
                        data["issues"].append(
                            {
                                "number": issue["number"],
                                "title": issue["title"],
                                "state": issue["state"],
                                "url": issue["html_url"],
                            }
                        )
        except Exception as exc:
            data["issues"] = [{"error": str(exc)}]

        result[key] = data

    return result


# ── Gmail ─────────────────────────────────────────────────────────────────────

def fetch_gmail_activity(creds: Credentials, start: datetime) -> list:
    service = build("gmail", "v1", credentials=creds)

    date_str = start.strftime("%Y/%m/%d")
    next_date = (start + timedelta(days=1)).strftime("%Y/%m/%d")
    query = f"after:{date_str} before:{next_date}"

    try:
        result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        messages = result.get("messages", [])
    except Exception as exc:
        return [{"error": str(exc)}]

    summaries = []
    for msg in messages[:25]:
        try:
            detail = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "To", "Date"],
            ).execute()
            h = {item["name"]: item["value"] for item in detail.get("payload", {}).get("headers", [])}
            summaries.append(
                {
                    "subject": h.get("Subject", "(無主旨)"),
                    "from": h.get("From", ""),
                    "to": h.get("To", ""),
                    "date": h.get("Date", ""),
                    "snippet": detail.get("snippet", "")[:200],
                }
            )
        except Exception:
            continue

    return summaries


# ── Google Drive ──────────────────────────────────────────────────────────────

def fetch_gdrive_activity(creds: Credentials, start: datetime) -> list:
    service = build("drive", "v3", credentials=creds)

    modified_after = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        result = service.files().list(
            q=f"modifiedTime >= '{modified_after}'",
            orderBy="modifiedTime desc",
            pageSize=30,
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
            spaces="drive",
        ).execute()
        files = result.get("files", [])
    except Exception as exc:
        return [{"error": str(exc)}]

    type_map = {
        "application/vnd.google-apps.document": "Google 文件",
        "application/vnd.google-apps.spreadsheet": "試算表",
        "application/vnd.google-apps.presentation": "簡報",
        "application/vnd.google-apps.folder": "資料夾",
        "application/pdf": "PDF",
    }

    return [
        {
            "name": f["name"],
            "type": type_map.get(f.get("mimeType", ""), f.get("mimeType", "").split(".")[-1]),
            "modified": f.get("modifiedTime", ""),
            "url": f.get("webViewLink", ""),
        }
        for f in files
    ]


# ── Claude summary ────────────────────────────────────────────────────────────

def generate_summary(date_str: str, github: dict, gmail: list, gdrive: list) -> str:
    client = anthropic.Anthropic()

    payload = json.dumps(
        {"date": date_str, "github": github, "gmail": gmail, "google_drive": gdrive},
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""你是一位專業的工作助理。以下是 {date_str}（台北時間）的工作數據，請彙整成一份清晰的每日工作報告。

原始數據：
{payload}

請輸出純文字格式（不使用 Markdown，使用 ── 分隔線），依以下結構撰寫，語言以中文為主、技術名詞保留英文：

每日工作彙整 {date_str}
══════════════════════════════

【GitHub 活動】
（依 repo 分類，列出 commits 摘要、PR 狀態、Issue 動態；若無活動則標示「無」）

【Gmail 信件摘要】
（列出重要信件，簡述內容；過濾廣告、系統通知；若無重要信件則標示「無」）

【Google Drive 更新】
（列出修改的文件名稱與類型；若無則標示「無」）

【今日工作重點】
（根據以上數據條列 3-5 個今日的主要工作成果）

【待辦 / 明日建議】
（根據未完成 PR、open issue 或信件中的 action items 提出建議）
"""

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Google Docs ───────────────────────────────────────────────────────────────

def create_google_doc(creds: Credentials, title: str, body_text: str) -> str:
    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    folder_id = os.environ.get("GDOC_FOLDER_ID", "").strip()
    if folder_id:
        file_meta = drive.files().get(fileId=doc_id, fields="parents").execute()
        current_parents = ",".join(file_meta.get("parents", []))
        drive.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=current_parents,
            fields="id,parents",
        ).execute()

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": body_text}}]},
    ).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    start, end, date_str = get_target_range()
    print(f"[daily-summary] 目標日期：{date_str}（台北時間）")

    print("[1/5] 取得 Google 憑證...")
    creds = get_google_creds()

    print("[2/5] 收集 GitHub 活動...")
    github_data = fetch_github_activity(start, end)

    print("[3/5] 收集 Gmail 信件...")
    gmail_data = fetch_gmail_activity(creds, start)

    print("[4/5] 收集 Google Drive 檔案...")
    gdrive_data = fetch_gdrive_activity(creds, start)

    print("[5/5] 使用 Claude Opus 生成彙整並建立 Google 文件...")
    summary = generate_summary(date_str, github_data, gmail_data, gdrive_data)
    title = f"每日工作彙整 {date_str}"
    doc_url = create_google_doc(creds, title, summary)

    print(f"\n完成！Google 文件：{doc_url}\n")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"## 每日工作彙整 {date_str}\n\n")
            f.write(f"**[開啟 Google 文件]({doc_url})**\n\n")
            f.write("```\n")
            f.write(summary[:1000])
            f.write("\n...\n```\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
