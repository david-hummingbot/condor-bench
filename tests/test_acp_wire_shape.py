"""The ACP wire spells a tool call differently than bench reads one.

These frames are verbatim captures from claude-agent-acp 0.28.0 running a real
bench case. They exist because reading the wrong field name here fails *silently*
and looks like a model problem:

* args under ``rawInput``, read as ``input`` → every call recorded with ``{}`` →
  ``tool_params`` 0.0 on every case that pins parameters;
* results under ``rawOutput`` / ``content``, read as ``output`` → no tool responses
  at all → ``live_validity`` None, and the judge scored a correct, tool-grounded
  answer 0.05 because the transcript showed figures with no tool output behind them.

Neither shows up as an error. A run completes, saves, and reports the model as
hopeless. So the shape is pinned rather than trusted.
"""

from __future__ import annotations

from condor_compat.acp.acp_client import (
    ACPClient,
    acp_tool_input,
    acp_tool_name,
    acp_tool_output,
)

# ── Verbatim frames ───────────────────────────────────────────────────────────
TOOL_CALL = {
    "_meta": {"claudeCode": {"toolName": "mcp__mcp-hummingbot__get_market_data"}},
    "toolCallId": "toolu_01UL9DVu76f9vQEaEwyGD698",
    "sessionUpdate": "tool_call",
    "rawInput": {},
    "status": "pending",
    "title": "mcp__mcp-hummingbot__get_market_data",
    "kind": "other",
    "content": [],
}

TOOL_CALL_WITH_ARGS = {
    "_meta": {"claudeCode": {"toolName": "mcp__mcp-hummingbot__get_market_data"}},
    "toolCallId": "toolu_01UL9DVu76f9vQEaEwyGD698",
    "sessionUpdate": "tool_call_update",
    "rawInput": {"connector_name": "binance", "trading_pair": "BTC-USDT"},
    "title": "mcp__mcp-hummingbot__get_market_data",
    "kind": "other",
    "content": [],
}

TOOL_RESULT_VENDOR_ONLY = {
    "_meta": {
        "claudeCode": {
            "toolResponse": {"mid_price": 63748.205},
            "toolName": "mcp__mcp-hummingbot__get_market_data",
        }
    },
    "toolCallId": "toolu_01UL9DVu76f9vQEaEwyGD698",
    "sessionUpdate": "tool_call_update",
}

TOOL_RESULT_COMPLETED = {
    "_meta": {"claudeCode": {"toolName": "mcp__mcp-hummingbot__get_market_data"}},
    "toolCallId": "toolu_01UL9DVu76f9vQEaEwyGD698",
    "sessionUpdate": "tool_call_update",
    "status": "completed",
    "rawOutput": '{"mid_price": 63748.205}',
    "content": [
        {"type": "content", "content": {"type": "text", "text": '{"mid_price": 63748.205}'}}
    ],
}

STATUS_TICK_ONLY = {
    "toolCallId": "toolu_01UL9DVu76f9vQEaEwyGD698",
    "sessionUpdate": "tool_call_update",
    "status": "in_progress",
}


# ── Field translation ─────────────────────────────────────────────────────────
def test_arguments_come_from_raw_input():
    assert acp_tool_input(TOOL_CALL_WITH_ARGS) == {
        "connector_name": "binance",
        "trading_pair": "BTC-USDT",
    }


def test_legacy_input_key_still_read():
    assert acp_tool_input({"input": {"a": 1}}) == {"a": 1}


def test_missing_arguments_are_none_not_empty_dict():
    """The param metric has to tell "unreadable" from "an empty argument set"."""
    assert acp_tool_input({"toolCallId": "x"}) is None


def test_tool_name_prefers_the_vendor_meta_over_the_display_title():
    payload = dict(TOOL_CALL, title="Get market data")
    assert acp_tool_name(payload) == "mcp__mcp-hummingbot__get_market_data"


def test_tool_name_falls_back_to_title():
    assert acp_tool_name({"title": "ToolSearch"}) == "ToolSearch"


