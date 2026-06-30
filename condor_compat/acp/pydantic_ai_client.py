"""Pydantic AI agent client — uses pydantic-ai with MCP tool servers.

Vendored from condor/condor/acp/pydantic_ai_client.py.
Source: https://github.com/hummingbot/condor

Drop-in alternative to ACPClient for open-source / local models.
Supports any model backend that pydantic-ai supports: ollama, openai-compatible
(LM Studio), groq, anthropic, etc.

Yields the same ACPEvent types so TickEngine can consume it identically.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .client import (
    ACPEvent,
    Heartbeat,
    PermissionCallback,
    PromptDone,
    TextChunk,
    ToolCallEvent,
    ToolCallUpdate,
)

log = logging.getLogger(__name__)


def _infer_tool_filter_mode(model_name: str) -> str:
    import re

    model_lower = model_name.lower()
    if any(
        provider in model_lower
        for provider in ["openai:", "anthropic:", "groq:", "google:", "openrouter:"]
    ):
        return "full"

    size_match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![a-z])", model_lower)
    if size_match:
        size = float(size_match.group(1))
        if size <= 8.0:
            return "essential"
        elif size <= 32.0:
            return "moderate"
        else:
            return "full"

    if any(name in model_lower for name in ["gemma", "phi", "tiny", "mini", "small"]):
        return "essential"
    if any(name in model_lower for name in ["deepseek", "mixtral", "command-r", "gpt"]):
        return "full"
    return "moderate"


PYDANTIC_AI_PREFIXES = frozenset(
    {"ollama", "openai", "groq", "anthropic", "google", "lmstudio", "openrouter"}
)

DEFAULT_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

_SERVER_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _get_server_semaphore(base_url: str) -> asyncio.Semaphore:
    if base_url not in _SERVER_SEMAPHORES:
        _SERVER_SEMAPHORES[base_url] = asyncio.Semaphore(1)
    return _SERVER_SEMAPHORES[base_url]


def is_pydantic_ai_model(agent_key: str) -> bool:
    prefix = agent_key.split(":", 1)[0] if ":" in agent_key else ""
    return prefix in PYDANTIC_AI_PREFIXES


def resolve_base_url(model_name: str, base_url: str | None = None) -> str | None:
    if base_url:
        return base_url
    prefix = model_name.split(":", 1)[0]
    return DEFAULT_BASE_URLS.get(prefix)


async def healthcheck_local_backend(
    model_name: str, base_url: str | None = None
) -> str | None:
    import httpx

    prefix, _, model_id = model_name.partition(":")
    is_local = prefix in ("ollama", "lmstudio") or (prefix == "openai" and base_url)
    if not is_local:
        return None

    url = resolve_base_url(model_name, base_url)
    if not url:
        return None

    models_url = f"{url.rstrip('/')}/models"
    try:
        timeout = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(models_url)
    except Exception as e:
        return (
            f"the model backend at {url} is unreachable ({type(e).__name__}) — "
            f"is the {prefix} server running?"
        )

    if resp.status_code != 200:
        return f"the model backend at {url} returned HTTP {resp.status_code}."

    if model_id:
        try:
            ids = {
                m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)
            }
        except Exception:
            ids = set()
        ids = {i for i in ids if isinstance(i, str) and i}
        loaded = any(i == model_id or i.startswith(f"{model_id}:") for i in ids)
        if ids and not loaded:
            available = ", ".join(sorted(ids)) or "(none)"
            return (
                f"model '{model_id}' is not loaded on the {prefix} backend at {url}. "
                f"Available: {available}."
            )

    return None


_NULL_SAFE_MODEL_CLS: Any = None


def _make_openai_compat_model(model_id: str, provider: Any) -> Any:
    global _NULL_SAFE_MODEL_CLS
    if _NULL_SAFE_MODEL_CLS is None:
        from pydantic_ai.models.openai import OpenAIModel

        class _NullContentSafeOpenAIModel(OpenAIModel):
            async def _map_messages(self, *args: Any, **kwargs: Any) -> Any:
                mapped = await super()._map_messages(*args, **kwargs)
                for msg in mapped:
                    if (
                        isinstance(msg, dict)
                        and msg.get("role") == "assistant"
                        and msg.get("content") is None
                    ):
                        msg["content"] = ""
                return mapped

        _NULL_SAFE_MODEL_CLS = _NullContentSafeOpenAIModel

    return _NULL_SAFE_MODEL_CLS(model_id, provider=provider)


def _tool_args_to_dict(args: Any) -> dict | None:
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class PydanticAIClient:
    """Manages a pydantic-ai agent with MCP tool servers.

    Mirrors ACPClient's interface: start() → prompt_stream() → stop().

    Model resolution:
      - "ollama:llama3.1"  → uses ollama provider (localhost:11434)
      - "openai:gpt-4o"    → uses OpenAI API
      - "openai:my-model"  → with base_url, uses any OpenAI-compatible API
      - "groq:llama-3.3-70b-versatile" → uses Groq cloud
      - "anthropic:claude-sonnet-4-6" → uses Anthropic API
      - "openrouter:anthropic/claude-sonnet-4-5" → uses OpenRouter
    """

    def __init__(
        self,
        model: str,
        mcp_servers: list[dict[str, Any]] | None = None,
        permission_callback: PermissionCallback | None = None,
        extra_env: dict[str, str] | None = None,
        base_url: str | None = None,
        tool_filter_mode: str | None = None,
        allowed_tools: list[str] | None = None,
    ):
        self.model_name = model
        self.mcp_server_configs = mcp_servers or []
        self.permission_callback = permission_callback
        self.extra_env = extra_env
        self.base_url = base_url
        self.allowed_tools = set(allowed_tools) if allowed_tools else None
        self.tool_filter_mode = tool_filter_mode or _infer_tool_filter_mode(model)
        self._mcp_servers: list[Any] = []
        self._agent: Any = None
        self._request_semaphore: asyncio.Semaphore | None = None
        self._mcp_task: asyncio.Task | None = None
        self._ready_event: asyncio.Event | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._startup_error: BaseException | None = None
        self._message_history: list = []

    def _build_model(self) -> Any:
        import httpx
        from openai import AsyncOpenAI
        from pydantic_ai.providers.openai import OpenAIProvider

        prefix, _, model_id = self.model_name.partition(":")
        base_url = self.base_url

        _read_timeout = float(os.environ.get("LOCAL_MODEL_READ_TIMEOUT", "600"))
        _local_timeout = httpx.Timeout(
            connect=30.0, read=_read_timeout, write=30.0, pool=30.0
        )

        if prefix == "openrouter":
            if not model_id:
                raise RuntimeError(
                    "OpenRouter requires an explicit model id, e.g. "
                    "'openrouter:openai/gpt-4o' or 'openrouter:anthropic/claude-sonnet-4-5'."
                )
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY is not set. Add it to your .env to use openrouter:* models."
                )
            openai_client = AsyncOpenAI(
                base_url=base_url or DEFAULT_BASE_URLS["openrouter"],
                api_key=api_key,
                timeout=_local_timeout,
            )
            return _make_openai_compat_model(
                model_id, OpenAIProvider(openai_client=openai_client)
            )

        if prefix in DEFAULT_BASE_URLS:
            base_url = base_url or DEFAULT_BASE_URLS[prefix]
            if not model_id:
                model_id = self._resolve_default_local_model(
                    prefix=prefix, base_url=base_url
                )
            openai_client = AsyncOpenAI(
                base_url=base_url,
                api_key="not-needed",
                timeout=_local_timeout,
            )
            return _make_openai_compat_model(
                model_id, OpenAIProvider(openai_client=openai_client)
            )

        if prefix == "openai" and base_url:
            openai_client = AsyncOpenAI(
                base_url=base_url,
                api_key="not-needed",
                timeout=_local_timeout,
            )
            return _make_openai_compat_model(
                model_id, OpenAIProvider(openai_client=openai_client)
            )

        from pydantic_ai.models import infer_model
        return infer_model(self.model_name)

    def _resolve_default_local_model(self, prefix: str, base_url: str) -> str:
        env_override = os.environ.get("CONDOR_DEFAULT_LOCAL_MODEL") or os.environ.get(
            "OLLAMA_MODEL"
        )
        if env_override:
            return env_override

        model_id = self._fetch_openai_compatible_model(base_url)
        if model_id:
            return model_id

        if prefix == "ollama":
            model_id = self._fetch_ollama_native_model(base_url)
            if model_id:
                return model_id

        raise RuntimeError(
            f"No local model found for '{prefix}'. "
            f"Use an explicit key like '{prefix}:<model-name>' or set CONDOR_DEFAULT_LOCAL_MODEL."
        )

    def _fetch_openai_compatible_model(self, base_url: str) -> str | None:
        url = f"{base_url.rstrip('/')}/models"
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=2) as resp:
                if resp.status != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                model_id = first.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    return model_id.strip()
        return None

    def _fetch_ollama_native_model(self, base_url: str) -> str | None:
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return None
        native_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
        try:
            req = Request(native_url, method="GET")
            with urlopen(req, timeout=2) as resp:
                if resp.status != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        models = payload.get("models")
        if isinstance(models, list) and models:
            first = models[0]
            if isinstance(first, dict):
                name = first.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None

    async def start(self) -> None:
        from pydantic_ai import Agent
        from pydantic_ai.mcp import MCPServerStdio

        toolsets = []
        for srv_config in self.mcp_server_configs:
            command = srv_config["command"]
            args = srv_config.get("args", [])

            env = dict(self.extra_env or {})
            for env_entry in srv_config.get("env", []):
                if isinstance(env_entry, dict):
                    env[env_entry["name"]] = env_entry["value"]

            mcp_server = MCPServerStdio(
                command,
                args=args,
                env=env if env else None,
                timeout=30,
            )
            toolsets.append(mcp_server)
            self._mcp_servers.append(mcp_server)

        model = self._build_model()
        prepare = self._prepare_tools if self.allowed_tools else None
        self._agent = Agent(model, toolsets=toolsets, prepare_tools=prepare)

        resolved_base_url = resolve_base_url(self.model_name, self.base_url)
        self._request_semaphore = (
            _get_server_semaphore(resolved_base_url)
            if resolved_base_url is not None
            else None
        )

        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._startup_error = None
        self._mcp_task = asyncio.create_task(self._run_mcp_lifecycle())

        await self._ready_event.wait()
        if self._startup_error is not None:
            self._mcp_task = None
            self._mcp_servers.clear()
            self._agent = None
            raise self._startup_error

    async def _prepare_tools(self, ctx: Any, tool_defs: list) -> list:
        allowed = self.allowed_tools or set()

        def _ok(name: str) -> bool:
            return name in allowed or name.rsplit("__", 1)[-1] in allowed

        return [td for td in tool_defs if _ok(td.name)]

    async def _run_mcp_lifecycle(self) -> None:
        try:
            async with self._agent.run_mcp_servers():
                self._ready_event.set()
                await self._shutdown_event.wait()
        except BaseException as exc:
            if not self._ready_event.is_set():
                self._startup_error = exc
                self._ready_event.set()

    async def stop(self) -> None:
        if self._mcp_task is not None:
            self._shutdown_event.set()
            try:
                await asyncio.wait_for(self._mcp_task, timeout=10)
            except Exception:
                log.exception("Error stopping MCP server task")
                self._mcp_task.cancel()
            self._mcp_task = None
        self._mcp_servers.clear()
        self._agent = None

    @property
    def alive(self) -> bool:
        return self._agent is not None

    @contextlib.asynccontextmanager
    async def _release_request_slot(self) -> AsyncIterator[None]:
        sem = self._request_semaphore
        if sem is None:
            yield
            return
        sem.release()
        try:
            yield
        finally:
            await sem.acquire()

    async def prompt(self, text: str) -> str:
        chunks: list[str] = []
        async for event in self.prompt_stream(text):
            if isinstance(event, TextChunk):
                chunks.append(event.text)
        return "".join(chunks)

    async def prompt_stream(self, text: str) -> AsyncIterator[ACPEvent]:
        assert self._agent is not None, "Client not started"

        async with self._request_semaphore or contextlib.nullcontext():
            start_time = time.monotonic()

            try:
                from pydantic_ai.agent import CallToolsNode, ModelRequestNode
                from pydantic_ai.messages import TextPart, ToolCallPart, ToolReturnPart
                from pydantic_graph import End

                async with self._agent.iter(
                    text, message_history=self._message_history
                ) as run:
                    async for node in run:
                        if isinstance(node, End):
                            if hasattr(node, "data") and node.data:
                                result_data = node.data
                                if hasattr(result_data, "data"):
                                    yield TextChunk(text=str(result_data.data))
                            break

                        if isinstance(node, ModelRequestNode):
                            elapsed = time.monotonic() - start_time
                            yield Heartbeat(elapsed_seconds=elapsed)
                            if hasattr(node, "request") and node.request:
                                for part in node.request.parts:
                                    if isinstance(part, ToolReturnPart):
                                        content = part.content
                                        output_str = (
                                            content
                                            if isinstance(content, str)
                                            else str(content)
                                        )
                                        yield ToolCallUpdate(
                                            tool_call_id=part.tool_call_id or "",
                                            status="completed",
                                            output=output_str,
                                        )

                        elif isinstance(node, CallToolsNode):
                            for part in node.model_response.parts:
                                if isinstance(part, TextPart) and part.content:
                                    yield TextChunk(text=part.content)

                                elif isinstance(part, ToolCallPart):
                                    tool_id = part.tool_call_id or uuid.uuid4().hex[:12]
                                    tool_name = part.tool_name

                                    if self.permission_callback:
                                        tool_call_info = {
                                            "tool": tool_name,
                                            "title": tool_name,
                                            "input": _tool_args_to_dict(part.args) or {},
                                        }
                                        options = [
                                            {"optionId": "allow", "kind": "allow_once"},
                                            {"optionId": "deny", "kind": "deny"},
                                        ]
                                        async with self._release_request_slot():
                                            result = await self.permission_callback(
                                                tool_call_info, options
                                            )
                                        outcome = result.get("outcome", {})
                                        if (
                                            isinstance(outcome, dict)
                                            and outcome.get("outcome") == "cancelled"
                                        ):
                                            yield ToolCallEvent(
                                                tool_call_id=tool_id,
                                                title=tool_name,
                                                status="blocked",
                                                kind="mcp",
                                                input=_tool_args_to_dict(part.args),
                                            )
                                            continue

                                    yield ToolCallEvent(
                                        tool_call_id=tool_id,
                                        title=tool_name,
                                        status="in_progress",
                                        kind="mcp",
                                        input=_tool_args_to_dict(part.args),
                                    )
                                    yield ToolCallUpdate(
                                        tool_call_id=tool_id,
                                        status="completed",
                                    )

                    if run.result is not None:
                        self._message_history.extend(run.result.new_messages())

                yield PromptDone(stop_reason="end_turn")

            except asyncio.TimeoutError:
                yield PromptDone(stop_reason="timeout")
            except Exception as e:
                log.exception("PydanticAI prompt error: %s", e)
                yield TextChunk(text=self._format_error(e))
                yield PromptDone(stop_reason="error")

    def _format_error(self, e: Exception) -> str:
        try:
            from pydantic_ai.exceptions import ModelHTTPError
        except ImportError:
            return f"(error: {e})"

        if not isinstance(e, ModelHTTPError):
            return f"(error: {e})"

        is_openrouter = self.model_name.startswith("openrouter:")
        status = getattr(e, "status_code", None)

        if is_openrouter and status == 402:
            return "OpenRouter rejected the request: insufficient credits."
        if is_openrouter and status == 401:
            return "OpenRouter rejected the API key (401). Check OPENROUTER_API_KEY."
        if is_openrouter and status == 429:
            return "OpenRouter rate-limited the request (429). Wait and retry."
        if is_openrouter and status and 500 <= status < 600:
            return f"OpenRouter upstream error ({status}). Try again or switch models."
        return f"(error: {e})"
