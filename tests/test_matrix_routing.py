"""The matrix and the router must not turn harness noise into a recommendation.

These tests pin the decisions that make a routing output trustworthy:

* an infra failure or a mis-scoped case is *excluded*, not scored 0 — otherwise a
  staging outage reads as "the model got worse"
* thin evidence is reported as thin, not as a failure
* a passing model is never rejected for using more tokens
* a destructive case below the floor blocks the recommendation outright
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.matrix import build_matrix
from bench.routing import recommend

# Domains these fixtures use must be domains the datasets actually produce: the
# router classifies anything else as *stale* (a domain only older results carry),
# which is correct behaviour and would silently stop these tests exercising their
# subject. Resolved from the dataset so a roster change moves the tests with it.
def _routing_domains() -> list[str]:
    from bench.dataset import is_routing_domain, load_all_cases

    return sorted({c.domain for c in load_all_cases() if is_routing_domain(c.domain)})


_DOMAINS = _routing_domains()
DOMAIN_A = "general_consult"
DOMAIN_B = next(d for d in _DOMAINS if d != DOMAIN_A)
DOMAIN_C = next(d for d in _DOMAINS if d not in (DOMAIN_A, DOMAIN_B))

MODELS_JSON = {
    "models": [
        {"key": "ollama:small:3b", "params_b": 3, "provider": "local"},
        {"key": "ollama:mid:14b", "params_b": 14, "provider": "local"},
        {"key": "cloud:big", "params_b": None, "provider": "cloud"},
    ]
}


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(MODELS_JSON))
    return path


def write_run(
    results: Path,
    model: str,
    cases: list[dict],
    *,
    timestamp: str = "2026-08-04T00:00:00Z",
) -> None:
    safe = model.replace(":", "_").replace("/", "_")
    run_dir = results / f"run_{safe}_{timestamp[:10]}"
    (run_dir / "cases").mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"model": model, "timestamp": timestamp})
    )
    for case in cases:
        (run_dir / "cases" / f"{case['case_id']}.json").write_text(json.dumps(case))


def case(
    case_id: str,
    domain: str,
    composite: float,
    *,
    risk_level: str = "read_only",
    error: str | None = None,
    harness_artifact: str | None = None,
    total_tokens: int | None = None,
    expected_tools: list[str] | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "domain": domain,
        "composite": composite,
        "risk_level": risk_level,
        "error": error,
        "harness_artifact": harness_artifact,
        "latency_s": 2.0,
        "expected_tools": expected_tools or [],
        "usage": {"total_tokens": total_tokens} if total_tokens else {},
    }


def _four(domain: str, composite: float, prefix: str, **kw) -> list[dict]:
    return [case(f"{prefix}{i}", domain, composite, **kw) for i in range(4)]


def test_infra_failures_are_excluded_not_scored_zero(tmp_path, registry):
    """A staging outage must not read as the model getting worse."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:mid:14b",
        _four("general_consult", 0.9, "ok")
        + [case("broken", "general_consult", 0.0, error="infra: connection refused")],
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    cell = matrix["domains"]["general_consult"]["ollama:mid:14b"]

    assert cell["cases"] == 5
    assert cell["scored"] == 4
    assert cell["excluded"] == 1
    assert cell["pass_rate"] == 1.0, (
        "the errored case dragged the pass rate down — an infra failure is not "
        "evidence about the model"
    )


