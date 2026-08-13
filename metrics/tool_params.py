"""Tool Param metric: did the model call the right tool with the right arguments?

Ported from condor-evals' ``metrics/tool_correctness.py`` param half, minus its
DeepEval dependency. Tool *name* F1 already lives in ``metrics/tool_accuracy.py``;
this scores the arguments, which is where small models actually fail. A 3B model
will happily call ``get_market_data`` and then ask for ``BTCUSD`` on ``binance``
with no ``data_type`` — a name-only metric scores that 1.0.

Matching is deliberately loose about representation and strict about meaning:

* ``"1.5"`` matches ``1.5`` — providers hand back JSON-stringified args, and a
  model that emitted the right number shouldn't lose points to the transport.
* ``"BTC-USDT"`` matches ``["BTC-USDT"]``, and a pinned ``trading_pair`` is
  satisfied by ``trading_pairs`` — several condor tools expose both the singular
  and plural spelling of the same parameter, and a model that chose the other one
  has not made a mistake.
* ``"binance"`` matches ``"Binance"`` for strings, since connector and account
  names are case-insensitive on the API side.
* ``{"a": 1}`` inside an expected value must match as a subset, so a case can
  pin one key of a nested config without freezing the whole object.

Nothing here rewards extra arguments and nothing penalises them either: a model
passing an additional valid filter is not wrong, and the dataset can't enumerate
every legitimate call shape.
"""

from __future__ import annotations

import json
from typing import Any

from metrics.tool_accuracy import normalize_tool_name

# Parameters condor's schemas expose in both a singular and a plural form for the
# same intent. Looking up only the pinned spelling would score a correct call 0
# whenever the model picked the other one — which for get_market_data is roughly a
# coin flip, since both are in the schema.
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "trading_pair": ("trading_pairs",),
    "trading_pairs": ("trading_pair",),
    "connector_name": ("connector_names",),
    "connector_names": ("connector_name",),
    "account_name": ("account_names",),
    "account_names": ("account_name",),
    "controller_id": ("controller_ids",),
    "controller_ids": ("controller_id",),
    "executor_type": ("executor_types",),
    "executor_types": ("executor_type",),
    "controller_name": ("controller_names",),
    "controller_names": ("controller_name",),
    "pool_address": ("pool_addresses",),
    "pool_addresses": ("pool_address",),
}


def _lookup(args: dict[str, Any], key: str) -> Any:
    """Read a pinned key from a call's args, accepting aliases and one nesting level.

    Several tools take their real payload in a sub-object: `manage_executors(action=
    "create")` wants `connector_name` and `trading_pair` inside `executor_config`,
    where the top-level keys of the same name are the *filter* args for
    `positions_summary`. Reading only the top level scored the correct call
    `actual: null` on both keys — `tool_manage_executors_002` lost the whole
    tool_params weight for putting the values exactly where the tool documents them.

    The descent is one level and stops at the first hit, so a pin can be written in
    either shape and still means the same thing.
    """
    if key in args:
        return args[key]
    for alias in _KEY_ALIASES.get(key, ()):
        if alias in args:
            return args[alias]
    names = (key, *_KEY_ALIASES.get(key, ()))
    for value in args.values():
        nested = _as_dict(value)
        if not nested:
            continue
        for name in names:
            if name in nested:
                return nested[name]
    return None


class ToolParamMetric:
    name = "Tool Params"

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    def score(
        self,
        tool_calls: list[dict[str, Any]],
        expected_params: dict[str, dict[str, Any]],
    ) -> float | None:
        """Mean per-tool param match across every tool the case pins.

        ``tool_calls`` is the bench trace: ``[{"tool": name, "args": {...}}, …]``.
        Returns ``None`` when the case pins no params — the caller redistributes
        that weight rather than crediting an unearned 1.0.
        """
        if not expected_params:
            return None

        scores = [
            self.score_one(tool_calls, tool_name, expected)
            for tool_name, expected in expected_params.items()
        ]
        return sum(scores) / len(scores) if scores else None

    def score_one(
        self,
        tool_calls: list[dict[str, Any]],
        tool_name: str,
        expected: dict[str, Any],
    ) -> float:
        """Fraction of pinned keys satisfied by the best matching call.

        Best-of, not first-of: a model that calls ``manage_executors`` to list and
        then again to create should be scored on the call the case is about.
        """
        if not expected:
            return 1.0

        candidates = [
            call.get("args") or {}
            for call in tool_calls
            if normalize_tool_name(str(call.get("tool", "")))
            == normalize_tool_name(tool_name)
        ]
        if not candidates:
            # The tool was never called. That is a tool-accuracy failure; scoring
            # it 0 here too is correct — the params were not right either.
            return 0.0

        best = 0.0
        for args in candidates:
            args = _as_dict(args)
            hits = sum(1 for k, v in expected.items() if _matches(_lookup(args, k), v))
            best = max(best, hits / len(expected))
        return best

    def passes(self, score: float) -> bool:
        return score >= self.threshold


