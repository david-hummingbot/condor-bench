"""Case order: easiest first, without ever inverting a dependent series.

Two properties, and they pull against each other:

* cheap read-only probes run before destructive multi-tool ticks, so a model
  that cannot do the easy work shows it in minutes and a run cut short has
  mutated less;
* a numbered series that can affect itself still runs in order —
  `tool_set_leverage_003` ("put it *back* to 2x") means nothing unless `_001`
  set 3x first, and it fails quietly, because reverting a value that was never
  changed still returns success.
"""

from __future__ import annotations

import pytest

from bench.dataset import ORDERS, case_family, load_all_cases, order_cases


def _positions(cases):
    return {c.id: i for i, c in enumerate(cases, 1)}


def _ordered():
    return order_cases(load_all_cases(), "easiest-first")


# ── the family rule ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case_id,expected",
    [
        ("tool_set_leverage_003", ("tool_set_leverage", 3)),
        ("t001", ("t", 1)),
        ("c_journal_roundtrip_002", ("c_journal_roundtrip", 2)),
        ("agent_condor_005", ("agent_condor", 5)),
        ("matrix", ("matrix", 0)),
    ],
)
def test_family_parsing(case_id, expected):
    assert case_family(case_id) == expected


def test_the_leverage_series_is_never_inverted():
    """The one real ordering dependency in the library."""
    pos = _positions(_ordered())
    assert (
        pos["tool_set_leverage_001"]
        < pos["tool_set_leverage_002"]
        < pos["tool_set_leverage_003"]
    )


def test_no_same_scope_series_is_inverted_anywhere():
    """Every family that shares a scope, not just the one we know about."""
    ordered = _ordered()
    by_scope: dict[tuple, list[int]] = {}
    for case in ordered:
        stem, num = case_family(case.id)
        by_scope.setdefault((stem, getattr(case, "agent_slug", None)), []).append(num)
    inverted = {k: v for k, v in by_scope.items() if v != sorted(v)}
    assert not inverted, inverted


def test_independent_scenarios_are_not_forced_into_numeric_order():
    """t001-t009 have a slug each, so they sort by difficulty, not by number.

    Forcing the sequence here pushed the three read-only ticks to positions
    78-80 and pulled destructive t001 up to 51 — the opposite of the point.
    """
    ordered = _ordered()
    pos = _positions(ordered)
    ticks = [c for c in ordered if case_family(c.id)[0] == "t"]
    assert len({c.agent_slug for c in ticks}) > 1, "ticks should have distinct slugs"
    read_only = [c for c in ticks if c.risk_level == "read_only"]
    destructive = [c for c in ticks if c.risk_level == "destructive"]
    assert read_only and destructive
    assert max(pos[c.id] for c in read_only) < min(pos[c.id] for c in destructive)


# ── easiest first ──────────────────────────────────────────────────────────────


def test_read_only_runs_before_destructive():
    ordered = _ordered()
    pos = _positions(ordered)
    last_read_only = max(pos[c.id] for c in ordered if c.risk_level == "read_only")
    first_destructive = min(
        pos[c.id] for c in ordered if c.risk_level == "destructive"
    )
    assert last_read_only < first_destructive


def test_the_run_starts_on_single_tool_read_only_probes():
    for case in _ordered()[:5]:
        assert case.risk_level == "read_only"
        assert case.type == "tool"


def test_nothing_is_lost_or_duplicated():
    cases = load_all_cases()
    ordered = order_cases(cases, "easiest-first")
    assert len(ordered) == len(cases)
    assert {c.id for c in ordered} == {c.id for c in cases}


def test_dataset_order_is_left_untouched():
    cases = load_all_cases()
    assert [c.id for c in order_cases(cases, "dataset")] == [c.id for c in cases]


def test_ordering_is_deterministic():
    """Two runs on one box must present cases identically or nothing compares."""
    first = [c.id for c in _ordered()]
    second = [c.id for c in _ordered()]
    assert first == second


def test_baselines_only_break_ties():
    """A slow read-only probe still runs before a fast destructive one."""
    cases = load_all_cases()
    slow_read_only = {c.id: 999.0 for c in cases if c.risk_level == "read_only"}
    ordered = order_cases(cases, "easiest-first", baselines=slow_read_only)
    pos = _positions(ordered)
    assert max(pos[c.id] for c in cases if c.risk_level == "read_only") < min(
        pos[c.id] for c in cases if c.risk_level == "destructive"
    )


def test_an_unknown_order_is_rejected():
    with pytest.raises(ValueError):
        order_cases(load_all_cases(), "hardest-first")
    assert "easiest-first" in ORDERS and "dataset" in ORDERS
