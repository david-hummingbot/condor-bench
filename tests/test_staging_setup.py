"""Auto-registration of the fixed bench_staging Condor server entry."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config import BENCH_CHAT_ID, BENCH_SERVER_NAME, BENCH_USER_ID, ROOT


def _real_condor() -> Path | None:
    sibling = (ROOT.parent / "condor").resolve()
    if (sibling / "config_manager.py").is_file():
        return sibling
    return None


@pytest.fixture
def condor_repo(tmp_path, monkeypatch):
    """Temp config.yml; import ConfigManager from the real sibling checkout."""
    real = _real_condor()
    if real is None:
        pytest.skip("no condor checkout with config_manager.py available")

    repo = tmp_path / "condor"
    repo.mkdir()
    (repo / "mcp_servers").mkdir()
    (repo / "handlers" / "agents").mkdir(parents=True)
    (repo / "handlers" / "agents" / "_shared.py").write_text(
        "def build_mcp_servers_for_session(**kwargs):\n    return []\n"
    )

    config = {
        "servers": {},
        "default_server": None,
        "admin_id": 1,
        "users": {
            1: {
                "user_id": 1,
                "role": "admin",
                "created_at": 0,
            }
        },
        "server_access": {},
        "chat_defaults": {},
        "version": 1,
    }
    (repo / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setenv("CONDOR_PATH", str(repo))
    monkeypatch.setenv("HUMMINGBOT_API_URL", "http://staging.example:8000")
    monkeypatch.setenv("HUMMINGBOT_USERNAME", "bench")
    monkeypatch.setenv("HUMMINGBOT_PASSWORD", "secret")
    monkeypatch.delenv("BENCH_SERVER_NAME", raising=False)
    monkeypatch.delenv("BENCH_CHAT_ID", raising=False)
    monkeypatch.delenv("BENCH_USER_ID", raising=False)
    monkeypatch.syspath_prepend(str(real))

    from config_manager import ConfigManager

    ConfigManager._instance = None
    return repo


def test_ensure_bench_server_registers_entry_acl_and_chat_default(condor_repo):
    from config_manager import ConfigManager
    from bench.staging_setup import ensure_bench_server

    ConfigManager._instance = None
    result = ensure_bench_server()
    assert result.ok, result.detail
    assert result.server_name == BENCH_SERVER_NAME
    assert result.host == "staging.example"
    assert result.port == 8000

    cm = ConfigManager.instance(str(condor_repo / "config.yml"))
    entry = cm.get_server(BENCH_SERVER_NAME)
    assert entry is not None
    assert entry["host"] == "staging.example"
    assert entry["port"] == 8000
    assert entry["username"] == "bench"
    assert cm.has_server_access(BENCH_USER_ID, BENCH_SERVER_NAME)
    assert cm.get_chat_default_server(BENCH_CHAT_ID) == BENCH_SERVER_NAME
    user = cm.get_user(BENCH_USER_ID)
    assert user is not None
    assert user["role"] == "user"


def test_ensure_bench_server_updates_existing(condor_repo, monkeypatch):
    from config_manager import ConfigManager
    from bench.staging_setup import ensure_bench_server

    ConfigManager._instance = None
    assert ensure_bench_server().ok

    monkeypatch.setenv("HUMMINGBOT_API_URL", "http://other.example:9000")
    monkeypatch.setenv("HUMMINGBOT_USERNAME", "bench2")
    monkeypatch.setenv("HUMMINGBOT_PASSWORD", "secret2")
    result = ensure_bench_server()
    assert result.ok, result.detail

    cm = ConfigManager.instance(str(condor_repo / "config.yml"))
    entry = cm.get_server(BENCH_SERVER_NAME)
    assert entry["host"] == "other.example"
    assert entry["port"] == 9000
    assert entry["username"] == "bench2"


def test_update_settings_clears_identity_overrides_and_syncs(tmp_path, monkeypatch, condor_repo):
    from config_manager import ConfigManager
    from bench import settings_store

    ConfigManager._instance = None
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HUMMINGBOT_API_URL=http://old:8000\n"
        "BENCH_SERVER_NAME=local\n"
        "BENCH_CHAT_ID=1\n"
        "BENCH_USER_ID=1\n"
    )
    monkeypatch.setattr(settings_store, "ENV_PATH", env_path)
    monkeypatch.setenv("HUMMINGBOT_API_URL", "http://old:8000")
    monkeypatch.setenv("BENCH_SERVER_NAME", "local")

    result = settings_store.update_settings(
        {
            "HUMMINGBOT_API_URL": "http://staging.example:8000",
            "HUMMINGBOT_USERNAME": "bench",
            "HUMMINGBOT_PASSWORD": "secret",
        }
    )
    text = env_path.read_text()
    assert "BENCH_SERVER_NAME" not in text
    assert "BENCH_CHAT_ID" not in text
    assert "BENCH_USER_ID" not in text
    assert "HUMMINGBOT_API_URL=http://staging.example:8000" in text
    assert result.get("staging_sync", {}).get("ok") is True, result.get("staging_sync")
    assert result["bench_identity"]["server_name"] == BENCH_SERVER_NAME
    assert all(
        f["key"] not in {"BENCH_SERVER_NAME", "BENCH_CHAT_ID", "BENCH_USER_ID"}
        for f in result["fields"]
    )
