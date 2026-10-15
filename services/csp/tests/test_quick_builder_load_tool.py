"""The offline load harness refuses unsafe inputs without starting services."""

import importlib.util
from pathlib import Path

import pytest


_SOURCE = Path(__file__).resolve().parents[3] / "tools/loadtest/quick_builder_load.py"
_SPEC = importlib.util.spec_from_file_location("quick_builder_load_under_test", _SOURCE)
load = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(load)


def test_fixed_profiles_are_bounded_and_exact_load_goal_is_available():
    assert load.validate_config("smoke", None, None, 50, 120, 90)["assistants"] == 6
    full = load.validate_config("nf08", None, None, 50, 120, 90)
    assert (full["assistants"], full["concurrency"]) == (300, 100)


@pytest.mark.parametrize(
    "config",
    [
        ("nf08", 299, 100, 50, 120, 90),
        ("nf08", 1001, 100, 50, 120, 90),
        ("nf08", 300, 101, 50, 120, 90),
        ("nf08", 301, 100, 50, 120, 90),
        ("nf08", 300, 100, 251, 120, 90),
        ("nf08", 300, 100, 50, 121, 90),
        ("nf08", 300, 100, 50, 120, 91),
        ("smoke", 300, 100, 50, 120, 90),
        ("unknown", 300, 100, 50, 120, 90),
        ("nf08", True, 100, 50, 120, 90),
    ],
)
def test_out_of_range_or_non_integral_limits_are_rejected(config):
    with pytest.raises(ValueError):
        load.validate_config(*config)


@pytest.mark.parametrize(
    "path",
    [
        "https://production.invalid",
        "/var/lib/production",
        "/tmp/unrelated",
        "relative",
        None,
    ],
)
def test_only_dedicated_temporary_paths_are_accepted(path):
    with pytest.raises(ValueError):
        load.private_tmp_path(path, "quick-builder-load-")


@pytest.mark.parametrize(
    "argument", ["--url", "--model-url", "--database-url", "--password"]
)
def test_cli_has_no_endpoint_database_or_login_override(argument):
    with pytest.raises(SystemExit) as caught:
        load.main([argument, "synthetic-not-a-real-secret"])
    assert caught.value.code == 2


def test_failure_injection_is_not_allowed_for_nf08():
    with pytest.raises(SystemExit) as caught:
        load.main(["--profile", "nf08", "--inject-failure"])
    assert caught.value.code == 2


def test_worker_failure_report_never_serializes_exception_text(monkeypatch, tmp_path):
    work = tmp_path / "work"
    output = tmp_path / "output"
    work.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    monkeypatch.setattr(
        load,
        "private_tmp_path",
        lambda value, prefix: work if value == "work" else output,
    )

    def failed_worker(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_SETTINGS_MUST_NOT_APPEAR")

    monkeypatch.setattr(load, "worker", failed_worker)
    assert (
        load.main(
            [
                "--profile",
                "smoke",
                "--worker",
                "--work-dir",
                "work",
                "--output-dir",
                "output",
            ]
        )
        == 1
    )
    encoded = (output / "worker-result.json").read_text()
    report = load.json.loads(encoded)
    assert report["error_type"] == "RuntimeError"
    assert "SYNTHETIC_PRIVATE_SETTINGS" not in encoded
    assert "message" not in report
    assert "traceback" not in report


def test_child_limits_include_cpu_address_space_open_files_and_core(monkeypatch):
    calls = []
    monkeypatch.setattr(
        load.resource, "setrlimit", lambda kind, values: calls.append((kind, values))
    )
    load.set_limits({"cpu_seconds": 90})
    assert (load.resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2) in calls
    assert (load.resource.RLIMIT_CPU, (90, 90)) in calls
    assert (load.resource.RLIMIT_NOFILE, (256, 256)) in calls
    assert (load.resource.RLIMIT_CORE, (0, 0)) in calls


@pytest.mark.parametrize(
    "event,args",
    [
        ("open", ("/tmp/forbidden.env.key",)),
        ("open", ("/tmp/.env.synthetic",)),
        ("open", ("/tmp/secrets/data.txt",)),
        ("socket.connect", (object(), ("127.0.0.1", 1))),
        ("socket.getaddrinfo", ("outside.invalid", 443)),
        ("os.system", ("anything",)),
        ("subprocess.Popen", ("python", ["python", "-c", "unexpected"], None, None)),
    ],
)
def test_audit_guard_denies_protected_files_network_and_child_processes(
    monkeypatch, event, args
):
    monkeypatch.setattr(load, "_AUDIT_DENIALS", {})
    monkeypatch.setattr(load, "_ALLOWED_SPAWN", None)
    with pytest.raises(PermissionError):
        load.audit_guard(event, args)
    assert sum(load._AUDIT_DENIALS.values()) == 1


def test_supervisor_spawn_allowance_is_exact_and_single_use(monkeypatch):
    command = ("python", "-I", "-S", "fixed-script", "--worker")
    monkeypatch.setattr(load, "_ALLOWED_SPAWN", command)
    monkeypatch.setattr(load, "_SPAWNED", False)
    load.audit_guard("subprocess.Popen", (command[0], command, None, None))
    with pytest.raises(PermissionError):
        load.audit_guard("subprocess.Popen", (command[0], command, None, None))


def test_percentiles_are_interpolated_and_empty_samples_are_explicit():
    assert load.percentile([10, 30, 20], 0.5) == 20
    assert load.percentile([], 0.95) is None


def test_child_environment_is_fixed_and_does_not_inherit_app_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "untrusted-parent-value")
    monkeypatch.setenv("SECRET_KEY", "untrusted-parent-value")
    environment = load.child_environment()
    assert set(environment) == {"PATH", "LANG", "LC_ALL", "PYTHONHASHSEED", "TZ"}
    assert "untrusted-parent-value" not in environment.values()


