#!/usr/bin/env python3
"""
一次性工具：取得 Google OAuth Refresh Token
在本機執行一次，將輸出的三個值存為 GitHub Secrets。

前置步驟：
  1. 前往 https://console.cloud.google.com/
  2. 建立專案 → 啟用 Gmail API、Google Drive API、Google Docs API
  3. 建立 OAuth 2.0 用戶端 ID（類型：桌面應用程式）
  4. 下載 JSON，命名為 credentials.json 放在此目錄

執行：
  pip install google-auth-oauthlib
  python scripts/get_google_token.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


def main():
    print("=" * 60)
    print("Google OAuth Refresh Token 取得工具")
    print("=" * 60)
    print()

    creds_file = input("credentials.json 路徑 [credentials.json]: ").strip() or "credentials.json"

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0)

    print()
    print("=" * 60)
    print("請將以下三個值加入 GitHub repo Secrets：")
    print("Settings → Secrets and variables → Actions → New repository secret")
    print("=" * 60)
    print()
    print(f"  GOOGLE_CLIENT_ID     =  {creds.client_id}")
    print(f"  GOOGLE_CLIENT_SECRET =  {creds.client_secret}")
    print(f"  GOOGLE_REFRESH_TOKEN =  {creds.refresh_token}")
    print()
    print("完成後可刪除 credentials.json，請勿將其 commit 進 repo。")
    print()


if __name__ == "__main__":
    main()
