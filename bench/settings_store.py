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
        "hint": "Required for Anthropic models, and for the judge when transport is API.",
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
        "key": "BENCH_JUDGE_BACKEND",
        "label": "Judge transport",
        "group": "Benchmark",
        "secret": False,
        "default": "api",
        "choices": [
            {"value": "api", "label": "Anthropic API"},
            {"value": "acp", "label": "Claude Code (ACP)"},
        ],
        "hint": (
            "How the answer-quality judge is called. ACP uses the local Claude Code "
            "login and does not need ANTHROPIC_API_KEY."
        ),
    },
    {
        "key": "BENCH_JUDGE_MODEL",
        "label": "Judge model",
        "group": "Benchmark",
        "secret": False,
        "hint": (
            "API: Anthropic model id (e.g. claude-sonnet-5). "
            "ACP: Claude Code alias (default, sonnet, opus[1m]) or leave as-is for the CLI default."
        ),
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
        "hint": "Hummingbot API base URL. Saving auto-registers it in Condor as bench_staging.",
    },
    {
        "key": "BENCH_EXPECTED_API_URL",
        "label": "Expected API URL",
        "group": "Staging",
        "secret": False,
        "hint": "Fail-closed check; auto-filled from Staging API URL when empty.",
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
        "key": "TELEGRAM_TOKEN",
        "label": "Telegram bot token",
        "group": "Staging",
        "secret": True,
        "hint": "Only needed if a case exercises send_notification.",
    },
    {
        "key": "BENCH_CHAT_ID",
        "label": "Telegram chat id",
        "group": "Staging",
        "secret": False,
        "hint": (
            "Where send_notification delivers. Leave empty to keep the synthetic "
            "default (999001), which Telegram rejects with 'chat not found' — a real "
            "id makes notification cases scorable, and means a run messages you."
        ),
    },
    {
        "key": "BENCH_USER_ID",
        "label": "Telegram user id",
        "group": "Staging",
        "secret": False,
        "hint": (
            "Who bench acts as. Auto-filled from the chat id on save, which is what "
            "you want for a DM — Telegram keys a private chat by its user, and Condor "
            "waves through delegate when the two match. Set them apart only for a "
            "group chat you are genuinely a member of."
        ),
    },
]

# Keys owned by bench (or retired) — cleared from .env on save so old values cannot
# stick. Server and user stay fixed constants; staging account was removed in favor of
# "whatever accounts the configured Hummingbot API has".
#
# BENCH_CHAT_ID is deliberately *not* here any more. It was a constant because nothing
# needed to change it, but the constant is 999001 — a synthetic id, not a Telegram chat —
# so `send_notification` could only ever answer "Bad Request: chat not found", and four
# cases could not earn live_validity for a reason no amount of bot configuration fixed.
# It is now an ordinary editable setting; `config.staging_config` already read the env
# override, so nothing downstream changes.
# BENCH_USER_ID left this list for the same reason BENCH_CHAT_ID did. It was
# "owned by bench" at the synthetic 999001, and the moment BENCH_CHAT_ID became a
# real Telegram id the two no longer matched — so Condor's delegate guard
# (condor/web/routes/agents.py) stopped short-circuiting on "this is the user's
# own DM", tried to verify that user 999001 belongs to a real chat, and answered
# `403 chat_id is not a chat you belong to` on every delegate call. Worse, a save
# used to *delete* an operator's correct override and put the 403 back.
_INTERNAL_STAGING_KEYS = (
    "BENCH_SERVER_NAME",
    "BENCH_STAGING_ACCOUNT",
)
_KNOWN = {f["key"] for f in SETTINGS_FIELDS} | set(_INTERNAL_STAGING_KEYS)
_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class SettingsError(ValueError):
    """A rejected settings value, surfaced to the operator rather than raised at run time."""

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
        if not raw and spec.get("default"):
            raw = str(spec["default"])
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
    from config import BENCH_CHAT_ID, BENCH_SERVER_NAME, BENCH_USER_ID

    return {
        "env_path": str(ENV_PATH),
        "fields": fields,
        "bench_identity": {
            "server_name": BENCH_SERVER_NAME,
            "chat_id": BENCH_CHAT_ID,
            "user_id": BENCH_USER_ID,
        },
        "note": (
            "Trusted-local only. Saving writes .env, updates the running process "
            "environment, and auto-registers the staging Hummingbot API in Condor's "
            "config.yml. Do not expose this dashboard."
        ),
    }