def test_error_rate_budget_uses_finished_attempts_including_first_failure():
    assert load.error_rate({"errors": 1, "completed": 0, "cancelled": 99}) == 1
    assert load.error_rate({"errors": 1, "completed": 99}) == 0.01
    assert load.error_rate({}) == 0


@pytest.mark.asyncio
async def test_synthetic_failure_cancels_barrier_waiters_without_waiting_timeout():
    stop = load.asyncio.Event()
    waiting = load.asyncio.Event()
    cancelled = []
    barrier = load.asyncio.Barrier(2)

    async def waiting_request():
        try:
            waiting.set()
            await load.asyncio.wait_for(barrier.wait(), timeout=20)
        finally:
            cancelled.append(True)

    async def failing_request():
        await waiting.wait()
        stop.set()

    await load.asyncio.wait_for(
        load.run_requesters([waiting_request(), failing_request()], stop, 5),
        timeout=0.5,
    )
    assert cancelled == [True]
    assert not [
        task
        for task in load.asyncio.all_tasks()
        if task is not load.asyncio.current_task()
    ]


@pytest.mark.parametrize("threshold", ["rss", "errors", "interrupt"])
def test_watchdog_stops_and_reaps_the_only_child(monkeypatch, tmp_path, threshold):
    output = tmp_path / "synthetic-report"
    observed = []

    class Child:
        pid = 123456789
        returncode = None

        def __init__(self, *args, **kwargs):
            if threshold == "errors":
                load.write_json(output / "progress.json", {"errors": 1, "completed": 0})
            assert kwargs["env"] == load.child_environment()
            assert kwargs["stdout"] == load.subprocess.DEVNULL
            assert kwargs["stderr"] == load.subprocess.DEVNULL

        def poll(self):
            return self.returncode

        def terminate(self):
            observed.append("terminate")

        def kill(self):
            observed.append("kill")
            self.returncode = -9

        def wait(self, timeout=None):
            observed.append("wait")
            if self.returncode is None:
                raise load.subprocess.TimeoutExpired("synthetic-child", timeout)
            return self.returncode

    monkeypatch.setattr(load.subprocess, "Popen", Child)
    monkeypatch.setattr(
        load,
        "linux_memory",
        lambda pid: {"VmRSS": load.RSS_BYTES + 1} if threshold == "rss" else {},
    )
    monkeypatch.setattr(load, "_ALLOWED_SPAWN", None)
    if threshold == "interrupt":

        def interrupted(pid):
            raise KeyboardInterrupt

        monkeypatch.setattr(load, "linux_memory", interrupted)
    assert (
        load.supervisor(load.validate_config("nf08", 300, 100, 50, 120, 90), output)
        == 1
    )
    report = load.json.loads((output / "report.json").read_text())
    assert (
        report["supervisor"]["stop_reason"]
        == {
            "rss": "rss_watchdog",
            "errors": "error_rate_watchdog",
            "interrupt": "supervisor_KeyboardInterrupt",
        }[threshold]
    )
    assert report["supervisor"]["child_reaped"] is True
    assert report["supervisor"]["temporary_database_removed"] is True
    assert observed == ["terminate", "wait", "kill", "wait", "wait"]
