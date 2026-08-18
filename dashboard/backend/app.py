"""condor-bench dashboard backend.

Endpoints:
  GET  /api/config            judge key status + resolved staging target
  GET  /api/providers         provider catalog
  GET  /api/provider-models   probe OpenAI-compat /v1/models (+ native fallbacks)
  GET  /api/staging           staging pre-flight report (fail-closed checks)
  GET  /api/datasets          case counts by layer / domain / risk level
  POST /api/runs              start benchmark run (returns run_id)
  GET  /api/runs              list completed + active runs
  GET  /api/runs/{id}         get completed run detail
  GET  /api/runs/{id}/stream  SSE live progress
  POST /api/runs/{id}/pause   stop after the in-flight case (for 429 backoff)
  POST /api/runs/{id}/resume  continue a paused run
  DELETE /api/runs/{id}       cancel active run
  GET  /api/matrix            model × domain/tool matrix (rebuilt on request)
  GET  /api/routing           routing recommendations
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
RESULTS_DIR = ROOT / "results"
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

log = logging.getLogger(__name__)

# active run state: run_id -> dict
_active_runs: dict[str, dict[str, Any]] = {}

# custom prompt runs: run_id -> dict
_custom_runs: dict[str, dict[str, Any]] = {}

PROVIDERS = [
    # ACP agents: the model is chosen inside the CLI, so bench can only name one via
    # the `provider:model` suffix. `fetch_acp_models` tells the UI it can ask the
    # bridge which ids it accepts — worth doing rather than leaving it implicit,
    # because the locally configured model can reject the request the bridge builds
    # and then every prompt in the run fails with an API 400 and no output.
    {"id": "claude-code", "label": "Claude Code", "kind": "agent", "bare_key": True,
     "needs_api_key": False, "supports_url": False, "fetch_acp_models": True,
     "models": []},
    {"id": "gemini", "label": "Gemini CLI", "kind": "agent", "bare_key": True,
     "needs_api_key": False, "supports_url": False, "fetch_acp_models": True,
     "models": []},
    {"id": "anthropic", "label": "Anthropic", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
     "key_hint": "sk-ant-..."},
    {"id": "openai", "label": "OpenAI", "kind": "cloud",
     "needs_api_key": True, "supports_url": False,
     "models": ["gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"],
     "key_hint": "sk-..."},
    {"id": "openrouter", "label": "OpenRouter", "kind": "cloud",
     "needs_api_key": True, "supports_url": False, "fetch_models": True,
     "api_base": "https://openrouter.ai/api/v1",
     "models": ["google/gemini-flash-2.0", "google/gemini-pro-2.5",
                "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-72b-instruct",
                "mistralai/mistral-small-3.1-24b-instruct"],
     "key_hint": "sk-or-..."},
    {"id": "groq", "label": "Groq", "kind": "cloud",
     "needs_api_key": True, "supports_url": False, "fetch_models": True,
     "api_base": "https://api.groq.com/openai/v1",
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
    "custom": "OPENAI_API_KEY",
}


def _judge_key_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _normalize_openai_base_url(base_url: str, default_port: int | None = None) -> str:
    """Ensure a user-supplied URL has an http:// scheme, optional default port, and /v1 suffix."""
    from urllib.parse import urlparse
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if default_port and not parsed.port:
        url = f"{parsed.scheme}://{parsed.hostname}:{default_port}{parsed.path or ''}"
    if not url.endswith("/v1"):
        url += "/v1"
    return url


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
            # pydantic-ai uses AsyncOpenAI (OpenAI-compat) for ollama, so also
            # set OPENAI_BASE_URL — the only path that reaches _build_model
            env["OPENAI_BASE_URL"] = _normalize_openai_base_url(base_url, default_port=11434)
            env.setdefault("OPENAI_API_KEY", "not-needed")
        elif provider in ("lmstudio", "custom", "openai"):
            env["OPENAI_BASE_URL"] = _normalize_openai_base_url(
                base_url, default_port=1234 if provider == "lmstudio" else None
            )
            if not api_key:
                env.setdefault("OPENAI_API_KEY", "local")
    return env


def _normalize_model_key(model_key: str) -> str:
    """Convert lmstudio:/custom: → openai: so pydantic-ai resolves it."""
    if model_key.startswith("lmstudio:"):
        return "openai:" + model_key[len("lmstudio:"):]
    if model_key.startswith("custom:"):
        return "openai:" + model_key[len("custom:"):]
    return model_key


async def _emit(run_id: str, event: dict) -> None:
    state = _active_runs.get(run_id)
    if not state:
        return
    state["events"].append(event)
    for q in list(state["listeners"]):
        await q.put(event)


# A paused run still owns the staging target and its accumulated scorecards, so it
# counts as active: starting a second run alongside it would interleave two models
# against one condor instance, which is the thing the one-run-at-a-time guard exists
# to prevent. It also has to keep showing up in /api/runs, or the UI offers no way
# back to the run holding the lock.
_ACTIVE_STATUSES = ("starting", "running", "paused")


