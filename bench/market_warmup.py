"""Warm markets a case needs before the model runs.

``manage_executors(create)`` for a grid on XRPL (and similar thin connectors)
fails with ``KeyError: '<pair>'`` when ``connector.trading_rules`` does not yet
contain the pair — even though hummingbot-api's create path calls
``add_market``. Order-book init can succeed while trading rules lag.

That race is infrastructure, not a model skill. Asking the agent to call
``market-data/trading-pair/add`` first would muddy tool scoring for create
cases. Instead the harness warms every ``(connector, pair)`` pinned in the
case's ground truth, and a warmup failure is recorded as a harness artifact
(excluded from routing), the same treatment staging blips already get.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import staging_config


@dataclass(frozen=True)
class MarketRef:
    connector_name: str
    trading_pair: str

    def __str__(self) -> str:
        return f"{self.connector_name}/{self.trading_pair}"


@dataclass
class WarmupReport:
    markets: list[MarketRef] = field(default_factory=list)
    warmed: list[MarketRef] = field(default_factory=list)
    failed: list[tuple[MarketRef, str]] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return bool(self.markets)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def detail(self) -> str:
        if not self.markets:
            return "no markets to warm"
        if self.ok:
            return (
                f"warmed {len(self.warmed)} market(s): "
                + ", ".join(str(m) for m in self.warmed)
            )
        parts = [f"{m}: {err}" for m, err in self.failed]
        return "market warmup failed — " + "; ".join(parts)

    def harness_artifact_reason(self) -> str | None:
        if self.ok:
            return None
        return (
            f"{self.detail} — the case was not run; this is staging readiness, "
            "not a model failure"
        )


def markets_from_case(case: Any) -> list[MarketRef]:
    """``(connector, pair)`` pins from case ground truth, de-duplicated."""
    found: list[MarketRef] = []
    seen: set[tuple[str, str]] = set()

    def _add(connector: Any, pair: Any) -> None:
        c = str(connector or "").strip()
        p = str(pair or "").strip()
        if not c or not p:
            return
        key = (c, p)
        if key in seen:
            return
        seen.add(key)
        found.append(MarketRef(c, p))

    params = getattr(case, "expected_tool_params", None) or {}
    if isinstance(params, dict):
        for args in params.values():
            if not isinstance(args, dict):
                continue
            connector = args.get("connector_name") or args.get("connector")
            pair = args.get("trading_pair")
            pairs = args.get("trading_pairs")
            if pair is not None:
                _add(connector, pair)
            if isinstance(pairs, str):
                _add(connector, pairs)
            elif isinstance(pairs, (list, tuple)):
                for item in pairs:
                    _add(connector, item)

    # Tick cases often pin the market on config rather than expected_tool_params.
    config = getattr(case, "config", None) or {}
    if isinstance(config, dict):
        _add(
            config.get("connector_name") or config.get("connector"),
            config.get("trading_pair"),
        )

    return found


async def ensure_markets_for_case(
    case: Any,
    *,
    timeout_s: float = 45.0,
    poll_s: float = 1.0,
) -> WarmupReport:
    """Add + wait until trading rules list each market the case pins.

    No-ops when the case does not pin a connector/pair. Uses the same staging
    credentials as the rest of the pre-flight.
    """
    markets = markets_from_case(case)
    report = WarmupReport(markets=list(markets))
    if not markets:
        return report

    staging = staging_config()
    url = str(staging["api_url"] or "").rstrip("/")
    username = str(staging["username"] or "")
    password = str(staging["password"] or "")
    if not url:
        for market in markets:
            report.failed.append((market, "HUMMINGBOT_API_URL unset"))
        return report

    auth = httpx.BasicAuth(username, password) if username else None
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=min(30.0, timeout_s), auth=auth) as client:
        for market in markets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report.failed.append((market, "timed out before starting"))
                continue
            try:
                await _warm_one(client, url, market, deadline=deadline, poll_s=poll_s)
            except Exception as exc:
                report.failed.append((market, f"{type(exc).__name__}: {exc}"))
            else:
                report.warmed.append(market)
    return report


async def _warm_one(
    client: httpx.AsyncClient,
    url: str,
    market: MarketRef,
    *,
    deadline: float,
    poll_s: float,
) -> None:
    add = await client.post(
        f"{url}/market-data/trading-pair/add",
        json={
            "connector_name": market.connector_name,
            "trading_pair": market.trading_pair,
        },
    )
    if add.status_code >= 400:
        body = add.text[:300]
        raise RuntimeError(
            f"trading-pair/add HTTP {add.status_code}: {body or add.reason_phrase}"
        )

    last_detail = "waiting for trading rules"
    while time.monotonic() < deadline:
        if await _rules_ready(client, url, market):
            # Mid price is best-effort: rules are the create gate, but a mid
            # confirms the book is live enough for a grid to size itself.
            if await _price_ready(client, url, market):
                return
            last_detail = "trading rules ready; waiting for mid price"
        else:
            last_detail = "trading rules do not yet list the pair"
        await asyncio.sleep(poll_s)

    raise TimeoutError(last_detail)


async def _rules_ready(
    client: httpx.AsyncClient, url: str, market: MarketRef
) -> bool:
    r = await client.get(f"{url}/connectors/{market.connector_name}/trading-rules")
    if r.status_code >= 400:
        return False
    try:
        data = r.json()
    except Exception:
        return False
    return market.trading_pair in _rule_keys(data)


def _rule_keys(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        # Shape from hummingbot-api: { "RLUSD-XRP": { ...rules... }, ... }
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return {str(k) for k in payload}
        for key in ("trading_rules", "data", "rules"):
            if key in payload:
                return _rule_keys(payload[key])
        return {str(k) for k in payload}
    if isinstance(payload, list):
        out: set[str] = set()
        for item in payload:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                name = item.get("trading_pair") or item.get("pair") or item.get("symbol")
                if name:
                    out.add(str(name))
        return out
    return set()


async def _price_ready(
    client: httpx.AsyncClient, url: str, market: MarketRef
) -> bool:
    r = await client.post(
        f"{url}/market-data/prices",
        json={
            "connector_name": market.connector_name,
            "trading_pairs": [market.trading_pair],
        },
    )
    if r.status_code >= 400:
        return False
    try:
        data = r.json()
    except Exception:
        return False
    prices = data.get("prices") if isinstance(data, dict) else None
    if not isinstance(prices, dict):
        return False
    value = prices.get(market.trading_pair)
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def warmup_failure_card(case: Any, model: str, report: WarmupReport):
    """Scorecard that marks a skipped case as a harness artifact."""
    from bench.scorer import ScoreCard

    reason = report.harness_artifact_reason() or report.detail
    return ScoreCard(
        case_id=getattr(case, "id", "?"),
        model=model,
        category=getattr(case, "category", "") or "",
        case_type=getattr(case, "type", "") or "",
        domain=getattr(case, "domain", "") or "",
        risk_level=getattr(case, "risk_level", "read_only") or "read_only",
        answer_quality=0.0,
        answer_reason="skipped — market warmup failed",
        tool_accuracy=None,
        tool_params=None,
        live_validity=None,
        latency_score=0.0,
        composite=0.0,
        latency_s=0.0,
        baseline_latency_s=0.0,
        expected_tools=list(getattr(case, "expected_tools", []) or []),
        harness_artifact=reason,
    )
