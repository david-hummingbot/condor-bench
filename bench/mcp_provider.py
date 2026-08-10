"""MCP server configs for benchmark runs, from condor's production wiring.

bench does not reimplement condor's MCP spawn args. It loads condor's own
``handlers/agents/_shared.py`` and calls ``build_mcp_servers_for_session()``, so
the benchmark launches the same subprocesses production does — including the two
args condor-evals' harness drops (``--server-name`` on both servers,
``--agent-slug`` on condor).

That loading is also what caught condor's SEC-095 change: credentials used to sit
on argv, where any local ``ps`` could read them, and now travel in the subprocess
``env`` instead. A vendored copy of the spawn args would have kept putting the
API password on the command line while production had stopped.

Why those two matter:

* ``--server-name`` picks which hummingbot-api instance the tools talk to and is
  what ``start_agent`` resolves against. Without it the MCP server falls back to
  ``HUMMINGBOT_API_URL`` or ``http://localhost:8000`` with ``admin``/``admin``
  (see condor's ``mcp_servers/hummingbot_api/settings.py``) — on a dev machine,
  plausibly the real API rather than staging.
* ``--agent-slug`` scopes condor's memory/skill tools to ``agents/{slug}/``.
  Without it a ``market_making_expert`` case reads the *chat* condor's stores,
  never finds its ``pmm_config_playbook`` skill, and fails — which the matrix
  would then report as "market_making_expert needs a bigger model". A harness
  artifact laundered into a routing recommendation.

The condor package's ``__init__`` chain imports python-telegram-bot, which bench
does not depend on, so ``_shared.py`` is loaded by file path instead of as
``handlers.agents._shared``. Its module-level imports are stdlib only and
``config_manager`` is imported lazily inside the builders, so this is a load of
the real production code, not a copy of it.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any

from config import condor_path, staging_config

log = logging.getLogger(__name__)

# Bench-only keys we add to a production config dict. The wiring drift test
# ignores exactly these when comparing against condor's output.
BENCH_ONLY_CONFIG_KEYS = ("cwd",)

# Servers condor's .mcp.json declares that a benchmark must never inherit.
# playwright's tools would join the discovery set on the ACP path and shift the
# `tool_defs[:limit]` cut that small models run under, so a model's tool score
# would depend on how many browser tools happened to sort ahead of the trading
# ones. Excluded here; reported in run metadata when ACP could still find it.
EXCLUDED_MCP_SERVERS = ("playwright",)

_shared_module: Any = None


class LiveWiringError(RuntimeError):
    """MCP configs could not be built. Never degrade to a weaker wiring silently."""


# ── Production wiring loader ───────────────────────────────────────────────────
def load_condor_shared() -> Any:
    """Import condor's ``handlers/agents/_shared.py`` as a module object.

    Cached. Also seeds ``ConfigManager`` with condor's absolute ``config.yml``
    before the builders touch it: the singleton defaults to a *relative*
    ``config.yml``, which under bench's cwd would resolve to nothing and make
    ConfigManager write a fresh default config — a server list with no staging
    entry in it.
    """
    global _shared_module
    if _shared_module is not None:
        return _shared_module

    repo = condor_path()
    if repo is None:
        raise LiveWiringError(
            "A benchmark run needs a condor checkout for the production MCP wiring. "
            "Set CONDOR_PATH=/path/to/condor (a directory containing mcp_servers/)."
        )

    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    try:
        import config_manager  # noqa: PLC0415  (condor's, resolved via sys.path)

        config_manager.ConfigManager.instance(str(repo / "config.yml"))
    except Exception as exc:  # pragma: no cover — condor layout change
        raise LiveWiringError(
            f"Could not initialise condor's ConfigManager from {repo / 'config.yml'}: {exc}"
        ) from exc

    shared_py = repo / "handlers" / "agents" / "_shared.py"
    if not shared_py.is_file():
        raise LiveWiringError(
            f"{shared_py} not found — condor's agent MCP helpers moved. "
            "bench must not fall back to a private copy of the spawn args."
        )

    spec = importlib.util.spec_from_file_location("condor_agents_shared", shared_py)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise LiveWiringError(f"Could not load {shared_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # condor once had a separate build_mcp_servers_for_agent(); it was folded into
    # the session builder, which now takes server_name and agent_slug and covers
    # both. Only the survivor is required — but loudly, because a rename here is
    # exactly the drift that would otherwise send bench off a private copy.
    if not hasattr(module, "build_mcp_servers_for_session"):
        raise LiveWiringError(
            "condor's _shared.py no longer exports build_mcp_servers_for_session(). "
            "Update bench/mcp_provider.py to match production — do not vendor a copy "
            "of the spawn args."
        )

    _shared_module = module
    return module


def condor_server_entry(server_name: str) -> dict[str, Any] | None:
    """Look up a server in condor's config, or None when it isn't registered."""
    load_condor_shared()
    from config_manager import get_config_manager  # noqa: PLC0415

    return get_config_manager().get_server(server_name)


# ── Config builder ─────────────────────────────────────────────────────────────
def build_mcp_configs(
    *,
    agent_slug: str | None = None,
    server_name: str | None = None,
) -> list[dict]:
    """MCP server configs for one benchmark case.

    Production spawn args for the staging server, from condor's own helpers.
    ``agent_slug`` must be set for agent-scoped cases (Layer 3, ticks) and left
    None for chat-scoped consults — see the module docstring for why.
    """
    shared = load_condor_shared()
    staging = staging_config()
    server_name = server_name or str(staging["server_name"])
    repo = condor_path()
    assert repo is not None  # load_condor_shared() raises otherwise

    entry = condor_server_entry(server_name)
    if not entry:
        raise LiveWiringError(
            f"Server '{server_name}' is not registered in {repo / 'config.yml'}. "
            "condor resolves the API URL and credentials from that entry, so an "
            "unregistered name makes it start WITHOUT mcp-hummingbot (a warning, "
            "not an error) and every tool case fails for the wrong reason. "
            "Register it with: uv run python scripts/register_bench_server.py"
        )

    # One builder covers both scopes: server_name pins the hummingbot instance and
    # agent_slug decides whether condor's memory/skill tools read the agent's own
    # stores or the chat's. Passing server_name explicitly also skips condor's
    # chat-preference resolution, which would otherwise pick whatever server the
    # bench chat id happens to default to.
    configs = shared.build_mcp_servers_for_session(
        user_id=int(staging["user_id"]),
        chat_id=int(staging["chat_id"]),
        server_name=server_name,
        agent_slug=agent_slug,
    )

    configs = [c for c in configs if c.get("name") not in EXCLUDED_MCP_SERVERS]

    names = {c.get("name") for c in configs}
    if "mcp-hummingbot" not in names:
        raise LiveWiringError(
            f"condor built no mcp-hummingbot server for '{server_name}' — the "
            "hummingbot tools would be missing entirely. Check the server entry "
            f"in {repo / 'config.yml'}."
        )

    api_url = str(staging["api_url"])
    for cfg in configs:
        # `uv run python -m mcp_servers.…` only resolves inside the condor
        # project. bench's cwd is its own repo, so set it explicitly rather than
        # relying on the parent process happening to be in the right directory.
        cfg["cwd"] = str(repo)
        if api_url:
            # Belt-and-suspenders over the explicit --url arg: if a future condor
            # version stops passing --url, settings.py reads HUMMINGBOT_API_URL
            # next and only then falls back to localhost:8000. Pinning it here
            # means the fallback chain can never reach localhost.
            cfg.setdefault("env", [])
            cfg["env"] = [e for e in cfg["env"] if e.get("name") != "HUMMINGBOT_API_URL"]
            cfg["env"].append({"name": "HUMMINGBOT_API_URL", "value": api_url})

    return configs


# ── Run metadata ───────────────────────────────────────────────────────────────
def effective_api_url(configs: list[dict]) -> str | None:
    """The ``--url`` the hummingbot MCP subprocess will actually be launched with."""
    for cfg in configs:
        if cfg.get("name") != "mcp-hummingbot":
            continue
        args = cfg.get("args", [])
        if "--url" in args:
            idx = args.index("--url")
            if idx + 1 < len(args):
                return str(args[idx + 1]).rstrip("/")
    return None


def _declared_mcp_servers() -> list[str]:
    """Server names in condor's .mcp.json, which ACP agents auto-discover from cwd."""
    repo = condor_path()
    if repo is None:
        return []
    mcp_json = repo / ".mcp.json"
    if not mcp_json.is_file():
        return []
    try:
        import json

        return sorted(json.loads(mcp_json.read_text()).get("mcpServers", {}))
    except Exception:
        return []


