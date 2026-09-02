"""第五次活體還剩兩種弱頁：
- 「懲戒判決確定後之行政作業」整頁只有一條 bullet（空心頁）；
- 「申訴程序流程」用「A → B → C」寫在 bullet 裡，沒用 process 版型。
"""
from __future__ import annotations

from app.schemas.studio import SlidesSpec


def _spec(*slides):
    return SlidesSpec(title="t", slides=[{"title": "封面", "layout_kind": "section_break", "bullets": ["x"]}, *slides])


def test_arrow_bullets_become_process_steps():
    from app.services.studio_layout import convert_arrow_bullets_to_process
    spec = _spec({"title": "申訴程序流程", "bullets": [
        "● 申訴程序流程",
        "◦ 提起申訴 → 申訴管轄機關處理 (30日內) → 若不服 → 於 20 日內向權保會提起再申訴",
    ]})
    out = convert_arrow_bullets_to_process(spec)
    s = out.slides[1]
    assert s.layout_kind == "process"
    assert [st.heading for st in s.steps] == ["提起申訴", "申訴管轄機關處理 (30日內)", "若不服", "於 20 日內向權保會提起再申訴"]
    # a slide with a single arrow inside prose is left alone
    keep = convert_arrow_bullets_to_process(_spec({"title": "說明", "bullets": ["輸入 → 輸出 的關係", "第二點", "第三點"]}))
    assert keep.slides[1].layout_kind == "standard"


def test_hollow_standard_slide_is_a_hard_violation_and_a_candidate():
    from app.services.studio_layout import _audit_layout_distribution, _select_rebalance_candidates, _should_rebalance
    spec = _spec(
        {"title": "懲戒判決確定後之行政作業", "bullets": ["懲戒判決確定後之行政流程"]},
        {"title": "正常頁", "layout_kind": "icon_rows", "bullets": ["a", "b", "c"],
         "icon_rows": [{"concept": "law", "heading": "h", "description": "d"}] * 3},
    )
    v = _audit_layout_distribution(spec, chunks_text="")
    hollow = [x for x in v if x.kind == "V5_HOLLOW"]
    assert hollow and hollow[0].severity == "hard" and hollow[0].slide_indices == [1]
    assert _should_rebalance(v)
    assert 1 in _select_rebalance_candidates(v)


def test_rebalance_prompt_lets_the_model_fill_hollow_slides():
    from app.services.studio_layout import LayoutViolation, _build_rebalance_prompt
    spec_dict = _spec({"title": "空心", "bullets": ["只有一條"]}).model_dump(mode="json")
    v = [LayoutViolation(kind="V5_HOLLOW", severity="hard", slide_indices=[1], detail="slide #1 只有 1 條 bullet")]
    system, user = _build_rebalance_prompt(spec_dict, v, "素材：申訴應於三十日內提出；管轄機關十日內處理。", [1])
    assert "V5_HOLLOW" in user
    assert "空心" in system and "可以補寫" in system
