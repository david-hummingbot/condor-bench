"""L0: does Condor's tool contract hold, before any model is involved?

Three properties of every MCP tool the benchmark measures, checked against the
real MCP servers on the mock lane with no model in the loop:

* **schema** — its input schema is valid JSON Schema. A schema a provider cannot
  compile drops the tool (or the whole request) for every model, and the result
  would read as "the model never called it".
* **required** — a call missing a required argument is rejected *before* the tool
  runs. Executing on partial input is how a model's half-formed call becomes a
  half-placed order.
* **enum** — an out-of-enum value for a required enum argument is rejected the
  same way.

Every rejection must also be *stable*: an error the bench's own response scorer
(:func:`metrics.live_validity._error_reason`) classifies as an error, naming the
offending field. That is what lets a model repair its call — and what keeps a
rejected call from being scored as a working one.

Wraps the existing scorers rather than replacing ``runner.py``: this is a
Pydantic Evals dataset over the tool surface, run by ``runner.py ladder``.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

#: Value no enum in the surface accepts.
INVALID_ENUM = "__bench_not_a_valid_value__"


@dataclass
class ContractInput:
    kind: str  # "schema" | "required" | "enum"
    server: str
    tool: str
    schema: dict[str, Any]
    target_field: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContractOutcome:
    schema_error: str | None = None
    rejected: bool | None = None
    error_text: str = ""
    classified_as_error: bool | None = None


# ── Evaluators ─────────────────────────────────────────────────────────────────
@dataclass
class SchemaCompiles(Evaluator[ContractInput, ContractOutcome]):
    def evaluate(self, ctx: EvaluatorContext[ContractInput, ContractOutcome]) -> bool:
        if ctx.inputs.kind != "schema":
            return True
        return ctx.output.schema_error is None


@dataclass
class RejectedBeforeRunning(Evaluator[ContractInput, ContractOutcome]):
    def evaluate(self, ctx: EvaluatorContext[ContractInput, ContractOutcome]) -> bool:
        if ctx.inputs.kind == "schema":
            return True
        return bool(ctx.output.rejected)


@dataclass
class StableErrorFormat(Evaluator[ContractInput, ContractOutcome]):
    """The rejection reads as an error to bench's scorer and names the field."""

    def evaluate(self, ctx: EvaluatorContext[ContractInput, ContractOutcome]) -> bool:
        if ctx.inputs.kind == "schema" or not ctx.output.rejected:
            return True
        names_field = (ctx.inputs.target_field or "") in ctx.output.error_text
        return bool(ctx.output.classified_as_error) and names_field


EVALUATORS = (SchemaCompiles(), RejectedBeforeRunning(), StableErrorFormat())


# ── Dataset construction ───────────────────────────────────────────────────────
def build_cases(surface: dict[str, list[dict[str, Any]]], *, only: set[str] | None = None) -> list[Case]:
    """One schema case per tool, plus required/enum rejection cases where they apply.

    *surface* maps server name → ``[{"name", "inputSchema"}]`` as listed live.
    *only* restricts to the tools the dataset measures.
    """
    cases: list[Case] = []
    for server, tools in sorted(surface.items()):
        for tool in sorted(tools, key=lambda t: t["name"]):
            name, schema = tool["name"], tool.get("inputSchema") or {}
            if only is not None and name not in only:
                continue
            cases.append(Case(name=f"schema:{name}", inputs=ContractInput("schema", server, name, schema)))
            required = list(schema.get("required") or [])
            props = schema.get("properties") or {}
            if required:
                missing = required[0]
                args = {k: _sample(props.get(k) or {}) for k in required[1:]}
                cases.append(Case(
                    name=f"required:{name}:{missing}",
                    inputs=ContractInput("required", server, name, schema, missing, args),
                ))
            enum_field = next((k for k in required if _enum_values(props.get(k) or {})), None)
            if enum_field and all(_enum_values(props.get(k) or {}) for k in required):
                args = {k: _sample(props.get(k) or {}) for k in required if k != enum_field}
                args[enum_field] = INVALID_ENUM
                cases.append(Case(
                    name=f"enum:{name}:{enum_field}",
                    inputs=ContractInput("enum", server, name, schema, enum_field, args),
                ))
    return cases


