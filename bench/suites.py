"""File-backed Environments, Suites, and suite-owned cases.

Dashboard and (later) MCP both call these helpers — no UI-only write path.
Optimistic concurrency via ``version``; atomic writes via :mod:`bench.atomic_io`.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench.atomic_io import atomic_write_json, atomic_write_jsonl
from bench.dataset import Case, _normalize_risk, load_all_cases
from config import ENVIRONMENTS_DIR, SUITES_DIR, staging_config

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SuiteStoreError(ValueError):
    """User-facing validation / concurrency error."""


class VersionConflict(SuiteStoreError):
    """PATCH version does not match the on-disk version."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "item"


def _validate_id(value: str, *, kind: str) -> str:
    if not _ID_RE.match(value):
        raise SuiteStoreError(
            f"Invalid {kind} id '{value}' — use lowercase letters, digits, "
            "_ or -, max 64 chars."
        )
    return value


def ensure_store_dirs() -> None:
    ENVIRONMENTS_DIR.mkdir(parents=True, exist_ok=True)
    SUITES_DIR.mkdir(parents=True, exist_ok=True)


# ── Environments ───────────────────────────────────────────────────────────────
def _env_path(env_id: str) -> Path:
    return ENVIRONMENTS_DIR / f"{env_id}.json"


def list_environments() -> list[dict[str, Any]]:
    ensure_store_dirs()
    envs = []
    for path in sorted(ENVIRONMENTS_DIR.glob("*.json")):
        try:
            envs.append(json.loads(path.read_text()))
        except Exception:
            continue
    return envs


def get_environment(env_id: str) -> dict[str, Any]:
    path = _env_path(env_id)
    if not path.is_file():
        raise SuiteStoreError(f"Environment '{env_id}' not found")
    return json.loads(path.read_text())


