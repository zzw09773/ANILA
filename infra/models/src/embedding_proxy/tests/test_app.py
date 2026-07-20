from __future__ import annotations

import sys
import math
from types import ModuleType
from typing import Any

import numpy as np
import pytest
from fastapi import HTTPException

import app as proxy


@pytest.fixture(autouse=True)
def _default_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", None)

    async def run_without_a_worker_thread(func: Any, *args: Any) -> Any:
        # The production path is asserted to call asyncio.to_thread below.
        # Running the fake synchronously keeps this unit suite deterministic in
        # restricted sandboxes where worker-thread wakeups are unavailable.
        return func(*args)

    monkeypatch.setattr(proxy.asyncio, "to_thread", run_without_a_worker_thread)


def _install_fake_grpc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: Any = None,
    infer_error: Exception | None = None,
    server_ready: bool = True,
    model_ready: bool = True,
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    grpc_module = ModuleType("tritonclient.grpc")

    class FakeInferInput:
        def __init__(self, name: str, shape: list[int], datatype: str) -> None:
            state["input"] = self
            self.name = name
            self.shape = shape
            self.datatype = datatype
            self.data = None

        def set_data_from_numpy(self, data: Any) -> None:
            self.data = data

    class FakeInferRequestedOutput:
        def __init__(self, name: str) -> None:
            state["requested_output"] = name

    class FakeResult:
        def as_numpy(self, name: str) -> Any:
            assert name == "embeddings"
            return output

    class FakeClient:
        def __init__(self, *, url: str) -> None:
            state["url"] = url

        def infer(self, **kwargs: Any) -> FakeResult:
            state["infer_kwargs"] = kwargs
            if infer_error is not None:
                raise infer_error
            return FakeResult()

        def is_server_ready(self, **kwargs: Any) -> bool:
            state["server_ready_kwargs"] = kwargs
            return server_ready

        def is_model_ready(self, **kwargs: Any) -> bool:
            state["model_ready_kwargs"] = kwargs
            return model_ready

        def close(self) -> None:
            state["closed"] = True

    grpc_module.InferInput = FakeInferInput  # type: ignore[attr-defined]
    grpc_module.InferRequestedOutput = FakeInferRequestedOutput  # type: ignore[attr-defined]
    grpc_module.InferenceServerClient = FakeClient  # type: ignore[attr-defined]
    package = ModuleType("tritonclient")
    package.__path__ = []  # type: ignore[attr-defined]
    package.grpc = grpc_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tritonclient", package)
    monkeypatch.setitem(sys.modules, "tritonclient.grpc", grpc_module)
    return state


class _HTTPResponse:
    text = ""

    def __init__(self, body: dict[str, Any], *, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body


class _HTTPClient:
    def __init__(
        self,
        response: _HTTPResponse,
        *,
        get_responses: dict[str, _HTTPResponse] | None = None,
        **_: Any,
    ) -> None:
        self.response = response
        self.get_responses = get_responses or {}
        self.get_urls: list[str] = []
        self.post_args: tuple[str, dict[str, Any]] | None = None

    async def __aenter__(self) -> _HTTPClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any]) -> _HTTPResponse:
        self.post_args = (url, json)
        return self.response

    async def get(self, url: str) -> _HTTPResponse:
        self.get_urls.append(url)
        return self.get_responses.get(url, self.response)


@pytest.mark.asyncio
async def test_empty_input_stays_bad_request() -> None:
    with pytest.raises(HTTPException) as caught:
        await proxy.create_embeddings(proxy.EmbeddingRequest(input=[]))

    assert caught.value.status_code == 400
    assert caught.value.detail == "input must not be empty"


@pytest.mark.asyncio
async def test_default_http_transport_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _HTTPResponse(
        {
            "outputs": [
                {
                    "name": "embeddings",
                    "shape": [2, proxy.EMBEDDING_DIMENSION],
                    "data": [
                        float(index) for index in range(2 * proxy.EMBEDDING_DIMENSION)
                    ],
                }
            ]
        }
    )
    client = _HTTPClient(response)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(proxy, "TRITON_URL", "http://local-triton:8000/")

    result = await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one", "two"]))

    assert len(result["data"][0]["embedding"]) == proxy.EMBEDDING_DIMENSION
    assert result["data"][0]["embedding"][:2] == [0.0, 1.0]
    assert result["data"][1]["embedding"][:2] == [
        float(proxy.EMBEDDING_DIMENSION),
        float(proxy.EMBEDDING_DIMENSION + 1),
    ]
    assert client.post_args is not None
    assert client.post_args[0] == "http://local-triton:8000/v2/models/nv-embed-v2/infer"


