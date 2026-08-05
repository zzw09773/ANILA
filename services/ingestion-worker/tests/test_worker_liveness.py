"""Liveness contract for the arq worker (2026-08-05).

This process has no HTTP surface — ``infra/compose/platform.yml`` gives the
ingestion-worker no ``ports`` and it starts no server — so the only thing CSP's
service health overview can ask it about is the health-check key arq writes
into redis (``health_checker._probe_queue_worker``). Two properties of that key
are load-bearing, and both are pinned here:

1. **The key name must be the one CSP reads.** Otherwise a perfectly healthy
   worker renders as a red card forever.

2. **The key must outlive the longest a handler can block.** ``record_health``
   runs on arq's poll loop and every registered handler occupies that loop with
   synchronous work — ``extract_text`` alone was measured at 33–89 s for a
   400-page PDF and 81–103 s for 1000 pages (the spread is host load, not code),
   and both ``evaluate_strategies`` and ``reresolve_collection_relations`` call
   it in a *loop*, once per document.
   If the key's TTL (interval + 1 s) is shorter than that, a worker doing
   exactly its job stops renewing and the overview paints it dead. arq's 3600 s
   default clears every document size the platform accepts (50 MB per file);
   shortening it is only safe after every blocking path in every registered
   handler has been moved off the loop — see the enumeration in
   ``services/ingestion-worker/README.md``.

Nothing here asks the worker to be configured specially. It asserts that nobody
has configured it *worse*.
"""

from __future__ import annotations

import inspect

from arq.constants import default_queue_name, health_check_key_suffix
from arq.worker import Worker

from ingestion_worker.main import WorkerSettings

#: The literal CSP probes. Kept as a literal on purpose: if this file derived
#: it the same way the worker does, the two sides could drift together and the
#: test would still pass while the overview read a key nobody writes.
CSP_SIDE_HEALTH_CHECK_KEY = "arq:queue:health-check"

#: Below this the key stops outliving a blocking handler (see module docstring).
MIN_LIVENESS_INTERVAL_SECONDS = 3600


def _arq_default(param: str):
    """arq's own default, so this file does not keep a second copy of it."""
    return inspect.signature(Worker.__init__).parameters[param].default


def _effective(param: str):
    """What arq will use for this WorkerSettings: the override, else the default."""
    return getattr(WorkerSettings, param, _arq_default(param))


def _effective_health_check_key() -> str:
    explicit = getattr(WorkerSettings, "health_check_key", None)
    if explicit:
        return explicit
    queue_name = getattr(WorkerSettings, "queue_name", default_queue_name)
    return queue_name + health_check_key_suffix


def test_health_check_key_is_the_one_csp_reads() -> None:
    assert _effective_health_check_key() == CSP_SIDE_HEALTH_CHECK_KEY, (
        "worker 寫的 key 與 CSP 服務健康總覽讀的 key 不一致 —— "
        "健康的 worker 會被畫成永久紅點"
    )


def test_liveness_key_outlives_a_blocking_handler() -> None:
    """Shortening the interval re-introduces the false red this package removed.

    Every registered handler parses documents synchronously on the poll loop.
    Until that changes, the key's lifetime is the only thing separating "busy"
    from "dead", and it has to win.
    """
    interval = _effective("health_check_interval")
    assert interval >= MIN_LIVENESS_INTERVAL_SECONDS, (
        f"health_check_interval={interval}s 撐不過一次同步解析"
        f"(量到 400 頁 33–89s、1000 頁 81–103s,而 evaluate_strategies 與 "
        f"reresolve_collection_relations 還是逐份文件跑迴圈)—— "
        f"正在工作的 worker 會被畫成死的。要縮短它,得先把每一個 handler 的"
        f"每一條阻塞路徑都搬離事件迴圈"
    )