async def _await_resume(run_id: str, state: dict) -> None:
    """Block at a case boundary while the run is paused.

    Called *between* cases, never mid-case: a pause that abandoned an in-flight
    call would leave a mutating case half-applied with no teardown, which is the
    one thing cancel already has to be careful about. Waiting here means the pause
    costs at most one more case, and the run resumes on the same connection.

    Pausing is what a 429 actually calls for — the provider is asking for fewer
    requests per minute, and cancelling would throw away every case already paid
    for.
    """
    pause_event = state.get("pause_event")
    if pause_event is None or not pause_event.is_set():
        return

    state["status"] = "paused"
    await _emit(run_id, {"type": "run_paused", "case_id": state.get("next_case")})

    while pause_event.is_set() and not state.get("cancelling"):
        # Polled rather than awaited on a second event so that cancelling a paused
        # run stays instant: task.cancel() lands on this sleep.
        await asyncio.sleep(0.25)

    # `cancel_run` clears the pause flag so the cancellation can land, which looks
    # exactly like a resume from in here. Announcing one would flash the run back to
    # "running" a beat before it reports "cancelled"; the `cancelling` marker is what
    # tells the two apart. Leave the status at "paused" and let the caller's
    # CancelledError handler set the final state.
    if state.get("cancelling"):
        return

    state["status"] = "running"
    await _emit(run_id, {"type": "run_resumed", "case_id": state.get("next_case")})


async def _run_benchmark(run_id: str, req: "RunRequest") -> None:
    from bench.baseline import BaselineStore
    from bench.cleanup import teardown
    from bench.client import case_input_text, run_case
    from bench.dataset import case_prompt_map, filter_cases, is_mutating, load_all_cases
    from bench.market_warmup import ensure_markets_for_case, warmup_failure_card
    from bench.mcp_provider import target_banner
    from bench.probe_journal import ensure_probe_journal
    from bench.reporter import save_run
    from bench.scorer import score_case, timeout_card
    from config import build_run_pin, case_timeout_s

    state = _active_runs[run_id]
    state["status"] = "running"

    try:
        # Fail closed before the first case, not per case: a run that would trade
        # on the wrong API must not start at all.
        from bench.staging_health import a_assert_ready

        await a_assert_ready()

        all_cases = load_all_cases()
        layers = None
        if req.layers:
            layers = req.layers
        elif req.consult_only:
            layers = ["consult"]
        elif req.tick_only:
            layers = ["tick"]

        cases = filter_cases(
            all_cases,
            domain=req.domain,
            category=req.category,
            layers=layers,
            risk_levels=req.risk_levels,
        )
        if not cases:
            raise ValueError("No cases matched the selected filters.")

        prompts = case_prompt_map()
        store = BaselineStore()
        total = len(req.models) * len(cases)
        state["total"] = total
        await _emit(run_id, {
            "type": "run_started",
            "total": total,
            "models": len(req.models),
            "cases_per_model": len(cases),
            "target_banner": target_banner(),
        })

        done_count = 0
        for model_cfg in req.models:
            model_key = model_cfg.model_key
            norm_key = _normalize_model_key(model_key)
            env_vars = _model_env_vars(model_key, model_cfg.api_key, model_cfg.base_url)

            await _emit(run_id, {"type": "model_started", "model": model_key})

            env_backup: dict[str, str | None] = {}
            scorecards = []
            responses: dict[str, str] = {}

            def _persist(*, partial: bool):
                """Write this model's scorecards. Returns the run dir, or None.

                Shared by the normal path and the cancel path so a cancelled run
                keeps what it measured. `save_run` is synchronous, which is what
                makes it safe to call while unwinding a cancellation — an `await`
                there would be cancelled again before it finished.
                """
                if not scorecards:
                    return None
                pin = build_run_pin(
                    run_type="adhoc",
                    # The cases actually scored, not the ones planned: a pin that
                    # claimed all 93 on a run cancelled at 12 would misdescribe
                    # its own coverage.
                    case_ids=[sc.case_id for sc in scorecards]
                    if partial
                    else [c.id for c in cases],
                    models=[norm_key],
                    shared_loaded=True,
                )
                if partial:
                    pin["partial"] = True
                    pin["cases_planned"] = len(cases)
                    pin["cases_scored"] = len(scorecards)
                return save_run(
                    norm_key,
                    scorecards,
                    responses,
                    uuid.uuid4().hex[:8],
                    prompts=prompts,
                    extra_summary=pin,
                )

            try:
                for k, v in env_vars.items():
                    env_backup[k] = os.environ.get(k)
                    os.environ[k] = v

                for case in cases:
                    # Gate before the case starts, so a pause requested mid-case
                    # takes effect once that case has finished and been cleaned up.
                    state["next_case"] = case.id
                    await _await_resume(run_id, state)

                    state["current_case"] = case.id
                    question = case_input_text(case)
                    await _emit(run_id, {
                        "type": "case_started",
                        "model": model_key,
                        "case_id": case.id,
                        "case_type": case.type,
                        "domain": case.domain,
                        "risk_level": case.risk_level,
                        "case_number": done_count + 1,
                        "total": total,
                        "question": question,
                    })

                    error: str | None = None
                    sc_dict: dict = {}
                    response = ""
                    try:
                        ensure_probe_journal(case)
                        warmup = await ensure_markets_for_case(case)
                        if not warmup.ok:
                            sc = warmup_failure_card(case, norm_key, warmup)
                            sc_dict = sc.as_dict()
                            sc_dict["question"] = question
                            scorecards.append(sc)
                        else:
                            # Baseline first: it sizes the ceiling, so a case gets room
                            # proportional to how slow it has always been.
                            baseline = store.load(case.id)
                            timeout_s = case_timeout_s(
                                baseline.latency_s if baseline else None
                            )
                            result = await asyncio.wait_for(
                                run_case(case, norm_key), timeout=timeout_s
                            )
                            baseline_latency = (
                                baseline.latency_s if baseline else result.latency_s
                            )
                            sc = await score_case(case, result, baseline_latency)
                            sc_dict = sc.as_dict()
                            sc_dict["question"] = question
                            response = result.response
                            scorecards.append(sc)
                            responses[case.id] = response

                            if is_mutating(case):
                                report = await teardown(
                                    result,
                                    norm_key,
                                    agent_slug=getattr(case, "agent_slug", None),
                                )
                                if not report.clean:
                                    await _emit(run_id, {
                                        "type": "cleanup",
                                        "case_id": case.id,
                                        "model": model_key,
                                        "report": report.as_dict(),
                                    })
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        # Record a card, not just an event: the dashboard already showed
                        # the error, but the saved run had no row for the case at all, so
                        # a thinned domain left no trace in the matrix.
                        base = store.load(case.id)
                        base_s = base.latency_s if base else 0.0
                        limit = case_timeout_s(base_s or None)
                        sc = timeout_card(case, norm_key, limit, base_s)
                        sc_dict = sc.as_dict()
                        sc_dict["question"] = question
                        scorecards.append(sc)
                        error = f"timed out after {limit:.0f}s"
                    except Exception as exc:
                        error = str(exc)

                    done_count += 1
                    await _emit(run_id, {
                        "type": "case_done",
                        "model": model_key,
                        "case_id": case.id,
                        "scorecard": sc_dict,
                        "response": response[:1000] if response else "",
                        "question": question,
                        "error": error,
                        "completed": done_count,
                        "total": total,
                    })

                run_dir = _persist(partial=False)
                if run_dir is not None:
                    await _emit(run_id, {"type": "model_done", "model": model_key, "run_dir": run_dir.name})

            except asyncio.CancelledError:
                # Cancelling used to discard everything scored so far: `save_run` sits
                # after the case loop, and the cancellation unwound straight past it.
                # A run stopped at case 82 of 93 threw away 81 scored cases and left
                # `results/` empty, which for an ACP model is hours of real spend.
                partial_dir = _persist(partial=True)
                if partial_dir is not None:
                    log.warning(
                        "run cancelled — saved %d scored case(s) to %s",
                        len(scorecards),
                        partial_dir.name,
                    )
                    state["partial_run_dirs"] = [
                        *state.get("partial_run_dirs", []),
                        partial_dir.name,
                    ]
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