@pytest.mark.asyncio
async def test_configured_grpc_transport_is_prioritized_and_offloads_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_grpc(
        monkeypatch,
        output=np.asarray(
            [
                [0.1] * proxy.EMBEDDING_DIMENSION,
                [0.2] * proxy.EMBEDDING_DIMENSION,
            ],
            dtype=np.float32,
        ),
    )
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")
    offload: dict[str, Any] = {}

    async def fake_to_thread(func: Any, *args: Any) -> Any:
        offload["called"] = True
        offload["function"] = func
        return func(*args)

    monkeypatch.setattr(proxy.asyncio, "to_thread", fake_to_thread)

    result = await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one", "two"]))

    assert result["data"][0]["embedding"] == pytest.approx(
        [0.1] * proxy.EMBEDDING_DIMENSION
    )
    assert result["data"][1]["embedding"] == pytest.approx(
        [0.2] * proxy.EMBEDDING_DIMENSION
    )
    assert state["url"] == "triton.example:9001"
    assert state["input"].name == "documents"
    assert state["input"].shape == [1, 2]
    assert state["input"].datatype == "BYTES"
    assert state["input"].data.tolist() == [["one", "two"]]
    assert state["requested_output"] == "embeddings"
    assert state["infer_kwargs"]["model_name"] == "nv-embed-v2"
    assert state["infer_kwargs"]["client_timeout"] == proxy.REQUEST_TIMEOUT
    assert state["closed"] is True
    assert offload == {"called": True, "function": proxy._infer_grpc}


@pytest.mark.asyncio
async def test_grpc_upstream_failure_is_bad_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_grpc(
        monkeypatch,
        output=np.asarray([[0.1] * proxy.EMBEDDING_DIMENSION], dtype=np.float32),
        infer_error=RuntimeError("connection refused"),
    )
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    with pytest.raises(HTTPException) as caught:
        await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one"]))

    assert caught.value.status_code == 502
    assert "gRPC request failed" in caught.value.detail
    assert "connection refused" in caught.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected_detail"),
    [
        (None, "missing 'embeddings' output"),
        (np.asarray([0.1, 0.2], dtype=np.float32), "unexpected output shape"),
    ],
)
async def test_grpc_contract_errors_are_internal_server_errors(
    monkeypatch: pytest.MonkeyPatch,
    output: Any,
    expected_detail: str,
) -> None:
    _install_fake_grpc(monkeypatch, output=output)
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    with pytest.raises(HTTPException) as caught:
        await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one"]))

    assert caught.value.status_code == 500
    assert expected_detail in caught.value.detail


@pytest.mark.asyncio
async def test_invalid_explicit_grpc_target_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "http://triton.example:9001/v1")
    http_called = False

    async def fail_http(*_: Any, **__: Any) -> list[list[Any]]:
        nonlocal http_called
        http_called = True
        return [[1.0]]

    monkeypatch.setattr(proxy, "_infer_http", fail_http)

    with pytest.raises(HTTPException) as caught:
        await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one"]))

    assert caught.value.status_code == 502
    assert "host:port without a scheme" in caught.value.detail
    assert http_called is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TRITON.Example.:9001", "triton.example:9001"),
        ("[2001:0db8:0:0:0:0:0:35]:9001", "[2001:db8::35]:9001"),
        ("127.0.0.1:9001", "127.0.0.1:9001"),
        ("127.provider.example:9001", "127.provider.example:9001"),
        ("model-127.vendor.example:9001", "model-127.vendor.example:9001"),
    ],
)
def test_grpc_target_is_canonicalised(raw: str, expected: str) -> None:
    assert proxy._validate_grpc_target(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://triton.example:9001",
        "user:pass@triton.example:9001",
        "triton.example:9001/path",
        "triton.example",
        "triton.example:",
        "[2001:db8::35]9001",
        "triton.example:abc",
    ],
)
def test_grpc_target_rejects_non_exact_host_port(raw: str) -> None:
    with pytest.raises(ValueError):
        proxy._validate_grpc_target(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "127.1:9001",
        "127.000.000.001:9001",
        "2130706433:9001",
        "0x7f000001:9001",
        "0177.0.0.1:9001",
        "127.0.1:9001",
        "127.1.1:9001",
        "0x7f.0.0.1:9001",
        "0X7F000001:9001",
        "1.2.3.4.:9001",
    ],
)
def test_grpc_target_rejects_ambiguous_numeric_ipv4_syntax(raw: str) -> None:
    with pytest.raises(ValueError, match="ambiguous numeric IPv4"):
        proxy._validate_grpc_target(raw)