def test_harness_artifacts_are_excluded(tmp_path, registry):
    """A case that ran chat-scoped when it needed --agent-slug is not a model result."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:mid:14b",
        _four(DOMAIN_B, 0.9, "ok")
        + [
            case(
                "misscoped",
                DOMAIN_B,
                0.1,
                harness_artifact="assistant prompt fell back (fallback:vendored)",
            )
        ],
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    cell = matrix["domains"][DOMAIN_B]["ollama:mid:14b"]
    assert cell["excluded"] == 1
    assert cell["pass_rate"] == 1.0
    assert cell["excluded_reasons"], "the exclusion must be visible, not silent"


def test_smallest_passing_model_wins(tmp_path, registry):
    results = tmp_path / "results"
    write_run(results, "ollama:small:3b", _four("general_consult", 0.95, "s"))
    write_run(results, "ollama:mid:14b", _four("general_consult", 0.99, "m"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    rec = routing["recommendations"]["general_consult"]
    assert rec["model"] == "ollama:small:3b", (
        "a higher-scoring larger model won — routing wants the smallest that passes, "
        "not the best"
    )
    assert rec["params_b"] == 3


def test_local_model_beats_cloud_even_when_cloud_scores_higher(tmp_path, registry):
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", _four("general_consult", 0.85, "m"))
    write_run(results, "cloud:big", _four("general_consult", 1.0, "c"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    rec = routing["recommendations"]["general_consult"]
    assert rec["model"] == "ollama:mid:14b"
    assert rec["cloud_fallback"] is False
    assert rec["no_local_passed"] is False


def test_cloud_fallback_is_flagged_when_no_local_passes(tmp_path, registry):
    results = tmp_path / "results"
    write_run(results, "ollama:small:3b", _four("tick_execution", 0.4, "s"))
    write_run(results, "ollama:mid:14b", _four("tick_execution", 0.5, "m"))
    write_run(results, "cloud:big", _four("tick_execution", 0.95, "c"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    rec = routing["recommendations"]["tick_execution"]
    assert rec["model"] == "cloud:big"
    assert rec["cloud_fallback"] is True
    assert rec["no_local_passed"] is True, (
        "a cloud-only domain is a finding worth surfacing, not just a value"
    )


def test_destructive_failure_blocks_a_recommendation(tmp_path, registry):
    """Passing on average is not enough when the irreversible case is the failure."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:small:3b",
        _four(DOMAIN_B, 0.95, "s")
        + [case("danger", DOMAIN_B, 0.4, risk_level="destructive")],
    )
    write_run(results, "ollama:mid:14b", _four(DOMAIN_B, 0.9, "m")
              + [case("danger", DOMAIN_B, 0.9, risk_level="destructive")])

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert matrix["domains"][DOMAIN_B]["ollama:small:3b"]["destructive_failures"]

    routing = recommend(matrix, models_path=registry)
    rec = routing["recommendations"][DOMAIN_B]
    assert rec["model"] == "ollama:mid:14b", (
        "the 3B model was recommended despite botching a destructive case"
    )


def test_thin_evidence_is_reported_as_thin(tmp_path, registry):
    """One passing case is not a verdict, and must not be reported as a failure."""
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", [case("only", DOMAIN_C, 1.0)])

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        min_cases=3,
        models_path=registry,
    )
    assert DOMAIN_C not in routing["recommendations"]
    gap = routing["unmet_domains"][DOMAIN_C]
    assert gap["insufficient_evidence"] is True
    assert "scored case" in gap["reason"]


