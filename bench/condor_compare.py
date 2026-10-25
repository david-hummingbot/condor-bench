"""Compare two Condor checkouts: the current one against a branch, PR or folder.

The question a Condor change has to answer is "did this make Condor better or
worse at what users actually ask for?". That is Experiment A — one model, two
checkouts — and the Suites machinery already runs it. This module is the short
path to it that the dashboard's *Compare Condor* page drives:

1. **Baseline** is the checkout bench is configured with (``config.condor_path()``).
   It is fixed: that is the Condor you have now.
2. **Candidate** is either a folder you already have, or a branch / PR. For a
   branch or PR bench makes a *git worktree* of the baseline repo — a separate
   directory under ``.condor-worktrees/`` — so nothing you have checked out is
   touched. A PR is written ``#285`` (GitHub's ``pull/285/head``).
3. The candidate needs the same local, untracked setup as the baseline — its
   ``config.yml`` (server entries, credentials) and ``.env`` — or every MCP call
   fails for a reason unrelated to the code. Missing ones are copied from the
   baseline; existing ones are never overwritten. Its Python environment is built
   with ``uv sync`` on first use.
4. A compare suite gets both environments, the model, Experiment A, and the
   practical case set (``datasets/suite_templates/practical.jsonl``).

Running and comparing are the existing suite run and Compare.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import ROOT

WORKTREE_ROOT = ROOT / ".condor-worktrees"
TEMPLATES_DIR = ROOT / "datasets" / "suite_templates"
BASELINE_ENV_ID = "condor-baseline"
#: Untracked files a checkout needs to talk to the same servers as the baseline.
LOCAL_FILES = ("config.yml", ".env")
_PR_RE = re.compile(r"^(?:#|pr[/#-]?|pull/)?(\d+)(?:/head)?$", re.I)
#: Prepares share the baseline repo's refs and worktree list; the dashboard runs
#: each in a thread, so two at once must not interleave their fetch and checkout.
_PREPARE_LOCK = threading.Lock()
#: Longest slug that still fits ``compare-<slug>`` in a 64-char suite id.
_SLUG_MAX = 56


class CompareError(RuntimeError):
    """A candidate could not be prepared; the message says what to fix."""


@dataclass
class Checkout:
    path: str
    branch: str | None = None
    commit: str | None = None
    dirty_files: int = 0
    source: str = "folder"  # baseline | folder | worktree | sibling
    label: str = ""
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── git helpers ────────────────────────────────────────────────────────────────
def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout
    )


def _git_ok(repo: Path, *args: str, timeout: int = 120) -> str:
    proc = _git(repo, *args, timeout=timeout)
    if proc.returncode != 0:
        raise CompareError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def is_condor_checkout(path: Path) -> bool:
    return (path / "condor" / "agents" / "prompts.py").is_file() and (path / "mcp_servers").is_dir()


def describe(path: Path, *, source: str = "folder", label: str = "") -> Checkout:
    """Branch, commit and dirtiness of a checkout, plus anything that will break a run."""
    co = Checkout(path=str(path), source=source, label=label or path.name)
    if not path.is_dir():
        co.problems.append("folder does not exist")
        return co
    if not is_condor_checkout(path):
        co.problems.append("not a condor checkout (no condor/agents/prompts.py and mcp_servers/)")
        return co
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    co.branch = branch.stdout.strip() if branch.returncode == 0 else None
    if co.branch == "HEAD":
        co.branch = None  # detached
    commit = _git(path, "rev-parse", "--short", "HEAD")
    co.commit = commit.stdout.strip() if commit.returncode == 0 else None
    status = _git(path, "status", "--porcelain")
    co.dirty_files = len([l for l in status.stdout.splitlines() if l.strip()]) if status.returncode == 0 else 0
    for name in LOCAL_FILES:
        if not (path / name).is_file():
            co.problems.append(f"no {name} — it will be copied from the baseline when prepared")
    return co


def baseline_path() -> Path:
    from config import condor_path

    repo = condor_path()
    if repo is None:
        raise CompareError("No condor checkout configured — set CONDOR_PATH.")
    return Path(repo).resolve()


def baseline() -> Checkout:
    return describe(baseline_path(), source="baseline", label="current condor")


# ── discovery ──────────────────────────────────────────────────────────────────
def candidates() -> list[Checkout]:
    """Checkouts worth offering as the other side: worktrees, then sibling clones."""
    base = baseline_path()
    seen = {str(base)}
    out: list[Checkout] = []

    listed = _git(base, "worktree", "list", "--porcelain")
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            p = Path(line[len("worktree "):]).resolve()
            # git keeps a record for a worktree whose folder was deleted by hand.
            if str(p) in seen or not p.is_dir():
                continue
            seen.add(str(p))
            managed = WORKTREE_ROOT.resolve() in p.parents
            out.append(describe(p, source="worktree", label=p.name if not managed else f"{p.name} (bench worktree)"))

    for sibling in sorted(base.parent.iterdir()):
        p = sibling.resolve()
        if str(p) in seen or not p.is_dir() or not is_condor_checkout(p):
            continue
        seen.add(str(p))
        out.append(describe(p, source="sibling"))
    return out


# ── preparing the candidate ────────────────────────────────────────────────────
def _slug(text: str) -> str:
    """A name that is valid as a suite/environment id (``bench.suites._ID_RE``).

    It once kept ``.``, which the store rejects: ``release/1.2`` prepared fine and
    then failed at Compare with an id the store would not take.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-_")[:_SLUG_MAX].strip("-_")
    return slug or "candidate"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """``(fetch refspec, label, slug)`` for a branch name or a PR (``#285``)."""
    ref = ref.strip()
    if not ref:
        raise CompareError("Enter a branch name or a PR number.")
    pr = _PR_RE.match(ref)
    if pr:
        num = pr.group(1)
        return f"pull/{num}/head", f"PR #{num}", f"pr-{num}"
    if ref.startswith("-") or any(c in ref for c in " ~^:?*[\\"):
        raise CompareError(f"{ref!r} is not a valid branch name.")
    return ref, ref, _slug(ref)


