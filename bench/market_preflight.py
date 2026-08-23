"""Which cases can actually run against the connectors this target has.

The case-side half of :mod:`bench.market_registry`. For every case it answers
one question — *is the market this case names available here?* — and separates
the two failures that look identical in a result file today:

``rebindable``
    The named connector has no keys, but an equivalent one does
    (``binance_perpetual`` → ``binance_perpetual_testnet``). Nothing is wrong
    with the case except the literal in it. This is what the resolver fixes.

``unrunnable``
    Nothing on this target satisfies the case. No substitution helps; the answer
    is to add credentials or accept the gap. A case in this state must be
    reported as a harness artifact, never scored.

Both currently show up in a run as a model that called a tool and got an error
back, which :mod:`metrics.live_validity` scores 0.0 — so this module's real job
is telling you how much of your last run measured the box rather than the model.

Read-only: it probes the target and reads the datasets, and changes neither.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from config import DATASETS_DIR
from bench.market_registry import (
    CEX,
    GATEWAY,
    PERPETUAL,
    Binding,
    MarketRegistry,
    probe_registry,
)

# Verdicts, worst last — the order the report groups by.
OK = "ok"
NO_DEPENDENCY = "no_dependency"
UNKNOWN = "unknown"
REBINDABLE = "rebindable"
UNRUNNABLE = "unrunnable"

VERDICT_ORDER = (UNRUNNABLE, REBINDABLE, UNKNOWN, OK, NO_DEPENDENCY)

# Tools that act *as* an account and therefore need keys on the connector they
# touch. Derived from the tool surface rather than listed here: a singular
# ``account_name`` param means the call operates on one account, where the plural
# ``account_names`` is a read filter that merely returns less when a connector is
# absent. Kept as a fallback for an unreadable snapshot.
_FALLBACK_ACCOUNT_ACTING = frozenset(
    {
        "set_account_position_mode_and_leverage",
        "manage_executors",
        "manage_bots",
    }
)


# DEX/AMM tools name a *gateway* connector, which lives in a different namespace
# and cannot be substituted with a centralised exchange. Also derived from the
# snapshot: a tool taking both ``connector`` (not ``connector_name``) and
# ``network`` is addressing a chain, not an exchange account.
_FALLBACK_GATEWAY_TOOLS = frozenset({"explore_dex_pools", "manage_amm"})


def _tool_specs() -> dict[str, dict[str, Any]]:
    try:
        snapshot = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
    except Exception:
        return {}
    return {
        tool: meta or {}
        for spec in snapshot.get("servers", {}).values()
        for tool, meta in (spec.get("tools") or {}).items()
    }


def account_acting_tools() -> frozenset[str]:
    """Tools whose calls need credentials on the connector they name."""
    specs = _tool_specs()
    found = {
        tool
        for tool, meta in specs.items()
        if "account_name" in (meta.get("params") or [])
    }
    return frozenset(found or _FALLBACK_ACCOUNT_ACTING)


def connector_required_tools() -> frozenset[str]:
    """Tools that cannot be called at all without naming a connector."""
    specs = _tool_specs()
    found = {
        tool
        for tool, meta in specs.items()
        if "connector_name" in (meta.get("required") or [])
    }
    return frozenset(found or {"set_account_position_mode_and_leverage", "get_market_data"})


# Words that mark a question as being about perpetuals even when no venue is
# named — "Put BTC-USDT perpetuals back to 2x leverage" needs a *perp* connector,
# and a box with only spot keys cannot run it however many keys it has.
_PERP_WORDS = re.compile(r"(?i)\b(perp|perps|perpetual|perpetuals|futures|leverage)\b")


def gateway_tools() -> frozenset[str]:
    """Tools whose ``connector`` argument names a DEX behind the gateway."""
    specs = _tool_specs()
    found = {
        tool
        for tool, meta in specs.items()
        if "connector" in (meta.get("params") or [])
        and "network" in (meta.get("params") or [])
    }
    return frozenset(found or _FALLBACK_GATEWAY_TOOLS)


@dataclass(frozen=True)
class ConnectorRef:
    """A connector a case names, and where the name came from."""

    connector: str
    source: str  # "params" | "config" | "prose"
    namespace: str = CEX


@dataclass
class CaseNeeds:
    case_id: str
    case_type: str
    risk_level: str
    refs: tuple[ConnectorRef, ...] = ()
    pairs: tuple[str, ...] = ()
    needs_credentials: bool = False
    acting_tools: tuple[str, ...] = ()
    # DEX tools the case is expected to call. A case can depend on the gateway
    # without naming a connector — "what positions do I own on Meteora?" pins
    # nothing and still cannot run while the service is down.
    dex_tools: tuple[str, ...] = ()
    # Set when the case names no connector but calls a tool that *requires* one
    # ("Put BTC-USDT perpetuals back to 2x leverage"). The model has to pick a
    # connector, so the box still has to have a credentialed one — and the kind
    # is whatever the question implies.
    implied_kind: str | None = None
    implied_tools: tuple[str, ...] = ()


@dataclass
class CaseVerdict:
    case_id: str
    case_type: str
    risk_level: str
    verdict: str
    detail: str
    referenced: tuple[str, ...] = ()
    suggested: str = ""

    @property
    def blocking(self) -> bool:
        return self.verdict in (REBINDABLE, UNRUNNABLE)


@dataclass
class PreflightReport:
    registry: MarketRegistry
    verdicts: list[CaseVerdict] = field(default_factory=list)

    def by_verdict(self, verdict: str) -> list[CaseVerdict]:
        return [v for v in self.verdicts if v.verdict == verdict]

    @property
    def counts(self) -> dict[str, int]:
        return {v: len(self.by_verdict(v)) for v in VERDICT_ORDER}

    @property
    def affected(self) -> list[CaseVerdict]:
        """Cases that cannot run as written, worst first."""
        return self.by_verdict(UNRUNNABLE) + self.by_verdict(REBINDABLE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_url": self.registry.api_url,
            "registry_ok": self.registry.ok,
            "registry_error": self.registry.error,
            "credentialed": {
                name: list(conn.credentialed_on)
                for name, conn in sorted(self.registry.connectors.items())
                if conn.credentialed
            },
            "counts": self.counts,
            "cases": [
                {
                    "case_id": v.case_id,
                    "type": v.case_type,
                    "risk_level": v.risk_level,
                    "verdict": v.verdict,
                    "detail": v.detail,
                    "referenced": list(v.referenced),
                    "suggested": v.suggested,
                }
                for v in self.verdicts
            ],
        }


def _case_text(case: Any) -> str:
    """Everything a model reads that could name a connector."""
    parts = [
        str(getattr(case, "question", "") or ""),
        str(getattr(case, "context", "") or ""),
        str(getattr(case, "scenario_name", "") or ""),
        str(getattr(case, "strategy_instructions", "") or ""),
        str(getattr(case, "agent_instructions", "") or ""),
    ]
    parts += [str(t) for t in (getattr(case, "turns", None) or [])]
    core = getattr(case, "core_data", None) or {}
    if isinstance(core, dict):
        parts += [str(v) for v in core.values()]
    return "\n".join(p for p in parts if p)


def prose_forms(connector: str) -> list[str]:
    """Phrases a case might use for a connector, longest first.

    Generated from the connector id so the vocabulary tracks whatever the target
    supports instead of a hand-kept alias table going stale.
    """
    base = connector
    testnet = False
    for marker in ("_testnet", "_sandbox"):
        if marker in base:
            base = base.replace(marker, "")
            testnet = True
    perp = "_perpetual" in base
    stem = base.replace("_perpetual", "").replace("_", " ").strip()
    forms = {connector, connector.replace("_", " ")}
    if perp and stem:
        forms |= {
            f"{stem} perpetual",
            f"{stem} perpetuals",
            f"{stem} perp",
            f"{stem} perps",
            f"{stem} futures",
        }
    elif stem:
        forms.add(stem)
    if testnet:
        forms |= {f"{f} testnet" for f in list(forms)}
    return sorted((f for f in forms if len(f) > 2), key=len, reverse=True)


def connectors_in_text(text: str, known: Iterable[str]) -> list[str]:
    """Connectors named in prose, longest phrase winning.

    Matched spans are consumed so "Binance perpetuals" resolves to the perp
    connector only — a plain substring sweep would also report spot ``binance``
    and turn one reference into two.
    """
    if not text:
        return []
    haystack = text.lower()
    forms: list[tuple[str, str]] = []
    for name in known:
        for form in prose_forms(name):
            forms.append((form, name))
    forms.sort(key=lambda item: len(item[0]), reverse=True)

    found: list[str] = []
    for form, name in forms:
        if name in found:
            continue
        pattern = re.compile(rf"(?<![\w-]){re.escape(form)}(?![\w-])")
        match = pattern.search(haystack)
        if not match:
            continue
        found.append(name)
        # Blank the span so a shorter form cannot claim the same words.
        haystack = haystack[: match.start()] + " " * (match.end() - match.start()) + haystack[match.end() :]
    return found


def needs_for_case(
    case: Any,
    known_connectors: Iterable[str],
    gateway_connectors: Iterable[str] = (),
) -> CaseNeeds:
    """What markets a case depends on, and whether it needs keys for them."""
    from bench.market_warmup import markets_from_case

    refs: list[ConnectorRef] = []
    seen: set[str] = set()
    dex_tools = gateway_tools()

    def _add(name: str, source: str, namespace: str = CEX) -> None:
        name = str(name or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        refs.append(ConnectorRef(name, source, namespace))

    pairs: list[str] = []
    for market in markets_from_case(case):
        if market.trading_pair not in pairs:
            pairs.append(market.trading_pair)

    # Attributed per tool, because the tool decides the namespace: the same
    # string is a DEX behind the gateway when explore_dex_pools names it and a
    # centralised exchange when get_market_data does.
    params = getattr(case, "expected_tool_params", None) or {}
    if isinstance(params, dict):
        for tool, args in params.items():
            if not isinstance(args, dict):
                continue
            namespace = GATEWAY if tool in dex_tools else CEX
            _add(
                args.get("connector_name") or args.get("connector") or "",
                "params",
                namespace,
            )
    config = getattr(case, "config", None) or {}
    if isinstance(config, dict):
        _add(config.get("connector_name") or config.get("connector") or "", "config")

    text = _case_text(case)
    for name in connectors_in_text(text, known_connectors):
        _add(name, "prose")
    for name in connectors_in_text(text, gateway_connectors):
        _add(name, "prose", GATEWAY)

    acting = account_acting_tools()
    tools = set(getattr(case, "expected_tools", None) or [])
    tools |= set(getattr(case, "expected_tool_calls", None) or [])
    for step in getattr(case, "steps", None) or []:
        if isinstance(step, dict):
            tools |= set(step.get("expected_tools") or [])
    hit = sorted(tools & acting)

    # Only meaningful when nothing was named: a case that pins a connector is
    # judged on that connector, not on what the box happens to have.
    implied = sorted(tools & acting & connector_required_tools()) if not refs else []

    return CaseNeeds(
        case_id=getattr(case, "id", "?"),
        case_type=getattr(case, "type", "") or "",
        risk_level=getattr(case, "risk_level", "read_only") or "read_only",
        refs=tuple(refs),
        pairs=tuple(pairs),
        needs_credentials=bool(hit),
        acting_tools=tuple(hit),
        dex_tools=tuple(sorted(tools & dex_tools)),
        implied_kind=(PERPETUAL if _PERP_WORDS.search(text) else None) if implied else None,
        implied_tools=tuple(implied),
    )


def judge(needs: CaseNeeds, registry: MarketRegistry) -> CaseVerdict:
    """Verdict for one case against one target."""
    base = dict(
        case_id=needs.case_id,
        case_type=needs.case_type,
        risk_level=needs.risk_level,
        referenced=tuple(ref.connector for ref in needs.refs),
    )
    # A dead gateway is decisive on its own: every DEX call in the case will come
    # back as an error the model cannot avoid, whether or not a connector is
    # named. Reported as unrunnable rather than unknown because the outcome is
    # certain — but the detail says it is the service, not the dataset, so nobody
    # goes looking for a literal to fix.
    if needs.dex_tools and not registry.namespace_readable(GATEWAY):
        return CaseVerdict(
            **base,
            verdict=UNRUNNABLE,
            detail=(
                f"calls {', '.join(needs.dex_tools)} but the gateway service is "
                f"unavailable: {registry.gateway_error or 'unknown reason'}"
            ),
        )
    if not needs.refs and needs.implied_tools:
        if not registry.ok:
            return CaseVerdict(
                **base, verdict=UNKNOWN, detail=f"target not readable: {registry.error}"
            )
        available = registry.candidates(kind=needs.implied_kind)
        kind_word = needs.implied_kind or "any-kind"
        if not available:
            return CaseVerdict(
                **base,
                verdict=UNRUNNABLE,
                detail=(
                    f"names no connector but {', '.join(needs.implied_tools)} "
                    f"requires one, and this target has no credentialed "
                    f"{kind_word} connector"
                ),
            )
        return CaseVerdict(
            **base,
            verdict=OK,
            detail=(
                f"names no connector; the model must pick one and a credentialed "
                f"{kind_word} connector exists ({available[0]})"
            ),
        )
    if not needs.refs:
        return CaseVerdict(
            **base, verdict=NO_DEPENDENCY, detail="names no connector"
        )
    if not registry.ok:
        return CaseVerdict(
            **base,
            verdict=UNKNOWN,
            detail=f"target not readable: {registry.error}",
        )

    problems: list[str] = []
    suggestions: list[str] = []
    unknowns: list[str] = []
    worst = OK
    for ref in needs.refs:
        name, ns = ref.connector, ref.namespace
        if not registry.namespace_readable(ns):
            # Cannot enumerate the namespace, so "not here" is unprovable. Saying
            # unrunnable would blame the dataset for a service being down.
            unknowns.append(
                f"{name} is a {ns} connector and that namespace is unreadable: "
                f"{registry.gateway_error or registry.error}"
            )
            continue
        # Gateway connectors are keyed by a wallet, not by account credentials,
        # so presence in the namespace is the whole availability question.
        wants_keys = needs.needs_credentials and ns == CEX
        if wants_keys:
            if registry.credentialed(name, ns):
                continue
            why = (
                f"{name} has no credentials on this target"
                if registry.supported(name, ns)
                else f"{name} is not a connector on this target"
            )
        else:
            if registry.supported(name, ns):
                continue
            why = f"{name} is not a {ns} connector on this target"
        alternatives = [
            a
            for a in registry.candidates(
                kind=registry.kind(name, ns),
                needs_credentials=wants_keys,
                namespace=ns,
            )
            if a != name
        ]
        if alternatives:
            problems.append(f"{why} (named in {ref.source})")
            suggestions.append(alternatives[0])
            if worst != UNRUNNABLE:
                worst = REBINDABLE
        else:
            kind_word = "perpetual" if registry.kind(name, ns) == PERPETUAL else "spot"
            scope = "credentialed " if wants_keys else ""
            problems.append(
                f"{why} (named in {ref.source}) and no {scope}"
                f"{kind_word} {ns} connector exists here"
            )
            worst = UNRUNNABLE

    if worst == OK and unknowns:
        return CaseVerdict(**base, verdict=UNKNOWN, detail="; ".join(unknowns))
    if worst == OK:
        scope = "credentialed" if needs.needs_credentials else "supported"
        return CaseVerdict(
            **base,
            verdict=OK,
            detail=f"all referenced connectors are {scope} here",
        )
    detail = "; ".join(problems)
    if needs.acting_tools:
        detail += f" — needed by {', '.join(needs.acting_tools)}"
    return CaseVerdict(
        **base, verdict=worst, detail=detail, suggested=suggestions[0] if suggestions else ""
    )


async def check_cases(
    cases: list[Any] | None = None, *, registry: MarketRegistry | None = None
) -> PreflightReport:
    """Probe the target and judge every case against it.

    Two passes. :func:`judge` is synchronous and pair-blind, which is enough to
    sort the cases; the second pass then asks the registry for the binding each
    affected case would actually get, because a candidate that cannot trade the
    case's pair is not a fix. That is what demotes a rebindable case to
    unrunnable — and it costs one trading-rules call per candidate, so it runs
    only for the cases that need it.
    """
    if cases is None:
        from bench.dataset import load_all_cases

        cases = load_all_cases()
    registry = registry if registry is not None else await probe_registry()
    known = sorted(registry.connectors)
    gateway = sorted(registry.gateway)

    # A case that declares `markets` has already answered the question this
    # module infers for everything else, so ask the resolver rather than
    # re-deriving it from prose that now holds placeholders.
    declared = [c for c in cases if getattr(c, "markets", None)]
    literal = [c for c in cases if not getattr(c, "markets", None)]

    needs = [needs_for_case(case, known, gateway) for case in literal]
    verdicts = [judge(need, registry) for need in needs]
    for case in declared:
        verdicts.append(await _judge_declared(case, registry))

    by_id = {need.case_id: need for need in needs}
    for verdict in verdicts:
        if verdict.verdict != REBINDABLE:
            continue
        need = by_id.get(verdict.case_id)
        binding = await resolve_for_case(need, registry) if need else None
        if binding is None:
            verdict.verdict = UNRUNNABLE
            verdict.detail += (
                " — and no available connector lists "
                f"{' / '.join(need.pairs) if need and need.pairs else 'the requested market'}"
            )
            verdict.suggested = ""
            continue
        verdict.suggested = binding.connector + (
            f"/{binding.trading_pair}" if binding.trading_pair else ""
        )
        if binding.kind_change:
            verdict.detail += f" — rebinding crosses {binding.kind_change}"

    order = {v: i for i, v in enumerate(VERDICT_ORDER)}
    verdicts.sort(key=lambda v: (order.get(v.verdict, 99), v.case_id))
    return PreflightReport(registry=registry, verdicts=verdicts)


async def _judge_declared(case: Any, registry: MarketRegistry) -> CaseVerdict:
    """Verdict for a case that declares ``markets``, from the resolver itself.

    Never ``rebindable``: a declared requirement either binds — which *is* the
    rebind, applied — or it does not, and then no literal edit would help.
    """
    from bench.market_resolver import resolve_case

    base = dict(
        case_id=getattr(case, "id", "?"),
        case_type=getattr(case, "type", "") or "",
        risk_level=getattr(case, "risk_level", "read_only") or "read_only",
    )
    try:
        resolution = await resolve_case(case, registry)
    except ValueError as exc:
        return CaseVerdict(
            **base, verdict=UNRUNNABLE, detail=f"malformed markets block: {exc}"
        )
    if resolution.ok:
        bound = ", ".join(
            f"{name} → {b.connector}" + (f"/{b.trading_pair}" if b.trading_pair else "")
            for name, b in sorted(resolution.bindings.items())
        )
        detail = f"declared markets bound: {bound}"
        if resolution.notes:
            detail += f" ({'; '.join(resolution.notes)})"
        return CaseVerdict(
            **base,
            verdict=OK,
            detail=detail,
            referenced=tuple(
                b.connector for _, b in sorted(resolution.bindings.items())
            ),
            suggested="",
        )
    # Unmet is unrunnable, whatever the cause: `prepare_cases` refuses the run on
    # exactly this condition, and a report that graded a down gateway as merely
    # "unknown" would tell you the dataset was fine right up until the run was
    # refused. Whose fault it is lives in the detail, not the verdict. The one
    # exception is a target that could not be read at all, where nothing about
    # the case has been established.
    return CaseVerdict(
        **base,
        verdict=UNKNOWN if not registry.ok else UNRUNNABLE,
        detail=f"declared markets unmet — {resolution.reason()}",
    )


async def resolve_for_case(
    needs: CaseNeeds, registry: MarketRegistry, *, prefer: Iterable[str] = ()
) -> Binding | None:
    """The binding a case would get, or ``None`` when nothing on the box fits.

    Prefers the connector the case already names — an available literal must
    never be swapped out — then the operator's ``prefer`` order, then whatever
    can trade the pair. Kind changes are allowed but recorded: on a box whose
    only credentialed markets are perpetual, a spot grid case can still run, and
    a human should see that it is no longer a spot test.
    """
    if not needs.refs:
        return None
    ref = needs.refs[0]
    return await registry.resolve(
        kind=registry.kind(ref.connector, ref.namespace),
        needs_credentials=needs.needs_credentials and ref.namespace == CEX,
        prefer=[ref.connector] + list(prefer),
        pair_candidates=needs.pairs,
        namespace=ref.namespace,
        allow_kind_change=True,
        optimistic_for=ref.connector,
    )


_VERDICT_STYLE = {
    UNRUNNABLE: "red",
    REBINDABLE: "yellow",
    UNKNOWN: "magenta",
    OK: "green",
    NO_DEPENDENCY: "dim",
}


def format_report(report: PreflightReport) -> str:
    """Plain-text report. The dashboard renders :meth:`PreflightReport.as_dict`."""
    reg = report.registry
    lines = [f"Target: {reg.api_url or '<unset>'}"]
    if not reg.ok:
        lines.append(f"  ! {reg.error}")
    else:
        creds = {
            name: conn.credentialed_on
            for name, conn in sorted(reg.connectors.items())
            if conn.credentialed
        }
        lines.append(
            f"  {len(reg.connectors)} connector(s) supported, "
            f"{len(creds)} credentialed on {len(reg.accounts)} account(s)"
        )
        for name, accounts in creds.items():
            lines.append(f"    ✓ {name}  ({', '.join(accounts)})")
    counts = report.counts
    lines.append("")
    lines.append(
        "Cases: "
        + ", ".join(f"{counts[v]} {v}" for v in VERDICT_ORDER if counts[v])
    )
    for verdict in (UNRUNNABLE, REBINDABLE, UNKNOWN):
        rows = report.by_verdict(verdict)
        if not rows:
            continue
        lines.append("")
        lines.append(f"{verdict.upper()} ({len(rows)})")
        for row in rows:
            suffix = f"  → suggest {row.suggested}" if row.suggested else ""
            lines.append(f"  {row.case_id}  [{row.risk_level}]  {row.detail}{suffix}")
    return "\n".join(lines)
