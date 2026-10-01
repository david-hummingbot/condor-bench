"""Persist Quick Run form fields in ``.env``.

Benchmark and Prompt share one record (``BENCH_FORM_STATE``). The dashboard
writes it when a field changes and reads it back on load, so a key, URL, model,
filter, or prompt does not have to be typed again. Clearing a field saves the
empty value, which is what forgets it.

The key is unknown to the Settings page on purpose: that form would otherwise
show a JSON blob of secrets. A Settings save leaves a single occurrence of an
unknown key verbatim, so the two writers do not fight.
"""

from __future__ import annotations

import json
import re
from typing import Any

FORM_STATE_KEY = "BENCH_FORM_STATE"

_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LAYERS = frozenset({"consult", "tick", "tool", "agent"})
_RISKS = frozenset({"read_only", "mutating", "destructive"})
_MAX_JSON = 100_000


class FormStateError(ValueError):
    """A rejected form-state value, surfaced to the operator."""


def empty_form_state() -> dict[str, Any]:
    return {
        "providers": {},
        "benchmark": {
            "layers": [],
            "domain": "",
            "category": "",
            "riskLevels": [],
            "coreOnly": False,
        },
        "prompt": {
            "question": "",
            "turns": [],
            "expectedTools": "",
            "agentSlug": "",
        },
    }


def get_form_state() -> dict[str, Any]:
    """Return the saved form, or the empty form when nothing is stored.

    A corrupt value is not rewritten here. The warning tells the UI; the next
    successful save replaces it.
    """
    raw = _read_raw()
    state = empty_form_state()
    if not raw.strip():
        return state
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        state["warning"] = f"{FORM_STATE_KEY} in .env is not valid JSON."
        return state
    if not isinstance(parsed, dict):
        state["warning"] = f"{FORM_STATE_KEY} in .env is not a JSON object."
        return state
    try:
        cleaned = _clean_stored(parsed)
    except FormStateError as exc:
        state["warning"] = str(exc)
        return state
    return cleaned


