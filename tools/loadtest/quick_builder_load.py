"""Bounded, offline Quick service load test. Run from /tmp with python -I -S.

This starts one restricted child and calls the real quick_execution.run against
a newly created SQLite database. It never starts an HTTP server, signs a login
token, contacts a model, or accepts an endpoint/database URL.
"""

# ruff: noqa: E402 -- the audit hook must precede all non-bootstrap imports.
import os
import sys

_ALLOWED_SPAWN = None
_SPAWNED = False
_AUDIT_DENIALS = {}


def audit_guard(event, args):
    global _SPAWNED
    reason = None
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        parts = os.fsdecode(args[0]).replace("\\", "/").split("/")
        if (
            "secrets" in parts
            or parts[-1].startswith(".env")
            or parts[-1].endswith((".pem", ".key"))
        ):
            reason = "protected_file"
    elif event in {
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.sendto",
    }:
        reason = "network"
    elif event == "socket.__new__" and len(args) > 1 and args[1] != 1:
        # asyncio's local AF_UNIX self-pipe is permitted; connecting or binding
        # even an AF_UNIX socket is forbidden above.
        reason = "network"
    elif event == "subprocess.Popen":
        command = (
            tuple(args[1])
            if len(args) > 1 and isinstance(args[1], (list, tuple))
            else None
        )
        if (
            _ALLOWED_SPAWN is None
            or _SPAWNED
            or command != _ALLOWED_SPAWN
            or "--worker" in sys.argv
        ):
            reason = "subprocess"
        else:
            _SPAWNED = True
    elif event in {"os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
        reason = "subprocess"
    if reason:
        _AUDIT_DENIALS[reason] = _AUDIT_DENIALS.get(reason, 0) + 1
        raise PermissionError("Quick load sandbox blocked " + reason)


# Install before importing dependencies or application modules. -I -S prevents
# sitecustomize and executable .pth files from running before this hook.
if __name__ == "__main__":
    sys.addaudithook(audit_guard)

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sysconfig
import tempfile
import time

MAX_ASSISTANTS = 1000
MAX_CONCURRENCY = 100
AS_BYTES = 1536 * 1024 * 1024
RSS_BYTES = 900 * 1024 * 1024
MAX_WALL_SECONDS = 120
MAX_CPU_SECONDS = 90
MAX_ERROR_RATE = 0.01
ROOT = Path(__file__).resolve().parents[2]


def validate_config(
    profile, assistants, concurrency, delay_ms, wall_seconds, cpu_seconds
):
    if profile not in ("smoke", "nf08"):
        raise ValueError("unknown profile")
    assistants = (
        assistants if assistants is not None else (6 if profile == "smoke" else 300)
    )
    concurrency = (
        concurrency if concurrency is not None else (2 if profile == "smoke" else 100)
    )
    minimum, maximum = (2, 30) if profile == "smoke" else (300, MAX_ASSISTANTS)
    cap = 10 if profile == "smoke" else MAX_CONCURRENCY
    for value in (assistants, concurrency, delay_ms, wall_seconds, cpu_seconds):
        if type(value) is not int:
            raise ValueError("limits must be integers")
    if (
        not minimum <= assistants <= maximum
        or not 1 <= concurrency <= cap
        or concurrency > assistants
    ):
        raise ValueError("assistant/concurrency limits exceeded")
    if assistants % concurrency:
        raise ValueError(
            "assistants must be a multiple of concurrency for bounded barrier rounds"
        )
    if (
        not 25 <= delay_ms <= 250
        or not 5 <= wall_seconds <= MAX_WALL_SECONDS
        or not 2 <= cpu_seconds <= MAX_CPU_SECONDS
    ):
        raise ValueError("time limits exceeded")
    return {
        "profile": profile,
        "assistants": assistants,
        "concurrency": concurrency,
        "delay_ms": delay_ms,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
    }


def private_tmp_path(value, prefix):
    if type(value) is not str:
        raise ValueError("a dedicated temporary directory is required")
    path = Path(value)
    if (
        not path.is_absolute()
        or path.parent != Path("/tmp")
        or not path.name.startswith(prefix)
    ):
        raise ValueError("only a dedicated /tmp/" + prefix + " directory is allowed")
    if path.is_symlink():
        raise ValueError("symlink output/work directories are forbidden")
    return path


def write_json(path, data):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def self_metrics():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "cpu_user_seconds": usage.ru_utime,
        "cpu_system_seconds": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
    }


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lo, hi = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def set_limits(config):
    for kind, value in (
        (resource.RLIMIT_CPU, config["cpu_seconds"]),
        (resource.RLIMIT_AS, AS_BYTES),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
    ):
        resource.setrlimit(kind, (value, value))


