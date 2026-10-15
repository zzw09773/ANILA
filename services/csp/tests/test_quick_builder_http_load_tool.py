"""Safety/parser regressions for the bounded real-loopback HTTP harness."""

import asyncio
import importlib.util
from pathlib import Path
import types

import pytest


_SOURCE = (
    Path(__file__).resolve().parents[3] / "tools/loadtest/quick_builder_http_load.py"
)
_SPEC = importlib.util.spec_from_file_location("quick_http_load_under_test", _SOURCE)
load = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(load)


@pytest.mark.parametrize(
    "argument", ["--url", "--host", "--port", "--database-url", "--dsn", "--password"]
)
def test_cli_never_accepts_url_port_dsn_or_credentials(argument):
    with pytest.raises(SystemExit) as caught:
        load.main([argument, "forbidden-input"])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "config",
    [
        ("nf08", 300, 101, 50, 120, 90, False),
        ("nf08", 1001, 100, 50, 120, 90, False),
        ("nf08", 300, 100, 50, 121, 90, False),
        ("nf08", 300, 100, 50, 120, 91, False),
        ("nf08", 300, 100, 50, 120, 90, True),
        ("smoke", 6, 1, 50, 120, 90, False),
    ],
)
def test_resource_and_failure_profile_caps(config):
    with pytest.raises(ValueError):
        load.validate_config(*config)


def test_single_listener_permission_is_object_bound_ephemeral_and_once(monkeypatch):
    listener = object()
    monkeypatch.setattr(load, "_LISTENER", listener)
    monkeypatch.setattr(load, "_BOUND", False)
    for target, endpoint in (
        (object(), ("127.0.0.1", 0)),
        (listener, ("0.0.0.0", 0)),
        (listener, ("127.0.0.1", 8080)),
    ):
        with pytest.raises(PermissionError):
            load.audit_guard("socket.bind", (target, endpoint))
    load.audit_guard("socket.bind", (listener, ("127.0.0.1", 0)))
    with pytest.raises(PermissionError):
        load.audit_guard("socket.bind", (listener, ("127.0.0.1", 0)))


def test_only_registered_listener_endpoint_can_be_contacted(monkeypatch):
    monkeypatch.setattr(load, "_ENDPOINT", ("127.0.0.1", 54321))
    load.audit_guard("socket.connect", (object(), ("127.0.0.1", 54321)))
    for endpoint in (
        ("127.0.0.1", 54322),
        ("localhost", 54321),
        ("192.0.2.1", 54321),
        "/tmp/other-service",
    ):
        with pytest.raises(PermissionError):
            load.audit_guard("socket.connect", (object(), endpoint))


def test_supervisor_may_spawn_only_one_exact_worker_command(monkeypatch):
    command = ("python3", "-I", "-S", str(_SOURCE), "--worker")
    monkeypatch.setattr(load, "_SPAWN_COMMAND", command)
    monkeypatch.setattr(load, "_SPAWNED", False)
    with pytest.raises(PermissionError):
        load.audit_guard(
            "subprocess.Popen", ("python3", ["python3", "-c", "forbidden"], None, None)
        )
    load.audit_guard("subprocess.Popen", (command[0], command, None, None))
    with pytest.raises(PermissionError):
        load.audit_guard("subprocess.Popen", (command[0], command, None, None))


@pytest.mark.parametrize(
    "event,args",
    [
        ("socket.getaddrinfo", ("127.0.0.1", 1)),
        ("socket.sendto", (object(), "127.0.0.1", 1)),
        ("socket.__new__", (object(), 10, 1, 0)),
        ("socket.__new__", (object(), 2, 2, 0)),
        ("subprocess.Popen", ("binary", ["binary"], None, None)),
        ("os.system", ("binary",)),
        ("psycopg2.connect", ("forbidden",)),
        ("open", ("/tmp/.env.forbidden",)),
        ("open", ("/tmp/secrets/forbidden",)),
        ("open", ("/tmp/forbidden.pem",)),
    ],
)
def test_nonlocal_native_process_or_protected_operations_are_denied(
    monkeypatch, event, args
):
    monkeypatch.setattr(load, "_SPAWN_COMMAND", None)
    with pytest.raises(PermissionError):
        load.audit_guard(event, args)


