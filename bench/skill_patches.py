"""Temporary patches applied to the Condor checkout before a run.

These compensate for known Condor bugs that poison benchmark cases until the
upstream fix lands. Each patch is a no-op once Condor no longer contains the
bad snippet — delete the entry when the Condor issue closes.

Patches write into ``CONDOR_PATH`` (the live skill the model reads via
``manage_skill``). That dirties the checkout on purpose: leaving the wrong
cookbook in place made ``agent_condor_routine_001`` invent a working create that
could never read a price, then guess ``binance_paper_trade``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import condor_path

# Known-bad cookbook snippet: teaches a flat get_prices map.
# Live API returns ``{"connector", "prices": {...}, "timestamp"}``. The model that
# followed the cookbook did ``prices.get("BTC-USDT")`` → None despite a live
# Binance price (agent_condor_routine_001).
_GET_PRICES_BAD = (
    'prices = await client.market_data.get_prices(connector, trading_pairs=["BTC-USDT", "ETH-USDT"])\n'
    '# Returns: {"BTC-USDT": 100000.0, "ETH-USDT": 3500.0}\n'
    'btc_price = prices.get("BTC-USDT", 0)'
)

_GET_PRICES_GOOD = (
    'prices = await client.market_data.get_prices(connector, trading_pairs=["BTC-USDT", "ETH-USDT"])\n'
    '# Returns: {"connector": "binance", "prices": {"BTC-USDT": 100000.0, "ETH-USDT": 3500.0}, "timestamp": …}\n'
    'btc_price = prices.get("prices", {}).get("BTC-USDT", 0)'
)

# (name, relative path under CONDOR_PATH, bad, good, why)
_PATCHES: list[tuple[str, str, str, str, str]] = [
    (
        "routine_cookbook_get_prices_shape",
        "agents/_shared/skills/routine_cookbook/hummingbot_client.md",
        _GET_PRICES_BAD,
        _GET_PRICES_GOOD,
        "get_prices response is nested under prices{}; cookbook showed a flat map. "
        "Remove this patch when Condor fixes hummingbot_client.md.",
    ),
]


@dataclass(frozen=True)
class PatchResult:
    name: str
    path: str
    status: str  # "applied" | "already_correct" | "missing" | "unchanged_unknown"
    detail: str


def apply_skill_patches(repo: Path | None = None) -> list[PatchResult]:
    """Rewrite known-bad skill snippets on the Condor checkout.

    Safe to call repeatedly: once the bad text is gone the patch is a no-op.
    """
    root = repo if repo is not None else condor_path()
    if root is None:
        return [
            PatchResult(
                name="skill_patches",
                path="",
                status="missing",
                detail="no CONDOR_PATH — skill patches skipped",
            )
        ]

    results: list[PatchResult] = []
    for name, rel, bad, good, why in _PATCHES:
        path = Path(root) / rel
        results.append(_apply_one(name, path, bad, good, why))
    return results


def _apply_one(
    name: str, path: Path, bad: str, good: str, why: str
) -> PatchResult:
    rel = str(path)
    if not path.is_file():
        return PatchResult(
            name=name,
            path=rel,
            status="missing",
            detail=f"{path} not found — {why}",
        )

    text = path.read_text(encoding="utf-8")
    if bad in text:
        path.write_text(text.replace(bad, good, 1), encoding="utf-8")
        return PatchResult(
            name=name,
            path=rel,
            status="applied",
            detail=f"rewrote get_prices example — {why}",
        )
    if good in text or 'prices.get("prices"' in text:
        return PatchResult(
            name=name,
            path=rel,
            status="already_correct",
            detail="cookbook already documents the nested prices{} shape",
        )
    return PatchResult(
        name=name,
        path=rel,
        status="unchanged_unknown",
        detail=(
            "file present but neither the known-bad nor known-good snippet matched "
            f"— check manually. {why}"
        ),
    )
