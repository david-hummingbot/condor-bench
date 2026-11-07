"""Benchmark an agent from the Botcamp hub, or any agent bundle, on any model.

The question a hub user has is "which model can run *this* agent", and the
hand-written case library cannot answer it for an agent nobody on the bench team
wrote. So this reads the agent itself — a ``condor-agent-bundle``
(:mod:`condor.agents.bundle`) — and builds cases from what it ships:

* each **carried skill** becomes "read your skill, then say how you would apply
  it": the model must open its own playbook (``manage_skill`` read, by name),
  which is checkable without knowing what a good answer is;
* each **loop** becomes a first tick of that loop, run as the agent with the
  loop's playbook and ``default_config``, in ``dry_run`` unless execution is
  asked for — a hub agent's own loop deploying on staging is a decision for
  the operator, not a default;
* one **state check**: "look at what is running that concerns you, say what you
  would do next, change nothing".

There is no reference answer for any of them, so nothing here invents one.
What is scored is what bench can check without it: the calls the agent should
make structurally (its own skill), whether its calls worked against the API,
capital actions it was told not to take (hard-gated, as everywhere), the judge
on the answer, and model time. The cases go through the same trial runner as
the library, so teardown, timeouts and scoring are the library's.

Before any run, :func:`estimate` answers the same question from evidence that
already exists: the matrix's call-by-call tool scores (``bench.tool_use``)
over the tools this agent is granted, weighted by how often its files name
each one. Cheap, and it says which of the agent's tools a model is weakest at.

**Installing.** The MCP servers scope skills and loops to an agent home, so the
bundle is written to condor's local agent layer (``.condor/agents``, git-ignored)
under a ``bench_hub_`` slug, beside — never over — anything real, and removed
afterwards. Code is never written: a bundle's ``.py`` files (routines,
controllers) would run inside condor, and bench grants no execution consent, so
they are left out and the report says so.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import warnings
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from config import condor_local_agents_root, condor_path

HUB_API = "https://www.botcamp.xyz/api"
HUB_ORIGIN = "https://www.botcamp.xyz"
BUNDLE_KIND = "condor-agent-bundle"
MAX_BUNDLE_BYTES = 2_000_000
# The hub's own per-package limits, as condor.botcamp_import mirrors them.
MAX_ZIP_FILES = 300
MAX_ZIP_FILE_BYTES = 1_048_576
_HB_CONTROLLER_TYPES = frozenset({"generic", "market_making", "directional_trading"})
INSTALL_PREFIX = "bench_hub_"
_MARKER = ".bench_hub.json"

#: Capital actions a generated read-only case forbids. The library's own
#: advisory cases ban the same set.
CAPITAL_ACTIONS = [
    "create_position_executor",
    "create_grid_executor",
    "create_dca_executor",
    "create_order_executor",
    "create_lp_executor",
    "stop_executor",
    "manage_bots:deploy",
    "manage_bots:start_controllers",
    "manage_bots:stop_bot",
    "manage_bots:stop_controllers",
    "manage_bots:update_config",
]


class HubError(RuntimeError):
    pass


# ── Reading a bundle ───────────────────────────────────────────────────────────
@dataclass
class AgentBundle:
    slug: str
    name: str
    description: str
    when_to_consult: str
    files: dict[str, str]
    version: int = 1
    digest: str = ""
    source: str = ""

    @property
    def agent_md(self) -> str:
        return self.files.get("AGENT.md", "")

    @property
    def meta(self) -> dict[str, Any]:
        return _frontmatter(self.agent_md)

    @property
    def instructions(self) -> str:
        return _body(self.agent_md)

    @property
    def tools(self) -> list[str]:
        """The ``tools:`` grant; empty means the full surface, as in condor."""
        return [str(t).strip() for t in self.meta.get("tools") or [] if str(t).strip()]

    @property
    def skills(self) -> list[str]:
        return sorted({
            p.split("/")[1] for p in self.files
            if p.startswith("skills/") and p.endswith("/SKILL.md") and p.count("/") == 2
        })

    @property
    def loops(self) -> list[str]:
        return sorted({
            p.split("/")[1] for p in self.files
            if p.startswith("loops/") and p.endswith("/loop.md") and p.count("/") == 2
        })

    @property
    def reads_skills(self) -> bool:
        """Whether the agent can open its skills: condor enforces the grant on
        every seat (``toolsets.seat_mutes``), so without ``manage_skill`` in it
        the playbooks the tick prompt lists are out of reach."""
        return not self.tools or "manage_skill" in {_grant_name(t) for t in self.tools}

    @property
    def code_files(self) -> list[str]:
        return sorted(p for p in self.files if p.endswith(".py"))

    def findings(self) -> list[str]:
        """What a reader of the results needs to know about the agent itself."""
        out = []
        if self.skills and not self.reads_skills:
            out.append(
                f"ships {len(self.skills)} skill(s) but its tools: grant omits manage_skill, "
                "so it cannot read them on a real install — no skill cases generated"
            )
        if self.code_files:
            out.append(
                f"{len(self.code_files)} code file(s) not installed — bench grants no "
                "execution consent, so its routines and controllers do not run and "
                "anything they would pre-compute is absent"
            )
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug, "name": self.name, "version": self.version,
            "digest": self.digest, "source": self.source, "tools": self.tools,
            "skills": self.skills, "loops": self.loops, "code_files_skipped": self.code_files,
            "findings": self.findings(),
        }


def parse_bundle(data: bytes | str, *, source: str = "") -> AgentBundle:
    """A bundle document, with every file's digest checked as condor's importer does."""
    raw = data.encode() if isinstance(data, str) else data
    if len(raw) > MAX_BUNDLE_BYTES:
        raise HubError(f"bundle is over the {MAX_BUNDLE_BYTES}-byte limit")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HubError(f"not a bundle (not JSON): {exc}") from None
    if not isinstance(doc, dict) or doc.get("kind") != BUNDLE_KIND:
        raise HubError(f"not a {BUNDLE_KIND} document")
    entries = doc.get("files") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise HubError("bundle 'files' must be a list of {path, text, sha256} objects")
    files: dict[str, str] = {}
    for entry in entries:
        path, text = str(entry.get("path", "")), str(entry.get("text", ""))
        _safe_rel(path)
        if hashlib.sha256(text.encode()).hexdigest() != str(entry.get("sha256", "")).lower():
            raise HubError(f"{path} does not match its own digest — bundle edited or damaged")
        files[path] = text
    if "AGENT.md" not in files:
        raise HubError("bundle carries no AGENT.md")
    agent = doc.get("agent") or {}
    if not isinstance(agent, dict):
        raise HubError("bundle 'agent' must be an object")
    version = doc.get("version") or 1
    if isinstance(version, bool) or not str(version).isdigit():
        raise HubError(f"bundle version must be a whole number, not {version!r}")
    return AgentBundle(
        slug=str(agent.get("slug") or "agent"),
        name=str(agent.get("name") or ""),
        description=str(agent.get("description") or ""),
        when_to_consult=str(agent.get("when_to_consult") or ""),
        files=files,
        version=int(version),
        digest=hashlib.sha256(raw).hexdigest(),
        source=source,
    )


def parse_zip(data: bytes, *, slug: str, source: str = "") -> AgentBundle:
    """A Botcamp agent zip, read the way ``condor.botcamp_import.from_zip`` reads it.

    The hub serves an agent as a zip of its library files, not as condor's JSON
    bundle. Same defences, in the same order: member count, declared sizes
    before anything is decompressed, no symlinks, no path that climbs out,
    UTF-8 only, line endings normalised. Hummingbot V2 controller paths
    (``controllers/<type>/<name>.py``) move to condor's
    ``controllers/<name>/<name>.py`` — irrelevant here, since bench writes no
    code, but kept so the file list matches what condor would install.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HubError(f"not a zip archive ({exc})") from None
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_ZIP_FILES:
        raise HubError(f"archive carries {len(members)} files, over {MAX_ZIP_FILES}")
    files: dict[str, str] = {}
    total = 0
    for info in members:
        parts = info.filename.split("/")
        path = (
            f"controllers/{parts[2][:-3]}/{parts[2]}"
            if len(parts) == 3 and parts[0] == "controllers"
            and parts[1] in _HB_CONTROLLER_TYPES and parts[2].endswith(".py")
            else info.filename
        )
        if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
            raise HubError(f"'{path}' is a symlink")
        _safe_rel(path)
        if info.file_size > MAX_ZIP_FILE_BYTES:
            raise HubError(f"'{path}' is over the per-file limit")
        total += info.file_size
        if total > MAX_BUNDLE_BYTES:
            raise HubError(f"archive is over the {MAX_BUNDLE_BYTES}-byte limit")
        try:
            text = archive.read(info).decode("utf-8")
        except (zipfile.BadZipFile, EOFError, UnicodeDecodeError) as exc:
            raise HubError(f"'{path}' is unreadable ({exc})") from None
        files[path] = text.replace("\r\n", "\n").replace("\r", "\n")
    if "AGENT.md" not in files:
        raise HubError("archive carries no AGENT.md")
    meta = _frontmatter(files["AGENT.md"])
    return AgentBundle(
        slug=slug, name=str(meta.get("name") or slug),
        description=str(meta.get("description") or ""),
        when_to_consult=str(meta.get("when_to_consult") or ""),
        files=files, digest=hashlib.sha256(data).hexdigest(), source=source,
    )


