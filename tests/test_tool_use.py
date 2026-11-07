"""Per-tool evidence, call by call (bench/tool_use.py)."""

from __future__ import annotations

from bench.tool_use import ToolUseCell, case_outcomes


def _resp(tool, ok=True, call_id=None):
    row = {"tool": tool, "score": 1.0 if ok else 0.0, "error": None if ok else "HTTP 500"}
    if call_id:
        row["tool_call_id"] = call_id
    return row


def _case(expected, calls, responses, params=None, **kw):
    return {
        "case_id": kw.pop("case_id", "c"),
        "expected_tools": expected,
        "tool_calls": calls,
        "tool_param_detail": params or {},
        "live_validity_detail": {"responses": responses},
        **kw,
    }


def _by_tool(case):
    return {o.tool: o for o in case_outcomes(case)}


def test_first_try_needs_the_pick_the_arguments_and_the_first_call():
    ok = _by_tool(_case(["get_prices"], ["get_prices"], [_resp("get_prices")],
                        {"get_prices": {"called": True, "matched": ["trading_pairs"], "mismatched": {}}}))
    assert ok["get_prices"].first_try_correct is True

    wrong_args = _by_tool(_case(["get_prices"], ["get_prices"], [_resp("get_prices")],
                                {"get_prices": {"called": True, "matched": [],
                                                "mismatched": {"connector_name": {}}}}))
    assert wrong_args["get_prices"].first_try_correct is False
    assert wrong_args["get_prices"].mismatched == ["connector_name"]

    not_picked = _by_tool(_case(["get_prices"], ["get_market_data"], [_resp("get_market_data")]))
    assert not_picked["get_prices"].first_try_correct is False
    assert not_picked["get_market_data"].first_try_correct is None, "not expected: no verdict"


def test_a_call_with_no_recorded_response_failed():
    """pydantic-ai sends a failed call back as a retry prompt and records nothing.
    The real case: three get_market_data calls, none answered."""
    o = _by_tool(_case(["get_market_data"], ["manage_skill", "get_market_data", "get_market_data"],
                       [_resp("manage_skill")]))["get_market_data"]
    assert o.responses == [False, False]
    assert o.first_try_correct is False


def test_older_records_put_unanswered_calls_first():
    """Fail-then-fix: two calls, one recorded success — the first try is not credited."""
    o = _by_tool(_case(["get_prices"], ["get_prices", "get_prices"], [_resp("get_prices")]))["get_prices"]
    assert o.responses == [False, True]
    assert o.first_try_correct is False and o.repaired is True


def test_records_with_call_ids_are_matched_exactly():
    case = _case(
        ["get_prices"], ["get_prices", "get_prices"],
        [_resp("get_prices", True, "a")],
        tool_call_details=[
            {"tool": "get_prices", "tool_call_id": "a", "origin": "mcp"},
            {"tool": "get_prices", "tool_call_id": "b", "origin": "mcp"},
        ],
    )
    o = _by_tool(case)["get_prices"]
    assert o.responses == [True, False], "the first call worked; the second never returned"
    assert o.first_try_correct is True


def test_acp_namespaced_names_fold_into_the_tool():
    o = _by_tool(_case(["manage_routines"], ["mcp__condor__manage_routines"],
                       [_resp("mcp__condor__manage_routines")]))
    assert set(o) == {"manage_routines"} and o["manage_routines"].first_try_correct


def test_the_cell_counts_cases_for_the_headline_and_calls_for_execution():
    cell = ToolUseCell()
    for case in (
        _case(["get_prices"], ["get_prices"], [_resp("get_prices")]),
        _case(["get_prices"], ["get_prices", "get_prices"], [_resp("get_prices", False), _resp("get_prices")]),
        # Not asked for, still evidence that its calls work.
        _case(["get_portfolio_overview"], ["get_portfolio_overview", "get_prices"],
              [_resp("get_portfolio_overview"), _resp("get_prices")]),
    ):
        for o in case_outcomes(case):
            if o.tool == "get_prices":
                cell.add(o)
    out = cell.as_dict(0.65)
    assert (out["expected"], out["first_try_rate"]) == (2, 0.5)
    assert out["selection_rate"] == 1.0
    assert (out["calls"], out["call_success_rate"]) == (4, 0.75)
    assert out["repair_rate"] == 1.0
    assert out["unprompted_calls"] == 1
    assert out["first_try_ci"] is not None


