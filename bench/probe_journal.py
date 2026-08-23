"""Provision the on-disk journal a probe agent's case writes to, and name it.

condor resolves a journal from the agent_id the model passes:
``resolve_agent_dirs`` splits ``"{agent_slug}.{strategy_slug}_{n}"`` into
``agents/{agent_slug}/strategies/{strategy_slug}/sessions/session_{n}`` and
returns ``(None, None)`` when the *strategy* directory is not there
(``condor/agents/journal.py``). Every ``trading_agent_journal_write`` then answers
``{"error": "no journal available for this agent"}`` however the model behaved.

Two halves have to be right, and this module owns both:

**The id has to be well-formed.** ``resolve_agent_dirs`` starts at
``agent_id.rfind("_")`` and gives up on ``-1`` before touching the disk, so a
hyphenated name never resolves — ``bench-journal-probe`` fails for every model,
in every run, whatever it does. Cases write ``{agent_id}`` and
:func:`bind_agent_id` fills in the composite form, so no dataset ever spells the
format out by hand again. That was the bug behind ``c_journal_roundtrip_001/002``
and ``tool_journal_write_001`` / ``tool_journal_read_001``: the tick path was
fixed (``bench.client.tick_agent_id``) and the four non-tick journal cases were
left on the old hand-written string.

**The directory has to exist.** Nothing created it. Provisioning was gated on
``case.type == "tick"``, which silently excluded every one of those four cases —
so even a correct id resolved to nothing.

A case that *reads* a journal needs entries to read, so those get a seeded one:
otherwise "summarise its last few decisions" is scored against an empty template
and the model can only report that there is nothing there. Seeding runs through
condor's own ``JournalManager`` rather than writing the markdown here, so the
fixture cannot drift from the format the tool under test produces.

Only ``bench_``-prefixed slugs are touched, the same guard
``scripts/clean_probe_journals.py`` deletes under: a real agent's journal is its
memory, and bench has no business creating or clearing directories there.

Everything written underneath is git-ignored on the condor checkout
(``agents/**/sessions/``, ``agents/**/learnings.md``), so warming a fixture never
dirties that repo.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from config import condor_path

BENCH_PREFIX = "bench_"

# Placeholder a case uses instead of spelling out the composite agent_id.
AGENT_ID_TOKEN = "{agent_id}"

# condor's session numbering starts at 1 and bench never runs a second session
# for a probe, so the fixture is always session 1.
PROBE_SESSION = 1

# Seed entries for cases that read a journal back. Deliberately unrelated to what
# any case writes, so a read case cannot pass by finding its own write.
_SEED_DECISIONS = (
    (3, "Held position, no executor opened.", "Spread 0.12% still above the 0.10% floor."),
    (4, "Opened a 12-level grid at mid.", "Spread tightened to 0.06% and depth recovered."),
    (5, "Left the grid running.", "Fills tracking expectation, no drawdown signal."),
)
_SEED_LEARNING = (
    "Grid fills stall when the book thins after a vol spike, even at a tight spread."
)
# Timestamps are fixed rather than "now" so a reseeded fixture is byte-identical
# and a case's result cannot shift with the clock.
_SEED_TIMESTAMPS = ("2026-01-05 09:00", "2026-01-05 09:05", "2026-01-05 09:10")


def probe_agent_id(case: Any) -> str | None:
    """The agent_id condor can resolve for this case, or ``None``.

    One definition of the format for every layer — ``bench.client.tick_agent_id``
    defers to this. A case is identified by its own id rather than by its slug
    alone because several cases share ``bench_journal_probe``, and sharing one
    journal would make a read case's result depend on whether a write case
    happened to run before it.
    """
    slug = str(getattr(case, "agent_slug", None) or "")
    case_id = str(getattr(case, "id", "") or "")
    if not slug or not case_id:
        return None
    return f"{slug}.{case_id}_{PROBE_SESSION}"


def _walk_replace(value: Any, token: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, replacement) if token in value else value
    if isinstance(value, dict):
        return {k: _walk_replace(v, token, replacement) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_replace(v, token, replacement) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk_replace(v, token, replacement) for v in value)
    return value


def bind_agent_id(case: Any) -> Any:
    """Case with ``{agent_id}`` replaced by the resolvable composite id.

    Applied by the dataset loaders, so the question the model reads and the
    ground truth it is scored against carry the same id — and neither can be a
    string someone typed from memory. Returns the case untouched when it holds no
    placeholder, which is every case that does not touch a journal.
    """
    import dataclasses

    agent_id = probe_agent_id(case)
    # Not a dataclass means not a dataset case (a suite case, a test stub): there
    # is no `replace` to build a copy with, and silently raising inside a loader
    # would take the whole library down.
    if not agent_id or not dataclasses.is_dataclass(case):
        return case
    changes = {}
    for f in dataclasses.fields(case):
        if f.name in ("id", "type", "agent_slug"):
            continue
        current = getattr(case, f.name, None)
        replaced = _walk_replace(current, AGENT_ID_TOKEN, agent_id)
        if replaced != current:
            changes[f.name] = replaced
    return dataclasses.replace(case, **changes) if changes else case


def _journal_tools(case: Any) -> list[str]:
    tools = set(getattr(case, "expected_tools", None) or [])
    tools |= set(getattr(case, "expected_tool_calls", None) or [])
    return sorted(t for t in tools if "journal" in t)


def needs_probe_journal(case: Any) -> bool:
    """Whether this case reads or writes a probe agent's journal.

    Ticks always qualify: condor's tick prompt carries an agent_id and the engine
    journals the tick whether or not the case pins the tool. Anything else has to
    actually name a journal tool — provisioning a directory for a case that never
    touches one would leave litter in the condor checkout for nothing.
    """
    slug = str(getattr(case, "agent_slug", None) or "")
    if not slug.startswith(BENCH_PREFIX):
        return False
    return getattr(case, "type", "") == "tick" or bool(_journal_tools(case))


def probe_strategy_dir(case: Any) -> Path | None:
    """Strategy directory condor resolves this case's journal from, if any.

    None when the case does not touch a probe journal or no condor checkout
    resolves — both are "nothing for us to provision", and a missing checkout is
    already reported by the wiring pre-flight.
    """
    if not needs_probe_journal(case):
        return None
    slug = str(getattr(case, "agent_slug", None) or "")
    case_id = str(getattr(case, "id", "") or "")
    if "/" in slug or "/" in case_id or ".." in slug or ".." in case_id:
        return None
    repo = condor_path()
    if repo is None:
        return None
    return repo / "agents" / slug / "strategies" / case_id


def _journal_manager_cls():
    """condor's ``JournalManager``, from the configured checkout.

    A named seam rather than an inline import: the seeding path has to degrade
    when condor is unavailable, and a test cannot force that by pointing
    ``condor_path`` at an empty directory — a condor already on ``sys.path`` from
    elsewhere in the process would satisfy the import anyway.
    """
    repo = condor_path()
    if repo is None:
        raise RuntimeError("no condor checkout")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from condor.agents.journal import JournalManager  # noqa: PLC0415

    return JournalManager


def should_seed(case: Any) -> bool:
    """Whether this case needs starter entries in its fresh journal.

    Only a case that *reads*. A write case starting from an empty journal is the
    honest test of the write — seeding it would let a model that never wrote
    anything still find content to summarise.
    """
    return any("read" in tool for tool in _journal_tools(case))


def _seed_journal(case: Any, strategy_dir: Path) -> str | None:
    """Write starter entries through condor's JournalManager. Note, or None.

    Best-effort: on any failure the directory still exists, so write cases work
    and a read case reads an empty-but-valid journal. Degrading beats raising
    inside a fixture step.
    """
    agent_id = probe_agent_id(case)
    if not agent_id:
        return None
    try:
        JournalManager = _journal_manager_cls()
        session_dir = strategy_dir / "sessions" / f"session_{PROBE_SESSION}"
        jm = JournalManager(agent_id, session_dir=session_dir, agent_dir=strategy_dir)
        for (tick, action, reasoning), stamp in zip(_SEED_DECISIONS, _SEED_TIMESTAMPS):
            jm.append_action(tick, action, reasoning)
            # `journal_read(section="recent")` — the default, and what a read case
            # gets — reads *snapshots*, not journal.md's Decisions section
            # (`JournalManager.read_recent`). Seeding only via append_action left
            # the read answering empty content, so the fixture has to write both.
            jm.save_full_snapshot(
                tick=tick,
                timestamp=stamp,
                system_prompt="(bench probe fixture)",
                response_text=f"{action} {reasoning}",
                tool_calls=[],
                executors_data="No executors.",
                risk_state={},
                duration=0.0,
            )
        jm.append_learning(_SEED_LEARNING, category="execution")
    except Exception as exc:
        return f"could not seed probe journal ({type(exc).__name__}: {exc})"
    return f"seeded probe journal with {len(_SEED_DECISIONS)} decisions"


def ensure_probe_journal(case: Any) -> str | None:
    """Create (and for read cases, seed) the probe agent's journal. Note or None.

    Idempotent: an existing directory is left exactly as it is, so journal
    entries a case wrote survive until ``clean_probe_journals.py`` clears them.
    """
    target = probe_strategy_dir(case)
    if target is None or target.is_dir():
        return None
    target.mkdir(parents=True, exist_ok=True)
    note = f"provisioned probe journal {target.parent.parent.name}/{target.name}"
    if should_seed(case):
        seeded = _seed_journal(case, target)
        if seeded:
            note = f"{note}; {seeded}"
    return note
