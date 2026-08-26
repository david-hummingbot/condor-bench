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
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
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
    # Leverage only, deliberately no `position_mode`.
    #
    # The reset used to send `position_mode: "ONEWAY"`, and condor's MCP layer rejects
    # that outright — `tools/trading.py:47` accepts only `HEDGE` or `ONE-WAY`, while the
    # hummingbot API behind it demands `HEDGE` or `ONEWAY`. The two disagree, so *no*
    # value satisfies both and the reset always failed: a baseline run reported
    # "left behind: set_account_position_mode_and_leverage BTC-USDT — Unknown". Leverage
    # is real account state with no delete, which is the whole reason this machinery
    # exists, so it was ratcheting upward across runs and never coming back.
    #
    # The tool documents `position_mode` as optional — "If position mode is not
    # specified, will only set the leverage" — so omitting it makes the reset work today
    # instead of waiting on the condor fix.
    "set_account_position_mode_and_leverage": {
        "leverage": 1,
    },
}

# Args carried from the original call into the reset, so it lands on the same
# account / connector / pair rather than resetting something else.
_STATE_SETTER_SCOPE = ("account_name", "connector_name", "trading_pair")

_UNDO = {
    "manage_executors": ("manage_executors", "stop"),
    # condor's action is `delete_routine`; `delete` is not one of its actions and
    # the tool answers with an error *as content*, which teardown used to record as
    # a successful removal. That is how `bench_btc_price` survived into the next
    # case's "list all routines" answer with a clean cleanup report behind it.
    "manage_routines": ("manage_routines", "delete_routine"),
    "manage_skill": ("manage_skill", "delete"),
    "manage_memory": ("manage_memory", "delete"),
    "manage_notes": ("manage_notes", "delete"),
    "manage_trading_agent": ("manage_trading_agent", "delete_strategy"),
}

# Some tools create more than one kind of thing, and the undo differs per kind.
# `manage_trading_agent` is the case that matters: `create_agent` makes an AGENT.md
# identity removed with `delete_agent(agent_slug=…)`, while `create_strategy` makes a
# playbook removed with `delete_strategy(strategy_id=…)`. Keyed on the tool alone,
# every agent this benchmark created was "cleaned up" by deleting a strategy that
# did not exist — which is why `bench_dca_sol` is still in the condor checkout.
#
# (tool, create action) -> (undo action, the argument that names the resource,
#                           keys to read the identifier from, response first)
_UNDO_BY_CREATE: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {
    ("manage_trading_agent", "create_agent"): (
        "delete_agent",
        "agent_slug",
        ("agent_slug", "slug"),
    ),
    ("manage_trading_agent", "create_strategy"): (
        "delete_strategy",
        "strategy_id",
        ("strategy_id", "id"),
    ),
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
        identifier = _identifier(
            tool, args, responses_by_id.get(call.get("tool_call_id")), action
        )
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


def _identifier(tool: str, args: dict, response: Any, action: str = "") -> str | None:
    """What to name in the undo call.

    Creations whose undo needs a server-assigned key look there *first*. An agent is
    created with a display name ("Bench DCA SOL") and deleted by the slug the tool
    returns ("bench_dca_sol"); reading the generic key order would have picked the
    display name and deleted nothing.
    """
    parsed = _as_json(response)
    preferred = _UNDO_BY_CREATE.get((tool, action), (None, None, ()))[2]
    for key in preferred:
        if isinstance(parsed, dict) and parsed.get(key):
            return str(parsed[key])
        if args.get(key):
            return str(args[key])

    for key in ("executor_id", "strategy_id", "agent_id", "bot_name", "name", "key", "config_name"):
        value = args.get(key)
        if value:
            return str(value)
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


def tool_error(outcome: Any) -> str | None:
    """The refusal in an MCP result, or None when the call really did the work.

    MCP hands a rejected call back as an ordinary result with ``isError`` set and
    the reason in its content — no exception is raised. Teardown treated anything
    that did not throw as a successful removal, so calling a *non-existent* undo
    action (``manage_routines(action="delete")``, which condor spells
    ``delete_routine``) was logged as "removed" for as long as it has been wrong.
    Nothing anywhere said otherwise: the report was clean and the routine stayed.
    """
    if outcome is None:
        return None
    if getattr(outcome, "isError", False):
        return _outcome_text(outcome) or "tool reported an error"
    text = _outcome_text(outcome)
    # FastMCP servers also answer with a plain string for an unknown action.
    if text and re.match(r"\s*(unknown|invalid|unsupported)\s+action\b", text, re.I):
        return text.strip()[:200]

    # condor's tools report a *refused* call as an ordinary result whose content is
    # a JSON body with an `error` key — no `isError`, and nothing matching the
    # "unknown action" spelling above. `delete_agent` answers that way when the agent
    # still owns a strategy, so the refusal was counted as a removal and the report
    # came back clean with the agent still on disk.
    payload = _as_json(text) if text else None
    if payload is None and isinstance(outcome, (dict, list)):
        payload = outcome
    if isinstance(payload, dict):
        message = payload.get("error")
        if message:
            return str(message)[:200]
        # A delete that matched nothing is not an error either — `delete_agent` on an
        # unknown slug returns `{"deleted": false}`. Only an explicitly *present* and
        # falsy flag counts: absence means the tool simply does not report one.
        for flag in ("deleted", "removed", "stopped", "success"):
            if flag in payload and not payload[flag]:
                return f"tool reported {flag}={payload[flag]!r} — nothing was removed"
    return None


def _outcome_text(outcome: Any) -> str:
    content = getattr(outcome, "content", None)
    if content is None:
        return str(outcome) if not isinstance(outcome, (dict, list)) else ""
    parts: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


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

    # Reverse order: the last thing created is the thing nothing else depends on.
    #
    # `agent_condor_builder_002` creates an agent and then a strategy under it, and
    # condor's `delete_agent` refuses outright while the agent still owns one
    # ("Agent 'x' still owns 1 strategy(ies). Delete its strategies first."). Undoing
    # in creation order therefore attempted the agent first, was refused, deleted the
    # strategy second, and left `bench_dca_agent` in the condor checkout — where it
    # then failed the roster drift check as an unclassified agent.
    for resource in reversed(reversible):
        by_create = _UNDO_BY_CREATE.get((resource.tool, resource.action))
        undo = (resource.tool, by_create[0]) if by_create else _UNDO.get(resource.tool)
        if undo is None and resource.tool in _STATE_SETTERS:
            undo = (resource.tool, "set")
        if undo is None:
            report.manual.append({**resource.as_dict(), "reason": "no undo action defined"})
            continue
        try:
            outcome = await _call_tool(
                undo[0],
                _undo_args(resource, undo[1]),
                agent_slug=agent_slug,
                model=model,
            )
            refusal = tool_error(outcome)
            if refusal:
                raise RuntimeError(refusal)
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
    by_create = _UNDO_BY_CREATE.get((resource.tool, resource.action))
    if by_create:
        args[by_create[1]] = resource.identifier
        return args
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


# ── the ACP agent's own memory, which teardown above cannot see ───────────────
# `created_resources` reads the MCP tool trace, so it only ever finds what a case
# built through condor's tools. An ACP agent also has a filesystem, and Claude Code
# keeps notes of its own under `~/.claude/projects/<cwd-slug>/memory/`. Nothing in
# the trace mentions those files, so nothing ever removed them.


@dataclass
class AgentMemoryReset:
    """What a pre-run reset found and moved aside."""

    directory: str = ""
    archived: list[str] = field(default_factory=list)
    archive_dir: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "archived": self.archived,
            "archive_dir": self.archive_dir,
            "error": self.error,
        }


