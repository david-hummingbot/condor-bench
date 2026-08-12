"""Probe journals: the fixture every tick journal write needed and never had."""

from __future__ import annotations

from types import SimpleNamespace

from bench import probe_journal
from bench.probe_journal import ensure_probe_journal, probe_strategy_dir


def _case(**over):
    base = dict(id="t003", type="tick", agent_slug="bench_tick_risk_blocked")
    base.update(over)
    return SimpleNamespace(**base)


def _checkout(monkeypatch, tmp_path):
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr(probe_journal, "condor_path", lambda: tmp_path)
    return tmp_path


def test_the_dir_matches_what_condor_resolves(monkeypatch, tmp_path):
    """condor splits `{slug}.{strategy}_{n}` to agents/<slug>/strategies/<strategy>.

    `bench.client.tick_agent_id` builds the id as `f"{slug}.{case.id}_1"`, so the
    strategy directory is named for the case id. Get this wrong and the write
    still answers "no journal available for this agent".
    """
    repo = _checkout(monkeypatch, tmp_path)
    assert probe_strategy_dir(_case()) == (
        repo / "agents" / "bench_tick_risk_blocked" / "strategies" / "t003"
    )


def test_ensure_creates_the_strategy_dir(monkeypatch, tmp_path):
    """`resolve_agent_dirs` returns (None, None) unless this directory exists.

    It does not need the session dir — condor's JournalManager makes that and
    journal.md itself once the strategy dir resolves.
    """
    _checkout(monkeypatch, tmp_path)
    case = _case()
    note = ensure_probe_journal(case)
    assert note and "t003" in note
    assert probe_strategy_dir(case).is_dir()


def test_ensure_is_idempotent_and_keeps_existing_entries(monkeypatch, tmp_path):
    """A second run must not wipe the journal a case already wrote.

    Clearing them is `scripts/clean_probe_journals.py`'s job, on demand.
    """
    _checkout(monkeypatch, tmp_path)
    case = _case()
    ensure_probe_journal(case)
    journal = probe_strategy_dir(case) / "sessions" / "session_1" / "journal.md"
    journal.parent.mkdir(parents=True)
    journal.write_text("# Journal\n- tick#1 held\n")

    assert ensure_probe_journal(case) is None
    assert "tick#1 held" in journal.read_text()


def test_only_bench_probe_slugs_are_touched(monkeypatch, tmp_path):
    """A real agent's journal is its memory. bench never provisions into one."""
    _checkout(monkeypatch, tmp_path)
    for slug in ("directional_trader", "", None):
        case = _case(agent_slug=slug)
        assert probe_strategy_dir(case) is None
        assert ensure_probe_journal(case) is None
    assert not list((tmp_path / "agents").iterdir())


def test_non_tick_cases_and_a_missing_checkout_are_no_ops(monkeypatch, tmp_path):
    """Nothing to provision is not an error — the wiring pre-flight owns that report."""
    _checkout(monkeypatch, tmp_path)
    assert ensure_probe_journal(_case(type="consult")) is None

    monkeypatch.setattr(probe_journal, "condor_path", lambda: None)
    assert ensure_probe_journal(_case()) is None
