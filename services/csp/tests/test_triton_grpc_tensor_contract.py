"""Triton gRPC wire contract: which tensor a role actually lands on.

這個檔案存在的理由,是 ``test_triton_grpc_embed.py`` 蓋不到的那一段。
那邊每一支測試都把 ``embed_texts`` 換成 fake,所以它證明的是
「``embedding_input_role`` 有傳到 ``embed_texts``」——**不是**「query 真的
送進 ``query`` 這條 tensor」。把 ``client.embed_texts`` 裡 query 分支改成
``input_name="documents"`` / ``shape=[1, 1]``(其餘識別字、簽章、呼叫點全
不動),對真 Triton 量到的 cosine(query, document) 會從 0.828 變成 1.000
——那正是 query/document 分流要防的靜默劣化——而整套測試不會有任何一支變紅。

因此這裡不 fake ``embed_texts``,只把 gRPC 通道與 stub 換掉,讓真正的
``_model_infer`` 去組出真的 ``ModelInferRequest``,再對「送上線的那顆
protobuf」斷言 tensor 名稱、shape 與 payload。
"""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from app.services.triton_grpc import client as triton_client
from app.services.triton_grpc import grpc_service_pb2

# 解碼器只有一份,從 test_triton_grpc_wire.py 借過來。
# 這裡本來自己抄了一份會 slice 的版本 —— 兩份各自寬鬆,於是長度前綴改成
# big-endian 時兩個檔案的 payload 斷言全都照樣過(對真 Triton 是每一次
# embedding 都 INVALID_ARGUMENT)。同一個不變式不留兩份實作,才不會只補到一邊。
from tests.test_triton_grpc_wire import decode_bytes_tensor


class _RecordingStub:
    """記下每一顆 ModelInferRequest,回傳一個形狀正確的合成 response。"""

    def __init__(self, channel):  # noqa: D107 - stub 建構子簽章要與真 stub 相同
        self.channel = channel

    requests: list = []
    dims = 4

    def ModelInfer(self, request, timeout=None):  # noqa: N802 - 對齊 gRPC 命名
        type(self).requests.append(request)
        rows, cols = 1, type(self).dims
        return grpc_service_pb2.ModelInferResponse(
            model_name=request.model_name,
            outputs=[
                grpc_service_pb2.ModelInferResponse.InferOutputTensor(
                    name="embeddings", datatype="FP32", shape=[rows, cols],
                )
            ],
            raw_output_contents=[struct.pack(f"<{rows * cols}f", *([0.5] * rows * cols))],
        )


@pytest.fixture
def captured(monkeypatch):
    """真 embed_texts / 真 _model_infer / 真 protobuf,只換掉通道與 stub。"""
    _RecordingStub.requests = []
    monkeypatch.setattr(
        triton_client, "_get_channel", lambda host, port, secure: object()
    )
    # 簽章必須與 client._wait_ready 一致(channel, budget=None)。只寫
    # (channel) 會在生產端加參數時整批 TypeError —— 這正是本輪要修的
    # 「過期測試替身」缺陷本身,別在修它的同一個檔案裡再犯一次。
    monkeypatch.setattr(
        triton_client, "_wait_ready", lambda channel, budget=None: None
    )
    monkeypatch.setattr(
        triton_client.grpc_service_pb2_grpc,
        "GRPCInferenceServiceStub",
        _RecordingStub,
    )
    return _RecordingStub.requests


# ── 單邊契約 ────────────────────────────────────────────────────────────────


def test_query_role_lands_on_the_query_tensor(captured):
    triton_client.embed_texts(
        "grpc://triton.example.org:9001",
        "nv-embed-v2",
        ["法規查詢"],
        role="query",
    )
    assert len(captured) == 1
    tensor = captured[0].inputs[0]
    assert tensor.name == "query"
    assert list(tensor.shape) == [1]
    assert tensor.datatype == "BYTES"
    assert decode_bytes_tensor(captured[0].raw_input_contents[0]) == ["法規查詢"]


def test_document_role_lands_on_the_documents_tensor(captured):
    triton_client.embed_texts(
        "grpc://triton.example.org:9001",
        "nv-embed-v2",
        ["受檢索的段落"],
        role="document",
    )
    assert len(captured) == 1
    tensor = captured[0].inputs[0]
    assert tensor.name == "documents"
    assert list(tensor.shape) == [1, 1]
    assert tensor.datatype == "BYTES"
    assert decode_bytes_tensor(captured[0].raw_input_contents[0]) == ["受檢索的段落"]


# ── 差分契約:兩個 role 必須落在不同 tensor ─────────────────────────────────