def error_rate(counters):
    return counters.get("errors", 0) / max(
        1, counters.get("completed", 0) + counters.get("errors", 0)
    )


def child_environment():
    # Do not inherit application configuration, credentials or Python startup
    # overrides. HOME is deliberately absent; all source paths are explicit.
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
    }


async def run_requesters(requesters, stop, timeout):
    tasks = [asyncio.create_task(requester) for requester in requesters]
    finished = asyncio.gather(*tasks)
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (finished, stopped), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("bounded synthetic load timed out")
        if finished in done:
            await finished
    finally:
        for task in tasks:
            task.cancel()
        stopped.cancel()
        await asyncio.gather(*tasks, stopped, return_exceptions=True)
        # Retrieve the gather's cancellation/exception as well.
        await asyncio.gather(finished, return_exceptions=True)


def worker(config, work_dir, output_dir):
    set_limits(config)
    started = time.monotonic()
    progress = {"phase": "before_import", **self_metrics(), "completed": 0, "errors": 0}
    write_json(output_dir / "progress.json", progress)
    database = work_dir / "synthetic.sqlite3"
    if database.exists() or database.is_symlink():
        raise ValueError("worker requires a fresh test database")
    os.environ.update(
        {
            "DATABASE_URL": "sqlite:///" + str(database),
            "MIGRATION_DATABASE_URL": "sqlite:///" + str(database),
            "SECRET_KEY": "synthetic-load-test-not-a-login-credential",
            "ANILA_SERVICE_CLIENT_AUTO_PROVISION": "0",
            "ANILA_JWT_KEYRING_MAINTAINER": "0",
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
    ]
    from sqlalchemy import func
    from sqlalchemy.orm import Session
    from app.database import Base, engine
    import app.models  # noqa: F401
    from app.models.agent import Agent, UserAgentPermission
    from app.models.audit_log import AuditLog
    from app.models.department import Department
    from app.models.model_registry import ModelRegistry
    from app.models.quick_builder import QuickAssistant, QuickRun, QuickVersion
    from app.models.user import User, UserModelPermission
    from app.services import internal_llm
    from app.services.quick_execution import run
    from app.services.quick_flow import validate_graph

    progress.update(phase="imported", **self_metrics())
    write_json(output_dir / "progress.json", progress)
    Base.metadata.create_all(engine)
    graph = validate_graph(
        {
            "nodes": [
                {"id": "input", "kind": "input"},
                {"id": "model", "kind": "model"},
                {"id": "output", "kind": "output"},
            ],
            "edges": [
                {"source": "input", "target": "model"},
                {"source": "model", "target": "output"},
            ],
        }
    )
    ids = []
    with Session(engine, expire_on_commit=False) as db:
        unit = Department(name="Synthetic load unit")
        db.add(unit)
        db.flush()
        users = [
            User(
                username=f"synthetic-load-reader-{index}",
                role="user",
                department_id=unit.id,
                is_active=True,
                is_approved=True,
                local_password_disabled=True,
                hashed_password="NO_LOGIN_CREDENTIAL_SYNTHETIC_ONLY",
            )
            for index in range(config["concurrency"])
        ]
        model = ModelRegistry(
            name="synthetic-load-model",
            display_name="合成負載模型",
            endpoint_url="anila://synthetic-load-model",
            model_type="llm",
            is_active=True,
            supports_streaming=False,
            classification_ceiling="機密",
        )
        db.add_all([*users, model])
        db.flush()
        user_ids = [user.id for user in users]
        owner = users[0]
        db.add_all(
            UserModelPermission(user_id=user_id, model_id=model.id)
            for user_id in user_ids
        )
        for index in range(config["assistants"]):
            caller_id = user_ids[index % config["concurrency"]]
            assistant = QuickAssistant(
                owner_user_id=owner.id,
                department_id=unit.id,
                draft_revision=1,
                published_revision=1,
            )
            agent = Agent(
                name=f"synthetic-load-agent-{index}",
                owner_user_id=owner.id,
                owner_department_id=unit.id,
                base_model_id=model.id,
                endpoint_url=f"anila://quick-builder/synthetic-load-{index}",
                approval_status="approved",
            )
            db.add_all([assistant, agent])
            db.flush()
            payload = {
                "name": f"Synthetic load assistant {index}",
                "description": "synthetic-only",
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
                [version, UserAgentPermission(user_id=caller_id, agent_id=agent.id)]
            )
            db.flush()
            ids.append((assistant.id, version.id))
        db.commit()
    progress.update(phase="seeded", seeded_assistants=len(ids), **self_metrics())
    write_json(output_dir / "progress.json", progress)
    counters = {
        "active_requests": 0,
        "peak_requests": 0,
        "active_models": 0,
        "peak_models": 0,
        "barrier_releases": 0,
        "completed": 0,
        "errors": 0,
        "cancelled": 0,
    }
    samples, errors = [], []
    per_worker = [0] * config["concurrency"]
    first_admission = [None] * config["concurrency"]
    barrier = asyncio.Barrier(config["concurrency"])
    stop = asyncio.Event()
    load_started = time.monotonic()
    failure_injected = False

    async def synthetic_model(db, model, body, **kwargs):
        nonlocal failure_injected
        # Match the real model adapter's release of its DB connection before IO.
        db.commit()
        inject_here = config.get("inject_failure") and not failure_injected
        if inject_here:
            failure_injected = True
        counters["active_models"] += 1
        counters["peak_models"] = max(
            counters["peak_models"], counters["active_models"]
        )
        try:
            if inject_here:
                # Allow the other requesters to enter their barrier waits so
                # the smoke failure check also exercises immediate cancellation.
                await asyncio.sleep(0.01)
                raise RuntimeError("SYNTHETIC_MODEL_FAILURE")
            position = await asyncio.wait_for(barrier.wait(), timeout=20)
            if position == 0:
                counters["barrier_releases"] += 1
            await asyncio.sleep(config["delay_ms"] / 1000)
            return "合成助手回答，負載測試完成。"
        finally:
            counters["active_models"] -= 1

    internal_llm.complete_chat = synthetic_model

    async def requester(number):
        for index in range(number, len(ids), config["concurrency"]):
            if stop.is_set():
                return
            request_started = time.monotonic()
            if first_admission[number] is None:
                first_admission[number] = request_started - load_started
            counters["active_requests"] += 1
            counters["peak_requests"] = max(
                counters["peak_requests"], counters["active_requests"]
            )
            try:
                with Session(engine, expire_on_commit=False) as db:
                    actor = db.get(User, user_ids[number])
                    result = await run(db, actor, *ids[index], "合成助手負載問題")
                if result["status"] != "completed":
                    raise RuntimeError("SYNTHETIC_RUN_FAILED")
                counters["completed"] += 1
                per_worker[number] += 1
            except asyncio.CancelledError:
                counters["cancelled"] += 1
                raise
            except Exception as exc:
                counters["errors"] += 1
                errors.append(
                    {
                        "worker": number,
                        "assistant_index": index,
                        "type": type(exc).__name__,
                    }
                )
                if error_rate(counters) > MAX_ERROR_RATE:
                    stop.set()
            finally:
                counters["active_requests"] -= 1
                samples.append((time.monotonic() - request_started) * 1000)
                progress.update(phase="running", **counters, **self_metrics())
                write_json(output_dir / "progress.json", progress)

    async def exercise():
        await run_requesters(
            [requester(i) for i in range(config["concurrency"])],
            stop,
            timeout=max(1, config["wall_seconds"] - (time.monotonic() - started) - 2),
        )

    asyncio.run(exercise())
    duration = time.monotonic() - load_started
    with Session(engine) as db:
        persisted = {
            "assistants": db.query(QuickAssistant).count(),
            "runs": db.query(QuickRun).count(),
            "completed_runs": db.query(QuickRun).filter_by(status="completed").count(),
            "distinct_run_assistants": db.query(
                func.count(func.distinct(QuickVersion.assistant_id))
            )
            .join(QuickRun, QuickRun.version_id == QuickVersion.id)
            .scalar(),
            "audit_events": db.query(AuditLog).count(),
            "distinct_requester_ids": [
                row[0]
                for row in db.query(QuickRun.user_id)
                .distinct()
                .order_by(QuickRun.user_id)
            ],
        }
        persisted["distinct_requester_count"] = len(persisted["distinct_requester_ids"])
    engine.dispose()
    denominator = len(per_worker) * sum(value * value for value in per_worker)
    return {
        "phase": "finished",
        "config": config,
        **counters,
        **self_metrics(),
        "persisted": persisted,
        "load_wall_seconds": duration,
        "total_worker_wall_seconds": time.monotonic() - started,
        "throughput_requests_per_second": counters["completed"] / duration,
        "latency_ms": {
            "min": min(samples, default=0),
            "mean": statistics.mean(samples) if samples else 0,
            "p50": percentile(samples, 0.5),
            "p95": percentile(samples, 0.95),
            "p99": percentile(samples, 0.99),
            "max": max(samples, default=0),
        },
        "error_rate": error_rate(counters),
        "error_rate_denominator": "completed + errors; cancellations excluded",
        "synthetic_failure_injected": failure_injected,
        "seeded_requester_ids": user_ids,
        "errors_sample": errors[:10],
        "fairness": {
            "completed_per_worker": per_worker,
            "jain_completed_requests": sum(per_worker) ** 2 / denominator
            if denominator
            else 0,
            "first_admission_max_seconds": max(
                (value for value in first_admission if value is not None), default=0
            ),
        },
        "audit_denials": dict(_AUDIT_DENIALS),
    }


def linux_memory(pid):
    found = {}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith(("VmRSS:", "VmSize:")):
                    key, value = line.split(":", 1)
                    found[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return found


def supervisor(config, output_dir):
    global _ALLOWED_SPAWN
    output_dir.mkdir(mode=0o700)
    stop_reason = None
    peak_rss = peak_vms = 0
    start = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="quick-builder-load-work-", dir="/tmp"
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
            "--profile",
            config["profile"],
            "--assistants",
            str(config["assistants"]),
            "--concurrency",
            str(config["concurrency"]),
            "--delay-ms",
            str(config["delay_ms"]),
            "--wall-seconds",
            str(config["wall_seconds"]),
            "--cpu-seconds",
            str(config["cpu_seconds"]),
        ]
        if config.get("inject_failure"):
            command.append("--inject-failure")
        _ALLOWED_SPAWN = tuple(command)
        with (output_dir / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd="/tmp",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=child_environment(),
            )
            try:
                while process.poll() is None:
                    memory = linux_memory(process.pid)
                    peak_rss, peak_vms = (
                        max(peak_rss, memory.get("VmRSS", 0)),
                        max(peak_vms, memory.get("VmSize", 0)),
                    )
                    if time.monotonic() - start > config["wall_seconds"]:
                        stop_reason = "wall_watchdog"
                    elif memory.get("VmRSS", 0) > RSS_BYTES:
                        stop_reason = "rss_watchdog"
                    try:
                        progress = json.loads(
                            (output_dir / "progress.json").read_text()
                        )
                        if error_rate(progress) > MAX_ERROR_RATE:
                            stop_reason = "error_rate_watchdog"
                    except (OSError, ValueError):
                        pass
                    if stop_reason:
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
            log.write(
                json.dumps(
                    {"return_code": return_code, "stop_reason": stop_reason}
                ).encode()
            )
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_elapsed = time.monotonic() - start
    try:
        result = json.loads((output_dir / "worker-result.json").read_text())
    except (OSError, ValueError):
        result = {"phase": "worker_exit_without_result"}
    if not stop_reason and error_rate(result) > MAX_ERROR_RATE:
        stop_reason = "error_rate_worker"
    if not stop_reason and return_code != 0:
        stop_reason = {
            "MemoryError": "address_space_limit",
            "TimeoutError": "worker_timeout",
        }.get(
            result.get("error_type"),
            "worker_error" if return_code > 0 else "signal_" + str(-return_code),
        )
    persisted = result.get("persisted", {})
    persistence_verified = (
        all(
            persisted.get(key) == config["assistants"]
            for key in (
                "assistants",
                "runs",
                "completed_runs",
                "distinct_run_assistants",
            )
        )
        and persisted.get("audit_events") == 2 * config["assistants"]
        and persisted.get("distinct_requester_count") == config["concurrency"]
    )
    passed = (
        return_code == 0
        and not stop_reason
        and result.get("completed") == config["assistants"]
        and result.get("errors") == 0
        and result.get("peak_models") == config["concurrency"]
        and persistence_verified
    )
    result.update(
        {
            "passed_configured_load": passed,
            "persistence_verified": persistence_verified,
            "achieved_nf08_service_goal": passed
            and config["assistants"] >= 300
            and config["concurrency"] == 100,
            "scope": "direct quick_execution.run service layer; not full HTTP API, SSE, upstream model or quota benchmark",
            "tool_source_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            "supervisor": {
                "return_code": return_code,
                "stop_reason": stop_reason,
                "child_reaped": process.returncode is not None,
                "wall_seconds": wall_elapsed,
                "sampled_peak_rss_bytes": peak_rss,
                "sampled_peak_vms_bytes": peak_vms,
                "child_peak_rss_bytes": usage.ru_maxrss * 1024,
                "child_cpu_seconds": usage.ru_utime + usage.ru_stime,
                "average_cpu_percent_one_core": 100
                * (usage.ru_utime + usage.ru_stime)
                / wall_elapsed,
                "temporary_database_removed": True,
            },
            "limits": {
                "wall_seconds": config["wall_seconds"],
                "cpu_seconds": config["cpu_seconds"],
                "address_space_bytes": AS_BYTES,
                "rss_stop_bytes": RSS_BYTES,
                "error_rate_stop_above": MAX_ERROR_RATE,
                "error_rate_denominator": "completed + errors; cancellations excluded",
                "max_assistants": MAX_ASSISTANTS,
                "max_concurrency": MAX_CONCURRENCY,
                "model_barrier_timeout_seconds": 20,
                "child_subprocesses": 0,
                "file_size_bytes": 64 * 1024 * 1024,
            },
            "host": {
                "logical_cpus": os.cpu_count(),
                "kernel": platform.release(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
            },
            "limitations": [
                "Synthetic fixed-delay model and SQLite; no production services, HTTP routing, login, proxy quota or GPU workload.",
                "Distinct synthetic requesters share one department and user role; no mixed-role or cross-department capacity claim.",
                "Barrier-assisted concurrency proves 100 simultaneous service/model waits, not 100 CPU-parallel model executions.",
                "Fairness is completed requests per fixed worker; it does not establish production scheduler fairness.",
                "RLIMIT and watchdogs are per-child bounds, not a VM/container or complete host-resource reservation.",
            ],
        }
    )
    write_json(output_dir / "report.json", result)
    print(
        json.dumps(
            {
                "report": str(output_dir / "report.json"),
                "passed": passed,
                "achieved_nf08_service_goal": result["achieved_nf08_service_goal"],
            },
            ensure_ascii=False,
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
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="smoke-only fixed synthetic model failure",
    )
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
        )
        if args.inject_failure:
            if config["profile"] != "smoke":
                raise ValueError("failure injection is restricted to the smoke profile")
            config["inject_failure"] = True
        if args.worker:
            work_dir = private_tmp_path(args.work_dir, "quick-builder-load-work-")
            output_dir = private_tmp_path(args.output_dir, "quick-builder-load-")
            if (
                not work_dir.is_dir()
                or not output_dir.is_dir()
                or work_dir.stat().st_mode & 0o077
            ):
                raise ValueError("worker requires private temporary directories")
            try:
                result = worker(config, work_dir, output_dir)
            except BaseException as exc:
                write_json(
                    output_dir / "worker-result.json",
                    {
                        "phase": "failed",
                        "error_type": type(exc).__name__,
                        "config": config,
                        **self_metrics(),
                        "audit_denials": dict(_AUDIT_DENIALS),
                    },
                )
                return 1
            write_json(output_dir / "worker-result.json", result)
            return 0 if result["errors"] == 0 else 1
        if args.work_dir:
            raise ValueError("work-dir is reserved for the restricted worker")
        output_dir = (
            private_tmp_path(args.output_dir, "quick-builder-load-")
            if args.output_dir
            else Path("/tmp/quick-builder-load-" + str(time.time_ns()))
        )
        return supervisor(config, output_dir)
    except (ValueError, OSError) as exc:
        parser.error(
            "invalid configuration or temporary workspace: " + type(exc).__name__
        )


if __name__ == "__main__":
    raise SystemExit(main())