def update_form_state(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial update and write ``.env``.

    ``providers`` replaces the provider map (so a cleared provider is dropped).
    ``benchmark`` and ``prompt`` replace only their own section, so saving one
    page does not wipe the other.
    """
    if not isinstance(patch, dict):
        raise FormStateError("Form state must be a JSON object.")
    unknown = set(patch) - {"providers", "benchmark", "prompt"}
    if unknown:
        raise FormStateError(f"Unknown form state field: {sorted(unknown)[0]}.")

    current = get_form_state()
    current.pop("warning", None)
    if "providers" in patch:
        current["providers"] = _clean_providers(patch["providers"])
    if "benchmark" in patch:
        current["benchmark"] = _clean_benchmark(patch["benchmark"])
    if "prompt" in patch:
        current["prompt"] = _clean_prompt(patch["prompt"])

    _write_state(current)
    return current


def _clean_stored(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys the form knows how to restore."""
    state = empty_form_state()
    if "providers" in data:
        state["providers"] = _clean_providers(data["providers"])
    if "benchmark" in data:
        state["benchmark"] = _clean_benchmark(data["benchmark"])
    if "prompt" in data:
        state["prompt"] = _clean_prompt(data["prompt"])
    return state


def _clean_providers(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise FormStateError("providers must be an object.")
    if len(value) > 20:
        raise FormStateError("Too many providers.")
    out: dict[str, dict[str, Any]] = {}
    for pid, fields in value.items():
        if not isinstance(pid, str) or not _PROVIDER_ID.match(pid):
            raise FormStateError(f"Invalid provider id: {pid!r}.")
        if not isinstance(fields, dict):
            raise FormStateError(f"Provider {pid} must be an object.")
        extra = set(fields) - {"enabled", "apiKey", "baseUrl", "selectedModel"}
        if extra:
            raise FormStateError(f"Unknown provider field: {sorted(extra)[0]}.")
        out[pid] = {
            "enabled": _bool(fields.get("enabled", False), "enabled"),
            "apiKey": _text(fields.get("apiKey", ""), "apiKey", 4096),
            "baseUrl": _text(fields.get("baseUrl", ""), "baseUrl", 2048),
            "selectedModel": _text(fields.get("selectedModel", ""), "selectedModel", 512),
        }
    return out


def _clean_benchmark(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FormStateError("benchmark must be an object.")
    extra = set(value) - {"layers", "domain", "category", "riskLevels", "coreOnly"}
    if extra:
        raise FormStateError(f"Unknown benchmark field: {sorted(extra)[0]}.")
    layers = _choice_list(value.get("layers", []), "layers", _LAYERS)
    risks = _choice_list(value.get("riskLevels", []), "riskLevels", _RISKS)
    return {
        "layers": layers,
        "domain": _text(value.get("domain", ""), "domain", 256),
        "category": _text(value.get("category", ""), "category", 256),
        "riskLevels": risks,
        "coreOnly": _bool(value.get("coreOnly", False), "coreOnly"),
    }


def _clean_prompt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FormStateError("prompt must be an object.")
    extra = set(value) - {"question", "turns", "expectedTools", "agentSlug"}
    if extra:
        raise FormStateError(f"Unknown prompt field: {sorted(extra)[0]}.")
    turns_raw = value.get("turns", [])
    if not isinstance(turns_raw, list):
        raise FormStateError("turns must be a list.")
    if len(turns_raw) > 20:
        raise FormStateError("Too many prompt turns.")
    turns = [_text(t, "turns", 20_000) for t in turns_raw]
    return {
        "question": _text(value.get("question", ""), "question", 20_000),
        "turns": turns,
        "expectedTools": _text(value.get("expectedTools", ""), "expectedTools", 2000),
        "agentSlug": _text(value.get("agentSlug", ""), "agentSlug", 256),
    }


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise FormStateError(f"{name} must be true or false.")
    return value


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise FormStateError(f"{name} must be a string.")
    if len(value) > limit:
        raise FormStateError(f"{name} is too long.")
    return value


def _choice_list(value: Any, name: str, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        raise FormStateError(f"{name} must be a list.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in allowed or item in out:
            raise FormStateError(f"Invalid {name} value: {item!r}.")
        out.append(item)
    return out


def _blank(state: dict[str, Any]) -> bool:
    if state.get("providers"):
        return False
    bench = state["benchmark"]
    if (
        bench["layers"]
        or bench["domain"]
        or bench["category"]
        or bench["riskLevels"]
        or bench["coreOnly"]
    ):
        return False
    prompt = state["prompt"]
    if prompt["question"] or prompt["turns"] or prompt["expectedTools"] or prompt["agentSlug"]:
        return False
    return True


def _env_path():
    from bench.settings_store import ENV_PATH

    return ENV_PATH


def _read_raw() -> str:
    from bench.settings_store import _parse_env_file

    # The file is the source of truth. A Settings save keeps this line verbatim,
    # and reading the process environment instead would resurrect a value the
    # file just dropped.
    return _parse_env_file(_env_path()).get(FORM_STATE_KEY, "")


def _encode(state: dict[str, Any]) -> str:
    """One env token the line parser can strip back to JSON.

    Values are wrapped in single quotes. The settings parser only removes one
    matching pair of quotes and does not unescape, and JSON uses double quotes,
    so a question containing an apostrophe round-trips. ``json.dumps`` keeps the
    line single-line.
    """
    payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    if len(payload) > _MAX_JSON:
        raise FormStateError("Form state is too large.")
    return "'" + payload + "'"


def _write_state(state: dict[str, Any]) -> None:
    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    token = "" if _blank(state) else _encode(state)
    prefix = f"{FORM_STATE_KEY}="
    out: list[str] = []
    replaced = False
    for raw in lines:
        if raw.strip().startswith(prefix):
            if replaced:
                continue
            replaced = True
            if token:
                out.append(prefix + token)
            continue
        out.append(raw)
    if token and not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(prefix + token)

    from bench.atomic_io import atomic_write_text

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)
