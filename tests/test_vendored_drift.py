"""AGENT.md must match the system prompt condor actually ships.

bench/client.py injects condor_compat/assistants/condor/AGENT.md verbatim as the
Condor system prompt. When condor edits its prompt and this copy doesn't follow,
the benchmark grades models against rules production no longer states — and the
tool-accuracy metric in particular starts measuring the wrong behaviour.

This check needs a condor checkout, so it skips when there isn't one (CI without
the sibling repo, a fresh clone). It is a guard for developers who have both
repos side by side, not a hard gate.

The other vendored files (acp/, agents/prompts.py) carry deliberate
bench-specific edits, so they cannot be byte-compared. Re-syncing those is a
manual review — see README "Keeping condor_compat in sync".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "condor_compat" / "agents" / "condor" / "AGENT.md"
UPSTREAM_REL = Path("agents") / "condor" / "AGENT.md"


def _strip_frontmatter(text: str) -> str:
    """Drop the leading YAML block; the vendored copy is body-only."""
    if text.startswith("---"):
        return text.split("---", 2)[2].lstrip("\n")
    return text


def _condor_repo() -> Path | None:
    candidate = Path(os.environ.get("CONDOR_REPO", ROOT.parent / "condor"))
    return candidate if (candidate / UPSTREAM_REL).is_file() else None


def test_agent_md_matches_condor():
    repo = _condor_repo()
    if repo is None:
        pytest.skip("no condor checkout — set CONDOR_REPO to enable this check")

    upstream = _strip_frontmatter((repo / UPSTREAM_REL).read_text())
    vendored = VENDORED.read_text()

    if upstream.strip() == vendored.strip():
        return

    import difflib

    diff = "\n".join(
        difflib.unified_diff(
            upstream.splitlines(),
            vendored.splitlines(),
            fromfile=f"condor/{UPSTREAM_REL}",
            tofile=str(VENDORED.relative_to(ROOT)),
            lineterm="",
            n=1,
        )
    )
    pytest.fail(
        "Vendored AGENT.md has drifted from condor's system prompt.\n"
        "Re-vendor it (frontmatter stripped) so benchmarks grade against the "
        f"rules production states.\n\n{diff}"
    )
