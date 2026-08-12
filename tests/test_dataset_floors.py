"""Every case the datasets still carry is load-bearing — prove it before trimming.

106 cases looked like padding, and most of it was not. The tool axis had been tuned
down to `MIN_TOOL_CASES` almost everywhere: 15 of 24 tools sat at exactly three
hits, so a proposed "cut the paraphrases" pass would have pushed nine tools under
the floor and turned their verdicts into `thin` — the same silence as never having
benchmarked them, but wearing the look of a completed run.

These tests make that failure loud at edit time instead of at the end of an
overnight sweep. Two floors, and they are not the same number:

* **Tools** need `MIN_TOOL_CASES`. `TOOL_PASS_RATE` (0.65) lets 2/3 pass, so three
  is the smallest sample where one miss is survivable.
* **Domains** need *five*, not `min_cases` (3). `DOMAIN_PASS_RATE` is 0.80 and
  `Candidate.blockers` rejects `pass_rate < min_pass_rate`, so 2/3 = 0.667 and
  3/4 = 0.75 both fail: at three or four cases a domain must score *perfectly* to
  earn a recommendation. 4/5 = 0.80 exactly is the first size that tolerates a
  single failure, which is what asking for evidence is supposed to mean.

Neither floor has slack for an *exclusion*. Infra failures are excluded rather
than scored 0 (see bench/matrix.py), so a tool at exactly three drops to `thin` on
one flaky live call. That is an argument for adding a fourth hit to the
network-dependent tools, never for removing the third.
"""

from __future__ import annotations

import collections

from bench.dataset import is_routing_domain, load_all_cases
from config import DOMAIN_PASS_RATE, MIN_TOOL_CASES, TOOL_PASS_RATE

# The smallest domain that can absorb one failed case. Derived below rather than
# hard-coded so raising DOMAIN_PASS_RATE moves the floor instead of quietly
# invalidating it.
def _min_domain_cases() -> int:
    n = MIN_TOOL_CASES
    while (n - 1) / n < DOMAIN_PASS_RATE:
        n += 1
    return n


def test_one_miss_is_survivable_at_each_floor():
    """The floors exist so a single bad case is not fatal. Check the arithmetic."""
    assert (MIN_TOOL_CASES - 1) / MIN_TOOL_CASES >= TOOL_PASS_RATE

    floor = _min_domain_cases()
    assert (floor - 1) / floor >= DOMAIN_PASS_RATE
    assert (floor - 2) / (floor - 1) < DOMAIN_PASS_RATE, (
        f"{floor} should be the *smallest* domain size tolerating one miss"
    )


def test_every_expected_tool_clears_min_tool_cases():
    counts: collections.Counter[str] = collections.Counter()
    for case in load_all_cases():
        for tool in case.expected_tools or []:
            counts[tool] += 1

    thin = {t: n for t, n in counts.items() if n < MIN_TOOL_CASES}
    assert not thin, (
        f"tools below MIN_TOOL_CASES={MIN_TOOL_CASES}: {thin}. Every model's verdict "
        "for these lands in `thin` rather than handled/unhandled — the tool is not "
        "measured at all. Add a case that calls the tool, or accept the gap "
        "deliberately by lowering MIN_TOOL_CASES."
    )


def test_every_routing_domain_can_absorb_one_failed_case():
    floor = _min_domain_cases()
    counts: collections.Counter[str] = collections.Counter()
    for case in load_all_cases():
        if is_routing_domain(case.domain):
            counts[case.domain] += 1

    brittle = {d: n for d, n in counts.items() if n < floor}
    assert not brittle, (
        f"routing domains below {floor} cases: {brittle}. At this size "
        f"{DOMAIN_PASS_RATE:.0%} requires a perfect score, so one unlucky case reads "
        "as 'no model can own this job'. Keep at least "
        f"{floor} cases per domain."
    )


def test_every_case_has_a_baseline():
    """A case with no baseline latency scores its own latency, which is free marks.

    Cheap to break by hand: deleting a dataset case and leaving `baseline/` alone
    is harmless, but *adding* one and forgetting `make baseline` is not.

    Skipped outright on a machine that has never run `make baseline` — the JSONs
    are not in git (only `baseline/.gitkeep` is), so on a fresh clone this would
    fail once per case and say nothing about the datasets.
    """
    import pytest

    from bench.baseline import BaselineStore

    case_ids = [c.id for c in load_all_cases()]
    store = BaselineStore()
    missing = store.missing(case_ids)
    if len(missing) == len(case_ids):
        pytest.skip("no baselines recorded — run `make baseline`")

    assert not missing, (
        f"{len(missing)} case(s) have no baseline latency: {sorted(missing)[:10]}. "
        "Run `make baseline`."
    )
