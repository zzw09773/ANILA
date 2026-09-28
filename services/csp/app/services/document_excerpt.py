"""長文件超過附件預算時，只留下跟這一輪問題有關的段落。"""
from __future__ import annotations

import inspect
import logging
import math
import re
from collections.abc import Callable, Sequence
from app.services.proxy.usage import _estimate_token_count

logger = logging.getLogger(__name__)

EXCERPT_NOTICE = "以下是文件摘錄，不是全文。"
_EMBED_CHARS = 800

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z0-9]{2,}")


def _tokens(text: str) -> int:
    return _estimate_token_count(None, text)


def split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """依 Markdown 標題切開。(標題, 內文)。標題前的前言標題是空字串。"""
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [("", text.strip())]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.end() : end].strip()))
    return sections or [("", text.strip())]


def _terms(text: str) -> set[str]:
    found = {word.lower() for word in _LATIN.findall(text)}
    chars = [char for char in text if _CJK.match(char)]
    found.update(a + b for a, b in zip(chars, chars[1:]))
    return found


def _keyword_score(query: str, heading: str, body: str) -> float:
    wanted = _terms(query)
    if not wanted:
        return 0.0
    heading_hit = len(wanted & _terms(heading)) / len(wanted)
    body_hit = len(wanted & _terms(body)) / len(wanted)
    return heading_hit * 3.0 + body_hit


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _render(heading: str, body: str) -> str:
    if heading:
        return f"# {heading}\n\n{body}".strip()
    return body


def _fit(text: str, token_budget: int) -> str:
    if token_budget <= 0 or not text:
        return ""
    if _tokens(text) <= token_budget:
        return text
    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _tokens(text[:mid]) <= token_budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def excerpt_generated_document(
    text: str,
    query: str,
    budget_tokens: int,
    *,
    vectors: Sequence[Sequence[float]] | None = None,
    embedder: Callable[[str, list[str]], Sequence[Sequence[float]] | None] | None = None,
) -> str:
    """沒超過預算就原樣退回。超過時附上「這是摘錄」，並盡量留下相關段落。

    ``vectors`` 的第 0 筆是問題，其後與標題段落對齊。長度不合、嵌入失敗，
    或沒有向量時，改用標題關鍵字，並附上目錄。
    """
    if _tokens(text) <= max(budget_tokens, 0):
        return text

    sections = split_markdown_sections(text)
    ranked = vectors
    if ranked is None and embedder is not None:
        try:
            ranked = embedder(query, [f"{heading}\n{body}" for heading, body in sections])
        except Exception:
            ranked = None

    use_embedding = (
        ranked is not None
        and len(ranked) == len(sections) + 1
        and all(isinstance(row, Sequence) for row in ranked)
    )
    if use_embedding:
        scores = [_cosine(ranked[0], ranked[index + 1]) for index in range(len(sections))]
    else:
        scores = [
            _keyword_score(query, heading, body) for heading, body in sections
        ]

    order = sorted(range(len(sections)), key=lambda index: (-scores[index], index))
    positive = any(score > 0 for score in scores)
    pieces = [EXCERPT_NOTICE]
    if not use_embedding:
        headings = [heading for heading, _body in sections if heading]
        if headings:
            pieces.append("目錄\n" + "\n".join(f"- {heading}" for heading in headings))
    used = _tokens("\n\n".join(pieces))
    chosen: list[str] = []
    for index in order:
        if positive and scores[index] <= 0:
            continue
        block = _render(*sections[index])
        cost = _tokens(block)
        separator = 2 if chosen or len(pieces) > 1 or pieces else 0
        if used + cost + separator <= budget_tokens:
            chosen.append(block)
            used += cost + separator
    if not chosen and sections:
        remaining = budget_tokens - used
        fitted = _fit(_render(*sections[order[0]]), remaining)
        if fitted:
            chosen.append(fitted)
    if not chosen:
        return EXCERPT_NOTICE
    return "\n\n".join(pieces + chosen)


async def embed_sections_for_excerpt(user, query: str, section_texts: list[str]) -> list[list[float]] | None:
    """用平台嵌入角色把問題與段落排在一起。沒有角色或呼叫失敗就回 None。"""
    if user is None or not section_texts:
        return None
    from app.database import SessionLocal
    from app.services.endpoint_author_service import visible_endpoint_url
    from app.services.platform_embedding import designated_platform_embedding
    from app.services.proxy.service import proxy_request, resolve_proxy_tuning
    from app.services.proxy.headers import downstream_identity

    db = SessionLocal()
    try:
        model = designated_platform_embedding(db)
        if model is None:
            return None
        from app.services.proxy.snapshot import snapshot_model

        snapshot = snapshot_model(model)
        if not getattr(snapshot, "protocol", None):
            snapshot.protocol = "openai_compatible"
        tuning = resolve_proxy_tuning(db)
        user_id = user.id
        department_id = getattr(user, "department_id", None)
        identity = downstream_identity(user)
        api_version = snapshot.api_version if snapshot.api_version in ("v1", "v2") else "v1"
        endpoint_display = visible_endpoint_url(
            snapshot.endpoint_url,
            is_internal=bool(getattr(snapshot, "is_internal", False)),
            db=db,
            caller=user,
        )
        model_name = snapshot.name
    except Exception:
        logger.exception("document excerpt could not read the platform embedding")
        return None
    finally:
        db.close()

    texts = [query[:_EMBED_CHARS]] + [piece[:_EMBED_CHARS] for piece in section_texts]
    try:
        response = await proxy_request(
            model=snapshot,
            api_key_id=None,
            user_id=user_id,
            user_identity=identity,
            department_id=department_id,
            request_body={"model": model_name, "input": texts},
            endpoint_path=f"/{api_version}/embeddings",
            endpoint_display=endpoint_display,
            embedding_input_role="query",
            record_usage=False,
            tuning=tuning,
        )
        rows = response.get("data") or []
        ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
        vectors = [list(item["embedding"]) for item in ordered]
    except Exception:
        logger.exception("document excerpt embedding failed")
        return None
    if len(vectors) != len(texts):
        return None
    return vectors


async def maybe_excerpt_generated(
    text: str,
    query: str,
    budget_tokens: int,
    *,
    user=None,
    passage_embedder=None,
) -> str:
    """超過預算才摘。嵌入用呼叫端給的函式，否則用平台嵌入角色。"""
    if _tokens(text) <= max(budget_tokens, 0):
        return text
    sections = split_markdown_sections(text)
    payloads = [f"{heading}\n{body}"[:_EMBED_CHARS] for heading, body in sections]
    vectors = None
    try:
        if passage_embedder is not None:
            result = passage_embedder(query, payloads)
            if inspect.isawaitable(result):
                result = await result
            vectors = result
        elif user is not None:
            vectors = await embed_sections_for_excerpt(user, query, payloads)
    except Exception:
        logger.exception("document excerpt ranking failed")
        vectors = None
    return excerpt_generated_document(text, query, budget_tokens, vectors=vectors)
