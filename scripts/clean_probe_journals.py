#!/usr/bin/env python3
"""Clear the journals of bench's own probe agents in the condor checkout.

Why this is a script and not part of ``bench.cleanup.teardown``: there is no MCP
delete. ``trading_agent_journal_write`` takes no ``action`` argument and condor
exposes no journal delete or clear tool, so a per-case teardown has nothing to
call. Entries land on disk under condor's local agent layer
(``config.condor_local_agents_root``, ``<condor>/.condor/agents`` by default)::

    <local agents root>/<slug>/loops/<case_id>/sessions/session_N/journal.md

and, from before condor moved journals there (FEAT-115), under the checkout's
``agents/<slug>/{sessions,strategies/*/sessions}`` — both are still swept.

Left alone they accumulated across every case, model and sweep. That is not just
untidy: ``trading_agent_journal_read`` returns them, so its responses grow until
they crowd the judge's context and slow every journal case down. A run now resets
each probe journal before every trial (``bench.probe_journal.ensure_probe_journal``,
which also re-seeds what this script removes), so what is left is the last trial's.

**Only bench's own probe agents are touched.** Any slug not starting with ``bench_``
is refused, so this can never clear a real trading agent's history — that history is
the agent's memory, and deleting it would be destroying user data, not cleaning up.

Usage:
    uv run python scripts/clean_probe_journals.py            # show what would go
    uv run python scripts/clean_probe_journals.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Load .env explicitly. config.condor_path() falls back to a sibling ../condor when
# CONDOR_PATH is unset, and this script deletes things — resolving the wrong checkout
# is not an acceptable failure mode. Do not rely on an unrelated import happening to
# call load_dotenv() as a side effect.
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

BENCH_PREFIX = "bench_"


def agent_roots(repo: Path) -> list[Path]:
    """Where bench's probe agents can live: condor's local layer, then the legacy
    in-checkout ``agents/`` where journals went before FEAT-115."""
    from config import condor_local_agents_root

    roots = [condor_local_agents_root(repo), repo / "agents"]
    return [r for i, r in enumerate(roots) if r is not None and r not in roots[:i]]


def probe_agent_dirs(repo: Path) -> list[Path]:
    """Agent directories owned by bench, i.e. slugs prefixed ``bench_``."""
    return sorted(
        d
        for agents in agent_roots(repo)
        if agents.is_dir()
        for d in agents.iterdir()
        if d.is_dir() and d.name.startswith(BENCH_PREFIX)
    )


def journal_targets(agent_dir: Path) -> list[Path]:
    """Session directories under an agent that hold a journal.

    Every layout condor has resolved: the flat ``<agent>/sessions/``, the
    per-strategy ``<agent>/loops/<slug>/sessions/`` that tick probes use now
    (``bench.probe_journal.probe_strategy_dir``), and the ``strategies/`` it was
    called before. Missing the current one would let exactly the journals bench
    provisions grow forever, which is what this script exists to prevent.
    """
    roots = [agent_dir / "sessions"]
    for parent in ("loops", "strategies"):
        strategies = agent_dir / parent
        if strategies.is_dir():
            roots.extend(d / "sessions" for d in sorted(strategies.iterdir()) if d.is_dir())
    return sorted(
        d
        for sessions in roots
        if sessions.is_dir()
        for d in sessions.iterdir()
        if d.is_dir() and (d / "journal.md").is_file()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the session directories. Without it, only report them.",
    )
    args = parser.parse_args()

    from config import condor_checkout_label, condor_path

    repo = condor_path()
    if repo is None:
        print("no condor checkout — set CONDOR_PATH", file=sys.stderr)
        return 2
    print(f"checkout: {condor_checkout_label()}")

    dirs = probe_agent_dirs(repo)
    if not dirs:
        where = " or ".join(str(r) for r in agent_roots(repo))
        print(f"no bench probe agents under {where} — nothing to clean")
        return 0

    total_sessions = 0
    total_bytes = 0
    for agent_dir in dirs:
        targets = journal_targets(agent_dir)
        if not targets:
            print(f"  {agent_dir.name}: no journals")
            continue
        size = sum(
            (d / "journal.md").stat().st_size for d in targets if (d / "journal.md").is_file()
        )
        total_sessions += len(targets)
        total_bytes += size
        print(f"  {agent_dir.name}: {len(targets)} session(s), {size / 1024:.1f} KiB")
        if args.apply:
            for d in targets:
                # Belt and braces: never step outside a bench_ agent, even if a
                # symlink or an odd session name tried to lead us out.
                resolved = d.resolve()
                if not resolved.is_relative_to(agent_dir.resolve()):
                    print(f"      refusing {d} — outside {agent_dir.name}")
                    continue
                shutil.rmtree(d)
            print(f"      cleared {len(targets)} session(s)")

    verb = "cleared" if args.apply else "would clear"
    print(f"\n{verb} {total_sessions} session(s), {total_bytes / 1024:.1f} KiB total")
    if not args.apply and total_sessions:
        print("re-run with --apply to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
