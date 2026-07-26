# -*- coding: utf-8 -*-
"""ISO 42001 追溯欄從「建了但沒人接」變成「讀得到」—— W3-12l。

`0035_iso_42001_traceability` 建了 8 個欄:

    agents.{last_reviewer_id, vv_status, aiia_doc_path}
    model_registry.{model_card_url, training_dataset_ref, weights_sha256,
                    intended_use, limitations}

而 **ORM / API / UI 全無** —— 那支 migration 的意圖從來沒有被實現,而且沒有任何
東西提醒過任何人。它們是本輪新增的 `UNDECLARED_COLUMN` drift 檢查找出來的
(先前只有帶 FK 的那些會碰巧透過 FK 檢查現形)。

為什麼「沒有映射」比「欄位是空的」嚴重:治理稽核問「這個模型的 model card 在哪」
時,平台連「欄位是空的」都答不出來,只能答「我沒有這個概念」。前者是待補的資料,
後者是待補的**功能**。

本包唯讀先行:讀得到、drift 不再把它們列為未宣告;寫入面與 UI 表單另排。
"""
from __future__ import annotations

from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from tests.conftest import login, make_agent, make_model, make_user


ISO_MODEL_FIELDS = (
    "model_card_url",
    "training_dataset_ref",
    "weights_sha256",
    "intended_use",
    "limitations",
)
ISO_AGENT_FIELDS = ("last_reviewer_id", "vv_status", "aiia_doc_path")


# ── ORM 映射存在 ──────────────────────────────────────────────────────────────

def test_agent_model_declares_the_three_traceability_columns():
    for field in ISO_AGENT_FIELDS:
        assert hasattr(Agent, field), f"Agent 沒有映射 {field}"


def test_model_registry_declares_the_five_traceability_columns():
    for field in ISO_MODEL_FIELDS:
        assert hasattr(ModelRegistry, field), f"ModelRegistry 沒有映射 {field}"


def test_vv_status_defaults_to_pending(db):
    """`vv_status` 是 NOT NULL 且 server_default 'pending' —— 映射不能讓它變成必填。"""
    user = make_user(db, username="alice")
    agent = make_agent(db, user, name="probe-agent")
    db.refresh(agent)
    assert agent.vv_status == "pending", (
        f"vv_status 應由 server_default 補成 pending,實際是 {agent.vv_status!r}"
    )


def test_traceability_values_round_trip(db):
    user = make_user(db, username="alice")
    agent = make_agent(db, user, name="traced-agent")
    agent.vv_status = "passed"
    agent.aiia_doc_path = "docs/governance/aiia/traced-agent.md"
    agent.last_reviewer_id = user.id
    model = make_model(db, name="traced-model")
    model.model_card_url = "docs/governance/model-cards/traced-model.md"
    model.weights_sha256 = "a" * 64
    model.intended_use = "內部文件問答"
    model.limitations = "不得用於法律意見"
    model.training_dataset_ref = "internal-corpus-2026Q2"
    db.commit()

    db.refresh(agent)
    db.refresh(model)
    assert agent.vv_status == "passed"
    assert agent.aiia_doc_path.endswith("traced-agent.md")
    assert agent.last_reviewer_id == user.id
    assert model.weights_sha256 == "a" * 64
    assert model.intended_use == "內部文件問答"


# ── API 讀得到 ────────────────────────────────────────────────────────────────

def test_model_api_exposes_traceability_fields(client, db):
    admin = make_user(db, username="root", role="owner")
    model = make_model(db, name="exposed-model")
    model.model_card_url = "docs/governance/model-cards/exposed-model.md"
    model.weights_sha256 = "b" * 64
    model.intended_use = "摘要"
    model.limitations = "不保證事實正確"
    model.training_dataset_ref = "corpus-x"
    db.commit()

    token = login(client, username="root")
    resp = client.get(
        "/api/models", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json() if r["name"] == "exposed-model")
    for field in ISO_MODEL_FIELDS:
        assert field in row, f"API 回應沒有 {field} —— 沒有露出的欄等於不存在"
    assert row["weights_sha256"] == "b" * 64
    assert row["intended_use"] == "摘要"
    assert admin.id  # 用到變數,避免 lint 抱怨


def test_unset_traceability_fields_are_null_not_missing(client, db):
    """沒填的欄要回 `null` 而不是整個 key 消失。

    差別很實際:key 在 → 前端知道「這個平台有這個概念,只是沒填」;key 不在 →
    前端無法區分「沒填」與「舊版後端」。
    """
    make_user(db, username="root", role="owner")
    make_model(db, name="bare-model")
    token = login(client, username="root")

    row = next(
        r
        for r in client.get(
            "/api/models", headers={"Authorization": f"Bearer {token}"}
        ).json()
        if r["name"] == "bare-model"
    )
    for field in ISO_MODEL_FIELDS:
        assert field in row
        assert row[field] is None
