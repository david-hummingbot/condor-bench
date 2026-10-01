"""BENCH_FORM_STATE round-trips through .env and survives a Settings save."""

import pytest

from bench.form_state import (
    FORM_STATE_KEY,
    FormStateError,
    get_form_state,
    update_form_state,
)


def _env(tmp_path, monkeypatch, text=""):
    from bench import settings_store

    path = tmp_path / ".env"
    path.write_text(text)
    monkeypatch.setattr(settings_store, "ENV_PATH", path)
    return path


def test_round_trip_keeps_keys_urls_and_prompt_text(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch, "ANTHROPIC_API_KEY=sk-ant-test\n")
    saved = update_form_state(
        {
            "providers": {
                "custom": {
                    "enabled": True,
                    "apiKey": "sk-live",
                    "baseUrl": "https://example.com/v1",
                    "selectedModel": "qwen/qwen3.6-27b",
                }
            },
            "prompt": {
                "question": "don't drop the apostrophe\nor the line break",
                "turns": ["follow up"],
                "expectedTools": "get_market_data, manage_executors",
                "agentSlug": "market_making_expert",
            },
        }
    )
    assert saved["providers"]["custom"]["apiKey"] == "sk-live"
    text = path.read_text()
    assert text.startswith("ANTHROPIC_API_KEY=sk-ant-test\n")
    form_line = next(line for line in text.splitlines() if line.startswith(f"{FORM_STATE_KEY}="))
    assert "\\n" in form_line

    again = get_form_state()
    assert again["providers"]["custom"]["baseUrl"] == "https://example.com/v1"
    assert again["prompt"]["question"] == "don't drop the apostrophe\nor the line break"
    assert again["prompt"]["turns"] == ["follow up"]
    assert again["benchmark"]["layers"] == []


def test_saving_one_page_leaves_the_other_page_alone(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    update_form_state(
        {
            "providers": {
                "openai": {
                    "enabled": True,
                    "apiKey": "sk-openai",
                    "baseUrl": "",
                    "selectedModel": "gpt-4o",
                }
            },
            "prompt": {
                "question": "keep me",
                "turns": [],
                "expectedTools": "",
                "agentSlug": "",
            },
        }
    )
    update_form_state(
        {
            "benchmark": {
                "layers": ["tool"],
                "domain": "tool:market_data",
                "category": "",
                "riskLevels": ["read_only"],
                "coreOnly": True,
            }
        }
    )
    state = get_form_state()
    assert state["prompt"]["question"] == "keep me"
    assert state["providers"]["openai"]["apiKey"] == "sk-openai"
    assert state["benchmark"]["layers"] == ["tool"]
    assert state["benchmark"]["coreOnly"] is True


def test_clearing_a_provider_forgets_it(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    update_form_state(
        {
            "providers": {
                "custom": {
                    "enabled": True,
                    "apiKey": "sk-live",
                    "baseUrl": "https://example.com/v1",
                    "selectedModel": "m",
                }
            }
        }
    )
    update_form_state({"providers": {}})
    state = get_form_state()
    assert state["providers"] == {}
    assert FORM_STATE_KEY not in (tmp_path / ".env").read_text()


def test_clearing_the_last_field_removes_the_env_line(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch, "OPENAI_API_KEY=sk-old\n")
    update_form_state(
        {
            "benchmark": {
                "layers": ["consult"],
                "domain": "",
                "category": "",
                "riskLevels": [],
                "coreOnly": False,
            }
        }
    )
    assert FORM_STATE_KEY in path.read_text()
    update_form_state(
        {
            "benchmark": {
                "layers": [],
                "domain": "",
                "category": "",
                "riskLevels": [],
                "coreOnly": False,
            }
        }
    )
    text = path.read_text()
    assert "OPENAI_API_KEY=sk-old" in text
    assert FORM_STATE_KEY not in text


def test_settings_save_keeps_the_form_line_verbatim(tmp_path, monkeypatch):
    from bench import settings_store

    path = _env(tmp_path, monkeypatch, "ANTHROPIC_API_KEY=sk-ant-test\n")
    update_form_state(
        {
            "prompt": {
                "question": "it's quoted",
                "turns": [],
                "expectedTools": "",
                "agentSlug": "",
            }
        }
    )
    before = next(line for line in path.read_text().splitlines() if line.startswith(FORM_STATE_KEY))
    settings_store.update_settings({"BENCH_BASELINE_MODEL": "anthropic:claude-sonnet-5"})
    after = path.read_text().splitlines()
    assert before in after
    assert sum(1 for line in after if line.startswith(f"{FORM_STATE_KEY}=")) == 1
    assert get_form_state()["prompt"]["question"] == "it's quoted"


def test_corrupt_value_is_reported_and_not_rewritten(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch, f"{FORM_STATE_KEY}=not-json\n")
    state = get_form_state()
    assert state["providers"] == {}
    assert "not valid JSON" in state["warning"]
    assert path.read_text() == f"{FORM_STATE_KEY}=not-json\n"


def test_rejects_unknown_fields_and_bad_layers(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    with pytest.raises(FormStateError):
        update_form_state({"benchmark": {"layers": ["nope"], "domain": "", "category": "", "riskLevels": [], "coreOnly": False}})
    with pytest.raises(FormStateError):
        update_form_state({"nope": {}})
    assert get_form_state()["providers"] == {}
    assert FORM_STATE_KEY not in (tmp_path / ".env").read_text()
