"""Post-case teardown for mutating live cases.

A benchmark that creates executors, routines and skills on staging and leaves
them there poisons every later run: the next pre-flight sees orphaned executors,
``manage_routines list`` returns a hundred ``bench_*`` entries that push the real
ones out of the model's context, and a "create" case starts colliding with its own
previous attempt.

The rule this module follows: **only delete what this run created.** Resource ids
come from the case's own tool trace, never from a "list everything and stop it"
sweep — a teardown that stopped every active executor on staging could kill
something a human was using, which is a worse failure than a dirty database.

Teardown is best-effort by design. A failure to clean up is reported, never
raised: it must not turn a scored case into an error, and the pre-flight orphan
scan is the backstop that makes leftovers visible on the next run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from metrics.tool_accuracy import normalize_tool_name

log = logging.getLogger(__name__)

# tool → (actions that create something, the arg/response key naming it, the
# action that undoes it). Only tools whose creations are cheaply reversible are
# listed; anything else is reported for manual attention instead of guessed at.
_CREATE_ACTIONS = {
    "manage_executors": {"create"},
    "manage_routines": {"create_routine", "create"},
    "manage_skill": {"create", "write"},
    "manage_memory": {"write", "create"},
    "manage_notes": {"write", "set"},
    "manage_trading_agent": {"create_strategy", "create_agent"},
    "manage_bots": {"deploy"},
    "manage_controllers": {"create", "save"},
}

# Tools that *set state* rather than create a named thing. They have no `action`
# argument, so _CREATE_ACTIONS cannot see them and _UNDO's (action, identifier)
# shape cannot reverse them. Their teardown is a second call to the same tool with a
# baseline value, carrying through whichever scoping args the original used.
#
# Leverage is the case that matters: it is real account state with no delete, so
# without this a sweep ratchets leverage upward across models and never comes back.
_STATE_SETTERS: dict[str, dict[str, Any]] = {
    "set_account_position_mode_and_leverage": {
        "leverage": 1,
        "position_mode": "ONEWAY",
    },
}

# Args carried from the original call into the reset, so it lands on the same
# account / connector / pair rather than resetting something else.
_STATE_SETTER_SCOPE = ("account_name", "connector_name", "trading_pair")

_UNDO = {
    "manage_executors": ("manage_executors", "stop"),
    "manage_routines": ("manage_routines", "delete"),
    "manage_skill": ("manage_skill", "delete"),
    "manage_memory": ("manage_memory", "delete"),
    "manage_notes": ("manage_notes", "delete"),
    "manage_trading_agent": ("manage_trading_agent", "delete_strategy"),
}

# Tools whose creations this module will not attempt to reverse. A deployed bot
# holds capital through a controller; stopping it is a trading decision, not a
# cleanup step, so it is surfaced for a human instead.
_MANUAL_ONLY = {"manage_bots", "manage_controllers"}


@dataclass
class CreatedResource:
    tool: str
    action: str
    identifier: str | None
    args: dict[str, Any] = field(default_factory=dict)
    manual_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "action": self.action,
            "identifier": self.identifier,
            "manual_only": self.manual_only,
        }


@dataclass
class CleanupReport:
    resources: list[CreatedResource] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    manual: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def clean(self) -> bool:
        return not self.failed and not self.manual

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": [r.as_dict() for r in self.resources],
            "removed": self.removed,
            "failed": self.failed,
            "manual": self.manual,
            "clean": self.clean,
            "skipped_reason": self.skipped_reason,
        }


def created_resources(result: Any) -> list[CreatedResource]:
    """Read a run's tool trace and list what it appears to have created.

    Uses the call arguments plus, when available, the tool's response — an
    executor's id is assigned server-side and only appears in the response.
    """
    responses_by_id = {
        r.get("tool_call_id"): r.get("output")
        for r in getattr(result, "tool_responses", []) or []
    }

    found: list[CreatedResource] = []
    for call in getattr(result, "tool_calls", []) or []:
        tool = normalize_tool_name(str(call.get("tool", "")))
        if tool in _STATE_SETTERS:
            setter_args = call.get("args") or {}
            if isinstance(setter_args, dict):
                found.append(
                    CreatedResource(
                        tool=tool,
                        action="set",
                        # The pair (or the connector when no pair was given) is what
                        # the reset has to target; there is no created id here.
                        identifier=str(
                            setter_args.get("trading_pair")
                            or setter_args.get("connector_name")
                            or "account"
                        ),
                        args=setter_args,
                    )
                )
            continue
        creating = _CREATE_ACTIONS.get(tool)
        if not creating:
            continue
        args = call.get("args") or {}
        if not isinstance(args, dict):
            continue
        action = str(args.get("action", "")).lower()
        # manage_executors' create is its default-ish action in some schemas, so
        # require the action to be stated rather than inferring creation.
        if action not in creating:
            continue
        identifier = _identifier(tool, args, responses_by_id.get(call.get("tool_call_id")))
        found.append(
            CreatedResource(
                tool=tool,
                action=action,
                identifier=identifier,
                args=args,
                manual_only=tool in _MANUAL_ONLY,
            )
        )
    return found


def _identifier(tool: str, args: dict, response: Any) -> str | None:
    for key in ("executor_id", "strategy_id", "agent_id", "bot_name", "name", "key", "config_name"):
        value = args.get(key)
        if value:
            return str(value)
    parsed = _as_json(response)
    if isinstance(parsed, dict):
        for key in ("executor_id", "strategy_id", "agent_id", "id", "name"):
            value = parsed.get(key)
            if value:
                return str(value)
    return None


def _as_json(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, str) and payload.strip()[:1] in ("{", "["):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


async def teardown(
    result: Any,
    model: str,
    *,
    agent_slug: str | None = None,
) -> CleanupReport:
    """Undo what a mutating case created. Best-effort; never raises.

    Runs the undo calls through a fresh MCP client with the same wiring the case
    used, so deletions land in the same stores and on the same API instance the
    creations did — a teardown pointed at a different ``agent_slug`` would delete
    nothing and report success.
    """
    report = CleanupReport(resources=created_resources(result))

    if not report.resources:
        return report

    report.manual = [r.as_dict() for r in report.resources if r.manual_only]
    reversible = [r for r in report.resources if not r.manual_only and r.identifier]
    unnamed = [r for r in report.resources if not r.manual_only and not r.identifier]
    report.manual += [
        {**r.as_dict(), "reason": "no identifier in the tool trace"} for r in unnamed
    ]

    if not reversible:
        return report

    for resource in reversible:
        undo = _UNDO.get(resource.tool)
        if undo is None and resource.tool in _STATE_SETTERS:
            undo = (resource.tool, "set")
        if undo is None:
            report.manual.append({**resource.as_dict(), "reason": "no undo action defined"})
            continue
        try:
            await _call_tool(
                undo[0],
                _undo_args(resource, undo[1]),
                agent_slug=agent_slug,
                model=model,
            )
            report.removed.append(resource.as_dict())
        except Exception as exc:
            log.warning(
                "cleanup failed for %s %s (%s): %s",
                resource.tool,
                resource.identifier,
                undo[1],
                exc,
            )
            report.failed.append({**resource.as_dict(), "error": str(exc)})

    return report


def _undo_args(resource: CreatedResource, undo_action: str) -> dict[str, Any]:
    if resource.tool in _STATE_SETTERS:
        # A reset, not a delete: baseline values plus the original scoping args. No
        # `action` key — the tool does not take one.
        args = dict(_STATE_SETTERS[resource.tool])
        for key in _STATE_SETTER_SCOPE:
            if resource.args.get(key):
                args[key] = resource.args[key]
        return args

    args: dict[str, Any] = {"action": undo_action}
    if resource.tool == "manage_executors":
        args["executor_id"] = resource.identifier
        # Stopping an executor without this flag can close the position, which is
        # a trade. Keep it: cleanup should remove bookkeeping, not move money.
        args["keep_position"] = True
        for key in ("account_name", "connector_name"):
            if resource.args.get(key):
                args[key] = resource.args[key]
    elif resource.tool == "manage_trading_agent":
        args["strategy_id"] = resource.identifier
    elif resource.tool == "manage_notes":
        args["key"] = resource.identifier
    else:
        args["name"] = resource.identifier
    return args


async def _call_tool(
    tool: str,
    args: dict[str, Any],
    *,
    agent_slug: str | None,
    model: str,
) -> Any:
    """Invoke one MCP tool directly, bypassing the model.

    Teardown must not depend on a model choosing to cooperate — especially not the
    small model whose failure to clean up is the reason cleanup exists.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from bench.mcp_provider import build_mcp_configs

    configs = build_mcp_configs(agent_slug=agent_slug)
    target = _server_for_tool(tool)
    config = next((c for c in configs if c.get("name") == target), None)
    if config is None:
        raise RuntimeError(f"no MCP server '{target}' in the live config set")

    import os

    env = dict(os.environ)
    for entry in config.get("env", []) or []:
        if isinstance(entry, dict):
            env[str(entry["name"])] = str(entry["value"])

    params = StdioServerParameters(
        command=config["command"],
        args=config.get("args", []),
        cwd=config.get("cwd"),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


_HUMMINGBOT_TOOLS = {
    "manage_executors",
    "manage_bots",
    "manage_controllers",
    "get_market_data",
    "get_portfolio_overview",
    "search_history",
}


def _server_for_tool(tool: str) -> str:
    return "mcp-hummingbot" if tool in _HUMMINGBOT_TOOLS else "condor"