def _enum_values(prop: dict[str, Any]) -> list[Any]:
    if "enum" in prop:
        return list(prop["enum"])
    for alt in prop.get("anyOf") or []:
        if "enum" in alt:
            return list(alt["enum"])
    return []


def _sample(prop: dict[str, Any]) -> Any:
    """A schema-valid value for an argument the case is not testing."""
    values = _enum_values(prop)
    if values:
        return values[0]
    kind = prop.get("type")
    return {"integer": 1, "number": 1.0, "boolean": False, "array": [], "object": {}}.get(kind, "x")


# ── Task: run one contract check against the live MCP session ──────────────────
class SurfaceSession:
    """One MCP client session per condor server, kept open for the whole dataset."""

    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        self.sessions: dict[str, Any] = {}

    async def __aenter__(self) -> "SurfaceSession":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        from bench.mcp_provider import build_mcp_configs

        for cfg in build_mcp_configs():
            env = dict(os.environ)
            for entry in cfg.get("env", []) or []:
                env[str(entry["name"])] = str(entry["value"])
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []), cwd=cfg.get("cwd"), env=env
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[cfg["name"]] = session
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def surface(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name, session in self.sessions.items():
            listed = await session.list_tools()
            out[name] = [{"name": t.name, "inputSchema": t.inputSchema} for t in listed.tools]
        return out

    async def run(self, inp: ContractInput) -> ContractOutcome:
        if inp.kind == "schema":
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import SchemaError

            try:
                Draft202012Validator.check_schema(inp.schema)
                return ContractOutcome()
            except SchemaError as exc:
                return ContractOutcome(schema_error=str(exc).splitlines()[0])

        from metrics.live_validity import _error_reason

        result = await self.sessions[inp.server].call_tool(inp.tool, inp.args)
        text = "\n".join(getattr(c, "text", "") or "" for c in (result.content or []))
        # Rejected means the arguments never reached the tool body: the server's
        # own argument validation answered, flagged as an error.
        rejected = bool(result.isError) and "validation error" in text.lower()
        return ContractOutcome(
            rejected=rejected,
            error_text=text[:600],
            classified_as_error=_error_reason(text) is not None,
        )


def measured_tools() -> set[str]:
    """Every tool the case library expects, holdout included."""
    from bench.dataset import load_all_cases

    return {t for c in load_all_cases(include_holdout=True) for t in (c.expected_tools or [])}


async def run_l0() -> dict[str, Any]:
    """Run the contract dataset on the current lane. Returns a JSON-able report."""
    async with SurfaceSession() as surface:
        listed = await surface.surface()
        cases = build_cases(listed, only=measured_tools())
        dataset = Dataset[ContractInput, ContractOutcome, Any](
            name="l0_contracts", cases=cases, evaluators=list(EVALUATORS)
        )
        report = await dataset.evaluate(surface.run, max_concurrency=1, progress=False)

    rows = []
    for case in report.cases:
        checks = {name: bool(res.value) for name, res in case.assertions.items()}
        rows.append({
            "case": case.name,
            "passed": all(checks.values()),
            "checks": checks,
            "detail": (
                case.output.schema_error
                or (case.output.error_text[:200] if case.output.rejected is not None else "")
            ),
        })
    for failure in report.failures:
        rows.append({"case": failure.name, "passed": False, "checks": {}, "detail": f"task error: {failure.error_message}"})
    passed = sum(1 for r in rows if r["passed"])
    return {
        "rung": "l0",
        "cases": len(rows),
        "passed": passed,
        "failed": [r for r in rows if not r["passed"]],
        "rows": rows,
        "tools_listed": {k: len(v) for k, v in listed.items()},
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"L0 contracts: {report['passed']}/{report['cases']} passed "
             f"(tools listed: {json.dumps(report['tools_listed'])})"]
    for row in report["failed"]:
        failed = [k for k, v in row["checks"].items() if not v] or ["error"]
        lines.append(f"  ✗ {row['case']} [{', '.join(failed)}] {row['detail']}")
    return "\n".join(lines)
