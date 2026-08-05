"""In-process Triton/KServe gRPC client for CSP embedding models.

Vendored ``*_pb2*.py`` stubs were generated from Triton Inference Server
common protobufs (tag ``r25.04``, matching the measured 2.57.0 server) with
``grpcio-tools`` against protobuf 7.x — the same major the csp image
already resolves. Do not regenerate against a different protobuf major
without re-pinning ``services/csp/requirements.txt``.
"""

from app.services.triton_grpc.client import (
    TritonEmbedError,
    embed_texts,
    parse_grpc_endpoint,
    probe_triton_health,
    reset_channel_pool_for_tests,
)

__all__ = [
    "TritonEmbedError",
    "embed_texts",
    "parse_grpc_endpoint",
    "probe_triton_health",
    "reset_channel_pool_for_tests",
]
