"""Live MCP spawn args must stay identical to what condor launches in production.

Same philosophy as ``test_tool_surface_drift.py``, one layer down: that file keeps
the *mocks* honest about which tools exist, this one keeps *live mode* honest
about how the real servers are started.

Three things are checked, and all three matter:

1. **bench == condor.** ``bench/mcp_provider.build_mcp_configs("live", …)`` must
   produce the same ``name``/``command``/``args`` as condor's own
   ``build_mcp_servers_for_session()``. bench loads that function rather than
   copying it, so this mostly guards the *call site* — a new required parameter on
   the condor side would change production's output while bench keeps calling the
   old signature.

2. **Both == a pinned shape.** The expected arg list is spelled out literally
   below. Comparing bench against condor alone can't catch a condor-side change,
   because bench would follow it silently. The pin is what turns "condor changed
   its MCP wiring" into a failing test someone has to look at. It already earned
   its keep once: condor deleted ``build_mcp_servers_for_agent()`` and folded it
   into the session builder, and this file is where that surfaced.

3. **No secret rides on argv.** condor moved the API credentials and the bot token
   off the command line into the subprocess ``env`` (SEC-095) — argv is
   world-readable through ``ps``. Values are never pinned here (they differ per
   machine, and pinning a token would put it in the repo); what is pinned is
   *placement*: credentials in ``env``, coordinates in ``args``.

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
# Deliberately unlike every other identifier here: the argv leak check scans for
# these values as substrings, and a username that appeared inside the server name
# would make it report a leak that isn't one.
USERNAME = "probe-user-xyzzy"
PASSWORD = "probe-pass-plugh"
AGENT_SLUG = "market_making_expert"


def _strip_bot_id(args: list[str]) -> list[str]:
    """Drop ``--bot-id <digest>``, which is machine-dependent by construction.

    The digest is derived from whatever TELEGRAM_TOKEN the environment holds, so
    pinning its value would make this test pass or fail based on the developer's
    ``.env``. Its *presence* is asserted separately.
    """
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--bot-id":
            skip = True
            continue
        out.append(arg)
    return out


def _expected_hummingbot_args() -> list[str]:
    """Pinned mcp-hummingbot spawn args, ``--bot-id`` removed.

    ``--server-name`` is the one condor-evals omits. It selects which
    hummingbot-api instance the tools bind to and is what ``start_agent``
    resolves against; without it the server falls back to HUMMINGBOT_API_URL and
    then to localhost:8000.

    Note what is *not* here: ``--username`` and ``--password``. They used to sit
    on argv and moved into ``env`` under SEC-095. If they reappear in this list,
    the fix regressed.
    """
    return [
        "run",
        "python",
        "-m",
        "mcp_servers.hummingbot_api",
        "--url",
        f"http://{HOST}:{PORT}",
        "--server-name",
        SERVER,
    ]


def _expected_condor_args(agent_slug: str | None) -> list[str]:
    """Pinned condor MCP spawn args, ``--bot-id`` removed.

    ``--agent-slug`` is present only for agent-scoped runs and scopes the
    memory/skill tools to ``agents/{slug}/``. Chat-scoped consults omit it, which
    is what production does for a consult session — so its absence there is
    correct, not a gap.

    ``--bot-token`` used to be here in clear text; SEC-095 replaced it with the
    non-secret ``--bot-id`` digest and moved the token into ``env``.
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


@pytest.fixture(scope="module", autouse=True)
def _report_checkout(condor_repo):
    """Name the checkout under test, so a failure here can be read correctly.

    A mismatch caused by pointing at a stale clone produces the same red as real
    upstream drift; the fix for one is CONDOR_PATH and for the other is
    re-vendoring, so the message has to say which checkout it compared against.
    """
    from config import condor_checkout_label

    print(f"\nMCP wiring drift compared against: {condor_checkout_label()}")


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


def _production_configs(shared, agent_slug: str | None) -> list[dict]:
    """What condor itself builds for these inputs."""
    return shared.build_mcp_servers_for_session(
        user_id=USER_ID, chat_id=CHAT_ID, server_name=SERVER, agent_slug=agent_slug
    )


# ── Check 2: production output matches the pinned shape ────────────────────────
@pytest.mark.parametrize("agent_slug", [AGENT_SLUG, None])
def test_condor_helpers_match_pinned_spawn_args(wiring, agent_slug):
    shared, _ = wiring
    produced = _production_configs(shared, agent_slug)

    by_name = _by_name(produced)
    assert set(by_name) == {"mcp-hummingbot", "condor"}, (
        f"condor now builds a different set of MCP servers: {sorted(by_name)}. "
        "Review bench/mcp_provider.py — live runs would get a tool surface the "
        "pinned expectations no longer describe."
    )

    assert _strip_bot_id(by_name["mcp-hummingbot"]["args"]) == _expected_hummingbot_args(), (
        "condor's mcp-hummingbot spawn args changed. Update the pin in this test "
        "only after confirming bench passes whatever the new args need."
    )
    assert _strip_bot_id(by_name["condor"]["args"]) == _expected_condor_args(agent_slug), (
        "condor's condor-MCP spawn args changed. Update the pin in this test only "
        "after confirming bench passes whatever the new args need."
    )
    for cfg in produced:
        assert cfg["command"] == "uv", (
            f"{cfg['name']} is no longer launched with `uv` ({cfg['command']!r}) — "
            "bench sets cwd to the condor repo for exactly that launcher."
        )


