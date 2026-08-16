"""Provider model-list parsing has to survive more than OpenAI's shape.

The dashboard picker used to do ``m["id"] for m in data["data"]``. That 500s
the whole list when one row has no id, and it ignores Ollama's ``/api/tags``
(``models[].name``) and LM Studio's ``/api/v1/models`` (``models[].key``) —
the endpoints that actually enumerate downloaded models rather than the ones
currently loaded.
"""

from dashboard.backend.app import _fallback_model_urls, _model_ids_from_payload, _openai_compat_models_url


def test_parse_openai_compat_list():
    payload = {"data": [{"id": "b"}, {"id": "a"}, {"object": "model"}]}
    assert _model_ids_from_payload(payload) == ["a", "b"]


def test_parse_ollama_tags():
    payload = {
        "models": [
            {"name": "llama3.1:8b", "size": 1},
            {"name": "qwen2.5:14b"},
        ]
    }
    assert _model_ids_from_payload(payload) == ["llama3.1:8b", "qwen2.5:14b"]


def test_parse_lmstudio_v1():
    payload = {
        "models": [
            {"key": "google/gemma-4-12b", "type": "llm"},
            {"key": "qwen/qwen3.5-9b", "type": "llm"},
        ]
    }
    assert _model_ids_from_payload(payload) == ["google/gemma-4-12b", "qwen/qwen3.5-9b"]


def test_parse_unions_data_and_models_keys():
    payload = {
        "data": [{"id": "loaded-only"}],
        "models": [{"name": "also-downloaded"}],
    }
    assert _model_ids_from_payload(payload) == ["also-downloaded", "loaded-only"]


def test_parse_rejects_junk():
    assert _model_ids_from_payload(None) == []
    assert _model_ids_from_payload([]) == []
    assert _model_ids_from_payload({"data": "nope"}) == []


def test_openai_compat_url_does_not_double_v1():
    assert _openai_compat_models_url("http://localhost:1234") == "http://localhost:1234/v1/models"
    assert _openai_compat_models_url("http://localhost:1234/v1") == "http://localhost:1234/v1/models"
    assert (
        _openai_compat_models_url("https://openrouter.ai/api/v1")
        == "https://openrouter.ai/api/v1/models"
    )


def test_fallback_urls_ollama_and_lmstudio_only():
    assert _fallback_model_urls("http://localhost:11434", "ollama") == [
        "http://localhost:11434/api/tags"
    ]
    assert _fallback_model_urls("http://localhost:1234", "lmstudio") == [
        "http://localhost:1234/api/v1/models",
        "http://localhost:1234/api/v0/models",
    ]
    assert _fallback_model_urls("https://openrouter.ai/api/v1", "openrouter") == []
    assert _fallback_model_urls("https://api.groq.com/openai/v1", "groq") == []
