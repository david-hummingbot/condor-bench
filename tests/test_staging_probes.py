"""Pre-flight HTTP probes against a stubbed hummingbot-api.

Both probes here regressed against a real API version once, in the same way: a
path that does not exist reads as "unreachable" or "clean" rather than "unknown".
That is the failure mode worth testing, because a probe that fails open lets a
mutating run start against a target the pre-flight never actually inspected.
"""

from __future__ import annotations

import httpx
import pytest

from bench import staging_health
from bench.staging_health import _probe_active_executors, _probe_api

URL = "http://staging:8000"


@pytest.fixture
def stub(monkeypatch):
    """Route the probes' own AsyncClient at a handler, recording paths hit."""
    seen: list[tuple[str, str]] = []

    def install(handler):
        real = httpx.AsyncClient

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            return handler(request)

        def factory(**kwargs):
            return real(transport=httpx.MockTransport(wrapped), **kwargs)

        monkeypatch.setattr(staging_health.httpx, "AsyncClient", factory)
        return seen

    return install


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


# ── /accounts/ ────────────────────────────────────────────────────────────────
async def test_probe_api_requests_the_slashed_path(stub):
    """hummingbot-api mounts /accounts/ and does not redirect from /accounts."""
    seen = stub(
        lambda r: _json(["master_account"])
        if r.url.path == "/accounts/"
        else _json({"detail": "Not Found"}, 404)
    )

    ok, detail, names = await _probe_api(URL, "bench", "pw", 5.0)

    assert ok, detail
    assert names == {"master_account"}
    assert ("GET", "/accounts/") in seen


async def test_probe_api_distinguishes_404_from_unreachable(stub):
    """Something listening on the wrong API is not a transport failure."""
    stub(lambda r: _json({"detail": "Not Found"}, 404))

    ok, detail, names = await _probe_api(URL, "bench", "pw", 5.0)

    assert not ok
    assert names is None
    assert "404" in detail and "unreachable" not in detail


async def test_probe_api_reports_rejected_credentials(stub):
    stub(lambda r: _json({"detail": "Unauthorized"}, 401))

    ok, detail, _ = await _probe_api(URL, "bench", "wrong", 5.0)

    assert not ok
    assert "credentials" in detail


async def test_probe_api_reports_transport_failure(stub):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    stub(boom)

    ok, detail, _ = await _probe_api(URL, "bench", "pw", 5.0)

    assert not ok
    assert "unreachable" in detail


# ── active executors ──────────────────────────────────────────────────────────
def _executor_api(*, total, running):
    """Handler for the two endpoints the executor probe reads."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/executors/summary":
            if total is None:
                return _json({"detail": "Not Found"}, 404)
            return _json({"total_active": total, "by_status": {}})
        if request.url.path == "/executors/search":
            if running is None:
                return _json({"detail": "Not Found"}, 404)
            return _json({"data": running, "pagination": {"has_more": False}})
        return _json({"detail": "Not Found"}, 404)

    return handler


async def test_clean_target_reports_empty_not_unknown(stub):
    seen = stub(_executor_api(total=0, running=[]))

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) == []
    assert ("POST", "/executors/search") in seen


async def test_running_executors_are_named(stub):
    stub(
        _executor_api(
            total=2,
            running=[
                {"executor_id": "exec-1", "is_active": True},
                {"executor_id": "exec-2", "is_active": True},
            ],
        )
    )

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) == ["exec-1", "exec-2"]


async def test_terminated_items_are_dropped(stub):
    stub(
        _executor_api(
            total=1,
            running=[
                {"executor_id": "exec-1", "is_active": True},
                {"executor_id": "exec-old", "is_active": False},
            ],
        )
    )

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) == ["exec-1"]


async def test_active_under_another_status_is_counted_not_lost(stub):
    """The fail-closed cross-check: summary knows about more than search named."""
    stub(_executor_api(total=3, running=[{"executor_id": "exec-1", "is_active": True}]))

    orphans = await _probe_active_executors(URL, "bench", "pw", 5.0)

    assert orphans is not None
    assert len(orphans) == 3
    assert orphans[0] == "exec-1"
    assert orphans.count(staging_health._UNNAMED_ACTIVE) == 2


async def test_summary_alone_still_blocks(stub):
    """Search absent, summary says busy — must not report a clean target."""
    stub(_executor_api(total=1, running=None))

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) == [
        staging_health._UNNAMED_ACTIVE
    ]


async def test_search_alone_is_enough(stub):
    stub(_executor_api(total=None, running=[{"executor_id": "exec-1"}]))

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) == ["exec-1"]


async def test_neither_endpoint_present_is_unknown(stub):
    """Unknown, not clean — the caller reports it instead of passing the check."""
    stub(_executor_api(total=None, running=None))

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) is None


async def test_transport_failure_is_unknown(stub):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    stub(boom)

    assert await _probe_active_executors(URL, "bench", "pw", 5.0) is None
