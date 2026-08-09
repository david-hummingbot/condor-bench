"""Isolated Condor checkout probe — run in a subprocess, never in the API process.

Usage:
  uv run python scripts/validate_environment.py \\
      --condor-path /path/to/condor \\
      --server-name bench_staging \\
      --expected-branch main \\
      --require-clean 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--condor-path", required=True)
    parser.add_argument("--server-name", default="")
    parser.add_argument("--expected-branch", default="")
    parser.add_argument("--require-clean", default="0")
    args = parser.parse_args()

    os.environ["CONDOR_PATH"] = args.condor_path
    # Ensure a fresh import path for this process.
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from config import build_run_pin, condor_checkout_state, condor_path

    repo = condor_path()
    state = condor_checkout_state()
    report: dict = {
        "ok": True,
        "blocking": [],
        "warnings": [],
        "checkout": state,
        "loaded": None,
        "server_registered": None,
        "branch_match": None,
    }

    if repo is None:
        report["ok"] = False
        report["blocking"].append(
            f"condor_path does not look like a checkout: {args.condor_path}"
        )
        print(json.dumps(report))
        return 1

    pin = build_run_pin(run_type="validate", shared_loaded=False)
    report["loaded"] = pin["condor"]["loaded"]  # type: ignore[index]

    expected = (args.expected_branch or "").strip()
    if expected:
        match = state.get("branch") == expected
        report["branch_match"] = match
        if not match:
            report["ok"] = False
            report["blocking"].append(
                f"expected branch '{expected}', got '{state.get('branch')}'"
            )

    if args.require_clean in ("1", "true", "yes") and int(state.get("dirty_files") or 0) > 0:
        report["ok"] = False
        report["blocking"].append(
            f"checkout has {state.get('dirty_files')} uncommitted file(s)"
        )

    server_name = (args.server_name or "").strip()
    if server_name:
        try:
            from bench.mcp_provider import condor_server_entry, load_condor_shared

            load_condor_shared()
            entry = condor_server_entry(server_name)
            report["server_registered"] = entry is not None
            report["loaded"] = build_run_pin(run_type="validate", shared_loaded=True)[
                "condor"
            ]["loaded"]  # type: ignore[index]
            if entry is None:
                report["ok"] = False
                report["blocking"].append(
                    f"server '{server_name}' is not registered in this checkout's config.yml"
                )
        except Exception as exc:
            report["ok"] = False
            report["server_registered"] = False
            report["blocking"].append(f"could not load condor wiring: {exc}")

    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