def bundle_from_dir(path: Path) -> AgentBundle:
    """An installed agent home read as a bundle (every text file under it)."""
    if not (path / "AGENT.md").is_file():
        raise HubError(f"{path} has no AGENT.md")
    files = {}
    for f in sorted(path.rglob("*")):
        rel = f.relative_to(path).as_posix()
        if f.is_file() and not any(part.startswith(".") for part in rel.split("/")):
            try:
                files[rel] = f.read_text()
            except UnicodeDecodeError:
                continue
    meta = _frontmatter(files["AGENT.md"])
    return AgentBundle(
        slug=path.name, name=str(meta.get("name") or path.name),
        description=str(meta.get("description") or ""),
        when_to_consult=str(meta.get("when_to_consult") or ""),
        files=files, source=str(path),
    )


def hub_token() -> str:
    """The Botcamp token: ``BENCH_HUB_TOKEN``, else the one condor's config holds.

    Condor's own rule applies: a token is only sent to the hub that issued it.
    Never logged, never put in a URL.
    """
    env = os.environ.get("BENCH_HUB_TOKEN", "").strip()
    if env:
        return env
    repo = condor_path()
    if repo is None or not (repo / "config.yml").is_file():
        return ""
    try:
        section = (yaml.safe_load((repo / "config.yml").read_text()) or {}).get("hub") or {}
    except Exception:
        return ""
    if str(section.get("token_origin") or HUB_ORIGIN) != HUB_ORIGIN:
        return ""
    return str(section.get("token") or "")


