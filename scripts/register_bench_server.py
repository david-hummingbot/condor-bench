#!/usr/bin/env python3
"""Register the bench staging server in condor's servers config.

Live benchmarks resolve Hummingbot through Condor's production MCP wiring, which
looks up a named server entry. Operators normally never run this by hand —
saving Staging URL/credentials in the dashboard Settings page calls the same
``ensure_bench_server()`` helper. This CLI remains for scripting and recovery.

Usage:
    uv run python scripts/register_bench_server.py            # add or update
    uv run python scripts/register_bench_server.py --dry-run  # show the change
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from bench.staging_setup import ensure_bench_server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = parser.parse_args()

    result = ensure_bench_server(dry_run=args.dry_run)
    print(result.detail)
    if result.actions:
        print("actions:", ", ".join(result.actions))
    if result.ok and not args.dry_run:
        print("\nVerify the wiring end to end with:  uv run python runner.py staging-check")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
