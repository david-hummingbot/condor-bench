"""GENERATED — do not edit. Vendored from condor by scripts/revendor.py.

Source: condor/runtime/context.py (chat preload only)
condor:  d89e74f2-dirty (main)

Imports are rewritten to condor_compat/upstream_shims.py; the prompt text itself
is byte-for-byte condor's. Bench's additions are appended below the marker at the
end of this file — edit condor_compat/agents/_bench_prompts.py for those.

Re-pull with:  uv run python scripts/revendor.py
"""
from __future__ import annotations


#: The ring the preload names. Attended chat seats mount ``agent`` (a bound
#: specialist) or ``full`` (the coordinator) — see :func:`toolsets.seat_profile`
#: — and ``agent`` is the common subset. What ``full`` adds on top is the admin
#: ring, which is the *server owner's* and which this very prompt goes on to
#: forbid ("do NOT call configure_server"), so naming it here would advertise
#: three tools most seats are downgraded out of anyway (SEC-252).
_CHAT_PRELOAD_PROFILE = "agent"

#: ``(MCP server name, the leaf module that says which tools it mounts)``.
#: The prefix is the ACP naming convention, ``mcp__<server>__<tool>``; the server
#: names are the ones :mod:`condor.runtime.toolsets` spawns each subprocess under.
_CHAT_MCP_SERVERS = ("condor", "mcp-hummingbot")

def _chat_mcp_tools() -> tuple[str, ...]:
    """Every MCP tool an attended chat seat has mounted, as ACP tool names.

    Derived, never restated. Under ACP these tools arrive *deferred*: a session
    sees only the names it has been told about, so a tool missing from this line
    is one that seat is unlikely to ever reach for — a keyword ``ToolSearch``
    could still surface it, but only if the model thinks to run one, and the
    whole point of the preload is that it does not have to. A hand-kept copy of
    the list therefore fails quietly and in the expensive direction, which is
    exactly how the previous two copies rotted: the pre-ARCH-190 list in
    ``handlers/agents/_shared.py`` still named five tools the servers had already
    removed, and the tuple this function replaced was missing thirteen it
    mounted, ``manage_clmm`` and ``list_orphaned_positions`` among them — both
    called by the ``recover_orphaned_position`` playbook every agent inherits.

    Names come from ``mcp_servers.*.profiles`` for the same reason
    :func:`toolsets.seat_tools` reads them there: those are leaf string modules,
    the definition site of each ring, and importing a ``server.py`` to ask would
    parse argv and build a ``FastMCP`` singleton as a side effect. The import is
    function-local to keep that dependency off ``context``'s import path.

    The tick seat keeps its own, deliberately narrower list in
    ``condor/agents/prompts.py`` — a tick must not be able to start or stop the
    loop it is running inside. Do not unify them.
    """
    from condor_compat.mcp_servers import condor_profiles
    from condor_compat.mcp_servers import hummingbot_profiles

    return tuple(
        f"mcp__{server}__{name}"
        for server, module in zip(
            _CHAT_MCP_SERVERS, (condor_profiles, hummingbot_profiles)
        )
        for name in module.PROFILE_TOOLS[_CHAT_PRELOAD_PROFILE]
    )

def chat_tool_preload(agent_key: str | None, agent_slug: str | None = None) -> str:
    """The ToolSearch preload line for a chat seat, or ``""`` when it needs none.

    ACP seats (Claude Code and friends) get MCP tools deferred: they must
    ``ToolSearch`` a name before they can call it. A keyword search can still
    find a name this line omits, so the preload is not the only route to a
    mounted tool — what it buys is the round trips, and the far likelier failure
    that the model never thinks to search at all. Pydantic-ai seats auto-discover
    their toolset and must never receive the line.

    Public because both chat branches need it: the coordinator's
    :func:`build_initial_context` and the specialist's ``bound_agent_context``,
    which skips that builder entirely (CORR-272).

    It names exactly what the seat mounts: the ring, minus what
    :func:`~condor.runtime.toolsets.seat_mutes` subtracts for ``agent_slug`` —
    the operator's mutes and whatever the Agent's allowlist leaves out. Naming
    the whole ring cost a Claude specialist ~39k tokens of schemas on its first
    turn, most of them for tools its allowlist never meant it to have. ``None``
    is the coordinator, whose mutes live under the chat's slug.
    """
    from condor_compat.acp.pydantic_ai_client import is_pydantic_ai_model
    from condor_compat.upstream_shims import seat_mutes

    if not agent_key or is_pydantic_ai_model(agent_key):
        return ""
    muted = set(seat_mutes(agent_slug))
    tools = [t for t in _chat_mcp_tools() if t.rsplit("__", 1)[-1] not in muted]
    return (
        "IMPORTANT: At the very start of the session (before your first response), "
        "load ALL MCP tools in a single ToolSearch call:\n"
        f'ToolSearch(query="select:{",".join(tools)}")\n'
        "This avoids repeated ToolSearch calls that waste context tokens. "
        "Do this silently without telling the user."
    )


def build_consult_preload(agent_slug: str | None, agent_key: str) -> str:
    """The chat seat's preload for a bench consult case.

    Consult cases are chat-shaped, so they must get the *chat* preload — the 42
    tools ``chat_tool_preload`` names — and not the tick seat's deliberately
    narrower ring, which condor says in as many words must not be unified with
    it. Running a consult on the tick preload is what cost the portfolio case a
    second ToolSearch: ``get_portfolio_overview`` is not in the tick list.
    """
    return chat_tool_preload(agent_key, agent_slug)
