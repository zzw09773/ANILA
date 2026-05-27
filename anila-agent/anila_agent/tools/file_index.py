"""P2-7 file-index fuzzy file search — port of claude-code-src ``file-index``。

對應 claude-code-src ``src/native-ts/file-index/index.ts``(370 LOC),將 nucleo / fzf-v2
風格的 fuzzy file search port 成 Python,並包成 :class:`AnilaTool`。

**動機**:

ANILA RAG retriever 只對 chunk 做 ranking,沒有「找檔案」的入口。User 問「auth 相關的
檔案在哪」時,LLM 只能 grep,在大型 workspace 下既慢又吃 token。把 file-index port
過來後,agent 可以呼叫一個 read-only / concurrency-safe 的 ``find_file`` meta-tool
拿到 ranked file path,類似 ``fzf`` / Spotlight 的體驗。

**模組成份**:

* :class:`FileIndex` — 在 workspace 內 walk 建索引,支援 ``refresh()`` 增量重建。
* :func:`fuzzy_search` — 對索引內 path 做 sub-string + fuzzy 評分,回 :class:`FileMatch`。
* :func:`build_find_file_tool` — 把上述包成 :class:`AnilaTool` 給 agent 用。
* :func:`register_file_index_tools` — 一次註冊進 :class:`ToolRegistry`。

**設計選擇**:

1. **不引外部依賴**:只用 ``pathlib`` / ``fnmatch`` / ``difflib`` std lib。
   原本 deep-dive 提到可以引 ``rapidfuzz`` 做 fallback,但為了避免動 ``pyproject.toml``
   全用 std lib 解決。
2. **mtime 增量 refresh**:``refresh()`` 不重 walk 全部,只看每個 dir 的 mtime,沒變的
   dir 直接沿用既有 index entry。對大型 monorepo 加速可觀。
3. **workspace 邊界**:接 :class:`AnilaToolContext`,所有結果都用 ``ctx.safe_path`` 過
   一道,確保不洩漏 workspace 外的 path(縱深防禦)。
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import FunctionTool

from anila_agent.core.context import AnilaToolContext, WorkspaceEscapeError
from anila_agent.tools.base import ToolMetadata, _attach_metadata
from anila_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# 預設 exclude pattern
# ---------------------------------------------------------------------------

# 大部分專案都不該 index 的目錄與檔尾。caller 可在 ``FileIndex(exclude_patterns=...)``
# 完全覆寫,或在 build 之後手動補 extend。
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".git/*",
    "node_modules/*",
    "__pycache__/*",
    ".venv/*",
    "*.pyc",
)

# Meta-tool 名稱 — 不放進 deferred pool,啟動就 expose 給 LLM。
FIND_FILE_TOOL_NAME = "find_file"


# ---------------------------------------------------------------------------
# Match 結構
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileMatch:
    """fuzzy_search 命中結果。

    Attributes:
        path: 命中檔案的絕對 :class:`Path`。
        score: 0..1 之間的相似度分數,越大越相關。
        match_segments: 命中段落的 ``(start, end)`` index list(對 path str 而言),
            供 UI highlight 用;若無法精確標記則為空 list。
    """

    path: Path
    score: float
    match_segments: list[tuple[int, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FileIndex
# ---------------------------------------------------------------------------


@dataclass
class _DirEntry:
    """index 內單一目錄的 mtime + 該目錄底下檔案 list。

    用來做增量 refresh — 比對 mtime 沒變就跳過。
    """

    mtime_ns: int
    files: list[Path]


class FileIndex:
    """workspace 內檔案的 fuzzy-searchable 索引。

    用法:

        idx = FileIndex(Path("/path/to/workspace"))
        idx.build()
        matches = idx.fuzzy_search("auth", top_k=10)
        # ... 之後檔案變動 ...
        idx.refresh()  # 增量重建
    """

    def __init__(
        self,
        workspace_root: Path,
        exclude_patterns: Iterable[str] | None = None,
    ) -> None:
        """建立 index 但不立即 walk。

        Args:
            workspace_root: 要 index 的根目錄;必須存在且為 directory。
            exclude_patterns: ``fnmatch`` glob list(相對 workspace_root 比對)。
                ``None`` → 用 :data:`DEFAULT_EXCLUDE_PATTERNS`。
                空 list / tuple → 不排除任何檔案。
        """
        root = Path(workspace_root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"workspace_root not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"workspace_root not a directory: {root}")

        self._root: Path = root
        # `is None` 用預設,空 tuple/list 視為「明確不排除」。
        self._exclude_patterns: tuple[str, ...] = (
            DEFAULT_EXCLUDE_PATTERNS
            if exclude_patterns is None
            else tuple(exclude_patterns)
        )
        self._files: list[Path] = []
        # dir path → entry,refresh 時拿來比對 mtime。
        self._dir_entries: dict[Path, _DirEntry] = {}
        self._built: bool = False

    # ---- public 屬性 ----------------------------------------------------

    @property
    def root(self) -> Path:
        """workspace 根目錄(已 resolve)。"""
        return self._root

    @property
    def exclude_patterns(self) -> tuple[str, ...]:
        """有效的 exclude pattern。"""
        return self._exclude_patterns

    @property
    def files(self) -> list[Path]:
        """目前 index 內所有檔案的淺拷貝。"""
        return list(self._files)

    def __len__(self) -> int:
        return len(self._files)

    # ---- build / refresh -----------------------------------------------

    def build(self) -> None:
        """全量 walk workspace_root 建索引。

        重複呼叫等同 ``refresh()`` 但會把整份結構重建,適合「我知道整棵樹都動過」的情境。
        """
        self._files = []
        self._dir_entries = {}
        for dir_path, files in self._walk(self._root):
            self._dir_entries[dir_path] = _DirEntry(
                mtime_ns=self._dir_mtime_ns(dir_path),
                files=files,
            )
            self._files.extend(files)
        self._files.sort()
        self._built = True

    def refresh(self) -> None:
        """以 dir mtime 增量更新索引。

        對每個目錄,若 mtime 不變則沿用先前的 files list;若有變動則重 listdir 該目錄。
        對新出現的 sub-dir 會遞迴進去 walk。已消失的 dir 整批 entry 移除。

        若還沒 :meth:`build` 過,直接退回 full :meth:`build`。
        """
        if not self._built:
            self.build()
            return

        new_dir_entries: dict[Path, _DirEntry] = {}
        new_files: list[Path] = []
        self._refresh_dir(self._root, new_dir_entries, new_files)

        self._dir_entries = new_dir_entries
        new_files.sort()
        self._files = new_files

    # ---- search ---------------------------------------------------------

    def fuzzy_search(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[FileMatch]:
        """對索引內 path 做 fuzzy match,回前 ``top_k`` 個 :class:`FileMatch`。

        評分策略(對齊 deep-dive §4.17 的 nucleo-style bonus):

        1. **filename match** > **path match** > **full path match**。
        2. **連續 substring 命中** 額外 +bonus。
        3. **檔名開頭命中** 額外 +bonus。
        4. **caseless** —— 比對全用 lowercase,LLM query 不需要 case 完全對齊。

        若 ``query`` 為空字串,回傳依 path 排序的前 ``top_k`` 筆(score 為 0.0)。

        Args:
            query: 搜尋字串。空字串 / 全空白 → 不做評分,直接回前 top_k 個檔案。
            top_k: 最多回傳幾筆;``<=0`` 視為「全部」。

        Returns:
            ``FileMatch`` list,依 score 由高到低排序;score 相同時依 path 字典序。
        """
        if not self._built:
            return []

        q = query.strip().lower()
        if not q:
            files = self._files if top_k <= 0 else self._files[:top_k]
            return [FileMatch(path=p, score=0.0, match_segments=[]) for p in files]

        scored: list[FileMatch] = []
        for path in self._files:
            score, segments = _score_path(q, path, self._root)
            if score <= 0.0:
                continue
            scored.append(FileMatch(path=path, score=score, match_segments=segments))

        # 主 key: score desc;次 key: path asc(可重現)。
        scored.sort(key=lambda m: (-m.score, str(m.path)))
        return scored if top_k <= 0 else scored[:top_k]

    # ---- 內部 walk / refresh helper -------------------------------------

    def _walk(self, root: Path) -> Iterable[tuple[Path, list[Path]]]:
        """遞迴 walk root,逐目錄 yield (dir_path, [files])。

        被 exclude 的 dir 整批跳過(不遞迴進去),被 exclude 的檔案個別跳過。
        遇到無法讀的目錄(PermissionError / OSError)直接跳過,不擴散例外。
        """
        try:
            entries = sorted(root.iterdir())
        except (PermissionError, OSError):
            return

        files_here: list[Path] = []
        sub_dirs: list[Path] = []
        for child in entries:
            rel = self._rel_for_match(child)
            if self._is_excluded(rel):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                sub_dirs.append(child)
            elif child.is_file():
                files_here.append(child)

        yield root, files_here
        for sub in sub_dirs:
            yield from self._walk(sub)

    def _refresh_dir(
        self,
        dir_path: Path,
        new_dir_entries: dict[Path, _DirEntry],
        new_files: list[Path],
    ) -> None:
        """refresh 子流程 — 對單一 dir 比對 mtime 決定增量還是 re-walk。"""
        try:
            current_mtime = self._dir_mtime_ns(dir_path)
        except (FileNotFoundError, OSError):
            return  # dir 已不存在 → 不放回 new_*

        prev = self._dir_entries.get(dir_path)
        if prev is not None and prev.mtime_ns == current_mtime:
            # 沿用既有 files,但仍需遞迴進已知 sub-dir 檢查(也許子目錄變了)。
            new_dir_entries[dir_path] = _DirEntry(
                mtime_ns=current_mtime, files=list(prev.files)
            )
            new_files.extend(prev.files)
            for sub in self._known_sub_dirs(dir_path):
                self._refresh_dir(sub, new_dir_entries, new_files)
            return

        # mtime 變了 → 對這個 dir 重 list,並對所有 sub-dir 遞迴。
        try:
            entries = sorted(dir_path.iterdir())
        except (PermissionError, OSError):
            return

        files_here: list[Path] = []
        sub_dirs: list[Path] = []
        for child in entries:
            rel = self._rel_for_match(child)
            if self._is_excluded(rel):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                sub_dirs.append(child)
            elif child.is_file():
                files_here.append(child)

        new_dir_entries[dir_path] = _DirEntry(
            mtime_ns=current_mtime, files=files_here
        )
        new_files.extend(files_here)
        for sub in sub_dirs:
            self._refresh_dir(sub, new_dir_entries, new_files)

    def _known_sub_dirs(self, dir_path: Path) -> list[Path]:
        """從前一份 dir_entries 推回某 dir 已知的直屬 sub-dir 集合。"""
        prefix = str(dir_path) + "/"
        out: list[Path] = []
        for known in self._dir_entries:
            ks = str(known)
            if ks == str(dir_path) or not ks.startswith(prefix):
                continue
            rel = ks[len(prefix) :]
            if "/" not in rel:  # 直屬子目錄
                out.append(known)
        return out

    @staticmethod
    def _dir_mtime_ns(dir_path: Path) -> int:
        return dir_path.stat().st_mtime_ns

    def _rel_for_match(self, path: Path) -> str:
        """把 path 轉成對 root 的相對 POSIX 字串,供 fnmatch 比對。"""
        try:
            return path.relative_to(self._root).as_posix()
        except ValueError:
            # path 不在 root 底下(理論上不會發生 — walk 從 root 起步)
            return path.as_posix()

    def _is_excluded(self, rel: str) -> bool:
        """以 fnmatch 比對 ``rel`` 是否中任何一個 exclude pattern。

        為了讓 ``.git/*`` 同時 match `.git` 自身與 `.git/foo`,把 pattern 拆兩種:

        * 原 pattern(``a/b/*``)── 直接 fnmatch。
        * stripped pattern(``a/b``)── 去尾 ``/*`` 後 fnmatch,涵蓋目錄自身的 case。
        * basename pattern(``*.pyc``)── 對 basename 也比一次,涵蓋深層檔。
        """
        basename = rel.rsplit("/", 1)[-1]
        for pat in self._exclude_patterns:
            if fnmatch.fnmatch(rel, pat):
                return True
            if pat.endswith("/*") and fnmatch.fnmatch(rel, pat[:-2]):
                return True
            if "/" not in pat and fnmatch.fnmatch(basename, pat):
                return True
        return False


# ---------------------------------------------------------------------------
# 評分函式
# ---------------------------------------------------------------------------


# 評分常數 — 對齊 deep-dive §4.17 的 fzf-v2 / nucleo style。
_SCORE_MATCH = 16
_BONUS_BOUNDARY = 8
_BONUS_FIRST_CHAR = 8
_BONUS_CONSECUTIVE = 4
_PENALTY_GAP_START = 3
_PENALTY_GAP_EXTENSION = 1
# 不同欄位的加權 — filename 比 full path 重要。
_WEIGHT_FILENAME = 2.0
_WEIGHT_RELATIVE_PATH = 1.0


def _score_path(
    query_lower: str, path: Path, root: Path
) -> tuple[float, list[tuple[int, int]]]:
    """對單一 path 算 score,回傳 (score_0_to_1, match_segments)。

    Args:
        query_lower: 已 lowercase 的 query 字串。
        path: 候選檔案 absolute path。
        root: workspace root,用來算 relative path。

    Returns:
        ``(score, segments)`` — score 介於 0..1;若 query 完全沒命中則 0.0,
        對應的 ``segments`` 為空 list。
    """
    if not query_lower:
        return 0.0, []

    filename = path.name
    try:
        rel_path = path.relative_to(root).as_posix()
    except ValueError:
        rel_path = path.as_posix()

    # 個別對 filename 與 relative path 做兩種 match。
    name_score, name_segs = _fuzzy_score(query_lower, filename)
    path_score, path_segs = _fuzzy_score(query_lower, rel_path)

    # 對 segments 來說,若 filename 命中且 path 命中,我們回 path-level segments
    # (UI 通常 highlight rel path);否則回有 score 那邊的 segments。
    weighted = max(
        name_score * _WEIGHT_FILENAME,
        path_score * _WEIGHT_RELATIVE_PATH,
    )
    if weighted <= 0.0:
        return 0.0, []

    # normalize 到 0..1 — 用「最大可能 score」=(最高權重 * query 長度 * 滿分常數組合)
    # 估算上界。實務上不會貼到 1.0,但能保證單調且穩定。
    max_possible = _max_score(query_lower) * _WEIGHT_FILENAME
    score = min(1.0, weighted / max_possible) if max_possible > 0 else 0.0

    if name_score * _WEIGHT_FILENAME >= path_score * _WEIGHT_RELATIVE_PATH:
        # filename 主導 — 把 segments 從 filename offset 平移到 rel_path 內。
        offset = len(rel_path) - len(filename) if rel_path.endswith(filename) else 0
        segments = [(s + offset, e + offset) for s, e in name_segs]
    else:
        segments = path_segs
    return score, segments


def _fuzzy_score(query: str, target: str) -> tuple[float, list[tuple[int, int]]]:
    """nucleo-style sub-sequence + bonus 評分。

    對 ``query`` 的每個 char,從 target 內依序找下一個出現位置;命中即加 :data:`_SCORE_MATCH`,
    再依「是否在 word boundary / first char / consecutive」加 bonus,「跳過幾個 char」扣
    penalty。若整段 query 沒有完全 subsequence 命中,score = 0。
    """
    if not query:
        return 0.0, []

    target_lower = target.lower()
    n_q = len(query)
    n_t = len(target_lower)
    if n_q > n_t:
        return 0.0, []

    score = 0
    i = 0  # query index
    j = 0  # target index
    last_match: int = -2  # 上次命中 index — -2 確保第一次命中不被當 consecutive
    segments: list[tuple[int, int]] = []
    seg_start: int | None = None

    while i < n_q and j < n_t:
        if query[i] == target_lower[j]:
            score += _SCORE_MATCH
            if j == 0:
                score += _BONUS_FIRST_CHAR
            elif _is_boundary(target_lower, j):
                score += _BONUS_BOUNDARY
            if j == last_match + 1:
                score += _BONUS_CONSECUTIVE
            else:
                # 跳過 gap 扣分(query 第一個 char 不算 gap)。
                if last_match >= 0:
                    gap = j - last_match - 1
                    score -= _PENALTY_GAP_START + _PENALTY_GAP_EXTENSION * max(
                        0, gap - 1
                    )
            if seg_start is None:
                seg_start = j
            last_match = j
            i += 1
            j += 1
        else:
            if seg_start is not None:
                segments.append((seg_start, last_match + 1))
                seg_start = None
            j += 1

    if i < n_q:  # query 還沒掃完 → 沒完整命中
        return 0.0, []

    if seg_start is not None:
        segments.append((seg_start, last_match + 1))

    return float(max(score, 0)), segments


def _is_boundary(s: str, idx: int) -> bool:
    """idx 是否為 word boundary —— 前一字元是 non-alnum 或 / / _ / -。"""
    if idx <= 0:
        return True
    prev = s[idx - 1]
    return not prev.isalnum() or prev in "/_-."


def _max_score(query: str) -> float:
    """估算 query 在某 target 上能拿到的最大可能 score(用於 normalize)。

    保守上界:每個 char 都拿 match + boundary,首字元再 +first_char + consecutive。
    """
    if not query:
        return 0.0
    n = len(query)
    return float(
        n * (_SCORE_MATCH + _BONUS_BOUNDARY + _BONUS_CONSECUTIVE) + _BONUS_FIRST_CHAR
    )


# ---------------------------------------------------------------------------
# 高層 fuzzy_search(供 caller 直接用,不一定要持有 FileIndex)
# ---------------------------------------------------------------------------


def fuzzy_search(
    index: FileIndex,
    query: str,
    top_k: int = 10,
) -> list[FileMatch]:
    """便利 wrapper — 呼叫 :meth:`FileIndex.fuzzy_search`。

    保留為 module-level function,跟 :mod:`anila_agent.tools.tool_search` 的
    :func:`search_deferred` 風格一致(讓 caller 可以用 function 形式直接拿來測)。
    """
    return index.fuzzy_search(query, top_k=top_k)


# ---------------------------------------------------------------------------
# Meta-tool:find_file
# ---------------------------------------------------------------------------


_FIND_FILE_METADATA = ToolMetadata(
    is_read_only=True,
    category="filesystem",
    cost_estimate="low",
    is_deferred=False,
    concurrency_safe=True,
)


def _resolve_index_root(
    index: FileIndex, ctx: AnilaToolContext | None
) -> Path:
    """取得最終要用來 build / refresh 的 root。

    若 ctx.workspace 與 index.root 不同,**以 ctx.workspace 為準** —— P0-3 規定:tool
    一律以 AnilaToolContext.workspace 為信任邊界。實際上 caller 在 build_find_file_tool
    時就應該對齊兩者,這裡只是縱深防禦。
    """
    if ctx is not None and ctx.workspace is not None:
        return ctx.workspace
    return index.root


def _format_matches(
    matches: list[FileMatch],
    ctx: AnilaToolContext | None,
    root: Path,
) -> str:
    """把 FileMatch list 序列化成 LLM 可讀的 JSON 字串。

    每筆額外回:相對 root 的 path 字串(避免洩漏完整 workspace 外的 prefix)、score、
    match_segments。若 ctx.safe_path 判定某 path 跳脫 workspace(理論不會發生 — index
    本來就在 workspace 內),直接從結果剔除做縱深防禦。
    """
    payload_matches: list[dict[str, Any]] = []
    for m in matches:
        path = m.path
        if ctx is not None and ctx.workspace is not None:
            try:
                ctx.safe_path(path)
            except WorkspaceEscapeError:
                continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        payload_matches.append(
            {
                "path": rel,
                "absolute_path": str(path),
                "score": round(m.score, 4),
                "match_segments": list(m.match_segments),
            }
        )
    payload: dict[str, Any] = {
        "matches": payload_matches,
        "count": len(payload_matches),
        "root": str(root),
    }
    if not payload_matches:
        payload["hint"] = (
            "No files matched the query. Try a shorter substring or a partial filename."
        )
    return json.dumps(payload, ensure_ascii=False)


ContextResolver = Callable[[], AnilaToolContext | None]
"""callable 型別 — 給 tool callback 拿當下 :class:`AnilaToolContext`(或 None)。"""


def build_find_file_tool(
    index: FileIndex,
    *,
    context_resolver: ContextResolver | None = None,
    auto_build: bool = True,
) -> FunctionTool:
    """建立 ``find_file`` :class:`FunctionTool`。

    Args:
        index: 要查詢的 :class:`FileIndex`。**呼叫者** 通常已先 ``index.build()``;
            若 ``auto_build=True``(預設),tool 第一次被呼叫時若還沒 build 過會自動
            build。
        context_resolver: callable,回傳當下 :class:`AnilaToolContext`(或 None)。
            若有 ctx,結果 path 會用 :meth:`AnilaToolContext.safe_path` 過濾;沒有 ctx
            就退回 ``index.root`` 為 root。
        auto_build: 若 index 還沒 build,第一次呼叫時自動 build。

    Returns:
        :class:`FunctionTool`,可直接 add 進 :class:`ToolRegistry`。
    """

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return json.dumps(
                {"matches": [], "count": 0, "error": "invalid_json"},
                ensure_ascii=False,
            )
        query = str(args.get("query") or "").strip()
        raw_top_k = args.get("top_k", 10)
        try:
            top_k = int(raw_top_k)
        except (TypeError, ValueError):
            top_k = 10

        ctx = context_resolver() if context_resolver is not None else None
        if auto_build and not index._built:  # module-internal flag check
            index.build()

        root = _resolve_index_root(index, ctx)
        matches = index.fuzzy_search(query, top_k=top_k)
        return _format_matches(matches, ctx, root)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Filename or path substring to fuzzy-match. "
                    "Empty string returns the first top_k files."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max number of file matches to return (default 10).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=FIND_FILE_TOOL_NAME,
        description=(
            "Fuzzy-search files inside the workspace by filename or path substring. "
            "Returns ranked file paths with score and match segments. Read-only."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _FIND_FILE_METADATA)


def register_file_index_tools(
    registry: ToolRegistry,
    index: FileIndex,
    *,
    context_resolver: ContextResolver | None = None,
    auto_build: bool = True,
) -> list[FunctionTool]:
    """建立 ``find_file`` tool 並註冊進 registry(非 deferred)。

    Args:
        registry: 目標 :class:`ToolRegistry`。
        index: 已建構好(或會自動 build)的 :class:`FileIndex`。
        context_resolver: 可選 callable,回傳當下 :class:`AnilaToolContext`。
        auto_build: 第一次 tool 呼叫時若 index 還沒 build,是否自動 build。

    Returns:
        register 進 registry 的 tool list(目前是 ``[find_file]``)。

    Raises:
        ValueError: registry 已有同名 tool。
    """
    tools = [
        build_find_file_tool(
            index,
            context_resolver=context_resolver,
            auto_build=auto_build,
        )
    ]
    for tool in tools:
        registry.register(tool, deferred=False)
    return tools


__all__ = [
    "DEFAULT_EXCLUDE_PATTERNS",
    "FIND_FILE_TOOL_NAME",
    "ContextResolver",
    "FileIndex",
    "FileMatch",
    "build_find_file_tool",
    "fuzzy_search",
    "register_file_index_tools",
]
