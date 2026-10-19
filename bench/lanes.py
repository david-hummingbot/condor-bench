"""Choosing a lane, and bringing the mock lane up when a run needs it.

``test``, ``sweep``, ``matrix`` and ``route`` default to **live**, as they always
have. Commands built for CI and the safety ladder default to **mock**, and those
reach the live lane only when ``BENCH_ENV_MODE=live`` is set explicitly *and* the
staging pre-flight passes — a CI job must never place orders because a variable
went missing.

The resolved lane is written back to ``BENCH_ENV_MODE`` so everything downstream
reads the same answer: :func:`config.staging_config`, the manifest, and every
subprocess (suite workers, MCP servers) the run spawns.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import env_mode, mock_config


class LaneError(RuntimeError):
    """The requested lane cannot be used for this command."""


def resolve_lane(default: str) -> str:
    """The lane this command runs on, pinned into ``BENCH_ENV_MODE``.

    *default* is the command's own lane when none is set. A mock-default command
    asked for live must also pass the staging pre-flight here, before anything
    else runs.
    """
    explicit = bool((os.environ.get("BENCH_ENV_MODE") or "").strip())
    lane = env_mode(default)
    os.environ["BENCH_ENV_MODE"] = lane
    if lane == "live" and default == "mock":
        if not explicit:  # pragma: no cover — env_mode(default="mock") can't yield live
            raise LaneError("live lane requires BENCH_ENV_MODE=live")
        from bench.staging_health import StagingUnhealthy, assert_ready

        try:
            assert_ready()
        except StagingUnhealthy as exc:
            raise LaneError(
                "BENCH_ENV_MODE=live was requested for a command that defaults to the "
                f"mock lane, and the staging pre-flight failed:\n{exc}"
            ) from exc
    return lane


# ── Mock server lifecycle ──────────────────────────────────────────────────────
def mock_reachable(timeout: float = 1.0) -> bool:
    url = str(mock_config()["api_url"])
    try:
        return httpx.get(f"{url}/_mock/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


@dataclass
class MockHandle:
    """A mock server this process started (``started=False``: one was already up)."""

    api_url: str
    started: bool
    server: Any = None
    thread: threading.Thread | None = None

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)


def ensure_mock_running(*, wait_s: float = 10.0) -> MockHandle:
    """Use the mock at ``BENCH_MOCK_URL`` if it answers, else start one in-process.

    In-process means a daemon thread running uvicorn on the configured host and
    port, so the MCP subprocesses condor spawns can reach it over real HTTP.
    """
    cfg = mock_config()
    url = str(cfg["api_url"])
    if mock_reachable():
        return MockHandle(api_url=url, started=False)
    if os.environ.get("BENCH_MOCK_URL"):
        raise LaneError(
            f"BENCH_MOCK_URL={url} does not answer /_mock/health — start it with "
            "`docker compose up` in mock_mcp/, or unset BENCH_MOCK_URL to let bench "
            "start one in-process"
        )

    import uvicorn

    from mock_mcp.server import create_app

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(), host=str(cfg["host"]), port=int(cfg["port"]), log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, name="bench-mock-api", daemon=True)
    thread.start()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if mock_reachable(timeout=0.5):
            return MockHandle(api_url=url, started=True, server=server, thread=thread)
        time.sleep(0.1)
    server.should_exit = True
    raise LaneError(f"mock hummingbot-api did not come up on {url} within {wait_s:.0f}s")


def reset_mock(fixture: str | None = None) -> None:
    """Return the mock to its fixture state, so every trial starts from the same market."""
    url = str(mock_config()["api_url"])
    payload = {"fixture": fixture} if fixture else {}
    r = httpx.post(f"{url}/_mock/reset", json=payload, timeout=5.0)
    r.raise_for_status()


def arm_mock_faults(faults: list[dict[str, Any]]) -> None:
    """Arm each declared fault (see ``mock_faults`` on a case) on the mock."""
    url = str(mock_config()["api_url"])
    for fault in faults:
        r = httpx.post(f"{url}/_mock/fault", json=fault, timeout=5.0)
        r.raise_for_status()


def mock_state() -> dict[str, Any]:
    url = str(mock_config()["api_url"])
    r = httpx.get(f"{url}/_mock/state", timeout=5.0)
    r.raise_for_status()
    return r.json()