async def _emit_custom(run_id: str, event: dict) -> None:
    state = _custom_runs.get(run_id)
    if not state:
        return
    state["events"].append(event)
    for q in list(state["listeners"]):
        await q.put(event)


async def _run_custom_prompt(run_id: str, req: "CustomPromptRequest") -> None:
    from bench.baseline import BaselineStore
    from bench.client import run_consult
    from bench.reporter import save_run
    from bench.scorer import score as do_score

    state = _custom_runs[run_id]
    state["status"] = "running"

    # Unique case id shared across all models in this prompt run so the Runs
    # page groups them together and the case file has a meaningful name.
    cp_case_id = f"cp_{run_id}"

    try:
        # An ad-hoc prompt is still a real agent with live tools attached, and
        # nothing constrains what it decides to call. It gets the same fail-closed
        # pre-flight a benchmark run does.
        from bench.staging_health import a_assert_ready

        await a_assert_ready()

        await _emit_custom(run_id, {"type": "started", "total": len(req.models)})
        store = BaselineStore()

        for model_cfg in req.models:
            model_key = model_cfg.model_key
            norm_key = _normalize_model_key(model_key)
            env_vars = _model_env_vars(model_key, model_cfg.api_key, model_cfg.base_url)

            await _emit_custom(run_id, {"type": "model_started", "model": model_key})

            env_backup: dict[str, str | None] = {}
            try:
                for k, v in env_vars.items():
                    env_backup[k] = os.environ.get(k)
                    os.environ[k] = v

                result = await run_consult(
                    cp_case_id,
                    req.question,
                    norm_key,
                    extra_turns=req.turns,
                    agent_slug=req.agent_slug,
                )

                baseline = store.load(cp_case_id)
                baseline_latency = baseline.latency_s if baseline else result.latency_s
                # None → no ground truth → skip tool accuracy
                expected = req.expected_tools if req.expected_tools else None
                sc = await do_score(
                    result,
                    req.question,
                    expected,
                    baseline_latency,
                    expected_tool_params=req.expected_tool_params or None,
                    domain="custom_prompt",
                )
                sc.category = "custom-prompt"
                sc.case_id = cp_case_id

                # Persist to disk so this run appears on the Runs page
                from config import build_run_pin

                pin = build_run_pin(
                    run_type="custom_prompt",
                    case_ids=[cp_case_id],
                    models=[norm_key],
                    shared_loaded=True,
                )
                pin["prompt_question"] = req.question[:300]
                run_dir = save_run(
                    norm_key,
                    [sc],
                    {cp_case_id: result.response},
                    run_id,
                    prompts={cp_case_id: req.question},
                    extra_summary=pin,
                )

                await _emit_custom(run_id, {
                    "type": "model_done",
                    "model": model_key,
                    "response": result.response,
                    "tool_calls": result.tool_calls,
                    "scorecard": sc.as_dict(),
                    "error": result.error,
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await _emit_custom(run_id, {
                    "type": "model_done",
                    "model": model_key,
                    "response": "",
                    "tool_calls": [],
                    "scorecard": {},
                    "error": str(exc),
                })
            finally:
                for k, v in env_backup.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        state["status"] = "completed"
        await _emit_custom(run_id, {"type": "done", "status": "completed"})

    except asyncio.CancelledError:
        state["status"] = "cancelled"
        await _emit_custom(run_id, {"type": "done", "status": "cancelled"})
    except Exception as exc:
        state["status"] = "failed"
        await _emit_custom(run_id, {"type": "done", "status": "failed", "error": str(exc)})
    finally:
        for q in list(state.get("listeners", [])):
            await q.put(None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    from bench.suites import ensure_store_dirs

    ensure_store_dirs()
    yield
    for state in _active_runs.values():
        task = state.get("task")
        if task and not task.done():
            task.cancel()
    for state in _custom_runs.values():
        task = state.get("task")
        if task and not task.done():
            task.cancel()


app = FastAPI(title="condor-bench", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    from bench.mcp_provider import target_banner
    from config import (
        DESTRUCTIVE_FLOOR,
        DOMAIN_PASS_RATE,
        MIN_TOOL_CASES,
        PASS_THRESHOLD,
        POST_CONDITION_FAIL_CAP,
        SCORE_WEIGHTS,
        TOOL_PASS_RATE,
        condor_path,
        judge_backend,
        judge_ready,
        staging_config,
    )

    staging = staging_config()
    return {
        "judge_key_configured": _judge_key_configured(),
        "judge_backend": judge_backend(),
        "judge_ready": judge_ready(),
        "target_banner": target_banner(),
        "condor_path": str(condor_path()) if condor_path() else None,
        "staging": {
            "api_url": staging["api_url"],
            "server_name": staging["server_name"],
        },
        # Served rather than hardcoded in the UI: a weights change in config.py has
        # to move the labels the dashboard prints, or the breakdown it shows stops
        # describing the composite it is breaking down.
        "scoring": {
            "weights": SCORE_WEIGHTS,
            "pass_threshold": PASS_THRESHOLD,
            "domain_pass_rate": DOMAIN_PASS_RATE,
            "tool_pass_rate": TOOL_PASS_RATE,
            "min_tool_cases": MIN_TOOL_CASES,
            "destructive_floor": DESTRUCTIVE_FLOOR,
            "post_condition_fail_cap": POST_CONDITION_FAIL_CAP,
        },
    }


@app.get("/api/staging")
async def get_staging_health():
    """Live pre-flight report. Never raises — the UI renders the failures."""
    from bench.staging_health import check_staging

    try:
        report = await check_staging()
        return report.as_dict()
    except Exception as exc:
        # A crash in the checker itself must not read as "staging is fine".
        return {
            "ok": False,
            "api_url": None,
            "server_name": None,
            "checks": [
                {
                    "name": "preflight",
                    "ok": False,
                    "detail": f"pre-flight itself failed: {exc}",
                    "blocking": True,
                }
            ],
        }


@app.get("/api/datasets")
async def get_datasets():
    """Case inventory, so the run form can offer real filters instead of guesses."""
    from bench.dataset import is_routing_domain, load_all_cases

    cases = load_all_cases()

    def _tally(key) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in cases:
            out[str(key(case))] = out.get(str(key(case)), 0) + 1
        return dict(sorted(out.items()))

    # layer × domain, so the run form can size a filter that names both. Summing
    # one axis and ignoring the other is how "3 models × 8 cases" became a number
    # unrelated to what the run actually executed.
    layer_domains: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = layer_domains.setdefault(str(case.type), {})
        bucket[str(case.domain)] = bucket.get(str(case.domain), 0) + 1

    # Every distinct (layer, domain, category) with its count — at most one entry
    # per case, so this is small. It exists because the three filters AND together
    # and the axes barely overlap: a Tool case's domain is always a `tool:` bucket
    # and its category is always "tool", so offering the routing domains and the
    # full category list alongside a Tools selection proposes combinations that
    # cannot match anything. With the combinations themselves in hand the form can
    # offer only what exists and count the selection exactly, instead of letting a
    # run be submitted and refused with "No cases matched the selected filters."
    # Risk joins the key rather than getting its own tally: it cuts across every
    # other axis (a `tool:leverage` case is destructive, a `tool:servers` one is
    # read-only), so a separate count would let the form offer "Tools + read_only"
    # and then report a total that ignored the risk selection.
    combos: dict[tuple[str, str, str, str], int] = {}
    for case in cases:
        key = (
            str(case.type),
            str(case.domain),
            str(case.category or ""),
            str(case.risk_level),
        )
        combos[key] = combos.get(key, 0) + 1

    return {
        "total": len(cases),
        "layers": _tally(lambda c: c.type),
        "domains": _tally(lambda c: c.domain),
        "layer_domains": {k: dict(sorted(v.items())) for k, v in sorted(layer_domains.items())},
        "combos": [
            {
                "layer": layer,
                "domain": domain,
                "category": category,
                "risk_level": risk,
                "count": count,
            }
            for (layer, domain, category, risk), count in sorted(combos.items())
        ],
        "routing_domains": sorted(
            {c.domain for c in cases if is_routing_domain(c.domain)}
        ),
        "categories": sorted({c.category for c in cases if c.category}),
        "risk_levels": _tally(lambda c: c.risk_level),
        "agent_scoped": sum(1 for c in cases if getattr(c, "agent_slug", None)),
    }


@app.get("/api/matrix")
async def get_matrix(rebuild: bool = True):
    """Model × domain/tool matrix.

    Rebuilt from results on request by default: a cached matrix.json goes stale the
    moment a new run lands, and a heatmap showing yesterday's numbers is worse than
    a slow one.
    """
    from bench.matrix import build_matrix, load_matrix, save_matrix

    if not rebuild:
        cached = load_matrix()
        if cached:
            return cached

    data = build_matrix()
    if not data["models"]:
        raise HTTPException(404, "No benchmark runs found yet.")
    save_matrix(data)
    return data


@app.get("/api/routing")
async def get_routing(
    min_pass_rate: float = 0.80,
    min_cases: int = 3,
    min_tool_pass_rate: float | None = None,
    min_tool_cases: int | None = None,
    prefer_lower_tokens: bool = False,
):
    """Routing recommendations, recomputed with the caller's criteria.

    The tool axis has its own, lower bar — see ``config.TOOL_PASS_RATE``. Both
    thresholds come back in ``criteria`` so the UI can label which is which.
    """
    from bench.matrix import save_matrix
    from bench.routing import generate, save_routing
    from config import MIN_TOOL_CASES, TOOL_PASS_RATE

    matrix_data, routing = generate(
        min_pass_rate=min_pass_rate,
        min_cases=min_cases,
        min_tool_pass_rate=(
            TOOL_PASS_RATE if min_tool_pass_rate is None else min_tool_pass_rate
        ),
        min_tool_cases=MIN_TOOL_CASES if min_tool_cases is None else min_tool_cases,
        prefer_lower_tokens=prefer_lower_tokens,
    )
    if not matrix_data["models"]:
        raise HTTPException(404, "No benchmark runs found yet.")
    save_matrix(matrix_data)
    save_routing(routing)
    return routing


@app.get("/api/models")
async def get_model_registry():
    """The sweep registry, so the UI can offer size-ordered multi-model selection."""
    from bench.matrix import load_models

    return {"models": [m.as_dict() for m in load_models()]}


@app.get("/api/settings")
async def api_get_settings():
    from bench.settings_store import get_settings

    return get_settings()


class SettingsUpdate(BaseModel):
    updates: dict[str, str | None] = {}


@app.put("/api/settings")
async def api_put_settings(body: SettingsUpdate):
    from bench.settings_store import SettingsError, update_settings

    try:
        return update_settings(body.updates)
    except SettingsError as exc:
        # A rejected value is operator error, not a server fault — 400 so the form can
        # show it next to the field instead of a generic failure.
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/providers")
async def get_providers():
    return {"providers": PROVIDERS}


@app.get("/api/acp-models")
async def get_acp_models(provider: str = "claude-code"):
    """Ask an ACP bridge which model ids it will accept.

    Doubles as a health check for the ACP path: if this fails, no run against that
    agent can work, and the error carries the bridge's own stderr instead of the
    empty responses the failure used to produce.
    """
    from bench.client import acp_available_models

    try:
        models = await asyncio.wait_for(acp_available_models(provider), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(
            504, f"`{provider}` did not answer within 60s — is the ACP bridge installed?"
        ) from None
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    rows = models.get("availableModels") or []
    return {
        "provider": provider,
        "models": [
            {
                "id": str(r.get("modelId")),
                "name": str(r.get("name") or r.get("modelId")),
                "description": str(r.get("description") or ""),
            }
            for r in rows
            if isinstance(r, dict) and r.get("modelId")
        ],
        # What a run with a bare `claude-code` key would use — the CLI's own
        # configured model, which is not necessarily one bench should recommend.
        "current": models.get("currentModelId"),
    }


def _model_ids_from_payload(data: object) -> list[str]:
    """Pull model ids out of OpenAI-compat, Ollama, or LM Studio list payloads.

    OpenAI / LM Studio ``/v1/models`` use ``data[].id``. Ollama ``/api/tags``
    uses ``models[].name``. LM Studio ``/api/v1/models`` uses ``models[].key``.
    A missing ``id`` on one row used to 500 the whole list.
    """
    if not isinstance(data, dict):
        return []
    ids: list[str] = []
    for key in ("data", "models"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, str) and row.strip():
                ids.append(row.strip())
                continue
            if not isinstance(row, dict):
                continue
            mid = row.get("id") or row.get("name") or row.get("key") or row.get("model")
            if mid:
                ids.append(str(mid))
    return sorted(set(ids))


def _openai_compat_models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base + "/models" if base.endswith("/v1") else base + "/v1/models"


def _fallback_model_urls(base_url: str, provider: str = "") -> list[str]:
    """Native list endpoints that often include models the OpenAI path omits."""
    origin = base_url.strip().rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[: -len("/v1")]
    out: list[str] = []
    if provider == "ollama" or origin.endswith(":11434"):
        out.append(origin + "/api/tags")
    if provider == "lmstudio" or origin.endswith(":1234"):
        out.append(origin + "/api/v1/models")
        out.append(origin + "/api/v0/models")
    return out


@app.get("/api/provider-models")
async def get_provider_models(base_url: str, api_key: str = "", provider: str = ""):
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    urls = [_openai_compat_models_url(base_url), *_fallback_model_urls(base_url, provider)]

    async def _one(client: httpx.AsyncClient, url: str) -> tuple[set[str], Exception | None]:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return set(_model_ids_from_payload(r.json())), None
        except Exception as exc:
            return set(), exc

    collected: set[str] = set()
    last_error: Exception | None = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            parts = await asyncio.gather(*(_one(client, url) for url in urls))
        for ids, err in parts:
            collected.update(ids)
            if err is not None:
                last_error = err
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if collected:
        return {"models": sorted(collected)}
    raise HTTPException(400, str(last_error) if last_error else "no models returned")


class ModelConfig(BaseModel):
    model_key: str
    api_key: str | None = None
    base_url: str | None = None


class RunRequest(BaseModel):
    models: list[ModelConfig]
    category: str | None = None
    domain: str | None = None
    # Dataset layers: consult | tick | tool | agent. None means all four.
    layers: list[str] | None = None
    # read_only | mutating | destructive. None means all three — a set, not a
    # ceiling, so "read_only + destructive" is expressible.
    risk_levels: list[str] | None = None
    consult_only: bool = False
    tick_only: bool = False


class CustomPromptRequest(BaseModel):
    question: str
    turns: list[str] = []
    expected_tools: list[str] = []  # empty → no ground truth, tool accuracy skipped
    expected_tool_params: dict = {}
    models: list[ModelConfig]
    # None keeps the run chat-scoped (a production consult); a slug scopes condor's
    # memory/skill tools to that agent's own stores.
    agent_slug: str | None = None


@app.post("/api/runs")
async def create_run(req: RunRequest):
    # One run at a time (a paused run still holds the target — see _ACTIVE_STATUSES)
    for state in _active_runs.values():
        if state["status"] in _ACTIVE_STATUSES:
            raise HTTPException(
                409, f"A benchmark is already {state['status']}. Cancel it first."
            )

    run_id = uuid.uuid4().hex[:8]
    _active_runs[run_id] = {
        "run_id": run_id,
        "status": "starting",
        "events": [],
        "listeners": [],
        "task": None,
        "total": 0,
        "current_case": None,
        "next_case": None,
        "started_at": time.time(),
        "models": [m.model_key for m in req.models],
        "pause_event": asyncio.Event(),
    }
    task = asyncio.create_task(_run_benchmark(run_id, req))
    _active_runs[run_id]["task"] = task
    return {"run_id": run_id}


@app.delete("/api/runs/{run_id}")
async def cancel_run(run_id: str):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    state["cancelling"] = True
    cancel_event = state.get("cancel_event")
    if cancel_event is not None:
        cancel_event.set()
    # A paused run parks in a sleep loop; clearing the flag lets the cancellation
    # land immediately instead of after a resume that will never come. `cancelling`
    # is set first so the gate reads this as a cancel rather than as a resume.
    pause_event = state.get("pause_event")
    if pause_event is not None:
        pause_event.clear()
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    return {"status": "cancelling"}


@app.post("/api/runs/{run_id}/pause")
async def pause_run(run_id: str):
    """Ask the run to stop at the next case boundary.

    Returns as soon as the request is recorded — the run is still `running` until
    the in-flight case finishes, at which point it emits `run_paused`. Callers that
    need the settled state should watch the event stream rather than this response.
    """
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    if state["status"] not in ("starting", "running", "paused"):
        raise HTTPException(409, f"Run is {state['status']} — nothing to pause.")
    pause_event = state.get("pause_event")
    if pause_event is None:
        raise HTTPException(409, "This run does not support pausing.")
    pause_event.set()
    return {"status": state["status"], "pause_requested": True}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    pause_event = state.get("pause_event")
    if pause_event is None:
        raise HTTPException(409, "This run does not support pausing.")
    pause_event.clear()
    return {"status": state["status"], "pause_requested": False}


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
        if state["status"] in _ACTIVE_STATUSES:
            # `.get` on models: a suite run's state carries `suite_id` and takes its
            # model list from the suite, so it never sets this key — and subscripting
            # it made this whole endpoint 500 whenever a suite run was in flight,
            # which is exactly when the UI needs to find its way back to the run.
            active.append({
                "run_id": rid,
                "status": state["status"],
                "models": state.get("models", []),
                "total": state.get("total", 0),
                "active": True,
                "started_at": state.get("started_at"),
                "suite_id": state.get("suite_id"),
            })
    for rid, state in _custom_runs.items():
        if state["status"] in ("starting", "running"):
            active.append({
                "run_id": rid,
                "status": state["status"],
                "models": state.get("models", []),
                "total": len(state.get("models", [])),
                "active": True,
                "run_type": "custom-prompt",
                "started_at": state.get("started_at"),
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
    prompts: dict[str, str] = {}
    try:
        suite_id = summary.get("suite_id")
        if suite_id:
            from bench.suites import suite_prompt_map

            prompts = suite_prompt_map(suite_id)
        else:
            from bench.dataset import case_prompt_map

            prompts = case_prompt_map()
    except Exception:
        pass
    cases = []
    for f in sorted((run_dir / "cases").glob("*.json")):
        try:
            record = json.loads(f.read_text())
            if not record.get("question"):
                q = prompts.get(record.get("case_id", ""))
                if q:
                    record["question"] = q
            # Custom-prompt runs store the question on the summary
            if not record.get("question") and summary.get("prompt_question"):
                record["question"] = summary["prompt_question"]
            cases.append(record)
        except Exception:
            continue
    return {"run_dir": run_dir_or_id, "summary": summary, "cases": cases, "active": False}


# ── Custom prompt ──────────────────────────────────────────────────────────────

@app.post("/api/custom-prompt")
async def create_custom_prompt(req: CustomPromptRequest):
    run_id = uuid.uuid4().hex[:8]
    _custom_runs[run_id] = {
        "run_id": run_id,
        "status": "starting",
        "events": [],
        "listeners": [],
        "task": None,
        "started_at": time.time(),
        "models": [m.model_key for m in req.models],
    }
    task = asyncio.create_task(_run_custom_prompt(run_id, req))
    _custom_runs[run_id]["task"] = task
    return {"run_id": run_id}


@app.get("/api/custom-prompt/{run_id}/stream")
async def stream_custom_prompt(run_id: str, request: Request):
    state = _custom_runs.get(run_id)
    if not state:
        raise HTTPException(404, "Custom prompt run not found")

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
                if event.get("type") == "done":
                    break
        finally:
            try:
                state["listeners"].remove(listener_q)
            except ValueError:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Suites / Environments API ──────────────────────────────────────────────────

def _suite_http_error(exc: Exception) -> HTTPException:
    from bench.suites import SuiteStoreError, VersionConflict

    if isinstance(exc, VersionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, SuiteStoreError):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


@app.get("/api/environments")
async def api_list_environments():
    from bench.suites import list_environments

    return {"environments": list_environments()}


@app.post("/api/environments")
async def api_create_environment(body: dict):
    from bench.suites import SuiteStoreError, create_environment

    try:
        return create_environment(body)
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.get("/api/environments/{env_id}")
async def api_get_environment(env_id: str):
    from bench.suites import SuiteStoreError, get_environment

    try:
        return get_environment(env_id)
    except SuiteStoreError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/environments/{env_id}")
async def api_patch_environment(env_id: str, body: dict, request: Request):
    from bench.suites import SuiteStoreError, update_environment

    expected = body.pop("version", None)
    if expected is None and request.headers.get("if-match"):
        try:
            expected = int(request.headers["if-match"])
        except ValueError:
            expected = None
    try:
        return update_environment(env_id, body, expected_version=expected)
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.delete("/api/environments/{env_id}")
async def api_delete_environment(env_id: str):
    from bench.suites import SuiteStoreError, delete_environment

    try:
        delete_environment(env_id)
        return {"ok": True}
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.get("/api/environments/{env_id}/validate")
async def api_validate_environment(env_id: str):
    from bench.suites import SuiteStoreError, validate_environment

    try:
        return validate_environment(env_id)
    except SuiteStoreError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/suites")
async def api_list_suites():
    from bench.suites import list_suites

    return {"suites": list_suites()}


@app.post("/api/suites")
async def api_create_suite(body: dict):
    from bench.suites import SuiteStoreError, create_suite

    try:
        return create_suite(body)
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.get("/api/suites/{suite_id}")
async def api_get_suite(suite_id: str):
    from bench.suites import SuiteStoreError, get_suite, list_suite_cases

    try:
        suite = get_suite(suite_id)
        suite["cases"] = list_suite_cases(suite_id)
        return suite
    except SuiteStoreError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/suites/{suite_id}")
async def api_patch_suite(suite_id: str, body: dict, request: Request):
    from bench.suites import SuiteStoreError, update_suite

    expected = body.pop("version", None)
    if expected is None and request.headers.get("if-match"):
        try:
            expected = int(request.headers["if-match"])
        except ValueError:
            expected = None
    try:
        return update_suite(suite_id, body, expected_version=expected)
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.delete("/api/suites/{suite_id}")
async def api_delete_suite(suite_id: str):
    from bench.suites import SuiteStoreError, delete_suite

    try:
        delete_suite(suite_id)
        return {"ok": True}
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.get("/api/suites/{suite_id}/cases")
async def api_list_suite_cases(suite_id: str):
    from bench.suites import SuiteStoreError, list_suite_cases

    try:
        return {"cases": list_suite_cases(suite_id)}
    except SuiteStoreError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/suites/{suite_id}/cases")
async def api_create_suite_case(suite_id: str, body: dict):
    from bench.suites import SuiteStoreError, create_suite_case

    expected = body.pop("version", None)
    if expected is None:
        raise HTTPException(400, "version is required for case writes")
    try:
        return create_suite_case(suite_id, body, expected_version=int(expected))
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.patch("/api/suites/{suite_id}/cases/{case_id}")
async def api_patch_suite_case(suite_id: str, case_id: str, body: dict):
    from bench.suites import SuiteStoreError, update_suite_case

    expected = body.pop("version", None)
    if expected is None:
        raise HTTPException(400, "version is required for case writes")
    try:
        return update_suite_case(
            suite_id, case_id, body, expected_version=int(expected)
        )
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.delete("/api/suites/{suite_id}/cases/{case_id}")
async def api_delete_suite_case(suite_id: str, case_id: str, version: int = Query(...)):
    from bench.suites import SuiteStoreError, delete_suite_case

    try:
        delete_suite_case(suite_id, case_id, expected_version=version)
        return {"ok": True}
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.post("/api/suites/{suite_id}/cases/import")
async def api_import_suite_cases(suite_id: str, body: dict):
    from bench.suites import SuiteStoreError, import_library_cases

    expected = body.get("version")
    if expected is None:
        raise HTTPException(400, "version is required")
    try:
        requested = body.get("case_ids") or []
        imported = import_library_cases(
            suite_id,
            case_ids=requested,
            layers=body.get("layers"),
            expected_version=int(expected),
        )
        # `import_library_cases` walks the library and keeps what was asked for, so
        # an id that no longer exists is simply never reached — it only raises when
        # *nothing* matched. Asking for a trimmed case alongside a live one therefore
        # succeeded quietly, having imported one of the two. Name the misses so a
        # stale id reads as a mistake rather than as a completed import.
        found = {r.get("source_case_id") for r in imported}
        unknown = [cid for cid in requested if cid not in found]
        return {"imported": imported, "count": len(imported), "unknown_case_ids": unknown}
    except SuiteStoreError as exc:
        raise _suite_http_error(exc) from exc


@app.get("/api/suites/{suite_id}/runs")
async def api_suite_runs(suite_id: str):
    runs = []
    for summary_file in sorted(RESULTS_DIR.glob("*/summary.json"), reverse=True):
        try:
            data = json.loads(summary_file.read_text())
        except Exception:
            continue
        if data.get("suite_id") != suite_id:
            continue
        data["run_dir"] = summary_file.parent.name
        runs.append(data)
    return {"runs": runs}


class SuiteRunRequest(BaseModel):
    case_ids: list[str] | None = None
    environment_ids: list[str] | None = None
    models: list[ModelConfig] | None = None


@app.post("/api/suites/{suite_id}/run")
async def api_run_suite(suite_id: str, req: SuiteRunRequest):
    from bench.suites import SuiteStoreError, get_suite

    for state in _active_runs.values():
        if state["status"] in _ACTIVE_STATUSES:
            raise HTTPException(
                409, f"A benchmark is already {state['status']}. Cancel it first."
            )

    try:
        get_suite(suite_id)
    except SuiteStoreError as exc:
        raise HTTPException(404, str(exc)) from exc

    run_id = uuid.uuid4().hex[:8]
    cancel_event = asyncio.Event()
    pause_event = asyncio.Event()
    _active_runs[run_id] = {
        "run_id": run_id,
        "status": "starting",
        "events": [],
        "listeners": [],
        "task": None,
        "total": 0,
        "current_case": None,
        "next_case": None,
        "started_at": time.time(),
        "suite_id": suite_id,
        "cancel_event": cancel_event,
        # Suite runs fan out to subprocess workers, so the finest boundary the
        # parent can hold is between members, not between cases. Pausing a suite
        # run therefore waits out the whole current model × environment member.
        "pause_event": pause_event,
    }

    async def _go():
        from bench.suite_runner import run_suite

        state = _active_runs[run_id]
        state["status"] = "running"

        async def emit(event: dict) -> None:
            await _emit(run_id, event)

        try:
            model_dicts = None
            if req.models is not None:
                model_dicts = [m.model_dump() for m in req.models]
            await run_suite(
                suite_id,
                parent_run_id=run_id,
                emit=emit,
                case_ids=req.case_ids,
                environment_ids=req.environment_ids,
                models=model_dicts,
                cancel_event=cancel_event,
                wait_if_paused=lambda: _await_resume(run_id, _active_runs[run_id]),
            )
            state["status"] = "completed"
        except asyncio.CancelledError:
            cancel_event.set()
            state["status"] = "cancelled"
            await _emit(run_id, {"type": "run_done", "status": "cancelled"})
        except Exception as exc:
            state["status"] = "failed"
            await _emit(run_id, {"type": "run_done", "status": "failed", "error": str(exc)})
        finally:
            for q in list(state.get("listeners", [])):
                await q.put(None)

    task = asyncio.create_task(_go())
    _active_runs[run_id]["task"] = task
    return {"run_id": run_id, "suite_id": suite_id}


@app.get("/api/run-groups/{run_group_id}")
async def api_run_group(run_group_id: str):
    from bench.compare import load_run_group

    return {"run_group_id": run_group_id, "members": load_run_group(run_group_id)}


@app.get("/api/compare")
async def api_compare(
    run_group: str | None = None,
    runs: str | None = None,
):
    from bench.compare import compare_runs

    run_dirs = [r for r in (runs or "").split(",") if r.strip()] or None
    return compare_runs(run_group_id=run_group, run_dirs=run_dirs)


# ── Static files (must come last) ─────────────────────────────────────────────

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(404)
