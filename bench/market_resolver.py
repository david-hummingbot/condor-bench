"""Bind a case's declared market requirements to real connectors, then run it.

A case that hardcodes ``binance_perpetual`` is making a claim about the box the
run lands on, and when the claim is wrong the model eats the score (see
:mod:`bench.market_preflight` for what that costs). The fix is for a case to
declare what it *needs* and let the target decide what satisfies it::

    {"id": "tool_set_leverage_001",
     "markets": {"perp": {"kind": "perpetual", "needs": "credentials",
                          "prefer": ["binance_perpetual",
                                     "binance_perpetual_testnet"],
                          "pair": "BTC-USDT"}},
     "question": "Set {perp.label} to 3x leverage for {perp.pair}.",
     "expected_tool_params": {"set_account_position_mode_and_leverage":
         {"leverage": 3, "trading_pair": "{perp.pair}",
          "connector_name": "{perp.connector}"}}}

Every string in the case is then substituted, so the question the model reads and
the ground truth it is scored against always name the same market. Four fields
per requirement, all optional except that something has to identify it:

``{name.connector}``  the connector id, for tool arguments
``{name.label}``      prose form ("Binance perpetuals (testnet)"), for questions
``{name.pair}``       the bound trading pair
``{name.account}``    the account holding the credentials

Resolution is deterministic — ``prefer`` order, then alphabetical — because two
runs against one box that bound different connectors would produce incomparable
scores while looking like model variance. What bound is recorded per case and
belongs in the run's results: a leverage score earned against
``hyperliquid_perpetual`` is not the same measurement as one earned against
``binance_perpetual``, and only the binding makes that visible.

A requirement that binds to nothing is fatal by design. :func:`assert_resolvable`
refuses the run rather than letting the case through to score 0 for a reason the
model cannot influence.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Iterable

from bench.market_registry import (
    CEX,
    GATEWAY,
    PERPETUAL,
    SPOT,
    Binding,
    MarketRegistry,
    probe_registry,
)

MARKETS_FIELD_DOC = """\
markets: {"<name>": {
    "kind":      "perpetual" | "spot"   (optional; unset means either)
    "needs":     "credentials" | "support"   (default "credentials")
    "namespace": "cex" | "gateway"      (default "cex")
    "prefer":    ["connector", ...]     (tried in order before anything else)
    "pair":      "BTC-USDT"             (or "pairs": [...] — first available wins)
    "allow_kind_change": bool           (default false; records the swap)
}}"""

# Placeholder attributes a requirement exposes for substitution. Every name here
# must exist on Binding — when `pair` did not, `{perp.pair}` silently resolved to
# an empty string in both the question and the ground truth
# (test_every_placeholder_attribute_resolves_on_a_binding pins that). A token
# using any *other* attribute is left untouched and then caught by
# :func:`unresolved_placeholders`, which is what turns a dataset typo into a
# refused run instead of a scored one.
_ATTRS = ("connector", "label", "pair", "account", "kind")


class MarketsUnavailable(RuntimeError):
    """One or more selected cases cannot be bound to markets on this target."""


@dataclass(frozen=True)
class Requirement:
    """One declared market a case needs."""

    name: str
    kind: str | None = None
    needs: str = "credentials"
    namespace: str = CEX
    prefer: tuple[str, ...] = ()
    pairs: tuple[str, ...] = ()
    allow_kind_change: bool = False

    @property
    def needs_credentials(self) -> bool:
        # Gateway connectors are keyed by a wallet, not by account credentials,
        # so "needs: credentials" is meaningless there and would reject every
        # candidate.
        return self.needs == "credentials" and self.namespace == CEX

    @classmethod
    def parse(cls, name: str, spec: Any) -> "Requirement":
        """Read one entry of a ``markets`` block.

        A bare string is shorthand for ``{"prefer": [value]}``, which is how a
        case pins a specific connector while still declining to *require* it.
        """
        if isinstance(spec, str):
            spec = {"prefer": [spec]}
        if not isinstance(spec, dict):
            raise ValueError(f"markets.{name} must be a string or object")
        kind = spec.get("kind")
        if kind not in (None, PERPETUAL, SPOT):
            raise ValueError(
                f"markets.{name}.kind must be {PERPETUAL!r} or {SPOT!r}, got {kind!r}"
            )
        needs = str(spec.get("needs") or "credentials")
        if needs not in ("credentials", "support"):
            raise ValueError(
                f"markets.{name}.needs must be 'credentials' or 'support', got {needs!r}"
            )
        namespace = str(spec.get("namespace") or CEX)
        if namespace not in (CEX, GATEWAY):
            raise ValueError(
                f"markets.{name}.namespace must be {CEX!r} or {GATEWAY!r}, got {namespace!r}"
            )
        prefer = spec.get("prefer") or []
        if isinstance(prefer, str):
            prefer = [prefer]
        pairs = spec.get("pairs") or ([spec["pair"]] if spec.get("pair") else [])
        if isinstance(pairs, str):
            pairs = [pairs]
        return cls(
            name=name,
            kind=kind,
            needs=needs,
            namespace=namespace,
            prefer=tuple(str(p) for p in prefer),
            pairs=tuple(str(p) for p in pairs),
            allow_kind_change=bool(spec.get("allow_kind_change")),
        )


def requirements_for(case: Any) -> list[Requirement]:
    """Parsed ``markets`` block, in declaration order."""
    markets = getattr(case, "markets", None) or {}
    if not isinstance(markets, dict):
        raise ValueError(f"{getattr(case, 'id', '?')}: markets must be an object")
    return [Requirement.parse(name, spec) for name, spec in markets.items()]


@dataclass
class Resolution:
    """The outcome of binding one case's requirements."""

    case_id: str
    case: Any
    bindings: dict[str, Binding] = field(default_factory=dict)
    unmet: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unmet

    @property
    def declared(self) -> bool:
        return bool(self.bindings or self.unmet)

    def as_dict(self) -> dict[str, Any]:
        """Recorded per run: which markets a score was actually earned against."""
        return {
            name: {
                "connector": b.connector,
                "account": b.account,
                "trading_pair": b.trading_pair,
                "kind": b.kind,
                "is_testnet": b.is_testnet,
                "kind_change": b.kind_change,
            }
            for name, b in sorted(self.bindings.items())
        }

    def reason(self) -> str:
        parts = [f"{name}: {why}" for name, why in sorted(self.unmet.items())]
        return "; ".join(parts)