@pytest.mark.asyncio
async def test_startup_rejects_invalid_explicit_grpc_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "http://triton.example:9001")
    with pytest.raises(RuntimeError, match="invalid TRITON_GRPC_URL"):
        await proxy.validate_startup_configuration()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        np.asarray([[math.nan] * proxy.EMBEDDING_DIMENSION], dtype=np.float32),
        np.asarray([[True] * proxy.EMBEDDING_DIMENSION], dtype=bool),
        np.asarray([["bad"] * proxy.EMBEDDING_DIMENSION], dtype=object),
    ],
)
async def test_grpc_output_rejects_non_finite_or_non_real_values(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    _install_fake_grpc(monkeypatch, output=output)
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    with pytest.raises(HTTPException) as caught:
        await proxy.create_embeddings(proxy.EmbeddingRequest(input=["one"]))

    assert caught.value.status_code == 500
    assert "embeddings' output contains" in caught.value.detail


@pytest.mark.asyncio
async def test_http_health_checks_triton_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _HTTPResponse({})
    client = _HTTPClient(response)
    client_options: dict[str, Any] = {}

    def make_client(**kwargs: Any) -> _HTTPClient:
        client_options.update(kwargs)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(proxy, "TRITON_URL", "http://local-triton:8000/")

    assert await proxy.health() == {"status": "ok"}
    assert client_options["timeout"] == proxy.READINESS_TIMEOUT
    assert client.get_urls == [
        "http://local-triton:8000/v2/health/ready",
        "http://local-triton:8000/v2/models/nv-embed-v2/ready",
    ]


@pytest.mark.asyncio
async def test_http_health_fails_when_model_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_url = "http://local-triton:8000/v2/models/nv-embed-v2/ready"
    client = _HTTPClient(
        _HTTPResponse({}),
        get_responses={model_url: _HTTPResponse({}, status_code=503)},
    )
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(proxy, "TRITON_URL", "http://local-triton:8000")

    with pytest.raises(HTTPException) as caught:
        await proxy.health()

    assert caught.value.status_code == 503
    assert "model 'nv-embed-v2' readiness returned status 503" in caught.value.detail


@pytest.mark.asyncio
async def test_grpc_health_fails_when_server_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_grpc(monkeypatch, server_ready=False)
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    with pytest.raises(HTTPException) as caught:
        await proxy.health()

    assert caught.value.status_code == 503
    assert "not ready" in caught.value.detail
    assert state["server_ready_kwargs"]["client_timeout"] == proxy.READINESS_TIMEOUT
    assert "model_ready_kwargs" not in state


@pytest.mark.asyncio
async def test_grpc_health_fails_when_model_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_grpc(monkeypatch, server_ready=True, model_ready=False)
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    with pytest.raises(HTTPException) as caught:
        await proxy.health()

    assert caught.value.status_code == 503
    assert "model 'nv-embed-v2' is not ready" in caught.value.detail
    assert state["model_ready_kwargs"] == {
        "model_name": "nv-embed-v2",
        "client_timeout": proxy.READINESS_TIMEOUT,
    }


@pytest.mark.asyncio
async def test_grpc_health_passes_when_server_and_model_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_grpc(monkeypatch, server_ready=True, model_ready=True)
    monkeypatch.setattr(proxy, "TRITON_GRPC_URL", "triton.example:9001")

    assert await proxy.health() == {"status": "ok"}
    assert state["server_ready_kwargs"]["client_timeout"] == proxy.READINESS_TIMEOUT
    assert state["model_ready_kwargs"]["model_name"] == "nv-embed-v2"
    assert state["model_ready_kwargs"]["client_timeout"] == proxy.READINESS_TIMEOUT
