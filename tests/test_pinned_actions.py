"""Every pinned ``action`` must be one the tool actually accepts.

Eight cases pinned an action that does not exist — ``manage_trading_agent`` with
``"list"`` (its actions are ``list_agent_definitions`` / ``list_strategies`` /
``list_agents``), ``manage_notes`` with ``"write"`` (it is ``set``),
``manage_bots`` with ``"list"`` (borrowed from ``manage_controllers``), and
``explore_geckoterminal`` with ``"search"`` (no such action on that tool).

The failure mode is invisible and total. ``action`` is usually the only pinned key,
so ``tool_params`` is capped at 0.0 no matter what the model does — a flat 0.15 off
the composite of every affected case, indistinguishable from a model that got the
parameters wrong. c011 lost exactly that while calling the action condor's own
docstring names as the right answer to the question.

The vocabulary is read from condor's ``Literal[...]`` annotations, which is what
FastMCP validates against, so this test tracks upstream rather than a copy of it.
"""

from __future__ import annotations

import ast
import re

import pytest

from bench.dataset import load_all_cases
from config import condor_path


def _action_vocabulary() -> dict[str, set[str]]:
    """tool name -> the actions it accepts.

    Two declaration styles, and both have to be read or the check has blind spots.
    Most tools constrain ``action`` with ``Literal[...]``, which FastMCP validates
    against. ``manage_trading_agent`` types it as a bare ``str`` and enumerates the
    actions in its docstring instead — which is exactly where the ``"list"`` pin
    hid, since a Literal-only reader has nothing to compare against for that tool.
    """
    repo = condor_path()
    if repo is None:
        return {}
    vocab: dict[str, set[str]] = {}
    for path in (repo / "mcp_servers").rglob("*.py"):
        try:
            source = path.read_text(errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            takes_action = any(
                arg.arg == "action"
                for arg in list(node.args.args) + list(node.args.kwonlyargs)
            )
            if not takes_action:
                continue
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.arg != "action" or arg.annotation is None:
                    continue
                literals = {
                    el.value
                    for el in ast.walk(arg.annotation)
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                }
                if literals:
                    vocab.setdefault(node.name, set()).update(literals)
            # Docstrings document actions as:  - "action_name": description
            doc = ast.get_docstring(node) or ""
            documented = set(re.findall(r'^\s*-\s*"([\w.]+)"\s*:', doc, re.M))
            if documented:
                vocab.setdefault(node.name, set()).update(documented)
    return vocab


def test_every_pinned_action_exists_on_its_tool():
    vocab = _action_vocabulary()
    if not vocab:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    offenders = []
    for case in load_all_cases():
        for tool, params in (getattr(case, "expected_tool_params", {}) or {}).items():
            action = (params or {}).get("action")
            if not isinstance(action, str):
                continue
            allowed = vocab.get(tool)
            if allowed and action not in allowed:
                offenders.append(
                    f"{case.id}: {tool}(action={action!r}) — accepts "
                    f"{', '.join(sorted(allowed))}"
                )

    assert not offenders, (
        "cases pin an action their tool does not accept, so tool_params can never "
        "be earned:\n  " + "\n  ".join(offenders)
    )


def _literal_vocabulary() -> dict[str, dict[str, set[str]]]:
    """tool name -> {parameter: the string values its Literal allows}.

    The sibling above reads only ``action``, which left every other
    Literal-constrained parameter unguarded — and c014 pinned
    ``get_market_data(data_type="funding_info")`` against a signature that spells the
    options ``Literal["prices", "candles", "funding_rate", "order_book"]``. condor
    answers an unknown value with "Error: Invalid data_type", so the pin was
    unearnable and cost a flat 0.15 of composite on every correct answer — the same
    invisible, total failure the ``action`` check was written for, one parameter over.

    Only tools bench actually pins are relevant, but the vocabulary is collected for
    every ``mcp_servers`` function so a new pin is covered the moment it is written.
    """
    repo = condor_path()
    if repo is None:
        return {}
    vocab: dict[str, dict[str, set[str]]] = {}
    for path in (repo / "mcp_servers").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.annotation is None:
                    continue
                # Only annotations that *are* a Literal constrain the value. A bare
                # `str` does not, and a `dict[str, Any]` holding string constants
                # elsewhere in the tree must not be read as a vocabulary.
                if not any(
                    isinstance(n, ast.Name) and n.id == "Literal"
                    for n in ast.walk(arg.annotation)
                ):
                    continue
                literals = {
                    el.value
                    for el in ast.walk(arg.annotation)
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                }
                if literals:
                    vocab.setdefault(node.name, {}).setdefault(arg.arg, set()).update(
                        literals
                    )
    return vocab


def test_every_pinned_literal_value_exists_on_its_tool():
    """Not just ``action``: any parameter condor constrains with a Literal."""
    vocab = _literal_vocabulary()
    if not vocab:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    offenders = []
    for case in load_all_cases():
        for tool, params in (getattr(case, "expected_tool_params", {}) or {}).items():
            allowed_by_param = vocab.get(tool) or {}
            for key, value in (params or {}).items():
                if not isinstance(value, str):
                    continue
                allowed = allowed_by_param.get(key)
                if allowed and value not in allowed:
                    offenders.append(
                        f"{case.id}: {tool}({key}={value!r}) — accepts "
                        f"{', '.join(sorted(allowed))}"
                    )

    assert not offenders, (
        "cases pin a value their tool's Literal does not allow, so tool_params can "
        "never be earned:\n  " + "\n  ".join(offenders)
    )


def _parameter_defaults() -> dict[str, dict[str, object]]:
    """tool name -> {parameter: default value} from condor's signatures."""
    repo = condor_path()
    if repo is None:
        return {}
    defaults: dict[str, dict[str, object]] = {}
    for path in (repo / "mcp_servers").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults))
            pairs += [
                (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
            ]
            for arg, node_default in pairs:
                try:
                    value = ast.literal_eval(node_default)
                except (ValueError, TypeError, SyntaxError):
                    continue
                defaults.setdefault(node.name, {})[arg.arg] = value
    return defaults


def test_no_pin_merely_restates_a_tool_default():
    """A pin a model earns by *omission* measures nothing and costs it everything.

    ``c001`` pinned ``get_portfolio_overview(include_balances=True)``. That is the
    tool's own default, so a model that calls the tool bare gets exactly the data
    the case wants — and scored ``tool_params`` 0.0 for not restating it, while its
    answer and live validity were perfect. Pin a parameter when a *choice* is being
    tested (which pair, which action), not when the default already decides it.
    """
    defaults = _parameter_defaults()
    if not defaults:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    offenders = []
    for case in load_all_cases():
        for tool, params in (getattr(case, "expected_tool_params", {}) or {}).items():
            for key, value in (params or {}).items():
                if key == "action":
                    continue
                tool_defaults = defaults.get(tool, {})
                if key in tool_defaults and tool_defaults[key] == value:
                    offenders.append(
                        f"{case.id}: {tool}.{key}={value!r} is already the default"
                    )

    assert not offenders, (
        "pins that restate a tool default — a model is penalised for relying on "
        "the default it was designed to rely on:\n  " + "\n  ".join(offenders)
    )


def test_the_vocabulary_reader_finds_real_tools():
    """Guard the guard: a parser that silently finds nothing would pass forever."""
    vocab = _action_vocabulary()
    if not vocab:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    assert "manage_bots" in vocab, sorted(vocab)[:20]
    assert "status" in vocab["manage_bots"]
    assert "list" not in vocab["manage_bots"], (
        "'list' belongs to manage_controllers — if it appears here the parser is "
        "merging tools and the check above is worthless"
    )
    assert "list_agent_definitions" in vocab.get("manage_trading_agent", set())
