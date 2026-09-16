"""Bench's own additions to the vendored prompt builder.

``scripts/revendor.py`` writes ``condor_compat/agents/prompts.py`` as condor's
file (imports rewritten) with this module's body appended verbatim underneath a
marker. So: **edit this file, never prompts.py.** Re-vendoring regenerates
everything above the marker and replaces nothing below it.

The point of the split is that pulling condor's changes stops being a merge. An
upstream edit to a prompt block lands wholesale; anything bench needs to say on
top lives here, where a diff shows it as bench's and not as drift.

Keep this file small. Something that belongs upstream belongs upstream — the
reason the previous copy diverged is that edits made here were indistinguishable
from a stale vendor.
"""
from __future__ import annotations

from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Nothing bench-specific is needed today.
#
# The three adaptations the old header described are now handled structurally
# rather than by editing prompt text:
#
#   1. Agent/Strategy annotations   -> condor_compat.upstream_shims (= Any)
#   2. is_pydantic_ai_model import  -> rewritten to condor_compat.acp by the script
#   3. The controller-mode branch   -> carried now, not omitted. It costs two
#      string constants and one builder, and omitting it is what let
#      BASE_PROMPT_COMMON and JOURNAL_SECTION_LIVE quietly lose content that
#      *was* in scope (the drift paragraph, the routine-authoring rule, the
#      whole SESSION CANVAS block).
#
# A future override goes here as a plain reassignment, with a comment saying
# what bench measures that production does not, e.g.:
#
#   BASE_PROMPT_COMMON = BASE_PROMPT_COMMON + "\n<bench-only line>\n"
# ─────────────────────────────────────────────────────────────────────────────


def bench_overrides_applied() -> dict[str, Any]:
    """What this section changed, for the result metadata and the drift test.

    An empty mapping means the vendored prompt is condor's, unmodified — which
    is the state a run wants to be able to assert rather than assume.
    """
    return {}
