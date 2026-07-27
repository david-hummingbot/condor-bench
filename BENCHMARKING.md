# Benchmarking Condor

How we measure — and then improve — Condor's ability to turn what a user
describes into a correct, safe, running trading agent. This doc merges
the original benchmark plan and its validity/safety addendum into one
reference; the harness and datasets live in this repo, the backtest
engine they drive lives in
[condor-simple](https://github.com/hummingbot/condor-simple) (`condor/backtest/` —
product capability first, benchmark harness second). Drafted in
condor-simple (`docs/benchmarking.md` there) and pushed here as the
harness-side copy — expect revisions as the two evolve.

**The claims discipline is the design center.** One `% resolved` number
must not imply properties it never tested. The benchmark is a *layered
scorecard*: each layer proves one thing, is graded its own way, and is
reported separately.

> The honest name for what exists today: a **Hyperliquid single-asset
> strategy translation benchmark** — does Condor turn a user-voice
> description into a valid deterministic strategy routine whose executor
> intents reproduce an accepted reference on frozen fixtures? That is an
> important product capability. It is not, by itself, evidence that
> Condor selects profitable strategies, predicts markets, reproduces
> live fills, or is safe under failure — each of those needs its own
> layer below.

---

## 1. The layered scorecard

| Layer | Question | Grading | Headline |
|---|---|---|---|
| **A0 — simulator conformance** | do the executor simulators match venue semantics? | independent execution fixtures, no LLM | `% execution cases passed` |
| **A1 — strategy translation** | does a user-voice prompt become the described strategy? | deterministic intent/config grading vs accepted references | `% resolved, pass@1` |
| **A2 — stateful tick fidelity** | does the live agent act on the strategy correctly? | sampled fixture states → one dry-run tick → assert on intents/dispositions | `% states handled exactly` |
| **A3 — lifecycle correctness** | create/update/launch/stop/restart/reconcile against an isolated store | deterministic scenarios | `% scenarios passed` |
| **A4 — safety** | risk limits, ownership, dry-run immutability, duplicate prevention | **hard gates** — one violation fails the release | pass/fail + violations |
| **B — helpfulness** | does consult advice recommend/diagnose/refuse correctly? | label match where possible; rubric + judge for prose only | score by case type |
| **C — efficiency** | quality-normalized cost | tokens/calls/time **among resolved cases**; paired ablations | resolved per million tokens |
| **D — coverage** | what fraction of real strategies can Condor express? | corpus classification | `% expressible / composition / clarify / blocked` |

Two independent oracles keep A0 and A1 from contaminating each other:
running the reference and the generated strategy through the *same*
engine proves strategy equivalence **under Condor's semantics** (A1);
whether those semantics match Hyperliquid is a separate question needing
independently captured venue conditions (A0). A shared simulator can be
consistently wrong about queue position, partial fills, intrabar
ordering, funding timestamps, min-notional rejection — so
`outcome_sanity` (PnL-curve correlation) stays a diagnostic cross-check,
never proof of live outcome accuracy.

## 2. Design principles

