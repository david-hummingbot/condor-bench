"""An OPENAI_BASE_URL-only model must keep the null-content-safe wrapper.

The gemma4-26b-qat run against a tunnelled Ollama lost 4 cases to
``status_code: 400 ... invalid message content type: <nil>``. The dashboard
points a "custom" provider at its server by normalising the key to ``openai:``
and exporting OPENAI_BASE_URL -- it never passes base_url. ``_build_model``
only honoured an explicit base_url for the ``openai:`` prefix, so the model
dropped through to ``infer_model()``: still the right endpoint, but a stock
OpenAIModel, which serializes a tool-call-only assistant turn as
``content: null`` and Ollama 400s the follow-up request.
"""

import asyncio
import os

import pytest

from condor_compat.acp.pydantic_ai_client import PydanticAIClient


def _client(model: str) -> PydanticAIClient:
    return PydanticAIClient(model=model, mcp_servers=[])


def _is_null_safe(model) -> bool:
    return type(model).__name__ == "_NullContentSafeOpenAIModel"


def test_openai_prefix_uses_env_base_url_and_stays_null_safe(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ollama.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "tunnel-key")

    model = _client("openai:gemma4-26b-qat:latest")._build_model()

    assert _is_null_safe(model)
    assert str(model.client.base_url).rstrip("/") == "https://ollama.example/v1"
    assert model.client.api_key == "tunnel-key"


def test_explicit_base_url_still_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ignored.example/v1")

    client = _client("openai:gemma4-26b-qat:latest")
    client.base_url = "https://explicit.example/v1"
    model = client._build_model()

    assert _is_null_safe(model)
    assert str(model.client.base_url).rstrip("/") == "https://explicit.example/v1"


def test_plain_openai_without_base_url_is_untouched(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    model = _client("openai:gpt-4o")._build_model()

    assert not _is_null_safe(model)


@pytest.mark.parametrize("role", ["assistant", "tool", "user"])
def test_null_content_is_coerced_for_every_role(monkeypatch, role):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ollama.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    model = _client("openai:gemma4-26b-qat:latest")._build_model()

    async def fake_super(*_a, **_kw):
        return [{"role": role, "content": None}]

    monkeypatch.setattr(
        type(model).__bases__[0], "_map_messages", staticmethod(fake_super), raising=True
    )
    mapped = asyncio.run(model._map_messages([], None))

    assert mapped[0]["content"] == ""
