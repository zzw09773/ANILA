"""升密的每一個機制各自釘一根釘子（mutation-killing）。

為什麼要獨立一個檔：``test_collection_classification_raise.py`` 走的是
「呼叫升密路由 → 讀文件密等」這條端到端路徑，而級聯**會把文件欄位寫成
新等級**——所以那條測試在下列任何一個機制被拿掉時仍然是綠的：

* 讀模型 ``effective_document_classification_level`` 的 ``max_of``
* 單檔上傳繼承知識庫密等
* zip 上傳繼承知識庫密等
* ``_document_response`` 用有效密等覆寫 ORM 欄位

也就是說它在驗證自己寫進去的值。本檔改成一個機制一支測試，且每支都刻意
**繞過**其他機制建立前提（直接寫 DB 列、只建單一種綁定），把三條路徑分開：

不變式 (a)：文件的有效密等永遠 ≥ 所屬知識庫。三個機制共同支撐它——
上傳時繼承（新文件）、升密時級聯（既有文件）、讀取時取 max（漏網的舊列）。
拿掉任何一個都必須有測試變紅。

不變式 (b)：知識庫升密時，任何**已綁定**且有效密等較低的 agent 都要擋下
並點名。綁定有兩個機制（junction 表 ``agent_collection_bindings`` 與舊
鏡像欄位 ``agents.bound_collection_id``），``PUT /api/agents/{id}`` 會同時
寫兩邊，所以端到端測試分不出來——這裡各建一種。
"""

from __future__ import annotations

