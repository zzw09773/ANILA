"""批次索引 / 管理腳本

將 data/documents/ 資料夾下的文件透過 ANILA Core API 進行索引，
並支援列出、刪除已索引文件。

使用方式：
    # 索引文件
    python3 index_documents.py
    python3 index_documents.py --dir /other/path

    # 列出已索引文件
    python3 index_documents.py --list

    # 刪除指定文件（可用 document_id 或檔名，支援多個）
    python3 index_documents.py --delete abc123 def456
    python3 index_documents.py --delete 陸海空軍懲罰法.pdf

    # 刪除全部已索引文件
    python3 index_documents.py --delete-all

    # 指定 API 位址
    python3 index_documents.py --api http://localhost:8000

支援格式：.txt  .md  .pdf  .docx  .doc  .odt
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

# ── 預設值 ────────────────────────────────────────────────────────────────────
DEFAULT_API     = "http://localhost:8000"
DEFAULT_DIR     = Path(__file__).parent / "data" / "documents"
DEFAULT_USER    = "default"
DEFAULT_PROJECT = "default"
SUPPORTED_EXT   = {".txt", ".md", ".pdf", ".docx", ".doc", ".odt"}
POLL_INTERVAL   = 3    # 秒
POLL_TIMEOUT    = 120  # 秒


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    api  = args.api.rstrip("/")

    with httpx.Client(timeout=60) as client:
        if args.list:
            cmd_list(client, api)
        elif args.delete:
            cmd_delete(client, api, args.delete)
        elif args.delete_all:
            cmd_delete_all(client, api, yes=args.yes)
        else:
            cmd_index(client, api, Path(args.dir), args.user, args.project)


# ── 子命令：列出已索引文件 ────────────────────────────────────────────────────

def cmd_list(client: httpx.Client, api: str) -> None:
    resp = client.get(f"{api}/documents")
    resp.raise_for_status()
    data = resp.json()
    docs = data.get("documents", [])

    if not docs:
        print("（尚無已索引文件）")
        return

    print(f"{'#':<4} {'document_id':<38} {'chunks':>6}  {'最後索引':^22}  檔名")
    print("-" * 100)
    for i, d in enumerate(docs, 1):
        # 優先用 filename，再用 title，最後取 source_path 末段
        display = (
            d.get("filename")
            or d.get("title")
            or Path(d.get("source_path", "")).name
            or d["document_id"]
        )
        last = (d.get("last_indexed") or "")[:19].replace("T", " ")
        print(f"{i:<4} {d['document_id']:<38} {d['chunk_count']:>6}  {last:<22}  {display}")
    print(f"\n共 {len(docs)} 份文件")


# ── 子命令：刪除指定文件 ──────────────────────────────────────────────────────

def cmd_delete(client: httpx.Client, api: str, targets: list[str]) -> None:
    """依 document_id 或檔名刪除一或多份文件。"""
    # 先取得清單，建立檔名 → doc_id 對照表
    docs = _fetch_doc_list(client, api)
    name_map: dict[str, str] = {}   # filename (lower) → doc_id
    id_set:   set[str]       = set()
    for d in docs:
        filename = Path(d.get("source_path", d.get("title", ""))).name.lower()
        name_map[filename] = d["document_id"]
        id_set.add(d["document_id"])

    to_delete: list[tuple[str, str]] = []  # [(doc_id, label), ...]
    not_found: list[str] = []

    for t in targets:
        if t in id_set:
            label = next(
                Path(d.get("source_path", "")).name
                for d in docs if d["document_id"] == t
            )
            to_delete.append((t, label))
        elif t.lower() in name_map:
            to_delete.append((name_map[t.lower()], t))
        else:
            # 嘗試部分檔名匹配
            matches = [(doc_id, name) for name, doc_id in name_map.items()
                       if t.lower() in name]
            if matches:
                to_delete.extend((doc_id, name) for doc_id, name in matches)
            else:
                not_found.append(t)

    if not_found:
        print("[!] 找不到以下文件（可用 --list 確認可用清單）：")
        for nf in not_found:
            print(f"    - {nf}")

    if not to_delete:
        print("沒有可刪除的文件。")
        return

    print(f"即將刪除 {len(to_delete)} 份文件：")
    for doc_id, label in to_delete:
        print(f"  - {label}  ({doc_id})")

    ok = fail = 0
    for doc_id, label in to_delete:
        try:
            resp = client.delete(f"{api}/documents/{doc_id}")
            resp.raise_for_status()
            print(f"  ✅ 已刪除：{label}")
            ok += 1
        except Exception as e:
            print(f"  ❌ 刪除失敗：{label}  ({e})")
            fail += 1

    print(f"\n完成：{ok} 成功 / {fail} 失敗")


# ── 子命令：刪除全部文件 ──────────────────────────────────────────────────────

def cmd_delete_all(client: httpx.Client, api: str, yes: bool = False) -> None:
    docs = _fetch_doc_list(client, api)
    if not docs:
        print("（目前沒有已索引文件，無需刪除）")
        return

    print(f"即將刪除全部 {len(docs)} 份已索引文件：")
    for d in docs:
        filename = Path(d.get("source_path", d.get("title", "unknown"))).name
        print(f"  - {filename}  ({d['document_id']})")

    if not yes:
        confirm = input("\n確認刪除？(y/N) ").strip().lower()
        if confirm != "y":
            print("已取消。")
            return

    ok = fail = 0
    for d in docs:
        doc_id   = d["document_id"]
        filename = Path(d.get("source_path", d.get("title", ""))).name
        try:
            resp = client.delete(f"{api}/documents/{doc_id}")
            resp.raise_for_status()
            print(f"  ✅ 已刪除：{filename}")
            ok += 1
        except Exception as e:
            print(f"  ❌ 刪除失敗：{filename}  ({e})")
            fail += 1

    print(f"\n完成：{ok} 成功 / {fail} 失敗")


# ── 子命令：索引文件 ──────────────────────────────────────────────────────────

def cmd_index(
    client: httpx.Client,
    api: str,
    doc_dir: Path,
    user_id: str,
    project_id: str,
) -> None:
    doc_dir.mkdir(parents=True, exist_ok=True)
    print(f"文件資料夾：{doc_dir.resolve()}")

    files = [f for f in sorted(doc_dir.rglob("*")) if f.suffix.lower() in SUPPORTED_EXT]
    if not files:
        print(f"[!] {doc_dir} 下沒有可索引的文件（{', '.join(sorted(SUPPORTED_EXT))}）")
        print("    請將文件放入資料夾後再執行。")
        sys.exit(0)

    print(f"找到 {len(files)} 個文件，開始索引...\n")

    results: list[dict] = []
    for idx, file_path in enumerate(files, 1):
        print(f"[{idx}/{len(files)}] {file_path.name}")
        result = ingest_file(client, api, file_path, user_id, project_id)
        results.append(result)
        status = "✅" if result["success"] else "❌"
        print(f"  {status} {result['message']}\n")

    ok   = sum(1 for r in results if r["success"])
    fail = len(results) - ok
    print("=" * 50)
    print(f"索引完成：{ok} 成功 / {fail} 失敗")
    if fail:
        print("\n失敗清單：")
        for r in results:
            if not r["success"]:
                print(f"  - {r['file']}: {r['message']}")


def ingest_file(
    client: httpx.Client,
    api: str,
    file_path: Path,
    user_id: str,
    project_id: str,
) -> dict:
    """上傳單一文件並等待索引完成，回傳結果字典。"""
    # Step 1: 上傳檔案
    try:
        with file_path.open("rb") as f:
            resp = client.post(
                f"{api}/documents/upload",
                files={"file": (file_path.name, f, "application/octet-stream")},
                data={"user_id": user_id, "project_id": project_id},
            )
        resp.raise_for_status()
        upload  = resp.json()
        doc_id  = upload["document_id"]
        fp_cont = upload["file_path"]
    except Exception as e:
        return {"file": file_path.name, "success": False, "message": f"上傳失敗: {e}"}

    # Step 2: 觸發 ingestion
    try:
        resp = client.post(
            f"{api}/documents/ingest",
            json={
                "file_path":   fp_cont,
                "document_id": doc_id,
                "user_id":     user_id,
                "project_id":  project_id,
                "metadata":    {"filename": file_path.name},
            },
        )
        resp.raise_for_status()
    except Exception as e:
        return {"file": file_path.name, "success": False, "message": f"觸發索引失敗: {e}"}

    # Step 3: 輪詢狀態
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp   = client.get(f"{api}/documents/{doc_id}/status")
            status = resp.json()
        except Exception:
            continue

        stage    = status.get("stage", "")
        progress = status.get("progress", 0)
        print(f"    進度：{progress}%  ({stage})", end="\r")

        if status["status"] == "completed":
            print()
            return {"file": file_path.name, "success": True,
                    "message": f"完成 (doc_id={doc_id})"}
        if status["status"] == "failed":
            print()
            return {"file": file_path.name, "success": False,
                    "message": status.get("error", "未知錯誤")}

    return {"file": file_path.name, "success": False, "message": "逾時（超過 120 秒）"}


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _fetch_doc_list(client: httpx.Client, api: str) -> list[dict]:
    try:
        resp = client.get(f"{api}/documents")
        resp.raise_for_status()
        return resp.json().get("documents", [])
    except Exception as e:
        print(f"[!] 無法取得文件清單：{e}")
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="批次索引 / 管理 data/documents/ 下的文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python3 index_documents.py                          # 索引所有文件
  python3 index_documents.py --list                   # 列出已索引文件
  python3 index_documents.py --delete 陸海空軍懲罰法.pdf
  python3 index_documents.py --delete abc123 def456   # 用 document_id 刪除
  python3 index_documents.py --delete-all             # 刪除全部（需確認）
  python3 index_documents.py --delete-all --yes       # 刪除全部（跳過確認）
""",
    )
    p.add_argument("--dir",        default=str(DEFAULT_DIR),  help="文件資料夾路徑")
    p.add_argument("--api",        default=DEFAULT_API,       help="ANILA Core API 位址")
    p.add_argument("--user",       default=DEFAULT_USER,      help="user_id")
    p.add_argument("--project",    default=DEFAULT_PROJECT,   help="project_id")
    p.add_argument("--list",       action="store_true",       help="列出已索引文件")
    p.add_argument("--delete",     nargs="+", metavar="ID",   help="刪除指定文件（document_id 或檔名）")
    p.add_argument("--delete-all", action="store_true",       help="刪除所有已索引文件")
    p.add_argument("--yes", "-y",  action="store_true",       help="跳過刪除確認提示")
    return p.parse_args()


if __name__ == "__main__":
    main()