- **Executable ground truth over judge opinion** (SWE-bench's lesson).
  The trading analog of a fail-to-pass test is a *golden backtest*: a
  frozen fixture + a reference `decide()` whose executor-intent timeline
  is recorded once. LLM judges survive only where determinism is
  impossible (prose quality in consult answers).
- **Benchmark the product, not just the model.** Accuracy runs drive the
  real Condor stack (skills + backtest tool + build flow) headless, with
  one pinned reference model so score deltas attribute to Condor
  changes. condor-bench's original mock-MCP path remains as the separate
  *model comparison* mode.
- **Grade signals, not PnL.** PnL can match by accident and diverge from
  harmless timing jitter. Signal fidelity ("did it implement what was
  described") is the grade; outcome metrics are secondary diagnostics
  that must never override incorrect intents.
- **Deterministic and cheap by default.** The accuracy grade needs zero
  LLM calls after the build phase; `bench-lite` runs in CI per PR.

## 3. Why a backtest engine is the centerpiece

An LLM cannot tick per candle — 30 days of 1m candles is 43k decisions.
So a backtestable strategy carries a **deterministic decision function**:

```
decide(ctx) -> list[Intent]      # Intent ≈ executor config draft + stop directives
```

- **Backtest mode:** the engine loops fixture candles, calls `decide()`
  on each close, feeds intents to executor simulators, books fills/PnL.
  No LLM in the loop.
- **Live mode — the proposal contract:** the same routine runs inside
  the agent tick as a *proposal generator* — the LLM reads the proposed
  configs and executes, resizes, or vetoes them against real positions
  and risk. The honest statement is: **the same deterministic
  entry-signal code is replayed in backtest and in live proposal
  generation** — not "what you backtest is what you run". The gap
  between proposal and execution is real and must be *measured*
  (recorded dispositions), not assumed away.

State parity across modes is closed: `ctx.book` (open positions, working
executors, recent closed round trips) and `ctx.memo` reach `decide()`
identically in backtest, smoke, and live — and are frozen into the
decision inputs, so a replay sees exactly what the decision saw.
`Book.pnl_evidence_class` labels which kind of P&L a strategy is reading
(`simulated` / `behavioral_state_projection` / `venue_reconciled`).

The engine (ported from Hummingbot's strategy_v2 backtesting, then
upgraded) applies the real Hyperliquid fee schedule, funding history to
held perps, venue minimums on the create path, a declared spread at
market exits, and books stop-losses at the stop price rather than the
candle close. `position_pred` is simulated; per-entity price paths flow
through the strategy's `quotes(rows)` seam so one run can price many
instruments. Grid/DCA are deliberately not ported — they compose from N
order executors.

## 4. Datasets

### Instance format (frozen, versioned, SWE-bench-style)

```
instances/{id}/
  instance.json      # source, category, difficulty, ambiguity class
  prompt.md          # USER-VOICE only — never mentions Condor internals
  reference/         # reference decide() + canonical params
  fixture/           # frozen candles + funding + meta
  golden/            # intent timeline + summary from the reference backtest
```

### Every instance declares an ambiguity class

1. **Fully specified** — enough information for a reasonably unique
   implementation.
2. **Clarification required** — the correct behavior is to *ask* the
   predefined material questions, not to guess.
3. **Multiple valid implementations** — an accepted behavior envelope
   (multiple references, parameter ranges, event windows) rather than
   one hidden golden.
4. **Needs decline** — mechanics outside the six-executor vocabulary
   (native TWAP, replace-in-place requoting); the correct output is
   Condor saying so and proposing the nearest expressible
   approximation. Failures here feed the executor-vocabulary roadmap.

### Sources and splits

Corpus: botcamp (~90 curated strategies with reference code) first,
WolfBot (~130 structured TS strategies) second, FMZ (5.8k markdowns,
heavily near-duplicated) cherry-picked for taxonomy and consult mining.
The `feasibility_in_condor` classification doubles as the D-coverage
map. Three versioned splits — development (visible), regression (frozen
fixed failures), hidden evaluation (rotated from sources not used to
author the skills) — with per-instance leakage records (was source code
visible to the build model? does a related library routine exist?).

## 5. Evaluation flow and statistical protocol

1. **Build** (stochastic) — the harness drives the real build flow
   headless from `prompt.md`: pinned model + the real
   `strategy_backtest` skill + a candidate-only backtest tool (no golden
   leakage) → submit, CLARIFY, or DECLINE. Telemetry recorded for
   Track C.
2. **Backtest** (deterministic) — the engine runs the submission on the
   frozen fixtures.
3. **Grade** (deterministic) — entry/exit F1 with candle tolerance
   against the golden, plus **critical fields that fail regardless of
   F1**: executor type, side, sizing rule, barrier config, ownership.
   `resolved` requires all of them; no-trade precision/recall is
   tracked separately (manufacturing a trade where flat is correct is a
   failure, and so is staying flat through an unambiguous signal).

Because artifact creation is stochastic: pin Condor commit, skill
hashes, model, temperature, and tool surface; run each instance ≥3
times; report pass@1/pass@3 with per-instance variance; preserve failed
artifacts and full traces; and never mark a stochastic failure resolved
on a single lucky rerun.

**CI split:** `make bench-lite` is deterministic and free (grader
perturbation tests + selfcheck: every reference re-graded against its
own frozen goldens — catches stale goldens and engine drift). The
end-to-end LLM build runs scheduled — nightly, per release, or when the
build flow changes — never per-PR.

## 6. Safety as hard gates

A safety violation fails the instance and the release regardless of
signal F1: venue/store mutation during dry-run, widened risk through
launch overrides, exceeded position/leverage/drawdown limits, touching
manual or other-agent inventory, duplicate exposure from repeated
signals, "no fill" inferred from a timeout, fabricated cost basis,
duplicate orders after restart, prompt-injected boundary bypasses.
These are invariant-style deterministic cases, not judge scores.

## 7. Current state

What is built and has actually run (here, driven under the
[condor-simple](https://github.com/hummingbot/condor-simple) environment via
`CONDOR_REPO`):

- **Engine + decision seam** (condor-simple): done — `condor/backtest/`
  with fees, funding, minimums, spread, pred simulation, multi-entity
  quotes; one `decide()` serves backtest, smoke (paper replay), and
  live proposal generation, with `ctx.book`/`ctx.memo` parity closed
  and frozen.
- **Dataset**: 18 instances (13 expressible + needs-decline set +
  clarify class + 4 WolfBot-derived), 4 frozen 30d BTC 1h regime
  fixtures (trend-up, trend-down, chop, mixed), goldens showing
  regime×strategy interaction; consult bank at 70 cases with
  deterministic labels where possible.
- **Harness**: deterministic grader with perturbation tests,
  `make bench-lite` selfcheck, and the stochastic A1 build driver
  (`harness/build.py`) reusing condor-simple's `build_agent_client` seam —
  `claude-acp:<tier>` runs on the operator's Claude subscription, no
  API key.
- **It has been run.** A1 across 13 instances × 3 runs on
  `claude-acp:sonnet`: **pass@1 = 11/13** (failures:
  trend-follow-breakout 0/3, twap-slicer 1/3), plus the P4 instances
  (wb-\*, clarify-rsi-vague) at 3/3 each. Earlier single-instance runs
  caught a real Cutler's-vs-Wilder's RSI divergence (prompt pinned, one
  ambiguity class born) and a correct DECLINE naming a missing feed.
  The original consult/tick model-comparison mode predates all of this
  and remains.

## 8. Future work

Prioritized by what the [improvement loop](https://github.com/hummingbot/condor-simple/blob/main/docs/improvement-loop.md) depends
on, not by breadth:

- **A0 simulator-conformance fixtures** — independent execution cases
  (maker rest-and-fill, touched-limit no-fill, partial fill + cancel,
  SL and TP inside one candle, gap through a stop, funding long/short,
  min-size rejection, timeout after a possibly-accepted order). These
  are what make the optimizer's numbers trustworthy, so they outrank
  new instances.
- **Disposition recording (A2)** — every proposed action records
  `executed` / `risk_rejected` / `agent_vetoed` / `stale_data` / … and
  any agent alteration as a structured diff; this is the measured half
  of the proposal contract.
- **A3/A4 suites** — lifecycle and safety cases as deterministic CI.
- **S4 goal instances** — bench instances that measure the improvement
  system itself (outcome correctness, calibration, lift, efficiency),
  once the loop has accumulated enough traversals to grade.
- **Dataset versioning + hidden splits** — needed before any headline
  number is quoted outside the team; consult bank capped at 70 until
  real user questions grow it.
- **Generalization suites (kept separate, never merged into the
  Hyperliquid score)** — multi-coin (the engine prices many entities
  but one clock drives every tick), market making, Solana spot,
  prediction markets.

Open questions carried forward: which LLM modifications to a proposal
preserve "parity" (the contract answer so far: none silently — record
or veto); tolerance calibration so honest paraphrases pass and real
logic errors fail; sizing semantics (corpus sizes as % equity, Condor
in quote — graders compare the rule, not the dollar figure); and when
Condor may legitimately claim outcome fidelity or profitability
evidence (answer: only from measured live fills, never from this
bench).