def test_sqlite_audit_allows_only_the_new_worker_database(monkeypatch):
    monkeypatch.setattr(
        load,
        "_DATABASE",
        "/tmp/quick-builder-http-load-work-synthetic/synthetic-http.sqlite3",
    )
    load.audit_guard("sqlite3.connect", (load._DATABASE,))
    for database in (":memory:", "/tmp/unrelated.db", "file:/tmp/unrelated.db?mode=ro"):
        with pytest.raises(PermissionError):
            load.audit_guard("sqlite3.connect", (database,))


def test_native_db_and_grpc_entrypoints_are_stubbed_before_app_import(monkeypatch):
    db_module = types.ModuleType("psycopg2")
    db_module.connect = lambda *args: pytest.fail("native connect was reached")
    grpc_module = types.ModuleType("grpc")
    grpc_module.insecure_channel = lambda *args: pytest.fail(
        "native channel was reached"
    )
    grpc_module.aio = types.SimpleNamespace(secure_channel=grpc_module.insecure_channel)
    monkeypatch.setitem(load.sys.modules, "psycopg2", db_module)
    monkeypatch.setitem(load.sys.modules, "grpc", grpc_module)
    monkeypatch.setattr(
        load.importlib.util,
        "find_spec",
        lambda name: object() if name in {"psycopg2", "grpc"} else None,
    )
    guarded = load.deny_native_service_entrypoints()
    assert set(guarded) == {
        "psycopg2.connect",
        "grpc.insecure_channel",
        "grpc.aio.secure_channel",
    }
    for call in (
        db_module.connect,
        grpc_module.insecure_channel,
        grpc_module.aio.secure_channel,
    ):
        with pytest.raises(load.LoadError, match="NATIVE_SERVICE_FORBIDDEN"):
            call("unused-synthetic-input")


def test_child_environment_does_not_inherit_application_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "forbidden-parent-value")
    monkeypatch.setenv("HTTP_PROXY", "forbidden-parent-value")
    environment = load.child_environment()
    assert set(environment) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONHASHSEED",
        "TZ",
        "MALLOC_ARENA_MAX",
    }
    assert "forbidden-parent-value" not in environment.values()


def test_failure_metadata_omits_exception_text_and_traceback():
    try:
        raise RuntimeError("DO_NOT_REPORT_SETTINGS_SENTINEL")
    except RuntimeError as exc:
        details = load.failure_details(exc)
    assert details["code"] == "RuntimeError"
    assert details["location"]["file"] == Path(__file__).name
    assert "DO_NOT_REPORT_SETTINGS" not in load.json.dumps(details)
    assert "message" not in details and "traceback" not in details


def test_live_stall_diagnostics_never_capture_locals_or_source(monkeypatch):
    synthetic_private_value = "DO_NOT_COPY_LOCAL_VARIABLE_SENTINEL"
    frame = load.sys._getframe()
    monkeypatch.setattr(
        load.sys, "_current_frames", lambda: {load.threading.main_thread().ident: frame}
    )
    snapshot = load.stack_metadata()
    encoded = load.json.dumps(snapshot)
    assert synthetic_private_value not in encoded
    assert snapshot[0]["main_thread"] is True
    assert len(snapshot[0]["locations"]) <= 32
    assert set(snapshot[0]["locations"][0]) == {"file", "function", "line"}


