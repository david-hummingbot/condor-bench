"""Live Validity metric: did the tool calls actually work against the real API?

What it catches that no other metric does: a model that picks the right tool with
plausible-looking arguments and gets an error back every time. Tool-name F1 scores
that 1.0, param match can score it 1.0 too (the pinned keys were right; an
unpinned one was wrong), and the judge only sees whatever the model said
afterwards — which is often a confident summary of data it never received. In a
model-sizing study that failure mode is exactly the one worth measuring: it is how
a small model looks competent while accomplishing nothing.

Two inputs:

* the tool responses captured from the run (``{"tool": …, "output": …}``)
* the case's optional ``live_expected`` assertions, e.g.::

      "live_expected": {"get_market_data": {"nonempty": true,
                                            "contains": ["mid_price"]}}

Score is the mean over responses of "this response was usable", with the
per-tool assertions folded in. ``None`` when there is nothing to judge.
"""

from __future__ import annotations

import json
import re
from typing import Any

from metrics.tool_accuracy import normalize_tool_name

# Substrings that mark an MCP/HTTP failure surfaced as tool *content* rather than
# as a protocol error. FastMCP hands these back as ordinary text, so a caller that
# only checks for exceptions sees a successful call returning prose.
_ERROR_PATTERNS = (
    # `\berror\b\s*[:=]` only catches "Error:" with nothing between. The failures that
    # actually mattered in the last run all put words there or none at all, so hard
    # failures scored a perfect 1.0 while a *recoverable* "Error: Failed to get schema"
    # scored 0.0 — the metric was inverted exactly where it counts:
    #   "Error creating executor: 500, message=... missing 2 required positional
    #    arguments: 'binance_api_key'"   → absent keys, scored 1.0
    #   "Error executing tool consult: API error (401): Invalid token" → scored 1.0
    re.compile(r"\berror\b(?:\s+\w+){0,4}\s*[:=]", re.I),
    re.compile(r"\bapi\s+error\b\s*\(?\s*\d{3}", re.I),
    re.compile(r"\b[45]\d{2}\s*,\s*message\s*=", re.I),
    re.compile(r"\binvalid\s+token\b", re.I),
    re.compile(r"\bmissing\s+\d+\s+required\b", re.I),
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"\b(?:4\d{2}|5\d{2})\s+(?:client|server)?\s*error", re.I),
    re.compile(r"\bunauthoriz(?:ed|ation)\b", re.I),
    re.compile(r"\bnot\s+found\b", re.I),
    re.compile(r"\bconnection\s+(?:refused|error|reset)\b", re.I),
    re.compile(r"\btimed?\s*out\b", re.I),
    re.compile(r"\bvalidation\s+error\b", re.I),
    re.compile(r"\bmissing\s+required\b", re.I),
)

# Payloads that are structurally fine but carry nothing — an empty list where the
# case expected market data means the call did not do what the model thought.
_EMPTY_FORMS = ("", "[]", "{}", "null", "none", "no data", "n/a")


class LiveValidityMetric:
    name = "Live Validity"

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    def score(
        self,
        tool_responses: list[dict[str, Any]],
        live_expected: dict[str, Any] | None = None,
        *,
        expected_tools: list[str] | None = None,
    ) -> float | None:
        """Mean usability of the captured tool responses, or None when N/A.

        ``None`` covers two distinct situations that must not be scored 0:
        a case with no tool calls at all (an advisory consult — nothing to
        validate), and a run whose transport never reported tool output.
        """
        if not tool_responses:
            # A case that was *supposed* to call tools and didn't already loses on
            # tool accuracy; double-counting it here would just make one failure
            # look like two.
            return None

        live_expected = live_expected or {}
        per_response: list[float] = []
        for record in tool_responses:
            tool = normalize_tool_name(str(record.get("tool", "")))
            assertions = _lookup(live_expected, tool)
            per_response.append(_score_response(record.get("output"), assertions))

        base = sum(per_response) / len(per_response)

        # A case can pin assertions for a tool that was never called. That is a
        # miss, not a silent pass.
        missing = [
            tool
            for tool in live_expected
            if not any(
                normalize_tool_name(str(r.get("tool", ""))) == normalize_tool_name(tool)
                for r in tool_responses
            )
        ]
        if missing:
            total = len(live_expected)
            base = base * (total - len(missing)) / total if total else base

        return max(0.0, min(1.0, base))

    def passes(self, score: float) -> bool:
        return score >= self.threshold