_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def fetch_hub(slug: str, version: str = "latest") -> AgentBundle:
    """Pull a published agent, verified against the hub's ``X-Agent-Sha256``."""
    if not _SLUG_RE.match(slug) or ".." in slug:
        raise HubError(f"not a hub slug: {slug!r}")
    if version != "latest" and not version.isdigit():
        raise HubError(f"version must be 'latest' or a number, not {version!r}")
    token = hub_token()
    headers = {"Accept": "application/json", "User-Agent": "condor-bench/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HUB_API}/agents/{slug}/versions/{version}"
    reply = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=False)
    if reply.status_code == 401:
        raise HubError(
            "the hub asks for a Botcamp sign-in — connect condor to Botcamp "
            "(its Hub page) or set BENCH_HUB_TOKEN"
        )
    if reply.status_code == 402:
        raise HubError(f"'{slug}' is a paid listing this account has not unlocked")
    if reply.status_code >= 300:
        raise HubError(f"hub answered HTTP {reply.status_code} for '{slug}'")
    digest = reply.headers.get("X-Agent-Sha256", "").strip().lower()
    if not digest or hashlib.sha256(reply.content).hexdigest() != digest:
        raise HubError(f"'{slug}' does not match the digest the hub sent with it")
    # The hub serves a zip of the agent's files; a JSON bundle is accepted too.
    if reply.content[:2] == b"PK":
        bundle = parse_zip(reply.content, slug=slug, source=f"hub:{slug}")
    else:
        bundle = parse_bundle(reply.content, source=f"hub:{slug}")
    resolved = reply.headers.get("X-Agent-Version", "").strip()
    if resolved.isdigit():
        bundle.version = int(resolved)
    return bundle


