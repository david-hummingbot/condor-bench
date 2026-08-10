"""Read/write bench .env settings for the dashboard Settings page.

Trusted-local only — the dashboard has no auth. Secrets are masked on read;
the process env is updated on save so the running server picks them up without
a restart for most keys.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from config import ROOT

ENV_PATH = ROOT / ".env"

# Keys the Settings UI can edit. Order = display order within each group.
SETTINGS_FIELDS: list[dict[str, Any]] = [
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API key",
        "group": "API keys",
        "secret": True,
        "hint": "Required for the answer-quality judge (and Anthropic models).",
    },
    {
        "key": "OPENROUTER_API_KEY",
        "label": "OpenRouter API key",
        "group": "API keys",
        "secret": True,
        "hint": "For openrouter:… model keys.",
    },
    {
        "key": "OPENAI_API_KEY",
        "label": "OpenAI API key",
        "group": "API keys",
        "secret": True,
        "hint": "For openai:… and some custom OpenAI-compat endpoints.",
    },
    {
        "key": "GROQ_API_KEY",
        "label": "Groq API key",
        "group": "API keys",
        "secret": True,
        "hint": "For groq:… models.",
    },
    {
        "key": "BENCH_BASELINE_MODEL",
        "label": "Baseline model",
        "group": "Benchmark",
        "secret": False,
        "hint": "Latency reference for scoring (e.g. anthropic:claude-sonnet-5).",
    },
    {
        "key": "BENCH_JUDGE_MODEL",
        "label": "Judge model",
        "group": "Benchmark",
        "secret": False,
        "hint": "Anthropic model id for answer quality (e.g. claude-sonnet-5).",
    },
    {
        "key": "CONDOR_PATH",
        "label": "Default Condor path",
        "group": "Benchmark",
        "secret": False,
        "hint": "Fallback checkout when an Environment does not override it. Prefer Environment.condor_path for A/B.",
    },
    {
        "key": "HUMMINGBOT_API_URL",
        "label": "Staging API URL",
        "group": "Staging",
        "secret": False,
        "hint": "Must match the URL MCP actually launches with.",
    },
    {
        "key": "BENCH_EXPECTED_API_URL",
        "label": "Expected API URL",
        "group": "Staging",
        "secret": False,
        "hint": "Fail-closed check; defaults to HUMMINGBOT_API_URL if empty.",
    },
    {
        "key": "HUMMINGBOT_USERNAME",
        "label": "Staging username",
        "group": "Staging",
        "secret": False,
    },
    {
        "key": "HUMMINGBOT_PASSWORD",
        "label": "Staging password",
        "group": "Staging",
        "secret": True,
    },
    {
        "key": "BENCH_SERVER_NAME",
        "label": "Bench server name",
        "group": "Staging",
        "secret": False,
        "hint": "Entry in condor config.yml (--server-name on both MCP servers).",
    },
    {
        "key": "BENCH_CHAT_ID",
        "label": "Bench chat id",
        "group": "Staging",
        "secret": False,
    },
    {
        "key": "BENCH_USER_ID",
        "label": "Bench user id",
        "group": "Staging",
        "secret": False,
    },
    {
        "key": "BENCH_STAGING_ACCOUNT",
        "label": "Staging account",
        "group": "Staging",
        "secret": False,
    },
    {
        "key": "BENCH_ALLOW_MUTATING",
        "label": "Allow mutating cases",
        "group": "Staging",
        "secret": False,
        "choices": ["false", "true"],
        "hint": "Only enable after staging-check passes. Default false.",
    },
    {
        "key": "TELEGRAM_TOKEN",
        "label": "Telegram bot token",
        "group": "Staging",
        "secret": True,
        "hint": "Only needed if a case exercises send_notification.",
    },
]

_KNOWN = {f["key"] for f in SETTINGS_FIELDS}
_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        out[key] = val
    return out


def get_settings() -> dict[str, Any]:
    """Return schema + current values (secrets masked). Prefer process env over file."""
    file_vals = _parse_env_file(ENV_PATH)
    fields = []
    for spec in SETTINGS_FIELDS:
        key = spec["key"]
        raw = os.environ.get(key)
        if raw is None:
            raw = file_vals.get(key, "")
        configured = bool(raw)
        display = _mask(raw) if spec.get("secret") else raw
        fields.append(
            {
                **spec,
                "value": display,
                "configured": configured,
                # Empty string when not set; secrets never return the real value.
                "has_value": configured,
            }
        )
    return {
        "env_path": str(ENV_PATH),
        "fields": fields,
        "note": (
            "Trusted-local only. Saving writes .env and updates the running "
            "process environment. Do not expose this dashboard."
        ),
    }


def update_settings(updates: dict[str, str | None]) -> dict[str, Any]:
    """Apply updates. None / omitted = leave unchanged. Empty string = clear.

    For secrets: if the client sends the masked display value (starts with ••••),
    treat as unchanged.
    """
    file_vals = _parse_env_file(ENV_PATH)
    # Seed from current process so we don't drop keys only set in the shell.
    current = {**file_vals}
    for key in _KNOWN:
        if key in os.environ:
            current[key] = os.environ[key]

    for key, value in updates.items():
        if key not in _KNOWN:
            continue
        if value is None:
            continue
        if isinstance(value, str) and value.startswith("••••"):
            continue  # masked placeholder — leave alone
        current[key] = str(value)

    # Drop empty known keys from the written file (clear).
    to_write = {k: v for k, v in current.items() if v != "" or k not in _KNOWN}

    _write_env_file(ENV_PATH, to_write, preserve_unknown_from=file_vals)

    # Apply to process env for immediate effect.
    for key in _KNOWN:
        val = to_write.get(key, "")
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)

    return get_settings()


def _write_env_file(
    path: Path, values: dict[str, str], *, preserve_unknown_from: dict[str, str]
) -> None:
    """Rewrite .env: keep comments/blank structure lightly; upsert known keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text().splitlines() if path.is_file() else []

    written: set[str] = set()
    out_lines: list[str] = []

    for raw in existing_lines:
        stripped = raw.strip()
        m = _LINE_RE.match(stripped) if stripped and not stripped.startswith("#") else None
        if m and m.group(1) in _KNOWN:
            key = m.group(1)
            if key in values and values[key] != "":
                out_lines.append(f"{key}={_quote(values[key])}")
                written.add(key)
            # else: cleared — skip the line
            continue
        out_lines.append(raw)

    # Append known keys that weren't in the file yet.
    missing = [k for k in _KNOWN if k in values and values[k] != "" and k not in written]
    if missing:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# Updated via dashboard Settings")
        for key in SETTINGS_FIELDS:
            k = key["key"]
            if k in missing:
                out_lines.append(f"{k}={_quote(values[k])}")
                written.add(k)

    # Preserve unknown keys from the previous parse that weren't in the file lines
    # (shouldn't happen often).
    for key, val in preserve_unknown_from.items():
        if key not in _KNOWN and key not in written and val:
            out_lines.append(f"{key}={_quote(val)}")

    from bench.atomic_io import atomic_write_text

    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)


def _quote(value: str) -> str:
    if re.search(r'[\s#"\']', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value
