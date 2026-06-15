#!/usr/bin/env python3
"""
每日工作彙整腳本
自動彙整前一日 GitHub / Gmail / Google Drive 活動並輸出成 Google 文件
由 GitHub Actions 於每日 04:00 台北時間（UTC 20:00）自動執行
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ── 日期設定（台北時間 UTC+8）─────────────────────────────────────────
TZ_TAIPEI = timezone(timedelta(hours=8))
now_taipei = datetime.now(TZ_TAIPEI)
yesterday  = now_taipei - timedelta(days=1)

DATE_LABEL     = yesterday.strftime("%Y/%m/%d")
DATE_LABEL_ISO = yesterday.strftime("%Y-%m-%d")

START_UTC = yesterday.replace(hour=0,  minute=0,  second=0,  microsecond=0).astimezone(timezone.utc)
END_UTC   = yesterday.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc)
START_ISO = START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")
END_ISO   = END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")

# Gmail 用日期（YYYY/MM/DD 格式）
GMAIL_FROM = yesterday.strftime("%Y/%m/%d")
GMAIL_TO   = (yesterday + timedelta(days=1)).strftime("%Y/%m/%d")

# ── GitHub ────────────────────────────────────────────────────────────
REPOS = [
    "zzw09773/anila",
    "zzw09773/anila-agent",
    "zzw09773/aiec_test",
]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _gh(path: str, params: dict | None = None) -> list | dict:
    r = requests.get(
        f"https://api.github.com{path}",
        headers=GH_HEADERS,
        params=params or {},
        timeout=30,
    )
    if not r.ok:
        print(f"  ⚠️  GitHub {path} → {r.status_code}", file=sys.stderr)
        return []
    return r.json()


def github_html() -> str:
    out = ""
    for repo in REPOS:
        commits = _gh(f"/repos/{repo}/commits", {
            "since": START_ISO, "until": END_ISO, "per_page": 100,
        })
        prs_raw = _gh(f"/repos/{repo}/pulls", {
            "state": "all", "sort": "updated", "direction": "desc", "per_page": 50,
        })
        issues_raw = _gh(f"/repos/{repo}/issues", {
            "state": "all", "sort": "updated", "direction": "desc",
            "per_page": 50, "since": START_ISO,
        })

        prs    = [p for p in prs_raw    if START_ISO <= p.get("updated_at", "") <= END_ISO]
        issues = [i for i in issues_raw
                  if "pull_request" not in i and i.get("updated_at", "") <= END_ISO]

        out += f"<h3>📦 {_esc(repo)}</h3>"

        if commits:
            out += f"<h4>Commits（{len(commits)} 筆）</h4><ul>"
            for c in commits:
                msg    = c["commit"]["message"].split("\n")[0][:120]
                author = c["commit"]["author"]["name"]
                sha    = c["sha"][:7]
                out += f"<li><code>{sha}</code> {_esc(msg)} <em>— {_esc(author)}</em></li>"
            out += "</ul>"
        else:
            out += "<p><em>無 commit</em></p>"

        if prs:
            out += f"<h4>Pull Requests（{len(prs)} 筆）</h4><ul>"
            for p in prs:
                icon = "🟢" if p["state"] == "open" else "🔴"
                out += (f"<li>{icon} <a href='{p['html_url']}'>"
                        f"#{p['number']} {_esc(p['title'])}</a></li>")
            out += "</ul>"

        if issues:
            out += f"<h4>Issues（{len(issues)} 筆）</h4><ul>"
            for i in issues:
                icon = "🟢" if i["state"] == "open" else "⚫"
                out += (f"<li>{icon} <a href='{i['html_url']}'>"
                        f"#{i['number']} {_esc(i['title'])}</a></li>")
            out += "</ul>"

    return out


# ── Google OAuth ──────────────────────────────────────────────────────
def get_access_token() -> str:
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ── Gmail ─────────────────────────────────────────────────────────────
def _gmail(token: str, path: str, params: dict | None = None) -> dict:
    r = requests.get(
        f"https://gmail.googleapis.com/gmail/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    if not r.ok:
        print(f"  ⚠️  Gmail {path} → {r.status_code}", file=sys.stderr)
        return {}
    return r.json()


def _msg_headers(token: str, msg_id: str, keys: list[str]) -> dict:
    data = _gmail(token, f"/users/me/messages/{msg_id}", {
        "format": "metadata",
        "metadataHeaders": keys,
    })
    hdrs = data.get("payload", {}).get("headers", [])
    return {h["name"]: h["value"] for h in hdrs if h["name"] in keys}


def gmail_html(token: str) -> str:
    def render(msgs: list, party_key: str, arrow: str) -> str:
        if not msgs:
            return "<p><em>無</em></p>"
        html = "<ul>"
        for m in msgs[:15]:
            h     = _msg_headers(token, m["id"], ["Subject", party_key])
            subj  = h.get("Subject", "(無主旨)")[:90]
            party = h.get(party_key, "")[:70]
            html += f"<li>{_esc(subj)} <em>{arrow} {_esc(party)}</em></li>"
        html += "</ul>"
        return html

    sent_msgs = _gmail(token, "/users/me/messages", {
        "q": f"in:sent after:{GMAIL_FROM} before:{GMAIL_TO}", "maxResults": 50,
    }).get("messages", [])

    recv_msgs = _gmail(token, "/users/me/messages", {
        "q": f"in:inbox is:important after:{GMAIL_FROM} before:{GMAIL_TO}", "maxResults": 50,
    }).get("messages", [])

    out  = f"<h3>📤 寄件（{len(sent_msgs)} 筆）</h3>" + render(sent_msgs, "To",   "→")
    out += f"<h3>📥 重要收件（{len(recv_msgs)} 筆）</h3>" + render(recv_msgs, "From", "←")
    return out


# ── Google Drive ──────────────────────────────────────────────────────
def drive_html(token: str) -> str:
    q = (
        f"modifiedTime >= '{START_UTC.isoformat()}' "
        f"and modifiedTime <= '{END_UTC.isoformat()}' "
        f"and trashed = false"
    )
    r = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "q":       q,
            "fields":  "files(id,name,mimeType,webViewLink)",
            "pageSize": 50,
            "orderBy": "modifiedTime desc",
        },
        timeout=30,
    )
    if not r.ok:
        return "<p><em>無法取得 Drive 資料</em></p>"

    files = r.json().get("files", [])
    if not files:
        return "<p><em>昨日無修改文件</em></p>"

    html = "<ul>"
    for f in files:
        name = f.get("name", "?")
        link = f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"
        mime = f.get("mimeType", "")
        icon = ("📄" if "document"     in mime else
                "📊" if "spreadsheet"  in mime else
                "📊" if "presentation" in mime else
                "📁" if "folder"       in mime else "📎")
        html += f"<li>{icon} <a href='{link}'>{_esc(name)}</a></li>"
    html += "</ul>"
    return html


# ── 建立 Google Doc（multipart upload：HTML → Google Doc）─────────────
def create_doc(token: str, title: str, body: str) -> str:
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body{{font-family:Arial,'Noto Sans TC',sans-serif;max-width:960px;
        margin:40px auto;padding:0 24px;color:#202124}}
  h1{{color:#1a73e8;border-bottom:3px solid #1a73e8;padding-bottom:8px}}
  h2{{color:#174ea6;margin-top:32px}}h3{{color:#185abc}}h4{{color:#3c4043}}
  code{{background:#f1f3f4;padding:2px 5px;border-radius:3px;font-size:.9em}}
  ul{{line-height:1.9}}em{{color:#5f6368}}
</style></head><body>{body}</body></html>"""

    boundary = "DailySummaryBoundary42"
    metadata = json.dumps({"name": title, "mimeType": "application/vnd.google-apps.document"})
    payload  = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: text/html; charset=UTF-8\r\n\r\n"
        f"{full_html}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    r = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files"
        "?uploadType=multipart&fields=id,webViewLink",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  f"multipart/related; boundary={boundary}",
        },
        data=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    fid  = data.get("id", "")
    return data.get("webViewLink") or f"https://docs.google.com/document/d/{fid}/edit"


# ── 工具 ──────────────────────────────────────────────────────────────
def _esc(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── 主流程 ────────────────────────────────────────────────────────────
def main() -> None:
    print(f"🗓️  開始彙整 {DATE_LABEL} 的工作紀錄…")

    print("  📦 GitHub 活動…")
    gh = github_html()

    print("  🔑 取得 Google OAuth token…")
    token = get_access_token()

    print("  📧 Gmail 活動…")
    gm = gmail_html(token)

    print("  📁 Google Drive 活動…")
    dr = drive_html(token)

    gen_time = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M")
    body_html = f"""
<h1>📅 工作日報 — {DATE_LABEL}</h1>
<p><em>自動生成於 {gen_time}（台北時間）</em></p>

<h2>一、GitHub 活動</h2>
{gh}

<h2>二、Gmail 郵件</h2>
{gm}

<h2>三、Google Drive</h2>
{dr}
"""

    print("  📝 建立 Google 文件…")
    url = create_doc(token, f"工作日報 {DATE_LABEL_ISO}", body_html)

    print(f"\n✅ 完成！")
    print(f"   📄 {url}")

    with open("doc_url.txt", "w") as f:
        f.write(url)


if __name__ == "__main__":
    main()
