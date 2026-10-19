"""Run manifest — full identity for every ``summary.json``.

Extends :func:`config.build_run_pin` with the fields that pin *what was
measured and how*: contract version, task-set version, grader version,
inference parameters, environment mode, and experiment type.  Together they
let :mod:`bench.compare` enforce the one-variable-under-test rule and let
:mod:`bench.routing` refuse to mix mock and live frontiers.

**Backwards compatible.**  The new keys are added alongside the existing
``run_type``, ``condor``, ``case_ids``, and ``models`` keys, so old tooling
that reads ``summary.json`` still finds everything it expects.

Usage::

    from bench.manifest import build_manifest

    pin = build_manifest(
        run_type="adhoc",
        case_ids=[c.id for c in cases],
        models=[model],
    )
    # pin is a dict ready for save_run(..., extra_summary=pin)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bench.client import inference_config
from config import (
    CASE_TIMEOUT_BASELINE_MULTIPLE,
    CASE_TIMEOUT_MAX_S,
    CASE_TIMEOUT_MIN_S,
    DATASETS_DIR,
    PASS_THRESHOLD,
    ROOT,
    SCORE_PROFILE,
    SCORE_WEIGHTS,
    build_run_pin,
    env_mode,
    judge_model,
)


# ── Version hashes ─────────────────────────────────────────────────────────────
def _sha256_file(path: Path) -> str | None:
    """Hex digest of a file, or None when the file does not exist."""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_dict(d: dict[str, Any]) -> str:
    """Hex digest of a JSON-serialisable dict (sorted keys for stability)."""
    blob = json.dumps(d, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def contract_version() -> str | None:
    """Hash of the tool surface snapshot, or None when absent.

    The contract version changes when condor's visible tool set changes —
    schema, arguments, or available tools.  Two runs with different contract
    versions measured a different condor.
    """
    return _sha256_file(DATASETS_DIR / "tool_surface.json")


def task_set_version(case_ids: list[str] | None = None) -> str:
    """Hash of the selected case ids.

    Phase 4 will add per-task ``version`` fields; until then the case list
    itself is the best we have.
    """
    ids = sorted(case_ids or [])
    return hashlib.sha256(json.dumps(ids).encode()).hexdigest()


def grader_version() -> str:
    """Hash of everything that determines how a run is scored.

    Weights, thresholds, judge model, and the score profile together form the
    grader identity.  Two runs with different grader versions are not
    meaningfully comparable even if both use the same model.
    """
    grader = {
        "score_weights": SCORE_WEIGHTS,
        "pass_threshold": PASS_THRESHOLD,
        "judge_model": judge_model(),
        "score_profile": SCORE_PROFILE,
    }
    return _sha256_dict(grader)


def uv_lock_hash() -> str | None:
    """Hash of uv.lock, or None when absent."""
    return _sha256_file(ROOT / "uv.lock")


# ── Experiment type ────────────────────────────────────────────────────────────
VALID_EXPERIMENT_TYPES = ("A", "B", "adhoc")


def experiment_type() -> str:
    """``A``, ``B``, or ``adhoc`` from ``BENCH_EXPERIMENT_TYPE``."""
    raw = (os.environ.get("BENCH_EXPERIMENT_TYPE") or "adhoc").strip()
    if raw not in VALID_EXPERIMENT_TYPES:
        return "adhoc"
    return raw


# ── Runtime settings snapshot ──────────────────────────────────────────────────
def _runtime_settings() -> dict[str, Any]:
    """Capture the runtime knobs that affect reproducibility."""
    return {
        "timeout_baseline_multiple": CASE_TIMEOUT_BASELINE_MULTIPLE,
        "timeout_min_s": CASE_TIMEOUT_MIN_S,
        "timeout_max_s": CASE_TIMEOUT_MAX_S,
        "concurrency": int(os.environ.get("BENCH_CONCURRENCY") or 1),
        "cache": os.environ.get("BENCH_CACHE", "off"),
    }


def build_manifest(
    *,
    run_type: str = "adhoc",
    suite_id: str | None = None,
    environment_id: str | None = None,
    run_group_id: str | None = None,
    case_ids: list[str] | None = None,
    models: list[str] | None = None,
    include_in_matrix: bool = False,
    shared_loaded: bool = False,
    experiment_type_override: str | None = None,
    trials: int = 1,
) -> dict[str, object]:
    """Full run identity, extending :func:`config.build_run_pin`.

    Returns a dict suitable for ``save_run(..., extra_summary=manifest)``.
    All existing ``build_run_pin`` keys are preserved at the top level for
    backwards compatibility — the new keys sit alongside them.
    """
    pin = build_run_pin(
        run_type=run_type,
        suite_id=suite_id,
        environment_id=environment_id,
        run_group_id=run_group_id,
        case_ids=case_ids,
        models=models,
        include_in_matrix=include_in_matrix,
        shared_loaded=shared_loaded,
    )

    inference = inference_config()
    exp_t = experiment_type_override or experiment_type()
    if exp_t not in VALID_EXPERIMENT_TYPES:
        exp_t = "adhoc"

    pin["manifest"] = {
        "contract_version": contract_version(),
        "task_set_version": task_set_version(case_ids),
        "grader_version": grader_version(),
        "inference_config": inference.as_dict(),
        "env_mode": env_mode(),
        "experiment_type": exp_t,
        "uv_lock_hash": uv_lock_hash(),
        "trials": int(trials),
        "runtime": _runtime_settings(),
    }

    return pin