import io
import os
import zipfile

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.api.ingestion import documents as documents_api
from app.models.agent import Agent, AgentCollectionBinding
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.schemas.contracts.classification import ClassificationLevel
from app.services.ingestion_classification import (
    agents_bound_below_level,
    effective_document_classification_level,
)
from tests.conftest import login, make_user


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_collection(db, owner, *, name: str, level: str) -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        embedding_model="test-embed",
        embedding_dim=8,
        chunking_config={"strategy": "fixed", "params": {"size": 256}},
        classification_level=level,
        created_by=owner.id,
        origin="csp",
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _insert_document(db, coll, *, filename: str, level: str, sha: str):
    """直接寫 DB 列——刻意繞過上傳端點的繼承，模擬「升密前就存在的舊文件」。"""
    doc = IngestionDocument(
        collection_id=coll.id,
        filename=filename,
        sha256=sha,
        status="pending",
        classification_level=level,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture
def upload_env(monkeypatch, tmp_path):
    """讓上傳端點在沒有 redis / 沒有 /var 寫入權的測試環境下可跑。"""
    monkeypatch.setattr(documents_api, "_UPLOAD_DIR", str(tmp_path))

    async def _fake_enqueue(document_id: int) -> str:
        return f"fake-job-{document_id}"

    monkeypatch.setattr(documents_api, "enqueue_ingest_document", _fake_enqueue)
    return tmp_path


# ── 不變式 (a) 機制 1：讀模型取 max ────────────────────────────────────────
class TestReadModelTakesMax:
    def test_effective_level_is_max_of_document_and_collection(self, db):
        """M1 釘子：``effective_document_classification_level`` 丟掉 ``max_of``
        （改回傳文件欄位）必須讓這支變紅。

        刻意不經過升密路由——文件欄位停在「無機密」，知識庫是「機密」。
        """
        owner = make_user(db, username="readmodel_owner", role="developer")
        coll = _make_collection(db, owner, name="readmodel-kb", level="機密")
        doc = _insert_document(
            db, coll, filename="legacy.txt", level="無機密", sha="a" * 64
        )

        assert doc.classification_level == "無機密"  # 前提：欄位真的比較低
        assert (
            effective_document_classification_level(doc, coll).to_storage()
            == "機密"
        )

    def test_effective_level_keeps_the_higher_document_value(self, db):
        """反向：文件比知識庫高時，不得被知識庫拉低（max 不是 override）。"""
        owner = make_user(db, username="readmodel_owner2", role="developer")
        coll = _make_collection(db, owner, name="readmodel-kb-low", level="無機密")
        doc = _insert_document(
            db, coll, filename="hot.txt", level="機密", sha="b" * 64
        )
        assert (
            effective_document_classification_level(doc, coll).to_storage()
            == "機密"
        )


# ── 不變式 (a) 機制 2：API 投影用有效密等 ─────────────────────────────────
class TestResponseProjectionOverridesColumn:
    def test_list_and_detail_report_effective_level_for_legacy_row(
        self, client, db
    ):
        """M7 釘子：``_document_response`` 改成直接投影 ORM 欄位必須變紅。

        前提同樣繞過級聯：文件列是直接寫進去的「無機密」，庫是「機密」。
        """
        user = make_user(db, username="proj_eff", role="developer")
        token = login(client, "proj_eff")
        h = _auth(token)

        coll = _make_collection(db, user, name="projection-kb", level="機密")
        doc = _insert_document(
            db, coll, filename="legacy.txt", level="無機密", sha="c" * 64
        )

        listed = client.get(
            f"/api/ingestion/collections/{coll.id}/documents", headers=h
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["classification_level"] == "機密"

        detail = client.get(f"/api/ingestion/documents/{doc.id}", headers=h)
        assert detail.status_code == 200, detail.text
        assert detail.json()["classification_level"] == "機密"

        # 欄位本身沒被讀取路徑偷偷改掉——這是讀模型，不是寫入。
        db.refresh(doc)
        assert doc.classification_level == "無機密"


# ── 不變式 (a) 機制 3：上傳時繼承 ─────────────────────────────────────────
class TestUploadInheritsCollectionLevel:
    def test_single_upload_writes_the_collection_level_into_the_column(
        self, client, db, upload_env
    ):
        """M5 釘子：單檔上傳不繼承知識庫密等必須變紅。

        斷言看的是 **DB 欄位**，不是回應——回應會被讀模型的 max 蓋過去，
        用回應斷言的話這個機制拿掉了也還是綠的。
        """
        user = make_user(db, username="upload_inherit", role="developer")
        token = login(client, "upload_inherit")
        h = _auth(token)
        coll = _make_collection(db, user, name="inherit-kb", level="機密")

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/documents",
            headers=h,
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
        assert resp.status_code == 202, resp.text
        doc_id = resp.json()["id"]

        row = db.get(IngestionDocument, doc_id)
        db.refresh(row)
        assert row.classification_level == "機密", (
            "上傳未繼承知識庫密等：文件列停在 "
            f"{row.classification_level!r}"
        )

    def test_zip_upload_writes_the_collection_level_into_every_column(
        self, client, db, upload_env
    ):
        """M6 釘子：zip 上傳不繼承知識庫密等必須變紅（同樣斷言 DB 欄位）。"""
        user = make_user(db, username="zip_inherit", role="developer")
        token = login(client, "zip_inherit")
        h = _auth(token)
        coll = _make_collection(db, user, name="zip-inherit-kb", level="密")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.txt", "alpha")
            zf.writestr("b.txt", "bravo")
        buf.seek(0)

        resp = client.post(
            f"/api/ingestion/collections/{coll.id}/documents/zip",
            headers=h,
            files={"file": ("bundle.zip", buf.read(), "application/zip")},
        )
        assert resp.status_code == 202, resp.text

        rows = (
            db.query(IngestionDocument)
            .filter(IngestionDocument.collection_id == coll.id)
            .all()
        )
        assert len(rows) == 2, f"expected 2 documents, got {len(rows)}"
        for row in rows:
            db.refresh(row)
            assert row.classification_level == "密", (
                f"zip 上傳未繼承知識庫密等：{row.filename} 停在 "
                f"{row.classification_level!r}"
            )


# ── 不變式 (b)：兩種綁定機制各自擋下 ──────────────────────────────────────
class TestStrandedBindBothMechanisms:
    def _low_agent(self, db, owner, name: str) -> Agent:
        agent = Agent(
            name=name,
            owner_user_id=owner.id,
            endpoint_url="http://agent:9100",
            description_for_router="bind mechanism probe",
            approval_status="registered",
            default_classification_level="無機密",
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return agent

    def test_legacy_mirror_column_alone_blocks_the_raise(self, client, db):
        """M2 釘子：綁定檢查忽略 ``agents.bound_collection_id`` 必須變紅。

        只寫舊鏡像欄位、**不建** junction 列——``PUT /api/agents/{id}``
        兩邊都會寫，所以端到端測試分不出這一條。
        """
        user = make_user(db, username="bind_legacy", role="developer")
        token = login(client, "bind_legacy")
        h = _auth(token)
        coll = _make_collection(db, user, name="legacy-bind-kb", level="無機密")
        agent = self._low_agent(db, user, "legacy-only-agent")
        agent.bound_collection_id = coll.id
        db.commit()

        assert (
            db.query(AgentCollectionBinding)
            .filter(AgentCollectionBinding.agent_id == agent.id)
            .count()
            == 0
        ), "前提壞了：這支測試要的是「只有舊欄位」"

        under = agents_bound_below_level(
            db, coll.id, ClassificationLevel.SECRET
        )
        assert [a.id for a in under] == [agent.id]

        refused = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert refused.status_code == 409, refused.text
        assert "legacy-only-agent" in refused.json()["detail"]

        db.refresh(coll)
        assert coll.classification_level == "無機密"
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"

    def test_junction_row_alone_blocks_the_raise(self, client, db):
        """M3 釘子：綁定檢查忽略 junction 表必須變紅。

        只建 ``agent_collection_bindings`` 列、``bound_collection_id`` 留空。
        """
        user = make_user(db, username="bind_junction", role="developer")
        token = login(client, "bind_junction")
        h = _auth(token)
        coll = _make_collection(db, user, name="junction-bind-kb", level="無機密")
        agent = self._low_agent(db, user, "junction-only-agent")
        db.add(AgentCollectionBinding(agent_id=agent.id, collection_id=coll.id))
        db.commit()

        db.refresh(agent)
        assert agent.bound_collection_id is None, (
            "前提壞了：這支測試要的是「只有 junction 列」"
        )

        under = agents_bound_below_level(
            db, coll.id, ClassificationLevel.SECRET
        )
        assert [a.id for a in under] == [agent.id]

        refused = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "機密"},
        )
        assert refused.status_code == 409, refused.text
        assert "junction-only-agent" in refused.json()["detail"]

        db.refresh(coll)
        assert coll.classification_level == "無機密"

    def test_requires_encryption_floor_is_respected(self, client, db):
        """舊 boolean ``requires_encryption`` 把 agent 的有效密等墊到「密」，
        升到「密」時就不該再被擋（有效密等不低於目標）。"""
        user = make_user(db, username="bind_floor", role="developer")
        token = login(client, "bind_floor")
        h = _auth(token)
        coll = _make_collection(db, user, name="floor-bind-kb", level="無機密")
        agent = self._low_agent(db, user, "floored-agent")
        agent.requires_encryption = True
        db.add(AgentCollectionBinding(agent_id=agent.id, collection_id=coll.id))
        db.commit()

        assert agents_bound_below_level(
            db, coll.id, ClassificationLevel.RESTRICTED
        ) == []

        raised = client.post(
            f"/api/ingestion/collections/{coll.id}/classification",
            headers=h,
            json={"classification_level": "密"},
        )
        assert raised.status_code == 200, raised.text
        assert raised.json()["classification_level"] == "密"
