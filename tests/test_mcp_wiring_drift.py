"""Live MCP spawn args must stay identical to what condor launches in production.

Same philosophy as ``test_tool_surface_drift.py``, one layer down: that file keeps
the *mocks* honest about which tools exist, this one keeps *live mode* honest
about how the real servers are started.

Two things are checked, and both matter:

1. **bench == condor.** ``bench/mcp_provider.build_mcp_configs("live", …)`` must
   produce the same ``name``/``command``/``args`` as condor's own
   ``build_mcp_servers_for_agent()`` / ``build_mcp_servers_for_session()``. bench
   loads those functions rather than copying them, so this mostly guards the
   *call site* — a new required parameter on the condor side (an
   ``--execution-mode``, say) would change production's output while bench keeps
   calling the old signature.

2. **Both == a pinned shape.** The expected arg list is spelled out literally
   below. Comparing bench against condor alone can't catch a condor-side change,
   because bench would follow it silently. The pin is what turns "condor changed
   its MCP wiring" into a failing test someone has to look at.

condor-evals' ``build_mcp_servers()`` fails check 2 today: no ``--server-name``
on either server and no ``--agent-slug`` on condor. That is why its wiring was
not ported.

Needs a condor checkout, so it skips without one (CI without the sibling repo).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The bench identity used for these comparisons. Fixed values, not the operator's
# env, so the test asserts the same thing on every machine.
CHAT_ID = 999001
USER_ID = 999001
SERVER = "bench_drift_probe"
HOST = "staging.internal"
PORT = 8123
USERNAME = "bench"
PASSWORD = "bench-secret"
AGENT_SLUG = "routine_builder"


def _expected_hummingbot_args() -> list[str]:
    """Pinned mcp-hummingbot spawn args.

    ``--server-name`` is the one condor-evals omits. It selects which
    hummingbot-api instance the tools bind to and is what ``start_agent``
    resolves against; without it the server falls back to HUMMINGBOT_API_URL and
    then to localhost:8000.
    """
    return [
        "run",
        "python",
        "-m",
        "mcp_servers.hummingbot_api",
        "--url",
        f"http://{HOST}:{PORT}",
        "--username",
        USERNAME,
        "--password",
        PASSWORD,
        "--server-name",
        SERVER,
    ]


def _expected_condor_args(agent_slug: str | None, bot_token: str) -> list[str]:
    """Pinned condor MCP spawn args.

    ``--agent-slug`` is present only for agent-scoped runs and scopes the
    memory/skill tools to ``agents/{slug}/``. Chat-scoped consults omit it, which
    is what production does for a consult session — so its absence there is
    correct, not a gap.
    """
    args = [
        "run",
        "python",
        "-m",
        "mcp_servers.condor",
        "--chat-id",
        str(CHAT_ID),
        "--user-id",
        str(USER_ID),
        "--bot-token",
        bot_token,
    ]
    if agent_slug:
        args += ["--agent-slug", agent_slug]
    args += ["--server-name", SERVER]
    return args


@pytest.fixture(scope="module")
def condor_repo() -> Path:
    from config import condor_path

    repo = condor_path()
    if repo is None:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")
    return repo


@pytest.fixture(scope="module")
def wiring(condor_repo: Path, tmp_path_factory):
    """Load condor's helpers against a throwaway config.yml holding our probe server.

    A temp config keeps the operator's real condor config.yml out of the test —
    both so the assertions don't depend on what servers they happen to have, and
    so a test run can never write to it.
    """
    import yaml

    cfg_dir = tmp_path_factory.mktemp("condor_config")
    cfg_path = cfg_dir / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "servers": {
                    SERVER: {
                        "host": HOST,
                        "port": PORT,
                        "username": USERNAME,
                        "password": PASSWORD,
                    }
                },
                "users": {},
                "server_access": {},
                "chat_defaults": {},
            }
        )
    )

    import sys

    if str(condor_repo) not in sys.path:
        sys.path.insert(0, str(condor_repo))
    import config_manager

    config_manager.ConfigManager.reset_instance()
    config_manager.ConfigManager.instance(str(cfg_path))

    # bench/mcp_provider caches the loaded module and would otherwise re-seed
    # ConfigManager with the operator's real config.yml.
    from bench import mcp_provider

    mcp_provider._shared_module = None
    shared = mcp_provider.load_condor_shared()
    # load_condor_shared() re-instantiates the singleton; it is a no-op when one
    # already exists, so our temp config stays in force. Assert that, because a
    # silent switch back to the real config.yml would make this test meaningless.
    assert config_manager.get_config_manager().get_server(SERVER) is not None, (
        "temp condor config did not take effect — the probe server is not visible"
    )

    yield shared, mcp_provider

    config_manager.ConfigManager.reset_instance()
    mcp_provider._shared_module = None


def _bench_configs(mcp_provider, monkeypatch, agent_slug: str | None) -> list[dict]:
    monkeypatch.setenv("BENCH_MODE", "live")
    monkeypatch.setenv("BENCH_SERVER_NAME", SERVER)
    monkeypatch.setenv("BENCH_CHAT_ID", str(CHAT_ID))
    monkeypatch.setenv("BENCH_USER_ID", str(USER_ID))
    monkeypatch.setenv("HUMMINGBOT_API_URL", f"http://{HOST}:{PORT}")
    return mcp_provider.build_mcp_configs(
        "live", agent_slug=agent_slug, server_name=SERVER
    )


def _by_name(configs: list[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in configs}


# ── Check 2: production output matches the pinned shape ────────────────────────
@pytest.mark.parametrize("agent_slug", [AGENT_SLUG, None])
def test_condor_helpers_match_pinned_spawn_args(wiring, agent_slug):
    shared, _ = wiring
    bot_token = os.environ.get("TELEGRAM_TOKEN", "")

    if agent_slug:
        produced = shared.build_mcp_servers_for_agent(
            server_name=SERVER, user_id=USER_ID, chat_id=CHAT_ID, agent_slug=agent_slug
        )
    else:
        produced = shared.build_mcp_servers_for_session(
            user_id=USER_ID, chat_id=CHAT_ID, server_name=SERVER, agent_slug=None
        )

    by_name = _by_name(produced)
    assert set(by_name) == {"mcp-hummingbot", "condor"}, (
        f"condor now builds a different set of MCP servers: {sorted(by_name)}. "
        "Review bench/mcp_provider.py — live runs would get a tool surface the "
        "pinned expectations no longer describe."
    )

    assert by_name["mcp-hummingbot"]["args"] == _expected_hummingbot_args(), (
        "condor's mcp-hummingbot spawn args changed. Update the pin in this test "
        "only after confirming bench passes whatever the new args need."
    )
    assert by_name["condor"]["args"] == _expected_condor_args(agent_slug, bot_token), (
        "condor's condor-MCP spawn args changed. Update the pin in this test only "
        "after confirming bench passes whatever the new args need."
    )
    for cfg in produced:
        assert cfg["command"] == "uv", (
            f"{cfg['name']} is no longer launched with `uv` ({cfg['command']!r}) — "
            "bench sets cwd to the condor repo for exactly that launcher."
        )


# ── Check 1: bench reproduces production output ────────────────────────────────
@pytest.mark.parametrize("agent_slug", [AGENT_SLUG, None])
def test_bench_live_configs_match_condor(wiring, monkeypatch, agent_slug):
    shared, mcp_provider = wiring

    if agent_slug:
        expected = shared.build_mcp_servers_for_agent(
            server_name=SERVER, user_id=USER_ID, chat_id=CHAT_ID, agent_slug=agent_slug
        )
    else:
        expected = shared.build_mcp_servers_for_session(
            user_id=USER_ID, chat_id=CHAT_ID, server_name=SERVER, agent_slug=None
        )

    actual = _bench_configs(mcp_provider, monkeypatch, agent_slug)

    exp, act = _by_name(expected), _by_name(actual)
    assert set(act) == set(exp), (
        f"bench live servers {sorted(act)} != condor's {sorted(exp)}"
    )
    for name in exp:
        assert act[name]["command"] == exp[name]["command"], f"{name}: command drifted"
        assert act[name]["args"] == exp[name]["args"], (
            f"{name}: bench spawn args diverged from condor's.\n"
            f"  condor: {exp[name]['args']}\n"
            f"  bench:  {act[name]['args']}"
        )


def test_bench_adds_only_documented_extras(wiring, monkeypatch):
    """bench's additions to a production config are cwd and one env pin, no more.

    Anything else appearing here means live mode is diverging from production in a
    way check 1 can't see, since it only compares command and args.
    """
    _, mcp_provider = wiring
    actual = _bench_configs(mcp_provider, monkeypatch, AGENT_SLUG)

    for cfg in actual:
        extra = set(cfg) - {"name", "command", "args", "env"}
        assert extra <= set(mcp_provider.BENCH_ONLY_CONFIG_KEYS), (
            f"{cfg['name']} carries undocumented config keys {sorted(extra)}"
        )
        # cwd must be the condor repo: `uv run python -m mcp_servers.…` resolves
        # only inside that project, and bench's own cwd is a different one.
        from config import condor_path

        assert cfg["cwd"] == str(condor_path()), (
            f"{cfg['name']} cwd={cfg['cwd']!r} — must be the condor repo root"
        )
        assert mcp_provider.env_overlay_keys(cfg) <= {"HUMMINGBOT_API_URL"}, (
            f"{cfg['name']} env overlay grew beyond the documented URL pin: "
            f"{sorted(mcp_provider.env_overlay_keys(cfg))}"
        )


def test_playwright_never_enters_the_bench_server_list(wiring, monkeypatch):
    """.mcp.json declares playwright; a bench run must not launch it.

    Its tools would join the discovery set and shift the ``tool_defs[:limit]``
    cut small models run under, so a model's tool score would depend on how many
    browser tools sorted ahead of the trading ones.
    """
    _, mcp_provider = wiring
    actual = _bench_configs(mcp_provider, monkeypatch, AGENT_SLUG)
    names = {c["name"] for c in actual}
    assert not (names & set(mcp_provider.EXCLUDED_MCP_SERVERS)), (
        f"excluded MCP server(s) present in a bench run: "
        f"{sorted(names & set(mcp_provider.EXCLUDED_MCP_SERVERS))}"
    )


def test_agent_slug_reaches_the_condor_server(wiring, monkeypatch):
    """The regression that would misreport a harness bug as a model limitation."""
    _, mcp_provider = wiring
    scoped = _by_name(_bench_configs(mcp_provider, monkeypatch, AGENT_SLUG))["condor"]
    assert "--agent-slug" in scoped["args"], (
        "agent-scoped live cases lost --agent-slug. condor's memory/skill tools "
        "would read the chat's stores instead of agents/{slug}/, so a "
        "routine_builder case could never find its routine_cookbook skill — and "
        "the matrix would blame the model."
    )
    idx = scoped["args"].index("--agent-slug")
    assert scoped["args"][idx + 1] == AGENT_SLUG

    chat_scoped = _by_name(_bench_configs(mcp_provider, monkeypatch, None))["condor"]
    assert "--agent-slug" not in chat_scoped["args"], (
        "chat-scoped consults must stay chat-scoped — production consults do."
    )


def test_server_name_reaches_both_servers(wiring, monkeypatch):
    """The other arg condor-evals drops. Wrong instance = wrong everything."""
    _, mcp_provider = wiring
    for cfg in _bench_configs(mcp_provider, monkeypatch, AGENT_SLUG):
        args = cfg["args"]
        assert "--server-name" in args, (
            f"{cfg['name']} is missing --server-name; production passes it to both "
            "servers, and start_agent resolves the target instance through it."
        )
        assert args[args.index("--server-name") + 1] == SERVER


def test_unregistered_server_fails_closed(wiring, monkeypatch):
    """An unknown server name must raise, not yield a hummingbot-less config set.

    condor logs a warning and returns condor-only in that case. Benchmarking on
    that would score every hummingbot tool case as a model failure.
    """
    _, mcp_provider = wiring
    monkeypatch.setenv("BENCH_MODE", "live")
    monkeypatch.setenv("HUMMINGBOT_API_URL", f"http://{HOST}:{PORT}")

    with pytest.raises(mcp_provider.LiveWiringError, match="not registered"):
        mcp_provider.build_mcp_configs(
            "live", agent_slug=None, server_name="no_such_bench_server"
        )
