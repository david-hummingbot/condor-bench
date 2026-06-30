"""JSON-RPC 2.0 peer — vendored from condor/condor/acp/jsonrpc.py (unchanged)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JSONRPCPeer:
    def __init__(self):
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, method: str, handler: Callable) -> None:
        self._handlers[method] = handler

    async def send_request(self, method: str, params: dict[str, Any], writer: asyncio.StreamWriter) -> Any:
        req_id = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
        writer.write((json.dumps(msg) + "\n").encode())
        await writer.drain()
        log.debug("-> %s (id=%d)", method, req_id)
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        return await future

    async def send_notification(self, method: str, params: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        writer.write((json.dumps(msg) + "\n").encode())
        await writer.drain()

    async def handle_line(self, line: str, writer: asyncio.StreamWriter) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if "result" in data or "error" in data:
            req_id = data.get("id")
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in data:
                    err = data["error"]
                    future.set_exception(JSONRPCError(err.get("code", -1), err.get("message", ""), err.get("data")))
                else:
                    future.set_result(data.get("result"))
            return
        method = data.get("method")
        params = data.get("params", {})
        msg_id = data.get("id")
        handler = self._handlers.get(method)
        if handler is None:
            if msg_id is not None:
                resp = {"jsonrpc": "2.0", "error": {"code": METHOD_NOT_FOUND, "message": f"Method not found: {method}"}, "id": msg_id}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
            return
        try:
            result = handler(**params) if not asyncio.iscoroutinefunction(handler) else await handler(**params)
        except Exception as e:
            if msg_id is not None:
                resp = {"jsonrpc": "2.0", "error": {"code": INTERNAL_ERROR, "message": str(e)}, "id": msg_id}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
            return
        if msg_id is not None:
            resp = {"jsonrpc": "2.0", "result": result, "id": msg_id}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()

    def cancel_all(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
