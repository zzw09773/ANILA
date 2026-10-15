"""Cross-session live source changes must stop the real Quick runtime."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.agent import Agent, UserAgentPermission
from app.models.department import Department
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import (
    QuickAssistant,
    QuickCollectionPublication,
    QuickVersion,
)
from app.models.user import User, UserModelPermission
from app.services.collection_scope import (
    can_read_collection,
    select_visible_collections,
)
from app.services.quick_builder import collection_usable
from app.services.quick_execution import run
from app.services.quick_flow import validate_graph


@pytest.fixture
def db(tmp_path):
    # Unlike the shared StaticPool fixture, this file-backed database gives
    # the reader and writer independent physical connections and identity maps.
    engine = create_engine("sqlite:///" + str(tmp_path / "live-sources.db"))
    Base.metadata.create_all(engine)
    with Session(bind=engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def live_sources(db):
    unit, source_unit = (
        Department(name="live-reader-unit"),
        Department(name="live-source-unit"),
    )
    db.add_all([unit, source_unit])
    db.flush()
    reader = User(
        username="live-reader",
        role="user",
        department_id=unit.id,
        hashed_password="synthetic-not-a-login",
        is_active=True,
        is_approved=True,
    )
    source_owner = User(
        username="live-source-owner",
        role="user",
        department_id=source_unit.id,
        hashed_password="synthetic-not-a-login",
        is_active=True,
        is_approved=True,
    )
    model = ModelRegistry(
        name="live-model",
        display_name="合成模型",
        endpoint_url="http://model.invalid",
        model_type="llm",
        is_active=True,
        classification_ceiling="機密",
    )
    db.add_all([reader, source_owner, model])
    db.flush()
    coll = IngestionCollection(
        name="live-source",
        created_by=source_owner.id,
        department_id=source_unit.id,
        chunking_config={},
        embedding_model="synthetic",
        embedding_dim=3,
        classification_level="無機密",
        status="active",
    )
    db.add(coll)
    db.flush()
    publication = QuickCollectionPublication(
        collection_id=coll.id,
        scope="public",
        target_department_ids=[],
        approved_by=source_owner.id,
    )
    assistant = QuickAssistant(
        owner_user_id=reader.id,
        department_id=unit.id,
        draft_revision=1,
        published_revision=1,
    )
    agent = Agent(
        name="live-agent",
        endpoint_url="anila://quick-builder/live",
        owner_user_id=reader.id,
        owner_department_id=unit.id,
        base_model_id=model.id,
        approval_status="approved",
    )
    db.add_all([publication, assistant, agent])
    db.flush()
    db.add_all(
        [
            UserAgentPermission(user_id=reader.id, agent_id=agent.id),
            UserModelPermission(user_id=reader.id, model_id=model.id),
        ]
    )
    db.commit()
    # Keep strong references to the policy rows, as a real run/UI may do.
    return SimpleNamespace(
        reader=reader,
        coll=coll,
        publication=publication,
        model=model,
        assistant=assistant,
        agent=agent,
    )


def _change_from_another_session(db, rows, change):
    reader_connection = db.connection().connection.driver_connection
    with Session(bind=db.get_bind(), expire_on_commit=False) as writer:
        assert writer.connection().connection.driver_connection is not reader_connection
        if change == "publication":
            writer.get(QuickCollectionPublication, rows.coll.id).scope = "unit"
        elif change == "collection_classification":
            writer.get(IngestionCollection, rows.coll.id).classification_level = "密"
        elif change == "collection_disabled":
            writer.get(IngestionCollection, rows.coll.id).status = "disabled"
        elif change == "model_ceiling":
            writer.get(ModelRegistry, rows.model.id).classification_ceiling = "無機密"
        elif change == "model_disabled":
            writer.get(ModelRegistry, rows.model.id).is_active = False
        else:
            raise AssertionError(change)
        writer.commit()


@pytest.mark.parametrize(
    "change",
    [
        "publication",
        "collection_classification",
        "collection_disabled",
        "model_ceiling",
        "model_disabled",
    ],
)
@pytest.mark.parametrize("checkpoint", ["next_callback", "final_output"])
def test_live_source_change_in_await_blocks_next_callback_and_final_output(
    db, live_sources, monkeypatch, change, checkpoint
):
    from app.services import internal_llm

    rows = live_sources
    nodes = [{"id": "input", "kind": "input"}, {"id": "first", "kind": "model"}]
    if checkpoint == "next_callback":
        nodes.extend(
            [{"id": "next", "kind": "model"}, {"id": "document", "kind": "document"}]
        )
    nodes.append({"id": "output", "kind": "output"})
    graph = validate_graph(
        {
            "nodes": nodes,
            "edges": [
                {"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])
            ],
        }
    )
    payload = {
        "name": "live-flow",
        "model_id": rows.model.id,
        "classification_level": "營業秘密",
        "collection_ids": [rows.coll.id],
        "scope": "unit",
        "target_department_ids": [],
        "tool_fingerprints": {},
        "graph": graph,
    }
    version = QuickVersion(
        assistant_id=rows.assistant.id,
        revision=1,
        agent_id=rows.agent.id,
        payload=payload,
        payload_hash="a" * 64,
        created_by=rows.reader.id,
        status="published",
    )
    db.add(version)
    db.commit()
    calls, stale_snapshots = [], []

    async def awaiting_model(*args, **kwargs):
        calls.append("outbound-model")
        await asyncio.sleep(0)
        if len(calls) == 1:
            _change_from_another_session(db, rows, change)
            # An independent Session did commit, without magically updating the
            # reader's identity map; the production policy checks must refresh it.
            stale_snapshots.append(
                (
                    rows.publication.scope,
                    rows.coll.classification_level,
                    rows.coll.status,
                    rows.model.classification_ceiling,
                    rows.model.is_active,
                )
            )
        return "SYNTHETIC-SOURCE-CONTENT"

    monkeypatch.setattr(internal_llm, "complete_chat", awaiting_model)
    result = asyncio.run(
        run(db, rows.reader, rows.assistant.id, version.id, "合成來源問題")
    )
    assert stale_snapshots == [("public", "無機密", "active", "機密", True)]
    assert calls == ["outbound-model"], (
        "source changes must prevent the next outbound service call"
    )
    assert result["status"] == "failed"
    assert result["output"] == "" and result["artifacts"] == []


@pytest.mark.parametrize("reader", ["can_read", "collection_usable", "list_visible"])
def test_publication_acl_reads_refresh_existing_identity_map(db, live_sources, reader):
    rows = live_sources
    assert can_read_collection(db, rows.reader, rows.coll)
    _change_from_another_session(db, rows, "publication")
    assert rows.publication.scope == "public"
    if reader == "can_read":
        assert not can_read_collection(db, rows.reader, rows.coll)
    elif reader == "collection_usable":
        assert not collection_usable(db, rows.reader, rows.coll)
    else:
        visible, _, _ = select_visible_collections(db, rows.reader, [rows.coll])
        assert visible == []