def worktree_for(ref: str) -> Checkout:
    """Fetch *ref* from the baseline's ``origin`` and check it out in a bench worktree.

    Detached at the fetched commit, under ``.condor-worktrees/<slug>``. Reused if
    it already exists (moved to the latest commit, unless it has local changes).
    """
    with _PREPARE_LOCK:
        return _worktree_for(ref)


def _worktree_for(ref: str) -> Checkout:
    base = baseline_path()
    refspec, label, slug = parse_ref(ref)
    # Fetched into a ref of its own, not read back from FETCH_HEAD: that file is
    # shared by every fetch in the repo, so a second prepare could overwrite it
    # between this fetch and the rev-parse and check out the other ref's commit.
    local_ref = f"refs/bench/{slug}"
    _git_ok(base, "fetch", "origin", f"+{refspec}:{local_ref}", timeout=300)
    commit = _git_ok(base, "rev-parse", f"{local_ref}^{{commit}}")
    target = WORKTREE_ROOT / slug
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _discard_bench_patches(target)
        if _git(target, "status", "--porcelain").stdout.strip():
            raise CompareError(
                f"{target} has local changes; commit or discard them, or remove the worktree first."
            )
        _git_ok(target, "checkout", "--detach", commit)
    else:
        _git_ok(base, "worktree", "add", "--detach", str(target), commit, timeout=300)
    co = describe(target, source="worktree", label=label)
    co.branch = label
    return co


def _discard_bench_patches(repo: Path) -> None:
    """Undo bench's own skill patches (``bench.skill_patches``) in *repo*.

    Every run's pre-flight patches the checkout it runs against, so a reused
    worktree is always "dirty" with them. A file is restored only when its change
    is exactly the patch; a hand edit, even in the same file, still blocks reuse.
    The next run's pre-flight re-applies the patch to the new commit.
    """
    from bench.skill_patches import patch_specs

    for rel, bad, good in patch_specs():
        head = _git(repo, "show", f"HEAD:{rel}")
        path = repo / rel
        if head.returncode != 0 or bad not in head.stdout or not path.is_file():
            continue
        if path.read_text(encoding="utf-8") == head.stdout.replace(bad, good, 1):
            _git_ok(repo, "checkout", "--", rel)


def ensure_local_files(path: Path) -> list[str]:
    """Copy the baseline's untracked setup into *path* where missing. Never overwrites."""
    base = baseline_path()
    copied = []
    for name in LOCAL_FILES:
        src, dst = base / name, path / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append(name)
    return copied


