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


def test_a_worker_signalled_twice_still_finishes_its_teardown(tmp_path):
    """The runner signals the process group and `uv run` forwards SIGTERM too; the
    second signal used to cancel the teardown the first one started."""
    import asyncio
    import os
    import signal

    from config import ROOT

    script = tmp_path / "worker_shape.py"
    script.write_text(
        "import asyncio, importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(ROOT / 'scripts' / 'suite_worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "async def run(job):\n"
        "    try:\n"
        "        print('ready', flush=True)\n"
        "        await asyncio.sleep(30)\n"
        "    except asyncio.CancelledError:\n"
        "        await asyncio.sleep(1.0)  # the trial's teardown\n"
        "        print('teardown done', flush=True)\n"
        "        raise\n"
        "w._run = run\n"
        "print(asyncio.run(w._run_until_stopped({'member_run_id': 'm0'})), flush=True)\n"
    )

    async def go():
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "python", str(script), cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, start_new_session=True)
        while (await proc.stdout.readline()).strip() != b"ready":
            pass
        os.killpg(proc.pid, signal.SIGTERM)  # exactly what _stop_worker sends
        # uv's forwarded copy can land after the teardown has started; send it late
        # on purpose, since landing in the same loop iteration hides the bug.
        await asyncio.sleep(0.3)
        os.killpg(proc.pid, signal.SIGTERM)
        out = (await proc.stdout.read()).decode()
        await proc.wait()
        return out

    out = asyncio.run(go())
    assert "teardown done" in out and "cancelled" in out