def test_token_cost_never_rejects_a_passing_model(tmp_path, registry):
    """The 3B model uses 4× the tokens and is still the recommendation."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:small:3b",
        _four("general_consult", 0.9, "s", total_tokens=40_000),
    )
    write_run(
        results,
        "ollama:mid:14b",
        _four("general_consult", 0.9, "m", total_tokens=10_000),
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    for prefer in (False, True):
        routing = recommend(
            matrix, prefer_lower_tokens=prefer, models_path=registry
        )
        assert routing["recommendations"]["general_consult"]["model"] == "ollama:small:3b", (
            f"prefer_lower_tokens={prefer} promoted a larger model on cost grounds"
        )


def test_unmeasured_tokens_are_not_zero(tmp_path, registry):
    """A model that reported no usage must not look like the cheapest option."""
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", _four("general_consult", 0.9, "m"))

    matrix = build_matrix(results_dir=results, models_path=registry)
    cell = matrix["domains"]["general_consult"]["ollama:mid:14b"]
    assert cell["avg_total_tokens"] is None
    assert cell["avg_cost_usd"] is None


def test_newest_run_owns_a_cell(tmp_path, registry):
    """Re-running a domain replaces it, never blends with the older attempt."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:mid:14b",
        _four("general_consult", 0.2, "old"),
        timestamp="2026-07-01T00:00:00Z",
    )
    write_run(
        results,
        "ollama:mid:14b",
        _four("general_consult", 0.95, "new"),
        timestamp="2026-08-04T00:00:00Z",
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    cell = matrix["domains"]["general_consult"]["ollama:mid:14b"]
    assert cell["scored"] == 4, "runs were blended instead of taking the newest"
    assert cell["pass_rate"] == 1.0


def test_domain_by_domain_sweeps_accumulate(tmp_path, registry):
    """`sweep -d routine_builder` must not erase the model's other domains.

    Resolution is per (model, cell), not per model: the newest run wins each cell
    it covers, and older runs still fill the cells nothing newer touched.
    """
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:mid:14b",
        _four("general_consult", 0.95, "gc"),
        timestamp="2026-07-01T00:00:00Z",
    )
    write_run(
        results,
        "ollama:mid:14b",
        _four(DOMAIN_B, 0.9, "rb"),
        timestamp="2026-08-04T00:00:00Z",
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert matrix["domains"]["general_consult"]["ollama:mid:14b"]["scored"] == 4, (
        "the older general_consult run was dropped when a newer domain-only run landed"
    )
    assert matrix["domains"][DOMAIN_B]["ollama:mid:14b"]["scored"] == 4

    routing = recommend(matrix, models_path=registry)
    assert set(routing["recommendations"]) == {DOMAIN_A, DOMAIN_B}


def test_unregistered_model_is_reported_not_silently_dropped(tmp_path, registry):
    """A benchmarked model with no params_b can't be ranked — say so."""
    results = tmp_path / "results"
    write_run(results, "ollama:mystery:99b", _four("general_consult", 0.99, "x"))

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert matrix["models"]["ollama:mystery:99b"]["in_registry"] is False

    routing = recommend(matrix, models_path=registry)
    assert "ollama:mystery:99b" in routing["unranked_models"]
    assert routing["unranked_note"]
    assert "general_consult" not in routing["recommendations"]


def test_tool_domains_are_not_routing_targets(tmp_path, registry):
    """Layer 2 buckets have no Condor config key and must not become recommendations."""
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", _four("tool:market_data", 0.99, "t"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    assert not any(d.startswith("tool:") for d in routing["recommendations"])
    assert not any(d.startswith("tool:") for d in routing["unmet_domains"])


def test_per_tool_verdicts_use_expected_tools(tmp_path, registry):
    """A tool's row is built from the tool the case was *supposed* to use."""
    results = tmp_path / "results"
    write_run(
        results,
        "ollama:mid:14b",
        [
            case("t1", "tool:market_data", 0.95, expected_tools=["get_market_data"]),
            case("t2", "tool:routines", 0.2, expected_tools=["manage_routines"]),
        ],
    )

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert matrix["tools"]["get_market_data"]["ollama:mid:14b"]["pass_rate"] == 1.0
    assert matrix["tools"]["manage_routines"]["ollama:mid:14b"]["pass_rate"] == 0.0

    routing = recommend(matrix, models_path=registry)
    assert "get_market_data" in routing["tool_gaps"]["smallest_passing"]
    assert "manage_routines" in routing["tool_gaps"]["unhandled"]


def test_domain_is_backfilled_for_runs_saved_before_the_field_existed(tmp_path, registry):
    """Otherwise every historical run collapses into one meaningless bucket."""
    from bench.dataset import load_consult_cases

    known = load_consult_cases()[0]
    results = tmp_path / "results"
    record = case(known.id, "", 0.9)
    del record["domain"]
    write_run(results, "ollama:mid:14b", [record])

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert known.domain in matrix["domains"]
    assert "unclassified" not in matrix["domains"]


def test_unclassified_is_not_routed(tmp_path, registry):
    """A case whose dataset entry is gone can't be attributed to a routing target."""
    results = tmp_path / "results"
    record = case("case_that_no_longer_exists", "", 0.99)
    del record["domain"]
    write_run(results, "ollama:mid:14b", [record] * 1 + [
        {**case(f"gone{i}", "", 0.99), "domain": None} for i in range(3)
    ])

    matrix = build_matrix(results_dir=results, models_path=registry)
    assert "unclassified" in matrix["domains"], "the cases should still be visible"

    routing = recommend(matrix, models_path=registry)
    assert "unclassified" not in routing["recommendations"]
    assert "unclassified" not in routing["unmet_domains"]


def test_shared_config_key_conflict_is_surfaced(tmp_path, registry, monkeypatch):
    """Two domains, one config key, different winners — don't let a dict decide.

    The key map is monkeypatched rather than relying on two real domains happening
    to share a key: condor's agent roster changes (``routine_builder`` was an agent
    and is now a shared skill), and a test that silently stops exercising its
    subject when the roster shifts is worse than no test.
    """
    import bench.routing as routing_mod

    key = "agents/shared_probe/agent_key"
    monkeypatch.setattr(
        routing_mod,
        "CONDOR_CONFIG_KEYS",
        {DOMAIN_A: key, DOMAIN_B: key},
    )

    results = tmp_path / "results"
    write_run(
        results,
        "ollama:small:3b",
        _four(DOMAIN_A, 0.95, "a3") + _four(DOMAIN_B, 0.3, "b3"),
    )
    write_run(
        results,
        "ollama:mid:14b",
        _four(DOMAIN_A, 0.95, "a14") + _four(DOMAIN_B, 0.95, "b14"),
    )

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    assert routing["recommendations"][DOMAIN_A]["model"] == "ollama:small:3b"
    assert routing["recommendations"][DOMAIN_B]["model"] == "ollama:mid:14b"
    assert routing["condor_config_snippet"][key] == "ollama:mid:14b", (
        "the shared key took the smaller model, which one of its domains fails"
    )
    assert key in routing["config_conflicts"]


def test_config_keys_name_agents_condor_actually_ships():
    """A recommendation is only useful if its config key exists upstream.

    condor deleted ``routine_builder`` and ``agent_builder`` as agents; a key
    pointing at ``agents/routine_builder/agent_key`` would be written into a config
    nothing reads, and the recommendation would look applied while changing
    nothing. Skips without a condor checkout.
    """
    from bench.routing import CONDOR_CONFIG_KEYS
    from config import condor_path

    repo = condor_path()
    if repo is None or not (repo / "agents").is_dir():
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    shipped = {p.name for p in (repo / "agents").iterdir() if p.is_dir()}
    missing = {}
    for domain, key in CONDOR_CONFIG_KEYS.items():
        if not key.startswith("agents/"):
            continue
        slug = key.split("/")[1]
        # `_defaults` is condor's fallback config, not an agent — it is a valid
        # target and legitimately has no AGENT.md.
        if slug not in shipped:
            missing[domain] = key

    assert not missing, (
        f"routing would write config keys for agents condor does not ship: {missing}. "
        f"condor's roster is {sorted(shipped)}. Update CONDOR_CONFIG_KEYS in "
        "bench/routing.py."
    )


def test_domain_deleted_from_the_datasets_is_stale_not_unmet(tmp_path, registry):
    """A domain only older results carry can't be routed — and isn't a gap to close.

    condor deletes agents (`routine_builder` went away), so results outlive the
    domains that produced them. Calling that "unmet" reads as "benchmark harder";
    the honest statement is that there is nothing left to route.
    """
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", _four("a_domain_no_dataset_produces", 0.2, "old"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    assert "a_domain_no_dataset_produces" not in routing["unmet_domains"]
    assert "a_domain_no_dataset_produces" in routing["stale_domains"]
    assert routing["stale_domains"]["a_domain_no_dataset_produces"]["models_with_results"] == [
        "ollama:mid:14b"
    ]


def test_a_live_domain_is_still_reported_as_unmet(tmp_path, registry):
    """The staleness check must not swallow a real gap."""
    from bench.dataset import load_all_cases

    live = next(c.domain for c in load_all_cases() if c.domain == "general_consult")
    results = tmp_path / "results"
    write_run(results, "ollama:mid:14b", _four(live, 0.2, "bad"))

    routing = recommend(
        build_matrix(results_dir=results, models_path=registry),
        models_path=registry,
    )
    assert live in routing["unmet_domains"]
    assert live not in routing["stale_domains"]
