"""Fair per-model queue. One user's burst must not skip the others."""

from __future__ import annotations

import asyncio

from app.services.model_gate import (
    MemoryBoard,
    ModelAdmission,
    QUEUE_WAIT_SECONDS,
    STALE_MS,
    apply,
    fresh_state,
    queue_status_message,
    queue_timeout_message,
)


def _arrive(state, ticket, user, limit, now):
    return apply(
        state,
        {"op": "arrive", "ticket": ticket, "user": user, "limit": limit},
        now,
    )


def test_queue_messages_name_the_place_and_the_timeout():
    assert queue_status_message(3) == "目前使用人數較多，排隊中，你是第 3 位"
    assert queue_timeout_message() == f"排隊超過 {QUEUE_WAIT_SECONDS} 秒，請稍後再試"


def test_round_robin_serves_the_other_user_before_the_burst():
    state = fresh_state()
    now = 1_000_000
    assert _arrive(state, "Z", "z", 1, now)["status"] == "granted"
    assert _arrive(state, "A1", "a", 1, now)["status"] == "queued"
    _arrive(state, "A2", "a", 1, now)
    _arrive(state, "A3", "a", 1, now)
    _arrive(state, "B1", "b", 1, now)
    apply(state, {"op": "release", "ticket": "Z"}, now)

    waiting = [("A1", "a"), ("A2", "a"), ("A3", "a"), ("B1", "b")]
    granted: list[str] = []
    while waiting:
        progressed = False
        for ticket, user in list(waiting):
            result = apply(
                state,
                {"op": "poll", "ticket": ticket, "user": user, "limit": 1},
                now,
            )
            if result["status"] == "granted":
                granted.append(ticket)
                waiting.remove((ticket, user))
                apply(state, {"op": "release", "ticket": ticket}, now)
                progressed = True
                break
        assert progressed, granted
    assert granted == ["A1", "B1", "A2", "A3"]


def test_fifty_then_one_serves_the_newcomer_on_the_next_round():
    """A's burst stays in A's queue. B is the next user, not ticket 51."""
    state = fresh_state()
    now = 1_000_000
    assert _arrive(state, "Z", "z", 1, now)["status"] == "granted"
    for index in range(1, 51):
        assert _arrive(state, f"A{index}", "a", 1, now)["status"] == "queued"
    assert _arrive(state, "B1", "b", 1, now)["status"] == "queued"
    apply(state, {"op": "release", "ticket": "Z"}, now)
    first = apply(state, {"op": "poll", "ticket": "A1", "user": "a", "limit": 1}, now)
    assert first["status"] == "granted"
    apply(state, {"op": "release", "ticket": "A1"}, now)
    second = apply(state, {"op": "poll", "ticket": "B1", "user": "b", "limit": 1}, now)
    assert second["status"] == "granted"


def test_newcomer_does_not_take_a_free_slot_ahead_of_the_queue():
    state = fresh_state()
    now = 1_000_000
    _arrive(state, "Z", "z", 1, now)
    _arrive(state, "A1", "a", 1, now)
    apply(state, {"op": "release", "ticket": "Z"}, now)
    stolen = _arrive(state, "C1", "c", 1, now)
    assert stolen["status"] == "queued"
    assert stolen["position"] == 2
    got = apply(
        state, {"op": "poll", "ticket": "A1", "user": "a", "limit": 1}, now
    )
    assert got["status"] == "granted"


def test_only_waiter_is_position_one_and_times_out():
    state = fresh_state()
    now = 5_000_000
    _arrive(state, "Z", "z", 1, now)
    queued = _arrive(state, "A1", "a", 1, now)
    assert queued["position"] == 1
    later = now + QUEUE_WAIT_SECONDS * 1000
    # The holder keeps its lease, and the waiter keeps polling, the way a
    # live stream does. A single 120s jump would look like both had died.
    cursor = now
    while cursor < later:
        cursor = min(cursor + 4_000, later)
        if cursor >= later:
            break
        apply(state, {"op": "pulse", "ticket": "Z"}, cursor)
        still = apply(
            state, {"op": "poll", "ticket": "A1", "user": "a", "limit": 1}, cursor
        )
        assert still["status"] == "queued"
    apply(state, {"op": "pulse", "ticket": "Z"}, later)
    timed = apply(
        state, {"op": "poll", "ticket": "A1", "user": "a", "limit": 1}, later
    )
    assert timed["status"] == "timeout"
    # The slot is still held by Z; A is gone, so B can be the next waiter.
    nxt = _arrive(state, "B1", "b", 1, later)
    assert nxt["status"] == "queued"
    assert nxt["position"] == 1


def test_stale_waiter_does_not_block_the_next_user():
    state = fresh_state()
    now = 9_000_000
    _arrive(state, "Z", "z", 1, now)
    _arrive(state, "A1", "a", 1, now)
    _arrive(state, "B1", "b", 1, now)
    apply(state, {"op": "release", "ticket": "Z"}, now)
    later = now + STALE_MS + 1
    got = apply(
        state, {"op": "poll", "ticket": "B1", "user": "b", "limit": 1}, later
    )
    assert got["status"] == "granted"


def test_unlimited_when_the_field_is_empty():
    class Row:
        id = 7
        max_concurrent = None

    assert ModelAdmission.maybe(Row(), 3) is None


def test_two_waiters_share_one_board_without_losing_tickets():
    async def _run():
        board = MemoryBoard()

        async def arrive(i: int):
            adm = ModelAdmission(4, i % 5, 2, board=board)
            return await adm._step("arrive")

        results = await asyncio.gather(*[arrive(i) for i in range(30)])
        granted = [item for item in results if item["status"] == "granted"]
        queued = [item for item in results if item["status"] == "queued"]
        assert len(granted) == 2
        assert len(queued) == 28
        snap = apply(board.states["4"], {"op": "snapshot"}, 0)
        assert snap["inflight"] == 2
        assert snap["queue_length"] == 28

    asyncio.run(_run())


def test_poll_lock_timeout_keeps_the_place(monkeypatch):
    from app.services import model_gate as gate

    monkeypatch.setattr(gate, "POLL_SECONDS", 0)

    class Busy:
        def __init__(self) -> None:
            self.calls = 0

        async def mutate(self, model_id, fn, now):
            self.calls += 1
            if self.calls == 1:
                return {"status": "queued", "position": 4}
            raise gate.ModelSlotDenied(
                gate.UNAVAILABLE_MESSAGE, code="model_queue_unavailable"
            )

    async def _run():
        admission = ModelAdmission(1, "u", 1, board=Busy())
        positions = []
        async for position in admission.wait_positions():
            positions.append(position)
            if len(positions) == 2:
                break
        assert positions == [4, 4]
        assert admission.held is False

    asyncio.run(_run())
