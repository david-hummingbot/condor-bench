"""Provision the on-disk journal a tick case's probe agent writes to.

condor resolves a journal from the agent_id in the tick prompt: ``resolve_agent_dirs``
splits ``"{agent_slug}.{strategy_slug}_{n}"`` into
``agents/{agent_slug}/strategies/{strategy_slug}/sessions/session_{n}`` and returns
``(None, None)`` when the *strategy* directory is not there
(``condor/agents/journal.py``). Every ``trading_agent_journal_write`` then answers
``{"error": "no journal available for this agent"}`` however the model behaved.

Nothing created those directories. :func:`bench.client.tick_agent_id` made the id
resolvable, which turned the failure from "malformed id" into "fixture missing" —
this closes the second half. Three tick cases pin a journal write and lost both
live validity and answer quality ("silently ignored the failed write") to a
directory that was never made.

Only ``bench_``-prefixed slugs are touched, the same guard
``scripts/clean_probe_journals.py`` deletes under: a real agent's journal is its
memory, and bench has no business creating or clearing directories there.

condor's ``JournalManager`` makes ``sessions/session_N/`` and ``journal.md`` itself
once the strategy dir exists, so that is all this provisions. Everything it writes
underneath is git-ignored on the condor checkout (``agents/**/sessions/``,
``agents/**/learnings.md``), so warming a fixture never dirties that repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import condor_path

BENCH_PREFIX = "bench_"


def probe_strategy_dir(case: Any) -> Path | None:
    """Strategy directory condor resolves this case's journal from, if any.

    None when the case is not a tick, its slug is not a bench probe, or no condor
    checkout resolves — all three are "nothing for us to provision", and a missing
    checkout is already reported by the wiring pre-flight.
    """
    if getattr(case, "type", "") != "tick":
        return None
    slug = str(getattr(case, "agent_slug", None) or "")
    case_id = str(getattr(case, "id", "") or "")
    if not slug.startswith(BENCH_PREFIX) or not case_id:
        return None
    if "/" in slug or "/" in case_id or ".." in slug or ".." in case_id:
        return None
    repo = condor_path()
    if repo is None:
        return None
    return repo / "agents" / slug / "strategies" / case_id


def ensure_probe_journal(case: Any) -> str | None:
    """Create the probe agent's strategy directory. Returns a note if it made one.

    Idempotent: an existing directory is left exactly as it is, so journal
    entries a case wrote survive until ``clean_probe_journals.py`` clears them.
    """
    target = probe_strategy_dir(case)
    if target is None or target.is_dir():
        return None
    target.mkdir(parents=True, exist_ok=True)
    return f"provisioned probe journal {target.parent.parent.name}/{target.name}"
