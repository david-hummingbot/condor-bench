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

from bench.dataset import filter_cases, is_routing_domain, load_all_cases
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


def test_core_subset_clears_the_same_floors():
    """``--tags core`` is for iteration, not a licence to reprint thin verdicts."""
    core = filter_cases(load_all_cases(), tags=["core"])
    assert core, "no cases tagged core — the iteration sweep would run nothing"

    floor = _min_domain_cases()
    tool_counts: collections.Counter[str] = collections.Counter()
    domain_counts: collections.Counter[str] = collections.Counter()
    for case in core:
        for tool in case.expected_tools or []:
            tool_counts[tool] += 1
        if is_routing_domain(case.domain):
            domain_counts[case.domain] += 1

    # Every tool the full library measures must still be measured in core. A tool
    # that only appears outside core would look handled on a full run and thin on
    # the cheap one, which is the failure this tag exists to prevent.
    library_tools = {
        tool
        for case in load_all_cases()
        for tool in (case.expected_tools or [])
    }
    thin = {
        t: tool_counts[t]
        for t in library_tools
        if tool_counts[t] < MIN_TOOL_CASES
    }
    assert not thin, (
        f"core subset tools below MIN_TOOL_CASES={MIN_TOOL_CASES}: {thin}. "
        "Tag another case that calls the tool, or drop it from the full library."
    )

    library_domains = {
        c.domain for c in load_all_cases() if is_routing_domain(c.domain)
    }
    brittle = {
        d: domain_counts[d]
        for d in library_domains
        if domain_counts[d] < floor
    }
    assert not brittle, (
        f"core subset routing domains below {floor} cases: {brittle}"
    )


def test_every_case_has_a_baseline():
    """A case with no baseline latency scores its own latency, which is free marks.

    Cheap to break by hand: deleting a dataset case and leaving `baseline/` alone
    is harmless, but *adding* one and forgetting `make baseline` is not.

    Skipped outright when the store is empty — a checkout that has not run
    `make baseline`, or one whose `baseline/` did not come down with the repo.
    Otherwise this would fail once per case and say nothing about the datasets.
    (The JSONs *are* tracked, so teams share one reference; see .gitignore.)
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


# ── Task bank (Phase 4) ────────────────────────────────────────────────────────
def test_every_task_block_is_valid():
    from bench.dataset import task_problems

    problems = [p for c in load_all_cases(include_holdout=True) for p in task_problems(c)]
    assert not problems, "\n".join(problems)


def test_task_ids_are_unique_across_every_split():
    ids = [c.task["task_id"] for c in load_all_cases(include_holdout=True)]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate task_ids: {dupes}"


def test_every_trading_category_clears_the_floor_or_is_a_stated_gap():
    """Ten per category outside holdout, or the shortfall is in the manifest's gaps."""
    import json

    from bench.dataset import CATEGORY_FLOOR, TRADING_CATEGORIES, case_task_category
    from config import DATASETS_DIR

    counts = collections.Counter(case_task_category(c) for c in load_all_cases())
    under = {cat: counts[cat] for cat in TRADING_CATEGORIES if counts[cat] < CATEGORY_FLOOR}
    gaps = json.loads((DATASETS_DIR / "manifest.json").read_text())["gaps"]
    unexplained = {
        cat: n for cat, n in under.items() if not any(g["area"].startswith(cat) for g in gaps)
    }
    assert not unexplained, (
        f"categories under {CATEGORY_FLOOR} with no gap entry: {unexplained}. Add cases, "
        "or record the gap in scripts/build_task_manifest.py"
    )


def test_the_manifest_is_current():
    import subprocess
    import sys

    from config import ROOT

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_task_manifest.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_holdout_is_invisible_to_default_runs():
    from bench.dataset import case_prompt_map, load_holdout_cases

    holdout = {c.id for c in load_holdout_cases()}
    assert holdout, "the holdout split is empty"
    default = {c.id for c in load_all_cases()}
    core = {c.id for c in filter_cases(load_all_cases(), tags=["core"])}
    assert not holdout & default, "holdout leaked into the default library"
    assert not holdout & core, "holdout leaked into --tags core"
    assert not holdout & set(case_prompt_map()), "holdout leaked into the dashboard's prompts"
    assert all(c.task["split"] == "holdout" for c in load_holdout_cases())
    assert not any(c.task["split"] == "holdout" for c in load_all_cases()), (
        "a case outside holdout.jsonl claims the holdout split"
    )


def test_mock_only_cases_declare_what_they_need_the_mock_for():
    from mock_mcp.server import FAULT_KINDS

    for case in load_all_cases(include_holdout=True):
        if case.lanes == ["mock"]:
            assert case.mock_faults, f"{case.id} is mock-only but arms no fault"
        for fault in case.mock_faults:
            assert fault.get("kind") in FAULT_KINDS, f"{case.id}: unknown fault {fault}"


def test_split_filter_selects_by_task_split():
    regression = filter_cases(load_all_cases(), splits=["regression"])
    assert regression and all(c.task["split"] == "regression" for c in regression)
