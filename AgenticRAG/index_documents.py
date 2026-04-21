"""Batch document ingestion CLI — writes directly to pgvector.

This is the **sample agent's** ingestion tool. It uses ``anila-core``
primitives (``IngestionService`` + ``PgVectorStore`` + ``NvidiaEmbeddingProvider``)
to parse / chunk / embed / index documents straight into the DB — no HTTP
layer in between.

Usage:
    # Index every supported file under data/documents/
    python index_documents.py

    # Target a different directory, user, or project
    python index_documents.py --dir ~/docs --user alice --project legal

    # List what's in the index
    python index_documents.py --list

    # Delete by document_id or by filename (partial match accepted)
    python index_documents.py --delete abc123 def456
    python index_documents.py --delete 刑法.pdf

    # Wipe all indexed docs for this user/project
    python index_documents.py --delete-all       # prompts for confirmation
    python index_documents.py --delete-all --yes # skip confirmation

Environment variables read from .env (see .env.example):
    DATABASE_URL             — pgvector connection string
    EMBEDDING_URL            — NV-Embed-V2 endpoint (or CSP_BASE_URL)
    EMBEDDING_API_KEY        — token for embedding endpoint
    EMBEDDING_MODEL          — default: nvidia/nv-embed-v2
    EMBEDDING_VERIFY_SSL     — 'true' / 'false' (default false for internal TLS)
    CSP_BASE_URL / CSP_API_KEY — if set, overrides direct embedding URL

Supported formats: .txt .md .pdf .docx .doc .odt

Fork notes: to swap out the data source, replace the ``cmd_index`` body
with your own document discovery. Keep the ``IngestionService`` call — it's
what guarantees chunks reach the same pgvector table that api.py queries.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from anila_core.ingestion.service import IngestionService
from anila_core.providers.embedding_nvidia import NvidiaEmbeddingProvider
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import PgVectorStore

load_dotenv(Path(__file__).parent / ".env")

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DIR = Path(__file__).parent / "data" / "documents"
DEFAULT_USER = "default"
DEFAULT_PROJECT = "default"
SUPPORTED_EXT = {".txt", ".md", ".pdf", ".docx", ".doc", ".odt"}


# ── Config helpers ────────────────────────────────────────────────────────────

def _build_embedding_provider() -> NvidiaEmbeddingProvider:
    csp_base = os.getenv("CSP_BASE_URL", "").rstrip("/")
    csp_key = os.getenv("CSP_API_KEY", "")
    if csp_base:
        base_url = f"{csp_base}/v1"
        api_key = csp_key or "not-set"
    else:
        base_url = os.getenv("EMBEDDING_URL", "https://172.16.120.35/v1")
        api_key = os.getenv("EMBEDDING_API_KEY", "not-set")

    return NvidiaEmbeddingProvider(
        base_url=base_url,
        api_key=api_key,
        model=os.getenv("EMBEDDING_MODEL", "nvidia/nv-embed-v2"),
        verify_ssl=os.getenv("EMBEDDING_VERIFY_SSL", "false").lower() == "true",
    )


async def _build_service() -> tuple[IngestionService, PgVectorStore, PgPool]:
    dsn = os.getenv("DATABASE_URL", "postgresql://anila:anila@localhost:5432/anila_rag")
    pool = PgPool(dsn=dsn)
    await pool.initialize()

    store = PgVectorStore(pool=pool, dimension=4096)
    await store.initialize_schema()

    service = IngestionService(
        embedding_provider=_build_embedding_provider(),
        document_store=store,
        retrieval_provider=store,
    )
    return service, store, pool


# ── Subcommands ───────────────────────────────────────────────────────────────

async def cmd_index(doc_dir: Path, user_id: str, project_id: str) -> None:
    doc_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in sorted(doc_dir.rglob("*")) if f.suffix.lower() in SUPPORTED_EXT]
    if not files:
        print(f"[!] {doc_dir} 下沒有可索引的文件（{', '.join(sorted(SUPPORTED_EXT))}）")
        print("    請將文件放入資料夾後再執行。")
        return

    print(f"找到 {len(files)} 個文件，開始索引...\n")
    service, _store, pool = await _build_service()

    ok = fail = 0
    for idx, path in enumerate(files, 1):
        print(f"[{idx}/{len(files)}] {path.name}")

        def _on_progress(cur: int, total: int, stage: str) -> None:
            print(f"    {stage}: {cur}/{total}", end="\r")

        try:
            doc_id = await service.ingest(
                file_path=str(path),
                user_id=user_id,
                project_id=project_id,
                metadata={"filename": path.name},
                on_progress=_on_progress,
            )
            print(f"\n  ✅ 完成 (doc_id={doc_id})\n")
            ok += 1
        except Exception as exc:
            print(f"\n  ❌ 失敗: {exc}\n")
            fail += 1

    await pool.close()
    print("=" * 50)
    print(f"索引完成：{ok} 成功 / {fail} 失敗")


async def cmd_list(user_id: str, project_id: str) -> None:
    _service, store, pool = await _build_service()
    try:
        docs = await store.list_all_documents(user_id=user_id, project_id=project_id)
    finally:
        await pool.close()

    if not docs:
        print("（尚無已索引文件）")
        return

    print(f"{'#':<4} {'document_id':<38} {'chunks':>6}  {'最後索引':^22}  檔名")
    print("-" * 100)
    for i, d in enumerate(docs, 1):
        display = d.get("filename") or d.get("title") or Path(d.get("source_path") or "").name or d["document_id"]
        last = str(d.get("last_indexed") or "")[:19].replace("T", " ")
        print(f"{i:<4} {d['document_id']:<38} {d['chunk_count']:>6}  {last:<22}  {display}")
    print(f"\n共 {len(docs)} 份文件")


async def cmd_delete(targets: list[str], user_id: str, project_id: str) -> None:
    service, store, pool = await _build_service()
    try:
        docs = await store.list_all_documents(user_id=user_id, project_id=project_id)

        # Build lookup: filename (lower) → doc_id, and an id set.
        name_map: dict[str, str] = {}
        id_set: set[str] = set()
        for d in docs:
            fname = Path(d.get("source_path") or d.get("filename") or "").name.lower()
            if fname:
                name_map[fname] = d["document_id"]
            id_set.add(d["document_id"])

        to_delete: list[tuple[str, str]] = []
        not_found: list[str] = []

        for t in targets:
            if t in id_set:
                label = next(
                    (Path(d.get("source_path") or d.get("filename") or "").name or t)
                    for d in docs if d["document_id"] == t
                )
                to_delete.append((t, label))
            elif t.lower() in name_map:
                to_delete.append((name_map[t.lower()], t))
            else:
                matches = [(doc_id, name) for name, doc_id in name_map.items() if t.lower() in name]
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
                await service.delete(document_id=doc_id, user_id=user_id, project_id=project_id)
                print(f"  ✅ 已刪除：{label}")
                ok += 1
            except Exception as exc:
                print(f"  ❌ 刪除失敗：{label} ({exc})")
                fail += 1

        print(f"\n完成：{ok} 成功 / {fail} 失敗")
    finally:
        await pool.close()


async def cmd_delete_all(user_id: str, project_id: str, yes: bool) -> None:
    service, store, pool = await _build_service()
    try:
        docs = await store.list_all_documents(user_id=user_id, project_id=project_id)
        if not docs:
            print("（目前沒有已索引文件，無需刪除）")
            return

        print(f"即將刪除全部 {len(docs)} 份已索引文件（scope user={user_id} project={project_id}）：")
        for d in docs:
            filename = Path(d.get("source_path") or d.get("filename") or "unknown").name
            print(f"  - {filename}  ({d['document_id']})")

        if not yes:
            confirm = input("\n確認刪除？(y/N) ").strip().lower()
            if confirm != "y":
                print("已取消。")
                return

        ok = fail = 0
        for d in docs:
            doc_id = d["document_id"]
            filename = Path(d.get("source_path") or d.get("filename") or "").name
            try:
                await service.delete(document_id=doc_id, user_id=user_id, project_id=project_id)
                print(f"  ✅ 已刪除：{filename}")
                ok += 1
            except Exception as exc:
                print(f"  ❌ 刪除失敗：{filename} ({exc})")
                fail += 1

        print(f"\n完成：{ok} 成功 / {fail} 失敗")
    finally:
        await pool.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="批次索引 / 管理 data/documents/ 下的文件（直接連 pgvector）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python index_documents.py                           # 索引所有文件
  python index_documents.py --list                    # 列出已索引文件
  python index_documents.py --delete 刑法.pdf
  python index_documents.py --delete abc123 def456    # 用 document_id 刪除
  python index_documents.py --delete-all              # 刪除全部（需確認）
  python index_documents.py --delete-all --yes        # 刪除全部（跳過確認）
""",
    )
    p.add_argument("--dir", default=str(DEFAULT_DIR), help="文件資料夾路徑")
    p.add_argument("--user", default=DEFAULT_USER, help="user_id scope")
    p.add_argument("--project", default=DEFAULT_PROJECT, help="project_id scope")
    p.add_argument("--list", action="store_true", help="列出已索引文件")
    p.add_argument("--delete", nargs="+", metavar="ID", help="刪除指定文件（document_id 或檔名）")
    p.add_argument("--delete-all", action="store_true", help="刪除所有已索引文件")
    p.add_argument("--yes", "-y", action="store_true", help="跳過刪除確認提示")
    return p.parse_args()


async def _main_async() -> None:
    args = parse_args()
    if args.list:
        await cmd_list(args.user, args.project)
    elif args.delete:
        await cmd_delete(args.delete, args.user, args.project)
    elif args.delete_all:
        await cmd_delete_all(args.user, args.project, yes=args.yes)
    else:
        await cmd_index(Path(args.dir), args.user, args.project)


def main() -> None:
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("\n（中斷）")
        sys.exit(130)


if __name__ == "__main__":
    main()
