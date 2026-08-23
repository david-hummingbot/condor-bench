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


# ── The agent_id itself ────────────────────────────────────────────────────────
# `resolve_agent_dirs` starts at `agent_id.rfind("_")` and returns (None, None)
# on -1, before touching the disk. A hyphenated name therefore fails for every
# model in every run — which is what four journal cases shipped with.

import json  # noqa: E402

import pytest  # noqa: E402

from bench.dataset import load_all_cases  # noqa: E402
from bench.probe_journal import (  # noqa: E402
    AGENT_ID_TOKEN,
    bind_agent_id,
    needs_probe_journal,
    probe_agent_id,
    should_seed,
)


def _resolvable(agent_id: str) -> bool:
    """The shape `resolve_agent_dirs` can parse, asserted without a checkout."""
    if "." not in agent_id or "_" not in agent_id:
        return False
    _, _, suffix = agent_id.rpartition("_")
    return suffix.isdigit() or (suffix.startswith("e") and suffix[1:].isdigit())


def test_probe_agent_id_is_the_format_condor_parses():
    assert probe_agent_id(_case()) == "bench_tick_risk_blocked.t003_1"
    assert _resolvable(probe_agent_id(_case()))


def test_the_hyphenated_form_that_shipped_is_not_resolvable():
    """Pins the actual bug: this id can never resolve, whatever the model does."""
    assert not _resolvable("bench-journal-probe")


def test_tick_agent_id_defers_to_one_definition():
    """Two definitions of the format is how the non-tick cases drifted."""
    from bench.client import tick_agent_id

    case = _case()
    assert tick_agent_id(case) == probe_agent_id(case)


def test_bind_agent_id_replaces_the_placeholder_everywhere():
    from bench.dataset import ToolCase

    case = ToolCase(
        id="tool_journal_read_001",
        tool="trading_agent_journal_read",
        question=f"Read back the journal for agent {AGENT_ID_TOKEN}.",
        expected_tool_params={
            "trading_agent_journal_read": {"agent_id": AGENT_ID_TOKEN}
        },
        agent_slug="bench_journal_probe",
    )
    bound = bind_agent_id(case)
    expected = "bench_journal_probe.tool_journal_read_001_1"
    assert expected in bound.question
    assert bound.expected_tool_params["trading_agent_journal_read"]["agent_id"] == expected


def test_a_non_dataclass_case_passes_through(monkeypatch):
    """Suite cases and stubs have no `dataclasses.replace`; a loader must not die."""
    stub = _case(question="no placeholder here")
    assert bind_agent_id(stub) is stub


# ── Which cases get a fixture ──────────────────────────────────────────────────


def test_a_non_tick_case_that_names_a_journal_tool_is_provisioned(monkeypatch, tmp_path):
    """The gate that excluded all four broken cases was `type == "tick"`."""
    _checkout(monkeypatch, tmp_path)
    case = _case(
        id="c_journal_roundtrip_001",
        type="consult",
        agent_slug="bench_journal_probe",
        expected_tools=["trading_agent_journal_write", "trading_agent_journal_read"],
    )
    assert needs_probe_journal(case)
    assert ensure_probe_journal(case)
    assert probe_strategy_dir(case).is_dir()


def test_a_case_that_names_no_journal_tool_is_left_alone(monkeypatch, tmp_path):
    """Provisioning for a case that never journals is litter in someone's repo."""
    _checkout(monkeypatch, tmp_path)
    case = _case(
        id="tool_get_market_data_001",
        type="tool",
        agent_slug="bench_journal_probe",
        expected_tools=["get_market_data"],
    )
    assert not needs_probe_journal(case)
    assert ensure_probe_journal(case) is None


def _read_case(**over):
    base = dict(
        id="tool_journal_read_001",
        type="tool",
        agent_slug="bench_journal_probe",
        expected_tools=["trading_agent_journal_read"],
    )
    base.update(over)
    return _case(**base)


def _write_case():
    return _case(
        id="tool_journal_write_001",
        type="tool",
        agent_slug="bench_journal_probe",
        expected_tools=["trading_agent_journal_write"],
    )