def _snapshot_tool_counts() -> dict[str, int]:
    try:
        import json

        from config import DATASETS_DIR

        snapshot = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
        return {
            name: len(spec.get("tools", {}))
            for name, spec in snapshot.get("servers", {}).items()
        }
    except Exception:
        return {}


def wiring_metadata(
    configs: list[dict],
    *,
    agent_slug: str | None,
    is_acp: bool = False,
) -> dict[str, Any]:
    """Describe the wiring a case ran under, for results + matrix comparisons.

    ``tool_count_effective`` is what the model was actually offered before the
    per-mode ``_TOOL_LIMITS`` cut, so a matrix row can tell "small model chose
    badly" apart from "small model was shown a different tool set".
    """
    counts = _snapshot_tool_counts()
    server_names = [c.get("name") for c in configs]
    tool_count = sum(counts.get(str(n), 0) for n in server_names) or None

    # The ACP path auto-discovers stdio servers from .mcp.json in its cwd. The
    # by-name overrides replace hummingbot and condor; anything else declared
    # there still joins the tool set and we cannot suppress it from this side.
    extras: list[str] = []
    if is_acp:
        extras = [
            name
            for name in _declared_mcp_servers()
            if name not in server_names and name in EXCLUDED_MCP_SERVERS
        ]
        if extras:
            log.warning(
                "ACP auto-discovery will add %s from condor/.mcp.json on top of the "
                "bench MCP servers — tool counts in this run are not comparable to "
                "the PydanticAI path.",
                ", ".join(extras),
            )

    return {
        "agent_slug": agent_slug,
        "agent_scoped": agent_slug is not None,
        "mcp_servers": server_names,
        "api_url": effective_api_url(configs),
        "server_name": _server_name_arg(configs),
        "tool_count_effective": tool_count,
        "autodiscovery_extras": extras,
    }