def test_query_and_document_must_not_collapse_onto_the_same_tensor(captured):
    """分流的不變式:同一段文字,兩個 role 送出的 tensor 不得相同。

    這條就是活體 cosine(query, document) → 1.0 的測試面代理。任何讓兩條
    路徑塌縮成同一顆 tensor 的改動(不論是把 query 改送 ``documents``、
    把 document 改送 ``query``、還是把 if/else 兩邊寫成一樣)都會在這裡變紅。
    """
    same_text = "同一段文字"
    triton_client.embed_texts(
        "grpc://triton.example.org:9001", "nv-embed-v2", [same_text], role="query",
    )
    triton_client.embed_texts(
        "grpc://triton.example.org:9001", "nv-embed-v2", [same_text], role="document",
    )
    assert len(captured) == 2
    q_tensor, d_tensor = captured[0].inputs[0], captured[1].inputs[0]

    assert q_tensor.name != d_tensor.name, (
        "query 與 document 送進了同一顆 input tensor —— 對真 Triton 這代表 "
        "cosine(query, document) 會塌到 1.0"
    )
    assert list(q_tensor.shape) != list(d_tensor.shape), (
        "query 與 document 的 shape 相同 —— rank 沒有分流"
    )
    # payload 相同才代表差異純粹來自 role,而不是測試自己送了不同文字。
    assert (
        decode_bytes_tensor(captured[0].raw_input_contents[0])
        == decode_bytes_tensor(captured[1].raw_input_contents[0])
        == [same_text]
    )


def test_role_is_the_only_thing_that_moves_the_tensor(captured):
    """同一個 role 重複呼叫必須落在同一顆 tensor(沒有隱藏狀態)。"""
    for text in ("一", "二", "三"):
        triton_client.embed_texts(
            "grpc://triton.example.org:9001", "nv-embed-v2", [text], role="query",
        )
    assert {t.inputs[0].name for c in captured for t in [c]} == {"query"}
    assert {tuple(c.inputs[0].shape) for c in captured} == {(1,)}


def test_document_batch_issues_one_request_per_text_on_the_documents_tensor(captured):
    triton_client.embed_texts(
        "grpc://triton.example.org:9001",
        "nv-embed-v2",
        ["甲", "乙"],
        role="document",
    )
    assert len(captured) == 2
    assert [c.inputs[0].name for c in captured] == ["documents", "documents"]
    assert [decode_bytes_tensor(c.raw_input_contents[0])[0] for c in captured] == [
        "甲",
        "乙",
    ]


def test_requested_output_is_the_embeddings_tensor(captured):
    triton_client.embed_texts(
        "grpc://triton.example.org:9001", "nv-embed-v2", ["x"], role="query",
    )
    assert [o.name for o in captured[0].outputs] == ["embeddings"]
    assert captured[0].model_name == "nv-embed-v2"


# ── 端到端:proxy 的 embedding_input_role → 線上的 tensor ────────────────────


def _triton_model(**overrides):
    base = dict(
        id=42,
        name="nv-embed-v2",
        model_type="embedding",
        endpoint_url="grpc://triton.example.org:9001",
        api_version="v1",
        protocol="triton_grpc",
        api_key_secret_ref=None,
        display_name="nv-embed-v2",
        is_internal=True,
        classification_ceiling=None,
        is_active=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def proxy_env(monkeypatch):
    from app.services.proxy import service as proxy_impl

    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "triton.example.org")

    async def _noop_usage(*_a, **_k):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", _noop_usage)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _noop_usage)
    monkeypatch.setattr(proxy_impl, "_note_proxy_outcome", lambda **_k: None)
    return proxy_impl


@pytest.mark.parametrize(
    "role,expected_name,expected_shape",
    [("query", "query", [1]), ("document", "documents", [1, 1])],
)
def test_proxy_role_reaches_the_wire_tensor(
    captured, proxy_env, role, expected_name, expected_shape,
):
    """整條鏈:``embedding_input_role`` → ``embed_texts`` → ModelInferRequest。

    這一支不 fake ``embed_texts``,所以鏈上任何一段把 role 弄丟或接錯
    tensor 都會變紅,而不是只有「參數有傳下去」。
    """
    asyncio.run(
        proxy_env.proxy_request(
            model=_triton_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": "檢索用文字"},
            endpoint_path="/v1/embeddings",
            embedding_input_role=role,
        )
    )
    assert len(captured) == 1
    assert captured[0].inputs[0].name == expected_name
    assert list(captured[0].inputs[0].shape) == expected_shape


def test_proxy_public_surface_defaults_to_the_documents_tensor(captured, proxy_env):
    """公開 /v1/embeddings 不帶 role → 必須落在 documents tensor。"""
    asyncio.run(
        proxy_env.proxy_request(
            model=_triton_model(),
            api_key_id=None,
            user_id=1,
            department_id=None,
            request_body={"model": "nv-embed-v2", "input": ["段落"]},
            endpoint_path="/v1/embeddings",
        )
    )
    assert captured[0].inputs[0].name == "documents"
    assert list(captured[0].inputs[0].shape) == [1, 1]
