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
        # Per prompt, as the bridge reports it: cumulative for the *session*, so
        # the second figure is a fresh session's own count.
        self.usage = [
            UsageEvent(input_tokens=10, output_tokens=5),
            UsageEvent(input_tokens=15, output_tokens=7),
        ]
        self.session = 1
        # Which session each prompt was sent in.
        self.prompt_sessions: list[int] = []
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

    async def new_session(self) -> None:
        self.session += 1

    async def prompt_stream(self, text: str):
        self.prompts.append(text)
        self.prompt_sessions.append(self.session)
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


def test_acp_judge_uses_a_fresh_session_per_judgement_and_records_usage(monkeypatch):
    """One bridge process, one session per verdict.

    A shared session put every earlier case's transcript and score in the context
    of the next verdict — anchoring the grade and growing the prompt with every
    case. The bridge is reused (startup is the expensive part); the session is not.
    """
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
    # "default" is the bridge's "don't override" sentinel, not a model id, so it
    # must NOT reach ANTHROPIC_MODEL. Forwarding it makes the real CLI answer
    # "There's an issue with the selected model" to every prompt with no error on
    # the turn — which, for a judge, scores that sentence as the verdict.
    assert "ANTHROPIC_MODEL" not in (fake.kwargs.get("extra_env") or {})
    assert len(fake.prompts) == 2
    assert fake.prompt_sessions == [1, 2], "the second verdict reused the first's session"
    assert all("JSON object only" in p for p in fake.prompts)
    assert all("Do not call tools" in p for p in fake.prompts)

    # Each session's own usage, summed — the per-session baseline resets, so the
    # second session's 15/7 is not read as a delta against the first's 10/5.
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


def test_close_judge_stops_the_module_level_bridge(monkeypatch):
    """The scorer's judge outlived the CLI's event loop: its bridge subprocess was
    still open when `asyncio.run` returned, and exit-time cleanup printed
    "Event loop is closed"."""
    import bench.scorer as scorer
    from metrics.answer_quality import AnswerQualityMetric

    monkeypatch.setattr("condor_compat.acp.acp_client.ACPClient", FakeACP)
    metric = AnswerQualityMetric()
    metric._judge = ClaudeJudge(model="default", backend="acp")
    monkeypatch.setattr(scorer, "_quality_metric", metric)

    async def _run():
        await metric._judge.a_generate("score this")
        await scorer.close_judge()

    asyncio.run(_run())
    assert FakeACP.instances[0].stopped
    assert metric._judge._acp is None


def test_the_cli_closes_the_judge_inside_its_event_loop(monkeypatch):
    """`runner._run_cases` is what every CLI scoring path `asyncio.run`s; it has to
    close the judge before that loop ends, even when the trials raise."""
    import bench.scorer
    import bench.trials
    import runner

    closed: list[bool] = []

    async def _close() -> None:
        closed.append(True)

    async def _boom(*a, **k):
        raise RuntimeError("trial runner exploded")

    monkeypatch.setattr(bench.scorer, "close_judge", _close)
    monkeypatch.setattr(bench.trials, "run_cases", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        asyncio.run(runner._run_cases([], "m", None, 1))
    assert closed == [True]
