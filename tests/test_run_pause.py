"""Pausing a run must stop it *between* cases, and never strand it.

A 429 asks for fewer requests per minute, not for the run to be thrown away —
cancelling a sweep two-thirds through discards every case already paid for. Pause
is the response that fits: hold at a case boundary, wait out the provider's window,
carry on with the same connection and the same accumulated scorecards.

The boundary matters. Pausing mid-case would abandon an in-flight tool call, and a
*mutating* case abandoned before `teardown` leaves a real resource behind on
staging with nothing left to remove it. So the gate sits before the next case
starts, which costs at most one more case and cannot half-apply anything.

The other trap is a paused run that cannot be cancelled: the pause loop would sit
on a flag nobody is left to clear. `cancel_run` therefore clears the pause flag
before cancelling, so the cancellation lands on the next poll.
"""

from __future__ import annotations

import asyncio

import pytest

from dashboard.backend.app import (
    _active_runs,
    _await_resume,
    cancel_run,
    pause_run,
    resume_run,
)


def _make_run(run_id: str = "testrun") -> dict:
    state = {
        "run_id": run_id,
        "status": "running",
        "events": [],
        "listeners": [],
        "task": None,
        "total": 0,
        "current_case": None,
        "next_case": None,
        "pause_event": asyncio.Event(),
    }
    _active_runs[run_id] = state
    return state


@pytest.fixture(autouse=True)
def _clean_runs():
    yield
    _active_runs.clear()


def _types(state: dict) -> list[str]:
    return [e["type"] for e in state["events"]]


def test_unpaused_run_passes_straight_through():
    async def go():
        state = _make_run()
        await asyncio.wait_for(_await_resume("testrun", state), timeout=1)
        return state

    state = asyncio.run(go())
    assert state["status"] == "running"
    assert _types(state) == [], "an unpaused gate must be silent, not emit events"


def test_pause_holds_at_the_gate_until_resumed():
    async def go():
        state = _make_run()
        await pause_run("testrun")
        # Still 'running': the flag is set, but no case boundary has been reached.
        assert state["status"] == "running"

        gate = asyncio.create_task(_await_resume("testrun", state))
        await asyncio.sleep(0.05)
        assert not gate.done(), "the gate must block while paused"
        assert state["status"] == "paused"

        await resume_run("testrun")
        await asyncio.wait_for(gate, timeout=2)
        return state

    state = asyncio.run(go())
    assert state["status"] == "running"
    assert _types(state) == ["run_paused", "run_resumed"]


def test_a_paused_run_can_still_be_cancelled():
    """Otherwise pause is a trap: the loop waits on a flag nobody will clear."""

    async def go():
        state = _make_run()
        await pause_run("testrun")
        gate = asyncio.create_task(_await_resume("testrun", state))
        state["task"] = gate
        await asyncio.sleep(0.05)
        assert not gate.done()

        await cancel_run("testrun")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(gate, timeout=2)
        return state

    state = asyncio.run(go())
    # Cancelled out of a pause must not claim the run resumed.
    assert "run_resumed" not in _types(state)


def test_pause_is_rejected_once_the_run_is_over():
    async def go():
        state = _make_run()
        state["status"] = "completed"
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await pause_run("testrun")
        assert exc.value.status_code == 409

    asyncio.run(go())


def test_pause_on_an_unknown_run_is_404():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(pause_run("nope"))
    assert exc.value.status_code == 404


def test_a_paused_run_still_blocks_a_second_run():
    """Pause suspends requests, not the staging lock.

    The run keeps its scorecards, its env overrides and its claim on the single
    condor target. Letting a new run start beside it would point two models at one
    instance and interleave their mutating cases — the exact collision the
    one-at-a-time guard exists to stop.
    """
    from fastapi import HTTPException

    from dashboard.backend.app import _ACTIVE_STATUSES, RunRequest, create_run

    assert "paused" in _ACTIVE_STATUSES

    state = _make_run()
    state["status"] = "paused"

    req = RunRequest(models=[{"model_key": "anthropic:claude-haiku-4-5"}])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_run(req))
    assert exc.value.status_code == 409
    assert "paused" in exc.value.detail


def test_listing_runs_survives_a_suite_shaped_state():
    """A suite run's state has no ``models`` key — it takes them from the suite.

    ``/api/runs`` used to subscript it, so the endpoint 500'd for as long as any
    suite run was in flight: precisely when the UI needs the listing to find its
    way back to the active run.
    """
    from dashboard.backend.app import list_runs

    state = _make_run()
    state["suite_id"] = "condor"
    state.pop("models", None)

    payload = asyncio.run(list_runs())
    row = next(r for r in payload["active"] if r["run_id"] == "testrun")
    assert row["models"] == []
    assert row["suite_id"] == "condor"
