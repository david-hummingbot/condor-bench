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

# How much of a *short* string field to show. Anything longer is treated as the
# payload itself and digested rather than head-truncated — see _digest_structured.
_SCALAR_PREVIEW_CHARS = 200

_NUM_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_SUMMARY_LINE_RE = re.compile(
    r"(?i)\b("
    r"total\b|summary\b|available\b|mid(?:[_\s-]?price)?\b|price\b|"
    r"funding\b|positions?\b|orders?\b|balance\b|value\b|error\b|failed\b"
    r")"
)
_PORTFOLIO_HINTS = ("portfolio overview", "token balances", "total balance value")

# Unix epochs the judge would otherwise have to convert in its head. The bands are
# deliberately narrow — 2001-09-09 to 2096 in seconds, the same window in
# milliseconds — because anything outside them is far more likely to be a quantity
# than a timestamp.
_EPOCH_S_RANGE = (1_000_000_000, 4_000_000_000)
_EPOCH_MS_RANGE = (1_000_000_000_000, 4_000_000_000_000)


def annotate_epochs(args: Any) -> Any:
    """Copy ``args`` with epoch-looking numbers spelled out as UTC timestamps.

    The judge reads tool *arguments* verbatim and cannot do the arithmetic, so it
    guesses — and on c008 it guessed wrong, calling ``start_time=1786406400``
    "likely a future/incorrect epoch" and docking a correct answer from 1.0 to 0.75.
    That value is 2026-08-11T00:00:00Z, a sound reading of "the last 24 hours".

    Rendering it as ``1786406400 (2026-08-11T00:00:00Z)`` removes the guess instead
    of asking the judge to be better at mental arithmetic. Applies to any
    time-windowed case, which is where this failure recurs.
    """
    from datetime import datetime, timezone

    def _annotate(value: Any) -> Any:
        # bool is an int subclass, and True/False are never timestamps.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(value, dict):
                return {k: _annotate(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_annotate(v) for v in value]
            return value
        for low, high, divisor in (
            (*_EPOCH_S_RANGE, 1),
            (*_EPOCH_MS_RANGE, 1000),
        ):
            if low <= value < high:
                try:
                    stamp = datetime.fromtimestamp(value / divisor, timezone.utc)
                except (OverflowError, OSError, ValueError):
                    return value
                return f"{value} ({stamp.strftime('%Y-%m-%dT%H:%M:%SZ')})"
        return value

    return _annotate(args)


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
            return _mark_truncation(digest, len(text), max_chars)

    return _mark_truncation(text, len(text), max_chars)


# Appended to a digest that dropped content, so the judge can tell "the tool did not
# return this" from "this digest did not show it". Kept short because it is paid for
# out of the same budget as the content it describes.
_TRUNCATION_NOTE = (
    "\n[digest truncated: ~{shown} of {total} chars shown — content absent "
    "here was not necessarily absent from the tool result]"
)
# Widest the note can render, used to reserve room inside max_chars.
_NOTE_RESERVE = len(_TRUNCATION_NOTE.format(shown=9_999_999, total=9_999_999))


def _mark_truncation(digest: str, original_chars: int, max_chars: int) -> str:
    """Say so when a digest is lossy. Silence on a complete one is the point.

    `tool_manage_skill_001` scored 0.15 for a verifiably correct answer. It read
    `routine_cookbook/report_builder.md`, a 10,135-character file, and summarised
    RoutineResult accurately — the import path, `table_data`/`table_columns`,
    `chart_image`, and the `{"type": "kpi", …, "trend": "up"|"down"}` section
    shape are all on lines 190-211 of the real file. The judge's share of the tool
    log was ~1,375 characters, about 13% of it, and the RoutineResult section is
    near the end. Seeing none of it, the judge concluded "the detailed field
    descriptions do not appear anywhere in the digest" and called it fabrication.

    The marker has to be conditional to be worth anything. In
    `tool_get_user_context_001` the model claimed two saved memories and
    `manage_memory` had returned `{"index": ""}` in full — 17 characters, nothing
    dropped. That accusation was sound, and a blanket "the log may be incomplete"
    would have excused it. An unmarked digest now means the output really is
    complete, which is what makes the marked case informative.
    """
    body = _fit(digest, max_chars)
    if original_chars <= len(body):
        return body
    # The note is part of the digest's budget, not an overrun of it: callers size the
    # judge transcript from max_chars, and a per-call overspend multiplies across a
    # long tool log.
    body = _fit(digest, max(1, max_chars - _NOTE_RESERVE))
    return body + _TRUNCATION_NOTE.format(shown=len(body), total=original_chars)


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
        # The rendered table, wherever this server put it. hummingbot answers with
        # {"result": "<table>"}, so reading only `formatted_output` left this
        # digester parsing the JSON envelope — it found no holdings and no total,
        # and quietly returned nothing for every live portfolio call.
        for nested_key in ("formatted_output", "result", "output"):
            nested = structured.get(nested_key)
            if isinstance(nested, str) and nested.strip():
                text = nested
                break

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


def _digest_text_payload(text: str, *, max_chars: int) -> str:
    """Summarise a string that *is* the payload, keeping its figures visible."""
    if len(text) <= max_chars:
        return text  # it fits: no summary can beat the real thing
    if "|" in text and text.count("\n") >= 3:
        table = _digest_pipe_table(text, None, max_chars=max_chars)
        if table:
            return _fit(table, max_chars)
    return _fit(_digest_generic_text(text, None, max_chars=max_chars) or text, max_chars)


def _digest_structured(text: str, structured: Any, *, max_chars: int) -> str:
    if isinstance(structured, dict):
        lines = ["[digest] json object"]
        scalars: list[str] = []
        nested: list[str] = []
        # A long string field is not a scalar to preview — it is the whole answer.
        # hummingbot's tools wrap their formatted output in {"result": "<table>"},
        # so taking a 200-char head cut an order book off at its column headers,
        # one line before the first price. The judge was shown a table with no rows
        # and correctly concluded the cited bid/ask "appear fabricated".
        payload_keys = [
            k for k, v in structured.items()
            if isinstance(v, str) and len(v) > _SCALAR_PREVIEW_CHARS
        ]
        per_payload = max(400, (max_chars - 200) // max(1, len(payload_keys)))
        for key, value in structured.items():
            if key in payload_keys:
                body = _digest_text_payload(str(value), max_chars=per_payload)
                indented = "\n".join(f"    {ln}" for ln in body.splitlines())
                scalars.append(f"  {key}:\n{indented}")
            elif isinstance(value, (str, int, float, bool)) or value is None:
                rendered = value if not isinstance(value, str) else value[:_SCALAR_PREVIEW_CHARS]
                scalars.append(f"  {key}: {rendered}")
            elif isinstance(value, list):
                nested.append(f"  {_summarize_list_field(str(key), value)}")
            elif isinstance(value, dict):
                nested.append(f"  {key}:\n{_digest_nested_dict(value, max_chars=per_payload)}")
            else:
                nested.append(f"  {key}: {type(value).__name__}")
        # Prefer scalars the answer is likely to quote.
        lines.extend(scalars[:40])
        lines.extend(nested[:20])
        return "\n".join(lines)
    if isinstance(structured, list):
        return "[digest] json list\n  " + _summarize_list_field("items", structured)
    return ""


def _digest_nested_dict(value: dict, *, max_chars: int) -> str:
    """Render one level inside a nested object instead of counting its keys.

    `manage_routines(action="run")` answers
    ``{"name": …, "status": "completed", "result": {"text": "<the whole report>", …}}``,
    and the payload the answer quotes is `result.text`. The digester handled a long
    string at the *top* level but rendered a nested object as "object with 5 keys", so
    the judge read a grounded summary of a real 96-pair scan — BTC-USDT $7.2B volume,
    APR-USDT +90.3% — and concluded it was fabricated, citing that very placeholder as
    its reason. Scored 0.05 and 0.35 on two runs of the same correct answer.

    One level deep only. Anything further is summarised, because the point is to surface
    the citeable payload, not to pretty-print arbitrary structure.
    """
    lines: list[str] = []
    payload_keys = [
        k for k, v in value.items()
        if isinstance(v, str) and len(v) > _SCALAR_PREVIEW_CHARS
    ]
    per_payload = max(300, (max_chars - 100) // max(1, len(payload_keys)))
    for key, inner in value.items():
        if key in payload_keys:
            body = _digest_text_payload(str(inner), max_chars=per_payload)
            lines.append(f"    {key}:")
            lines.extend(f"      {ln}" for ln in body.splitlines())
        elif isinstance(inner, (str, int, float, bool)) or inner is None:
            rendered = inner if not isinstance(inner, str) else inner[:_SCALAR_PREVIEW_CHARS]
            lines.append(f"    {key}: {rendered}")
        elif isinstance(inner, list):
            lines.append(f"    {_summarize_list_field(str(key), inner)}")
        elif isinstance(inner, dict):
            lines.append(f"    {key}: object with {len(inner)} keys")
        else:
            lines.append(f"    {key}: {type(inner).__name__}")
    return "\n".join(lines[:30])


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


# How many rows of a list-of-records to actually show the judge, and how much of each.
_LIST_ROWS = 15
# Wide enough for slug + name + strategies=[…] before a truncated description.
_LIST_ROW_CHARS = 220
_LIST_NESTED_ITEMS = 6
_LIST_NESTED_CHARS = 100
# Strings longer than this are deferred so short citeables (and nested lists) fit.
_LIST_LONG_SCALAR_CHARS = 48


def _summarize_list_field(label: str, value: Any) -> str:
    """Render a list field as rows, not just a count.

    A count is unusable as evidence. The judge is asked to verify that figures in the
    answer appear in tool output, and every list-returning tool — routines, servers,
    agent definitions, orders, bots — was collapsed to "N items (e.g. keys: …)". So a
    model that correctly named one continuous routine out of 29 was marked down for
    "unverified implementation details": the names it cited had been deleted before the
    judge ever saw them. `agent_condor_routine_003` scored 0.55 that way.

    Nested *list* fields on each row used to be dropped entirely (only scalars were
    kept). ``list_agent_definitions`` returns ``strategies: ["BTC-USDT Adaptive Grid",
    …]`` per agent; c011 and ``tool_manage_trading_agent_001`` quoted those names and
    were scored 0.35 / 0.55 for fabricating a column the digest had erased.

    Rows are capped and the remainder is stated, mirroring the pipe-table digester —
    the point is that *some* rows are citeable, not that all of them fit.
    """
    if not isinstance(value, list):
        return f"{label}: {value!r}"
    if not value:
        return f"{label}: [] (0)"

    shown, omitted = value[:_LIST_ROWS], max(0, len(value) - _LIST_ROWS)
    lines = [f"{label}: {len(value)} items"]
    for item in shown:
        if isinstance(item, dict):
            rendered = _format_record_row(item)
        else:
            rendered = str(item)
        lines.append(f"  - {rendered[:_LIST_ROW_CHARS]}")
    if omitted:
        lines.append(f"  … {omitted} additional row(s) omitted")
    return "\n".join(lines)


def _format_record_row(item: dict) -> str:
    """One list-of-dicts row: short scalars and nested scalar lists before long text.

    ``description`` and similar long strings used to consume the whole row budget, so
    even after nested lists were included they still fell past ``_LIST_ROW_CHARS``.
    Short identifiers and list fields (name, strategies) are the citeable bits.
    """
    short: list[str] = []
    nested_lists: list[str] = []
    long: list[str] = []
    for key, value in item.items():
        if isinstance(value, list):
            if value and not all(
                isinstance(x, (str, int, float, bool)) or x is None for x in value
            ):
                continue
            nested_lists.append(f"{key}={_format_scalar_list(value)}")
        elif isinstance(value, (str, int, float, bool)) or value is None:
            part = f"{key}={_scalar(value)}"
            if isinstance(value, str) and len(value) > _LIST_LONG_SCALAR_CHARS:
                long.append(part)
            else:
                short.append(part)
    body = " ".join(short + nested_lists + long)
    return body or ", ".join(list(item.keys())[:6])


def _format_scalar_list(value: list, *, max_items: int = _LIST_NESTED_ITEMS,
                        max_chars: int = _LIST_NESTED_CHARS) -> str:
    shown = value[:max_items]
    body = ", ".join(_scalar(x) for x in shown)
    omitted = len(value) - len(shown)
    if omitted:
        body = f"{body}, … +{omitted}"
    if len(body) > max_chars:
        body = body[: max_chars - 1] + "…"
    return f"[{body}]"


def _scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    return text[:60]


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
