"""Compare Condor: baseline vs a branch, PR or folder, on the practical case set."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench import condor_compare as cc


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _condor_repo(path: Path) -> Path:
    """A minimal checkout that passes is_condor_checkout."""
    (path / "condor" / "agents").mkdir(parents=True)
    (path / "condor" / "agents" / "prompts.py").write_text("# prompts\n")
    (path / "mcp_servers").mkdir()
    (path / "mcp_servers" / "__init__.py").write_text("")
    (path / ".gitignore").write_text("config.yml\n.env\n.venv/\n")
    from bench.skill_patches import patch_specs

    for rel, bad, _good in patch_specs():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(f"# cookbook\n{bad}\n")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """origin (bare) ← baseline clone with config.yml/.env, plus a feature branch and a PR ref."""
    seed = _condor_repo(tmp_path / "seed")
    _git(seed, "checkout", "-q", "-b", "feature/x")
    (seed / "condor" / "agents" / "prompts.py").write_text("# prompts v2\n")
    _git(seed, "commit", "-qam", "feature")
    _git(seed, "checkout", "-q", "main")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    _git(origin, "update-ref", "refs/pull/7/head", _git(origin, "rev-parse", "feature/x"))
    base = tmp_path / "condor"
    _git(tmp_path, "clone", "-q", str(origin), str(base))
    (base / "config.yml").write_text("servers: {bench_staging: {host: h}}\n")
    (base / ".env").write_text("TOKEN=x\n")

    monkeypatch.setattr(cc, "baseline_path", lambda: base.resolve())
    monkeypatch.setattr(cc, "WORKTREE_ROOT", tmp_path / "bench" / ".condor-worktrees")
    monkeypatch.setattr(cc, "ensure_environment", lambda path, **k: "skipped in tests")
    return {"base": base, "tmp": tmp_path}


def test_refs_parse_as_branches_or_prs():
    assert cc.parse_ref("#288") == ("pull/288/head", "PR #288", "pr-288")
    assert cc.parse_ref("pr-288")[1] == "PR #288"
    assert cc.parse_ref("feature/new-api") == ("feature/new-api", "feature/new-api", "feature-new-api")
    for bad in ("", "-rf", "a b", "x~1"):
        with pytest.raises(cc.CompareError):
            cc.parse_ref(bad)


def test_a_branch_becomes_a_worktree_with_the_baselines_local_setup(world):
    out = cc.prepare(ref="feature/x")
    cand = Path(out["candidate"]["path"])
    assert cand.parent == cc.WORKTREE_ROOT and cand.name == "feature-x"
    assert (cand / "condor" / "agents" / "prompts.py").read_text() == "# prompts v2\n"
    assert (cand / "config.yml").read_text() == (world["base"] / "config.yml").read_text()
    assert "copied config.yml, .env from the baseline" in out["steps"]
    # The baseline checkout itself is untouched.
    assert _git(world["base"], "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_pr_number_fetches_the_pull_ref(world):
    out = cc.prepare(ref="#7")
    assert out["candidate"]["branch"] == "PR #7"
    assert Path(out["candidate"]["path"]).name == "pr-7"


def test_a_reused_worktree_with_local_changes_is_refused(world):
    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    (cand / "condor" / "agents" / "prompts.py").write_text("# hand edit\n")
    with pytest.raises(cc.CompareError, match="local changes"):
        cc.prepare(ref="feature/x")


def test_bench_skill_patches_do_not_block_reusing_a_worktree(world):
    """The pre-flight patches every checkout it runs on; that is not a local change."""
    from bench.skill_patches import apply_skill_patches, patch_specs

    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    assert [r.status for r in apply_skill_patches(cand)] == ["applied"]
    cc.prepare(ref="feature/x")  # no "local changes" refusal
    assert not _git(cand, "status", "--porcelain")

    # A hand edit in the patched file still counts.
    apply_skill_patches(cand)
    rel = patch_specs()[0][0]
    (cand / rel).write_text((cand / rel).read_text() + "my note\n")
    with pytest.raises(cc.CompareError, match="local changes"):
        cc.prepare(ref="feature/x")


def test_a_folder_gets_missing_setup_copied_but_never_overwritten(world):
    other = _condor_repo(world["tmp"] / "condor-other")
    (other / ".env").write_text("TOKEN=mine\n")
    out = cc.prepare(folder=str(other))
    assert (other / "config.yml").exists(), "missing config.yml copied"
    assert (other / ".env").read_text() == "TOKEN=mine\n", "existing .env kept"
    assert out["candidate"]["source"] == "folder"


def test_preparing_refuses_the_baseline_both_inputs_or_a_non_checkout(world):
    with pytest.raises(cc.CompareError, match="baseline itself"):
        cc.prepare(folder=str(world["base"]))
    with pytest.raises(cc.CompareError, match="one, not both"):
        cc.prepare(folder="/x", ref="y")
    (world["tmp"] / "notcondor").mkdir()
    with pytest.raises(cc.CompareError, match="not a condor checkout"):
        cc.prepare(folder=str(world["tmp"] / "notcondor"))


def test_worktrees_and_siblings_are_offered_as_candidates(world):
    cc.prepare(ref="feature/x")
    _condor_repo(world["tmp"] / "condor-sibling")
    found = {Path(c.path).name: c for c in cc.candidates()}
    assert "feature-x" in found and found["feature-x"].source == "worktree"
    assert "condor-sibling" in found and found["condor-sibling"].source == "sibling"
    assert "condor" not in found, "the baseline is not its own candidate"


def test_only_bench_worktrees_can_be_removed(world):
    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    with pytest.raises(cc.CompareError):
        cc.remove_worktree("../../condor")
    cc.remove_worktree("feature-x")
    assert not cand.exists()


# ── the suite ──────────────────────────────────────────────────────────────────
def test_the_practical_set_is_well_formed():
    rows = cc.load_template("practical")
    assert len(rows) >= 12
    assert len({r["id"] for r in rows}) == len(rows)
    trades = [r for r in rows if r["risk_level"] == "destructive" and "create_order_executor" in r.get("expected_tools", [])]
    assert trades and all(r.get("post_conditions") for r in trades), "an order case must verify its end state"
    assert any(r["id"] == "candles_trend" for r in rows)


def test_setup_builds_an_experiment_a_suite_with_both_checkouts(world, tmp_path, monkeypatch):
    import bench.suites as suites

    monkeypatch.setattr(suites, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(suites, "ENVIRONMENTS_DIR", tmp_path / "envs")
    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    suite = cc.setup_suite(str(cand), label="feature/x", model={"model_key": "openrouter:openai/gpt-4o-mini"})
    assert suite["id"] == "compare-feature-x"
    assert suite["environment_ids"] == [cc.BASELINE_ENV_ID, "cmp-feature-x"]
    assert suites.get_environment(cc.BASELINE_ENV_ID)["condor_path"] == str(world["base"].resolve())
    assert suites.get_environment("cmp-feature-x")["require_clean"] is False
    rows = suites.list_suite_cases(suite["id"])
    assert len(rows) == len(cc.load_template()) and all(r["id"].startswith("compare-feature-x__") for r in rows)

    # Re-running setup refreshes rather than duplicating.
    again = cc.setup_suite(str(cand), label="feature/x", model={"model_key": "openrouter:openai/gpt-4o-mini"})
    assert len(suites.list_suite_cases(again["id"])) == len(rows)

    # Suite cases keep what the library loader keeps: markets and post-conditions.
    cases = {c.id.split("__")[-1]: c for c in suites.load_suite_cases_as_objects(suite["id"])}
    assert cases["limit_buy"].markets["venue"]["pair"] == "BTC-USDT"
    assert cases["limit_buy"].post_conditions["list_executors"]["status"] == "RUNNING"
    assert cases["candles_trend"].expected_tool_params["get_market_data"]["action"] == "candles"


def test_the_compare_page_endpoint_reports_baseline_and_cases(monkeypatch, world):
    from fastapi.testclient import TestClient

    from dashboard.backend.app import app

    data = TestClient(app).get("/api/compare-condor").json()
    assert data["baseline"]["source"] == "baseline"
    assert len(data["case_sets"]["practical"]) == len(cc.load_template())


def test_the_suite_worker_preflight_runs_inside_its_event_loop(monkeypatch):
    """The worker is async; the sync assert_ready() started a second loop and failed
    every suite member at start ("asyncio.run() cannot be called from a running
    event loop") — so no suite, and no Compare Condor run, ever reached a case."""
    import asyncio
    import importlib.util

    import bench.staging_health as health
    from config import ROOT

    spec = importlib.util.spec_from_file_location("suite_worker", ROOT / "scripts" / "suite_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    async def unhealthy(*a, **k):
        raise health.StagingUnhealthy("api_reachable: refused")

    def sync_must_not_be_used(*a, **k):
        raise AssertionError("the sync pre-flight was called from the running loop")

    monkeypatch.setattr(health, "a_assert_ready", unhealthy)
    monkeypatch.setattr(health, "assert_ready", sync_must_not_be_used)
    job = {"suite_id": "s", "environment_id": "e", "run_group_id": "g", "model": "m",
           "member_run_id": "m0", "case_ids": None}
    out = asyncio.run(worker._run(job))
    assert out["ok"] is False and "refused" in out["error"]


def test_suite_progress_streams_per_trial_across_members(monkeypatch):
    """A member used to report only when it finished — a whole checkout's worth of
    trials — so the progress bar sat still for half an hour."""
    import asyncio
    import sys

    import bench.suite_runner as sr

    monkeypatch.setattr(sr, "get_suite", lambda sid: {"id": sid, "environment_ids": ["a", "b"],
                                                      "models": [{"model_key": "m"}]})
    monkeypatch.setattr(sr, "get_environment", lambda eid: {"id": eid, "condor_path": "/x"})
    monkeypatch.setattr(sr, "check_experiment_freeze", lambda *a, **k: "A")
    worker = (
        "import json\n"
        f"P = {sr.PROGRESS_PREFIX!r}\n"
        "for i in (1, 2):\n"
        "    print(P + json.dumps({'type': 'case_done', 'case_id': f'c{i}', 'completed': i, 'total': 2}), flush=True)\n"
        "print('some other output')\n"
        "print(json.dumps({'ok': True, 'run_dir': 'r', 'cases': 2}))\n"
    )
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kw):
        return await real_exec(sys.executable, "-c", worker, **{k: v for k, v in kw.items() if k != "cwd"})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    events: list = []

    async def emit(e):
        events.append(e)

    out = asyncio.run(sr.run_suite("s", parent_run_id="p", emit=emit))
    done = [(e["environment_id"], e["completed"], e["total"]) for e in events if e["type"] == "case_done"]
    assert done == [("a", 1, 4), ("a", 2, 4), ("b", 3, 4), ("b", 4, 4)]
    assert [m["status"] for m in out["members"]] == ["done", "done"]


def test_stopping_a_worker_stops_what_it_started(monkeypatch):
    """`uv run` cannot forward SIGKILL: killing only it left the Python worker
    running, still placing orders after the run was marked cancelled."""
    import asyncio
    import os
    import sys

    import bench.suite_runner as sr

    monkeypatch.setattr(sr, "_WORKER_STOP_GRACE_S", 2.0)
    # A parent that ignores SIGTERM (as uv would not forward SIGKILL), with a child.
    script = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )

    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE, start_new_session=True
        )
        child = int((await proc.stdout.readline()).decode())
        await sr._stop_worker(proc)
        await asyncio.sleep(0.2)
        return proc.returncode, child

    code, child = asyncio.run(go())
    assert code is not None
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    if alive:  # reap a zombie re-parented to init before declaring it alive
        with open(f"/proc/{child}/stat") as f:
            alive = f.read().split()[2] != "Z"
    assert not alive, "the worker's own child must not survive"
