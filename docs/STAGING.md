# Staging setup

Every benchmark run goes against a real `hummingbot-api` through condor's own MCP
servers. There is no offline mode, because canned payloads cannot answer the
question the model sizing study is actually about: **can this model size call the
right tool with the right arguments and do something useful with what comes
back?** Mock payloads are correct by construction, so a model that fumbles every
real response would still score well against them.

Running against a real API also introduces a way to lose money, so most of this
document is about the guard rails.

---

## The failure this setup is designed around

condor's `.mcp.json` declares `mcp-hummingbot` with **no CLI arguments**:

```json
"mcp-hummingbot": { "args": ["run", "python", "-m", "mcp_servers.hummingbot_api"] }
```

With no `--url`, the MCP server's own settings chain falls back to
`HUMMINGBOT_API_URL`, and then to `http://localhost:8000` with `admin`/`admin`
(`mcp_servers/hummingbot_api/settings.py`). On a developer machine, that last hop
is plausibly the **real** hummingbot-api.

So a benchmark that fails to pass `--url` explicitly does not error. It connects
to whatever is listening on localhost and starts placing orders, while reporting
tool-accuracy scores that look completely normal.

Three things prevent that:

1. **bench never relies on `.mcp.json`.** `bench/mcp_provider.py` builds explicit
   by-name server configs by calling condor's own
   `build_mcp_servers_for_agent()` / `build_mcp_servers_for_session()`. It loads
   those functions from the condor checkout rather than copying them, so the
   spawn args cannot drift into a private variant.
2. **The pre-flight is fail-closed.** `bench/staging_health.py` reads the `--url`
   the subprocess will *actually* be launched with and refuses to run unless it
   equals `HUMMINGBOT_API_URL` exactly. No prefix matching, no fallback.
3. **Mutating cases are gated separately.** `BENCH_ALLOW_MUTATING` defaults to
   false, and even when true it only takes effect if every other check passed.

---

## What you need

| Component | Why |
|---|---|
| A staging `hummingbot-api` | Isolated from production. Paper or testnet accounts only. |
| A pre-seeded account | Named in `BENCH_STAGING_ACCOUNT`; the tool cases reference it by name. A wrong name makes portfolio and executor cases return empty rather than error, which scores as a *model* failure. |
| A condor checkout | bench loads the production MCP wiring from it. |
| A server entry in condor's `config.yml` | condor resolves the API URL and credentials by server name. Without the entry it starts *without* `mcp-hummingbot` — a log warning, not an error — and every hummingbot tool case fails for a reason unrelated to the model. |
| Fixed `BENCH_CHAT_ID` / `BENCH_USER_ID` | Keeps condor MCP state (memory, notes, journals) out of any real chat's stores. |
| Named bots for bot cases | `tool_manage_bots_002` asks for logs from a bot called `eth-maker`. Either create it or expect that case to fail. |

---

## Setup

### 1. Configure the environment

```bash
cp .env.example .env
```

Then set, at minimum:

```env
CONDOR_PATH=/path/to/condor
HUMMINGBOT_API_URL=http://staging:8000
HUMMINGBOT_USERNAME=bench
HUMMINGBOT_PASSWORD=...
BENCH_SERVER_NAME=bench_staging
BENCH_STAGING_ACCOUNT=bench_paper
```

Leave `BENCH_ALLOW_MUTATING` unset for now.

### 2. Register the server in condor

```bash
uv run python scripts/register_bench_server.py --dry-run   # inspect
uv run python scripts/register_bench_server.py             # write
```

This adds (or updates) a `bench_staging` entry in condor's `config.yml` derived
from the env vars above. Keeping URL resolution in condor's config — rather than
passing a URL from bench — is what lets bench reuse the production helpers
unchanged.

### 3. Verify

```bash
uv run python runner.py staging-check
```

Every check is printed, passing or not. It exits non-zero on any blocking
failure, so it works as a CI or Makefile gate.

```
staging pre-flight url=http://staging:8000
  ✓ condor_checkout: condor at /path/to/condor
  ✓ api_url_declared: http://staging:8000
  ✓ api_url_aliases_agree: HUMMINGBOT_API_URL matches BENCH_EXPECTED_API_URL
  ✓ server_registered: 'bench_staging' → staging:8000
  ✓ mcp_url_matches: MCP --url resolves to http://staging:8000 (matches HUMMINGBOT_API_URL)
  ✓ api_reachable: http://staging:8000 reachable (2 account(s))
  ✓ bench_account (mutating only): 'bench_paper' present
  ✓ no_orphaned_executors (mutating only): no active executors
```

### 4. Run something read-only

```bash
uv run python runner.py test ollama:qwen2.5:14b --layers tool -d tool:market_data
```

