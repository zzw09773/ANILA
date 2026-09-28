"""標題與大字數字不得寫進來源沒有的專名或數字。

來源只有「GaN 功率放大器，陣列規模 16×16，共 256 個單元…冷板入口溫度上限 35 度」
時，模型若把簡報題成「X 波段 16×16 相位陣列初步設計」，「X 波段」要被標出；
16×16、256、35 在來源裡，不標。仍不接地就從標題拿掉並在 JobStatus.warning
留一句，工作不因此失敗。
"""
from __future__ import annotations

import copy
import json
import logging

from app.services.studio_grounding import (
    GROUNDING_TITLE_WARNING,
    apply_grounding,
    grounding_retry_instruction,
    ungrounded_tokens,
)


SOURCE = "GaN 功率放大器，陣列規模 16×16，共 256 個單元…冷板入口溫度上限 35 度"

_SUPPORTING = "來源寫明此陣列的單元數，投影片只沿用該數字，不另作推估或換算。"


def _deck(title: str = "X 波段 16×16 相位陣列初步設計") -> dict:
    return {
        "title": title,
        "theme": "corporate_navy",
        "slides": [
            {
                "title": "封面",
                "layout_kind": "section_break",
                "bullets": ["陣列規模與溫度上限"],
            },
            {
                "title": "陣列共 256 個單元",
                "layout_kind": "stat_callout",
                "bullets": ["單元數量沿用來源"],
                "stat": {
                    "value": "256",
                    "label": "單元",
                    "supporting": _SUPPORTING,
                },
            },
            {
                "title": "冷板入口溫度上限 35 度",
                "bullets": ["入口溫度沿用來源"],
            },
        ],
    }


def _outline() -> dict:
    return {
        "title": "X 波段 16×16 相位陣列初步設計",
        "theme": "corporate_navy",
        "sections": [
            {
                "heading": "陣列規模",
                "slides": [
                    {
                        "title": "X 波段 16×16 相位陣列初步設計",
                        "evidence": "number",
                        "query": "陣列 單元 數量",
                    },
                    {
                        "title": "冷板入口溫度 35 度",
                        "evidence": "number",
                        "query": "冷板 入口 溫度",
                    },
                ],
            }
        ],
    }


async def test_invented_x_band_is_removed_and_source_numbers_stay():
    """生產碼若把「X 波段」留在標題，或把 16×16／256／35 當成沒根據，此測試失敗。"""
    seen: list[list[str]] = []

    async def reask(tokens: list[str]) -> dict:
        seen.append(list(tokens))
        return copy.deepcopy(_deck())

    result = await apply_grounding(_deck(), SOURCE, kind="deck", reask=reask)

    assert seen == [["X 波段"]]
    assert result.data["title"] == "16×16 相位陣列初步設計"
    assert result.warning == GROUNDING_TITLE_WARNING
    assert result.removed == ["X 波段"]
    assert "16×16" in result.data["title"]
    assert result.data["slides"][1]["stat"]["value"] == "256"
    assert "35" in result.data["slides"][2]["title"]


def test_times_sign_and_ascii_x_count_as_the_same_number():
    assert ungrounded_tokens("16×16", "陣列規模 16x16") == []
    assert ungrounded_tokens("16x16", "陣列規模 16×16") == []
    assert ungrounded_tokens("X波段", SOURCE) == ["X 波段"]


async def test_grounded_deck_does_not_retry():
    deck = _deck("16×16 GaN 功率放大器")

    async def reask(tokens: list[str]) -> dict:
        raise AssertionError(f"grounded deck must not retry, got {tokens}")

    result = await apply_grounding(deck, SOURCE, kind="deck", reask=reask)

    assert result.warning is None
    assert result.removed == []
    assert result.data["title"] == "16×16 GaN 功率放大器"
    assert result.data["slides"][1]["title"] == "陣列共 256 個單元"


async def test_retry_replaces_invented_band_without_warning():
    fixed = _deck("16×16 相位陣列初步設計")
    calls = 0

    async def reask(tokens: list[str]) -> dict:
        nonlocal calls
        calls += 1
        assert tokens == ["X 波段"]
        return fixed

    result = await apply_grounding(_deck(), SOURCE, kind="deck", reask=reask)

    assert calls == 1
    assert result.warning is None
    assert result.removed == []
    assert result.data["title"] == "16×16 相位陣列初步設計"


