"""Ordered phases and post-conditions: what makes a build case scorable.

A build ("read the playbook, then create the routine, then verify it runs") cannot
be scored by multiset F1. Two failures were indistinguishable under it:

* **order** — creating first and reading the playbook afterwards scored a clean 1.0
* **retries vs skips** — recovering from a schema error scored the same 0.667 as
  skipping a required phase entirely

And F1 only ever sees the responses the model got, never the state it left behind,
which for a build is the only question that matters.
"""

from __future__ import annotations

import pytest

from bench.post_conditions import _split, post_condition_score, verify
from metrics.tool_accuracy import ToolAccuracyMetric, phase_breakdown, score_phases

STEPS = [
    {"name": "read the playbook", "required_tools": ["manage_skill"]},
    {"name": "create the routine", "required_tools": ["manage_routines"]},
]


# ── ordered phases ─────────────────────────────────────────────────────────────
def test_correct_order_scores_one():
    assert score_phases(["manage_skill", "manage_routines"], STEPS) == 1.0


def test_wrong_order_is_penalised_where_f1_was_blind():
    """The hole this matcher exists to close."""
    trajectory = ["manage_routines", "manage_skill"]
    assert score_phases(trajectory, STEPS) == 0.5
    assert ToolAccuracyMetric().score(
        trajectory, ["manage_skill", "manage_routines"]
    ) == 1.0, "if F1 stopped scoring this 1.0, this test's premise is gone"


@pytest.mark.parametrize("retries", [1, 2, 3, 5])
def test_retries_are_free(retries):
    """Recovering from an error is competence, not noise to charge for."""
    trajectory = ["manage_skill"] + ["manage_routines"] * (1 + retries)
    assert score_phases(trajectory, STEPS) == 1.0


def test_a_skipped_phase_costs_its_share():
    assert score_phases(["manage_routines"], STEPS) == 0.5


def test_retry_and_skip_are_no_longer_the_same_score():
    """Under multiset F1 both landed on 0.667."""
    retried = ["manage_skill", "manage_routines", "manage_routines"]
    skipped = ["manage_routines"]
    assert score_phases(retried, STEPS) != score_phases(skipped, STEPS)


def test_unrelated_extra_calls_do_not_penalise():
    trajectory = [
        "get_user_context",
        "manage_skill",
        "get_market_data",
        "manage_routines",
    ]
    assert score_phases(trajectory, STEPS) == 1.0


def test_forbidden_tool_still_zeroes_everything():
    """A dry-run violation is not a partial-credit situation."""
    trajectory = ["manage_skill", "manage_routines", "manage_executors"]
    assert score_phases(trajectory, STEPS, ["manage_executors"]) == 0.0


def test_multi_tool_phase_needs_all_of_them():
    steps = [{"name": "gather", "required_tools": ["get_market_data", "search_history"]}]
    assert score_phases(["get_market_data"], steps) == 0.0
    assert score_phases(["get_market_data", "search_history"], steps) == 1.0


def test_namespaced_names_match():
    assert score_phases(["mcp__condor__manage_skill", "manage_routines"], STEPS) == 1.0


def test_no_steps_is_not_a_failure():
    assert score_phases(["anything"], []) == 1.0


def test_breakdown_names_the_failing_phase():
    rows = phase_breakdown(["manage_routines"], STEPS)
    assert rows[0]["phase"] == "read the playbook"
    assert rows[0]["satisfied"] is False
    assert rows[0]["missing_or_out_of_order"] == ["manage_skill"]
    assert rows[1]["satisfied"] is True


# ── post-conditions ────────────────────────────────────────────────────────────
def test_args_and_assertions_are_separated():
    args, assertions = _split(
        {"action": "list", "contains": ["bench_btc_price"], "nonempty": True}
    )
    assert args == {"action": "list"}
    assert assertions == {"contains": ["bench_btc_price"], "nonempty": True}


def test_unreachable_probe_scores_none_not_zero():
    """A staging blip must not read as a model that failed to build something."""
    rows = [{"tool": "manage_routines", "reachable": False, "score": None}]
    assert post_condition_score(rows) is None


def test_probe_scores_are_averaged():
    rows = [
        {"tool": "a", "reachable": True, "score": 1.0},
        {"tool": "b", "reachable": True, "score": 0.0},
    ]
    assert post_condition_score(rows) == 0.5


def test_unreachable_probes_are_ignored_in_the_average():
    rows = [
        {"tool": "a", "reachable": True, "score": 1.0},
        {"tool": "b", "reachable": False, "score": None},
    ]
    assert post_condition_score(rows) == 1.0


def test_no_conditions_means_no_probes(anyio_backend=None):
    import asyncio

    assert asyncio.run(verify({}, model="m")) == []


def test_probe_failure_is_reported_not_raised(monkeypatch):
    """verify() must never take a run down."""
    import asyncio

    async def _boom(*a, **kw):
        raise RuntimeError("no MCP here")

    import bench.cleanup as cleanup

    monkeypatch.setattr(cleanup, "_call_tool", _boom)
    rows = asyncio.run(
        verify({"manage_routines": {"action": "list"}}, model="m")
    )
    assert len(rows) == 1
    assert rows[0]["reachable"] is False
    assert rows[0]["score"] is None
    assert "no MCP here" in rows[0]["detail"]


# ── the fold into live_validity ────────────────────────────────────────────────
def test_post_conditions_do_not_add_a_sixth_weight():
    """Folding into live_validity is what keeps the weights summing to 1.0."""
    from config import SCORE_WEIGHTS

    assert "post_conditions" not in SCORE_WEIGHTS
    assert sum(SCORE_WEIGHTS.values()) == pytest.approx(1.0)