def create_environment(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_store_dirs()
    env_id = _validate_id(str(payload.get("id") or _slugify(str(payload.get("name", "")))), kind="environment")
    if _env_path(env_id).exists():
        raise SuiteStoreError(f"Environment '{env_id}' already exists")
    condor_path = str(payload.get("condor_path") or "").strip()
    if not condor_path:
        raise SuiteStoreError("condor_path is required")
    record = {
        "id": env_id,
        "name": str(payload.get("name") or env_id),
        "version": 1,
        "condor_path": condor_path,
        "expected_branch": payload.get("expected_branch"),
        "require_clean": bool(payload.get("require_clean", True)),
        "server_name": payload.get("server_name") or staging_config()["server_name"],
        "staging_overrides": payload.get("staging_overrides") or {},
        "updated_at": _utcnow(),
    }
    atomic_write_json(_env_path(env_id), record)
    return record


def update_environment(
    env_id: str, patch: dict[str, Any], *, expected_version: int | None
) -> dict[str, Any]:
    current = get_environment(env_id)
    if expected_version is not None and int(current.get("version", 0)) != int(expected_version):
        raise VersionConflict(
            f"Environment '{env_id}' version conflict: "
            f"disk={current.get('version')} request={expected_version}"
        )
    for key in (
        "name",
        "condor_path",
        "expected_branch",
        "require_clean",
        "server_name",
        "staging_overrides",
    ):
        if key in patch:
            current[key] = patch[key]
    if not str(current.get("condor_path") or "").strip():
        raise SuiteStoreError("condor_path is required")
    current["version"] = int(current.get("version", 0)) + 1
    current["updated_at"] = _utcnow()
    atomic_write_json(_env_path(env_id), current)
    return current


def delete_environment(env_id: str) -> None:
    path = _env_path(env_id)
    if not path.is_file():
        raise SuiteStoreError(f"Environment '{env_id}' not found")
    # Refuse delete when a suite still references it.
    for suite in list_suites():
        if env_id in (suite.get("environment_ids") or []):
            raise SuiteStoreError(
                f"Environment '{env_id}' is attached to suite '{suite['id']}'"
            )
    path.unlink()


def validate_environment(env_id: str) -> dict[str, Any]:
    """Checkout probe in an isolated subprocess (no API-process module cache)."""
    env = get_environment(env_id)
    script = Path(__file__).resolve().parent.parent / "scripts" / "validate_environment.py"
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(script),
            "--condor-path",
            str(env["condor_path"]),
            "--server-name",
            str(env.get("server_name") or ""),
            "--expected-branch",
            str(env.get("expected_branch") or ""),
            "--require-clean",
            "1" if env.get("require_clean") else "0",
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=60,
        env={**dict(**__import__("os").environ), "CONDOR_PATH": str(env["condor_path"])},
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {
            "ok": False,
            "environment_id": env_id,
            "error": proc.stderr.strip() or f"validate exited {proc.returncode}",
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "environment_id": env_id,
            "error": proc.stderr.strip() or proc.stdout.strip() or "invalid validate output",
        }
    payload["environment_id"] = env_id
    return payload


# ── Suites ─────────────────────────────────────────────────────────────────────
def _suite_dir(suite_id: str) -> Path:
    return SUITES_DIR / suite_id


def _suite_meta_path(suite_id: str) -> Path:
    return _suite_dir(suite_id) / "suite.json"


def _suite_cases_path(suite_id: str) -> Path:
    return _suite_dir(suite_id) / "cases.jsonl"


def list_suites() -> list[dict[str, Any]]:
    ensure_store_dirs()
    suites = []
    for path in sorted(SUITES_DIR.glob("*/suite.json")):
        try:
            suites.append(json.loads(path.read_text()))
        except Exception:
            continue
    return suites


def get_suite(suite_id: str) -> dict[str, Any]:
    path = _suite_meta_path(suite_id)
    if not path.is_file():
        raise SuiteStoreError(f"Suite '{suite_id}' not found")
    return json.loads(path.read_text())


def create_suite(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_store_dirs()
    suite_id = _validate_id(
        str(payload.get("id") or _slugify(str(payload.get("name", "")))),
        kind="suite",
    )
    if _suite_meta_path(suite_id).exists():
        raise SuiteStoreError(f"Suite '{suite_id}' already exists")
    env_ids = list(payload.get("environment_ids") or [])
    for eid in env_ids:
        get_environment(eid)  # raises if missing
    models = payload.get("models") or []
    record = {
        "id": suite_id,
        "name": str(payload.get("name") or suite_id),
        "version": 1,
        "environment_ids": env_ids,
        "models": models,
        "include_in_matrix": bool(payload.get("include_in_matrix", False)),
        "notes": str(payload.get("notes") or ""),
        "updated_at": _utcnow(),
    }
    _suite_dir(suite_id).mkdir(parents=True, exist_ok=True)
    atomic_write_json(_suite_meta_path(suite_id), record)
    if not _suite_cases_path(suite_id).exists():
        atomic_write_jsonl(_suite_cases_path(suite_id), [])
    return record


def update_suite(
    suite_id: str, patch: dict[str, Any], *, expected_version: int | None
) -> dict[str, Any]:
    current = get_suite(suite_id)
    if expected_version is not None and int(current.get("version", 0)) != int(expected_version):
        raise VersionConflict(
            f"Suite '{suite_id}' version conflict: "
            f"disk={current.get('version')} request={expected_version}"
        )
    if "environment_ids" in patch:
        for eid in patch["environment_ids"] or []:
            get_environment(eid)
        current["environment_ids"] = list(patch["environment_ids"] or [])
    for key in ("name", "models", "include_in_matrix", "notes"):
        if key in patch:
            current[key] = patch[key]
    current["version"] = int(current.get("version", 0)) + 1
    current["updated_at"] = _utcnow()
    atomic_write_json(_suite_meta_path(suite_id), current)
    return current


def delete_suite(suite_id: str) -> None:
    meta = _suite_meta_path(suite_id)
    if not meta.is_file():
        raise SuiteStoreError(f"Suite '{suite_id}' not found")
    import shutil

    shutil.rmtree(_suite_dir(suite_id))


# ── Suite cases ────────────────────────────────────────────────────────────────
def _load_case_rows(suite_id: str) -> list[dict[str, Any]]:
    path = _suite_cases_path(suite_id)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _save_case_rows(suite_id: str, rows: list[dict[str, Any]], *, version: int) -> None:
    # Version lives on suite.json; bump it whenever cases change.
    suite = get_suite(suite_id)
    if int(suite.get("version", 0)) != int(version):
        raise VersionConflict(
            f"Suite '{suite_id}' version conflict while saving cases: "
            f"disk={suite.get('version')} request={version}"
        )
    atomic_write_jsonl(_suite_cases_path(suite_id), rows)
    suite["version"] = int(version) + 1
    suite["updated_at"] = _utcnow()
    suite["cases_version"] = suite["version"]
    atomic_write_json(_suite_meta_path(suite_id), suite)


def list_suite_cases(suite_id: str) -> list[dict[str, Any]]:
    get_suite(suite_id)
    return _load_case_rows(suite_id)


def suite_prompt_map(suite_id: str) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for row in _load_case_rows(suite_id):
        cid = row.get("id")
        if not cid:
            continue
        if row.get("type") == "tick":
            prompts[cid] = str(row.get("scenario_name") or row.get("question") or "")
        else:
            prompts[cid] = str(row.get("question") or "")
    return prompts


def mint_case_id(suite_id: str, source_or_slug: str) -> str:
    base = f"{suite_id}__{_slugify(source_or_slug)}"
    existing = {r["id"] for r in _load_case_rows(suite_id)}
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def create_suite_case(
    suite_id: str, payload: dict[str, Any], *, expected_version: int
) -> dict[str, Any]:
    get_suite(suite_id)
    rows = _load_case_rows(suite_id)
    case_type = str(payload.get("type") or "consult")
    raw_id = payload.get("id")
    if raw_id:
        case_id = str(raw_id)
        if not case_id.startswith(f"{suite_id}__"):
            case_id = mint_case_id(suite_id, case_id)
    else:
        case_id = mint_case_id(
            suite_id, str(payload.get("question") or payload.get("tool") or "case")[:40]
        )
    if any(r.get("id") == case_id for r in rows):
        raise SuiteStoreError(f"Case '{case_id}' already exists in suite '{suite_id}'")
    row = dict(payload)
    row["id"] = case_id
    row["type"] = case_type
    row["risk_level"] = _normalize_risk(row.get("risk_level"))
    row.setdefault("source_case_id", None)
    row.setdefault("source_dataset", None)
    row.setdefault("imported_at", None)
    rows.append(row)
    _save_case_rows(suite_id, rows, version=expected_version)
    return row


def update_suite_case(
    suite_id: str,
    case_id: str,
    patch: dict[str, Any],
    *,
    expected_version: int,
) -> dict[str, Any]:
    rows = _load_case_rows(suite_id)
    idx = next((i for i, r in enumerate(rows) if r.get("id") == case_id), None)
    if idx is None:
        raise SuiteStoreError(f"Case '{case_id}' not found in suite '{suite_id}'")
    current = dict(rows[idx])
    for key, value in patch.items():
        if key == "id":
            continue
        current[key] = value
    if "risk_level" in current:
        current["risk_level"] = _normalize_risk(current.get("risk_level"))
    rows[idx] = current
    _save_case_rows(suite_id, rows, version=expected_version)
    return current


def delete_suite_case(
    suite_id: str, case_id: str, *, expected_version: int
) -> None:
    rows = _load_case_rows(suite_id)
    new_rows = [r for r in rows if r.get("id") != case_id]
    if len(new_rows) == len(rows):
        raise SuiteStoreError(f"Case '{case_id}' not found in suite '{suite_id}'")
    _save_case_rows(suite_id, new_rows, version=expected_version)


def import_library_cases(
    suite_id: str,
    *,
    case_ids: list[str] | None = None,
    layers: list[str] | None = None,
    expected_version: int,
) -> list[dict[str, Any]]:
    """Copy-on-import from shared datasets/ into the suite's cases.jsonl."""
    library = load_all_cases(layers=layers)
    wanted = set(case_ids) if case_ids else None
    imported: list[dict[str, Any]] = []
    rows = _load_case_rows(suite_id)
    existing = {r["id"] for r in rows}

    dataset_for = {
        "consult": "consult.jsonl",
        "tick": "tick.jsonl",
        "tool": "tools.jsonl",
        "agent": "agents.jsonl",
    }

    for case in library:
        if wanted is not None and case.id not in wanted:
            continue
        new_id = mint_case_id(suite_id, case.id)
        while new_id in existing:
            new_id = mint_case_id(suite_id, f"{case.id}_x")
        row = _case_to_row(case)
        row["id"] = new_id
        row["source_case_id"] = case.id
        row["source_dataset"] = dataset_for.get(case.type, "unknown")
        row["imported_at"] = _utcnow()
        rows.append(row)
        existing.add(new_id)
        imported.append(row)

    if not imported and wanted:
        raise SuiteStoreError("No matching library cases to import")
    _save_case_rows(suite_id, rows, version=expected_version)
    return imported


def _case_to_row(case: Case) -> dict[str, Any]:
    """Serialize a library Case dataclass into a suite case row."""
    from dataclasses import asdict, is_dataclass

    if is_dataclass(case):
        row = asdict(case)
    else:
        row = dict(case)  # type: ignore[arg-type]
    # domain is a property on some types — keep type/risk explicit
    row["type"] = case.type
    row["risk_level"] = _normalize_risk(getattr(case, "risk_level", None))
    if case.type == "tool" and "domain_name" not in row and hasattr(case, "domain_name"):
        row["domain"] = case.domain_name
    return row


def load_suite_cases_as_objects(
    suite_id: str,
    *,
    case_ids: list[str] | None = None,
) -> list[Case]:
    """Parse suite rows into the same Case objects the runner expects."""
    from bench.dataset import AgentCase, ConsultCase, TickCase, ToolCase

    rows = _load_case_rows(suite_id)
    if case_ids is not None:
        wanted = set(case_ids)
        rows = [r for r in rows if r.get("id") in wanted]

    cases: list[Case] = []
    for data in rows:
        ctype = data.get("type", "consult")
        if ctype == "tick":
            cases.append(
                TickCase(
                    id=data["id"],
                    scenario_name=data.get("scenario_name", data.get("question", "")),
                    agent_instructions=data.get("agent_instructions", ""),
                    strategy_instructions=data.get("strategy_instructions", ""),
                    config=data.get("config", {}),
                    risk_state=data.get("risk_state", {}),
                    core_data=data.get("core_data", {}),
                    learnings=data.get("learnings", ""),
                    summary=data.get("summary", ""),
                    recent_decisions=data.get("recent_decisions", ""),
                    tick_number=data.get("tick_number", 1),
                    expected_tool_calls=data.get("expected_tool_calls")
                    or data.get("expected_tools", []),
                    expected_no_calls=data.get("expected_no_calls", []),
                    category=data.get("category", ""),
                    tags=data.get("tags", []),
                    expected_tool_params=data.get("expected_tool_params", {}),
                    live_expected=data.get("live_expected", {}),
                    risk_level=_normalize_risk(data.get("risk_level")),
                    agent_slug=data.get("agent_slug") or f"bench_{data['id']}",
                )
            )
        elif ctype == "tool":
            tool = data.get("tool") or (data.get("expected_tools") or ["unknown"])[0]
            cases.append(
                ToolCase(
                    id=data["id"],
                    tool=tool,
                    question=data.get("question", ""),
                    domain_name=data.get("domain") or data.get("domain_name", ""),
                    expected_tools=data.get("expected_tools") or [tool],
                    expected_tool_params=data.get("expected_tool_params", {}),
                    expected_no_calls=data.get("expected_no_calls", []),
                    live_expected=data.get("live_expected", {}),
                    risk_level=_normalize_risk(data.get("risk_level")),
                    agent_slug=data.get("agent_slug"),
                    tags=data.get("tags", []),
                )
            )
        elif ctype == "agent":
            cases.append(
                AgentCase(
                    id=data["id"],
                    agent_slug=data.get("agent_slug"),
                    question=data.get("question", ""),
                    assistant=data.get("assistant", ""),
                    expected_tools=data.get("expected_tools", []),
                    expected_tool_params=data.get("expected_tool_params", {}),
                    expected_no_calls=data.get("expected_no_calls", []),
                    turns=data.get("turns", []),
                    live_expected=data.get("live_expected", {}),
                    risk_level=_normalize_risk(data.get("risk_level")),
                    tags=data.get("tags", []),
                )
            )
        else:
            cases.append(
                ConsultCase(
                    id=data["id"],
                    question=data.get("question", ""),
                    context=data.get("context", ""),
                    category=data.get("category", ""),
                    expected_tools=data.get("expected_tools", []),
                    turns=data.get("turns", []),
                    tags=data.get("tags", []),
                    type="consult",
                    expected_tool_params=data.get("expected_tool_params", {}),
                    live_expected=data.get("live_expected", {}),
                    expected_no_calls=data.get("expected_no_calls", []),
                    steps=data.get("steps", []),
                    post_conditions=data.get("post_conditions", {}),
                    risk_level=_normalize_risk(data.get("risk_level")),
                    agent_slug=data.get("agent_slug"),
                )
            )

    return cases



