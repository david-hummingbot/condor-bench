"""Judge transport switch: Anthropic API vs Claude Code ACP."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from condor_compat.acp.client import PromptDone, TextChunk, UsageEvent
from metrics.judge import (
    JUDGE_USAGE,
    ClaudeJudge,
    acp_judge_model_key,
    judge_acp_workdir,
)


class FakeACP:
    instances: list[FakeACP] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.prompts: list[str] = []
        self.started = False
        self.stopped = False
        self._alive = False
        self.replies = ['{"score": 0.8, "reason": "grounded"}']
        self.usage = [
            UsageEvent(input_tokens=10, output_tokens=5),
            UsageEvent(input_tokens=25, output_tokens=12),
        ]
        self.done = PromptDone(stop_reason="end_turn")
        FakeACP.instances.append(self)

    @property
    def alive(self) -> bool:
        return self._alive and not self.stopped

    async def start(self) -> None:
        self.started = True
        self._alive = True

    async def stop(self) -> None:
        self.stopped = True
        self._alive = False

    def stderr_tail(self) -> str:
        return ""

    async def prompt_stream(self, text: str):
        self.prompts.append(text)
        i = len(self.prompts) - 1
        yield TextChunk(self.replies[min(i, len(self.replies) - 1)])
        if i < len(self.usage):
            yield self.usage[i]
        yield self.done


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeACP.instances = []
    yield
    FakeACP.instances = []


def test_judge_backend_defaults_to_api(monkeypatch):
    monkeypatch.delenv("BENCH_JUDGE_BACKEND", raising=False)
    from config import judge_backend

    assert judge_backend() == "api"


def test_judge_backend_accepts_acp_aliases(monkeypatch):
    from config import judge_backend

    for raw in ("acp", "ACP", "claude-code", "claude-acp"):
        monkeypatch.setenv("BENCH_JUDGE_BACKEND", raw)
        assert judge_backend() == "acp", raw


def test_judge_ready_api_needs_key(monkeypatch):
    from config import judge_ready

    monkeypatch.setenv("BENCH_JUDGE_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert judge_ready() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert judge_ready() is True


def test_judge_ready_acp_does_not_need_key(monkeypatch):
    from config import judge_ready

    monkeypatch.setenv("BENCH_JUDGE_BACKEND", "acp")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert judge_ready() is True


def test_acp_judge_model_key_shapes():
    assert acp_judge_model_key("") == "claude-code"
    assert acp_judge_model_key("claude-code") == "claude-code"
    assert acp_judge_model_key("claude-code:sonnet") == "claude-code:sonnet"
    assert acp_judge_model_key("sonnet") == "claude-code:sonnet"
    assert acp_judge_model_key("opus[1m]") == "claude-code:opus[1m]"
    assert acp_judge_model_key("claude-sonnet-5") == "claude-code:sonnet"
    assert acp_judge_model_key("claude-opus-5") == "claude-code:opus"
    assert acp_judge_model_key("claude-haiku-4-5") == "claude-code:haiku"


def test_judge_acp_workdir_is_not_the_condor_checkout():
    path = judge_acp_workdir()
    assert path.name == ".judge-acp"
    assert not (path / ".mcp.json").exists()


def test_settings_schema_exposes_the_switch():
    from bench.settings_store import SETTINGS_FIELDS

    field = next(f for f in SETTINGS_FIELDS if f["key"] == "BENCH_JUDGE_BACKEND")
    values = [c["value"] if isinstance(c, dict) else c for c in field["choices"]]
    assert values == ["api", "acp"]
    assert field["default"] == "api"


def test_acp_judge_reuses_one_session_and_records_usage_deltas(monkeypatch):
    monkeypatch.setattr("condor_compat.acp.acp_client.ACPClient", FakeACP)
    judge = ClaudeJudge(model="default", backend="acp")
    before = JUDGE_USAGE.snapshot()

    async def _run():
        first = await judge.a_generate("score this")
        second = await judge.a_generate("score that")
        await judge.aclose()
        return first, second

    first, second = asyncio.run(_run())
    assert first == '{"score": 0.8, "reason": "grounded"}'
    assert second == first
    assert len(FakeACP.instances) == 1
    fake = FakeACP.instances[0]
    assert fake.started and fake.stopped
    assert fake.kwargs["mcp_servers"] == []
    assert Path(fake.kwargs["working_dir"]).name == ".judge-acp"
    assert fake.kwargs.get("extra_env", {}).get("ANTHROPIC_MODEL") == "default"
    assert len(fake.prompts) == 2
    assert all("JSON object only" in p for p in fake.prompts)
    assert all("Do not call tools" in p for p in fake.prompts)

    delta = JUDGE_USAGE.delta_since(before)
    assert delta["input_tokens"] == 25
    assert delta["output_tokens"] == 12
    assert delta["calls"] == 2


def test_acp_judge_surfaces_a_bridge_error(monkeypatch):
    monkeypatch.setattr("condor_compat.acp.acp_client.ACPClient", FakeACP)

    async def _run():
        judge = ClaudeJudge(backend="acp")
        await judge._ensure_acp()
        FakeACP.instances[0].done = PromptDone(
            stop_reason="error", error="credit balance too low"
        )
        with pytest.raises(RuntimeError, match="ACP judge failed"):
            await judge.a_generate("x")
        await judge.aclose()

    asyncio.run(_run())


def test_empty_acp_reply_raises(monkeypatch):
    monkeypatch.setattr("condor_compat.acp.acp_client.ACPClient", FakeACP)

    async def _run():
        judge = ClaudeJudge(backend="acp")
        await judge._ensure_acp()
        FakeACP.instances[0].replies = [""]
        with pytest.raises(ValueError, match="no text block"):
            await judge.a_generate("x")
        await judge.aclose()

    asyncio.run(_run())