def _server_name_arg(configs: list[dict]) -> str | None:
    for cfg in configs:
        args = cfg.get("args", [])
        if "--server-name" in args:
            idx = args.index("--server-name")
            if idx + 1 < len(args):
                return str(args[idx + 1])
    return None


def requires_agent_slug(case: Any) -> bool:
    """True when a case is agent-scoped and must not run chat-scoped.

    Used to flag harness artifacts: a Layer 3 / tick case that ran without
    ``--agent-slug`` was measuring the wrong stores and is excluded from routing
    rather than counted as a model failure.
    """
    case_type = getattr(case, "type", "")
    if case_type in ("agent", "tick"):
        return True
    return bool(getattr(case, "agent_slug", None))


def target_banner() -> str:
    """One-line description of the API a run will hit, for CLI/dashboard display."""
    staging = staging_config()
    url = staging["api_url"] or "(HUMMINGBOT_API_URL unset)"
    mutating = "mutating allowed" if staging["allow_mutating"] else "read-only"
    return f"{url} via server '{staging['server_name']}' ({mutating})"


def env_overlay_keys(cfg: dict) -> set[str]:
    """Env var names bench adds on top of a production config (for the drift test)."""
    return {str(e.get("name")) for e in cfg.get("env", []) if isinstance(e, dict)}
