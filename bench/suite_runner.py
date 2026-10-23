"""Suite Run-all orchestrator: one parent run_id, sequential subprocess members."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from bench.suites import get_environment, get_suite
from config import ROOT

DEFAULT_TRIALS = 1
#: A worker's stdout line starting with this carries one progress event as JSON.
#: Every other line is the worker's own output; the last of those is its result.
PROGRESS_PREFIX = "@@bench-progress "
#: A progress line carries a scorecard and a response; the 64 KiB default would
#: fail the read on a long one.
_WORKER_LINE_LIMIT = 16 * 1024 * 1024

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
WaitFn = Callable[[], Awaitable[None]]


class FreezeViolationError(ValueError):
    """Raised when an Experiment A or B freeze invariant is violated."""


_INFERENCE_ENV = {
    "temperature": "BENCH_TEMPERATURE",
    "top_p": "BENCH_TOP_P",
    "max_tokens": "BENCH_MAX_TOKENS",
    "reasoning": "BENCH_REASONING",
    "seed": "BENCH_SEED",
}


def _model_key(cfg: dict[str, Any]) -> str | None:
    return cfg.get("model_key") or cfg.get("key")


def effective_inference(model_cfg: dict[str, Any]) -> dict[str, Any]:
    """The inference config a member will run with: env defaults + per-model overrides."""
    from bench.client import inference_config  # noqa: PLC0415 — heavy import

    base = inference_config().as_dict()
    for key, value in (model_cfg.get("inference_config") or {}).items():
        if key in base and value is not None:
            base[key] = value
    return base


def _git(condor_p: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=condor_p, capture_output=True, text=True
    )


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def check_experiment_freeze(
    suite: dict[str, Any],
    env_ids: list[str],
    model_cfgs: list[dict[str, Any]],
    *,
    experiment_type: str | None = None,
    adhoc: bool = False,
) -> str:
    """Validate Experiment A or B freeze invariants before running members.

    Returns the effective experiment type: 'A', 'B', or 'adhoc'. ``suite["freeze"]``
    carries the preset's pins (see ``experiments/*.yaml``): ``model_id`` and
    ``inference_config`` for A, ``condor_commit`` for B.
    """
    if adhoc:
        return "adhoc"

    exp_type = (
        experiment_type
        or suite.get("experiment_type")
        or os.environ.get("BENCH_EXPERIMENT_TYPE")
        or "adhoc"
    ).strip()
    if exp_type not in ("A", "B", "adhoc"):
        raise FreezeViolationError(
            f"Unknown experiment type {exp_type!r} — expected 'A', 'B' or 'adhoc'."
        )
    freeze = suite.get("freeze") or {}
    hint = "Pass --adhoc to run as an ad-hoc experiment."

    if exp_type == "A":
        # Freeze model & inference; vary Condor checkouts via environments.
        models = {_model_key(m) for m in model_cfgs if _model_key(m)}
        if len(models) > 1:
            raise FreezeViolationError(
                f"Experiment A freeze violation: expected a single frozen model, but found {sorted(models)}. "
                f"Experiment A freezes the model and varies the Condor checkout. {hint}"
            )
        pinned_model = freeze.get("model_id")
        if pinned_model and models and models != {pinned_model}:
            raise FreezeViolationError(
                f"Experiment A freeze violation: preset pins model '{pinned_model}', "
                f"suite runs {sorted(models)}. {hint}"
            )
        configs = {_canon(effective_inference(m)) for m in model_cfgs}
        if len(configs) > 1:
            raise FreezeViolationError(
                "Experiment A freeze violation: members would run with different "
                f"inference configs ({sorted(configs)}). Experiment A freezes inference "
                f"parameters along with the model. {hint}"
            )
        pinned_inf = freeze.get("inference_config")
        if pinned_inf is not None:
            for m in model_cfgs:
                actual = effective_inference(m)
                wrong = {
                    k: (v, actual.get(k))
                    for k, v in pinned_inf.items()
                    if k in actual and actual.get(k) != v
                }
                if wrong:
                    raise FreezeViolationError(
                        "Experiment A freeze violation: inference config differs from the "
                        f"preset pin (pinned, actual): {wrong}. {hint}"
                    )
    elif exp_type == "B":
        # Freeze Condor; vary models.
        if len(env_ids) > 1:
            raise FreezeViolationError(
                f"Experiment B freeze violation: expected a single frozen Condor environment, but found {env_ids}. "
                f"Experiment B freezes the Condor environment and varies models. {hint}"
            )
        for env_id in env_ids:
            env = get_environment(env_id)
            condor_p = Path(env["condor_path"])
            # A checkout that cannot be inspected cannot be shown to be frozen.
            if not condor_p.is_dir():
                raise FreezeViolationError(
                    f"Experiment B freeze violation: Condor checkout '{condor_p}' does not exist. {hint}"
                )
            status = _git(condor_p, "status", "--porcelain")
            if status.returncode != 0:
                raise FreezeViolationError(
                    f"Experiment B freeze violation: cannot read git state of '{condor_p}' "
                    f"({status.stderr.strip() or 'not a git checkout'}). {hint}"
                )
            if status.stdout.strip():
                raise FreezeViolationError(
                    f"Experiment B freeze violation: Condor checkout at '{condor_p}' has uncommitted changes. "
                    f"Experiment B freezes Condor and requires a clean checkout. {hint}"
                )
            pinned = freeze.get("condor_commit") or env.get("expected_commit")
            if pinned:
                head = _git(condor_p, "rev-parse", "HEAD").stdout.strip()
                if not head.startswith(str(pinned)):
                    raise FreezeViolationError(
                        f"Experiment B freeze violation: Condor at '{condor_p}' is on {head[:12] or 'unknown'}, "
                        f"but the pin is {pinned}. {hint}"
                    )
    return exp_type


async def run_suite(
    suite_id: str,
    *,
    parent_run_id: str,
    emit: EmitFn,
    case_ids: list[str] | None = None,
    environment_ids: list[str] | None = None,
    models: list[dict[str, Any]] | None = None,
    cancel_event: asyncio.Event | None = None,
    wait_if_paused: WaitFn | None = None,
    experiment_type: str | None = None,
    adhoc: bool = False,
    trials: int | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """Fan out suite × environments × models via subprocess workers.

    Returns ``{run_group_id, members: [...]}``. Emits ``member_*`` at member
    boundaries, and passes on each worker's ``case_started`` / ``case_done`` as it
    reports them, counted across the whole group.
    """
    suite = get_suite(suite_id)
    env_ids = environment_ids or list(suite.get("environment_ids") or [])
    if not env_ids:
        raise ValueError("Suite has no environments attached")

    model_cfgs = models or list(suite.get("models") or [])
    if not model_cfgs:
        raise ValueError("Suite has no models configured")

    # A preset declares the experiment; its freeze block is what the check below
    # enforces. A bad preset or a contradiction with the caller refuses the run.
    preset = preset or suite.get("preset")
    if preset:
        from bench.presets import PresetError, apply_preset

        try:
            suite = apply_preset(suite, preset, experiment_type=experiment_type, adhoc=adhoc)
        except PresetError as exc:
            raise FreezeViolationError(str(exc)) from exc

    # Enforce A/B freeze rules before scheduling members
    effective_exp_type = check_experiment_freeze(
        suite,
        env_ids,
        model_cfgs,
        experiment_type=experiment_type,
        adhoc=adhoc,
    )

    # Every member of a group runs the same trial count — a 1-trial member is not
    # comparable with a 3-trial one.
    trials = max(1, int(trials or suite.get("trials") or DEFAULT_TRIALS))

    run_group_id = uuid.uuid4().hex
    members_plan: list[tuple[dict, dict]] = []
    for env_id in env_ids:
        env = get_environment(env_id)
        for model_cfg in model_cfgs:
            members_plan.append((env, model_cfg))

    await emit(
        {
            "type": "run_started",
            "suite_id": suite_id,
            "run_group_id": run_group_id,
            "total_members": len(members_plan),
            "members": [
                {
                    "environment_id": e["id"],
                    "model": m.get("model_key") or m.get("key"),
                }
                for e, m in members_plan
            ],
        }
    )

    results: list[dict[str, Any]] = []
    current_proc: asyncio.subprocess.Process | None = None

    member_total = 0  # trials per member, from the first worker that reports
    for index, (env, model_cfg) in enumerate(members_plan):
        # Members are subprocesses, so this is the finest boundary the parent can
        # pause on — the current member runs to completion first. Held before the
        # cancel check so a cancel issued while paused still wins.
        if wait_if_paused is not None:
            await wait_if_paused()

        if cancel_event and cancel_event.is_set():
            await emit(
                {
                    "type": "member_failed",
                    "environment_id": env["id"],
                    "model": model_cfg.get("model_key") or model_cfg.get("key"),
                    "error": "cancelled",
                    "status": "skipped",
                }
            )
            results.append(
                {
                    "environment_id": env["id"],
                    "status": "skipped",
                    "error": "cancelled",
                }
            )
            continue

        model_key = model_cfg.get("model_key") or model_cfg.get("key")
        member_run_id = f"{parent_run_id}m{index}"
        await emit(
            {
                "type": "member_started",
                "environment_id": env["id"],
                "model": model_key,
                "member_index": index + 1,
                "total_members": len(members_plan),
                "member_run_id": member_run_id,
            }
        )

        job = {
            "suite_id": suite_id,
            "environment_id": env["id"],
            "run_group_id": run_group_id,
            "parent_run_id": parent_run_id,
            "member_run_id": member_run_id,
            "model": model_key,
            "case_ids": case_ids,
            "include_in_matrix": bool(suite.get("include_in_matrix", False)),
            "expected_branch": env.get("expected_branch"),
            "expected_commit": (
                (suite.get("freeze") or {}).get("condor_commit") or env.get("expected_commit")
            ),
            "require_clean": bool(env.get("require_clean", True)),
            "experiment_type": effective_exp_type,
            "preset": suite.get("preset"),
            "trials": trials,
        }
        job_path = Path(tempfile.mkstemp(prefix="suite_job_", suffix=".json")[1])
        job_path.write_text(json.dumps(job))

        env_vars = os.environ.copy()
        env_vars["CONDOR_PATH"] = str(env["condor_path"])
        if env.get("server_name"):
            env_vars["BENCH_SERVER_NAME"] = str(env["server_name"])
        # Forward optional model credentials from the request config.
        if model_cfg.get("api_key"):
            # Best-effort: set common provider keys based on model prefix.
            key = str(model_key)
            if key.startswith("anthropic:"):
                env_vars["ANTHROPIC_API_KEY"] = str(model_cfg["api_key"])
            elif key.startswith("openrouter:"):
                env_vars["OPENROUTER_API_KEY"] = str(model_cfg["api_key"])
            elif key.startswith("openai:") or key.startswith("custom:"):
                env_vars["OPENAI_API_KEY"] = str(model_cfg["api_key"])
        if model_cfg.get("base_url"):
            env_vars["OPENAI_BASE_URL"] = str(model_cfg["base_url"])
        # Per-model inference overrides reach the worker the same way the run-wide
        # BENCH_* settings do, so the worker's manifest records what it ran with.
        for key, value in (model_cfg.get("inference_config") or {}).items():
            if key in _INFERENCE_ENV and value is not None:
                env_vars[_INFERENCE_ENV[key]] = str(value)

        worker = ROOT / "scripts" / "suite_worker.py"
        try:
            current_proc = await asyncio.create_subprocess_exec(
                "uv",
                "run",
                "python",
                str(worker),
                "--job",
                str(job_path),
                cwd=str(ROOT),
                env=env_vars,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_WORKER_LINE_LIMIT,
            )
            # Read as it runs, not at exit: a member is a whole checkout's worth of
            # cases × trials, and waiting for it left the progress bar still for
            # half an hour. Progress is counted across the group, assuming each
            # member runs the size of the first one that reported.
            stderr_task = asyncio.create_task(current_proc.stderr.read())
            out_lines: list[str] = []
            async for raw in current_proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line.startswith(PROGRESS_PREFIX):
                    out_lines.append(line)
                    continue
                try:
                    event = json.loads(line[len(PROGRESS_PREFIX):])
                except ValueError:
                    continue
                member_total = member_total or int(event.get("total") or 0)
                event.update(
                    environment_id=env["id"],
                    member_index=index + 1,
                    total_members=len(members_plan),
                    total=member_total * len(members_plan),
                )
                if "completed" in event:
                    event["completed"] = member_total * index + int(event["completed"])
                await emit(event)
            await current_proc.wait()
            stdout = "\n".join(out_lines).strip()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            payload: dict[str, Any]
            try:
                # Worker prints a single JSON object on the last non-empty line.
                line = [ln for ln in stdout.splitlines() if ln.strip()][-1]
                payload = json.loads(line)
            except Exception:
                payload = {
                    "ok": False,
                    "error": stderr or stdout or f"worker exit {current_proc.returncode}",
                    "member_run_id": member_run_id,
                }

            if payload.get("ok"):
                await emit(
                    {
                        "type": "member_done",
                        "environment_id": env["id"],
                        "model": model_key,
                        "member_index": index + 1,
                        "total_members": len(members_plan),
                        "member_run_id": member_run_id,
                        "run_dir": payload.get("run_dir"),
                        "cases": payload.get("cases"),
                    }
                )
                results.append({**payload, "environment_id": env["id"], "status": "done"})
            else:
                await emit(
                    {
                        "type": "member_failed",
                        "environment_id": env["id"],
                        "model": model_key,
                        "member_run_id": member_run_id,
                        "error": payload.get("error"),
                        "status": "failed",
                    }
                )
                results.append(
                    {
                        "environment_id": env["id"],
                        "status": "failed",
                        "error": payload.get("error"),
                        "member_run_id": member_run_id,
                    }
                )
        except asyncio.CancelledError:
            if current_proc and current_proc.returncode is None:
                current_proc.kill()
                await current_proc.wait()
            raise
        finally:
            current_proc = None
            try:
                job_path.unlink(missing_ok=True)
            except OSError:
                pass

    await emit(
        {
            "type": "run_done",
            "status": "completed",
            "run_group_id": run_group_id,
            "members": results,
        }
    )
    return {"run_group_id": run_group_id, "members": results}
