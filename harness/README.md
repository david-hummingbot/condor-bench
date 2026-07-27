# harness/ — Track A grading and end-to-end build evaluation

Two workflows, deliberately separated (addendum §8):

## Deterministic CI — `make bench-lite`

No LLM, no network. Runs:

1. `tests/test_grader.py` — the grader passes faithful candidates
   (timing jitter within tolerance, extra protective barriers) and fails
   every distinct error class (side flip, wrong executor type, missing/off
   barriers, sizing drift, spurious/missed entries, manufactured trades).
2. `harness/selfcheck.py` — every ready instance's own reference is
   backtested as a candidate and graded against its frozen goldens.
   Expected 8/8 resolved, F1 = 1.0. A failure means stale goldens
   (`make goldens`, review the diff), condor-simple engine semantic drift,
   or a grader regression — all things a PR must not slip past.

## Stochastic end-to-end — `make build-eval INSTANCE=<id> [MODEL=..] [RUNS=k]`

`harness/build.py`: prompt.md → a pinned agent builds a strategy file using
Condor's real `strategy_backtest.md` skill and a bench-tools MCP server with
two tools — `backtest_strategy` (candidate-only summaries; goldens never
enter the build context) and `submit_strategy` (or `DECLINE: <reason>` for
needs-decline prompts). The artifact is then backtested and graded
deterministically. Results persist under `results/builds/<instance>/<run>/`
(artifact, result.json, grade) for pass@k analysis.

Backends reuse condor-simple's `build_agent_client` factory — the default
`claude-acp:sonnet` runs on the **operator's Claude subscription** via the
claude-agent-acp bridge (no API key); `<provider>:<model>` keys (openrouter,
kimi, deepseek, ...) run on Condor's direct runner with that provider's API
key. Because claude-agent-acp is full Claude Code, the driver's permission
callback denies every tool except the two bench tools, and the agent's
working dir is the empty run dir — the model cannot read goldens or
references off disk.

This driver is the **skill + contract fidelity** rung: it evaluates Condor's
authored contract and skill docs with a pinned model. The next rung — the
full product surface (agent-builder flow over the real condor MCP server
with an isolated store) — reuses this grading tail unchanged.

## Files

| File | Env | Role |
| --- | --- | --- |
| `grade.py` | stdlib | event matching, config fidelity, resolved decision |
| `run_artifact.py` | condor-simple | candidate strategy → per-fixture intents |
| `selfcheck.py` | condor-simple | references self-grade vs frozen goldens |
| `build.py` | condor-bench | end-to-end build → grade, pass@k records |
