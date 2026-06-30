"""condor-bench dashboard backend.

Endpoints:
  GET  /api/config            judge key status
  GET  /api/providers         provider catalog
  GET  /api/provider-models   probe OpenAI-compat /v1/models
  POST /api/runs              start benchmark run (returns run_id)
  GET  /api/runs              list completed + active runs
  GET  /api/runs/{id}         get completed run detail
  GET  /api/runs/{id}/stream  SSE live progress
  DELETE /api/runs/{id}       cancel active run
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")
RESULTS_DIR = ROOT / "results"
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

# active run state: run_id -> dict
_active_runs: dict[str, dict[str, Any]] = {}

PROVIDERS = [
    {"id": "claude-code", "label": "Claude Code", "kind": "agent", "bare_key": True,
     "needs_api_key": False, "supports_url": False, "models": []},
    {"id": "gemini", "label": "Gemini CLI", "kind": "agent", "bare_key": True,
     "needs_api_key": False, "supports_url": False, "models": []},
    {"id": "anthropic", "label": "Anthropic", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
     "key_hint": "sk-ant-..."},
    {"id": "openai", "label": "OpenAI", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"],
     "key_hint": "sk-..."},
    {"id": "openrouter", "label": "OpenRouter", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["google/gemini-flash-2.0", "google/gemini-pro-2.5",
                "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-72b-instruct",
                "mistralai/mistral-small-3.1-24b-instruct"],
     "key_hint": "sk-or-..."},
    {"id": "groq", "label": "Groq", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
     "key_hint": "gsk_..."},
    {"id": "ollama", "label": "Ollama", "kind": "local",
     "needs_api_key": False, "supports_url": True, "fetch_models": True,
     "default_url": "http://localhost:11434", "models": []},
    {"id": "lmstudio", "label": "LM Studio", "kind": "local",
     "needs_api_key": False, "supports_url": True, "fetch_models": True,
     "default_url": "http://localhost:1234", "models": []},
    {"id": "custom", "label": "Custom (OpenAI-compat)", "kind": "local",
     "needs_api_key": False, "supports_url": True, "fetch_models": True,
     "default_url": "", "models": [], "key_hint": "optional"},
]

_PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
}


def _judge_key_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _model_env_vars(model_key: str, api_key: str | None, base_url: str | None) -> dict[str, str]:
    provider = model_key.partition(":")[0]
    env: dict[str, str] = {}
    if api_key:
        var = _PROVIDER_ENV.get(provider)
        if var:
            env[var] = api_key
    if base_url:
        if provider == "ollama":
            env["OLLAMA_HOST"] = base_url
        elif provider in ("lmstudio", "custom", "openai"):
            env["OPENAI_BASE_URL"] = base_url
            if not api_key:
                env.setdefault("OPENAI_API_KEY", "local")
    return env


def _normalize_model_key(model_key: str) -> str:
    """Convert lmstudio:model → openai:model so pydantic-ai resolves it."""
    if model_key.startswith("lmstudio:"):
        return "openai:" + model_key[len("lmstudio:"):]
    return model_key


async def _emit(run_id: str, event: dict) -> None:
    state = _active_runs.get(run_id)
    if not state:
        return
    state["events"].append(event)
    for q in list(state["listeners"]):
        await q.put(event)


async def _run_benchmark(run_id: str, req: "RunRequest") -> None:
    import sys
    sys.path.insert(0, str(ROOT))
    from bench.baseline import BaselineStore
    from bench.client import run_consult, run_tick, build_tick_prompt_for_case
    from bench.dataset import load_all_cases
    from bench.reporter import save_run
    from bench.scorer import score as do_score

    state = _active_runs[run_id]
    state["status"] = "running"

    try:
        cases = load_all_cases()
        if req.consult_only:
            cases = [c for c in cases if c.type == "consult"]
        elif req.tick_only:
            cases = [c for c in cases if c.type == "tick"]
        if req.category:
            cases = [c for c in cases if c.category == req.category]

        store = BaselineStore()
        total = len(req.models) * len(cases)
        state["total"] = total
        await _emit(run_id, {"type": "run_started", "total": total, "models": len(req.models), "cases_per_model": len(cases)})

        done_count = 0
        for model_cfg in req.models:
            model_key = model_cfg["model_key"]
            norm_key = _normalize_model_key(model_key)
            env_vars = _model_env_vars(model_key, model_cfg.get("api_key"), model_cfg.get("base_url"))

            await _emit(run_id, {"type": "model_started", "model": model_key})

            env_backup: dict[str, str | None] = {}
            scorecards = []
            responses: dict[str, str] = {}
            try:
                for k, v in env_vars.items():
                    env_backup[k] = os.environ.get(k)
                    os.environ[k] = v

                for i, case in enumerate(cases):
                    state["current_case"] = case.id
                    await _emit(run_id, {
                        "type": "case_started",
                        "model": model_key,
                        "case_id": case.id,
                        "case_type": case.type,
                        "case_number": done_count + 1,
                        "total": total,
                    })

                    error: str | None = None
                    sc_dict: dict = {}
                    response = ""
                    try:
                        if case.type == "tick":
                            prompt = build_tick_prompt_for_case(case, norm_key)
                            result = await run_tick(case.id, prompt, norm_key, case.mock_tools)
                            expected_tools = case.expected_tool_calls
                        else:
                            result = await run_consult(
                                case.id, case.question, norm_key,
                                extra_turns=getattr(case, "turns", []),
                                mock_tools=getattr(case, "mock_tools", {}),
                            )
                            expected_tools = getattr(case, "expected_tools", [])

                        baseline = store.load(case.id)
                        baseline_latency = baseline.latency_s if baseline else result.latency_s
                        sc = await do_score(result, case.question, expected_tools, baseline_latency)
                        sc_dict = sc.as_dict()
                        response = result.response
                        scorecards.append(sc)
                        responses[case.id] = response
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        error = str(exc)

                    done_count += 1
                    await _emit(run_id, {
                        "type": "case_done",
                        "model": model_key,
                        "case_id": case.id,
                        "scorecard": sc_dict,
                        "response": response[:1000] if response else "",
                        "error": error,
                        "completed": done_count,
                        "total": total,
                    })

                if scorecards:
                    run_id_short = uuid.uuid4().hex[:8]
                    run_dir = save_run(norm_key, scorecards, responses, run_id_short)
                    await _emit(run_id, {"type": "model_done", "model": model_key, "run_dir": run_dir.name})

            except asyncio.CancelledError:
                raise
            finally:
                for k, v in env_backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        state["status"] = "completed"
        await _emit(run_id, {"type": "run_done", "status": "completed"})

    except asyncio.CancelledError:
        state["status"] = "cancelled"
        await _emit(run_id, {"type": "run_done", "status": "cancelled"})
    except Exception as exc:
        state["status"] = "failed"
        await _emit(run_id, {"type": "run_done", "status": "failed", "error": str(exc)})
    finally:
        for q in list(state.get("listeners", [])):
            await q.put(None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for state in _active_runs.values():
        task = state.get("task")
        if task and not task.done():
            task.cancel()


app = FastAPI(title="condor-bench", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return {"judge_key_configured": _judge_key_configured()}


@app.get("/api/providers")
async def get_providers():
    return {"providers": PROVIDERS}


@app.get("/api/provider-models")
async def get_provider_models(base_url: str, api_key: str = ""):
    url = base_url.rstrip("/") + "/v1/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            models = sorted(m["id"] for m in data.get("data", []))
            return {"models": models}
    except Exception as exc:
        raise HTTPException(400, str(exc))


class ModelConfig(BaseModel):
    model_key: str
    api_key: str | None = None
    base_url: str | None = None


class RunRequest(BaseModel):
    models: list[ModelConfig]
    category: str | None = None
    consult_only: bool = False
    tick_only: bool = False


@app.post("/api/runs")
async def create_run(req: RunRequest):
    # One run at a time
    for state in _active_runs.values():
        if state["status"] in ("starting", "running"):
            raise HTTPException(409, "A benchmark is already running. Cancel it first.")

    run_id = uuid.uuid4().hex[:8]
    _active_runs[run_id] = {
        "run_id": run_id,
        "status": "starting",
        "events": [],
        "listeners": [],
        "task": None,
        "total": 0,
        "current_case": None,
        "started_at": time.time(),
        "models": [m.model_key for m in req.models],
    }
    task = asyncio.create_task(_run_benchmark(run_id, req))
    _active_runs[run_id]["task"] = task
    return {"run_id": run_id}


@app.delete("/api/runs/{run_id}")
async def cancel_run(run_id: str):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    return {"status": "cancelling"}


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")

    listener_q: asyncio.Queue = asyncio.Queue()
    state["listeners"].append(listener_q)

    async def generate():
        try:
            for event in list(state["events"]):
                yield f"data: {json.dumps(event)}\n\n"
            if state["status"] in ("completed", "cancelled", "failed"):
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(listener_q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "run_done":
                    break
        finally:
            try:
                state["listeners"].remove(listener_q)
            except ValueError:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
async def list_runs():
    completed = []
    for summary_file in sorted(RESULTS_DIR.glob("*/summary.json"), reverse=True):
        try:
            data = json.loads(summary_file.read_text())
            data["run_dir"] = summary_file.parent.name
            data["active"] = False
            completed.append(data)
        except Exception:
            continue

    active = []
    for rid, state in _active_runs.items():
        if state["status"] in ("starting", "running"):
            active.append({
                "run_id": rid,
                "status": state["status"],
                "models": state["models"],
                "total": state["total"],
                "active": True,
                "started_at": state["started_at"],
            })

    return {"runs": completed, "active": active}


@app.get("/api/runs/{run_dir_or_id}")
async def get_run(run_dir_or_id: str):
    # Check active runs first
    if run_dir_or_id in _active_runs:
        state = _active_runs[run_dir_or_id]
        return {"run_id": run_dir_or_id, "status": state["status"], "active": True}

    run_dir = RESULTS_DIR / run_dir_or_id
    summary_file = run_dir / "summary.json"
    if not summary_file.exists():
        raise HTTPException(404, "Run not found")

    summary = json.loads(summary_file.read_text())
    cases = []
    for f in sorted((run_dir / "cases").glob("*.json")):
        try:
            cases.append(json.loads(f.read_text()))
        except Exception:
            continue
    return {"run_dir": run_dir_or_id, "summary": summary, "cases": cases, "active": False}


# ── Static files (must come last) ─────────────────────────────────────────────

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(404)
