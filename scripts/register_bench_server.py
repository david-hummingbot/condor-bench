#!/usr/bin/env python3
"""Register the bench staging server in condor's servers config.

Live benchmarks reuse condor's ``build_mcp_servers_for_agent()``, which resolves
the API URL and credentials from ``config.yml`` by server name. That keeps URL
resolution in one place — but it means the bench server has to exist there, or
condor starts *without* mcp-hummingbot (a log warning, not an error) and every
tool case fails for a reason that has nothing to do with the model.

This script writes that entry from bench's own env vars:

    BENCH_SERVER_NAME    entry name           (default: bench_staging)
    HUMMINGBOT_API_URL   host + port          (required)
    HUMMINGBOT_USERNAME  credentials
    HUMMINGBOT_PASSWORD

Usage:
    uv run python scripts/register_bench_server.py            # add or update
    uv run python scripts/register_bench_server.py --dry-run  # show the change
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from config import condor_path, staging_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = parser.parse_args()

    repo = condor_path()
    if repo is None:
        print(
            "No condor checkout found. Set CONDOR_PATH=/path/to/condor.",
            file=sys.stderr,
        )
        return 1

    staging = staging_config()
    api_url = str(staging["api_url"])
    if not api_url:
        print("HUMMINGBOT_API_URL is not set — nothing to register.", file=sys.stderr)
        return 1

    parsed = urlparse(api_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        print(f"Could not parse a host out of HUMMINGBOT_API_URL={api_url!r}", file=sys.stderr)
        return 1

    name = str(staging["server_name"])
    username = str(staging["username"])
    password = str(staging["password"])
    if not username or not password:
        print(
            "HUMMINGBOT_USERNAME / HUMMINGBOT_PASSWORD are required — condor passes "
            "them to the MCP server as --username/--password.",
            file=sys.stderr,
        )
        return 1

    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from config_manager import ConfigManager  # noqa: PLC0415

    cm = ConfigManager.instance(str(repo / "config.yml"))
    existing = cm.get_server(name)

    print(f"condor config: {repo / 'config.yml'}")
    print(f"  {name} → {host}:{port} as {username}")
    if existing:
        print(f"  (replacing existing entry {existing['host']}:{existing['port']})")

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    if existing:
        ok = cm.modify_server(name, host=host, port=port, username=username, password=password)
    else:
        ok = cm.add_server(name, host=host, port=port, username=username, password=password)

    if not ok:
        print(f"config_manager refused to write '{name}'.", file=sys.stderr)
        return 1

    resolved = cm.get_server(name)
    print(f"registered: {resolved}")
    print("\nVerify the wiring end to end with:  uv run python runner.py staging-check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