def test_cjk_words_and_bare_band_qualifier_are_not_flagged():
    title = "相位陣列初步設計與冷板散熱的波段規劃"
    assert ungrounded_tokens(title, SOURCE) == []
    assert ungrounded_tokens(title, "完全無關的段落") == []


def test_retry_instruction_names_the_tokens():
    text = grounding_retry_instruction(["X 波段"])
    assert "X 波段" in text
    assert "these are not in the source; remove or replace them" in text


async def test_removed_tokens_are_logged(caplog):
    caplog.set_level(logging.INFO)

    async def reask(tokens: list[str]) -> dict:
        return copy.deepcopy(_deck())

    await apply_grounding(_deck(), SOURCE, kind="deck", reask=reask)

    removed_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.services.studio_grounding" and "X 波段" in r.getMessage()
    ]
    assert removed_logs
    assert all("audit" not in r.name for r in caplog.records)


def test_prompts_tell_the_model_not_to_invent_names_or_numbers():
    from app.services.studio_llm import build_generation_prompt
    from app.services.studio_outline import build_outline_prompt

    chunk = {
        "filename": "gan.txt",
        "chunk_key": "c1",
        "content": SOURCE,
        "score": 0.9,
    }
    outline_system, _user = build_outline_prompt(
        "功率元件",
        "教學投影片",
        None,
        [chunk],
        count_hint="8-12 張投影片",
        min_slides=8,
    )
    deck_system, _user = build_generation_prompt(
        "功率元件",
        "教學投影片",
        None,
        [chunk],
        retrieval_failed=False,
    )
    for prompt in (outline_system, deck_system):
        assert "型號" in prompt
        assert "料號" in prompt
        assert "平實" in prompt
        assert "來源" in prompt


async def test_outline_still_ungrounded_after_retry_is_stripped_before_content(monkeypatch):
    """大綱兩次都寫「X 波段」時，寫投影片那一輪不該再看到這個詞，並帶回警告。"""
    from app.api import studio as studio_mod

    bad_outline = json.dumps(_outline(), ensure_ascii=False)
    fixed_deck = json.dumps(
        _deck("16×16 相位陣列初步設計"),
        ensure_ascii=False,
    )
    calls: list[list[dict]] = []

    async def fake_llm(bearer, model, messages, **kwargs):
        calls.append(messages)
        if len(calls) <= 2:
            return bad_outline
        return fixed_deck

    async def fake_retrieve(bearer, collection_id, query, *, top_k=4):
        return [{
            "filename": "gan.txt",
            "chunk_key": "k1",
            "content": SOURCE,
            "score": 0.8,
        }]

    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve)
    seed = [{
        "filename": "gan.txt",
        "chunk_key": "c1",
        "content": SOURCE,
        "score": 0.9,
    }]
    two_pass: dict = {"collection_id": 2}
    spec, fallback, warning = await studio_mod._generate_validated_spec(
        "b",
        "功率元件",
        "教學投影片",
        None,
        seed,
        retrieval_failed=False,
        two_pass=two_pass,
    )

    assert fallback is False
    assert warning == GROUNDING_TITLE_WARNING
    assert len(calls) == 3
    content_user = calls[2][1]["content"]
    assert "X 波段" not in content_user
    assert "16×16" in content_user
    assert "256" in content_user or "35" in content_user
    outline = two_pass["outline"]
    assert outline.title == "16×16 相位陣列初步設計"
    assert outline.all_slides()[0].title == "16×16 相位陣列初步設計"
    assert "X" not in spec.title
    assert spec.title