# ── regressions: ids, concurrency, the run endpoint ────────────────────────────
@pytest.fixture
def store(tmp_path, monkeypatch):
    import bench.suites as suites

    monkeypatch.setattr(suites, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(suites, "ENVIRONMENTS_DIR", tmp_path / "envs")
    return suites


def test_slugs_are_ids_the_suite_store_accepts():
    """`release/1.2` kept its dot, the store refused `cmp-release-1.2`, and Compare
    failed with a 500 after Prepare had succeeded."""
    from bench.suites import _ID_RE

    for ref in ("release/1.2", "v1.2.3", "Feature/X..Y", "-_-weird-_-", "a" * 200, "...."):
        slug = cc._slug(ref)
        assert _ID_RE.match(f"cmp-{slug}") and _ID_RE.match(f"compare-{slug}"), (ref, slug)
        assert len(f"compare-{slug}") <= 64
    assert cc.parse_ref("release/1.2")[2] == "release-1-2"


def test_a_dotted_branch_sets_up_a_suite(world, store):
    _git(world["base"], "push", "-q", "origin", "HEAD:refs/heads/release/1.2")
    cand = Path(cc.prepare(ref="release/1.2")["candidate"]["path"])
    suite = cc.setup_suite(str(cand), label="release/1.2", model={"model_key": "m"})
    assert suite["id"] == "compare-release-1-2"
    assert suite["environment_ids"] == [cc.BASELINE_ENV_ID, "cmp-release-1-2"]


def test_a_worktree_named_under_the_old_slug_rule_can_still_be_removed(world):
    base = world["base"]
    target = cc.WORKTREE_ROOT / "release-1.2"
    cc.WORKTREE_ROOT.mkdir(parents=True)
    _git(base, "worktree", "add", "-q", "--detach", str(target), "HEAD")
    cc.remove_worktree("release-1.2")
    assert not target.exists()


def test_a_concurrent_fetch_cannot_swap_the_commit_a_worktree_is_made_at(world, monkeypatch):
    """Both prepares fetched into the shared FETCH_HEAD and read it back, so the
    second one's fetch could land in between and the first got the wrong commit."""
    base = world["base"]
    feature = _git(base, "ls-remote", "origin", "refs/heads/feature/x").split()[0]
    real = cc._git_ok
    interleaved = []

    def git_ok(repo, *args, **kw):
        out = real(repo, *args, **kw)
        if args[0] == "fetch" and not interleaved:
            # Another prepare's fetch, between this fetch and its rev-parse.
            interleaved.append(real(base, "fetch", "origin", "main"))
        return out

    monkeypatch.setattr(cc, "_git_ok", git_ok)
    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    assert interleaved
    assert _git(cand, "rev-parse", "HEAD") == feature


def test_concurrent_prepares_each_get_their_own_ref(world):
    from concurrent.futures import ThreadPoolExecutor

    base = world["base"]
    want = {
        "feature/x": _git(base, "ls-remote", "origin", "refs/heads/feature/x").split()[0],
        "#7": _git(base, "ls-remote", "origin", "refs/pull/7/head").split()[0],
        "main": _git(base, "ls-remote", "origin", "refs/heads/main").split()[0],
    }
    with ThreadPoolExecutor(3) as pool:
        out = dict(zip(want, pool.map(lambda r: cc.prepare(ref=r), want)))
    for ref, commit in want.items():
        assert _git(Path(out[ref]["candidate"]["path"]), "rev-parse", "HEAD") == commit, ref


def test_two_folders_on_the_same_branch_get_their_own_environments(world, store):
    """Both folders are on `main`; naming the environment after the branch made the
    second Compare repoint the first one's environment at the second folder."""
    one = _condor_repo(world["tmp"] / "one" / "condor")
    two = _condor_repo(world["tmp"] / "two" / "condor")
    s1 = cc.setup_suite(str(one), label="main", model={"model_key": "m"})
    s2 = cc.setup_suite(str(two), label="main", model={"model_key": "m"})
    assert s1["id"] != s2["id"]
    env1, env2 = s1["environment_ids"][1], s2["environment_ids"][1]
    assert env1 != env2
    assert store.get_environment(env1)["condor_path"] == str(one.resolve())
    assert store.get_environment(env2)["condor_path"] == str(two.resolve())
    # The same folder maps to the same suite next time.
    assert cc.setup_suite(str(one), label="main", model={"model_key": "m"})["id"] == s1["id"]


def test_setup_survives_a_concurrent_environment_write(world, store, monkeypatch):
    """A version conflict on update fell through to create, which then failed with
    "already exists"; it is retried against the record as it is now."""
    cand = Path(cc.prepare(ref="feature/x")["candidate"]["path"])
    cc.setup_suite(str(cand), label="feature/x", model={"model_key": "m"})
    real = store.update_environment
    raced = []

    def racing_update(eid, patch, *, expected_version):
        if not raced:
            raced.append(eid)
            real(eid, {}, expected_version=None)  # someone else wrote it first
        return real(eid, patch, expected_version=expected_version)

    monkeypatch.setattr(store, "update_environment", racing_update)
    suite = cc.setup_suite(str(cand), label="feature/x", model={"model_key": "m2"})
    assert raced and suite["models"] == [{"model_key": "m2"}]


def _compare_client():
    from fastapi.testclient import TestClient

    from dashboard.backend.app import app

    return TestClient(app)


_RUN_BODY = {"candidate_path": "/x", "label": "x", "model": {"model_key": "m"}}


def test_compare_while_a_run_is_active_changes_nothing(monkeypatch):
    """setup_suite rewrote the running group's suite and environments before the
    busy check refused the second click."""
    from dashboard.backend import app as appmod

    calls = []
    monkeypatch.setattr(cc, "setup_suite", lambda *a, **k: calls.append(a))
    appmod._active_runs["busy-cmp"] = {"status": "running"}
    try:
        r = _compare_client().post("/api/compare-condor/run", json=_RUN_BODY)
    finally:
        appmod._active_runs.pop("busy-cmp", None)
    assert r.status_code == 409
    assert calls == [], "the suite must not be touched while a run is active"


def test_a_store_error_during_compare_setup_is_a_4xx(monkeypatch):
    from bench.suites import SuiteStoreError, VersionConflict

    def bad_id(*a, **k):
        raise SuiteStoreError("Invalid environment id 'cmp-x.y'")

    monkeypatch.setattr(cc, "setup_suite", bad_id)
    r = _compare_client().post("/api/compare-condor/run", json=_RUN_BODY)
    assert r.status_code == 400 and "Invalid environment id" in r.json()["detail"]

    def conflict(*a, **k):
        raise VersionConflict("changed")

    monkeypatch.setattr(cc, "setup_suite", conflict)
    assert _compare_client().post("/api/compare-condor/run", json=_RUN_BODY).status_code == 409


# ── the task-by-task table (dashboard/frontend/src/compareRows.js) ─────────────
def _side_by_side(base_rows, cand_rows):
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from config import ROOT

    module = (ROOT / "dashboard" / "frontend" / "src" / "compareRows.js").as_uri()
    script = (
        f"import {{ sideBySide }} from {json.dumps(module)};\n"
        "let raw = ''; process.stdin.on('data', (d) => raw += d);\n"
        "process.stdin.on('end', () => { const [b, c] = JSON.parse(raw);\n"
        "  console.log(JSON.stringify(sideBySide(b, c))) })\n"
    )
    out = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps([base_rows, cand_rows]),
                         capture_output=True, text=True, check=True)
    return {r["task"]: r for r in json.loads(out.stdout)}


