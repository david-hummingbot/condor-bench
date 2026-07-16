"""Prompt builder for trading agent ticks.

Vendored from condor/condor/agents/prompts.py with two changes:
  1. Agent/Strategy type annotations replaced with Any (no condor model imports).
  2. is_pydantic_ai_model imported from condor_compat instead of condor.

Source: https://github.com/hummingbot/condor
"""

from __future__ import annotations

from typing import Any

BASE_PROMPT_LIVE = """\
You are an autonomous trading agent running inside Condor.

RULES:
- Trade ONLY via manage_executors(action="create"). NEVER use place_order.
- Be conservative. When in doubt, hold and journal why.
- Never claim an executor was created/stopped unless the tool result confirms it.

ERROR RECOVERY:
- If manage_executors(action="create") fails, call manage_executors(executor_type="<type>") \
to fetch the full config schema, compare it against what you sent, fix the missing/wrong \
fields, and retry ONCE. Journal the error and fix as a learning.
- Pass controller_id as a TOP-LEVEL argument to manage_executors (not nested inside \
executor_config). Example shape: manage_executors(action="create", executor_type="grid_strike", \
controller_id="<agent_id>", connector_name="binance", trading_pair="SOL-USDT", \
total_amount_quote=..., min_price=..., max_price=..., n_levels=...).
"""

BASE_PROMPT_DRY_RUN = """\
You are an autonomous trading agent running inside Condor in 🧪 DRY RUN mode.

RULES:
- This is OBSERVATION ONLY. Do NOT create or stop executors.
- manage_executors is available for read-only queries (performance_report).
- Analyze the market and describe what you WOULD do, but take NO trading action.

DRY RUN MESSAGING:
- Use conditional language: "Would place grid..." not "Grid placed"
- Prefix actions with 🧪 to signal dry-run
- End with: "No executors were created (dry run)"
"""

BASE_PROMPT_COMMON = """\
GENERAL:
- The mcp-hummingbot server is pre-configured. Do NOT call configure_server.
- Keep tool chains short (1-5 calls per tick).
- Your executor state and positions are pre-loaded in [CORE DATA] below — no need to query them.

SKILLS & ROUTINES:
- [AVAILABLE SKILLS & ROUTINES] below lists SKILLS (playbooks — know-how: when to
  act + steps) and ROUTINES (executable scripts).
- Before a known flow, read the relevant playbook with manage_skill(action="read",
  name="...") and follow it instead of re-deriving the procedure.
- A skill may reference a routine (shown as "→ routine: <name>"); run it with
  manage_routines(action="run", name="...", config={...}). manage_routines(action="list")
  to discover routines; routines tagged "agent" are local to your strategy.
- Skills are read-only playbooks shipped with this agent — follow them, you can't
  create or edit them. Operational facts you learn go to [LEARNINGS] (journal).

MEMORY (about the user, NOT operational learnings):
- [USER MEMORY] below is what is known about the OWNER (preferences, profile).
  This is distinct from [LEARNINGS] (market/execution), which go to the journal.
- Read detail with manage_memory(action="read", name="...").
- If you learn something new and stable about the USER (a standing preference,
  a profile fact, a correction), save it with manage_memory(action="write",
  name="short-name", description="one line", content="...", type="preference|fact").
  Operational/market learnings go to the journal (see JOURNAL above), NOT here.

NOTIFICATIONS:
- Use send_notification(text="...") to message the user on Telegram.
"""

JOURNAL_SECTION_LIVE = """\
JOURNAL:
- Write ONE action entry per tick via trading_agent_journal_write(entry_type="action"). One line.
- Learnings must specify a category: "market" or "execution".
  trading_agent_journal_write(entry_type="learning", category="market|execution", text="...")
  - market: band behavior, volatility regimes, S/R patterns, routine observations.
  - execution: executor errors, schema issues, fill problems, timing.
- Keep learnings factual and short (1 line). No speculation.
- Only write a learning if it's genuinely NEW. Duplicates are auto-filtered.
- Do NOT call trading_agent_journal_read — context is already in this prompt.
"""

JOURNAL_SECTION_EXPERIMENT = """\
JOURNAL:
- This is an experiment (dry-run / run-once): there is NO journal this tick.
- Do NOT call trading_agent_journal_write or trading_agent_journal_read — they are
  unavailable here and will error.
- Put all observations, reasoning, and what you WOULD record straight into your
  response. The full tick is saved automatically as a dry-run snapshot.
"""


def _build_tool_preload(*, is_dry_run: bool, is_experiment: bool) -> str:
    tools = ["mcp__mcp-hummingbot__get_market_data"]
    if not is_dry_run:
        tools.append("mcp__mcp-hummingbot__manage_executors")
    tools += [
        "mcp__mcp-hummingbot__search_history",
        "mcp__mcp-hummingbot__explore_geckoterminal",
    ]
    if not is_experiment:
        tools.append("mcp__condor__trading_agent_journal_write")
    tools += [
        "mcp__condor__send_notification",
        "mcp__condor__manage_memory",
        "mcp__condor__manage_skill",
        "mcp__condor__manage_routines",
    ]
    return (
        "IMPORTANT: At the very start, load ALL MCP tools in a single ToolSearch call:\n"
        f'ToolSearch(query="select:{",".join(tools)}")\n'
        "Do this silently."
    )


