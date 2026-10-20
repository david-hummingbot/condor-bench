"""Statistical routines for benchmark analysis.

Implements:
- Wilson score confidence intervals for binomial rates (pass rate, unsafe rate).
- Bootstrap confidence intervals for continuous metrics (latency, tokens, cost).
- Median, p95, standard deviation across trials for consistency.
- Gate evaluation: 'passing' (lower >= gate), 'not_yet_passing' (crosses gate), 'failing' (upper < gate).
- Paired differences for A/B trial comparisons.
"""

from __future__ import annotations

import math
import random
from typing import Any, Sequence

# 95% two-sided normal quantile (z = 1.95996)
Z_95 = 1.959963984540054


def wilson_interval(
    k: int,
    n: int,
    *,
    z: float = Z_95,
) -> tuple[float, float]:
    """Calculate the Wilson score confidence interval for a binomial proportion.

    Parameters
    ----------
    k : int
        Number of successes / events (0 <= k <= n).
    n : int
        Total number of trials (n >= 0).
    z : float
        Confidence level z-score (default 1.96 for 95% CI).

    Returns
    -------
    tuple[float, float]
        (lower_bound, upper_bound) bounded in [0.0, 1.0].
    """
    if n <= 0:
        return 0.0, 0.0

    k = max(0, min(k, n))
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    spread = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * (n * n))))

    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return round(lower, 4), round(upper, 4)


def bootstrap_ci(
    values: Sequence[float],
    *,
    resamples: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
    stat_fn: str = "mean",
) -> tuple[float, float] | None:
    """Bootstrap confidence interval for a metric (default 1000 resamples at 95% CI).

    Parameters
    ----------
    values : Sequence[float]
        Sample values.
    resamples : int
        Number of bootstrap iterations.
    alpha : float
        Significance level (0.05 = 95% CI).
    seed : int
        RNG seed for reproducibility.
    stat_fn : str
        'mean' or 'median'.

    Returns
    -------
    tuple[float, float] | None
        (lower_bound, upper_bound), or None if sample size is empty.
    """
    clean = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    n = len(clean)
    if n == 0:
        return None
    if n == 1:
        val = round(clean[0], 4)
        return val, val

    rng = random.Random(seed)
    estimates: list[float] = []

    # rng.choices draws the whole resample in C; the matrix bootstraps every cell,
    # so a per-element Python loop here is the difference between ms and seconds.
    for _ in range(resamples):
        sample = rng.choices(clean, k=n)
        if stat_fn == "median":
            sample.sort()
            mid = n // 2
            med = sample[mid] if (n % 2 == 1) else (sample[mid - 1] + sample[mid]) / 2.0
            estimates.append(med)
        else:
            estimates.append(sum(sample) / n)

    estimates.sort()
    lower_idx = max(0, int(resamples * (alpha / 2.0)))
    upper_idx = min(resamples - 1, int(resamples * (1.0 - alpha / 2.0)))
    return round(estimates[lower_idx], 4), round(estimates[upper_idx], 4)


def std_dev(values: Sequence[float]) -> float | None:
    """Sample standard deviation (N - 1 denominator). None if N < 2."""
    clean = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    n = len(clean)
    if n < 2:
        return None
    mean_val = sum(clean) / n
    variance = sum((x - mean_val) ** 2 for x in clean) / (n - 1)
    return round(math.sqrt(variance), 4)


def median(values: Sequence[float]) -> float | None:
    """Sample median."""
    clean = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not clean:
        return None
    clean.sort()
    n = len(clean)
    mid = n // 2
    if n % 2 == 1:
        return round(clean[mid], 4)
    return round((clean[mid - 1] + clean[mid]) / 2.0, 4)


def percentile_95(values: Sequence[float]) -> float | None:
    """95th percentile."""
    clean = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not clean:
        return None
    clean.sort()
    idx = min(len(clean) - 1, max(0, round(0.95 * (len(clean) - 1))))
    return round(clean[idx], 4)


def evaluate_gate(
    interval: tuple[float, float],
    gate: float,
) -> str:
    """Evaluate whether an interval satisfies, crosses, or fails a threshold.

    Returns:
    - 'passing' if lower bound >= gate
    - 'not_yet_passing' if lower < gate <= upper (interval crosses threshold)
    - 'failing' if upper bound < gate
    """
    lower, upper = interval
    if lower >= gate:
        return "passing"
    if upper < gate:
        return "failing"
    return "not_yet_passing"


def paired_deltas(
    baseline_values: dict[str, float],
    candidate_values: dict[str, float],
) -> dict[str, Any]:
    """Calculate paired differences for matching keys (e.g. task/trial seeds)."""
    common_keys = sorted(set(baseline_values.keys()) & set(candidate_values.keys()))
    if not common_keys:
        return {"n": 0, "mean_delta": None, "ci": None}

    diffs = [candidate_values[k] - baseline_values[k] for k in common_keys]
    mean_d = sum(diffs) / len(diffs)
    ci = bootstrap_ci(diffs, stat_fn="mean")
    return {
        "n": len(common_keys),
        "mean_delta": round(mean_d, 4),
        "ci": ci,
        "std_dev": std_dev(diffs),
    }


def trial_consistency(groups: dict[Any, Sequence[float]]) -> float | None:
    """Mean across-trial standard deviation, over the groups that have 2+ trials.

    *groups* maps a task (case id) to its per-trial values. 0.0 means every task
    scored identically on every trial; None means no task was run more than once,
    so there is nothing to measure — not perfect consistency.
    """
    stds = [sd for values in groups.values() if (sd := std_dev(values)) is not None]
    if not stds:
        return None
    return round(sum(stds) / len(stds), 4)


def rate_summary(k: int, n: int, *, gate: float | None = None) -> dict[str, Any]:
    """Point rate, Wilson interval and (when *gate* is set) the gate verdict."""
    if n <= 0:
        return {"k": k, "n": n, "rate": None, "ci": None, "gate_status": None}
    ci = wilson_interval(k, n)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4),
        "ci": list(ci),
        "gate_status": evaluate_gate(ci, gate) if gate is not None else None,
    }


def distribution(values: Sequence[float], *, digits: int = 2) -> dict[str, Any] | None:
    """Mean, median, p95 and a bootstrap CI of the mean — None for no data."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    ci = bootstrap_ci(clean)
    return {
        "n": len(clean),
        "mean": round(sum(clean) / len(clean), digits),
        "median": None if (m := median(clean)) is None else round(m, digits),
        "p95": None if (p := percentile_95(clean)) is None else round(p, digits),
        "ci": None if ci is None else [round(ci[0], digits), round(ci[1], digits)],
    }