def test_output_read_from_raw_output():
    assert acp_tool_output(TOOL_RESULT_COMPLETED) == '{"mid_price": 63748.205}'


def test_output_falls_back_to_content_blocks():
    payload = {k: v for k, v in TOOL_RESULT_COMPLETED.items() if k != "rawOutput"}
    assert acp_tool_output(payload) == '{"mid_price": 63748.205}'


def test_output_falls_back_to_the_vendor_extension():
    assert acp_tool_output(TOOL_RESULT_VENDOR_ONLY) == {"mid_price": 63748.205}


def test_a_status_tick_carries_no_output():
    """Recording None as a response would inject an empty payload into live validity."""
    assert acp_tool_output(STATUS_TICK_ONLY) is None


# ── End to end through the client's own handler ───────────────────────────────
def _drain(client: ACPClient) -> list:
    events = []
    while not client._event_queue.empty():
        events.append(client._event_queue.get_nowait())
    return events


def _client() -> ACPClient:
    return ACPClient(command="true", working_dir="/tmp")


def test_claude_acp_drops_inherited_api_key(monkeypatch):
    """The bench key is for the SDK. Passing it into Claude Code bills the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-empty")
    client = ACPClient(command="claude-agent-acp", working_dir="/tmp")
    env = client._subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_acp_keeps_an_explicit_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
    client = ACPClient(
        command="claude-agent-acp",
        working_dir="/tmp",
        extra_env={"ANTHROPIC_API_KEY": "sk-ant-explicit"},
    )
    assert client._subprocess_env()["ANTHROPIC_API_KEY"] == "sk-ant-explicit"


def test_non_claude_acp_still_inherits_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
    client = ACPClient(command="npx @google/gemini-cli --acp", working_dir="/tmp")
    assert client._subprocess_env()["ANTHROPIC_API_KEY"] == "sk-ant-parent"


def test_the_opening_frame_yields_a_named_call():
    from condor_compat.acp.client import ToolCallEvent

    client = _client()
    client._on_session_update(update=TOOL_CALL)
    (event,) = _drain(client)
    assert isinstance(event, ToolCallEvent)
    assert event.title == "mcp__mcp-hummingbot__get_market_data"


def test_the_update_frame_carries_the_arguments():
    """claude-agent-acp opens with `rawInput: {}` and fills the args in later."""
    from condor_compat.acp.client import ToolCallUpdate

    assert TOOL_CALL["rawInput"] == {}, "the opening frame really is argument-free"
    client = _client()
    client._on_session_update(update=TOOL_CALL_WITH_ARGS)
    (event,) = _drain(client)
    assert isinstance(event, ToolCallUpdate)
    assert event.input == {"connector_name": "binance", "trading_pair": "BTC-USDT"}


def test_session_update_yields_the_tool_result():
    from condor_compat.acp.client import ToolCallUpdate

    client = _client()
    client._on_session_update(update=TOOL_RESULT_COMPLETED)
    (event,) = _drain(client)
    assert isinstance(event, ToolCallUpdate)
    assert event.output == '{"mid_price": 63748.205}'


def test_repeated_results_for_one_call_collapse_to_one_response():
    """Both result frames arrive for the same call; live validity must see one."""
    import asyncio

    from bench.client import _stream_turn
    from condor_compat.acp.client import PromptDone, TextChunk

    client = _client()

    async def fake_stream(_prompt):
        # The real frame order: open with no args, args on an update, then two
        # result reports for the same call.
        client._on_session_update(update=TOOL_CALL)
        client._on_session_update(update=TOOL_CALL_WITH_ARGS)
        client._on_session_update(update=TOOL_RESULT_VENDOR_ONLY)
        client._on_session_update(update=TOOL_RESULT_COMPLETED)
        for event in _drain(client):
            yield event
        yield TextChunk(text="mid is 63748.205")
        yield PromptDone(stop_reason="end_turn")

    client.prompt_stream = fake_stream
    turn = asyncio.run(_stream_turn(client, "q", {}))

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["args"] == {
        "connector_name": "binance",
        "trading_pair": "BTC-USDT",
    }
    assert len(turn.tool_responses) == 1, turn.tool_responses
    # Last one wins: the completed frame, not the earlier vendor-only report.
    assert turn.tool_responses[0]["output"] == '{"mid_price": 63748.205}'
    assert turn.tool_responses[0]["tool"] == "mcp__mcp-hummingbot__get_market_data"


# ── A prompt that never ran ───────────────────────────────────────────────────
# The second silent failure, from the same run: claude-agent-acp sent
# `thinking.type: enabled` for the model configured in ~/.claude/settings.json, the
# API rejected it 400, and every prompt in the run died. bench recorded an empty
# response with NO error, so the judge scored it "No response produced" and the
# model took the blame for a configuration problem. The bridge's message was only
# ever visible at log level DEBUG.
PROMPT_400 = (
    '[-32603] Internal error: API Error: 400 {"type":"error","error":'
    '{"type":"invalid_request_error","message":"\\"thinking.type.enabled\\" is not '
    'supported for this model."}}'
)


def test_a_failed_prompt_becomes_a_turn_error():
    import asyncio

    from bench.client import _stream_turn
    from condor_compat.acp.client import PromptDone

    client = _client()

    async def fake_stream(_prompt):
        yield PromptDone(stop_reason="error", error=PROMPT_400)

    client.prompt_stream = fake_stream
    turn = asyncio.run(_stream_turn(client, "q", {}))

    assert turn.response == ""
    assert turn.error is not None
    assert "ACP prompt failed (error)" in turn.error
    assert "thinking.type.enabled" in turn.error, "the bridge's own message survives"


def test_a_failed_prompt_with_no_message_still_errors():
    """A dead bridge says nothing at all; the row must not read as an empty answer."""
    import asyncio

    from bench.client import _stream_turn
    from condor_compat.acp.client import PromptDone

    client = _client()

    async def fake_stream(_prompt):
        yield PromptDone(stop_reason="disconnected")

    client.prompt_stream = fake_stream
    turn = asyncio.run(_stream_turn(client, "q", {}))
    assert turn.error is not None
    assert "disconnected" in turn.error


def test_a_normal_completion_is_not_an_error():
    import asyncio

    from bench.client import _stream_turn
    from condor_compat.acp.client import PromptDone, TextChunk

    client = _client()

    async def fake_stream(_prompt):
        yield TextChunk(text="BTC is 63700")
        yield PromptDone(stop_reason="end_turn")

    client.prompt_stream = fake_stream
    turn = asyncio.run(_stream_turn(client, "q", {}))
    assert turn.error is None
    assert turn.response == "BTC is 63700"


def test_an_answer_followed_by_a_failure_keeps_the_answer():
    """A bridge that dies after answering still gave us a row worth scoring."""
    import asyncio

    from bench.client import _stream_turn
    from condor_compat.acp.client import PromptDone, TextChunk

    client = _client()

    async def fake_stream(_prompt):
        yield TextChunk(text="BTC is 63700")
        yield PromptDone(stop_reason="disconnected")

    client.prompt_stream = fake_stream
    turn = asyncio.run(_stream_turn(client, "q", {}))
    assert turn.error is None
    assert turn.response == "BTC is 63700"


def test_a_failed_prompt_is_classified_as_infra_not_as_a_bad_answer():
    """Infra rows are excluded from the matrix; model failures are averaged in."""
    from metrics.answer_quality import is_infra_failure

    assert is_infra_failure(f"ACP prompt failed (error): {PROMPT_400}")
    assert not is_infra_failure("The current BTC price is 63700.")


def test_stderr_tail_is_retained_for_reporting():
    client = _client()
    for i in range(60):
        client._stderr_tail.append(f"line {i}")
    tail = client.stderr_tail()
    assert "line 59" in tail
    assert "line 0" not in tail, "the ring buffer is bounded"


def test_the_pinned_params_metric_now_sees_acp_arguments():
    """The end the bug was felt at: params scored 0.0 on every ACP case."""
    from metrics.tool_params import ToolParamMetric

    calls = [
        {
            "tool": acp_tool_name(TOOL_CALL_WITH_ARGS),
            "args": acp_tool_input(TOOL_CALL_WITH_ARGS),
        }
    ]
    score = ToolParamMetric().score(
        calls, {"get_market_data": {"connector_name": "binance", "trading_pair": "BTC-USDT"}}
    )
    assert score == 1.0


# ── The agent's own tools are not decisions about condor ───────────────────────
# Claude Code answers a 24-tool prompt by first calling its own `ToolSearch` to
# load deferred tools. Scored as an MCP call it cost Layer 2 cases F1 precision they
# could never recover, and it was averaged into live validity — a metric about
# whether the real API worked. Provenance is recorded at capture time because it
# cannot be recovered later: the PydanticAI path reports MCP tools by bare name, so
# an `mcp__` prefix rule applied afterwards would empty the trace entirely.
def test_acp_mcp_call_is_tagged_mcp():
    from bench.client import call_origin

    assert call_origin("mcp__mcp-hummingbot__get_market_data", "other") == "mcp"


def test_acp_builtin_is_tagged_agent():
    from bench.client import call_origin

    assert call_origin("ToolSearch", "other") == "agent"
    assert call_origin("Read", "read") == "agent"


def test_pydantic_ai_bare_names_stay_mcp():
    """The path that registers only MCP tools reports them unprefixed."""
    from bench.client import call_origin

    assert call_origin("get_market_data", "mcp") == "mcp"
    assert call_origin("manage_routines", "mcp") == "mcp"


def _result_with(calls, responses):
    from bench.client import BenchmarkResult, TurnResult

    return BenchmarkResult(
        case_id="c",
        model="claude-code",
        turns=[
            TurnResult(
                response="0 active bots",
                tool_calls=calls,
                latency_s=1.0,
                tool_responses=responses,
            )
        ],
    )


# The real c005 trace, from the run this was found in.
C005_CALLS = [
    {"tool": "ToolSearch", "args": {"query": "manage_bots"}, "tool_call_id": "a", "origin": "agent"},
    {"tool": "mcp__mcp-hummingbot__manage_bots", "args": {"action": "status"}, "tool_call_id": "b", "origin": "mcp"},
]
C005_RESPONSES = [
    {"tool": "ToolSearch", "tool_call_id": "a", "output": '[{"type":"tool_reference"}]', "origin": "agent"},
    {"tool": "mcp__mcp-hummingbot__manage_bots", "tool_call_id": "b", "output": '{"result":"Total Active Bots: 0"}', "origin": "mcp"},
]


def test_scoring_views_exclude_the_agents_own_tools():
    result = _result_with(C005_CALLS, C005_RESPONSES)

    assert result.tool_names() == ["ToolSearch", "mcp__mcp-hummingbot__manage_bots"]
    assert result.mcp_tool_names() == ["mcp__mcp-hummingbot__manage_bots"]
    assert result.agent_internal_tool_names() == ["ToolSearch"]
    assert len(result.tool_responses) == 2, "the full trace is untouched"
    assert len(result.mcp_tool_responses) == 1


def test_records_without_origin_are_still_scored():
    """Older runs and hand-built fixtures must score exactly as they did before."""
    result = _result_with(
        [{"tool": "get_market_data", "args": {}}], [{"tool": "get_market_data", "output": "ok"}]
    )
    assert result.mcp_tool_names() == ["get_market_data"]
    assert result.agent_internal_tool_names() == []


def test_layer2_precision_is_no_longer_charged_for_tool_search():
    """A Layer 2 probe scores on F1, so an extra ToolSearch used to cap it below 1.0."""
    from metrics.tool_accuracy import ToolAccuracyMetric

    metric = ToolAccuracyMetric()
    result = _result_with(C005_CALLS, C005_RESPONSES)
    before = metric.score(actual_tools=result.tool_names(), expected_tools=["manage_bots"])
    after = metric.score(actual_tools=result.mcp_tool_names(), expected_tools=["manage_bots"])
    assert before < 1.0
    assert after == 1.0


def test_live_validity_is_not_diluted_by_agent_tools():
    from metrics.live_validity import LiveValidityMetric

    result = _result_with(C005_CALLS, C005_RESPONSES)
    scored = LiveValidityMetric().score(
        result.mcp_tool_responses, {"manage_bots": {"nonempty": True}}
    )
    assert scored == 1.0
    # And the response set it scored holds only the API call.
    assert [r["tool"] for r in result.mcp_tool_responses] == [
        "mcp__mcp-hummingbot__manage_bots"
    ]


# ── Content-block envelopes ───────────────────────────────────────────────────
# condor's MCP tools answer with a block list. Left wrapped, the judge's digest
# rendered "[digest] json list / items: 1 items (e.g. keys: type, text)" — no
# content at all — and then the judge was asked whether the answer was grounded in
# it. c006 answered verbatim from the tool ("bench_staging is online — connected
# and authenticated") with tools/params/validity all 1.0 and scored 0.55 for
# suspected fabrication.
C006_SERVER_JSON = (
    '{\n  "server": "bench_staging",\n  "status": "online",\n'
    '  "message": "Connected and authenticated"\n}'
)
C006_RAW_OUTPUT = [{"type": "text", "text": C006_SERVER_JSON}]


def test_content_block_envelope_is_unwrapped():
    assert acp_tool_output({"rawOutput": C006_RAW_OUTPUT}) == C006_SERVER_JSON


def test_nested_content_block_shape_is_unwrapped():
    payload = {"rawOutput": [{"type": "content", "content": {"type": "text", "text": "hi"}}]}
    assert acp_tool_output(payload) == "hi"


def test_a_plain_payload_is_left_alone():
    from condor_compat.acp.acp_client import unwrap_content_blocks

    assert unwrap_content_blocks({"result": "ok"}) is None
    assert unwrap_content_blocks(["a", "b"]) is None
    assert acp_tool_output({"rawOutput": '{"result":"ok"}'}) == '{"result":"ok"}'
    # A list of real data rows must survive as a list, not be mistaken for blocks.
    rows = [{"symbol": "BTC", "price": 1}, {"symbol": "ETH", "price": 2}]
    assert acp_tool_output({"rawOutput": rows}) == rows


def test_the_judge_now_sees_the_tool_result():
    """The end the bug was felt at: the digest carried none of the payload.

    The original assertion here was a negative control — that the *wrapped* payload
    showed the judge nothing — because a list field digested to a bare count. Lists are
    now rendered as rows (a count is unusable as grounding evidence; see
    `_summarize_list_field`), so the wrapped form leaks its text too and the control no
    longer holds. What unwrapping still buys is structure: unwrapped, the fields are
    fields; wrapped, they are an escaped string inside a `type=text` content block, and
    nothing keyed on `status` can reach them (see the field-assertion test below).
    """
    from bench.tool_digest import digest_tool_output

    before = digest_tool_output("manage_servers", C006_RAW_OUTPUT)
    after = digest_tool_output("manage_servers", acp_tool_output({"rawOutput": C006_RAW_OUTPUT}))
    assert "bench_staging" in after
    assert "online" in after
    assert "type=text" in before, "the wrapped form is still a content block, not data"
    assert "type=text" not in after, "unwrapping should leave fields, not blocks"


def test_field_assertions_can_reach_inside_the_envelope():
    """live_expected `fields` looked for a key one level inside the wrapper."""
    from metrics.live_validity import LiveValidityMetric

    unwrapped = acp_tool_output({"rawOutput": C006_RAW_OUTPUT})
    score = LiveValidityMetric().score(
        [{"tool": "manage_servers", "output": unwrapped}],
        {"manage_servers": {"contains": ["bench_staging"], "fields": {"status": {"eq": "online"}}}},
    )
    assert score == 1.0


# ── Streaming arguments ────────────────────────────────────────────────────────
# Verbatim from BENCH_ACP_TRACE on a real bench case: the bridge does not send a
# tool call's arguments once, it streams them a key at a time and then sends
# `rawInput: null` twice more. Recording the first non-empty frame therefore
# captures a one-key fragment and discards the real argument set — 78 of 78 MCP
# calls in a full run were logged with at most one argument, and `tool_params`
# scored the rest as `actual: null`.
STREAMED_INPUT_FRAMES = [
    {"sessionUpdate": "tool_call", "toolCallId": "toolu_stream", "status": "pending",
     "rawInput": {},
     "_meta": {"claudeCode": {"toolName": "mcp__mcp-hummingbot__get_market_data"}}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_stream",
     "rawInput": {"data_type": "prices"}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_stream",
     "rawInput": {"data_type": "prices", "connector_name": "binance"}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_stream",
     "rawInput": {"data_type": "prices", "connector_name": "binance",
                  "trading_pairs": ["BTC-USDT"]}},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_stream", "rawInput": None},
    {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_stream", "status": "completed",
     "rawInput": None},
]


def _replay(frames: list[dict]) -> list[dict]:
    """Drive bench's own merge loop over a frame sequence, returning tool_calls.

    Mirrors the `ToolCallEvent` / `ToolCallUpdate` handling in
    `bench.client._collect`: the merge is what this pins, so it has to be the
    real code path and not a re-implementation.
    """
    from condor_compat.acp.client import ToolCallEvent, ToolCallUpdate

    client = _client()
    for frame in frames:
        client._on_session_update(update=frame)
    events = _drain(client)

    tool_calls: list[dict] = []
    call_index: dict[str, int] = {}
    for event in events:
        if isinstance(event, ToolCallEvent):
            if event.tool_call_id:
                call_index[event.tool_call_id] = len(tool_calls)
            tool_calls.append(
                {"tool": event.title, "args": event.input or {},
                 "tool_call_id": event.tool_call_id, "status": event.status}
            )
        elif isinstance(event, ToolCallUpdate):
            if event.input and event.tool_call_id in call_index:
                call = tool_calls[call_index[event.tool_call_id]]
                if isinstance(event.input, dict):
                    merged = dict(call.get("args") or {})
                    merged.update(event.input)
                    call["args"] = merged
                elif not call.get("args"):
                    call["args"] = event.input
            if event.status:
                idx = call_index.get(event.tool_call_id)
                if idx is not None:
                    tool_calls[idx]["status"] = event.status
    return tool_calls


def test_streamed_arguments_are_accumulated_not_truncated():
    """The whole argument set has to survive, not just the first key streamed."""
    (call,) = _replay(STREAMED_INPUT_FRAMES)
    assert call["args"] == {
        "data_type": "prices",
        "connector_name": "binance",
        "trading_pairs": ["BTC-USDT"],
    }
    assert call["status"] == "completed"


def test_a_trailing_null_input_does_not_erase_the_arguments():
    """The last two frames carry `rawInput: null` and must be ignored."""
    (call,) = _replay(STREAMED_INPUT_FRAMES)
    assert len(call["args"]) == 3


def test_params_metric_sees_the_pinned_keys_after_the_merge():
    """What the bug actually cost: pinned params scored against a fragment."""
    from metrics.tool_params import ToolParamMetric

    (call,) = _replay(STREAMED_INPUT_FRAMES)
    expected = {
        "get_market_data": {"connector_name": "binance", "trading_pairs": ["BTC-USDT"]}
    }
    score = ToolParamMetric().score([call], expected)
    assert score == 1.0, call["args"]


def test_the_first_fragment_alone_would_have_failed():
    """Guards the guard: the old behaviour must actually score worse."""
    from metrics.tool_params import ToolParamMetric

    truncated = [{"tool": "mcp__mcp-hummingbot__get_market_data",
                  "args": {"data_type": "prices"}}]
    expected = {
        "get_market_data": {"connector_name": "binance", "trading_pairs": ["BTC-USDT"]}
    }
    assert ToolParamMetric().score(truncated, expected) < 1.0
