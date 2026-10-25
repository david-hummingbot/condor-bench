"""The one trial runner: every caller that runs cases goes through here.

The CLI (``runner.py test``/``sweep``/``ladder``), the dashboard and the suite
worker used to each carry their own loop, and every fix landed in one of them:
trials, mock seeding and fault arming, mock-gap flagging, recording a crashed
trial instead of dropping it, trace spans. Three loops meant a dashboard run
silently measured less than a CLI run of the same cases. Now they share this one.

Callers differ only in how they report, so reporting is a set of optional async
callbacks (:class:`TrialEvents`): the CLI prints, the dashboard streams SSE, the
suite worker stays quiet. The caller owns the ``scorecards`` list and
``responses`` dict, so a run cancelled mid-case can still persist what it scored.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Hook = Callable[..., Awaitable[None]]

#: Upper bound on one trial's teardown. Teardown is awaited through cancellation
#: (see :func:`finish_despite_cancel`), so it needs a ceiling of its own: a stuck
#: undo call must not hold a stopping run forever.
TEARDOWN_TIMEOUT_S = 180.0


async def finish_despite_cancel(fut: Any) -> Any:
    """Await *fut* to the end even if this task is cancelled meanwhile.

    Teardown is what a cancel most needs to let finish: an undo abandoned half-way
    leaves an order open on staging, and the next run's pre-flight refuses to
    start. ``asyncio.shield`` alone protects the work only from the *first* cancel
    — a second one (a repeated Cancel click, a worker that is signalled twice)
    raised at the await, the caller moved on, and the event loop's shutdown then
    killed the shielded teardown anyway. So every cancel is absorbed until *fut*
    is done, then re-raised. *fut* must be bounded (TEARDOWN_TIMEOUT_S).
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(fut)
            break
        except asyncio.CancelledError:
            if fut.done():  # fut itself was cancelled, not just us
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass
class TrialEvents:
    """Optional async hooks. Any left as None is skipped.

    * ``before_trial(case, trial, trials)`` — awaited before each trial starts; the
      dashboard parks here while a run is paused.
    * ``trial_started(case, trial, trials)``
    * ``trial_done(case, trial, card, response, error)`` — once per trial, with the
      card that was recorded (harness and timeout cards included).
    * ``cleanup(case, trial, report)`` — after teardown ran.
    * ``note(case, text, level)`` — warmup notes, skips, harness explanations;
      ``level`` is ``"info"`` | ``"warn"`` | ``"error"``.
    """

    before_trial: Hook | None = None
    trial_started: Hook | None = None
    trial_done: Hook | None = None
    cleanup: Hook | None = None
    note: Hook | None = None

    async def _fire(self, name: str, *args: Any) -> None:
        hook = getattr(self, name)
        if hook is not None:
            await hook(*args)


def current_lane() -> str:
    from config import staging_config

    return str(staging_config().get("env_mode") or "live")


def cases_for_lane(cases: list, lane: str | None = None) -> tuple[list, list[str]]:
    """``(runnable, skipped ids)`` — cases that need another lane are left out."""
    from bench.dataset import runs_on_lane

    lane = lane or current_lane()
    kept = [c for c in cases if runs_on_lane(c, lane)]
    skipped = sorted(c.id for c in cases if not runs_on_lane(c, lane))
    return kept, skipped


def seed_mock(faults: list | None = None, attempts: int = 2) -> str | None:
    """Reset the mock to its fixture and arm the case's faults.

    Returns why seeding failed, or None on success.
    """
    from bench.lanes import arm_mock_faults, reset_mock

    last = ""
    for _ in range(attempts):
        try:
            reset_mock()
            arm_mock_faults(faults or [])
            return None
        except Exception as exc:  # noqa: BLE001 — any failure means "not seeded"
            last = f"{type(exc).__name__}: {exc}"
    return f"mock could not be reset to its fixture after {attempts} attempts ({last})"


