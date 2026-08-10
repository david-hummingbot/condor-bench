"""Suite Run-all orchestrator: one parent run_id, sequential subprocess members."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from bench.suites import get_environment, get_suite
from config import ROOT

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


async def run_suite(
    suite_id: str,
    *,
    parent_run_id: str,
    emit: EmitFn,
    case_ids: list[str] | None = None,
    environment_ids: list[str] | None = None,
    models: list[dict[str, Any]] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Fan out suite × environments × models via subprocess workers.

    Returns ``{run_group_id, members: [...]}``. Emits ``member_*`` and case-level
    progress is owned by the worker (parent only sees member boundaries).
    """
    suite = get_suite(suite_id)
    env_ids = environment_ids or list(suite.get("environment_ids") or [])
    if not env_ids:
        raise ValueError("Suite has no environments attached")

    model_cfgs = models or list(suite.get("models") or [])
    if not model_cfgs:
        raise ValueError("Suite has no models configured")

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

    for index, (env, model_cfg) in enumerate(members_plan):
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
            "require_clean": bool(env.get("require_clean", True)),
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
            )
            stdout_b, stderr_b = await current_proc.communicate()
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()
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
