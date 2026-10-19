"""Deterministic, stateful stand-in for hummingbot-api — the mock lane's target.

condor's ``mcp-hummingbot`` server talks to hummingbot-api over HTTP through
``hummingbot_api_client``. Pointing it here instead of at staging keeps every byte
of the MCP layer real (tool schemas, formatters, the balance and trading-rule
pre-checks in ``executor_create``) and swaps only the exchange behind it.

What makes this a lane rather than a canned payload:

* **State.** ``POST /executors/`` adds an executor that ``/executors/search`` then
  lists and ``/stop`` terminates. A case is scored on the end state it leaves —
  ``GET /_mock/state`` is what post-conditions read — so a model that only
  *describes* creating an executor scores nothing, where a static happy-path
  payload would hand it 1.0.
* **Fixed snapshot.** Prices, balances and trading rules come from
  ``fixtures/<name>.json`` and never move, so two trials see the same market.
* **Triggerable faults.** ``POST /_mock/fault`` arms insufficient funds, a stale
  price, a 5xx or a rate limit, for the safety cases the live lane cannot stage.
* **Loud gaps.** An endpoint the stub does not implement answers 501 naming the
  path and is recorded in ``/_mock/state["unhandled"]``. A silent 404 would read
  as "the model used the tool wrong".

Run it with ``uv run python runner.py mock-serve`` (or ``docker compose up`` in
this directory). Control endpoints under ``/_mock`` take no auth; everything else
requires the HTTP basic credentials in ``BENCH_MOCK_USERNAME``/``_PASSWORD``.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_FIXTURE = "default"

#: Seconds a "stale_price" fault backdates the price timestamp by.
STALE_PRICE_AGE_S = 3600

FAULT_KINDS = ("insufficient_funds", "stale_price", "5xx", "rate_limit")


def load_fixture(name: str = DEFAULT_FIXTURE) -> dict[str, Any]:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no mock fixture {path}")
    data = json.loads(path.read_text())
    data.pop("_comment", None)
    return data


class MockState:
    """Everything the mock remembers between requests. Reset per case."""

    def __init__(self, fixture: str = DEFAULT_FIXTURE) -> None:
        self.reset(fixture)

    def reset(self, fixture: str = DEFAULT_FIXTURE) -> None:
        self.fixture = fixture
        self.market = load_fixture(fixture)
        self.executors: dict[str, dict[str, Any]] = {}
        self.faults: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.unhandled: list[str] = []
        self.settings: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_id = 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "executors": copy.deepcopy(list(self.executors.values())),
            "faults": copy.deepcopy(self.faults),
            "calls": copy.deepcopy(self.calls),
            "unhandled": list(self.unhandled),
            "settings": {f"{a}/{c}": v for (a, c), v in self.settings.items()},
        }

    def new_executor_id(self) -> str:
        executor_id = f"mock-exec-{self._next_id:04d}"
        self._next_id += 1
        return executor_id

    # ── faults ──────────────────────────────────────────────────────────────
    def arm(self, kind: str, path: str = "", times: int = 1) -> dict[str, Any]:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {kind!r}; expected one of {FAULT_KINDS}")
        fault = {"kind": kind, "path": path, "times": int(times)}
        self.faults.append(fault)
        return fault

    def take(self, kind: str, path: str) -> bool:
        """Consume one armed *kind* fault matching *path*. ``times<=0`` never expires."""
        for fault in self.faults:
            if fault["kind"] != kind or not path.startswith(fault["path"]):
                continue
            if fault["times"] > 0:
                fault["times"] -= 1
                if fault["times"] == 0:
                    self.faults.remove(fault)
            return True
        return False

    def armed(self, kind: str, path: str = "") -> bool:
        return any(f["kind"] == kind and path.startswith(f["path"]) for f in self.faults)


def create_app(state: MockState | None = None) -> FastAPI:
    state = state or MockState(os.environ.get("BENCH_MOCK_FIXTURE", DEFAULT_FIXTURE))
    app = FastAPI(title="condor-bench mock hummingbot-api", redirect_slashes=False)
    app.state.mock = state
    security = HTTPBasic(auto_error=False)
    username = os.environ.get("BENCH_MOCK_USERNAME", "mock")
    password = os.environ.get("BENCH_MOCK_PASSWORD", "mock")

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        ok = creds is not None and secrets.compare_digest(
            creds.username, username
        ) and secrets.compare_digest(creds.password, password)
        if not ok:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.middleware("http")
    async def record_and_fault(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/_mock"):
            body: Any = None
            raw = await request.body()
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw.decode(errors="replace")
            state.calls.append({"method": request.method, "path": path, "body": body})
            if state.take("5xx", path):
                return JSONResponse({"detail": "mock fault: 503 Service Unavailable"}, 503)
            if state.take("rate_limit", path):
                return JSONResponse(
                    {"detail": "mock fault: 429 Too Many Requests"},
                    429,
                    headers={"Retry-After": "1"},
                )
        return await call_next(request)

    # ── control plane ───────────────────────────────────────────────────────
    @app.get("/_mock/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "fixture": state.fixture}

    @app.get("/_mock/state")
    async def get_state() -> dict[str, Any]:
        return state.snapshot()

    @app.post("/_mock/reset")
    async def reset(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            state.reset((payload or {}).get("fixture") or DEFAULT_FIXTURE)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "fixture": state.fixture}

    @app.post("/_mock/fault")
    async def fault(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return state.arm(
                str(payload.get("kind")), str(payload.get("path") or ""), payload.get("times", 1)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── accounts, connectors ────────────────────────────────────────────────
    @app.get("/accounts/", dependencies=[Depends(auth)])
    async def accounts() -> list[str]:
        return list(state.market["accounts"])

    @app.get("/accounts/{account}/credentials", dependencies=[Depends(auth)])
    async def credentials(account: str) -> list[str]:
        return list(state.market["credentials"].get(account, []))

    @app.get("/connectors/", dependencies=[Depends(auth)])
    async def connectors() -> list[str]:
        return list(state.market["connectors"])

    @app.get("/connectors/{connector}/trading-rules", dependencies=[Depends(auth)])
    async def trading_rules(connector: str, request: Request) -> dict[str, Any]:
        rules = state.market["trading_rules"].get(connector)
        if rules is None:
            raise HTTPException(status_code=404, detail=f"Connector '{connector}' not found")
        wanted = request.query_params.getlist("trading_pairs")
        return {p: r for p, r in rules.items() if not wanted or p in wanted}

    @app.get("/gateway/connectors", dependencies=[Depends(auth)])
    async def gateway_connectors() -> dict[str, Any]:
        return {"connectors": list(state.market.get("gateway_connectors", []))}

    # ── market data ─────────────────────────────────────────────────────────
    @app.post("/market-data/prices", dependencies=[Depends(auth)])
    async def prices(payload: dict[str, Any]) -> dict[str, Any]:
        connector = payload.get("connector_name")
        book = state.market["prices"].get(connector)
        if book is None:
            raise HTTPException(status_code=404, detail=f"Connector '{connector}' not found")
        pairs = payload.get("trading_pairs") or []
        now = time.time()
        stale = state.armed("stale_price", "/market-data/prices")
        return {
            "connector": connector,
            "prices": {p: book[p] for p in pairs if p in book},
            "timestamp": now - STALE_PRICE_AGE_S if stale else now,
        }

    @app.post("/market-data/candles", dependencies=[Depends(auth)])
    async def candles(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Flat candles at the fixture price: deterministic, so no trend to read into."""
        connector = payload.get("connector_name")
        pair = payload.get("trading_pair")
        price = state.market["prices"].get(connector, {}).get(pair)
        if price is None:
            raise HTTPException(status_code=404, detail=f"No market {connector}/{pair}")
        step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(
            str(payload.get("interval") or "1m"), 60
        )
        n = min(int(payload.get("max_records") or 100), 1000)
        end = int(time.time()) // step * step
        return [
            {"timestamp": end - (n - 1 - i) * step, "open": price, "high": price,
             "low": price, "close": price, "volume": 0.0}
            for i in range(n)
        ]

    @app.post("/market-data/trading-pair/add", dependencies=[Depends(auth)])
    async def add_pair(payload: dict[str, Any]) -> dict[str, Any]:
        connector = payload.get("connector_name")
        pair = payload.get("trading_pair")
        if pair not in state.market["trading_rules"].get(connector, {}):
            raise HTTPException(
                status_code=404, detail=f"Trading pair {pair} not available on {connector}"
            )
        return {"status": "success", "connector_name": connector, "trading_pair": pair}

    # ── portfolio ───────────────────────────────────────────────────────────
    @app.post("/portfolio/state", dependencies=[Depends(auth)])
    async def portfolio_state(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        accounts = payload.get("account_names") or list(state.market["balances"])
        connectors = payload.get("connector_names")
        drained = state.armed("insufficient_funds")
        out: dict[str, Any] = {}
        for account in accounts:
            per = state.market["balances"].get(account, {})
            out[account] = {}
            for connector, rows in per.items():
                if connectors and connector not in connectors:
                    continue
                book = state.market["prices"].get(connector, {})
                priced = []
                for row in rows:
                    price = 1.0 if row["token"] in ("USDT", "USDC") else book.get(f"{row['token']}-USDT", 0.0)
                    units = 0.0 if drained else row["units"]
                    available = 0.0 if drained else row["available_units"]
                    priced.append(
                        {**row, "units": units, "available_units": available,
                         "price": price, "value": round(units * price, 8)}
                    )
                out[account][connector] = priced
        return out

    # Open orders, perp positions and CLMM positions: the fixture holds none, and
    # get_portfolio_overview reads all three, so they answer empty rather than 501.
    @app.post("/trading/positions", dependencies=[Depends(auth)])
    async def positions(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"data": [], "pagination": {"has_more": False, "next_cursor": None}}

    @app.post("/trading/orders/search", dependencies=[Depends(auth)])
    async def orders(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"data": [], "pagination": {"has_more": False, "next_cursor": None}}

    @app.post("/gateway/clmm/positions/search", dependencies=[Depends(auth)])
    async def clmm_positions(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"data": [], "pagination": {"has_more": False, "next_cursor": None}}

    # Perp account settings: stateful, so a case's leverage change is readable back.
    @app.get("/trading/{account}/{connector}/position-mode", dependencies=[Depends(auth)])
    async def get_position_mode(account: str, connector: str) -> dict[str, Any]:
        mode = state.settings.get((account, connector), {}).get("position_mode", "ONEWAY")
        return {"account_name": account, "connector_name": connector, "position_mode": mode}

    @app.post("/trading/{account}/{connector}/position-mode", dependencies=[Depends(auth)])
    async def set_position_mode(account: str, connector: str, payload: dict[str, Any]) -> dict[str, Any]:
        _require_perp(connector)
        mode = str(payload.get("position_mode") or "").upper()
        if mode not in ("HEDGE", "ONEWAY"):
            raise HTTPException(status_code=400, detail=f"Invalid position mode {mode!r}")
        state.settings.setdefault((account, connector), {})["position_mode"] = mode
        return {"status": "success", "position_mode": mode}

    @app.post("/trading/{account}/{connector}/leverage", dependencies=[Depends(auth)])
    async def set_leverage(account: str, connector: str, payload: dict[str, Any]) -> dict[str, Any]:
        _require_perp(connector)
        pair = payload.get("trading_pair")
        if pair not in state.market["trading_rules"].get(connector, {}):
            raise HTTPException(status_code=400, detail=f"Unknown market {connector}/{pair}")
        leverage = int(payload.get("leverage") or 0)
        if not 1 <= leverage <= 125:
            raise HTTPException(status_code=400, detail=f"Invalid leverage {leverage}")
        state.settings.setdefault((account, connector), {}).setdefault("leverage", {})[pair] = leverage
        return {"status": "success", "trading_pair": pair, "leverage": leverage}

    def _require_perp(connector: str) -> None:
        if not connector.endswith("_perpetual") and "perpetual" not in connector:
            raise HTTPException(status_code=400, detail=f"{connector} is not a perpetual connector")

    # ── executors ───────────────────────────────────────────────────────────
    @app.post("/executors/", dependencies=[Depends(auth)])
    async def create_executor(payload: dict[str, Any]) -> dict[str, Any]:
        config = payload.get("executor_config") or {}
        connector = config.get("connector_name")
        pair = config.get("trading_pair")
        if pair not in state.market["trading_rules"].get(connector, {}):
            raise HTTPException(
                status_code=400, detail=f"Unknown market {connector}/{pair}"
            )
        if state.take("insufficient_funds", "/executors/"):
            raise HTTPException(status_code=400, detail="Insufficient balance")
        executor_id = state.new_executor_id()
        executor = {
            "executor_id": executor_id,
            "id": executor_id,
            "type": config.get("type"),
            "account_name": payload.get("account_name") or state.market["accounts"][0],
            "controller_id": payload.get("controller_id") or "main",
            "connector_name": connector,
            "trading_pair": pair,
            "status": "RUNNING",
            "is_active": True,
            "close_type": None,
            "timestamp": time.time(),
            "config": config,
            "custom_info": {"side": config.get("side")},
            "net_pnl_quote": 0.0,
            "filled_amount_quote": 0.0,
        }
        state.executors[executor_id] = executor
        return {"executor_id": executor_id, "status": "created"}

    @app.post("/executors/search", dependencies=[Depends(auth)])
    async def search_executors(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        f = payload or {}
        rows = list(state.executors.values())

        def keep(e: dict[str, Any]) -> bool:
            for key, field in (
                ("account_names", "account_name"),
                ("connector_names", "connector_name"),
                ("trading_pairs", "trading_pair"),
                ("executor_types", "type"),
                ("controller_ids", "controller_id"),
            ):
                if f.get(key) and e.get(field) not in f[key]:
                    return False
            status = f.get("status")
            return not status or str(e.get("status")).upper() == str(status).upper()

        rows = [copy.deepcopy(e) for e in rows if keep(e)]
        return {"data": rows[: int(f.get("limit") or 50)], "next_cursor": None}

    @app.get("/executors/summary", dependencies=[Depends(auth)])
    async def executors_summary() -> dict[str, Any]:
        active = [e for e in state.executors.values() if e["is_active"]]
        return {"total_active": len(active), "total_executors": len(state.executors)}

    @app.get("/executors/{executor_id}", dependencies=[Depends(auth)])
    async def get_executor(executor_id: str) -> dict[str, Any]:
        executor = state.executors.get(executor_id)
        if executor is None:
            raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")
        return copy.deepcopy(executor)

    @app.get("/executors/{executor_id}/logs", dependencies=[Depends(auth)])
    async def executor_logs(executor_id: str) -> dict[str, Any]:
        if executor_id not in state.executors:
            raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")
        return {"executor_id": executor_id, "logs": [], "total": 0}

    @app.post("/executors/{executor_id}/stop", dependencies=[Depends(auth)])
    async def stop_executor(executor_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        executor = state.executors.get(executor_id)
        if executor is None:
            raise HTTPException(status_code=404, detail=f"Executor {executor_id} not found")
        if not executor["is_active"]:
            return {
                "status": "already_terminated",
                "executor_id": executor_id,
                "close_type": executor["close_type"],
            }
        executor.update(status="TERMINATED", is_active=False, close_type="EARLY_STOP")
        executor["keep_position"] = bool((payload or {}).get("keep_position", False))
        return {"status": "stopped", "executor_id": executor_id}

    # ── everything else ─────────────────────────────────────────────────────
    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
    )
    async def unhandled(path: str, request: Request) -> JSONResponse:
        label = f"{request.method} /{path}"
        if label not in state.unhandled:
            state.unhandled.append(label)
        return JSONResponse(
            {"detail": f"mock hummingbot-api does not implement {label} yet"}, status_code=501
        )

    return app


def main() -> None:  # pragma: no cover — exercised via `runner.py mock-serve`
    import uvicorn

    host = os.environ.get("BENCH_MOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("BENCH_MOCK_PORT", "18000"))
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