def update_settings(updates: dict[str, str | None]) -> dict[str, Any]:
    """Apply updates. None / omitted = leave unchanged. Empty string = clear.

    For secrets: if the client sends the masked display value (starts with ••••),
    treat as unchanged.

    When staging URL/credentials are present after the save, upserts the fixed
    ``bench_staging`` server entry + ACL in Condor's config.yml.
    """
    file_vals = _parse_env_file(ENV_PATH)
    # Seed from current process so we don't drop keys only set in the shell.
    current = {**file_vals}
    for key in _KNOWN:
        if key in os.environ:
            current[key] = os.environ[key]

    for key, value in updates.items():
        if key not in _KNOWN or key in _INTERNAL_STAGING_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, str) and value.startswith("••••"):
            continue  # masked placeholder — leave alone
        text = str(value).strip()
        # `staging_config` does `int(...)` on this, and it is read by every run and by
        # the pre-flight — so a typo here would surface as a ValueError traceback from
        # somewhere unrelated. Reject it while the operator is still looking at the form.
        if key in ("BENCH_CHAT_ID", "BENCH_USER_ID") and text:
            candidate = text[1:] if text.startswith("-") else text
            if not candidate.isdigit():
                label = "chat id" if key == "BENCH_CHAT_ID" else "user id"
                raise SettingsError(
                    f"Telegram {label} must be a number (got {text!r}). Group chats are "
                    "negative; leave it empty to keep the synthetic default."
                )
        if key == "BENCH_JUDGE_BACKEND" and text:
            normalized = text.strip().lower()
            if normalized in ("claude-code", "claude-acp"):
                normalized = "acp"
            if normalized not in ("api", "acp"):
                raise SettingsError(
                    f"Judge transport must be 'api' or 'acp' (got {text!r})."
                )
            current[key] = normalized
            continue
        current[key] = text

    # Drop operator-facing identity overrides — bench owns these.
    for key in _INTERNAL_STAGING_KEYS:
        current.pop(key, None)

    # Keep the fail-closed expected URL aligned unless the operator set it apart.
    api_url = (current.get("HUMMINGBOT_API_URL") or "").rstrip("/")
    expected = (current.get("BENCH_EXPECTED_API_URL") or "").rstrip("/")
    if api_url and not expected:
        current["BENCH_EXPECTED_API_URL"] = api_url

    # A real chat id with no user id is the 403 configuration. Mirroring is right
    # for a DM (Telegram keys it by its user) and is what an operator who filled in
    # one field meant; a group chat needs both set, which this does not override.
    if current.get("BENCH_CHAT_ID") and not current.get("BENCH_USER_ID"):
        current["BENCH_USER_ID"] = current["BENCH_CHAT_ID"]

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

    staging_sync = None
    if to_write.get("HUMMINGBOT_API_URL") and to_write.get("HUMMINGBOT_USERNAME") and to_write.get(
        "HUMMINGBOT_PASSWORD"
    ):
        from bench.staging_setup import ensure_bench_server

        staging_sync = ensure_bench_server().as_dict()

    result = get_settings()
    if staging_sync is not None:
        result["staging_sync"] = staging_sync
    return result


def _write_env_file(
    path: Path, values: dict[str, str], *, preserve_unknown_from: dict[str, str]
) -> None:
    """Rewrite .env: keep comments/blank structure lightly; upsert known keys.

    One key, one line. Every save used to *append* every unknown key it had
    parsed — on the theory that they "weren't in the file lines" — but the
    pass-through below has always emitted them already, so each save added a
    second copy of BENCH_MODE, a third, a fourth. A real .env reached four copies
    of two keys before anyone noticed, and nothing broke loudly because dotenv
    takes the last occurrence: the file just grew, and the value you saw in an
    editor was not necessarily the one in force.

    So duplicates are now collapsed on write, whoever created them, and the
    surviving line keeps the value that was *effective* before the save — the
    last occurrence, matching dotenv — not the first one seen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text().splitlines() if path.is_file() else []

    def key_of(raw: str) -> str | None:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            return None
        m = _LINE_RE.match(stripped)
        return m.group(1) if m else None

    # How many times each key appears, so a key that appears once keeps its line
    # verbatim (comments, spacing and quoting untouched) and only a duplicated one
    # gets normalised.
    counts: dict[str, int] = {}
    for raw in existing_lines:
        key = key_of(raw)
        if key:
            counts[key] = counts.get(key, 0) + 1

    written: set[str] = set()   # known keys given their new value
    seen: set[str] = set()      # every key already emitted or deliberately dropped
    out_lines: list[str] = []

    for raw in existing_lines:
        key = key_of(raw)
        if key is None:
            out_lines.append(raw)
            continue
        if key in seen:
            continue  # a duplicate of a key already emitted — drop it
        if key in _KNOWN:
            seen.add(key)
            if key in values and values[key] != "":
                out_lines.append(f"{key}={_quote(values[key])}")
                written.add(key)
            # else: cleared — the line is dropped, and `seen` stops a later
            # duplicate from resurrecting the old value.
            continue
        seen.add(key)
        if counts.get(key, 1) > 1:
            # Collapse to the effective (last) value rather than this first line.
            out_lines.append(f"{key}={_quote(preserve_unknown_from.get(key, ''))}")
        else:
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
                seen.add(k)

    # Unknown keys that really were absent from the file lines — a value that came
    # from the process env, not from disk. `seen` is what makes this an append of
    # last resort instead of a duplicate of every line above.
    for key, val in preserve_unknown_from.items():
        if key not in _KNOWN and key not in seen and val:
            out_lines.append(f"{key}={_quote(val)}")
            seen.add(key)

    from bench.atomic_io import atomic_write_text

    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)


def _quote(value: str) -> str:
    if re.search(r'[\s#"\']', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value