async def test_job_status_warning_when_title_tokens_are_removed(monkeypatch, tmp_path):
    from app.api import studio as studio_mod
    from app.auth import CurrentUserIdentity
    from app.clients.csp_client import CollectionMeta
    from app.config import settings as settings_mod
    from app.schemas.studio import GenerateSpecRequest
    from app.services import studio_job_service as job_mod

    monkeypatch.setattr(settings_mod, "ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(studio_mod, "TWO_PASS_ENABLED", False)

    bad = json.dumps(_deck(), ensure_ascii=False)
    calls: list[list[dict]] = []

    async def fake_get_collection(collection_id, *, bearer):
        return CollectionMeta(
            id=collection_id,
            name="功率元件",
            embedding_model="dummy",
            embedding_dim=512,
            status="active",
            created_by=1,
        )

    async def fake_retrieve_chunks(bearer, collection_id, seed_query):
        return [{
            "filename": "gan.txt",
            "chunk_key": "c1",
            "content": SOURCE,
            "score": 0.91,
        }]

    async def fake_retrieve_images(bearer, collection_id, seed_query):
        return []

    async def fake_call_llm_chat(bearer, model, messages, **kwargs):
        calls.append(messages)
        return bad

    async def fake_render_pptx(spec, images_lookup, **kwargs):
        return b"PK-fake-pptx", None

    async def fake_rebalance(spec_dict, violations, chunks_text, *, bearer):
        return spec_dict

    monkeypatch.setattr(studio_mod, "get_collection", fake_get_collection)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve_chunks)
    monkeypatch.setattr(studio_mod, "_retrieve_images", fake_retrieve_images)
    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_call_llm_chat)
    monkeypatch.setattr(studio_mod, "_render_pptx", fake_render_pptx)
    monkeypatch.setattr(studio_mod, "_rebalance_layouts", fake_rebalance)

    identity = CurrentUserIdentity(id=7, username="alice", role="user", token_version=0)
    payload = GenerateSpecRequest(collection_id=1, preset="教學投影片")
    job_mod._reset_for_tests()

    async def runner(updater):
        await studio_mod._run_pipeline(
            identity=identity, bearer="t", payload=payload, updater=updater,
        )

    rec = await job_mod.create_job(
        user_id=identity.id, collection_id=1, runner=runner, report_ctx=None,
    )
    await job_mod.get_job(rec.job_id).task
    status = job_mod.get_job(rec.job_id).to_status()
    job_mod._reset_for_tests()

    assert status.state == "done"
    assert status.error is None
    assert status.warning == GROUNDING_TITLE_WARNING
    assert status.title == "16×16 相位陣列初步設計"
    assert len(calls) == 2
    retry_user = calls[1][-1]["content"]
    assert "X 波段" in retry_user
    assert "these are not in the source; remove or replace them" in retry_user


_USER_TITLE = "NX-9 2026 Q3 Lightning Talk 液冷報告"
_USER_INSTRUCTIONS = "標題用 2026 Q3 液冷報告"


def _user_title_deck() -> dict:
    return {
        "title": _USER_TITLE,
        "theme": "corporate_navy",
        "slides": [
            {
                "title": "封面",
                "layout_kind": "section_break",
                "bullets": ["液冷報告"],
            },
            {
                "title": _USER_TITLE,
                "bullets": ["標題依補充指示"],
            },
        ],
    }


def _user_title_outline() -> dict:
    return {
        "title": _USER_TITLE,
        "sections": [
            {
                "heading": "報告範圍",
                "slides": [
                    {
                        "title": _USER_TITLE,
                        "evidence": "list",
                        "query": "液冷",
                    },
                ],
            }
        ],
    }


async def test_user_text_allows_year_quarter_and_labels_missing_from_source():
    """補充指示、知識庫名稱、風格名稱裡的詞不算編造。來源沒寫 2026 或 Q3 也要原樣留下。"""

    async def reask(tokens: list[str]) -> dict:
        raise AssertionError(tokens)

    for kind, payload in (("deck", _user_title_deck()), ("outline", _user_title_outline())):
        result = await apply_grounding(
            payload,
            SOURCE,
            kind=kind,
            reask=reask,
            collection_name="NX-9 液冷專案",
            extra_instructions=_USER_INSTRUCTIONS,
            preset="Lightning Talk",
        )
        assert result.warning is None
        assert result.removed == []
        assert result.data["title"] == _USER_TITLE


def test_year_absent_from_source_and_user_text_is_still_flagged():
    assert ungrounded_tokens("2026 Q3 液冷報告", SOURCE) == ["2026", "Q3"]


def test_generic_presentation_words_and_section_numbers_are_not_flagged():
    title = "Q&A FAQ SOP KPI AI vs PPT Agenda Summary 01/02 第 1 章"
    assert ungrounded_tokens(title, "無關段落") == []
    assert ungrounded_tokens(title.lower(), "無關段落") == []
    assert ungrounded_tokens("SUMMARY 與 ０１", "無關段落") == []
    assert ungrounded_tokens("第 1 章 2026", "無關段落") == ["2026"]


