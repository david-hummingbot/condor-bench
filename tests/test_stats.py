"""Phase 2: the statistics every interval in the matrix, router and compare rests on."""

from __future__ import annotations

import pytest

from bench import stats


def test_wilson_interval_matches_reference_values():
    # Reference values for the 95% Wilson score interval.
    assert stats.wilson_interval(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)
    assert stats.wilson_interval(16, 16) == pytest.approx((0.8064, 1.0), abs=1e-4)
    assert stats.wilson_interval(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert stats.wilson_interval(0, 10)[0] == 0.0


def test_wilson_interval_handles_no_data():
    assert stats.wilson_interval(0, 0) == (0.0, 0.0)


def test_gate_verdicts():
    assert stats.evaluate_gate((0.81, 0.99), 0.80) == "passing"
    assert stats.evaluate_gate((0.51, 1.0), 0.80) == "not_yet_passing"
    assert stats.evaluate_gate((0.20, 0.70), 0.80) == "failing"


def test_rate_summary_carries_the_gate():
    out = stats.rate_summary(4, 4, gate=0.80)
    assert out["rate"] == 1.0
    assert out["gate_status"] == "not_yet_passing", (
        "4/4 is a perfect point estimate but its lower bound is ~0.51 — not evidence "
        "that the model clears 0.80"
    )
    assert stats.rate_summary(0, 0, gate=0.8)["gate_status"] is None


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    ci = stats.bootstrap_ci(values)
    assert ci == stats.bootstrap_ci(values), "seeded — the same data gives the same CI"
    assert ci[0] <= 4.5 <= ci[1]
    assert stats.bootstrap_ci([]) is None
    assert stats.bootstrap_ci([3.0]) == (3.0, 3.0)


def test_median_p95_and_std_dev():
    assert stats.median([3, 1, 2]) == 2
    assert stats.median([1, 2, 3, 4]) == 2.5
    assert stats.percentile_95(list(range(1, 101))) == 95
    assert stats.std_dev([1.0]) is None
    assert stats.std_dev([2.0, 2.0, 2.0]) == 0.0


def test_trial_consistency_ignores_single_trial_cases():
    assert stats.trial_consistency({"a": [1.0], "b": [0.0]}) is None, (
        "no case ran twice — nothing to measure, which is not perfect consistency"
    )
    assert stats.trial_consistency({"a": [1.0, 1.0, 1.0], "b": [1.0]}) == 0.0
    flaky = stats.trial_consistency({"a": [1.0, 0.0, 1.0, 0.0]})
    assert flaky == pytest.approx(0.5774, abs=1e-4)


def test_distribution_summary():
    d = stats.distribution([1.0, 2.0, 3.0, 10.0])
    assert d["n"] == 4
    assert d["mean"] == 4.0
    assert d["median"] == 2.5
    assert d["p95"] == 10.0
    assert d["ci"][0] <= d["mean"] <= d["ci"][1]
    assert stats.distribution([]) is None


def test_paired_deltas_use_only_shared_keys():
    out = stats.paired_deltas({"a#1": 0.5, "b#1": 0.5, "c#1": 1.0}, {"a#1": 1.0, "b#1": 1.0})
    assert out["n"] == 2
    assert out["mean_delta"] == 0.5
