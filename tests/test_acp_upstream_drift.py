"""condor's `acp/` moved and the vendored copy did not (see scripts/acp_drift.py).

This is the one vendored area `revendor.py` cannot regenerate: bench diverges
*inside* these functions, so pulling condor's file whole would delete the
auto-approved permissions, `PromptDone.error`, `include_instructions=True`, the
per-server `cwd`, the `default`-sentinel handling and the two-shape
`parse_session_models`. So nothing is pulled — the pin only reports what changed,
and a human decides per function whether bench needs it.

It exists because the last gap here was found by accident, months late. condor's
ffe9e5af added one keyword inside an existing `start()`; bench kept scoring
pydantic-ai models that had never been sent the condor server's own instructions
while ACP models on the same cases had them. Nothing failed — there was nothing
watching this layer at all.

Skips without a condor checkout, like the other drift checks: it guards a
developer who has both repos side by side, and is not a hard gate in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _condor() -> Path | None:
    from config import condor_path

    candidate = condor_path()
    if candidate and (candidate / "condor" / "acp").is_dir():
        return candidate
    return None


def test_vendored_acp_is_reviewed_against_current_condor():
    condor = _condor()
    if condor is None:
        pytest.skip("no condor checkout with condor/acp/ — set CONDOR_PATH")

    from acp_drift import PIN, compare

    assert PIN.is_file(), (
        "no upstream pin recorded. Create one with: "
        "uv run python scripts/acp_drift.py --update"
    )
    lines, _ = compare(condor)
    assert not lines, (
        "condor_compat/acp/ is behind condor — review each entry, port what bench "
        "needs, then re-pin with `uv run python scripts/acp_drift.py --update`:\n"
        + "\n".join(lines)
    )


def test_the_pin_covers_the_files_bench_actually_vendors():
    """A pin that silently stopped covering a file would report 'up to date'."""
    from acp_drift import FILES, PIN

    if not PIN.is_file():
        pytest.skip("no pin recorded yet")
    import json

    pinned = json.loads(PIN.read_text())
    for upstream in set(FILES.values()):
        assert upstream in pinned["files"], f"{upstream} dropped out of the pin"
    for vendored in FILES:
        assert (ROOT / vendored).is_file(), f"{vendored} is no longer vendored"
