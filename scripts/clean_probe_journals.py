#!/usr/bin/env python3
"""Clear the journals of bench's own probe agents in the condor checkout.

Why this is a script and not part of ``bench.cleanup.teardown``: there is no MCP
delete. ``trading_agent_journal_write`` takes no ``action`` argument and condor
exposes no journal delete or clear tool, so a per-case teardown has nothing to
call. Entries land on disk under::

    <condor>/agents/<slug>/sessions/session_N/journal.md

Left alone they accumulate across every case, model and sweep. That is not just
untidy: ``trading_agent_journal_read`` returns them, so its responses grow until
they crowd the judge's context and slow every journal case down.

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


def probe_agent_dirs(repo: Path) -> list[Path]:
    """Agent directories owned by bench, i.e. slugs prefixed ``bench_``."""
    agents = repo / "agents"
    if not agents.is_dir():
        return []
    return sorted(
        d for d in agents.iterdir() if d.is_dir() and d.name.startswith(BENCH_PREFIX)
    )


def journal_targets(agent_dir: Path) -> list[Path]:
    """Session directories under an agent that hold a journal."""
    sessions = agent_dir / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(d for d in sessions.iterdir() if d.is_dir() and (d / "journal.md").is_file())


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
        print(f"no bench probe agents under {repo / 'agents'} — nothing to clean")
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