def acp_memory_dir(project_dir: "Path | str") -> "Path":
    """Where the ACP agent keeps its own memory for a project.

    Claude Code derives this from the working directory it was launched in, with
    path separators flattened to dashes — condor at ``/home/x/dev/condor`` becomes
    ``~/.claude/projects/-home-x-dev-condor/memory``. Both bench
    (``bench.client``) and production condor (``condor.runtime.llm_client``, via
    ``get_project_dir()``) launch the bridge in the condor project root, so the two
    resolve to the same directory — which is what makes wiping it a fair
    simulation of a cold-start production agent rather than a divergence from one.

    ``CLAUDE_CONFIG_DIR`` relocates the whole tree when set.
    """
    from pathlib import Path

    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    slug = str(Path(project_dir).resolve()).replace(os.sep, "-")
    return root / "projects" / slug / "memory"


def reset_agent_memory(project_dir: "Path | str", archive_to: "Path") -> AgentMemoryReset:
    """Archive the ACP agent's own memory, then clear it. Never raises.

    A benchmark run must not depend on the one before it, and this directory made
    every run depend on all of them. `agent_directional_trader_008` asks the agent
    to remember a chop filter. In one run it wrote `feedback_chop_filter.md` here
    instead of calling `manage_memory`; in the next it read that file back, said
    "already saved, no changes needed", called nothing, and failed the
    `manage_memory` post-condition. The failure is self-reinforcing — once the note
    exists the case can never pass again, because declining to duplicate it is the
    correct response — and the poisoned set grows run over run, which is why
    post-condition failures went from two to four between the last two runs.

    Files are archived before they are removed, never deleted outright. The
    directory belongs to the user's Claude Code install, not to bench: today its
    contents are all bench artifacts, but anyone who uses Claude Code on the condor
    checkout would have real notes here, and a benchmark has no business destroying
    them. The archive lands with the run, so a result also records the state it
    started from.

    Note what this does *not* fix: the agent will write to its own memory again on
    the next run and fail the post-condition again. That is the honest signal, and
    it is a finding about condor rather than about bench — condor scopes memory per
    agent and per user (``agents/<slug>/store/user_<id>/memories``) while Claude
    Code scopes it per project directory, so a Claude-Code-backed agent told to
    remember something writes to a store shared across every agent and every user,
    that condor itself cannot read back.
    """
    from pathlib import Path

    reset = AgentMemoryReset()
    try:
        memory = acp_memory_dir(project_dir)
        reset.directory = str(memory)
        if not memory.is_dir():
            return reset
        files = sorted(p for p in memory.iterdir() if p.is_file())
        if not files:
            return reset

        archive = Path(archive_to)
        archive.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, archive / path.name)
        # Only unlink once every file is safely copied.
        for path in files:
            path.unlink()

        reset.archived = [p.name for p in files]
        reset.archive_dir = str(archive)
    except Exception as exc:  # never let housekeeping fail a run
        log.warning("agent memory reset failed: %s", exc)
        reset.error = str(exc)
    return reset
