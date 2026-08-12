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
3. **Isolation is the API instance's job.** There is no in-bench gate on
   capital-affecting cases; point `HUMMINGBOT_API_URL` at an instance carrying
   only test connectors. What this repo guarantees is that bench is provably
   talking to *that* instance — which is what check 2 above is for.

---

## What you need

| Component | Why |
|---|---|
| A staging `hummingbot-api` | Isolated from production. Point Settings at an instance that only has paper/dummy credentials when you want isolation — there is no separate "bench account" setting. |
| A condor checkout | bench loads the production MCP wiring from it. |
| Staging URL + credentials | Entered in dashboard Settings (or `.env`). Saving auto-registers a fixed `bench_staging` Condor server entry, bench user `999001`, and chat default — you do not set server name / chat id / user id by hand. |
| Named bots for bot cases | `tool_manage_bots_002` asks for logs from a bot called `eth-maker`. Either create it or expect that case to fail. |

---

## Setup

### 1. Configure the environment

```bash
cp .env.example .env
```

Then set, at minimum (or use **Settings** in the dashboard):

```env
CONDOR_PATH=/path/to/condor
HUMMINGBOT_API_URL=http://staging:8000
HUMMINGBOT_USERNAME=bench
HUMMINGBOT_PASSWORD=...
```

### 2. Register the server in Condor (automatic)

Saving Staging URL/credentials in Settings — or running `staging-check` —
upserts `bench_staging` in condor's `config.yml`, grants the isolated bench
user access, and sets the chat default. You can still force it from the CLI:

```bash
uv run python scripts/register_bench_server.py --dry-run   # inspect
uv run python scripts/register_bench_server.py             # write
```

Keeping URL resolution in Condor's config — rather than passing a URL only from
bench — is what lets bench reuse the production MCP helpers unchanged.

### 3. Verify

```bash
uv run python runner.py staging-check
```

Every check is printed, passing or not. It exits non-zero on any blocking
failure, so it works as a CI or Makefile gate.

```
staging pre-flight url=http://staging:8000
  ✓ condor_checkout: condor at /path/to/condor
  ✓ bench_server_sync: Registered 'bench_staging' → staging:8000 …
  ✓ api_url_declared: http://staging:8000
  ✓ api_url_aliases_agree: HUMMINGBOT_API_URL matches BENCH_EXPECTED_API_URL
  ✓ server_registered: 'bench_staging' → staging:8000
  ✓ mcp_url_matches: MCP --url resolves to http://staging:8000 (matches HUMMINGBOT_API_URL)
  ✓ api_reachable: http://staging:8000 reachable (2 account(s))
  ✓ accounts_listed: 2 account(s) on API
  ✓ no_orphaned_executors: no active executors
```

### 4. Run something read-only

```bash
uv run python runner.py test ollama:qwen2.5:14b --layers tool -d tool:market_data
```

## Risk levels

Every case carries a `risk_level`, and the default is the one that cannot place an
order:

| Level | Means | Examples |
|---|---|---|
| `read_only` | No state change anywhere | `get_market_data`, `get_portfolio_overview`, advisory consults |
| `mutating` | condor-side state only | `manage_routines` create, `manage_memory` write, journal writes |
| `destructive` | Capital-affecting | `manage_executors` create, `manage_bots` deploy, leverage changes, strategy creation |

**Every level runs.** The level is not a gate; it decides two things:

* `is_mutating` (anything above `read_only`) triggers `bench/cleanup.py` teardown
  after the case. A case that mutates but is labelled `read_only` will not be
  cleaned up, so mislabelling leaks state.
* `destructive` cases have to clear a higher score floor (`DESTRUCTIVE_FLOOR`,
  0.70) before a model can be recommended for that domain — a model that passes a
  domain on average but botches the irreversible case is not a routing candidate.

Because there is no gate, the case text itself is part of the safety story: keep
capital small and actions unremarkable, and run against an API instance that has
only test connectors.

---

## Cleanup

`bench/cleanup.py` runs after each mutating case and undoes what that case
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

The pre-flight's orphan scan is the backstop: leftovers block the *next*
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

## Market warmup

Before each case, bench warms every `(connector, trading_pair)` pinned in
`expected_tool_params` (and tick `config`) via
`POST /market-data/trading-pair/add`, then waits until trading rules list the
pair and a mid price is available.

XRPL (and similar) can accept `create_executor` while `trading_rules` still lack
the pair, which surfaces as `Failed to create executor: '<pair>'`. That is
staging readiness, not a model skill — so warmup lives in the harness
(`bench/market_warmup.py`), not in the prompt. A warmup failure skips the case
and records a `harness_artifact`.

---

## Troubleshooting

**`Server 'bench_staging' is not registered`** — run
`scripts/register_bench_server.py`. bench refuses rather than falling back,
because the fallback is condor starting without `mcp-hummingbot`.

**`MCP --url resolves to X but HUMMINGBOT_API_URL is Y`** — the `bench_staging`
entry in condor's `config.yml` points somewhere else. Fix the host/port there;
condor's config is the source of truth for URL resolution, deliberately.

**`A benchmark run needs a condor checkout`** — set `CONDOR_PATH`.

**Tool cases fail with empty payloads rather than errors** — usually the
configured Hummingbot API has no accounts (or the wrong instance). Point
`HUMMINGBOT_API_URL` at an API that has the accounts you intend to exercise;
the pre-flight checks that at least one account is listed.

**`market warmup failed` harness artifact** — the case's pinned connector/pair
could not be made ready (add failed, rules never listed the pair, or no mid
price within the timeout). Check that the connector has credentials and the
pair exists on staging; do not treat this as a model failure.

**`condor's _shared.py no longer exports build_mcp_servers_for_agent()`** — condor
moved the helper. Update `bench/mcp_provider.py` to match. Do not vendor a copy;
the whole point is that bench launches what production launches.