async def test_outline_and_deck_keep_title_taken_from_user_instructions(monkeypatch):
    from app.api import studio as studio_mod

    outline_json = json.dumps(_user_title_outline(), ensure_ascii=False)
    deck_json = json.dumps(_user_title_deck(), ensure_ascii=False)
    calls: list[list[dict]] = []

    async def fake_llm(bearer, model, messages, **kwargs):
        calls.append(messages)
        return outline_json if len(calls) == 1 else deck_json

    async def fake_retrieve(bearer, collection_id, query, *, top_k=4):
        return [{
            "filename": "gan.txt",
            "chunk_key": "k1",
            "content": SOURCE,
            "score": 0.8,
        }]

    monkeypatch.setattr(studio_mod, "_call_llm_chat", fake_llm)
    monkeypatch.setattr(studio_mod, "_retrieve_chunks", fake_retrieve)
    seed = [{
        "filename": "gan.txt",
        "chunk_key": "c1",
        "content": SOURCE,
        "score": 0.9,
    }]
    two_pass: dict = {"collection_id": 2}
    spec, fallback, warning = await studio_mod._generate_validated_spec(
        "b",
        "NX-9 液冷專案",
        "Lightning Talk",
        _USER_INSTRUCTIONS,
        seed,
        retrieval_failed=False,
        two_pass=two_pass,
    )

    assert fallback is False
    assert warning is None
    assert len(calls) == 2
    assert spec.title == _USER_TITLE
    assert two_pass["outline"].title == _USER_TITLE
    assert two_pass["outline"].all_slides()[0].title == _USER_TITLE


async def test_ungrounded_stat_bullet_and_subtitle_are_cleared_after_retry():
    """重試仍寫 1200 時，統計、副標與條列都要拿掉，不能只改標題。"""
    deck = _deck("相位陣列")
    deck["subtitle"] = "型號 NX-9"
    deck["slides"][1]["stat"]["value"] = "1200"
    deck["slides"][1]["stat"]["baseline"] = "900"
    deck["slides"][1]["bullets"] = ["沿用 1200", "單元數量沿用來源"]

    async def reask(tokens: list[str]) -> dict:
        assert "1200" in tokens
        assert "NX-9" in tokens
        return copy.deepcopy(deck)

    result = await apply_grounding(deck, SOURCE, kind="deck", reask=reask)

    assert result.data["slides"][1]["stat"]["value"] == "—"
    assert result.data["slides"][1]["stat"]["baseline"] is None
    assert all("1200" not in str(item) for item in result.data["slides"][1]["bullets"])
    assert "NX-9" not in (result.data.get("subtitle") or "")
    assert result.warning is not None
    assert "統計數字" in result.warning
    assert "條列" in result.warning
    assert "副標" in result.warning


async def test_ungrounded_supporting_quote_steps_and_table_are_cleared():
    deck = _deck("相位陣列")
    slide = deck["slides"][1]
    slide["key_message"] = "型號 ZX-1 已量產"
    slide["stat"]["label"] = "ZX-1"
    slide["stat"]["supporting"] = "補充說明提到 8800 組備援，來源沒有這組數字。"
    slide["quote"] = {"text": "負責人說 8800 已驗收", "attribution": "ZX-1 計畫"}
    slide["steps"] = [
        {"heading": "步驟 8800", "description": "沿用來源"},
        {"heading": "複核", "description": "對照文件"},
    ]
    slide["table"] = {
        "columns": ["項目", "ZX-1"],
        "rows": [["備援", "8800"]],
    }

    async def reask(tokens: list[str]) -> dict:
        assert "8800" in tokens
        assert "ZX-1" in tokens
        return copy.deepcopy(deck)

    result = await apply_grounding(deck, SOURCE, kind="deck", reask=reask)
    cleaned = result.data["slides"][1]
    assert "8800" not in json.dumps(cleaned, ensure_ascii=False)
    assert "ZX-1" not in json.dumps(cleaned, ensure_ascii=False)
    assert len(cleaned["stat"]["supporting"]) >= 20
    assert result.warning is not None
    for label in ("統計數字", "引言", "重點", "步驟", "表格"):
        assert label in result.warning