def validity_breakdown(
    tool_responses: list[dict[str, Any]],
    live_expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-response detail for the dashboard and for debugging a low score."""
    live_expected = live_expected or {}
    rows = []
    for record in tool_responses:
        tool = normalize_tool_name(str(record.get("tool", "")))
        output = record.get("output")
        assertions = _lookup(live_expected, tool)
        rows.append(
            {
                "tool": tool,
                "score": _score_response(output, assertions),
                "error": _error_reason(output),
                "empty": _is_empty(output),
                "preview": _preview(output),
            }
        )
    return {
        "responses": rows,
        "unfulfilled_assertions": [
            tool
            for tool in live_expected
            if not any(r["tool"] == normalize_tool_name(tool) for r in rows)
        ],
    }


def _lookup(live_expected: dict[str, Any], tool: str) -> dict[str, Any]:
    for key, value in live_expected.items():
        if normalize_tool_name(key) == tool and isinstance(value, dict):
            return value
    return {}


def _score_response(output: Any, assertions: dict[str, Any]) -> float:
    """0.0 for an errored call, else 1.0 minus a share per failed assertion."""
    if _error_reason(output):
        return 0.0

    checks: list[bool] = []

    # `nonempty` defaults to True: a tool call that returns nothing is not a
    # working call, whether or not the case bothered to say so.
    if assertions.get("nonempty", True):
        checks.append(not _is_empty(output))

    for needle in assertions.get("contains", []) or []:
        checks.append(str(needle).casefold() in _as_text(output).casefold())

    for path, spec in (assertions.get("fields") or {}).items():
        checks.append(_check_field(output, str(path), spec))

    if not checks:
        return 1.0
    return sum(1 for c in checks if c) / len(checks)


def _error_reason(output: Any) -> str | None:
    """The matched error signal, or None when the response looks like real data."""
    text = _as_text(output)
    if not text.strip():
        return None  # emptiness is handled separately; it isn't an error
    parsed = _as_json(output)
    if isinstance(parsed, dict):
        # Only an explicit error flag or a non-2xx status counts as structured
        # evidence: `detail` and `message` appear on successful payloads too.
        if parsed.get("error"):
            return f"error field: {str(parsed['error'])[:120]}"
        status = parsed.get("status_code") or parsed.get("status")
        if isinstance(status, int) and status >= 400:
            return f"status {status}"
    # Only inspect the head: a long successful payload can legitimately contain
    # the word "error" in a log line or a field name.
    head = text[:400]
    for pattern in _ERROR_PATTERNS:
        # Every match, not just the first: skipping a legend must not abandon the
        # pattern, or a genuine "Error: …" further down the payload goes unseen.
        for match in pattern.finditer(head):
            if _is_legend_line(head, match.start()):
                continue
            return f"matched {pattern.pattern!r} at {match.start()}"
    return None


def _is_legend_line(text: str, position: int) -> bool:
    """True when the match sits in a key describing what a value *means*.

    ``manage_bots`` ends every successful payload with a legend::

        controller state: running = active | stopped = kill switch on |
        error = performance report failed | unknown = controller config unread

    ``\\berror\\b\\s*[:=]`` matches "error =" there, so a healthy "0 active bots"
    response scored 0.0 for live validity and dragged the case's composite down —
    a false negative in the one metric whose job is to say whether the call really
    worked. Real failures announce themselves in a sentence; an enum legend lists
    several ``name = meaning`` pairs on one line, which is what this detects.
    """
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    line = text[start:] if end == -1 else text[start:end]
    return line.count("|") >= 2 and line.count("=") >= 2


def _is_empty(output: Any) -> bool:
    if output is None:
        return True
    if isinstance(output, (list, dict)):
        return len(output) == 0
    text = _as_text(output).strip()
    if text.casefold() in _EMPTY_FORMS:
        return True
    parsed = _as_json(output)
    if isinstance(parsed, (list, dict)):
        return len(parsed) == 0
    return False


def _check_field(output: Any, path: str, spec: Any) -> bool:
    """Resolve a dotted path in the payload and test it against a spec.

    Spec forms: ``{"gt": 0}``, ``{"gte": …}``, ``{"lt": …}``, ``{"lte": …}``,
    ``{"eq": …}``, ``{"present": true}``, or a bare value meaning equality.
    """
    value = _resolve(_as_json(output), path)
    if value is _MISSING:
        return isinstance(spec, dict) and spec.get("present") is False

    if not isinstance(spec, dict):
        return value == spec

    if "present" in spec:
        return bool(spec["present"])
    number = _to_number(value)
    for op, want in spec.items():
        want_num = _to_number(want)
        if op == "eq" and value != want:
            return False
        if op in ("gt", "gte", "lt", "lte"):
            if number is None or want_num is None:
                return False
            if op == "gt" and not number > want_num:
                return False
            if op == "gte" and not number >= want_num:
                return False
            if op == "lt" and not number < want_num:
                return False
            if op == "lte" and not number <= want_num:
                return False
    return True


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def _resolve(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            # A bare index, or "take the first element" for list payloads whose
            # shape the case doesn't want to pin.
            if part.isdigit():
                idx = int(part)
                if idx >= len(current):
                    return _MISSING
                current = current[idx]
                continue
            if not current:
                return _MISSING
            current = current[0]
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif not part.isdigit():
            return _MISSING
    return current


def _to_number(value: Any) -> float | None:
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


def _as_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    try:
        return json.dumps(output)
    except (TypeError, ValueError):
        return str(output)


def _as_json(output: Any) -> Any:
    if isinstance(output, (dict, list)):
        return output
    if isinstance(output, str) and output.strip()[:1] in ("{", "["):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


def _preview(output: Any, limit: int = 240) -> str:
    text = _as_text(output)
    return text[:limit] + ("…" if len(text) > limit else "")
