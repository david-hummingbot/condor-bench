"""Compact tool-output digests for the quality judge.

The judge must verify that figures in the model answer appear in tool output.
Naively truncating the first N characters fails on live portfolios: hundreds of
zero-priced dust rows push totals and meaningful holdings past the cutoff, so a
correct answer looks fabricated.

A digest keeps the facts an answer would cite (totals, top holdings by USD,
position/order counts, scalar JSON fields) and drops the long tail.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Soft budget for one tool's digest in the judge transcript. Larger than the old
# raw 700-char head truncate because digests are already filtered; still small
# enough that a multi-tool turn cannot crowd out the model response.
DEFAULT_DIGEST_CHARS = 1600

_NUM_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_SUMMARY_LINE_RE = re.compile(
    r"(?i)\b("
    r"total\b|summary\b|available\b|mid(?:[_\s-]?price)?\b|price\b|"
    r"funding\b|positions?\b|orders?\b|balance\b|value\b|error\b|failed\b"
    r")"
)
_PORTFOLIO_HINTS = ("portfolio overview", "token balances", "total balance value")


def digest_tool_output(
    tool_name: str,
    output: Any,
    *,
    max_chars: int = DEFAULT_DIGEST_CHARS,
) -> str:
    """Return a judge-facing digest of a tool response.

    Short outputs pass through unchanged. Long ones are summarised by tool-
    specific or generic heuristics so cited figures stay visible.
    """
    if output is None:
        return "(no output captured)"

    text, structured = _as_text_and_structured(output)
    if not text.strip() and structured is None:
        return "(empty)"

    name = (tool_name or "").lower()
    digesters = (
        _digest_portfolio if _looks_like_portfolio(name, text, structured) else None,
        _digest_structured if structured is not None else None,
        _digest_pipe_table if "|" in text and text.count("\n") >= 3 else None,
        _digest_generic_text,
    )
    for digester in digesters:
        if digester is None:
            continue
        digest = digester(text, structured, max_chars=max_chars)
        if digest:
            return _fit(digest, max_chars)

    return _fit(text, max_chars)


def _as_text_and_structured(output: Any) -> tuple[str, Any | None]:
    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output, default=str), output
        except (TypeError, ValueError):
            return str(output), output
    if not isinstance(output, str):
        return str(output), None
    text = output
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return text, json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return text, None


def _looks_like_portfolio(name: str, text: str, structured: Any) -> bool:
    if "portfolio" in name:
        return True
    lower = text[:800].lower()
    if any(h in lower for h in _PORTFOLIO_HINTS):
        return True
    if isinstance(structured, dict):
        keys = {str(k).lower() for k in structured}
        return bool(
            keys
            & {
                "total_balance_usd",
                "available_balance_usd",
                "total_balance_value",
                "formatted_output",
            }
        )
    return False


def _digest_portfolio(text: str, structured: Any, *, max_chars: int) -> str:
    lines_out: list[str] = ["[digest] portfolio"]

    # Prefer structured API dicts when present.
    if isinstance(structured, dict):
        for key in (
            "total_balance_usd",
            "available_balance_usd",
            "total_balance_value",
        ):
            if key in structured and structured[key] is not None:
                lines_out.append(f"{key}: {structured[key]}")
        for key, label in (
            ("positions", "positions"),
            ("open_orders", "open_orders"),
            ("lp_positions", "lp_positions"),
            ("perp_positions", "perp_positions"),
        ):
            if key in structured:
                lines_out.append(_summarize_list_field(label, structured[key]))
        # Nested formatted_output from an unwrapped API payload.
        nested = structured.get("formatted_output")
        if isinstance(nested, str) and nested.strip():
            text = nested

    summary_lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip()
        and not set(ln.strip()) <= {"=", "-", "─", "━"}
        and (
            re.match(r"(?i)^total balance value:", ln.strip())
            or re.match(r"(?i)^active (?:perpetual |lp )?positions?:", ln.strip())
            or re.match(r"(?i)^active orders:", ln.strip())
            or re.match(r"(?i)^total\s", ln.strip())
        )
    ]
    if summary_lines:
        lines_out.append("summary:")
        lines_out.extend(f"  {s}" for s in summary_lines)

    holdings = _parse_portfolio_rows(text)
    valued = [h for h in holdings if (h.get("value_usd") or 0) > 0]
    dust = len(holdings) - len(valued)
    valued.sort(key=lambda h: h.get("value_usd") or 0, reverse=True)
    top_n = 12
    if valued:
        lines_out.append(
            f"top holdings by usd ({min(top_n, len(valued))} of {len(valued)}"
            f" valued; {dust} zero/unpriced omitted):"
        )
        for h in valued[:top_n]:
            avail = h.get("available")
            avail_bit = f" avail={avail}" if avail not in (None, h.get("total")) else ""
            usd = h.get("value_usd_raw") or f"{h['value_usd']:.2f}"
            lines_out.append(
                f"  {h['token']} | {h['connector']} | "
                f"amount={h.get('total')}{avail_bit} | usd={usd}"
            )
    elif holdings:
        lines_out.append(f"holdings: {len(holdings)} rows, none with value_usd > 0")

    if len(lines_out) == 1:
        return ""
    return "\n".join(lines_out)


def _parse_portfolio_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if "|" not in line:
            continue
        if re.search(r"(?i)\btoken\b.*\bconnector\b", line):
            continue
        if set(line.replace("|", "").replace(" ", "")) <= {"-", "─", "="}:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        token, connector = parts[0], parts[1]
        if not token or token.lower() in {"token", "n/a"}:
            continue
        total = parts[2] if len(parts) > 2 else None
        available = parts[3] if len(parts) > 3 else None
        value_raw = parts[4] if len(parts) > 4 else None
        # Some tables omit available and put value in column 3.
        if len(parts) == 4 and value_raw is None:
            value_raw = parts[3]
            available = None
        value_usd = _parse_number(value_raw)
        rows.append(
            {
                "token": token,
                "connector": connector,
                "total": total,
                "available": available,
                "value_usd": value_usd if value_usd is not None else 0.0,
                "value_usd_raw": (value_raw or "").strip() or None,
            }
        )
    return rows


def _digest_structured(text: str, structured: Any, *, max_chars: int) -> str:
    if isinstance(structured, dict):
        lines = ["[digest] json object"]
        scalars: list[str] = []
        nested: list[str] = []
        for key, value in structured.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                rendered = value if not isinstance(value, str) else value[:200]
                scalars.append(f"  {key}: {rendered}")
            elif isinstance(value, list):
                nested.append(f"  {_summarize_list_field(str(key), value)}")
            elif isinstance(value, dict):
                nested.append(f"  {key}: object with {len(value)} keys")
            else:
                nested.append(f"  {key}: {type(value).__name__}")
        # Prefer scalars the answer is likely to quote.
        lines.extend(scalars[:40])
        lines.extend(nested[:20])
        return "\n".join(lines)
    if isinstance(structured, list):
        return "[digest] json list\n  " + _summarize_list_field("items", structured)
    return ""


def _digest_pipe_table(text: str, structured: Any, *, max_chars: int) -> str:
    rows = []
    header = None
    for raw in text.splitlines():
        line = raw.strip()
        if "|" not in line:
            continue
        if set(line.replace("|", "").replace(" ", "")) <= {"-", "─", "="}:
            continue
        parts = [p.strip() for p in line.split("|")]
        if header is None and any(re.search(r"[A-Za-z]", p) for p in parts):
            # First letterful row is treated as header if later rows parse as data.
            header = parts
            continue
        if header is None:
            continue
        score = _row_sort_key(parts)
        rows.append((score, parts))

    if not rows:
        return ""

    rows.sort(key=lambda item: item[0], reverse=True)
    keep = rows[:15]
    lines = [
        f"[digest] table ({len(rows)} data rows; showing top {len(keep)} by trailing numeric)",
        "  | ".join(header or []),
    ]
    for _, parts in keep:
        lines.append("  | ".join(parts))
    omitted = len(rows) - len(keep)
    if omitted:
        lines.append(f"  … {omitted} additional rows omitted")
    # Preserve non-table summary lines (totals etc.).
    extras = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and "|" not in ln and _SUMMARY_LINE_RE.search(ln)
    ]
    if extras:
        lines.append("other:")
        lines.extend(f"  {e}" for e in extras[:12])
    return "\n".join(lines)


def _digest_generic_text(text: str, structured: Any, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    kept: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if _SUMMARY_LINE_RE.search(s) or _NUM_RE.search(s):
            kept.append(s)
        if sum(len(x) + 1 for x in kept) >= max_chars:
            break
    if kept:
        body = "\n".join(kept)
        return f"[digest] text highlights\n{body}"
    # Last resort: head + tail so both preamble and trailing totals survive.
    head = max_chars // 2
    tail = max_chars - head - 20
    return f"{text[:head]}\n…\n{text[-tail:]}"


def _summarize_list_field(label: str, value: Any) -> str:
    if not isinstance(value, list):
        return f"{label}: {value!r}"
    if not value:
        return f"{label}: [] (0)"
    sample = value[0]
    if isinstance(sample, dict):
        keys = ", ".join(list(sample.keys())[:6])
        return f"{label}: {len(value)} items (e.g. keys: {keys})"
    return f"{label}: {len(value)} items (e.g. {sample!r})"


def _row_sort_key(parts: list[str]) -> float:
    for cell in reversed(parts):
        num = _parse_number(cell)
        if num is not None:
            return abs(num)
    return 0.0


def _parse_number(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.upper() in {"N/A", "NONE", "-", "—"}:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "")
    # Compact formatter: 1.50K / 2.3M
    mult = 1.0
    if text[-1:] in "KkMmBb" and len(text) > 1:
        suffix = text[-1].upper()
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[suffix]
        text = text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        match = _NUM_RE.search(str(raw))
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None


def _fit(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
