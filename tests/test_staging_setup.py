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
    assert "HUMMINGBOT_API_URL=http://staging.example:8000" in text
    assert result.get("staging_sync", {}).get("ok") is True, result.get("staging_sync")
    assert result["bench_identity"]["server_name"] == BENCH_SERVER_NAME
    # Neither BENCH_CHAT_ID nor BENCH_USER_ID is an internal identity constant any
    # more, for the same reason: the constants are synthetic (999001), and a
    # synthetic identity cannot do the job. A synthetic chat id made
    # `send_notification` answer "chat not found"; a synthetic *user* id against a
    # real chat id made Condor answer `403 chat_id is not a chat you belong to` on
    # every delegate call — and a save used to delete the operator's correct
    # override and reinstate that 403. An unrelated save must leave both alone.
    assert "BENCH_CHAT_ID=1" in text, "an unrelated save cleared the operator's chat id"
    assert "BENCH_USER_ID=1" in text, "an unrelated save cleared the operator's user id"
    assert all(f["key"] != "BENCH_SERVER_NAME" for f in result["fields"])
    assert any(f["key"] == "BENCH_CHAT_ID" for f in result["fields"])
    assert any(f["key"] == "BENCH_USER_ID" for f in result["fields"])


def test_the_telegram_chat_id_is_editable_and_validated(monkeypatch, tmp_path):
    """`send_notification` could never succeed, and nothing let you fix it.

    `BENCH_CHAT_ID` defaulted to 999001 — a synthetic bench id, not a Telegram chat — so
    every notification answered "Bad Request: chat not found", and four cases could not
    earn live_validity for a reason no bot configuration addressed. It was in the
    settings store's *internal* list, meaning the save path deliberately stripped it, and
    the dashboard offered no field.
    """
    import bench.settings_store as store

    # It must be offered to the UI...
    keys = {f["key"] for f in store.SETTINGS_FIELDS}
    assert "BENCH_CHAT_ID" in keys, "the field is not exposed to the Settings page"
    # ...and no longer wiped on save.
    assert "BENCH_CHAT_ID" not in store._INTERNAL_STAGING_KEYS

    env = tmp_path / ".env"
    env.write_text("HUMMINGBOT_API_URL=http://localhost:8000\n")
    monkeypatch.setattr(store, "ENV_PATH", env)
    monkeypatch.delenv("BENCH_CHAT_ID", raising=False)

    # A real id (group chats are negative) survives the round trip.
    store.update_settings({"BENCH_CHAT_ID": "-1001234567890"})
    assert "BENCH_CHAT_ID=-1001234567890" in env.read_text()

    # A typo is rejected at save time, not as a ValueError from an unrelated run.
    for bad in ("my-chat", "12 34", "abc"):
        try:
            store.update_settings({"BENCH_CHAT_ID": bad})
        except store.SettingsError:
            pass
        else:
            raise AssertionError(f"accepted a non-numeric chat id: {bad!r}")


def test_a_hand_edited_chat_id_does_not_break_every_run():
    """`staging_config` is called by every run and by the pre-flight.

    `int()` on a stale or hand-edited value used to raise from a place that gave no hint
    where the bad value came from. The form validates; this is the backstop.
    """
    from config import BENCH_CHAT_ID, _int_or

    assert _int_or("12345", BENCH_CHAT_ID) == 12345
    assert _int_or("-1001234", BENCH_CHAT_ID) == -1001234
    assert _int_or("not-a-number", BENCH_CHAT_ID) == BENCH_CHAT_ID
    assert _int_or("", BENCH_CHAT_ID) == BENCH_CHAT_ID
    assert _int_or(None, BENCH_CHAT_ID) == BENCH_CHAT_ID


# ── .env writing: one key, one line ───────────────────────────────────────────


def _store(tmp_path, monkeypatch, text: str):
    from bench import settings_store

    env_path = tmp_path / ".env"
    env_path.write_text(text)
    monkeypatch.setattr(settings_store, "ENV_PATH", env_path)
    return settings_store, env_path