def test_agent_builder_removal_is_handled(wiring):
    """condor folded build_mcp_servers_for_agent() into the session builder.

    Pinned so that if it comes back — or the survivor is renamed — bench is
    updated deliberately rather than silently falling back to a private copy of
    the spawn args.
    """
    shared, _ = wiring
    assert hasattr(shared, "build_mcp_servers_for_session")
    assert not hasattr(shared, "build_mcp_servers_for_agent"), (
        "condor re-introduced build_mcp_servers_for_agent(). Decide which builder "
        "bench should call for agent-scoped cases and update bench/mcp_provider.py."
    )


# ── Check 1: bench reproduces production output ────────────────────────────────
@pytest.mark.parametrize("agent_slug", [AGENT_SLUG, None])
def test_bench_live_configs_match_condor(wiring, monkeypatch, agent_slug):
    shared, mcp_provider = wiring
    expected = _production_configs(shared, agent_slug)
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
    shared, mcp_provider = wiring
    actual = _bench_configs(mcp_provider, monkeypatch, AGENT_SLUG)
    produced = _by_name(_production_configs(shared, AGENT_SLUG))

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
        # condor populates env itself now (credentials, bot token). bench may add
        # exactly one key on top; anything more is bench diverging silently.
        added = mcp_provider.env_overlay_keys(cfg) - mcp_provider.env_overlay_keys(
            produced[cfg["name"]]
        )
        assert added <= {"HUMMINGBOT_API_URL"}, (
            f"{cfg['name']} env overlay grew beyond the documented URL pin: "
            f"{sorted(added)}"
        )
        # And bench must not drop what condor put there — without the credentials
        # the subprocess would fall back to admin/admin.
        dropped = mcp_provider.env_overlay_keys(produced[cfg["name"]]) - (
            mcp_provider.env_overlay_keys(cfg)
        )
        assert not dropped, (
            f"{cfg['name']} lost env entries condor set: {sorted(dropped)}"
        )


@pytest.mark.parametrize("agent_slug", [AGENT_SLUG, None])
def test_no_secret_rides_on_argv(wiring, monkeypatch, agent_slug):
    """SEC-095: credentials travel in env, never on the command line.

    argv is world-readable through ``ps``, so an API password or a bot token there
    is readable by every local user. condor moved them into the subprocess ``env``;
    this pins that so a future refactor can't quietly put them back — and so bench,
    which launches the same subprocesses, can't reintroduce them on its own side.
    """
    _, mcp_provider = wiring
    configs = _by_name(_bench_configs(mcp_provider, monkeypatch, agent_slug))

    secrets = {PASSWORD, USERNAME, os.environ.get("TELEGRAM_TOKEN", "")} - {""}
    for name, cfg in configs.items():
        argv = " ".join(str(a) for a in cfg["args"])
        for flag in ("--password", "--username", "--bot-token"):
            assert flag not in cfg["args"], (
                f"{name} passes {flag} on argv — SEC-095 moved credentials into env "
                "because argv is readable via `ps`."
            )
        for secret in secrets:
            assert secret not in argv, (
                f"{name} leaks a credential value on argv ({secret[:4]}…). Secrets "
                "belong in the config's env entries."
            )

    # The credentials must actually be somewhere, or the subprocess falls back to
    # admin/admin against whatever answers on the resolved URL.
    hb_env = mcp_provider.env_overlay_keys(configs["mcp-hummingbot"])
    assert {"HUMMINGBOT_API_USERNAME", "HUMMINGBOT_API_PASSWORD"} <= hb_env, (
        "mcp-hummingbot got no credentials in env — condor changed how they are "
        f"delivered (env keys: {sorted(hb_env)}). Update bench to match."
    )


def test_bot_id_digest_replaces_the_raw_token(wiring, monkeypatch):
    """``--bot-id`` is a non-secret digest; the token itself stays in env."""
    _, mcp_provider = wiring
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        pytest.skip("no TELEGRAM_TOKEN in this environment — nothing to digest")

    configs = _by_name(_bench_configs(mcp_provider, monkeypatch, AGENT_SLUG))
    for name, cfg in configs.items():
        assert "--bot-id" in cfg["args"], (
            f"{name} lost --bot-id. condor's startup reaper seeds on that digest to "
            "find subprocess trees orphaned by a crash."
        )
        digest = cfg["args"][cfg["args"].index("--bot-id") + 1]
        assert token not in digest, "--bot-id is carrying the raw token, not a digest"


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
