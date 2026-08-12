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


# ── the post-condition cap ─────────────────────────────────────────────────────
def _built(post: list[dict]) -> object:
    """A convincing-sounding build result, with whatever the probe found."""
    from bench.client import BenchmarkResult, TurnResult

    r = BenchmarkResult(
        case_id="b",
        model="m",
        turns=[
            TurnResult(
                "Created it!",
                [{"tool": "manage_routines", "args": {}}],
                2.0,
                tool_responses=[{"tool": "manage_routines", "output": '{"ok":1}'}],
            )
        ],
        wiring={"api_url": "http://staging:8000", "autodiscovery_extras": []},
    )
    r.post_conditions = post
    return r


async def _score_with(post: list[dict]):
    from unittest.mock import patch

    from bench.scorer import score

    with patch("bench.scorer._quality_metric.a_score", return_value=(0.95, "ok")):
        return await score(_built(post), "create a routine", ["manage_routines"], 5.0)


async def test_absent_artefact_fails_the_case_even_when_the_prose_is_good():
    """0.5, not 0.0, is the real failure mode.

    Against {action: list, contains: [name]} a missing routine scores 0.5 — the
    `nonempty` half passes because the list holds *other* routines. A ==0 test would
    wave this through, which is why the cap triggers below 1.0.
    """
    from config import PASS_THRESHOLD, POST_CONDITION_FAIL_CAP

    card = await _score_with(
        [{"tool": "manage_routines", "reachable": True, "score": 0.5}]
    )
    assert card.composite == POST_CONDITION_FAIL_CAP
    assert card.composite < PASS_THRESHOLD, "a build that built nothing must not pass"
    assert card.post_condition_failed
    assert "manage_routines" in card.post_condition_failed


async def test_met_post_condition_leaves_the_composite_alone():
    from config import PASS_THRESHOLD

    card = await _score_with(
        [{"tool": "manage_routines", "reachable": True, "score": 1.0}]
    )
    assert card.composite > PASS_THRESHOLD
    assert card.post_condition_failed is None


async def test_unreachable_probe_does_not_cap():
    """A staging blip must not read as a model that failed to build something."""
    from config import PASS_THRESHOLD

    card = await _score_with(
        [{"tool": "manage_routines", "reachable": False, "score": None}]
    )
    assert card.composite > PASS_THRESHOLD
    assert card.post_condition_failed is None


async def test_case_without_post_conditions_is_untouched():
    from config import PASS_THRESHOLD

    card = await _score_with([])
    assert card.composite > PASS_THRESHOLD
    assert card.post_condition_failed is None


async def test_the_failure_reaches_the_matrix_through_the_composite():
    """matrix.py recomputes pass from composite, not from ScoreCard.passed.

    A flag alone would never affect a domain pass rate — this pins the mechanism.
    """
    from config import PASS_THRESHOLD

    card = await _score_with(
        [{"tool": "manage_routines", "reachable": True, "score": 0.0}]
    )
    persisted = card.as_dict()
    assert persisted["composite"] < PASS_THRESHOLD
    assert persisted["post_condition_failed"]


def test_every_post_condition_in_the_dataset_asserts_something_specific():
    """`nonempty` on a list call passes if *anything* is stored.

    Four post-conditions originally used it, so they would have passed even if the
    model did nothing — and once the cap exists, a vacuous assertion is worse than
    no assertion because it manufactures the appearance of verification.
    """
    from bench.dataset import load_all_cases

    vague = {}
    for case in load_all_cases():
        for tool, spec in (getattr(case, "post_conditions", {}) or {}).items():
            if not isinstance(spec, dict):
                continue
            if not spec.get("contains") and not spec.get("fields"):
                vague[case.id] = tool
    assert not vague, (
        f"post-conditions assert only nonempty: {vague}. Pin `contains` (or `fields`) "
        "on something the case created — name the artefact in the question so the "
        "assertion can check for it."
    )