def _count(text: str, key: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith(f"{key}="))


def test_a_save_does_not_duplicate_unknown_keys(tmp_path, monkeypatch):
    """The growth bug: every save appended every unknown key it had parsed.

    They were already emitted by the pass-through, so each save added another
    copy. A real .env reached four copies of BENCH_MODE, and nothing failed
    loudly — dotenv takes the last one, so the file just grew.
    """
    store, env_path = _store(
        tmp_path,
        monkeypatch,
        "BENCH_MODE=live\nBENCH_ALLOW_MUTATING=false\nCONDOR_PATH=/tmp/condor\n",
    )
    for _ in range(3):
        store.update_settings({"BENCH_BASELINE_MODEL": "anthropic:claude-sonnet-5"})
    text = env_path.read_text()
    assert _count(text, "BENCH_MODE") == 1, text
    assert _count(text, "BENCH_ALLOW_MUTATING") == 1, text
    assert _count(text, "BENCH_BASELINE_MODEL") == 1, text


def test_existing_duplicates_are_collapsed_on_save(tmp_path, monkeypatch):
    """Self-healing: a file that already grew gets tidied by the next save."""
    store, env_path = _store(
        tmp_path,
        monkeypatch,
        "BENCH_MODE=live\nCONDOR_PATH=/a\nBENCH_MODE=live\nBENCH_MODE=live\n",
    )
    store.update_settings({"BENCH_BASELINE_MODEL": "x"})
    assert _count(env_path.read_text(), "BENCH_MODE") == 1


def test_the_surviving_line_keeps_the_effective_value(tmp_path, monkeypatch):
    """dotenv takes the last occurrence, so collapsing must keep the last value.

    Keeping the first would silently change configuration while tidying it.
    """
    store, env_path = _store(tmp_path, monkeypatch, "BENCH_MODE=stale\nBENCH_MODE=live\n")
    store.update_settings({"BENCH_BASELINE_MODEL": "x"})
    text = env_path.read_text()
    assert "BENCH_MODE=live" in text
    assert "BENCH_MODE=stale" not in text


def test_a_single_unknown_key_keeps_its_line_verbatim(tmp_path, monkeypatch):
    """Only duplicated keys get normalised; everything else is left as written."""
    store, env_path = _store(
        tmp_path, monkeypatch, '# a comment\nWEIRD_KEY="spaced value"\n'
    )
    store.update_settings({"BENCH_BASELINE_MODEL": "x"})
    text = env_path.read_text()
    assert "# a comment" in text
    assert '"spaced value"' in text


def test_a_real_chat_id_mirrors_into_the_user_id(tmp_path, monkeypatch):
    """The 403 configuration cannot be produced by filling in one field."""
    store, env_path = _store(tmp_path, monkeypatch, "")
    store.update_settings({"BENCH_CHAT_ID": "1883786161"})
    text = env_path.read_text()
    assert "BENCH_CHAT_ID=1883786161" in text
    assert "BENCH_USER_ID=1883786161" in text, "a real chat id needs a matching user id"


def test_an_explicit_user_id_is_not_overwritten(tmp_path, monkeypatch):
    """A group chat legitimately has chat_id != user_id."""
    store, env_path = _store(tmp_path, monkeypatch, "")
    store.update_settings({"BENCH_CHAT_ID": "-100123", "BENCH_USER_ID": "555"})
    text = env_path.read_text()
    assert "BENCH_CHAT_ID=-100123" in text
    assert "BENCH_USER_ID=555" in text


def test_a_non_numeric_user_id_is_rejected(tmp_path, monkeypatch):
    import pytest

    store, _ = _store(tmp_path, monkeypatch, "")
    with pytest.raises(store.SettingsError):
        store.update_settings({"BENCH_USER_ID": "me"})
