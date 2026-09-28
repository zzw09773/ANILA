"""Worker counts stay inside the cap on a large host and a 1-CPU quota."""

from anila_core.runtime.cpu_budget import (
    cgroup_cpus,
    csp_worker_count,
    router_worker_count,
    visible_cpus,
)


def test_csp_caps_at_32_and_floors_at_2():
    assert csp_worker_count(1) == 2
    assert csp_worker_count(8) == 16
    assert csp_worker_count(24) == 32
    assert csp_worker_count(128) == 32


def test_router_caps_at_8():
    assert router_worker_count(1) == 2
    assert router_worker_count(4) == 4
    assert router_worker_count(24) == 8
    assert router_worker_count(128) == 8


def test_visible_cpus_is_at_least_one():
    assert visible_cpus() >= 1
    assert cgroup_cpus() is None or cgroup_cpus() >= 1
