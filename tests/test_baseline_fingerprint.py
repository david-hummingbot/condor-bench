"""A baseline record has to know which case it measured.

Without that, an edited case silently keeps a latency reference for work it no
longer does — and since latency scores `min(1, baseline / test)`, the failure is
quiet and *generous*: `tool_consult_001` went from a blocking `consult` to
`delegate(action="start")`, which returns as soon as the task is handed off, and
kept a reference measured against the blocking call. Every model since has
collected a free 1.0 on it. `t002` went from one `manage_executors` call to
three without its question changing at all.
"""
from __future__ import annotations

from types import SimpleNamespace

from bench.baseline import (
    BASELINE_MISSING,
    BASELINE_OK,
    BASELINE_STALE,
    BASELINE_UNVERIFIED,
    BaselineRecord,
    baseline_status,
    case_fingerprint,
)


def _case(**overrides):
    base = dict(
        id="tool_x_001",
        type="tool",
        question="What is the current mid price of BTC-USDT on Binance?",
        expected_tools=["get_prices"],
        expected_tool_params={"get_prices": {"connector_name": "binance"}},
        expected_no_calls=["manage_bots:deploy"],
        markets={"venue": {"needs": "credentials", "pair": "BTC-USDT"}},
        config={},
        agent_slug=None,
        turns=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_same_case_fingerprints_the_same_twice():
    assert case_fingerprint(_case()) == case_fingerprint(_case())


def test_a_rewritten_question_changes_the_fingerprint():
    """`tool_get_market_data_002` went from 24h of candles to a single mid price."""
    candles = _case(question="Pull the last 24 hours of 1h candles for ETH-USDT.")
    assert case_fingerprint(candles) != case_fingerprint(_case())


def test_a_changed_trajectory_changes_the_fingerprint():
    """The t002 case: same question, one call became three."""
    before = _case(question="Take profit if overbought.", expected_tools=["manage_executors"])
    after = _case(
        question="Take profit if overbought.",
        expected_tools=["list_executors", "stop_executor", "trading_agent_journal_write"],
    )
    assert case_fingerprint(before) != case_fingerprint(after)


def test_expected_tool_order_is_not_a_change():
    """Recall does not care about order, so neither does the amount of work."""
    a = _case(expected_tools=["get_prices", "search_history"])
    b = _case(expected_tools=["search_history", "get_prices"])
    assert case_fingerprint(a) == case_fingerprint(b)


def test_scoring_ground_truth_is_not_part_of_the_fingerprint():
    """Pinning a parameter or a ban does not make the model work harder.

    A fingerprint that fires on every scoring tweak is one people learn to ignore,
    and then it is not there for the change that mattered.
    """
    tighter = _case(
        expected_tool_params={"get_prices": {"connector_name": "binance",
                                             "trading_pairs": ["BTC-USDT"]}},
        expected_no_calls=["manage_bots:deploy", "manage_bots:stop_bot"],
    )
    assert case_fingerprint(tighter) == case_fingerprint(_case())


def test_the_bound_venue_is_not_part_of_the_fingerprint():
    """The same dataset binds to different connectors on different boxes."""
    elsewhere = _case(markets={"venue": {"needs": "credentials", "prefer": ["hyperliquid"]}})
    assert case_fingerprint(elsewhere) == case_fingerprint(_case())


def test_the_pinned_agent_key_is_not_part_of_the_fingerprint():
    """bench overrides it with the model under test, so it decides nothing here."""
    tick = _case(type="tick", config={"execution_mode": "loop", "agent_key": "anthropic:x"})
    other = _case(type="tick", config={"execution_mode": "loop", "agent_key": "ollama:y"})
    assert case_fingerprint(tick) == case_fingerprint(other)
    # But a config value the prompt actually renders does count.
    changed = _case(type="tick", config={"execution_mode": "dry_run", "agent_key": "ollama:y"})
    assert case_fingerprint(changed) != case_fingerprint(tick)


def test_a_tick_scenario_edit_changes_the_fingerprint():
    """A tick's prompt is assembled from these fields, so they are its input."""
    case = _case(type="tick", question="", scenario_name="BTC Grid", core_data={"market": "RSI 48"})
    edited = _case(type="tick", question="", scenario_name="BTC Grid", core_data={"market": "RSI 79"})
    assert case_fingerprint(case) != case_fingerprint(edited)


class _Store:
    def __init__(self, record):
        self._record = record

    def load(self, case_id):
        return self._record


def test_status_tells_the_four_situations_apart():
    case = _case()
    assert baseline_status(case, None) == BASELINE_MISSING

    legacy = BaselineRecord(case_id=case.id, model="m", latency_s=10.0)
    assert baseline_status(case, legacy) == BASELINE_UNVERIFIED, (
        "a record written before fingerprints is unknown, not wrong — calling it "
        "stale would flag all 73 of them and teach everyone to ignore the report"
    )

    fresh = BaselineRecord(
        case_id=case.id, model="m", latency_s=10.0, fingerprint=case_fingerprint(case)
    )
    assert baseline_status(case, fresh) == BASELINE_OK

    rewritten = _case(question="Something else entirely.")
    assert baseline_status(rewritten, fresh) == BASELINE_STALE


def test_classify_covers_every_case_exactly_once():
    from bench.baseline import classify_baselines

    cases = [_case(id="a"), _case(id="b", question="different")]
    record = BaselineRecord(
        case_id="a", model="m", latency_s=1.0, fingerprint=case_fingerprint(cases[0])
    )
    found = classify_baselines(cases, _Store(record))
    assert sum(len(v) for v in found.values()) == 2
    assert found[BASELINE_OK] == ["a"]
    assert found[BASELINE_STALE] == ["b"]


def test_a_recorded_baseline_round_trips_its_fingerprint(tmp_path):
    from bench.baseline import BaselineStore

    store = BaselineStore(tmp_path)
    case = _case()
    store.save(
        BaselineRecord(
            case_id=case.id, model="m", latency_s=12.5, fingerprint=case_fingerprint(case)
        )
    )
    assert baseline_status(case, store.load(case.id)) == BASELINE_OK


def test_every_real_case_fingerprints_without_raising():
    """Four case types, all read through getattr — none may blow up on a missing field."""
    from bench.dataset import load_all_cases

    cases = load_all_cases()
    prints = {c.id: case_fingerprint(c) for c in cases}
    assert len(set(prints.values())) == len(prints), (
        "two cases fingerprint identically — the payload is missing something that "
        f"distinguishes them: {prints}"
    )


# ── The baseline has to run the case a scored run would run ───────────────────
def test_the_fingerprint_ignores_which_venue_a_case_bound_to():
    """Binding is per-box, so a digest over the bound copy is useless.

    `generate_baselines` runs the *bound* case and fingerprints the *dataset* one
    for this reason: the same case takes binance on one machine and hyperliquid on
    another, and a fingerprint over the substituted text would report every
    baseline as stale on the next box.
    """
    template = _case(question="Open a position on `{venue.connector}` for {venue.pair}.")
    here = _case(question="Open a position on `binance` for ETH-USDT.")
    there = _case(question="Open a position on `hyperliquid_perpetual` for ETH-USD.")
    assert case_fingerprint(here) != case_fingerprint(template)
    assert case_fingerprint(here) != case_fingerprint(there)


def test_the_baseline_path_binds_markets_before_measuring():
    """The bug this guards: 17 cases were measured against literal placeholders.

    `generate_baselines` called `run_case` on the raw dataset case, so
    `tool_create_position_executor_002` asked the reference model to open a
    position "on connector `{venue.connector}` for {venue.pair}". It declined, and
    4-6s of a model reading an unanswerable question became the reference for a
    case that opens a real position — the rewarding direction, since latency is
    min(1, baseline / test).
    """
    import asyncio
    import inspect

    import bench.baseline as baseline_mod

    source = inspect.getsource(baseline_mod.generate_baselines)
    assert "resolve_cases" in source, (
        "generate_baselines no longer resolves declared markets — templated cases "
        "would again be measured against literal {venue.connector} placeholders"
    )

    seen: dict[str, str] = {}

    async def _fake_run_case(case, model):
        seen[case.id] = case.question
        return SimpleNamespace(case_id=case.id, latency_s=1.0, tool_calls=[], tool_responses=[])

    async def _fake_resolve(cases):
        bound = [
            SimpleNamespace(**{**c.__dict__, "question": c.question.replace(
                "{venue.connector}", "binance").replace("{venue.pair}", "ETH-USDT")})
            for c in cases
        ]
        return bound, {c.id: SimpleNamespace(ok=True, reason=lambda: "") for c in cases}

    class _Store:
        def __init__(self):
            self.saved = []

        def exists(self, case_id):
            return False

        def load(self, case_id):
            return None

        def save(self, record):
            self.saved.append(record)

    case = _case(question="Open a position on `{venue.connector}` for {venue.pair}.")
    store = _Store()
    original_run, original_resolve = baseline_mod.run_case, baseline_mod.resolve_cases
    baseline_mod.run_case, baseline_mod.resolve_cases = _fake_run_case, _fake_resolve
    try:
        asyncio.run(baseline_mod.generate_baselines([case], store, model="m"))
    finally:
        baseline_mod.run_case, baseline_mod.resolve_cases = original_run, original_resolve

    assert "{venue" not in seen[case.id], (
        f"the model was handed an unresolved placeholder: {seen[case.id]!r}"
    )
    assert seen[case.id] == "Open a position on `binance` for ETH-USDT."
    # …but the record fingerprints the dataset case, not what it bound to.
    (record,) = store.saved
    assert record.fingerprint == case_fingerprint(case)