def test_only_a_read_case_is_seeded():
    """A write case starting empty is the honest test of the write."""
    assert should_seed(_read_case())
    assert not should_seed(_write_case())
    assert not should_seed(_case())  # a tick journals its own entries


def test_a_write_case_gets_no_seed_clause(monkeypatch, tmp_path):
    _checkout(monkeypatch, tmp_path)
    note = ensure_probe_journal(_write_case())
    assert note and "seed" not in note, note


def test_seeding_failure_still_leaves_a_usable_fixture(monkeypatch, tmp_path):
    """Degrade, never raise: a fixture step must not take the case down.

    The seam is monkeypatched rather than relying on the import failing: another
    test in the same process may have put a real condor on sys.path.
    """
    _checkout(monkeypatch, tmp_path)

    def _boom():
        raise RuntimeError("no condor checkout")

    monkeypatch.setattr(probe_journal, "_journal_manager_cls", _boom)
    case = _read_case()
    note = ensure_probe_journal(case)
    assert "could not seed" in note, note
    assert probe_strategy_dir(case).is_dir()


def test_a_read_case_is_seeded_where_read_recent_looks(tmp_path, monkeypatch):
    """`journal_read(section="recent")` reads *snapshots*, not journal.md.

    Seeding only via `append_action` left the read answering empty content, so
    the fixture writes both. Needs the real checkout: the format belongs to
    condor's JournalManager, not to bench.
    """
    from config import condor_path

    repo = condor_path()
    if repo is None or not (repo / "condor" / "agents" / "journal.py").is_file():
        pytest.skip("no condor checkout — seeding needs condor's JournalManager")

    # Provision into a throwaway strategy id under the real checkout, then remove
    # it: the seed's format has to come from condor's own writer.
    import shutil

    case = _read_case(id="bench_probe_seed_selftest")
    target = probe_strategy_dir(case)
    try:
        note = ensure_probe_journal(case)
        assert "seeded" in note, note
        session = target / "sessions" / "session_1"
        assert list((session / "snapshots").glob("snapshot_*.md")), "no snapshots"
        assert (session / "journal.md").is_file()
        assert (target / "learnings.md").is_file()
    finally:
        if target and target.is_dir():
            shutil.rmtree(target)
            parent = target.parent
            if parent.is_dir() and not any(parent.iterdir()):
                shutil.rmtree(parent.parent)


# ── The shipped dataset ────────────────────────────────────────────────────────


def test_every_pinned_agent_id_in_the_dataset_is_resolvable():
    """The regression guard: no case may pin an id condor cannot parse."""
    offenders = []
    for case in load_all_cases():
        for tool, args in (getattr(case, "expected_tool_params", None) or {}).items():
            if not isinstance(args, dict):
                continue
            agent_id = args.get("agent_id")
            if agent_id and not _resolvable(str(agent_id)):
                offenders.append(f"{case.id}.{tool}: {agent_id!r}")
    assert not offenders, (
        "agent_id must be '{agent_slug}.{strategy_slug}_{n}' — condor's "
        f"resolve_agent_dirs cannot parse: {offenders}"
    )


def test_no_placeholder_survives_loading():
    """`{agent_id}` reaching a model would be scored as if the dataset meant it."""
    leftover = [
        case.id
        for case in load_all_cases()
        if AGENT_ID_TOKEN in json.dumps(
            {
                k: v
                for k, v in vars(case).items()
                if isinstance(v, (str, dict, list))
            },
            default=str,
        )
    ]
    assert not leftover, leftover


def test_the_journal_cases_all_declare_a_bench_probe_slug():
    """A journal case without a bench_ slug gets no fixture and cannot pass."""
    for case in load_all_cases():
        tools = set(getattr(case, "expected_tools", None) or []) | set(
            getattr(case, "expected_tool_calls", None) or []
        )
        if any("journal" in t for t in tools):
            slug = getattr(case, "agent_slug", None) or ""
            assert slug.startswith("bench_"), f"{case.id}: {slug!r}"
            assert needs_probe_journal(case), case.id