def build_tick_prompt(
    agent: Any,
    strategy: Any,
    config: dict[str, Any],
    core_data: dict[str, str],
    learnings: str,
    summary: str,
    recent_decisions: str,
    risk_state: dict[str, Any],
    tick_number: int = 1,
    agent_id: str = "",
    cached_routines_section: str | None = None,
    user_memory: str = "",
    skills_index: str = "",
) -> str:
    """Build the full prompt for one agent tick."""
    from condor_compat.acp.pydantic_ai_client import is_pydantic_ai_model

    execution_mode = config.get("execution_mode", "loop")
    is_dry_run = execution_mode == "dry_run"
    is_experiment = execution_mode in ("dry_run", "run_once")
    agent_key = config.get("agent_key") or getattr(strategy, "agent_key", None) or getattr(agent, "agent_key", "")
    use_pydantic_ai = is_pydantic_ai_model(agent_key)

    base_prompt = BASE_PROMPT_DRY_RUN if is_dry_run else BASE_PROMPT_LIVE
    journal_section = (
        JOURNAL_SECTION_EXPERIMENT if is_experiment else JOURNAL_SECTION_LIVE
    )
    sections: list[str] = [base_prompt, journal_section, BASE_PROMPT_COMMON]

    if not use_pydantic_ai:
        sections.append(
            _build_tool_preload(is_dry_run=is_dry_run, is_experiment=is_experiment)
        )
    else:
        sections.append(
            "TOOLS:\n"
            "All MCP tools are pre-loaded and available. Call them directly by name."
        )

    tick_info = f"[TICK INFO]\nThis is tick #{tick_number}. Use this number in journal entries and notifications."
    if agent_id:
        tick_info += f"\nAgent ID: {agent_id}"
        if not is_dry_run:
            tick_info += f'\nPass controller_id="{agent_id}" as a TOP-LEVEL arg to manage_executors (not inside executor_config).'
    sections.append(tick_info)

    if execution_mode == "run_once":
        sections.append(
            "[EXECUTION MODE — RUN ONCE]\n"
            "Single-tick session with LIVE execution. The engine will stop after this tick. "
            "Make your best move now — there will be no follow-up ticks."
        )

    agent_instructions = getattr(agent, "instructions", "")
    if agent_instructions.strip():
        sections.append(f"[AGENT — domain identity & knowledge]\n{agent_instructions}")
    sections.append(f"[STRATEGY INSTRUCTIONS]\n{strategy.instructions}")

    # Skills + routines section (pass cached_routines_section="" to skip discovery)
    routines_section = cached_routines_section if cached_routines_section is not None else ""
    skills_routines = ["[AVAILABLE SKILLS & ROUTINES]"]
    if skills_index:
        skills_routines.append(
            "\nSKILLS — playbooks (read before a known flow with "
            'manage_skill(action="read", name="..."); "→ routine:" links to an '
            "executable routine):\n"
            f"{skills_index}"
        )
    if routines_section:
        skills_routines.append(f"\n{routines_section}")
    sections.append("\n".join(skills_routines))

    trading_context = config.get("trading_context", "")
    if trading_context:
        sections.append(
            "[SESSION CONTEXT]\n"
            "The user provided the following natural language context for this trading session. "
            "Use this to guide your market selection, risk appetite, and trading style:\n\n"
            f"{trading_context}"
        )

    _CONFIG_EXCLUDE = {
        "trading_context", "risk_limits", "agent_key",
        "server_name", "frequency_sec", "execution_mode",
    }
    config_lines = [
        "[CURRENT CONFIG]",
        "These are the ACTIVE values for this session. If the strategy instructions mention different defaults, IGNORE them and use these values instead.",
    ]
    for k, v in config.items():
        if k in _CONFIG_EXCLUDE:
            continue
        config_lines.append(f"{k}: {v}")
    sections.append("\n".join(config_lines))

    bot_name = config.get("bot_name", "")
    if bot_name:
        sections.append(
            "[CONTROLLER MODE]\n"
            f"You operate the Hummingbot bot '{bot_name}'. Steer its controllers "
            "instead of creating standalone executors:\n"
            '- Check current state first: manage_bots(action="status").\n'
            "- Define/update controller config templates with manage_controllers.\n"
            f"- Apply them with manage_bots: deploy if '{bot_name}' is not running, "
            "otherwise update_config / start_controllers / stop_controllers.\n"
            "Do NOT create standalone executors unless the strategy instructions "
            "explicitly tell you to. The bot's PnL is attributed to you automatically."
        )

    rs = risk_state
    max_dd = rs.get("max_drawdown_pct", -1)
    dd_display = (
        f"{rs.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
        if max_dd >= 0
        else "disabled"
    )
    risk_lines = [
        "[RISK STATE]",
        f"Position Size: ${rs.get('total_exposure', 0):.2f} / ${rs.get('max_position_size', 500):.2f} limit",
        f"Open Executors: {rs.get('executor_count', 0)} / {rs.get('max_open_executors', 5)} limit",
        f"Drawdown: {dd_display}",
        f"Status: {'BLOCKED - ' + rs.get('block_reason', '') if rs.get('is_blocked') else 'ACTIVE'}",
    ]
    sections.append("\n".join(risk_lines))

    for name, data_summary in core_data.items():
        sections.append(f"[CORE DATA - {name}]\n{data_summary}")

    if user_memory:
        sections.append(
            "[USER MEMORY — what is known about the owner; advisory]\n"
            'Read detail with manage_memory(action="read", name="...").\n\n'
            f"{user_memory}"
        )

    if learnings:
        sections.append(
            f"[LEARNINGS — do NOT repeat these, only add genuinely new insights]\n{learnings}"
        )
    if summary:
        sections.append(f"[CURRENT STATUS]\n{summary}")
    if recent_decisions:
        sections.append(f"[RECENT DECISIONS — last 3 snapshots]\n{recent_decisions}")

    return "\n\n".join(sections)
