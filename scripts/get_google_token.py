#!/usr/bin/env python3
"""
一次性工具：取得 Google OAuth refresh_token
在本機執行後，將輸出的三個值存入 GitHub Secrets。

使用方式：
  pip install requests
  python scripts/get_google_token.py

所需 Google Cloud 設定：
  1. 前往 https://console.cloud.google.com/apis/credentials
  2. 建立「OAuth 2.0 用戶端 ID」（類型：桌面版應用程式）
  3. 啟用以下 API：
     - Gmail API
     - Google Drive API
  4. 下載用戶端 ID / 密鑰，填入下方提示
"""

import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
]

print("=" * 60)
print("Google OAuth refresh_token 取得工具")
print("=" * 60)
CLIENT_ID     = input("\n請輸入 Google Client ID    : ").strip()
CLIENT_SECRET = input("請輸入 Google Client Secret: ").strip()

auth_url = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    + urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",   # 強制重新同意以確保取得 refresh_token
    })
)

print(f"\n正在開啟瀏覽器進行授權…\n")
webbrowser.open(auth_url)

auth_code: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params    = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            b"<h2>&#x2705; Google &#x6388;&#x6b0a;&#x6210;&#x529f;&#xff01;"
            b"&#x8acb;&#x56de;&#x5230;&#x7d42;&#x7aef;&#x6a5f;&#x67e5;&#x770b; refresh_token&#x3002;</h2>"
        )

    def log_message(self, *_):
        pass


print("等待 Google 授權回呼（localhost:8080）…")
HTTPServer(("localhost", 8080), _Handler).handle_request()

if not auth_code:
    print("\n❌ 未收到授權碼，請重新執行。")
    raise SystemExit(1)

r = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "code":          auth_code,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    },
    timeout=30,
)
r.raise_for_status()
tokens = r.json()

refresh_token = tokens.get("refresh_token")
if not refresh_token:
    print("\n❌ 未取得 refresh_token（可能需要在 Google 帳號移除應用程式授權後重試）。")
    raise SystemExit(1)

print("\n" + "=" * 60)
print("✅ 成功！請將以下三個值加入 GitHub Secrets：")
print("   Settings → Secrets and variables → Actions → New repository secret")
print("=" * 60)
print(f"\nGOOGLE_CLIENT_ID     = {CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET = {CLIENT_SECRET}")
print(f"GOOGLE_REFRESH_TOKEN = {refresh_token}")
print("\n" + "=" * 60)
print("\n若需跨三個 repo 查詢 GitHub 資料，另需建立 Personal Access Token（read:org, repo）")
print("並存入 Secret 名稱：GH_PAT")
