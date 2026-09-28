#!/usr/bin/env python3
"""Log in test users and open streaming chats through the dev stack.

The mock model is registered for the run and deactivated afterward.
Test users are removed when the API allows it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time

import httpx

MODEL = "anila-loadtest-mock"
USER_PREFIX = "zzload"
USER_PASSWORD = "loadtest-pass"


def _percent(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _admin_password() -> str:
    explicit = os.environ.get("ANILA_LOADTEST_PASSWORD")
    if explicit:
        return explicit
    try:
        names = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}"],
            text=True,
        ).splitlines()
    except Exception:
        names = []
    for name in names:
        if "csp" in name and "dev" in name:
            try:
                value = subprocess.check_output(
                    ["docker", "exec", name, "printenv", "ADMIN_PASSWORD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                continue
            if value:
                return value
    return "changeme"


def _stats_snapshot() -> list[str]:
    try:
        text = subprocess.check_output(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return [f"stats unavailable: {type(exc).__name__}"]
    keep = ("anila-platform-dev", "anila-nginx-dev", "loadtest-model", "pgbouncer")
    return [line for line in text.splitlines() if any(token in line for token in keep)]


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    # nginx 對 /api/auth/ 是每秒 10 次、burst 20。建立帳號可以快，登入要等。
    response = None
    for attempt in range(12):
        response = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code not in (429, 503) or attempt == 11:
            break
        await asyncio.sleep(0.25 * (attempt + 1))
    assert response is not None
    response.raise_for_status()
    return response.json()["access_token"]


async def _ensure_model(client: httpx.AsyncClient, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    listing = await client.get("/api/models", headers=headers)
    listing.raise_for_status()
    body = {
        "name": MODEL,
        "display_name": "負載測試（暫時）",
        "model_type": "llm",
        "endpoint_url": "http://loadtest-model:8080/v1",
        "api_version": "v1",
        "protocol": "openai_compatible",
        "is_internal": True,
        "router_enabled": True,
        "supports_streaming": True,
        "thinking_effort": "none",
        "max_concurrent": None,
    }
    found = next((row for row in listing.json() if row.get("name") == MODEL), None)
    if found is None:
        created = await client.post("/api/models", headers=headers, json=body)
        created.raise_for_status()
        model_id = int(created.json()["id"])
    else:
        model_id = int(found["id"])
        updated = await client.put(f"/api/models/{model_id}", headers=headers, json=body)
        updated.raise_for_status()
    grants = await client.put(
        f"/api/models/{model_id}/router-grants",
        headers=headers,
        json={"grants": [{"scope_type": "all"}]},
    )
    grants.raise_for_status()
    return model_id


async def _ensure_users(client: httpx.AsyncClient, admin: str, count: int) -> list[str]:
    headers = {"Authorization": f"Bearer {admin}"}
    semaphore = asyncio.Semaphore(8)

    async def create(index: int) -> None:
        name = f"{USER_PREFIX}{index:04d}"
        async with semaphore:
            response = await client.post(
                "/api/users",
                headers=headers,
                json={
                    "username": name,
                    "password": USER_PASSWORD,
                    "role": "user",
                    "email": f"{name}@loadtest.local",
                },
            )
            if response.status_code == 400 and "已存在" in response.text:
                return
            response.raise_for_status()

    await asyncio.gather(*(create(i) for i in range(count)))

    # /api/auth/ 是 10 次/秒、burst 20。間隔 120ms 才不會被 nginx 擋成 503。
    pace = asyncio.Lock()
    earliest = 0.0
    logged = 0

    async def login(index: int) -> str:
        nonlocal earliest, logged
        name = f"{USER_PREFIX}{index:04d}"
        async with pace:
            wait = earliest - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            earliest = time.monotonic() + 0.12
        token = await _login(client, name, USER_PASSWORD)
        async with pace:
            logged += 1
            done = logged
        if done % 500 == 0:
            print(f"logged in {done}/{count}", flush=True)
        return token

    return list(await asyncio.gather(*(login(i) for i in range(count))))


async def _one_chat(client: httpx.AsyncClient, token: str) -> dict:
    started = time.perf_counter()
    ttfb = None
    ttft = None
    error = None
    status = None
    try:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
                "max_tokens": 400,
            },
            timeout=httpx.Timeout(180.0, connect=20.0),
        ) as response:
            ttfb = time.perf_counter() - started
            status = response.status_code
            if response.status_code != 200:
                raw = (await response.aread())[:240]
                error = raw.decode("utf-8", errors="replace")
            else:
                event_name = None
                async for line in response.aiter_lines():
                    if line == "":
                        event_name = None
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event_name == "anila.error":
                        if isinstance(payload, dict):
                            error = "anila.error:" + str(
                                payload.get("code") or payload.get("message") or "empty"
                            )
                        else:
                            error = "anila.error"
                        break
                    if not isinstance(payload, dict):
                        continue
                    choices = payload.get("choices") or []
                    if not choices:
                        continue
                    content = (choices[0].get("delta") or {}).get("content")
                    if content and ttft is None:
                        ttft = time.perf_counter() - started
    except Exception as exc:
        error = type(exc).__name__
    return {
        "ok": error is None and ttft is not None,
        "status": status,
        "ttfb": ttfb,
        "ttft": ttft,
        "error": error,
    }


async def _run_level(base: str, tokens: list[str], level: int) -> dict:
    limits = httpx.Limits(max_connections=level + 64, max_keepalive_connections=level)
    samples: list[list[str]] = []
    stop = asyncio.Event()

    async def watch() -> None:
        while not stop.is_set():
            samples.append(await asyncio.to_thread(_stats_snapshot))
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    watcher = asyncio.create_task(watch())
    async with httpx.AsyncClient(base_url=base, verify=False, limits=limits, http2=False) as client:
        results = await asyncio.gather(
            *(_one_chat(client, tokens[i % len(tokens)]) for i in range(level))
        )
    stop.set()
    await watcher
    ok = [row for row in results if row["ok"]]
    ttft = [row["ttft"] for row in ok if row["ttft"] is not None]
    ttfb = [row["ttfb"] for row in results if row["ttfb"] is not None]
    errors: dict[str, int] = {}
    for row in results:
        if row["ok"]:
            continue
        # A 200 whose body never delivered a content token used to be keyed
        # as "200", which hid ReadTimeout-after-headers and anila.error frames.
        status = row["status"]
        if status not in (None, 200):
            key = str(status)
        elif row["error"]:
            key = row["error"]
        elif row["ttft"] is None:
            key = "200_empty"
        else:
            key = str(status or "unknown")
        errors[key] = errors.get(key, 0) + 1
    return {
        "level": level,
        "completed": len(ok),
        "total": level,
        "ttft_p50": _percent(ttft, 50),
        "ttft_p95": _percent(ttft, 95),
        "ttfb_p50": _percent(ttfb, 50),
        "ttfb_p95": _percent(ttfb, 95),
        "errors": errors,
        "samples": samples[-3:],
    }


async def _cleanup(client: httpx.AsyncClient, admin: str) -> None:
    headers = {"Authorization": f"Bearer {admin}"}
    listing = await client.get("/api/users", headers=headers)
    if listing.status_code == 200:
        for user in listing.json():
            if not str(user.get("username", "")).startswith(USER_PREFIX):
                continue
            removed = await client.delete(
                f"/api/users/{user['id']}/permanent",
                headers=headers,
            )
            if removed.status_code >= 400:
                await client.delete(f"/api/users/{user['id']}", headers=headers)
    models = await client.get("/api/models", headers=headers)
    if models.status_code == 200:
        for row in models.json():
            if row.get("name") == MODEL and row.get("is_active"):
                await client.delete(f"/api/models/{row['id']}", headers=headers)


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}s"


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://127.0.0.1:8443")
    parser.add_argument("--levels", default="300,500,1000,3000")
    parser.add_argument("--admin-user", default="admin")
    args = parser.parse_args()
    levels = [int(part) for part in args.levels.split(",") if part.strip()]
    password = _admin_password()
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    async with httpx.AsyncClient(base_url=args.base, verify=False, limits=limits, timeout=60) as client:
        admin = await _login(client, args.admin_user, password)
        await _ensure_model(client, admin)
        tokens = await _ensure_users(client, admin, max(levels))
        reports = []
        try:
            for level in levels:
                print(f"level {level} starting", flush=True)
                report = await _run_level(args.base, tokens, level)
                reports.append(report)
                print(
                    f"level {level} completed {report['completed']}/{report['total']} "
                    f"ttft p50={_fmt(report['ttft_p50'])} p95={_fmt(report['ttft_p95'])} "
                    f"errors={report['errors']}",
                    flush=True,
                )
        finally:
            await _cleanup(client, admin)
    print("\n# 結果")
    for report in reports:
        print(
            f"{report['level']}\t完成 {report['completed']}/{report['total']}\t"
            f"TTFT p50 {_fmt(report['ttft_p50'])}\tp95 {_fmt(report['ttft_p95'])}\t"
            f"TTFB p50 {_fmt(report['ttfb_p50'])}\tp95 {_fmt(report['ttfb_p95'])}\t"
            f"錯誤 {report['errors']}"
        )
        for sample in report["samples"]:
            for line in sample:
                print(f"  {line}")
    print(
        "\n外推：CSP 與 Router 的 worker 數在 32 與 8 封頂，這台 24 核與 EPYC 128 執行緒"
        "會開出同一組 process。EPYC 多出來的核主要給 nginx（worker_processes auto）"
        "和作業系統。記憶體 755GB 對 62GB，而且線上那套不會跟這次測試搶同一台機器。"
        "若這台已經被 CPU 打滿而錯誤很少，EPYC 的餘裕在 nginx 與系統，不在更多 CSP worker。"
    )


if __name__ == "__main__":
    asyncio.run(_main())