def test_the_matrix_carries_a_tool_use_axis(tmp_path, monkeypatch):
    import json

    import bench.matrix as matrix

    run = tmp_path / "r1_m"
    (run / "cases").mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "model": "ollama:m:3b", "timestamp": "2026-11-07T00:00:00Z",
        "manifest": {"env_mode": "live"}, "source": "cli",
    }))
    good = _case(["get_prices"], ["get_prices"], [_resp("get_prices")], case_id="a",
                 composite=0.9, passed=True, domain="tool:market_data")
    infra = _case(["get_prices"], ["get_prices"], [_resp("get_prices", False)], case_id="b",
                  composite=0.0, error="infra: rate limited", domain="tool:market_data")
    for case in (good, infra):
        (run / "cases" / f"{case['case_id']}.json").write_text(json.dumps(case))
    monkeypatch.setattr(matrix, "_dataset_expected_tools", lambda: {})
    monkeypatch.setattr(matrix, "_dataset_domains", lambda: {})

    data = matrix.build_matrix(results_dir=tmp_path, models_path=tmp_path / "none.json")
    cell = data["tool_use"]["get_prices"]["ollama:m:3b"]
    assert cell["expected"] == 1, "the infra row is not evidence"
    assert cell["first_try_rate"] == 1.0


def test_an_empty_answer_is_a_call_that_worked():
    """Live validity scores an empty result 0 (``nonempty`` defaults on); an empty
    executor list on an idle account is still a call the API answered."""
    empty = {"tool": "list_executors", "score": 0.0, "error": None, "empty": True}
    o = _by_tool(_case(["list_executors"], ["list_executors"], [empty]))["list_executors"]
    assert o.responses == [True] and o.first_try_correct is True


def _write_run(root, name, stamp, cases):
    import json

    run = root / name
    (run / "cases").mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "model": "ollama:m:3b", "timestamp": stamp,
        "manifest": {"env_mode": "live"}, "source": "cli",
    }))
    for case in cases:
        (run / "cases" / f"{case['case_id']}.json").write_text(json.dumps(case))


def test_an_unprompted_call_in_a_newer_run_does_not_take_the_cell(tmp_path, monkeypatch):
    import bench.matrix as matrix

    _write_run(tmp_path, "r1_tools", "2026-11-06T00:00:00Z", [
        _case(["get_prices"], ["get_prices"], [_resp("get_prices")], case_id=f"t{i}",
              composite=0.9, passed=True, domain="tool:market_data")
        for i in range(3)
    ])
    _write_run(tmp_path, "r2_tick", "2026-11-07T00:00:00Z", [
        _case(["get_portfolio_overview"], ["get_portfolio_overview", "get_prices"],
              [_resp("get_portfolio_overview"), _resp("get_prices")], case_id="k",
              composite=0.9, passed=True, domain="tick:x"),
    ])
    monkeypatch.setattr(matrix, "_dataset_expected_tools", lambda: {})
    monkeypatch.setattr(matrix, "_dataset_domains", lambda: {})

    data = matrix.build_matrix(results_dir=tmp_path, models_path=tmp_path / "none.json")
    cell = data["tool_use"]["get_prices"]["ollama:m:3b"]
    assert cell["run_dir"].endswith("r1_tools"), "the run that asked for the tool owns it"
    assert (cell["expected"], cell["first_try_rate"]) == (3, 1.0)
    # A tool only ever called unprompted still gets a cell from that run.
    assert data["tool_use"]["get_portfolio_overview"]["ollama:m:3b"]["expected"] == 1