### 5. Only then enable mutating cases

```env
BENCH_ALLOW_MUTATING=true
```

Re-run `staging-check`. Mutating cases stay blocked unless the mutating-only
checks pass too.

---

## Risk levels

Every case carries a `risk_level`, and the default is the one that cannot place an
order:

| Level | Means | Examples |
|---|---|---|
| `read_only` | No state change anywhere | `get_market_data`, `get_portfolio_overview`, advisory consults |
| `mutating` | condor-side state only | `manage_routines` create, `manage_memory` write, journal writes |
| `destructive` | Capital-affecting | `manage_executors` create, `manage_bots` deploy, leverage changes, strategy creation |

A run without `BENCH_ALLOW_MUTATING` keeps only `read_only`. Destructive cases
additionally have to clear a higher score floor (`DESTRUCTIVE_FLOOR`, 0.70) before
a model can be recommended for that domain — a model that passes a domain on
average but botches the irreversible case is not a routing candidate.

---

## Cleanup

`bench/cleanup.py` runs after each mutating live case and undoes what that case
created. Two rules matter:

- **Only what this run created.** Resource ids come from the case's own tool
  trace. There is no "stop everything active" sweep — that could kill a position
  a human was using, which is worse than a dirty database.
- **Executors are stopped with `keep_position=true`.** Cleanup removes
  bookkeeping; closing a position is a trade, not a teardown step.

Deployed bots and saved controllers are **not** auto-removed. A deployed bot holds
capital through a controller, so stopping it is a trading decision. Those are
reported for manual attention instead:

```
      left behind: manage_bots eth-maker-bench — manual
```

The pre-flight's orphan scan is the backstop: leftovers block the *next* mutating
run rather than accumulating silently.

---

## Agent scoping (`--agent-slug`)

This is subtle and it caused the review correction that this whole design hinges
on, so it is worth stating plainly.

condor's MCP tools read and write memory and skills scoped to whoever is asking.
Passing `--agent-slug market_making_expert` scopes them to
`agents/market_making_expert/`. Omitting it scopes them to the **chat** condor's
own stores.

A `market_making_expert` case run without the flag therefore cannot find its
`pmm_config_playbook` skill. It fails. And the matrix would report
"market_making_expert needs a 14B model" when the truth is that the harness was
misconfigured — a harness artifact laundered into a production routing decision.

So:

- Layer 3 cases (`datasets/agents.jsonl`) must declare `agent_slug`, with explicit
  `null` for chat-scoped cases. `tests/test_tool_surface_drift.py` enforces that
  the key is present.
- Tick cases are agent-scoped by construction and get a per-case slug.
- `tests/test_mcp_wiring_drift.py` asserts the flag actually reaches the condor
  MCP subprocess, and that chat-scoped cases still omit it.
- When the flag is missing, or an assistant prompt falls back to the generic
  Condor one, the result is tagged `harness_artifact` and **excluded** from the
  routing matrix rather than counted as a model failure.

---

## The playwright trap

condor's `.mcp.json` also declares a `playwright` server. On the PydanticAI path
this never matters — bench passes explicit configs and never reads `.mcp.json`.

On the **ACP** path (`claude-code`, `gemini`) the agent auto-discovers stdio
servers from its working directory, which has to be the condor repo.
The by-name overrides replace `mcp-hummingbot` and `condor`, but nothing can
*remove* playwright from that side.

Why it matters: extra tools shift the `tool_defs[:limit]` cut that small models
run under (`_TOOL_LIMITS`), so a model's tool score would partly depend on how
many browser tools happened to sort ahead of the trading ones. Bench reports it
rather than pretending otherwise — such runs are tagged `harness_artifact` and
excluded from routing, and `tool_count_effective` is recorded per case so tool
counts across paths are comparable.

---

## Troubleshooting

**`Server 'bench_staging' is not registered`** — run
`scripts/register_bench_server.py`. bench refuses rather than falling back,
because the fallback is condor starting without `mcp-hummingbot`.

**`MCP --url resolves to X but HUMMINGBOT_API_URL is Y`** — the `bench_staging`
entry in condor's `config.yml` points somewhere else. Fix the host/port there;
condor's config is the source of truth for URL resolution, deliberately.

**`A benchmark run needs a condor checkout`** — set `CONDOR_PATH`.

**Tool cases fail with empty payloads rather than errors** — usually
`BENCH_STAGING_ACCOUNT` naming an account that doesn't exist. The pre-flight
checks this, but only as a mutating-only check, so a read-only run won't block on
it.

**`condor's _shared.py no longer exports build_mcp_servers_for_agent()`** — condor
moved the helper. Update `bench/mcp_provider.py` to match. Do not vendor a copy;
the whole point is that bench launches what production launches.