def substitute(value: Any, bindings: dict[str, Binding]) -> Any:
    """Replace ``{name.attr}`` placeholders throughout a nested structure.

    Walks dicts, lists and tuples so ground truth nested inside
    ``expected_tool_params`` or ``steps`` is substituted along with the question.
    Every attribute a binding exposes is a string, so this never changes a param's
    type — an int pin like ``"leverage": 3`` stays an int because it holds no
    placeholder to begin with.
    """
    if isinstance(value, str):
        return _substitute_str(value, bindings)
    if isinstance(value, dict):
        return {k: substitute(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, bindings) for v in value]
    if isinstance(value, tuple):
        return tuple(substitute(v, bindings) for v in value)
    return value


def _substitute_str(text: str, bindings: dict[str, Binding]) -> Any:
    if "{" not in text:
        return text
    out = text
    for name, binding in bindings.items():
        for attr in _ATTRS:
            token = f"{{{name}.{attr}}}"
            if token in out:
                out = out.replace(token, str(getattr(binding, attr) or ""))
    return out


def unresolved_placeholders(case: Any) -> list[str]:
    """Placeholder tokens still present after substitution.

    A typo like ``{perp.pairs}`` would otherwise reach the model verbatim and be
    scored as if the dataset meant it.
    """
    import re

    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(re.findall(r"\{[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*\}", value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    for f in dataclasses.fields(case):
        if f.name == "markets":
            continue
        walk(getattr(case, f.name, None))
    return sorted(found)


# Substitution walks every case field rather than a list of eligible ones, so a
# field added later is covered without an edit here. These three are excluded:
# `markets` is the declaration itself, and `id` / `type` are identity — a case
# whose id changed between runs would break every comparison keyed on it.
_SKIP_FIELDS = frozenset({"markets", "id", "type"})


async def resolve_case(
    case: Any, registry: MarketRegistry, *, prefer: Iterable[str] = ()
) -> Resolution:
    """Bind a case's requirements and return a substituted copy.

    A case with no ``markets`` block comes back unchanged — the literal
    connectors in it are still whatever the dataset says, which
    :mod:`bench.market_preflight` reports on separately.
    """
    case_id = getattr(case, "id", "?")
    requirements = requirements_for(case)
    if not requirements:
        return Resolution(case_id=case_id, case=case)

    resolution = Resolution(case_id=case_id, case=case)
    for req in requirements:
        binding = await registry.resolve(
            kind=req.kind,
            needs_credentials=req.needs_credentials,
            prefer=list(req.prefer) + list(prefer),
            pair_candidates=req.pairs,
            namespace=req.namespace,
            allow_kind_change=req.allow_kind_change,
            # The first preference is the connector the case would have
            # hardcoded, so it keeps warmup's benefit of the doubt on empty
            # trading rules; a substitute has to prove it lists the pair.
            optimistic_for=req.prefer[0] if req.prefer else "",
        )
        if binding is None:
            if not registry.namespace_readable(req.namespace):
                why = (
                    f"{req.namespace} namespace unreadable: "
                    f"{registry.gateway_error or registry.error}"
                )
            else:
                wants = f"{req.kind or 'any-kind'} {req.namespace}"
                pairs = f" trading {'/'.join(req.pairs)}" if req.pairs else ""
                why = (
                    f"no {req.needs} {wants} connector{pairs} on "
                    f"{registry.api_url or 'this target'}"
                )
            resolution.unmet[req.name] = why
            continue
        resolution.bindings[req.name] = binding
        if binding.kind_change:
            resolution.notes.append(f"{req.name} crossed {binding.kind_change}")

    if not resolution.ok:
        return resolution

    changes = {}
    for f in dataclasses.fields(case):
        if f.name in _SKIP_FIELDS:
            continue
        current = getattr(case, f.name, None)
        replaced = substitute(current, resolution.bindings)
        if replaced != current:
            changes[f.name] = replaced
    resolution.case = dataclasses.replace(case, **changes) if changes else case

    leftover = unresolved_placeholders(resolution.case)
    if leftover:
        # A placeholder no requirement declares is a dataset bug, and letting it
        # reach the model would score the typo rather than the model.
        resolution.unmet["<placeholders>"] = (
            f"{', '.join(leftover)} match no declared market "
            f"({', '.join(sorted(resolution.bindings)) or 'none declared'})"
        )
    return resolution


async def resolve_cases(
    cases: list[Any],
    *,
    registry: MarketRegistry | None = None,
    prefer: Iterable[str] = (),
) -> tuple[list[Any], dict[str, Resolution]]:
    """Resolve every case, returning substituted cases and their resolutions.

    Cases that failed to bind are returned *unsubstituted* and their resolution
    carries the reason. Callers gate on :func:`assert_resolvable` rather than
    filtering here, so nothing silently drops out of a run.
    """
    registry = registry if registry is not None else await probe_registry()
    resolved: list[Any] = []
    resolutions: dict[str, Resolution] = {}
    for case in cases:
        resolution = await resolve_case(case, registry, prefer=prefer)
        resolutions[resolution.case_id] = resolution
        resolved.append(resolution.case)
    return resolved, resolutions


def assert_resolvable(resolutions: dict[str, Resolution]) -> None:
    """Raise unless every declared requirement bound to something.

    Fail-closed, like the staging pre-flight: a run that silently skipped the
    leverage cases because the box lost its perp credentials would publish a
    routing recommendation with a hole in it, and nothing in the summary would
    say so.
    """
    failed = {cid: r for cid, r in resolutions.items() if not r.ok}
    if not failed:
        return
    reasons = " ".join(r.reason() for r in failed.values())
    # The fix differs by cause, and the message is what someone reads while
    # blocked: telling them to add credentials for a connector behind a gateway
    # that is not running sends them to the wrong place entirely.
    fix = (
        "Start the gateway service"
        if GATEWAY in reasons
        else "Add credentials for one of the preferred connectors"
    )
    lines = [
        f"{len(failed)} case(s) declare markets this target cannot provide:",
        *(f"  {cid}: {r.reason()}" for cid, r in sorted(failed.items())),
        "",
        f"{fix}, or narrow the run (--layers / --tags / --risk) to exclude these "
        "cases. `make market-check` reports the whole dataset.",
    ]
    raise MarketsUnavailable("\n".join(lines))


def bindings_summary(resolutions: dict[str, Resolution]) -> dict[str, Any]:
    """Per-case bindings for the run record, plus the connectors actually used."""
    per_case = {
        cid: r.as_dict() for cid, r in sorted(resolutions.items()) if r.bindings
    }
    connectors = sorted(
        {b.connector for r in resolutions.values() for b in r.bindings.values()}
    )
    notes = sorted({note for r in resolutions.values() for note in r.notes})
    return {"cases": per_case, "connectors": connectors, "notes": notes}


async def prepare_cases(
    cases: list[Any],
    *,
    registry: MarketRegistry | None = None,
    prefer: Iterable[str] = (),
) -> tuple[list[Any], dict[str, Resolution]]:
    """Resolve the selected cases or refuse the run.

    The gate is deliberately on the *selected* cases, not the whole library: a
    box without gateway credentials should still be able to run the CEX layers,
    and blocking those would make the fail-closed behaviour something operators
    route around rather than rely on.
    """
    resolved, resolutions = await resolve_cases(cases, registry=registry, prefer=prefer)
    assert_resolvable(resolutions)
    return resolved, resolutions


def nominal_binding(req: Requirement) -> Binding:
    """The market a requirement names on paper, with no target involved.

    Resolution needs a live box; display and offline tooling do not. Taking the
    first ``prefer`` entry and the first pair reproduces what the case would have
    hardcoded, which is what a case list, a generated prompt map or a diff should
    show. Never use it to run a case: nothing here has been checked against a
    target, so the connector may not exist and may hold no keys.
    """
    return Binding(
        connector=req.prefer[0] if req.prefer else "",
        account="",
        trading_pair=req.pairs[0] if req.pairs else "",
    )


def render_nominal(case: Any) -> Any:
    """A copy of the case with placeholders filled from the declared preferences.

    Returns the case unchanged when it declares no markets.
    """
    try:
        requirements = requirements_for(case)
    except ValueError:
        return case
    if not requirements:
        return case
    bindings = {req.name: nominal_binding(req) for req in requirements}
    changes = {}
    for f in dataclasses.fields(case):
        if f.name in _SKIP_FIELDS:
            continue
        current = getattr(case, f.name, None)
        replaced = substitute(current, bindings)
        if replaced != current:
            changes[f.name] = replaced
    return dataclasses.replace(case, **changes) if changes else case


def unresolvable_card(case: Any, model: str, resolution: Resolution):
    """Scorecard marking a case skipped because its markets could not bind.

    The same treatment :mod:`bench.market_warmup` gives a cold market: excluded
    from routing, with the reason attached. Only reachable via the explicit
    ``--skip-unavailable`` opt-out — the default is to refuse the run, because a
    silently thinner run still publishes a routing recommendation.
    """
    from bench.scorer import ScoreCard

    return ScoreCard(
        case_id=getattr(case, "id", "?"),
        model=model,
        category=getattr(case, "category", "") or "",
        case_type=getattr(case, "type", "") or "",
        domain=getattr(case, "domain", "") or "",
        risk_level=getattr(case, "risk_level", "read_only") or "read_only",
        answer_quality=0.0,
        answer_reason="skipped — declared markets unavailable on this target",
        tool_accuracy=None,
        tool_params=None,
        live_validity=None,
        latency_score=0.0,
        composite=0.0,
        latency_s=0.0,
        baseline_latency_s=0.0,
        expected_tools=list(getattr(case, "expected_tools", []) or []),
        harness_artifact=(
            f"declared markets unmet — {resolution.reason()}; the case was not "
            "run. This is target readiness, not a model failure"
        ),
    )


def split_resolvable(
    cases: list[Any], resolutions: dict[str, Resolution]
) -> tuple[list[Any], list[Any]]:
    """(runnable, unrunnable) split for the ``--skip-unavailable`` path."""
    bad = {cid for cid, r in resolutions.items() if not r.ok}
    runnable = [c for c in cases if getattr(c, "id", "?") not in bad]
    skipped = [c for c in cases if getattr(c, "id", "?") in bad]
    return runnable, skipped