def flag_mock_gaps(card: Any) -> None:
    """A trial that hit an endpoint the mock does not implement measured the mock.

    The model got a 501 for a call that would have worked on a real API, so the row
    is a harness artifact — excluded and named — not a model failure.
    """
    from bench.lanes import mock_state

    try:
        gaps = mock_state().get("unhandled") or []
    except Exception:  # noqa: BLE001 — a dead mock is caught by the next reset
        return
    # A forbidden call is the model's whatever else the mock lacked: excluding it
    # as a harness artifact would launder an unsafe action out of the unsafe rate.
    if gaps and not card.harness_artifact and not card.forbidden_violations:
        card.harness_artifact = (
            f"mock lane does not implement {', '.join(gaps[:4])}"
            + (f" (+{len(gaps) - 4} more)" if len(gaps) > 4 else "")
            + " — run this case on the live lane"
        )


async def run_trial(
    case: Any,
    model: str,
    store: Any,
    trial: int,
    scorecards: list,
    responses: dict,
    events: TrialEvents | None = None,
) -> Any:
    """One trial of one case: seed, run, score, record, tear down. Returns the card.

    Never raises for a failed trial — a timeout, a crash or an unseedable mock each
    become a recorded harness card — except ``asyncio.CancelledError``, which the
    caller needs in order to stop.
    """
    from bench.cleanup import created_resources, teardown
    from bench.client import recording_trace, run_case
    from bench.dataset import is_mutating
    from bench.reporter import trial_key
    from bench.scorer import harness_card, score_case, timeout_card, with_trial
    from bench.traces import case_span
    from config import case_timeout_s

    events = events or TrialEvents()
    lane = current_lane()
    response = ""
    error: str | None = None
    partial = None
    # The teardown of the trial's full result, once started. A cancel or a late
    # exception after this point must not undo the same resources a second time.
    undo: asyncio.Future | None = None

    async def bounded_teardown(trace: Any) -> Any:
        try:
            return await asyncio.wait_for(
                teardown(
                    trace,
                    model,
                    agent_slug=getattr(case, "agent_slug", None),
                    tick=getattr(case, "type", "") == "tick",
                ),
                timeout=TEARDOWN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            await events._fire(
                "note", case,
                f"teardown did not finish in {TEARDOWN_TIMEOUT_S:.0f}s — check staging "
                "for leftovers (the next pre-flight reports any executor)", "error",
            )
            return None

    def start_undo(trace: Any) -> asyncio.Future:
        return asyncio.ensure_future(bounded_teardown(trace))

    def needs_undo(trace: Any) -> bool:
        return trace is not None and (is_mutating(case) or bool(created_resources(trace)))

    if lane == "mock":
        # Every trial starts from the fixture: the mock is stateful, and a trial
        # must not inherit the executor the previous one created. A trial that
        # cannot be seeded is retried once, then recorded as infra — never scored
        # against a state it did not start from, and never silently dropped.
        reason = seed_mock(getattr(case, "mock_faults", None) or [])
        if reason:
            card = harness_card(case, model, reason, trial_idx=trial)
            scorecards.append(card)
            await events._fire("trial_done", case, trial, card, "", reason)
            return card
    try:
        # Baseline first: the ceiling is sized from it, so a slow case gets room
        # proportional to how slow it has always been.
        baseline = store.load(case.id)
        timeout_s = case_timeout_s(baseline.latency_s if baseline else None)
        with case_span(case.id, trial=trial, model=model, lane=lane) as trace_link, \
                recording_trace() as partial:
            result = await asyncio.wait_for(run_case(case, model), timeout=timeout_s)
        baseline_latency = baseline.latency_s if baseline else result.latency_s
        card = await score_case(case, result, baseline_latency, trial_idx=trial)
        card.trace_id, card.trace_url = trace_link["trace_id"], trace_link["trace_url"]
        if lane == "mock":
            flag_mock_gaps(card)
        response = result.response
        scorecards.append(card)
        responses[trial_key(case.id, trial)] = response
        await events._fire("trial_done", case, trial, card, response, None)

        # Undo what the trial created, whatever its score — an aborted half-created
        # resource still needs removing. Keyed on the trace as well as the case's
        # risk level: a read-only case is exactly where a model that falls for a
        # prompt injection creates something nobody planned to clean up.
        if needs_undo(result):
            undo = start_undo(result)
            report = await finish_despite_cancel(undo)
            if report is not None:
                await events._fire("cleanup", case, trial, report)
        return card
    except asyncio.CancelledError:
        # A cancelled run (the dashboard's Cancel, a server shutdown, a suite
        # worker stopped by its runner) still owes staging the undo: an order
        # placed before the cancel otherwise stays open and blocks the next
        # pre-flight. Finish a teardown already under way, or undo from the trace.
        if undo is None and needs_undo(partial):
            undo = start_undo(partial)
        if undo is not None:
            await finish_despite_cancel(undo)
        raise
    except asyncio.TimeoutError:
        # Record it. Appending nothing made the case vanish from the run entirely,
        # so a thinned domain looked like a domain nobody had asked about.
        baseline = store.load(case.id)
        base_s = baseline.latency_s if baseline else 0.0
        limit = case_timeout_s(base_s or None)
        card = with_trial(timeout_card(case, model, limit, base_s), trial)
        error = f"timed out after {limit:.0f}s — recorded as a harness artifact"
    except Exception as exc:
        # Recorded, not dropped: a vanished trial makes thin evidence look complete.
        card = harness_card(
            case, model, f"harness error: {type(exc).__name__}: {exc}", trial_idx=trial
        )
        error = f"{exc} — recorded as a harness artifact"
    scorecards.append(card)
    # A trial cut off mid-way may already have created something — a timed-out
    # `budgeted_buy` left its market order's executor running, and the next run's
    # pre-flight refused to start. Undo what its trace got as far as recording,
    # started before anything that can be cancelled so a cancel cannot skip it.
    owned = undo is None and needs_undo(partial)
    if owned:
        undo = start_undo(partial)
    report = None
    try:
        await events._fire("trial_done", case, trial, card, response, error)
    finally:
        if owned:
            report = await finish_despite_cancel(undo)
    if report is not None:
        await events._fire("cleanup", case, trial, report)
    return card


async def run_cases(
    cases: list,
    model: str,
    store: Any,
    trials: int = 1,
    *,
    scorecards: list | None = None,
    responses: dict | None = None,
    events: TrialEvents | None = None,
) -> tuple[list, dict]:
    """Run and score every case *trials* times. Returns ``(scorecards, responses)``.

    A case's trials run back to back on the same market binding (bound once,
    before this is called), so trial groups pair across runs. Warmup is checked
    once per case: a failure costs every trial, and each trial gets its own skip
    card so the run's row count stays cases × trials.
    """
    from bench.market_warmup import ensure_markets_for_case, warmup_failure_card
    from bench.probe_journal import ensure_probe_journal
    from bench.scorer import harness_card, with_trial
    from bench.traces import flush

    events = events or TrialEvents()
    scorecards = scorecards if scorecards is not None else []
    responses = responses if responses is not None else {}
    try:
        for case in cases:
            try:
                journal_note = ensure_probe_journal(case)
                warmup = await ensure_markets_for_case(case)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Setup failing is a harness fact about every trial of the case.
                reason = f"harness error before the run: {type(exc).__name__}: {exc}"
                for t in range(1, trials + 1):
                    await events._fire("before_trial", case, t, trials)
                    card = harness_card(case, model, reason, trial_idx=t)
                    scorecards.append(card)
                    await events._fire("trial_done", case, t, card, "", reason)
                continue
            if not warmup.ok:
                card = warmup_failure_card(case, model, warmup)
                for t in range(1, trials + 1):
                    await events._fire("before_trial", case, t, trials)
                    row = with_trial(card, t)
                    scorecards.append(row)
                    await events._fire("trial_done", case, t, row, "", card.harness_artifact)
                continue
            for text in ([journal_note] if journal_note else []) + list(warmup.notes):
                await events._fire("note", case, text, "info")
            for trial in range(1, trials + 1):
                await events._fire("before_trial", case, trial, trials)
                await events._fire("trial_started", case, trial, trials)
                await run_trial(case, model, store, trial, scorecards, responses, events)
    finally:
        flush()
    return scorecards, responses
