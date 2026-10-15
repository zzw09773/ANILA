"""Bounded real-loopback HTTP Quick load; run in /tmp with python3 -I -S.

No URL, port, DSN or credential input is accepted. One restricted worker owns
both a 127.0.0.1:0 Uvicorn socket and its bounded stdlib HTTP clients.
"""

# ruff: noqa: E402 -- install the audit hook before all non-bootstrap imports.
import os
import sys

_SPAWN_COMMAND = None
_SPAWNED = False
_LISTENER = None
_BOUND = False
_ENDPOINT = None
_DATABASE = None
_DENIALS = {}


def audit_guard(event, args):
    global _SPAWNED, _BOUND
    reason = None
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if (
            "secrets" in parts
            or parts[-1].startswith(".env")
            or parts[-1].endswith((".pem", ".key"))
        ):
            reason = "protected_file"
    elif event == "sqlite3.connect":
        if _DATABASE is None or str(args[0]) != _DATABASE:
            reason = "database"
    elif event in {"psycopg.connect", "psycopg2.connect"}:
        reason = "database"
    elif event == "socket.bind":
        if (
            _LISTENER is None
            or _BOUND
            or args[0] is not _LISTENER
            or args[1] != ("127.0.0.1", 0)
        ):
            reason = "network"
        else:
            _BOUND = True
    elif event == "socket.connect":
        if _ENDPOINT is None or args[1] != _ENDPOINT:
            reason = "network"
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        reason = "network"
    elif event == "socket.__new__":
        # AF_UNIX is asyncio's unconnected self-pipe. Only IPv4 TCP is used
        # for the ephemeral listener/client; bind and connect are checked above.
        if len(args) < 3 or args[1] not in (1, 2) or (args[1] == 2 and args[2] != 1):
            reason = "network"
    elif event == "subprocess.Popen":
        command = (
            tuple(args[1])
            if len(args) > 1 and isinstance(args[1], (list, tuple))
            else None
        )
        if (
            _SPAWN_COMMAND is None
            or _SPAWNED
            or command != _SPAWN_COMMAND
            or "--worker" in sys.argv
        ):
            reason = "subprocess"
        else:
            _SPAWNED = True
    elif event in {"os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
        reason = "subprocess"
    if reason:
        _DENIALS[reason] = _DENIALS.get(reason, 0) + 1
        raise PermissionError("Quick HTTP sandbox blocked " + reason)


if __name__ == "__main__":
    sys.addaudithook(audit_guard)

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import platform
import resource
import secrets
import socket
import statistics
import subprocess
import sysconfig
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quick_builder_load as base

ROOT = base.ROOT
MAX_RESPONSE_BYTES = 256 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_REQUEST_BYTES = 64 * 1024
ANSWER = "合成本機HTTP回答。" * 32


class LoadError(RuntimeError):
    """Only stable codes are ever included in reports."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def failure_details(exc):
    detail = {
        "error_type": type(exc).__name__,
        "code": exc.code if isinstance(exc, LoadError) else type(exc).__name__,
    }
    trace = exc.__traceback__
    while trace and trace.tb_next:
        trace = trace.tb_next
    if trace:
        detail["location"] = {
            "file": Path(trace.tb_frame.f_code.co_filename).name,
            "line": trace.tb_lineno,
        }
    if (
        isinstance(exc, ModuleNotFoundError)
        and isinstance(exc.name, str)
        and all(char.isalnum() or char in "_." for char in exc.name)
    ):
        detail["missing_module"] = exc.name[:120]
    return detail


def stack_metadata():
    """Bounded live locations only: never locals, source lines or exceptions."""
    snapshots = []
    frames = sys._current_frames()
    main_id = threading.main_thread().ident
    ordered_ids = ([main_id] if main_id in frames else []) + [
        thread_id for thread_id in frames if thread_id != main_id
    ]
    for thread_id in ordered_ids[:48]:
        frame = frames[thread_id]
        locations = []
        while frame is not None and len(locations) < 32:
            locations.append(
                {
                    "file": Path(frame.f_code.co_filename).name,
                    "function": frame.f_code.co_name,
                    "line": frame.f_lineno,
                }
            )
            frame = frame.f_back
        snapshots.append(
            {
                "main_thread": thread_id == threading.main_thread().ident,
                "locations": locations,
            }
        )
    return snapshots


def validate_config(
    profile,
    assistants,
    concurrency,
    delay_ms,
    wall_seconds,
    cpu_seconds,
    inject_failure=False,
):
    config = base.validate_config(
        profile, assistants, concurrency, delay_ms, wall_seconds, cpu_seconds
    )
    if (
        config["concurrency"] < 2
        or type(inject_failure) is not bool
        or (inject_failure and profile != "smoke")
    ):
        raise ValueError("HTTP requires at least two users; faults are smoke-only")
    config["inject_failure"] = inject_failure
    return config


async def response_chunks(reader, headers):
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise LoadError("HTTP_COMPRESSION_FORBIDDEN")
    if "transfer-encoding" in headers:
        if (
            headers["transfer-encoding"].lower() != "chunked"
            or "content-length" in headers
        ):
            raise LoadError("HTTP_FRAMING_INVALID")
        total = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            if len(line) > 128:
                raise LoadError("HTTP_CHUNK_HEADER_LIMIT")
            try:
                size = int(line.strip(), 16)
            except ValueError:
                raise LoadError("HTTP_CHUNK_INVALID") from None
            if size < 0 or size > MAX_RESPONSE_BYTES - total:
                raise LoadError("HTTP_RESPONSE_LIMIT")
            if not size:
                if await reader.readexactly(2) != b"\r\n":
                    raise LoadError("HTTP_TRAILERS_FORBIDDEN")
                return
            chunk = await reader.readexactly(size)
            if await reader.readexactly(2) != b"\r\n":
                raise LoadError("HTTP_CHUNK_INVALID")
            total += size
            yield chunk
    else:
        try:
            remaining = int(headers["content-length"])
        except (KeyError, ValueError):
            raise LoadError("HTTP_LENGTH_REQUIRED") from None
        if not 0 <= remaining <= MAX_RESPONSE_BYTES:
            raise LoadError("HTTP_RESPONSE_LIMIT")
        while remaining:
            chunk = await reader.readexactly(min(8192, remaining))
            remaining -= len(chunk)
            yield chunk


async def http_request(
    method, path, *, token=None, body=None, extra_headers=None, on_chunk=None
):
    if (
        _ENDPOINT is None
        or method not in {"GET", "POST", "PUT"}
        or not path.startswith(("/api/", "/v1/"))
        or any(char in path for char in "\r\n ")
    ):
        raise LoadError("HTTP_TARGET_INVALID")
    encoded = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise LoadError("HTTP_REQUEST_LIMIT")
    reader, writer = await asyncio.open_connection(*_ENDPOINT, limit=MAX_HEADER_BYTES)
    try:
        headers = {
            "Host": f"127.0.0.1:{_ENDPOINT[1]}",
            "Connection": "close",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra_headers or {})
        if any("\r" in str(value) or "\n" in str(value) for value in headers.values()):
            raise LoadError("HTTP_HEADER_INVALID")
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            + "\r\n"
        )
        writer.write(head.encode("ascii") + encoded)
        await writer.drain()
        raw_headers = await reader.readuntil(b"\r\n\r\n")
        if len(raw_headers) > MAX_HEADER_BYTES:
            raise LoadError("HTTP_HEADER_LIMIT")
        lines = raw_headers.decode("latin1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        if 300 <= status < 400:
            raise LoadError("HTTP_REDIRECT_FORBIDDEN")
        received = {}
        for line in lines[1:]:
            if not line:
                continue
            key, value = line.split(":", 1)
            name = key.lower()
            if name in received and name in {
                "content-length",
                "transfer-encoding",
                "content-encoding",
            }:
                raise LoadError("HTTP_FRAMING_INVALID")
            received[name] = value.strip()
        chunks = []
        async for chunk in response_chunks(reader, received):
            if on_chunk:
                await on_chunk(chunk)
            chunks.append(chunk)
        return status, b"".join(chunks), received.get("content-type", "")
    finally:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def json_request(method, path, *, expected=200, **kwargs):
    status, content, _ = await asyncio.wait_for(
        http_request(method, path, **kwargs), timeout=20
    )
    if status != expected:
        raise LoadError("HTTP_STATUS_" + str(status))
    try:
        return json.loads(content)
    except ValueError:
        raise LoadError("HTTP_JSON_INVALID") from None


def seed(db, config):
    from app.models.agent import Agent, UserAgentPermission
    from app.models.department import Department
    from app.models.model_registry import ModelRegistry
    from app.models.quick_builder import QuickAssistant, QuickVersion
    from app.models.user import User, UserModelPermission
    from app.services.quick_flow import validate_graph
    from app.utils.security import hash_password

    unit = Department(name="Synthetic HTTP unit")
    db.add(unit)
    db.flush()
    owner = User(
        username="http-synthetic-owner",
        role="developer",
        department_id=unit.id,
        is_active=True,
        is_approved=True,
        local_password_disabled=True,
        hashed_password="NOT_A_LOGIN_HASH",
    )
    model = ModelRegistry(
        name="http-synthetic-model",
        display_name="合成HTTP模型",
        model_type="llm",
        endpoint_url="anila://synthetic-http-model",
        is_active=True,
        supports_streaming=True,
        classification_ceiling="機密",
    )
    db.add_all([owner, model])
    db.flush()
    users = []
    for index in range(config["concurrency"]):
        password = secrets.token_urlsafe(30)
        user = User(
            username=f"http-synthetic-user-{index}",
            role="user",
            department_id=unit.id,
            is_active=True,
            is_approved=True,
            hashed_password=hash_password(password),
        )
        db.add(user)
        db.flush()
        db.add(UserModelPermission(user_id=user.id, model_id=model.id))
        users.append({"id": user.id, "username": user.username, "password": password})
    graph = validate_graph(
        {
            "nodes": [
                {"id": "i", "kind": "input"},
                {"id": "m", "kind": "model"},
                {"id": "o", "kind": "output"},
            ],
            "edges": [{"source": "i", "target": "m"}, {"source": "m", "target": "o"}],
        }
    )
    assistants = []
    for index in range(config["assistants"]):
        assistant = QuickAssistant(
            owner_user_id=owner.id,
            department_id=unit.id,
            draft_revision=1,
            published_revision=1,
        )
        agent = Agent(
            name=f"http-synthetic-agent-{index}",
            owner_user_id=owner.id,
            owner_department_id=unit.id,
            base_model_id=model.id,
            endpoint_url=f"anila://quick-builder/http-synthetic-{index}",
            approval_status="approved",
        )
        db.add_all([assistant, agent])
        db.flush()
        payload = {
            "name": agent.name,
            "description": "synthetic-http-only",
            "model_id": model.id,
            "classification_level": "無機密",
            "collection_ids": [],
            "scope": "unit",
            "target_department_ids": [],
            "tool_fingerprints": {},
            "graph": graph,
        }
        version = QuickVersion(
            assistant_id=assistant.id,
            revision=1,
            payload=payload,
            payload_hash=hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            status="published",
            created_by=owner.id,
            agent_id=agent.id,
        )
        db.add_all(
            [
                version,
                UserAgentPermission(
                    user_id=users[index % len(users)]["id"], agent_id=agent.id
                ),
            ]
        )
        assistants.append({"agent_id": agent.id, "name": agent.name})
    db.commit()
    return users, assistants


def memory_keyring():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app.services import jwt_keyring

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    kid = "synthetic-http-" + secrets.token_hex(12)
    signing = jwt_keyring.SigningMaterial(kid=kid, private_pem=private)
    verification = jwt_keyring.VerificationMaterial(
        public_pem=public,
        state=jwt_keyring.STATE_ACTIVE,
        retiring_at=None,
        accept_missing_iat=False,
    )
    # Only the storage provider is replaced. Real RS256 encode/decode,
    # issuance/type/token_version/user checks remain in product code.
    jwt_keyring.active_signing_material = lambda db=None: signing
    jwt_keyring.verification_material = lambda requested, db=None: (
        verification if requested == kid else None
    )
    jwt_keyring.active_kid = lambda db=None: kid


def deny_native_service_entrypoints():
    """Python socket auditing cannot govern libpq / gRPC C-core transports."""

    def forbidden(*args, **kwargs):
        _DENIALS["native_service"] = _DENIALS.get("native_service", 0) + 1
        raise LoadError("NATIVE_SERVICE_FORBIDDEN")

    guarded = []
    for name, attributes in (
        ("psycopg2", ("connect",)),
        ("psycopg", ("connect",)),
        ("asyncpg", ("connect", "create_pool")),
        ("grpc", ("insecure_channel", "secure_channel", "server")),
    ):
        if importlib.util.find_spec(name) is None:
            continue
        module = __import__(name)
        for attribute in attributes:
            if hasattr(module, attribute):
                setattr(module, attribute, forbidden)
                guarded.append(name + "." + attribute)
        if name == "grpc" and hasattr(module, "aio"):
            for attribute in ("insecure_channel", "secure_channel", "server"):
                if hasattr(module.aio, attribute):
                    setattr(module.aio, attribute, forbidden)
                    guarded.append("grpc.aio." + attribute)
    return guarded


class MeasuredApp:
    def __init__(self, app, counters):
        self.app, self.counters = app, counters

    async def __call__(self, scope, receive, send):
        measured = (
            scope["type"] == "http"
            and scope.get("path") == "/v1/chat/completions"
            and self.counters["load_active"]
        )
        if measured:
            self.counters["active_http_chat"] += 1
            self.counters["peak_http_chat"] = max(
                self.counters["peak_http_chat"], self.counters["active_http_chat"]
            )
        try:
            await self.app(scope, receive, send)
        finally:
            if measured:
                self.counters["active_http_chat"] -= 1


async def exercise_http(app, users, assistants, config, progress_path, started):
    global _LISTENER, _ENDPOINT
    import uvicorn
    from app.services.proxy import service as proxy_service

    counters = {
        "load_active": False,
        "active_http_chat": 0,
        "peak_http_chat": 0,
        "active_models": 0,
        "peak_models": 0,
        "barrier_releases": 0,
        "completed": 0,
        "errors": 0,
        "cancelled": 0,
        "stream_completed": 0,
        "nonstream_completed": 0,
        "login_completed": 0,
        "catalog_verified": 0,
        "permission_denials_verified": 0,
        "foreign_conversation_denial_verified": False,
        "event_loop_max_stall_ms": 0,
        "conversation_reload_verified": 0,
    }
    barrier, stop = asyncio.Barrier(config["concurrency"]), asyncio.Event()
    failure_injected = False

    async def model_wait():
        nonlocal failure_injected
        inject = config["inject_failure"] and not failure_injected
        if inject:
            failure_injected = True
        counters["active_models"] += 1
        counters["peak_models"] = max(
            counters["peak_models"], counters["active_models"]
        )
        try:
            if inject:
                await asyncio.sleep(0.025)
                raise LoadError("SYNTHETIC_MODEL_FAILURE")
            position = await asyncio.wait_for(barrier.wait(), timeout=20)
            if position == 0:
                counters["barrier_releases"] += 1
            await asyncio.sleep(config["delay_ms"] / 1000)
        except BaseException:
            counters["active_models"] -= 1
            raise

    async def synthetic_proxy_request(**kwargs):
        await model_wait()
        try:
            return {
                "object": "chat.completion",
                "model": "http-synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }
                ],
            }
        finally:
            counters["active_models"] -= 1

    async def synthetic_proxy_stream(**kwargs):
        await model_wait()
        try:
            for index in range(0, len(ANSWER), 128):
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": ANSWER[index : index + 128]},
                                    "finish_reason": None,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                await asyncio.sleep(0)
            yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield "data: [DONE]\n\n"
        finally:
            counters["active_models"] -= 1

    # Keep real complete_chat / stream_chat: quota prechecks, model snapshots,
    # SSE parsing, redaction and terminal validation all execute normally.
    proxy_service.proxy_request = synthetic_proxy_request
    proxy_service.proxy_stream = synthetic_proxy_stream
    _LISTENER = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _LISTENER.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _LISTENER.bind(("127.0.0.1", 0))
    _ENDPOINT = _LISTENER.getsockname()
    _LISTENER.listen(128)
    _LISTENER.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            MeasuredApp(app, counters),
            host="127.0.0.1",
            port=0,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            timeout_keep_alive=2,
            timeout_graceful_shutdown=3,
            limit_concurrency=150,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[_LISTENER]))
    last_loop_tick, stall_snapshots = [time.monotonic()], []
    observer_stop = threading.Event()

    def observe_stalls():
        last_capture = 0
        while not observer_stop.wait(0.2):
            now = time.monotonic()
            if (
                now - last_loop_tick[0] > 2
                and now - last_capture > 2
                and len(stall_snapshots) < 3
            ):
                last_capture = now
                stall_snapshots.append(
                    {
                        "stall_seconds": now - last_loop_tick[0],
                        "threads": stack_metadata(),
                    }
                )
                base.write_json(
                    progress_path.parent / "stall-stacks.json",
                    {
                        "scope": "live file/function/line metadata only; no source, locals or exception text",
                        "snapshots": stall_snapshots,
                    },
                )

    observer = threading.Thread(
        target=observe_stalls, name="quick-http-stall-observer", daemon=True
    )
    observer.start()

    async def watch_loop():
        previous = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            last_loop_tick[0] = now
            counters["event_loop_max_stall_ms"] = max(
                counters["event_loop_max_stall_ms"], (now - previous - 0.1) * 1000
            )
            previous = now

    loop_watch = asyncio.create_task(watch_loop())
    samples, chat_samples, ttft, errors = [], [], [], []
    login_samples = []
    per_user = [0] * len(users)
    first_conversations = [None] * len(users)
    load_started = None
    auth_started = time.monotonic()
    auth_cpu_started = cpu_seconds()
    try:
        while not server.started:
            if serving.done():
                await serving
                raise LoadError("HTTP_SERVER_START_FAILED")
            if time.monotonic() - auth_started > 10:
                raise LoadError("HTTP_SERVER_START_TIMEOUT")
            await asyncio.sleep(0.01)
        if (await http_request("GET", "/v1/agents"))[0] != 401:
            raise LoadError("UNAUTHENTICATED_ACCESS_ALLOWED")
        wrong = await http_request(
            "POST",
            "/api/auth/login",
            body={
                "username": users[0]["username"],
                "password": secrets.token_urlsafe(30),
            },
        )
        if wrong[0] != 401:
            raise LoadError("INVALID_PASSWORD_ALLOWED")
        for number, user in enumerate(users):
            login_started = time.monotonic()
            result = await json_request(
                "POST",
                "/api/auth/login",
                body={"username": user["username"], "password": user.pop("password")},
            )
            login_samples.append((time.monotonic() - login_started) * 1000)
            user["token"] = result["access_token"]
            counters["login_completed"] += 1
            catalog = await json_request("GET", "/v1/agents", token=user["token"])
            expected = {
                assistants[index]["name"]
                for index in range(number, len(assistants), len(users))
            }
            if {row["id"] for row in catalog["data"]} != expected:
                raise LoadError("CATALOG_AUTHORIZATION_MISMATCH")
            counters["catalog_verified"] += 1
            denied = await http_request(
                "POST",
                "/v1/chat/completions",
                token=user["token"],
                body={
                    "model": assistants[(number + 1) % len(users)]["name"],
                    "messages": [{"role": "user", "content": "合成權限反例"}],
                },
            )
            if denied[0] not in (403, 404):
                raise LoadError("UNAUTHORIZED_AGENT_ALLOWED")
            counters["permission_denials_verified"] += 1
            base.write_json(
                progress_path, {"phase": "auth", **counters, **base.self_metrics()}
            )
        auth_duration = time.monotonic() - auth_started
        auth_cpu = cpu_seconds() - auth_cpu_started
        counters["load_active"] = True
        load_started = time.monotonic()
        load_cpu_started = cpu_seconds()

        async def requester(number):
            user = users[number]
            for round_number, index in enumerate(
                range(number, len(assistants), len(users))
            ):
                if stop.is_set():
                    return
                request_started = time.monotonic()
                stage = "conversation_create"
                try:
                    assistant = assistants[index]
                    conv = await json_request(
                        "POST",
                        "/api/conversations",
                        expected=201,
                        token=user["token"],
                        body={
                            "title": "合成HTTP負載對話",
                            "agent_id": assistant["agent_id"],
                        },
                        extra_headers={"X-ANILA-Expected-User-Id": str(user["id"])},
                    )
                    conv_id = conv["id"]
                    if first_conversations[number] is None:
                        first_conversations[number] = conv_id
                    writer_token = secrets.token_urlsafe(18)
                    stage = "turn_create"
                    turn = await json_request(
                        "POST",
                        f"/api/conversations/{conv_id}/turn",
                        expected=201,
                        token=user["token"],
                        body={
                            "content": "合成HTTP負載問題",
                            "stream_writer": writer_token,
                            "agent_name": assistant["name"],
                        },
                    )
                    live = (number + round_number) % 2 == 1
                    stage = "chat"
                    chat_started, first_delta, stream_buffer = (
                        time.monotonic(),
                        None,
                        b"",
                    )

                    async def chunk_received(chunk):
                        nonlocal first_delta, stream_buffer
                        if not live:
                            return
                        stream_buffer += chunk
                        for line in stream_buffer.split(b"\n")[:-1]:
                            if line.startswith(b"data: {"):
                                frame = json.loads(line[6:])
                                if (
                                    frame.get("choices", [{}])[0]
                                    .get("delta", {})
                                    .get("content")
                                    and first_delta is None
                                ):
                                    first_delta = (
                                        time.monotonic() - chat_started
                                    ) * 1000
                        stream_buffer = stream_buffer.rsplit(b"\n", 1)[-1]

                    status, content, content_type = await asyncio.wait_for(
                        http_request(
                            "POST",
                            "/v1/chat/completions",
                            token=user["token"],
                            body={
                                "model": assistant["name"],
                                "stream": live,
                                "messages": [
                                    {"role": "user", "content": "合成HTTP負載問題"}
                                ],
                            },
                            extra_headers={"X-ANILA-Conversation-Id": str(conv_id)},
                            on_chunk=chunk_received,
                        ),
                        timeout=30,
                    )
                    chat_samples.append((time.monotonic() - chat_started) * 1000)
                    if status != 200:
                        raise LoadError("CHAT_HTTP_" + str(status))
                    answer = parse_answer(content, live, content_type)
                    if answer != ANSWER:
                        raise LoadError("CHAT_ANSWER_MISMATCH")
                    if live:
                        if first_delta is None:
                            raise LoadError("SSE_CONTENT_MISSING")
                        ttft.append(first_delta)
                    stage = "reply_finalize"
                    await json_request(
                        "PUT",
                        f"/api/conversations/{conv_id}/messages/{turn['assistant']['id']}",
                        token=user["token"],
                        body={
                            "content": answer,
                            "stream_writer": writer_token,
                            "metadata": {"anila_stream": {"state": "complete"}},
                        },
                    )
                    stage = "conversation_reload"
                    restored = await json_request(
                        "GET", f"/api/conversations/{conv_id}", token=user["token"]
                    )
                    if (
                        restored["agent_id"] != assistant["agent_id"]
                        or len(restored["messages"]) != 2
                        or restored["messages"][-1]["content"] != ANSWER
                    ):
                        raise LoadError("CONVERSATION_RELOAD_MISMATCH")
                    counters["conversation_reload_verified"] += 1
                    counters["stream_completed" if live else "nonstream_completed"] += 1
                    counters["completed"] += 1
                    per_user[number] += 1
                except asyncio.CancelledError:
                    counters["cancelled"] += 1
                    raise
                except Exception as exc:
                    counters["errors"] += 1
                    errors.append(
                        {
                            "user_index": number,
                            "assistant_index": index,
                            "stage": stage,
                            "code": exc.code
                            if isinstance(exc, LoadError)
                            else type(exc).__name__,
                        }
                    )
                    if base.error_rate(counters) > base.MAX_ERROR_RATE:
                        stop.set()
                        await barrier.abort()
                finally:
                    samples.append((time.monotonic() - request_started) * 1000)
                    base.write_json(
                        progress_path,
                        {
                            "phase": "load",
                            **counters,
                            "errors_sample": errors[:10],
                            **base.self_metrics(),
                        },
                    )

        await base.run_requesters(
            [requester(index) for index in range(len(users))],
            stop,
            max(1, config["wall_seconds"] - (time.monotonic() - started) - 5),
        )
        # Cross-user conversation ACL is tested through the real HTTP route.
        if not counters["errors"]:
            denied = await http_request(
                "GET",
                f"/api/conversations/{first_conversations[0]}",
                token=users[1]["token"],
            )
            if denied[0] not in (403, 404):
                raise LoadError("FOREIGN_CONVERSATION_ALLOWED")
            counters["foreign_conversation_denial_verified"] = True
        load_duration = time.monotonic() - load_started
        load_cpu = cpu_seconds() - load_cpu_started
    finally:
        observer_stop.set()
        observer.join(timeout=1)
        loop_watch.cancel()
        await asyncio.gather(loop_watch, return_exceptions=True)
        await barrier.abort()
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, timeout=8)
        except TimeoutError:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        _LISTENER.close()
        _ENDPOINT = None
        for user in users:
            user.pop("token", None)
            user.pop("password", None)
    duration = load_duration
    denominator = len(per_user) * sum(value * value for value in per_user)
    return {
        **counters,
        "load_wall_seconds": duration,
        "auth_wall_seconds": auth_duration,
        "auth_cpu_seconds": auth_cpu,
        "login_scheduling": "sequential before steady-state chat load; not simultaneous-login capacity",
        "login_total_wall_seconds": sum(login_samples) / 1000,
        "login_latency_ms": latency(login_samples),
        "load_cpu_seconds": load_cpu,
        "error_rate": base.error_rate(counters),
        "errors_sample": errors[:10],
        "latency_ms": latency(samples),
        "chat_latency_ms": latency(chat_samples),
        "stream_ttft_ms": latency(ttft),
        "synthetic_failure_injected": failure_injected,
        "throughput_workflows_per_second": counters["completed"] / max(duration, 0.001),
        "fairness": {
            "completed_per_user": per_user,
            "jain_completed_requests": sum(per_user) ** 2 / denominator
            if denominator
            else 0,
        },
        "listener_closed": _LISTENER.fileno() == -1,
        "server_task_done": serving.done(),
        "stall_observer_stopped": not observer.is_alive(),
    }


def parse_answer(content, live, content_type):
    try:
        if not live:
            return json.loads(content)["choices"][0]["message"]["content"]
        if not content_type.startswith("text/event-stream"):
            raise LoadError("SSE_CONTENT_TYPE_INVALID")
        parts, done = [], False
        for line in content.splitlines():
            if line in (b"event: anila.error", b"event: error"):
                raise LoadError("SSE_ERROR_FRAME")
            if line == b"data: [DONE]":
                done = True
            elif line.startswith(b"data: {"):
                frame = json.loads(line[6:])
                if (
                    frame.get("error")
                    or frame.get("type") == "anila.error"
                    or frame.get("object") == "anila.error"
                ):
                    raise LoadError("SSE_ERROR_FRAME")
                parts.append(
                    frame.get("choices", [{}])[0].get("delta", {}).get("content", "")
                )
        if not done:
            raise LoadError("SSE_DONE_MISSING")
        return "".join(parts)
    except (ValueError, KeyError, IndexError, TypeError):
        raise LoadError("CHAT_RESPONSE_INVALID") from None


def latency(samples):
    return {
        "count": len(samples),
        "mean": statistics.mean(samples) if samples else None,
        "p50": base.percentile(samples, 0.5),
        "p95": base.percentile(samples, 0.95),
        "p99": base.percentile(samples, 0.99),
        "max": max(samples, default=None),
    }


def cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def worker(config, work_dir, output_dir):
    global _DATABASE
    for kind, value in (
        (resource.RLIMIT_CPU, config["cpu_seconds"]),
        (resource.RLIMIT_AS, base.AS_BYTES),
        (resource.RLIMIT_NOFILE, 512),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
    ):
        resource.setrlimit(kind, (value, value))
    threading.stack_size(512 * 1024)
    started = time.monotonic()
    setup_cpu_started = cpu_seconds()
    progress_path = output_dir / "progress.json"
    base.write_json(progress_path, {"phase": "before_import", **base.self_metrics()})
    database = work_dir / "synthetic-http.sqlite3"
    if database.exists() or database.is_symlink():
        raise LoadError("FRESH_TEST_DATABASE_REQUIRED")
    _DATABASE = str(database)
    os.environ.update(
        {
            "DATABASE_URL": "sqlite:///" + str(database),
            "MIGRATION_DATABASE_URL": "sqlite:///" + str(database),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "ADMIN_PASSWORD": secrets.token_urlsafe(40),
            "ANILA_AUTH_MODE": "password",
            "ALLOWED_HOSTS": "127.0.0.1",
            "ANILA_SERVICE_CLIENT_AUTO_PROVISION": "0",
            "ANILA_JWT_KEYRING_MAINTAINER": "0",
            "ANILA_EXTERNAL_SERVICE_PROBE": "0",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    sys.path[:0] = [
        str(ROOT / "services/csp"),
        str(ROOT / "packages/anila-core/src"),
        sysconfig.get_path("purelib"),
        sysconfig.get_path("purelib", scheme="posix_user"),
        str(
            Path(sys.executable).resolve().parents[2]
            / ".local/lib"
            / ("python" + sysconfig.get_python_version())
            / "site-packages"
        ),
    ]
    logging.disable(logging.CRITICAL)
    native_guards = deny_native_service_entrypoints()
    from sqlalchemy import func
    from sqlalchemy.orm import Session
    from app.database import Base, engine
    from app.main import app
    from app.models.audit_log import AuditLog
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.models.quick_builder import QuickAssistant, QuickRun, QuickVersion
    from app.models.user import User
    from app.utils import security

    @asynccontextmanager
    async def isolated_lifespan(app):
        # Deliberately excludes production migrations, bootstrap accounts,
        # key files, external ingestion pools and maintenance jobs.
        yield

    app.router.lifespan_context = isolated_lifespan
    Base.metadata.create_all(engine)
    memory_keyring()
    with Session(engine, expire_on_commit=False) as db:
        users, assistants = seed(db, config)
    setup_wall = time.monotonic() - started
    setup_cpu = cpu_seconds() - setup_cpu_started
    base.write_json(progress_path, {"phase": "seeded", **base.self_metrics()})
    result = asyncio.run(
        exercise_http(app, users, assistants, config, progress_path, started)
    )
    with Session(engine) as db:
        requester_ids = [
            row[0]
            for row in db.query(QuickRun.user_id).distinct().order_by(QuickRun.user_id)
        ]
        result["persisted"] = {
            "assistants": db.query(QuickAssistant).count(),
            "runs": db.query(QuickRun).count(),
            "completed_runs": db.query(QuickRun).filter_by(status="completed").count(),
            "running_runs": db.query(QuickRun).filter_by(status="running").count(),
            "distinct_assistants": db.query(
                func.count(func.distinct(QuickVersion.assistant_id))
            )
            .join(QuickRun, QuickRun.version_id == QuickVersion.id)
            .scalar(),
            "distinct_user_ids": requester_ids,
            "distinct_user_count": len(requester_ids),
            "conversations": db.query(Conversation).count(),
            "messages": db.query(Message).count(),
            "assistant_answers": db.query(Message)
            .filter_by(role="assistant", content=ANSWER)
            .count(),
            "successful_logins": db.query(User)
            .filter(User.last_login_at.is_not(None))
            .count(),
            "audit_events": db.query(AuditLog).count(),
        }
    engine.dispose()
    result.update(
        config=config,
        phase="finished",
        total_worker_wall_seconds=time.monotonic() - started,
        bcrypt_rounds=security._BCRYPT_ROUNDS,
        setup_wall_seconds=setup_wall,
        setup_cpu_seconds=setup_cpu,
        native_service_guards=native_guards,
        audit_denials=dict(_DENIALS),
        **base.self_metrics(),
    )
    return result


def child_environment():
    return {**base.child_environment(), "MALLOC_ARENA_MAX": "2"}


def supervisor(config, output_dir):
    global _SPAWN_COMMAND
    output_dir.mkdir(mode=0o700)
    start, stop_reason, peak_rss, peak_vms = time.monotonic(), None, 0, 0
    error_detected = None
    with tempfile.TemporaryDirectory(
        prefix="quick-builder-http-load-work-", dir="/tmp"
    ) as directory:
        command = [
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).resolve()),
            "--worker",
            "--work-dir",
            directory,
            "--output-dir",
            str(output_dir),
        ]
        for key in (
            "profile",
            "assistants",
            "concurrency",
            "delay_ms",
            "wall_seconds",
            "cpu_seconds",
        ):
            command.extend(["--" + key.replace("_", "-"), str(config[key])])
        if config["inject_failure"]:
            command.append("--inject-failure")
        _SPAWN_COMMAND = tuple(command)
        process = subprocess.Popen(
            command,
            cwd="/tmp",
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            while process.poll() is None:
                memory = base.linux_memory(process.pid)
                peak_rss, peak_vms = (
                    max(peak_rss, memory.get("VmRSS", 0)),
                    max(peak_vms, memory.get("VmSize", 0)),
                )
                if time.monotonic() - start > config["wall_seconds"]:
                    stop_reason = "wall_watchdog"
                elif memory.get("VmRSS", 0) > base.RSS_BYTES:
                    stop_reason = "rss_watchdog"
                try:
                    if (
                        base.error_rate(
                            json.loads((output_dir / "progress.json").read_text())
                        )
                        > base.MAX_ERROR_RATE
                    ):
                        if stop_reason in (None, "error_rate_watchdog"):
                            stop_reason = "error_rate_watchdog"
                            if error_detected is None:
                                error_detected = time.monotonic()
                except (OSError, ValueError):
                    pass
                if stop_reason and (
                    stop_reason != "error_rate_watchdog"
                    or time.monotonic() - error_detected >= 5
                ):
                    break
                time.sleep(0.1)
        except BaseException as exc:
            stop_reason = "supervisor_" + type(exc).__name__
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            return_code = process.wait()
    try:
        result = json.loads((output_dir / "worker-result.json").read_text())
    except (OSError, ValueError):
        result = {"phase": "worker_exit_without_result"}
        try:
            progress = json.loads((output_dir / "progress.json").read_text())
            # Only this harness's bounded counters / stable error codes. A
            # forcibly killed child cannot certify listener/run finalization.
            fields = (
                "phase",
                "completed",
                "errors",
                "cancelled",
                "peak_http_chat",
                "peak_models",
                "login_completed",
                "event_loop_max_stall_ms",
                "errors_sample",
            )
            result["last_progress"] = {
                key: progress[key] for key in fields if key in progress
            }
        except (OSError, ValueError):
            pass
    if not stop_reason and base.error_rate(result) > base.MAX_ERROR_RATE:
        stop_reason = "error_rate_worker"
    if not stop_reason and result.get("audit_denials"):
        stop_reason = "audit_denial"
    if not stop_reason and return_code:
        stop_reason = {
            "MemoryError": "address_space_limit",
            "TimeoutError": "worker_timeout",
        }.get(
            result.get("error_type"),
            "worker_error" if return_code > 0 else "signal_" + str(-return_code),
        )
    persisted = result.get("persisted", {})
    verified = (
        all(
            persisted.get(key) == config["assistants"]
            for key in (
                "assistants",
                "runs",
                "completed_runs",
                "distinct_assistants",
                "conversations",
                "assistant_answers",
            )
        )
        and persisted.get("messages") == config["assistants"] * 2
        and persisted.get("distinct_user_count") == config["concurrency"]
        and persisted.get("successful_logins") == config["concurrency"]
        and persisted.get("running_runs") == 0
    )
    passed = (
        return_code == 0
        and stop_reason is None
        and result.get("errors") == 0
        and result.get("completed") == config["assistants"]
        and result.get("peak_http_chat") == config["concurrency"]
        and result.get("peak_models") == config["concurrency"]
        and result.get("foreign_conversation_denial_verified") is True
        and result.get("stream_completed", 0) > 0
        and result.get("nonstream_completed", 0) > 0
        and verified
    )
    usage, elapsed = (
        resource.getrusage(resource.RUSAGE_CHILDREN),
        time.monotonic() - start,
    )
    result.update(
        passed_configured_load=passed,
        achieved_nf08_http_goal=passed
        and config["concurrency"] == 100
        and config["assistants"] >= 300,
        persistence_verified=verified,
        scope="real local TCP HTTP / password login / JWT / catalog / chat streaming+nonstreaming / conversation APIs; synthetic model and isolated test lifespan",
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        helper_source_sha256=hashlib.sha256(
            Path(base.__file__).read_bytes()
        ).hexdigest(),
        supervisor={
            "return_code": return_code,
            "stop_reason": stop_reason,
            "child_reaped": process.returncode is not None,
            "temporary_database_removed": True,
            "wall_seconds": elapsed,
            "sampled_peak_rss_bytes": peak_rss,
            "sampled_peak_vms_bytes": peak_vms,
            "child_cpu_seconds": usage.ru_utime + usage.ru_stime,
            "average_cpu_percent_one_core": 100
            * (usage.ru_utime + usage.ru_stime)
            / elapsed,
        },
        limits={
            "wall_seconds": config["wall_seconds"],
            "cpu_seconds": config["cpu_seconds"],
            "address_space_bytes": base.AS_BYTES,
            "rss_stop_bytes": base.RSS_BYTES,
            "max_users": 100,
            "max_assistants": 1000,
            "open_files": 512,
            "response_bytes": MAX_RESPONSE_BYTES,
            "header_bytes": MAX_HEADER_BYTES,
            "error_rate_stop_above": base.MAX_ERROR_RATE,
            "error_cleanup_grace_seconds": 5,
            "thread_stack_bytes": 512 * 1024,
            "malloc_arena_max": 2,
            "error_rate_denominator": "completed+errors; cancellations excluded",
            "barrier_timeout_seconds": 20,
            "worker_subprocesses": 0,
            "bind": "127.0.0.1:0 exactly once",
        },
        host={
            "logical_cpus": os.cpu_count(),
            "kernel": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        limitations=[
            "Isolated test lifespan excludes production bootstrap/migrations/background external services; all route authentication and middleware remain intact.",
            "Input-model-output graphs, one department/user role, synthetic fixed-delay model, SQLite; not production model/GPU/PostgreSQL or mixed-role capacity.",
            "100 simultaneous HTTP/model waits are not 100 CPU-parallel model executions; finite three-round sample, not sustained capacity certification.",
            "RS256 key storage provider is in-memory only; no keyring persistence/rotation, TLS/nginx, browser cookie/CSRF workflow or upstream model quota measured.",
            "proxy_request/proxy_stream transport boundary is synthetic: outbound admission, SSRF/DNS, model credential transport and usage settlement inside that proxy are excluded. Real model callbacks retain quota prechecks (empty quota policy), snapshots, SSE parser and terminal checks.",
            "Single-child limits and watchdogs are not VM/container isolation or host resource reservation.",
        ],
    )
    base.write_json(output_dir / "report.json", result)
    base.write_json(
        output_dir / "worker-status.json",
        {"return_code": return_code, "stop_reason": stop_reason},
    )
    print(
        json.dumps(
            {
                "report": str(output_dir / "report.json"),
                "passed": passed,
                "achieved_nf08_http_goal": result["achieved_nf08_http_goal"],
            }
        )
    )
    return 0 if passed else 1


def main(argv=None):
    if not sys.flags.isolated or not sys.flags.no_site or os.getcwd() != "/tmp":
        raise SystemExit("Run from /tmp using python3 -I -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("smoke", "nf08"), default="nf08")
    parser.add_argument("--assistants", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--delay-ms", type=int, default=50)
    parser.add_argument("--wall-seconds", type=int, default=120)
    parser.add_argument("--cpu-seconds", type=int, default=90)
    parser.add_argument("--inject-failure", action="store_true")
    parser.add_argument("--output-dir")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config = validate_config(
            args.profile,
            args.assistants,
            args.concurrency,
            args.delay_ms,
            args.wall_seconds,
            args.cpu_seconds,
            args.inject_failure,
        )
        if args.worker:
            work_dir = base.private_tmp_path(
                args.work_dir, "quick-builder-http-load-work-"
            )
            output_dir = base.private_tmp_path(
                args.output_dir, "quick-builder-http-load-"
            )
            if (
                not work_dir.is_dir()
                or not output_dir.is_dir()
                or work_dir.stat().st_mode & 0o077
            ):
                raise ValueError("private temporary directories required")
            try:
                result = worker(config, work_dir, output_dir)
            except BaseException as exc:
                base.write_json(
                    output_dir / "worker-result.json",
                    {
                        "phase": "failed",
                        **failure_details(exc),
                        "config": config,
                        "audit_denials": dict(_DENIALS),
                        **base.self_metrics(),
                    },
                )
                return 1
            base.write_json(output_dir / "worker-result.json", result)
            return 0 if result["errors"] == 0 else 1
        if args.work_dir:
            raise ValueError("work-dir is worker-only")
        output_dir = (
            base.private_tmp_path(args.output_dir, "quick-builder-http-load-")
            if args.output_dir
            else Path("/tmp/quick-builder-http-load-" + str(time.time_ns()))
        )
        return supervisor(config, output_dir)
    except (ValueError, OSError) as exc:
        parser.error(
            "invalid configuration or temporary workspace: " + type(exc).__name__
        )


if __name__ == "__main__":
    raise SystemExit(main())
