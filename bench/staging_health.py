"""Fail-closed pre-flight for benchmark runs.

The failure this exists to prevent: condor's ``.mcp.json`` declares
``mcp-hummingbot`` with no CLI args, so the MCP server's own settings chain falls
back to ``HUMMINGBOT_API_URL`` and then to ``http://localhost:8000`` with
``admin``/``admin``. On a developer machine that last hop is plausibly the *real*
hummingbot-api. A benchmark that quietly lands there would place orders against
live capital while reporting tool scores.

So this module refuses to run rather than guessing. Every check returns a
verdict and ``assert_ready()`` raises unless the ones marked blocking pass.

    from bench.staging_health import assert_ready
    assert_ready()     # raises StagingUnhealthy on any blocking failure

Isolation is the API instance's job, not a flag here: point
``HUMMINGBOT_API_URL`` at an instance carrying only test connectors. What this
module guarantees is that bench is provably talking to *that* instance — see the
``mcp_url_matches`` check, which is the reason the rest of the guard rails can be
this thin.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from config import condor_path, staging_config
from bench.mcp_provider import (
    LiveWiringError,
    build_mcp_configs,
    condor_server_entry,
    effective_api_url,
)


class StagingUnhealthy(RuntimeError):
    """A blocking pre-flight check failed. Do not run against this target."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class HealthReport:
    api_url: str | None
    server_name: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every blocking check passed."""
        return all(c.ok for c in self.checks if c.blocking)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_url": self.api_url,
            "server_name": self.server_name,
            "ok": self.ok,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "blocking": c.blocking,
                }
                for c in self.checks
            ],
        }


def is_condor_main_cmdline(parts: list[str]) -> bool:
    """True when an argv looks like ``python … main.py``.

    Argument-exact, not substring. A substring test on the whole command line matched
    any *shell* whose script text happened to mention main.py — including this
    project's own tooling — and a check that fires on itself is worse than no check.
    """
    if not parts or "python" not in parts[0]:
        return False
    return any(a == "main.py" or a.endswith("/main.py") for a in parts[1:])


def running_condor_checkouts() -> list[str]:
    """Working directories of any live condor ``main.py`` process.

    Read from ``/proc`` and best-effort: an empty list means "could not tell", not
    "nothing is running". Used only to warn about a mismatch, never to assert one.
    """
    found: set[str] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            parts = [a for a in raw.decode(errors="replace").split("\0") if a]
            if not parts:
                continue
            if not is_condor_main_cmdline(parts):
                continue
            cwd = (entry / "cwd").resolve()
        except (OSError, PermissionError, RuntimeError):
            continue
        found.add(str(cwd))
    return sorted(found)


async def check_staging(*, timeout: float = 10.0) -> HealthReport:
    """Probe the live target and return a verdict per check. Never raises."""
    staging = staging_config()
    report = HealthReport(
        api_url=None,
        server_name=str(staging["server_name"]),
    )

    # 1. condor checkout — the MCP wiring comes from its _shared.py
    repo = condor_path()
    report.checks.append(
        Check(
            "condor_checkout",
            repo is not None,
            f"condor at {repo}" if repo else "no condor checkout — set CONDOR_PATH",
        )
    )
    if repo is None:
        return report

    # 1a'. Temporary skill patches on the checkout the model will read.
    #
    # Condor's routine_cookbook still documents get_prices as a flat pair→price
    # map; the API nests under prices{}. agent_condor_routine_001 followed the
    # cookbook, saw None for a live BTC-USDT price, and guessed
    # binance_paper_trade. Until Condor fixes hummingbot_client.md, rewrite the
    # known-bad snippet here so the case measures the model, not the doc bug.
    # Non-blocking: a missing/unknown file must not abort the whole run.
    from bench.skill_patches import apply_skill_patches

    for patch in apply_skill_patches(repo):
        report.checks.append(
            Check(
                f"skill_patch:{patch.name}",
                patch.status in {"applied", "already_correct"},
                f"{patch.status}: {patch.detail}",
                blocking=False,
            )
        )

    # 1a. The checkout bench reads must be the one that is *running*.
    #
    # Two checkouts existed side by side — a feature branch serving the live bot and
    # web API, and `main`, which is where CONDOR_PATH pointed. Nothing complained:
    # bench spawned its MCP servers from `main`, and `_jwt_secret()` falls back to a
    # secret persisted per-checkout in config.yml, so the subprocess signed service
    # tokens with one key while the web API verified with the other. Every `delegate`,
    # `consult` and `manage_routines:run` call answered "401: Invalid token", and eight
    # cases were scored as model failures for it.
    #
    # The quieter half is worse: bench's vendored-prompt drift checks compared against
    # a checkout nobody was running, so they could pass while the prompt in production
    # had moved. A run measured code that was not deployed.
    running = running_condor_checkouts()
    if running:
        resolved = str(Path(repo).resolve())
        matched = resolved in running
        report.checks.append(
            Check(
                "condor_process_matches_checkout",
                matched,
                f"live condor runs from {resolved}"
                if matched
                else (
                    f"CONDOR_PATH is {resolved} but the running condor is "
                    f"{', '.join(running)} — bench would measure code that is not "
                    "deployed, and service tokens signed from a different config.yml "
                    "are rejected as 401 by the running web API"
                ),
            )
        )
        if not matched:
            return report

    # 1b. Auto-register the fixed bench_staging entry from HUMMINGBOT_* env so
    #     Settings (URL + creds) is enough — no manual server name / ACL step.
    if staging.get("api_url"):
        from bench.staging_setup import ensure_bench_server

        sync = ensure_bench_server()
        report.checks.append(
            Check("bench_server_sync", sync.ok, sync.detail, blocking=True)
        )
        if not sync.ok:
            return report

    # 2. HUMMINGBOT_API_URL declared, and the two spellings agree
    declared = str(staging["api_url"])
    expected = str(staging["expected_api_url"])
    report.checks.append(
        Check(
            "api_url_declared",
            bool(declared),
            declared or "HUMMINGBOT_API_URL is unset — nothing to verify the MCP "
            "server's target against, so a localhost fallback would go unnoticed",
        )
    )
    report.checks.append(
        Check(
            "api_url_aliases_agree",
            declared == expected,
            "HUMMINGBOT_API_URL matches BENCH_EXPECTED_API_URL"
            if declared == expected
            else f"HUMMINGBOT_API_URL={declared!r} != BENCH_EXPECTED_API_URL={expected!r} "
            "— resolve the ambiguity before running",
        )
    )

    # 3. BENCH_SERVER_NAME registered in condor's servers config
    server_name = str(staging["server_name"])
    try:
        entry = condor_server_entry(server_name)
    except LiveWiringError as exc:
        report.checks.append(Check("server_registered", False, str(exc)))
        return report
    report.checks.append(
        Check(
            "server_registered",
            entry is not None,
            f"'{server_name}' → {entry['host']}:{entry['port']}"
            if entry
            else f"'{server_name}' is not in {repo / 'config.yml'}. condor would start "
            "without mcp-hummingbot and every tool case would fail for the wrong "
            "reason. Save Staging URL/credentials in Settings, or run: "
            "uv run python scripts/register_bench_server.py",
        )
    )
    if entry is None:
        return report

    # 4. THE fail-closed check: the URL the subprocess is actually launched with
    #    must be the staging URL, exactly. No prefix matching, no localhost hop.
    try:
        configs = build_mcp_configs(agent_slug=None, server_name=server_name)
        resolved = effective_api_url(configs)
    except LiveWiringError as exc:
        report.checks.append(Check("mcp_url_matches", False, str(exc)))
        return report

    report.api_url = resolved
    matches = bool(resolved) and bool(declared) and resolved == declared
    report.checks.append(
        Check(
            "mcp_url_matches",
            matches,
            f"MCP --url resolves to {resolved} (matches HUMMINGBOT_API_URL)"
            if matches
            else f"MCP --url resolves to {resolved!r} but HUMMINGBOT_API_URL is "
            f"{declared!r}. Refusing to run: this is how a benchmark ends up "
            f"trading on the wrong hummingbot-api. Fix the '{server_name}' entry "
            f"in {repo / 'config.yml'} (host/port) so it points at staging.",
        )
    )
    if not matches:
        return report

    # 5. Staging API reachable and authenticating
    reachable, detail, accounts = await _probe_api(
        resolved, str(staging["username"]), str(staging["password"]), timeout
    )
    report.checks.append(Check("api_reachable", reachable, detail))

    # 6. At least one account on the API. We do not pin a specific account name —
    #    point HUMMINGBOT_API_URL at an instance that only has test connectors when
    #    you want isolation.
    if accounts is None:
        report.checks.append(
            Check(
                "accounts_listed",
                False,
                "could not list accounts on the API",
                blocking=False,
            )
        )
    else:
        report.checks.append(
            Check(
                "accounts_listed",
                len(accounts) > 0,
                f"{len(accounts)} account(s) on API"
                if accounts
                else "API reachable but has no accounts — every case that trades "
                "will fail",
                blocking=True,
            )
        )

    # 7. Orphaned executors from a prior run. Blocking for every run: any case may
    #    create an executor, and teardown then runs against this instance — a
    #    teardown that stops "everything running" would kill unrelated positions.
    orphans = await _probe_active_executors(
        resolved, str(staging["username"]), str(staging["password"]), timeout
    )
    if orphans is None:
        report.checks.append(
            Check(
                "no_orphaned_executors",
                False,
                "could not read active executors",
                blocking=False,
            )
        )
    else:
        report.checks.append(
            Check(
                "no_orphaned_executors",
                not orphans,
                "no active executors"
                if not orphans
                else f"{len(orphans)} active executor(s) predate this run "
                f"({', '.join(orphans[:5])}) — clean up first so teardown can't "
                "stop something it didn't create",
                blocking=True,
            )
        )

    return report


async def _probe_api(
    url: str, username: str, password: str, timeout: float
) -> tuple[bool, str, set[str] | None]:
    """Return (reachable, detail, account names or None).

    The path carries a trailing slash deliberately: hummingbot-api mounts the
    router as ``/accounts/`` and runs with slash-redirects off, so ``/accounts``
    is a hard 404 rather than a 307 to the real route.
    """
    auth = httpx.BasicAuth(username, password) if username else None
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
            r = await client.get(f"{url}/accounts/")
            if r.status_code == 401:
                return False, f"{url} rejected the bench credentials (401)", None
            if r.status_code == 404:
                # Reachable, but not the API we expect. Distinguished from a
                # transport failure because "unreachable" sends you debugging the
                # network when the process is up and answering.
                return (
                    False,
                    f"{url} answered 404 for /accounts/ — something is listening "
                    f"but it does not look like a hummingbot-api",
                    None,
                )
            r.raise_for_status()
            data = r.json()
            names = _account_names(data)
            return True, f"{url} reachable ({len(names)} account(s))", names
    except Exception as exc:
        return False, f"{url} unreachable: {type(exc).__name__}: {exc}", None


def _account_names(payload: Any) -> set[str]:
    if isinstance(payload, list):
        return {
            str(item.get("account_name") or item.get("name"))
            if isinstance(item, dict)
            else str(item)
            for item in payload
        }
    if isinstance(payload, dict):
        for key in ("accounts", "data", "items"):
            if key in payload:
                return _account_names(payload[key])
    return set()


# Status hummingbot-api reports for an executor that is still working. The search
# filter takes one value, so anything active under a different status is caught by
# the count cross-check in _probe_active_executors rather than by this filter.
_RUNNING_STATUS = "RUNNING"
# Schema maximum for ExecutorFilterRequest.limit. A staging box with more than
# this many *running* executors is not a scenario worth paginating for: the check
# fails on the first non-empty page either way.
_SEARCH_LIMIT = 1000
_UNNAMED_ACTIVE = "<active, id unavailable>"


async def _probe_active_executors(
    url: str, username: str, password: str, timeout: float
) -> list[str] | None:
    """Ids of executors active on the target, or ``None`` when unknowable.

    Reads two endpoints, because neither is fail-closed on its own.
    ``/executors/summary`` counts active executors authoritatively but carries no
    ids; ``POST /executors/search`` returns ids, but only for the one status it is
    filtered on, so an executor active under some other status would read as a
    clean target. Taking the count from the first and the ids from the second
    makes that gap surface as an unnamed entry instead of passing the check.

    Returning ``None`` (unknown) rather than ``[]`` matters: the caller reports
    unknown as a non-blocking failure, where a wrongly-empty list would let a run
    start against a target holding someone else's positions.
    """
    auth = httpx.BasicAuth(username, password) if username else None
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
            total = await _active_executor_count(client, url)
            ids = await _running_executor_ids(client, url)
    except Exception:
        return None

    if total is None and ids is None:
        # Neither endpoint answered — an older API version, or not one at all.
        return None

    found = list(ids or [])
    if total is not None and total > len(found):
        # Active, but not under the status we filtered on. Counted without an id
        # so the check still fails rather than calling the target clean.
        found += [_UNNAMED_ACTIVE] * (total - len(found))
    return found


async def _active_executor_count(client: httpx.AsyncClient, url: str) -> int | None:
    """``total_active`` from the executors summary, or None if unavailable."""
    try:
        r = await client.get(f"{url}/executors/summary")
        if r.status_code >= 400:
            return None
        payload = r.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("total_active")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def _running_executor_ids(client: httpx.AsyncClient, url: str) -> list[str] | None:
    """Executor ids in the running status, or None if the search is unavailable."""
    try:
        r = await client.post(
            f"{url}/executors/search",
            json={"status": _RUNNING_STATUS, "limit": _SEARCH_LIMIT},
        )
        if r.status_code >= 400:
            return None
        payload = r.json()
    except Exception:
        return None

    items = payload if isinstance(payload, list) else payload.get("data", [])
    if not isinstance(items, list):
        return None
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Only an explicit False rules an item out; the filter already asked for
        # running, and a version that omits the field should not silently drop it.
        if item.get("is_active") is False:
            continue
        ids.append(str(item.get("executor_id") or item.get("id") or "?"))
    return ids


def assert_ready(*, timeout: float = 10.0) -> HealthReport:
    """Run the pre-flight and raise ``StagingUnhealthy`` unless it passes.

    Call this before the first case of a run.
    """
    report = asyncio.run(check_staging(timeout=timeout))
    _raise_unless_ready(report)
    return report


async def a_assert_ready(*, timeout: float = 10.0) -> HealthReport:
    """Async form of :func:`assert_ready`, for use inside a running loop."""
    report = await check_staging(timeout=timeout)
    _raise_unless_ready(report)
    return report


def _raise_unless_ready(report: HealthReport) -> None:
    failures = report.failures()
    if failures:
        lines = "\n".join(f"  ✗ {c.name}: {c.detail}" for c in failures)
        raise StagingUnhealthy(
            f"Staging pre-flight failed ({len(failures)} blocking check(s)) — "
            f"refusing to run:\n{lines}"
        )


def format_report(report: HealthReport) -> str:
    """Human-readable pre-flight summary for the CLI."""
    head = "staging pre-flight"
    if report.api_url:
        head += f" url={report.api_url}"
    lines = [head]
    for c in report.checks:
        mark = "✓" if c.ok else ("✗" if c.blocking else "•")
        lines.append(f"  {mark} {c.name}: {c.detail}")
    return "\n".join(lines)