def param_breakdown(
    tool_calls: list[dict[str, Any]],
    expected_params: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Per-key detail for the dashboard: which pinned args matched and which didn't."""
    detail: dict[str, Any] = {}
    for tool_name, expected in expected_params.items():
        candidates = [
            _as_dict(call.get("args") or {})
            for call in tool_calls
            if normalize_tool_name(str(call.get("tool", "")))
            == normalize_tool_name(tool_name)
        ]
        if not candidates:
            detail[tool_name] = {"called": False, "missing": sorted(expected)}
            continue
        # Report against the call that scored best, matching score_one().
        best_args, best_hits = candidates[0], -1
        for args in candidates:
            hits = sum(1 for k, v in expected.items() if _matches(_lookup(args, k), v))
            if hits > best_hits:
                best_args, best_hits = args, hits
        detail[tool_name] = {
            "called": True,
            "matched": sorted(
                k for k, v in expected.items() if _matches(_lookup(best_args, k), v)
            ),
            "mismatched": {
                k: {"expected": v, "actual": _lookup(best_args, k)}
                for k, v in expected.items()
                if not _matches(_lookup(best_args, k), v)
            },
        }
    return detail


def _as_dict(args: Any) -> dict[str, Any]:
    """Tool args as a dict — OpenAI-compatible providers deliver them as JSON text."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _matches(actual: Any, expected: Any) -> bool:
    """True when ``actual`` satisfies ``expected`` under the rules in the docstring."""
    if actual is None:
        # An expected None means "must be absent or null", which None satisfies.
        return expected is None

    if isinstance(expected, bool) or isinstance(actual, bool):
        # Checked before numbers, and identity-compared, because in Python
        # ``True == 1``. A model that passed 1 where the schema declares a boolean
        # made a differently-typed call, and several hummingbot-api endpoints
        # reject it — so it must not score as a match.
        actual_bool = _coerce_bool(actual)
        expected_bool = _coerce_bool(expected)
        if not isinstance(actual_bool, bool) or not isinstance(expected_bool, bool):
            return False
        return actual_bool is expected_bool

    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        actual_num = _coerce_number(actual)
        return actual_num is not None and float(expected) == actual_num

    if isinstance(expected, str):
        if isinstance(actual, (list, tuple)):
            # Scalar-vs-list tolerance: several tools accept trading_pair or
            # trading_pairs for the same intent.
            return any(_matches(item, expected) for item in actual)
        return _norm_text(actual) == _norm_text(expected)

    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            # One expected element can be satisfied by the scalar form.
            return len(expected) == 1 and _matches(actual, expected[0])
        # Order-insensitive: the API does not care which pair came first.
        remaining = list(actual)
        for want in expected:
            for i, got in enumerate(remaining):
                if _matches(got, want):
                    remaining.pop(i)
                    break
            else:
                return False
        return True

    if isinstance(expected, dict):
        actual_dict = _as_dict(actual)
        if not actual_dict:
            return False
        # Subset match, so a case can pin one key of a nested config.
        return all(_matches(actual_dict.get(k), v) for k, v in expected.items())

    return actual == expected


def _norm_text(value: Any) -> str:
    """Compare strings ignoring case, surrounding space and trailing sentence marks.

    Free-text pins were punctuation-exact: `tool_send_notification_002` pins
    `text="funding check complete"` and the model sent "funding check complete." —
    copying the full stop from the question's own sentence — which scored 0.0. The
    sister case only escaped by luck. Trailing `.` and `!` never change what a pinned
    value *means*, so they are not a thing to test.
    """
    return str(value).strip().rstrip(".!").strip().casefold()


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return value
