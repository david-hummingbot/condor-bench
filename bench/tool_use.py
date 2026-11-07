"""Per-tool evidence, read call by call from every scored trace.

The matrix's ``tools`` axis credits a case's whole verdict to each tool the case
expected: a case expecting four tools that failed on one marks all four failed,
and a tool a model called correctly in an agent or tick case counts for nothing.
That answers "can this model own a job that needs this tool", which is what the
router's per-tool verdicts are for, and it stays.

This answers a narrower question, the one an agent built on a model needs: when
the model should use tool T, does it use T correctly the first time? The
evidence is per call, from whatever layer the call happened in, and it is
already on every saved case record, so it needs no new runs:

* ``expected_tools`` / ``tool_calls`` — was T picked when it was needed;
* ``tool_param_detail`` — were the arguments the case pins right;
* ``live_validity_detail.responses`` — did the call work against the real API.

Each case that expected T is one Bernoulli trial, **first-try correct**: T was
called, its pinned arguments matched, and the first T call's response was a
success. That is the headline, with a Wilson interval. The parts are kept so a
low score says which part failed — selection, arguments or execution — and calls
of T in cases that did not expect it still count toward execution, since the
API answering them is evidence too.

Rows that are not evidence about the model (harness artifacts, infra errors)
are skipped, exactly as the matrix skips them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from bench import stats
from metrics.tool_accuracy import normalize_tool_name


def _ok(row: dict[str, Any]) -> bool:
    """A recorded response that worked: the API answered without an error.

    Not the validity ``score``: that is zero for an empty result (``nonempty``
    defaults on) and for a failed case assertion, and an empty executor list on
    an idle account is a call that worked. Whether the data was what the case
    wanted is the case's verdict, not this tool's execution.
    """
    return not row.get("error")


def _args_ok(detail: dict[str, Any] | None) -> bool | None:
    """Whether the pinned arguments matched; None when the case pins none for T."""
    if not detail:
        return None
    if not detail.get("called"):
        return False
    return not detail.get("mismatched") and not detail.get("missing")


@dataclass
class ToolOutcome:
    """What one case says about one tool."""

    tool: str
    expected: bool
    called: bool
    args_ok: bool | None  # None: nothing pinned
    mismatched: list[str]  # pinned keys the model got wrong
    responses: list[bool]  # each T response, in order: worked or not
    unsafe: bool

    @property
    def first_try_correct(self) -> bool | None:
        """Only defined where T was expected."""
        if not self.expected:
            return None
        if not self.called or self.args_ok is False:
            return False
        return bool(self.responses) and self.responses[0]

    @property
    def repaired(self) -> bool | None:
        """Only defined when the first T call failed: did a later one work?"""
        if not self.responses or self.responses[0]:
            return None
        return any(self.responses[1:])


def _norm(names: Any) -> list[str]:
    return [normalize_tool_name(str(n)) for n in (names or [])]


def _call_results(case: dict[str, Any], called: list[str]) -> dict[str, list[bool]]:
    """Per tool, whether each of its calls worked, in call order.

    A call with no recorded response never returned a result: on pydantic-ai a
    failed call goes back to the model as a retry prompt, and nothing records it.
    Counting only recorded responses read three failed ``get_market_data`` calls
    as no calls at all, and the tool's success rate as perfect.

    Records that pair each response with its ``tool_call_id`` are matched
    exactly. Older ones are matched by count, with the unanswered calls placed
    first: fail-then-fix is how a model's repeated calls usually go, and the
    other order would credit a first try that failed.
    """
    rows = (case.get("live_validity_detail") or {}).get("responses") or []
    details = [
        d for d in case.get("tool_call_details") or []
        if d.get("origin", "mcp") == "mcp"
    ]
    by_id = {r["tool_call_id"]: _ok(r) for r in rows if r.get("tool_call_id")}
    out: dict[str, list[bool]] = {}
    if by_id and details:
        for d in details:
            tool = normalize_tool_name(str(d.get("tool", "")))
            out.setdefault(tool, []).append(by_id.get(d.get("tool_call_id"), False))
        return out

    recorded: dict[str, list[bool]] = {}
    for row in rows:
        recorded.setdefault(normalize_tool_name(str(row.get("tool", ""))), []).append(_ok(row))
    calls = Counter(called)
    for tool in set(calls) | set(recorded):
        got = recorded.get(tool, [])
        out[tool] = [False] * max(0, calls.get(tool, 0) - len(got)) + got
    return out


def case_outcomes(case: dict[str, Any], expected: list[str] | None = None) -> list[ToolOutcome]:
    """One outcome per tool the case expected or the model called.

    ``expected`` overrides the record's own list (older records did not persist
    it; the matrix recovers it from the dataset).
    """
    want = set(_norm(case.get("expected_tools") if expected is None else expected))
    called = _norm(case.get("tool_calls"))
    params = {
        normalize_tool_name(str(k)): v
        for k, v in (case.get("tool_param_detail") or {}).items()
    }
    by_tool = _call_results(case, called)
    banned = {
        normalize_tool_name(str(v).partition(":")[0])
        for v in case.get("forbidden_violations") or []
    }

    out = []
    for tool in sorted(want | set(called)):
        detail = params.get(tool)
        out.append(
            ToolOutcome(
                tool=tool,
                expected=tool in want,
                called=tool in called,
                args_ok=_args_ok(detail),
                mismatched=sorted((detail or {}).get("mismatched") or {}),
                responses=by_tool.get(tool, []),
                unsafe=tool in banned,
            )
        )
    return out


@dataclass
class ToolUseCell:
    """One (tool, model) cell: counts, then rates with intervals on output."""

    run_dir: str = ""
    cases: int = 0
    expected: int = 0
    selected: int = 0
    first_try: int = 0
    args_checked: int = 0
    args_ok: int = 0
    calls: int = 0
    calls_ok: int = 0
    first_failed: int = 0
    repaired: int = 0
    unprompted_calls: int = 0
    unsafe: int = 0
    wrong_keys: Counter = field(default_factory=Counter)

    def add(self, o: ToolOutcome) -> None:
        self.cases += 1
        self.calls += len(o.responses)
        self.calls_ok += sum(o.responses)
        if o.unsafe:
            self.unsafe += 1
        if o.repaired is not None:
            self.first_failed += 1
            self.repaired += int(o.repaired)
        if not o.expected:
            self.unprompted_calls += len(o.responses) or int(o.called)
            return
        self.expected += 1
        self.selected += int(o.called)
        self.first_try += int(bool(o.first_try_correct))
        if o.args_ok is not None:
            self.args_checked += 1
            self.args_ok += int(o.args_ok)
            self.wrong_keys.update(o.mismatched)

    def as_dict(self, gate: float | None = None) -> dict[str, Any]:
        headline = stats.rate_summary(self.first_try, self.expected, gate=gate)

        def rate(k: int, n: int) -> float | None:
            return round(k / n, 4) if n else None

        return {
            "cases": self.cases,
            "expected": self.expected,
            # Headline: of the cases that needed T, the share where the model got
            # it right on the first try (picked, arguments right, call worked).
            "first_try_rate": headline["rate"],
            "first_try_ci": headline["ci"],
            "gate_status": headline["gate_status"],
            # Where it went wrong, in pipeline order.
            "selection_rate": rate(self.selected, self.expected),
            "args_rate": rate(self.args_ok, self.args_checked),
            "args_checked": self.args_checked,
            "call_success_rate": rate(self.calls_ok, self.calls),
            "calls": self.calls,
            "repair_rate": rate(self.repaired, self.first_failed),
            "unprompted_calls": self.unprompted_calls,
            "unsafe": self.unsafe,
            "wrong_keys": [k for k, _ in self.wrong_keys.most_common(5)],
            "run_dir": self.run_dir,
        }