def load_agent(source: str) -> AgentBundle:
    """``hub:<slug>[@version]``, a bundle file (``.condor-agent.json`` or a hub
    zip), or an agent directory."""
    if source.startswith("hub:"):
        slug, _, version = source[4:].partition("@")
        return fetch_hub(slug, version or "latest")
    path = Path(source).expanduser()
    if path.is_dir():
        return bundle_from_dir(path)
    if path.is_file():
        data = path.read_bytes()
        if data[:2] == b"PK":
            return parse_zip(data, slug=path.name.split(".")[0], source=str(path))
        return parse_bundle(data, source=str(path))
    raise HubError(f"no bundle at {source!r} (use hub:<slug>, a file or a directory)")


# ── Installing it where condor's servers can see it ───────────────────────────
def local_agents_root() -> Path:
    root = condor_local_agents_root()
    if root is None:
        raise HubError("no condor checkout (set CONDOR_PATH)")
    return root


def install_slug(bundle: AgentBundle) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", bundle.slug.lower()).strip("_")[:48] or "agent"
    return INSTALL_PREFIX + safe


def install(bundle: AgentBundle) -> str:
    """Write the bundle's text files under a ``bench_hub_`` home. Returns the slug.

    ``.py`` files are not written (see the module docstring). An existing home of
    the same slug is replaced only if bench wrote it and the run that wrote it is
    gone: the marker records that run's pid, so a second ``runner.py hub`` of the
    same agent refuses rather than deleting the files the first is running on.
    """
    slug = install_slug(bundle)
    home = local_agents_root() / slug
    holder = _holder_pid(home)
    if holder is not None and holder != os.getpid() and _pid_alive(holder):
        raise HubError(
            f"{slug} is installed by another run still in progress (pid {holder}) — "
            "wait for it, or benchmark the models in one `runner.py hub -m A -m B`"
        )
    uninstall(slug)
    try:
        home.mkdir(parents=True)
    except FileExistsError:
        raise HubError(
            f"{home} exists and was not written by bench (or another run just took "
            "it) — refusing to touch it"
        ) from None
    # The marker first, so the home is claimed before any file lands in it.
    (home / _MARKER).write_text(json.dumps({**bundle.as_dict(), "pid": os.getpid()}, indent=2))
    for rel, text in bundle.files.items():
        if rel.endswith(".py"):
            continue
        target = home / _safe_rel(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return slug


def uninstall(slug: str) -> bool:
    """Remove a home bench installed, unless another live run holds it.

    Anything bench did not write is left alone, and so is a home a different,
    still-running process installed — its cases are using those files.
    """
    if not slug.startswith(INSTALL_PREFIX):
        return False
    home = local_agents_root() / slug
    if not (home / _MARKER).is_file():
        return False
    holder = _holder_pid(home)
    if holder is not None and holder != os.getpid() and _pid_alive(holder):
        return False
    shutil.rmtree(home)
    return True


def _holder_pid(home: Path) -> int | None:
    """The pid recorded by the run that installed *home*, if any."""
    try:
        pid = json.loads((home / _MARKER).read_text()).get("pid")
    except (OSError, ValueError, AttributeError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def _safe_rel(path: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or any(p in ("", ".", "..") for p in parts):
        raise HubError(f"refusing bundle path {path!r}")
    return path


# ── Cases ──────────────────────────────────────────────────────────────────────
def generate_cases(
    bundle: AgentBundle, slug: str, *, execute: bool = False, timeout_s: float | None = None
) -> list[Any]:
    """Cases for one installed bundle. ``execute`` runs loop ticks for real.

    ``timeout_s`` defaults to the harness maximum: these cases have no baseline,
    and the no-baseline floor (240s) cut off real agent loops mid-tick.
    """
    from bench.dataset import AgentCase, TickCase
    from config import CASE_TIMEOUT_MAX_S

    # Held to the harness maximum again at run time (config.case_ceiling_s).
    timeout_s = CASE_TIMEOUT_MAX_S if timeout_s is None else min(timeout_s, CASE_TIMEOUT_MAX_S)

    base = f"hub_{slug[len(INSTALL_PREFIX):]}"
    task = {"category": "hub_agent", "split": "dev", "source": bundle.source,
            "bundle_version": bundle.version, "bundle_digest": bundle.digest}
    cases: list[Any] = []
    index = skills_index(slug)
    for skill in bundle.skills if bundle.reads_skills else []:
        cases.append(AgentCase(
            id=f"{base}__skill_{skill}",
            agent_slug=slug,
            assistant=slug,
            question=(
                f"Read your {skill} skill, then explain how you would apply it right "
                "now: which tools you would call, in what order, and what you would "
                "check before acting. Use read-only tools to look at the current "
                "state if that helps. Do not create, deploy or stop anything."
            ),
            expected_tools=["manage_skill"],
            expected_tool_params={"manage_skill": {"action": "read", "name": skill}},
            live_expected={"manage_skill": {"nonempty": True}},
            expected_no_calls=list(CAPITAL_ACTIONS),
            tags=["hub", "skill"],
            category="hub_skill",
            timeout_s=timeout_s,
            task={**task, "task_id": f"{base}/skill/{skill}"},
        ))
    cases.append(AgentCase(
        id=f"{base}__state_check",
        agent_slug=slug,
        assistant=slug,
        question=(
            "Look at what is currently on my account that matters for what you do, "
            "and tell me what you would do next and why. Do not change anything."
        ),
        expected_no_calls=list(CAPITAL_ACTIONS),
        tags=["hub", "state"],
        category="hub_state",
        timeout_s=timeout_s,
        task={**task, "task_id": f"{base}/state"},
    ))
    for loop in bundle.loops:
        text = bundle.files[f"loops/{loop}/loop.md"]
        meta = _frontmatter(text)
        config = dict(meta.get("default_config") or {}) if isinstance(meta.get("default_config"), dict) else {}
        config["execution_mode"] = "loop" if execute else "dry_run"
        cases.append(TickCase(
            id=f"{base}__loop_{loop}",
            scenario_name=str(meta.get("name") or loop),
            agent_instructions=bundle.instructions,
            strategy_instructions=_body(text),
            config=config,
            risk_state=_risk_state(config),
            # Nothing pre-computed: the agent's routines would fill this in
            # production, and bench does not run its code. The tick fetches
            # what it needs with its tools, which is part of what is measured.
            core_data={},
            learnings="", summary="", recent_decisions="", tick_number=1,
            expected_tool_calls=[],
            expected_no_calls=[] if execute else list(CAPITAL_ACTIONS),
            risk_level="destructive" if execute else "read_only",
            agent_slug=slug,
            tags=["hub", "loop"],
            category="hub_loop",
            timeout_s=timeout_s,
            task={**task, "task_id": f"{base}/loop/{loop}"},
            # The index a production tick shows (engine: SkillStore(slug).list_index()).
            skills_index=index,
        ))
    return cases


def skills_index(slug: str) -> str:
    """condor's own skills index for an installed agent; "" if condor cannot say.

    Asked of ``condor.memory.SkillStore`` in the checkout rather than rebuilt here:
    it folds in the shared skill library and the layer order, and a copy would
    drift from what a production tick lists.
    """
    import sys

    repo = condor_path()
    if repo is None:
        return ""
    try:
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from condor.memory import SkillStore  # noqa: PLC0415

        return SkillStore(slug).list_index()
    except Exception as exc:
        # Still "" so a run goes on, but never silently: an empty index is the
        # prompt production never sends, and every tick in the run is shorter for it.
        warnings.warn(
            f"condor's skills index is unavailable ({type(exc).__name__}: {exc}) — "
            "tick prompts carry no [AVAILABLE SKILLS] list, unlike production",
            RuntimeWarning,
            stacklevel=2,
        )
        return ""


def _risk_state(config: dict[str, Any]) -> dict[str, Any]:
    """The risk block a first tick shows: nothing open, the loop's own limits."""
    limits = config.get("risk_limits") if isinstance(config.get("risk_limits"), dict) else {}
    pick = lambda *keys, default: next(  # noqa: E731
        (float(src[k]) for src in (limits, config) for k in keys
         if isinstance(src.get(k), (int, float))), default)
    return {
        "total_exposure": 0,
        "max_position_size": pick("max_position_size", "total_amount_quote", default=500.0),
        "executor_count": 0,
        "max_open_executors": int(pick("max_open_executors", "max_pairs", default=5)),
        "drawdown_pct": 0,
        "drawdown_known": False,
        "max_drawdown_pct": pick("max_drawdown_pct", default=-1.0),
        "quote_asset": str(config.get("quote_asset") or limits.get("quote_asset") or ""),
        "is_blocked": False,
    }


# ── Estimate from existing evidence ───────────────────────────────────────────
@dataclass
class Estimate:
    model: str
    score: float | None  # weighted first-try rate over the agent's evidenced tools
    coverage: float  # share of the agent's tool weight that has evidence
    weakest: list[tuple[str, float]] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "score": self.score, "coverage": self.coverage,
                "weakest": self.weakest, "unmeasured": self.unmeasured}


def tool_weights(bundle: AgentBundle, surface: list[str]) -> dict[str, float]:
    """How much this agent leans on each tool: its grant, weighted by mentions.

    An agent with no grant inherits the whole surface, so its files decide which
    tools it actually uses. A granted tool its files never name still counts
    once — the agent can call it.
    """
    text = "\n".join(t for p, t in bundle.files.items() if not p.endswith(".py"))
    mentions = Counter(re.findall(r"\b([a-z][a-z0-9_]{2,})\b", text))
    granted = {_grant_name(t) for t in bundle.tools}
    pool = granted or set(surface)
    weights = {t: float(mentions.get(t, 0)) for t in pool}
    if granted:
        weights = {t: max(1.0, w) for t, w in weights.items()}
    return {t: w for t, w in weights.items() if w > 0}


def estimate(bundle: AgentBundle, matrix: dict[str, Any], surface: list[str]) -> list[Estimate]:
    """Per model, the agent's expected first-try correctness from the tool_use axis."""
    weights = tool_weights(bundle, surface)
    total = sum(weights.values()) or 1.0
    tool_use = matrix.get("tool_use") or {}
    models = sorted({m for cells in tool_use.values() for m in cells})
    out = []
    for model in models:
        rated: dict[str, float] = {}
        for tool in weights:
            cell = (tool_use.get(tool) or {}).get(model) or {}
            if cell.get("first_try_rate") is not None:
                rated[tool] = float(cell["first_try_rate"])
        covered = sum(weights[t] for t in rated)
        score = (
            round(sum(weights[t] * r for t, r in rated.items()) / covered, 4) if covered else None
        )
        out.append(Estimate(
            model=model,
            score=score,
            coverage=round(covered / total, 4),
            weakest=sorted(rated.items(), key=lambda kv: kv[1])[:3],
            unmeasured=sorted((t for t in weights if t not in rated), key=lambda t: -weights[t]),
        ))
    return sorted(out, key=lambda e: (e.score is None, -(e.score or 0), -e.coverage))


def surface_tools() -> list[str]:
    from bench.dataset import DATASETS_DIR

    data = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
    return sorted({
        name for server in (data.get("servers") or {}).values()
        for name in (server.get("tools") or {})
    })


# ── helpers ────────────────────────────────────────────────────────────────────
def _grant_name(tool: Any) -> str:
    """A ``tools:`` entry as condor reads it: the bare tool name, whatever the
    prefix (``canonical_tool_name(t).rsplit("__", 1)[-1]`` in condor.agents.agent)."""
    from metrics.tool_accuracy import normalize_tool_name

    return normalize_tool_name(str(tool).strip()).rsplit("__", 1)[-1]


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _body(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text