def _row(task, *, passed=None, composite=0.9, error=None, harness=None, trial=1):
    return {"case_id": f"compare-x__{task}", "trial_idx": trial, "passed": passed,
            "composite": composite, "error": error, "harness_artifact": harness}


def test_a_candidate_that_only_errors_reads_as_worse_not_no_data():
    base = [_row("t", passed=True, trial=i) for i in (1, 2, 3)]
    errored = [_row("t", error="boom: MCP server exited", composite=0.0, trial=1),
               _row("t", error="timeout after 120s", harness="case exceeded its 120s ceiling",
                    composite=0.0, trial=2),
               _row("t", error="model returned nothing", composite=0.0, trial=3)]
    row = _side_by_side(base, errored)["t"]
    assert row["verdict"] == "worse"
    assert "candidate only errored" in row["note"]
    assert row["cand"]["n"] == 3 and row["cand"]["passed"] == 0
    # Mirrored: the current checkout erroring is the candidate being better.
    assert _side_by_side(errored, base)["t"]["verdict"] == "better"


def test_harness_and_infra_rows_stay_excluded():
    base = [_row("t", passed=True)]
    cand = [_row("t", harness="market warm-up failed", error="infra: market warm-up failed"),
            _row("t", error="infra: 429 rate limit")]
    row = _side_by_side(base, cand)["t"]
    assert row["verdict"] == "no data" and row["cand"]["excluded"] == 2 and row["cand"]["n"] == 0


def test_an_errored_trial_counts_against_the_pass_rate():
    base = [_row("t", passed=True, trial=1), _row("t", passed=True, trial=2)]
    cand = [_row("t", passed=True, trial=1), _row("t", error="boom", composite=0.0, trial=2)]
    row = _side_by_side(base, cand)["t"]
    assert row["verdict"] == "worse" and row["cand"]["passed"] == 1 and row["cand"]["n"] == 2
    assert row["delta"] == 0, "an error's 0.0 composite is not a measured score"


# ── suite members get the request's credentials the way a dashboard run does ───
def test_suite_workers_get_every_providers_key_and_a_normalised_base_url(monkeypatch):
    """Only anthropic/openrouter/openai/custom keys were forwarded, so a Groq or
    NVIDIA key typed in the form never reached the worker; and a base URL went
    through raw where the dashboard adds scheme, port and /v1."""
    import asyncio
    import sys

    import bench.suite_runner as sr

    for var in ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    models = [
        {"model_key": "groq:llama-3.3-70b-versatile", "api_key": "gsk_test"},
        {"model_key": "nvidia:openai/gpt-oss-20b", "api_key": "nvapi-test"},
        {"model_key": "lmstudio:qwen", "base_url": "localhost"},
    ]
    monkeypatch.setattr(sr, "get_suite", lambda sid: {"id": sid, "environment_ids": ["a"], "models": models})
    monkeypatch.setattr(sr, "get_environment", lambda eid: {"id": eid, "condor_path": "/x"})
    monkeypatch.setattr(sr, "check_experiment_freeze", lambda *a, **k: "adhoc")
    seen: list = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, env=None, **kw):
        job = json.loads(Path(args[args.index("--job") + 1]).read_text())
        seen.append((job, env))
        ok = "import json; print(json.dumps({'ok': True, 'run_dir': 'r', 'cases': 0}))"
        return await real_exec(sys.executable, "-c", ok, **{k: v for k, v in kw.items() if k != "cwd"})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def emit(e):
        pass

    asyncio.run(sr.run_suite("s", parent_run_id="p", emit=emit))
    (groq_job, groq), (nim_job, nim), (_, local) = seen
    assert groq["GROQ_API_KEY"] == "gsk_test"
    assert nim["NVIDIA_API_KEY"] == "nvapi-test"
    assert local["OPENAI_BASE_URL"] == "http://localhost:1234/v1"
    assert "OPENAI_BASE_URL" not in groq, "a base URL is only set for providers that use one"
    # The plan position the summary records, so Compare can order members by it.
    assert [groq_job["member_index"], nim_job["member_index"]] == [0, 1]
