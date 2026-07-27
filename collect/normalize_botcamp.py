"""Normalize the Botcamp strategy corpus into corpus/botcamp.jsonl.

One record per certified strategy from "Botcamp Strategy Designs.csv" —
the raw material for accuracy-instance curation (botcamp_curated.yaml) and
Track B question mining. Run: make normalize (any Python 3.11+, stdlib only).
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = Path.home() / "botcamp-site" / "Botcamp Strategy Designs.csv"
OUT_PATH = BENCH_ROOT / "corpus" / "botcamp.jsonl"

# Coarse category from the site's Strategy Type, refined by name/description
# keywords where the CSV field is blank.
_KEYWORDS = [
    ("market-making", re.compile(r"\b(market.mak|pmm|spread|quot)", re.I)),
    ("arbitrage", re.compile(r"\b(arb|xemm|cross.exchange|funding rate)", re.I)),
    ("grid", re.compile(r"\bgrid\b", re.I)),
    ("mean-reversion", re.compile(r"\b(mean rever|rsi|bollinger|chop|scalp)", re.I)),
    ("trend-following", re.compile(r"\b(trend|momentum|crossover|breakout|macd)", re.I)),
    ("execution", re.compile(r"\b(vwap|twap|without moving the price)", re.I)),
    ("lp", re.compile(r"\b(liquidity pool|clmm|dlmm|\blp\b)", re.I)),
]


def _slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:60]


def _category(row: dict) -> str:
    typed = (row.get("Strategy Type") or "").strip().lower()
    mapped = {
        "directional": "directional",
        "arbitrage": "arbitrage",
        "xemm": "arbitrage",
        "pmm": "market-making",
        "lp": "lp",
        "index": "index",
        "screener": "screener",
    }.get(typed)
    if mapped and mapped != "directional":
        return mapped
    text = f"{row.get('Name', '')} {row.get('Description', '')}"
    for cat, pattern in _KEYWORDS:
        if pattern.search(text):
            return cat
    return mapped or "other"


def main() -> int:
    if not CSV_PATH.exists():
        print(f"botcamp CSV not found: {CSV_PATH}", file=sys.stderr)
        return 1
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT_PATH.open("w") as out:
        for row in rows:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            record = {
                "source": "botcamp",
                "id": _slug(name),
                "name": name,
                "category": _category(row),
                "strategy_type": (row.get("Strategy Type") or "").strip(),
                "description": (row.get("Description") or "").strip(),
                "exchanges": [
                    e.strip() for e in (row.get("Exchange") or "").split(",") if e.strip()
                ],
                "code_type": (row.get("Code Type") or "").strip(),
                "code_link": (row.get("Code Link") or "").strip(),
                "cohort": (row.get("Cohort") or "").strip(),
            }
            out.write(json.dumps(record) + "\n")
            n += 1
    print(f"wrote {n} strategies -> {OUT_PATH.relative_to(BENCH_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