def test_stack_diagnostic_always_keeps_main_thread_under_thread_cap(monkeypatch):
    frame = load.sys._getframe()
    frames = {number: frame for number in range(60)}
    frames[load.threading.main_thread().ident] = frame
    monkeypatch.setattr(load.sys, "_current_frames", lambda: frames)
    snapshot = load.stack_metadata()
    assert len(snapshot) == 48
    assert snapshot[0]["main_thread"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,wire,code",
    [
        ({"content-length": "262145"}, b"", "HTTP_RESPONSE_LIMIT"),
        ({"content-length": "-1"}, b"", "HTTP_RESPONSE_LIMIT"),
        ({}, b"", "HTTP_LENGTH_REQUIRED"),
        (
            {"content-length": "2", "content-encoding": "gzip"},
            b"hi",
            "HTTP_COMPRESSION_FORBIDDEN",
        ),
        (
            {"transfer-encoding": "chunked", "content-length": "0"},
            b"",
            "HTTP_FRAMING_INVALID",
        ),
        ({"transfer-encoding": "chunked"}, b"40001\r\n", "HTTP_RESPONSE_LIMIT"),
        ({"transfer-encoding": "chunked"}, b"badhex\r\n", "HTTP_CHUNK_INVALID"),
        (
            {"transfer-encoding": "chunked"},
            b"0\r\ntrailer\r\n",
            "HTTP_TRAILERS_FORBIDDEN",
        ),
    ],
)
async def test_http_body_bombs_invalid_framing_and_compression_are_rejected(
    headers, wire, code
):
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    with pytest.raises(load.LoadError) as caught:
        _ = [chunk async for chunk in load.response_chunks(reader, headers)]
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_chunked_response_total_bound_and_valid_decoding():
    reader = asyncio.StreamReader()
    reader.feed_data(b"2\r\nhi\r\n3\r\nyou\r\n0\r\n\r\n")
    reader.feed_eof()
    assert (
        b"".join(
            [
                chunk
                async for chunk in load.response_chunks(
                    reader, {"transfer-encoding": "chunked"}
                )
            ]
        )
        == b"hiyou"
    )


@pytest.mark.asyncio
async def test_http_redirect_is_never_followed_and_connection_is_closed(monkeypatch):
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"HTTP/1.1 302 Found\r\nLocation: https://outside.invalid\r\nContent-Length: 0\r\n\r\n"
    )
    reader.feed_eof()
    closed = []

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            closed.append(True)

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        assert args == ("127.0.0.1", 54321)
        return reader, Writer()

    monkeypatch.setattr(load, "_ENDPOINT", ("127.0.0.1", 54321))
    monkeypatch.setattr(load.asyncio, "open_connection", connect)
    with pytest.raises(load.LoadError, match="HTTP_REDIRECT_FORBIDDEN"):
        await load.http_request("GET", "/v1/agents")
    assert closed == [True]