def ensure_environment(path: Path, *, timeout: int = 900) -> str:
    """Build the checkout's Python environment with ``uv sync`` if it has none."""
    if (path / ".venv").is_dir():
        return "existing .venv"
    proc = subprocess.run(
        ["uv", "sync"], cwd=path, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise CompareError(f"uv sync failed in {path}: {proc.stderr.strip()[-400:]}")
    return "created .venv with uv sync"


def prepare(*, folder: str | None = None, ref: str | None = None) -> dict[str, Any]:
    """Make the candidate runnable. Exactly one of *folder* / *ref*."""
    if bool(folder) == bool(ref):
        raise CompareError("Choose a folder or enter a branch/PR — one, not both.")
    if ref:
        co = worktree_for(ref)
    else:
        path = Path(folder).expanduser().resolve()
        if path == baseline_path():
            raise CompareError("That is the baseline itself — pick a different checkout.")
        co = describe(path)
        if any("does not exist" in p or "not a condor" in p for p in co.problems):
            raise CompareError(f"{path}: {'; '.join(co.problems)}")
    path = Path(co.path)
    steps = []
    copied = ensure_local_files(path)
    if copied:
        steps.append(f"copied {', '.join(copied)} from the baseline")
    steps.append(ensure_environment(path))
    co = describe(path, source=co.source, label=co.label) if not ref else co
    co.problems = [p for p in co.problems if "will be copied" not in p]
    return {"candidate": co.as_dict(), "steps": steps}


def remove_worktree(slug: str) -> None:
    """Delete a bench-managed worktree (``git worktree remove``)."""
    # Taken as the folder name, not re-slugged: a worktree made under an older
    # slug rule (``release-1.2``) must still be removable by the name it has.
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        raise CompareError(f"No bench worktree named {slug!r}.")
    target = (WORKTREE_ROOT / slug).resolve()
    if WORKTREE_ROOT.resolve() not in target.parents or not target.exists():
        raise CompareError(f"No bench worktree named {slug!r}.")
    _git_ok(baseline_path(), "worktree", "remove", "--force", str(target))


# ── the comparison suite ───────────────────────────────────────────────────────
def load_template(name: str = "practical") -> list[dict[str, Any]]:
    path = TEMPLATES_DIR / f"{name}.jsonl"
    if not path.is_file():
        raise CompareError(f"No case template {name!r} in {TEMPLATES_DIR}.")
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def candidate_slug(cand: Path) -> str:
    """The ``cmp-``/``compare-`` id suffix for a candidate checkout.

    A bench worktree's folder is already its ref's slug (``feature-x``,
    ``pr-285``). Any other folder is named after its path: naming it after its
    branch gave two folders on ``main`` the same environment, and preparing the
    second silently repointed the first's at the new path.
    """
    if WORKTREE_ROOT.resolve() in cand.parents:
        return _slug(cand.name)
    digest = hashlib.sha1(str(cand).encode()).hexdigest()[:8]
    return f"{_slug(cand.name)[:_SLUG_MAX - 9].strip('-_') or 'condor'}-{digest}"


def _upsert(get, update, create, attempts: int = 3):
    """Update the record if it exists, else create it — tolerating a concurrent writer.

    Catching every store error on update and falling back to create turned a
    version conflict into "already exists"; a conflict is retried against the
    record as it is now, and a create that lost a race becomes an update.
    """
    from bench import suites

    for _ in range(attempts):
        try:
            current = get()
        except suites.SuiteStoreError:
            try:
                return create()
            except suites.SuiteStoreError as exc:
                if "already exists" not in str(exc):
                    raise
                continue
        try:
            return update(current)
        except suites.VersionConflict:
            continue
    raise suites.VersionConflict("The compare suite changed while it was being set up; try again.")


def setup_suite(
    candidate_path: str,
    *,
    label: str,
    model: dict[str, Any],
    case_set: str = "practical",
) -> dict[str, Any]:
    """Create or refresh the environments and the compare suite. Returns the suite."""
    from bench import suites

    base = baseline_path()
    cand = Path(candidate_path).resolve()
    slug = candidate_slug(cand)
    env_id = f"cmp-{slug}"
    suite_id = f"compare-{slug}"

    def upsert_env(eid: str, name: str, path: Path) -> None:
        payload = {"id": eid, "name": name, "condor_path": str(path),
                   # Experiment A compares code, and bench's own skill patch dirties
                   # the baseline; dirtiness is recorded in each run's manifest instead.
                   "require_clean": False, "expected_branch": None}
        _upsert(lambda: suites.get_environment(eid),
                lambda cur: suites.update_environment(eid, payload, expected_version=cur.get("version")),
                lambda: suites.create_environment(payload))

    upsert_env(BASELINE_ENV_ID, "Current condor (baseline)", base)
    upsert_env(env_id, label or cand.name, cand)

    meta = {
        "name": f"Compare: current condor vs {label or cand.name}",
        "environment_ids": [BASELINE_ENV_ID, env_id],
        "models": [model],
        "notes": f"Created by Compare Condor. Cases: {case_set}.",
    }
    suite = _upsert(lambda: suites.get_suite(suite_id),
                    lambda cur: suites.update_suite(suite_id, meta, expected_version=cur.get("version")),
                    lambda: suites.create_suite({"id": suite_id, **meta}))
    suites.replace_suite_cases(suite_id, load_template(case_set), expected_version=suite["version"])
    return suites.get_suite(suite_id)
