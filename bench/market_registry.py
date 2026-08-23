"""What the target box can actually trade, probed once and reused.

The datasets name connectors literally — "Set Binance perpetuals to 3x leverage"
— and a literal is a claim about the box the run lands on. When that claim is
wrong the model still gets scored: it calls ``binance_perpetual``, the API
answers ``missing 2 required positional arguments: 'binance_api_key'``, and
:mod:`metrics.live_validity` reads that as a failed tool call. The model did
nothing wrong; the dataset asked for a market that does not exist here.

This module is the box-side half of the fix — the facts a case has to be checked
against, in three tiers that must not be conflated:

``supported``
    ``GET /connectors/`` — every connector the hummingbot build knows how to
    construct. Says nothing about whether it can trade. All 60-odd are listed on
    a stock install.

``credentialed``
    ``GET /accounts/{account}/credentials`` — connectors with API keys on an
    account. Required by anything that acts *as* the account: leverage, position
    mode, executors, bots. This is the tier that actually gates a case, and the
    one the datasets implicitly assume.

``pairs``
    ``GET /connectors/{name}/trading-rules`` — public, no credentials needed, so
    a pair can look perfectly available on a connector that cannot trade a lot
    size. Also the tier :mod:`bench.market_warmup` exists to populate, and it
    can legitimately be empty for a moment on a thin connector.

Probe failures return a registry with ``ok=False`` rather than an empty one. An
empty registry is indistinguishable from "nothing is credentialed", which would
let an unreachable API report every case as broken — the opposite of the
fail-closed behaviour the rest of the pre-flight has.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from config import staging_config

# Substring, not suffix: the testnet variants are `binance_perpetual_testnet`,
# so a suffix test on `_perpetual` misses exactly the connectors a staging box is
# most likely to have keys for.
_PERPETUAL_MARKER = "_perpetual"
_TESTNET_MARKERS = ("_testnet", "_sandbox")

PERPETUAL = "perpetual"
SPOT = "spot"

# Two namespaces, two endpoints, and never interchangeable. A CEX connector is
# keyed by API credentials on an account; a gateway (DEX/AMM) connector is keyed
# by a wallet and lives behind `/gateway/connectors`, which is a separate service
# that can be down while the API is perfectly healthy. Swapping across them turns
# "find SOL pools on Meteora" into a centralised-exchange query — a different
# question, not a rebind.
CEX = "cex"
GATEWAY = "gateway"


@dataclass(frozen=True)
class Connector:
    """One connector on the target, with the accounts holding keys for it."""

    name: str
    credentialed_on: tuple[str, ...] = ()
    namespace: str = CEX

    @property
    def kind(self) -> str:
        return PERPETUAL if _PERPETUAL_MARKER in self.name else SPOT

    @property
    def is_testnet(self) -> bool:
        return any(marker in self.name for marker in _TESTNET_MARKERS)

    @property
    def credentialed(self) -> bool:
        return bool(self.credentialed_on)


@dataclass(frozen=True)
class Binding:
    """A resolved market: a connector that exists, with keys, and a pair."""

    connector: str
    account: str
    trading_pair: str = ""
    # Set when the binding had to cross spot/perpetual to find anything, e.g.
    # "spot → perpetual". Non-empty means the case's meaning shifted.
    kind_change: str = ""

    @property
    def kind(self) -> str:
        """Derived from the connector id, never passed in separately.

        These were fields once, and a Binding built outside the registry could
        then claim ``binance_perpetual_testnet`` was spot — which the label
        rendered into the question as plain "Binance". The name is the one source
        that cannot disagree with itself.
        """
        return PERPETUAL if _PERPETUAL_MARKER in self.connector else SPOT

    @property
    def is_testnet(self) -> bool:
        return any(marker in self.connector for marker in _TESTNET_MARKERS)

    @property
    def pair(self) -> str:
        """Alias for :attr:`trading_pair`, so ``{name.pair}`` reads naturally.

        The placeholder vocabulary and the field names have to agree: when they
        did not, ``{perp.pair}`` resolved to an empty string and the model was
        asked to set leverage "for ." with the pair silently gone from the
        ground truth too.
        """
        return self.trading_pair

    @property
    def label(self) -> str:
        """Human name for the connector, for substitution into a question.

        ``binance_perpetual_testnet`` → "Binance perpetuals (testnet)". The model
        is asked in prose and answers in tool calls, so the prose has to name
        something a reader would recognise while the tool call gets the exact
        connector id.
        """
        base = self.connector
        for marker in _TESTNET_MARKERS:
            base = base.replace(marker, "")
        words = base.replace(_PERPETUAL_MARKER, "").replace("_", " ").strip()
        name = words.title() if words else base
        if self.kind == PERPETUAL:
            name = f"{name} perpetuals"
        return f"{name} (testnet)" if self.is_testnet else name


@dataclass
class MarketRegistry:
    """Connector availability on one target. Build with :func:`probe_registry`."""

    api_url: str = ""
    accounts: tuple[str, ...] = ()
    connectors: dict[str, Connector] = field(default_factory=dict)
    ok: bool = True
    error: str = ""
    # Gateway is a separate service. `gateway_ok=False` means "could not tell
    # what DEX connectors exist", which is not the same as "none exist" — a case
    # naming one has to come back unknown rather than broken.
    gateway: dict[str, Connector] = field(default_factory=dict)
    gateway_ok: bool = True
    gateway_error: str = ""
    # trading-rules is one request per connector, so it is fetched on demand and
    # cached rather than swept for all 60 during a pre-flight.
    _pairs: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    def pool(self, namespace: str = CEX) -> dict[str, Connector]:
        return self.gateway if namespace == GATEWAY else self.connectors

    def namespace_readable(self, namespace: str = CEX) -> bool:
        """Whether the namespace could be enumerated at all.

        Guards the difference between "this connector does not exist here" and
        "the service that would have told me is down".
        """
        return self.gateway_ok if namespace == GATEWAY else self.ok

    def supported(self, name: str, namespace: str = CEX) -> bool:
        return name in self.pool(namespace)

    def credentialed(self, name: str, namespace: str = CEX) -> bool:
        conn = self.pool(namespace).get(name)
        return bool(conn and conn.credentialed)

    def kind(self, name: str, namespace: str = CEX) -> str:
        conn = self.pool(namespace).get(name)
        return conn.kind if conn else (PERPETUAL if _PERPETUAL_MARKER in name else SPOT)

    def credentialed_names(self, kind: str | None = None) -> list[str]:
        return sorted(
            c.name
            for c in self.connectors.values()
            if c.credentialed and (kind is None or c.kind == kind)
        )

    def candidates(
        self,
        *,
        kind: str | None = None,
        needs_credentials: bool = True,
        prefer: Iterable[str] = (),
        namespace: str = CEX,
    ) -> list[str]:
        """Connectors that satisfy a requirement, best first.

        ``prefer`` wins in the order given, then everything else alphabetically.
        Deterministic on purpose: a resolver that picked differently on two runs
        against the same box would make the two runs' scores incomparable while
        looking like model variance.
        """
        pool = [
            c
            for c in self.pool(namespace).values()
            if (kind is None or c.kind == kind)
            and (c.credentialed if needs_credentials else True)
        ]
        names = {c.name for c in pool}
        ranked = [name for name in prefer if name in names]
        ranked += sorted(names - set(ranked))
        return ranked

    def account_for(self, connector: str, namespace: str = CEX) -> str:
        """The account holding keys for a connector, or the first known account."""
        conn = self.pool(namespace).get(connector)
        if conn and conn.credentialed_on:
            return conn.credentialed_on[0]
        return self.accounts[0] if self.accounts else ""

    async def pairs(self, connector: str, *, timeout_s: float = 20.0) -> tuple[str, ...]:
        """Trading pairs the connector currently lists, cached per registry.

        Empty is a real answer here, not an error: a thin connector reports no
        rules until something warms it (see :mod:`bench.market_warmup`).
        """
        if connector in self._pairs:
            return self._pairs[connector]
        pairs: tuple[str, ...] = ()
        if self.api_url:
            auth = _auth()
            try:
                async with httpx.AsyncClient(timeout=timeout_s, auth=auth) as client:
                    r = await client.get(f"{self.api_url}/connectors/{connector}/trading-rules")
                    if r.status_code < 400:
                        payload = r.json()
                        if isinstance(payload, dict):
                            pairs = tuple(sorted(str(k) for k in payload))
            except Exception:
                # Unknown, not empty — but a failed rules fetch must not be
                # cached as "this connector has no markets", so leave the cache
                # alone and let the next call retry.
                return ()
        self._pairs[connector] = pairs
        return pairs

    async def resolve(
        self,
        *,
        kind: str | None = None,
        needs_credentials: bool = True,
        prefer: Iterable[str] = (),
        pair: str = "",
        pair_candidates: Iterable[str] = (),
        namespace: str = CEX,
        allow_kind_change: bool = False,
        optimistic_for: str = "",
    ) -> Binding | None:
        """Bind a requirement to a concrete market, or ``None`` when nothing fits.

        ``None`` is the signal a case is unrunnable on this target. Callers must
        treat it as a harness artifact, never as a model score of zero.

        A candidate that cannot trade any of the requested pairs is rejected, not
        merely deprioritised — the first credentialed connector on a box is not
        automatically a place a BTC-USDT grid can run, and binding to one would
        hand the model a market that does not exist.

        ``allow_kind_change`` widens a failed same-kind search to any kind
        (spot → perpetual). Off by default because it changes what the case asks;
        the binding records the swap in :attr:`Binding.kind_change` so a caller
        can surface it for a human decision rather than applying it silently.

        ``optimistic_for`` names the one connector allowed to pass on an empty
        trading-rules response — normally the connector the case already names,
        which :mod:`bench.market_warmup` is about to populate. Every *other*
        candidate must positively list the pair: a thin connector reporting no
        rules is not evidence it can trade BTC-USDT, and treating it as such is
        how "rebind the Binance grid" becomes "run it on XRPL".
        """
        attempts: list[str | None] = [kind]
        if allow_kind_change and kind is not None:
            attempts.append(None)
        wanted = [p for p in ([pair] if pair else []) + list(pair_candidates) if p]
        for attempt_kind in attempts:
            for name in self.candidates(
                kind=attempt_kind,
                needs_credentials=needs_credentials,
                prefer=prefer,
                namespace=namespace,
            ):
                chosen = ""
                if wanted:
                    available = await self.pairs(name)
                    if not available:
                        # No rules yet is only forgivable for the connector the
                        # case already names — warmup runs before the case and
                        # will populate them, and a genuine failure there gets
                        # reported with warmup's own message.
                        if name != optimistic_for:
                            continue
                        chosen = wanted[0]
                    else:
                        chosen = next((p for p in wanted if p in available), "")
                        if not chosen:
                            continue
                found_kind = self.kind(name, namespace)
                return Binding(
                    connector=name,
                    account=self.account_for(name, namespace),
                    trading_pair=chosen,
                    kind_change=(
                        f"{kind} → {found_kind}" if kind and found_kind != kind else ""
                    ),
                )
        return None


def _auth() -> httpx.BasicAuth | None:
    staging = staging_config()
    username = str(staging["username"] or "")
    password = str(staging["password"] or "")
    return httpx.BasicAuth(username, password) if username else None


def _account_names(payload: Any) -> list[str]:
    if isinstance(payload, list):
        out = []
        for item in payload:
            if isinstance(item, dict):
                name = item.get("account_name") or item.get("name")
                if name:
                    out.append(str(name))
            elif item:
                out.append(str(item))
        return out
    if isinstance(payload, dict):
        for key in ("accounts", "data", "items"):
            if key in payload:
                return _account_names(payload[key])
    return []


def _connector_names(payload: Any) -> list[str]:
    """Connector ids out of either endpoint's shape.

    ``/connectors/`` answers a flat list of strings; ``/gateway/connectors``
    answers ``{"connectors": [{"name": "meteora", "chain": …}, …]}``. Dropping
    the dict form is how a healthy gateway once read as "listed no connectors".
    """
    if isinstance(payload, list):
        names = []
        for item in payload:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("connector") or item.get("id")
                if name:
                    names.append(str(name))
        return names
    if isinstance(payload, dict):
        for key in ("connectors", "data", "items"):
            if key in payload:
                return _connector_names(payload[key])
        return [str(k) for k in payload]
    return []


async def probe_registry(*, timeout_s: float = 20.0) -> MarketRegistry:
    """Read supported connectors and per-account credentials off the target.

    Two requests plus one per account. Trading pairs are left to
    :meth:`MarketRegistry.pairs` so a pre-flight over 80 cases costs three calls,
    not eighty.
    """
    staging = staging_config()
    url = str(staging["api_url"] or "").rstrip("/")
    if not url:
        return MarketRegistry(ok=False, error="HUMMINGBOT_API_URL is not set")

    try:
        async with httpx.AsyncClient(timeout=timeout_s, auth=_auth()) as client:
            # The trailing slash is load-bearing: hummingbot-api mounts these
            # routers with redirects off, so the bare path is a hard 404.
            connectors_r, accounts_r = await asyncio.gather(
                client.get(f"{url}/connectors/"),
                client.get(f"{url}/accounts/"),
            )
            if connectors_r.status_code == 401 or accounts_r.status_code == 401:
                return MarketRegistry(
                    api_url=url,
                    ok=False,
                    error=f"{url} rejected the bench credentials (401)",
                )
            connectors_r.raise_for_status()
            accounts_r.raise_for_status()
            supported = _connector_names(connectors_r.json())
            accounts = _account_names(accounts_r.json())

            creds: dict[str, list[str]] = {}
            results = await asyncio.gather(
                *(
                    client.get(f"{url}/accounts/{account}/credentials")
                    for account in accounts
                ),
                return_exceptions=True,
            )
            for account, result in zip(accounts, results):
                if isinstance(result, BaseException) or result.status_code >= 400:
                    continue
                try:
                    named = _connector_names(result.json())
                except Exception:
                    continue
                for name in named:
                    creds.setdefault(name, []).append(account)
    except Exception as exc:
        return MarketRegistry(
            api_url=url, ok=False, error=f"{url} unreachable: {type(exc).__name__}: {exc}"
        )

    # A credentialed connector missing from /connectors/ is still real — keys
    # exist for it — so union both sides rather than intersecting.
    names = set(supported) | set(creds)
    gateway, gateway_ok, gateway_error = await _probe_gateway(url, timeout_s)
    return MarketRegistry(
        api_url=url,
        accounts=tuple(accounts),
        connectors={
            name: Connector(name=name, credentialed_on=tuple(sorted(creds.get(name, []))))
            for name in sorted(names)
        },
        gateway=gateway,
        gateway_ok=gateway_ok,
        gateway_error=gateway_error,
    )


async def _probe_gateway(
    url: str, timeout_s: float
) -> tuple[dict[str, Connector], bool, str]:
    """DEX connectors behind the gateway service, if it is up.

    A down gateway is reported as unreadable, not as empty. hummingbot-api
    answers ``{"detail": "Gateway service is not available"}`` with a 2xx or 5xx
    depending on version, so the body is checked as well as the status.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s, auth=_auth()) as client:
            r = await client.get(f"{url}/gateway/connectors")
    except Exception as exc:
        return {}, False, f"gateway unreachable: {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return {}, False, f"gateway returned HTTP {r.status_code}"
    try:
        payload = r.json()
    except Exception:
        return {}, False, "gateway returned a non-JSON body"
    if isinstance(payload, dict) and "detail" in payload:
        return {}, False, str(payload["detail"])
    named = _connector_names(payload)
    if not named:
        # An empty payload is a real answer — a gateway with nothing configured.
        # A *non-empty* payload that yielded no names is a shape we do not
        # understand, and saying "no connectors" there sent a debugging session
        # after the container when the parser was at fault.
        if not payload:
            return {}, True, ""
        return {}, False, f"gateway response not understood: {str(payload)[:200]}"
    return (
        {
            name: Connector(name=name, namespace=GATEWAY)
            for name in sorted(named)
        },
        True,
        "",
    )
