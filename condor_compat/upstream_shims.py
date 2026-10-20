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


# Mirrors condor/memory/paths.py's module-level set: the vendored prompt builder
# re-binds it by name (``_paths._SHADOWED_RULEBOOKS_WARNED``).
_SHADOWED_RULEBOOKS_WARNED: set[tuple[str, str]] = set()


def read_layered_file(
    filename: str,
    agent_slug: str | None = None,
    *,
    within: tuple[str, ...] | list[str] = (),
    skip_empty_body: bool = False,
) -> tuple[dict, str] | None:
    """Upstream's single-file policy resolver, on bench's layers.

    Same levels as ``condor.memory.paths.read_layered_file`` — ``<home>/<*within>``,
    then ``<home>``, then ``_defaults`` — over :func:`agent_home_layers`, which in a
    checkout is one root seen twice. No shadow warning: with one root there is
    nothing to shadow. An unreadable file is skipped, as upstream does.
    """
    local_home, _ = agent_home_layers(agent_slug)
    dirs = []
    if within:
        dirs.append(local_home.joinpath(*within))
    dirs += [local_home, defaults_layers()[0]]
    for directory in dirs:
        path = directory / filename
        try:
            if not path.is_file():
                continue
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            body = body.strip()
            if skip_empty_body and not body:
                continue
            return meta or {}, body
        except Exception:  # noqa: BLE001 — a broken policy is never a crash
            continue
    return None


class _MemoryPaths:
    """``from condor.memory import paths as _paths`` — the names the builder reads."""

    _SHADOWED_RULEBOOKS_WARNED = _SHADOWED_RULEBOOKS_WARNED
    read_layered_file = staticmethod(read_layered_file)
    agent_home_layers = staticmethod(agent_home_layers)
    defaults_layers = staticmethod(defaults_layers)


memory_paths = _MemoryPaths()


# ── condor.agent_controllers ─────────────────────────────────────────────────
def controllers_section(agent_slug: str | None) -> str:
    """The CONTROLLERS index production puts in the tick prompt, from the checkout.

    Not reimplemented: the index is derived from each controller's source,
    CONTROLLER.md and sample configs, and a copy of that parser would drift the
    first time condor changed it. So this loads condor's own
    ``condor.agent_controllers`` from the configured checkout (the same
    load-production-code-by-path rule ``bench/mcp_provider.py`` follows) and asks
    it. Its imports are condor-internal and stdlib only — no Telegram stack.
    """
    import importlib
    import sys

    from config import condor_path

    repo = condor_path()
    if repo is None:
        return ""
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    module = importlib.import_module("condor.agent_controllers")
    return module.controllers_section(agent_slug)


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
