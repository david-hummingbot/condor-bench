"""The handful of condor symbols the vendored prompt builders import.

Re-vendoring is a copy plus a fixed import rewrite (``scripts/revendor.py``):
every ``from condor.<x> import name`` in an upstream file becomes
``from condor_compat.upstream_shims import name``. This module is the other half
of that contract — it has to provide every name those files reach for.

That is deliberate leverage. When condor grows a new import, the rewrite points
it here, the name is missing, and the re-vendor script's import check fails with
the name in the message. The alternative — stubbing unknown imports — is how the
previous copy rotted silently for a whole commit.

Nothing here is upstream logic. It is bench's answer to a question condor
answers from its own runtime: where the agent files live (a checkout, not an
install), what the seat mutes (bench's allowlist, not an operator's), and which
routines exist (none — bench passes its own section in).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# ── Types ────────────────────────────────────────────────────────────────────
# Upstream annotates with the real models. Bench never constructs one — cases
# carry duck-typed stand-ins — and importing them would drag condor's model
# layer in, so the annotations resolve to Any. Safe because every vendored file
# carries ``from __future__ import annotations``: these are never evaluated.
Agent = Any
Strategy = Any


# ── condor.runtime.state ─────────────────────────────────────────────────────
#: Mirrors ``condor/runtime/state.py``. A literal rather than an import because
#: the state module is a large runtime dependency for one integer.
MAX_STATE_VALUE_CHARS = 2000


# ── condor.frontmatter ───────────────────────────────────────────────────────
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """``(metadata, body)`` for a leading ``---`` YAML block.

    Upstream returns the parsed mapping; the vendored prompt builder only ever
    uses the body (``_, body = parse_frontmatter(...)``), so the mapping is
    parsed best-effort and never load-bearing here.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    try:
        import yaml

        loaded = yaml.safe_load(parts[1])
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    return meta, parts[2].lstrip("\n")


# ── condor.memory.paths ──────────────────────────────────────────────────────
def _agents_root() -> Path | None:
    """``<condor checkout>/agents``, or ``None`` without a checkout.

    Resolved through ``config.condor_path()`` — the same function
    ``tests/test_vendored_drift.py`` uses — so a machine with two condor clones
    cannot validate against one and read rulebooks from the other.
    """
    from config import condor_path

    repo = condor_path()
    return (repo / "agents") if repo else None


def agent_home_layers(agent_slug: str | None = None) -> tuple[Path, Path]:
    """``(local, stock)`` for an agent.

    condor has two roots (an install's own ``agents/`` overriding the shipped
    one); a checkout has one, so both layers are the same directory. The
    vendored ``_warn_once_if_shadowed`` compares the two files and returns early
    when they are equal, so one root never produces a spurious shadow warning.
    """
    root = _agents_root() or Path("/nonexistent")
    home = root / (agent_slug or "condor")
    return home, home


def defaults_layers() -> tuple[Path, Path]:
    """``_defaults/`` in both roots — one directory here, for the reason above."""
    root = _agents_root() or Path("/nonexistent")
    return root / "_defaults", root / "_defaults"


# ── condor.runtime.toolsets ──────────────────────────────────────────────────
def _every_tool_name() -> set[str]:
    """Every tool name any ring of either vendored profile can mount."""
    from condor_compat.mcp_servers import condor_profiles, hummingbot_profiles

    return {
        name
        for module in (condor_profiles, hummingbot_profiles)
        for ring in module.PROFILE_TOOLS.values()
        for name in ring
    }


def seat_mutes(agent_slug: str | None = None) -> list[str]:
    """What this seat must not mount, as upstream's mute names.

    Upstream is operator mutes plus the complement of the Agent's ``tools:``
    allowlist. Bench has no operator mutes, so this is the allowlist half only —
    read through ``bench.client.load_agent_tools``, which already resolves the
    same ``tools:`` key from the same checkout. ``None`` there means unrestricted
    (no slug, or an agent that declares no allowlist), and mutes nothing.
    """
    from bench.client import load_agent_tools

    allowed = load_agent_tools(agent_slug)
    if not allowed:
        return []
    names = {str(t).rsplit("__", 1)[-1] for t in allowed}
    return sorted(_every_tool_name() - names)


# ── routines.base ────────────────────────────────────────────────────────────
def assistant_routines(*_args: Any, **_kwargs: Any) -> list:
    """No routine library in bench.

    Upstream discovers the installed routines to build ``[ROUTINES]``. Bench
    always passes ``cached_routines_section`` explicitly, so this is the
    can't-happen branch; it returns empty rather than raising, because upstream
    treats discovery failure as "no routines" and not as a failed tick.
    """
    return []