@pytest.mark.asyncio
async def test_client_cancellation_closes_socket_without_waiting_for_response(
    monkeypatch,
):
    reader = asyncio.StreamReader()
    entered, closed = asyncio.Event(), []

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            entered.set()

        def close(self):
            closed.append(True)

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        return reader, Writer()

    monkeypatch.setattr(load, "_ENDPOINT", ("127.0.0.1", 54321))
    monkeypatch.setattr(load.asyncio, "open_connection", connect)
    task = asyncio.create_task(
        load.http_request("POST", "/v1/chat/completions", body={"synthetic": True})
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    assert closed == [True]


@pytest.mark.parametrize(
    "wire,code",
    [
        (
            b'event: anila.error\ndata: {"code":"EXECUTION_FAILED","message":"synthetic"}\n\ndata: [DONE]\n\n',
            "SSE_ERROR_FRAME",
        ),
        (
            b'data: {"error":{"type":"EXECUTION_FAILED"}}\n\ndata: [DONE]\n\n',
            "SSE_ERROR_FRAME",
        ),
        (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            "SSE_DONE_MISSING",
        ),
        (b"data: broken\n\n", "SSE_DONE_MISSING"),
    ],
)
def test_sse_partial_or_error_never_counts_as_success(wire, code):
    with pytest.raises(load.LoadError) as caught:
        load.parse_answer(wire, True, "text/event-stream")
    assert caught.value.code == code


def test_real_sse_and_nonstream_shapes_decode_consistently():
    buffered = b'{"choices":[{"message":{"content":"answer"}}]}'
    streamed = b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
    assert load.parse_answer(buffered, False, "application/json") == "answer"
    assert load.parse_answer(streamed, True, "text/event-stream") == "answer"


def test_swallowed_native_audit_denial_cannot_be_reported_as_success(
    monkeypatch, tmp_path
):
    output = tmp_path / "denied-http-output"

    class Child:
        pid, returncode = 123456789, 0

        def __init__(self, *args, **kwargs):
            load.base.write_json(
                output / "worker-result.json",
                {
                    "completed": 6,
                    "errors": 0,
                    "peak_http_chat": 2,
                    "peak_models": 2,
                    "stream_completed": 3,
                    "nonstream_completed": 3,
                    "foreign_conversation_denial_verified": True,
                    "audit_denials": {"native_service": 1},
                    "persisted": {
                        "assistants": 6,
                        "runs": 6,
                        "completed_runs": 6,
                        "distinct_assistants": 6,
                        "conversations": 6,
                        "assistant_answers": 6,
                        "messages": 12,
                        "distinct_user_count": 2,
                        "successful_logins": 2,
                        "running_runs": 0,
                    },
                },
            )

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(load.subprocess, "Popen", Child)
    assert (
        load.supervisor(load.validate_config("smoke", None, None, 50, 120, 90), output)
        == 1
    )
    report = load.json.loads((output / "report.json").read_text())
    assert report["persistence_verified"] is True
    assert report["passed_configured_load"] is False
    assert report["supervisor"]["stop_reason"] == "audit_denial"


@pytest.mark.parametrize("reason", ["rss", "errors", "interrupt"])
def test_watchdog_faults_reap_only_child_and_remove_database(
    monkeypatch, tmp_path, reason
):
    output = tmp_path / "http-output"
    events = []
    clock = [0]

    class Child:
        pid, returncode = 123456789, None

        def __init__(self, command, **kwargs):
            assert command[1:3] == ["-I", "-S"] and "--worker" in command
            assert kwargs["env"] == load.child_environment()
            assert kwargs["stderr"] == load.subprocess.DEVNULL
            if reason == "errors":
                load.base.write_json(
                    output / "progress.json", {"completed": 0, "errors": 1}
                )

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            events.append("wait")
            if self.returncode is None:
                raise load.subprocess.TimeoutExpired("synthetic-only", timeout)
            return self.returncode

    def memory(pid):
        if reason == "interrupt":
            raise KeyboardInterrupt
        return {"VmRSS": load.base.RSS_BYTES + 1} if reason == "rss" else {}

    monkeypatch.setattr(load.subprocess, "Popen", Child)
    monkeypatch.setattr(load.base, "linux_memory", memory)

    def monotonic():
        clock[0] += 0.01
        return clock[0]

    monkeypatch.setattr(load.time, "monotonic", monotonic)
    monkeypatch.setattr(
        load.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + 1)
    )
    assert (
        load.supervisor(load.validate_config("smoke", None, None, 50, 120, 90), output)
        == 1
    )
    report = load.json.loads((output / "report.json").read_text())
    assert (
        report["supervisor"]["stop_reason"]
        == {
            "rss": "rss_watchdog",
            "errors": "error_rate_watchdog",
            "interrupt": "supervisor_KeyboardInterrupt",
        }[reason]
    )
    assert report["supervisor"]["child_reaped"] is True
    assert report["supervisor"]["temporary_database_removed"] is True
    assert events == ["terminate", "wait", "kill", "wait", "wait"]
