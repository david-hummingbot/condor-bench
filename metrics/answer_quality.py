"""Answer Quality metric — reference-free judge evaluation.

Calls the Claude judge directly (no DeepEval GEval) to avoid internal
OpenAI routing that GEval performs regardless of the custom model.

Score: 0.0 (wrong / unhelpful) → 1.0 (excellent).
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel

from metrics.judge import ClaudeJudge

_CRITERIA = """\
You are evaluating a response from Condor, an automated crypto trading assistant \
that helps users manage grid trading, DCA, CLMM liquidity provision, and \
trend-following strategies via AI agents backed by pydantic-ai and MCP tools.

Score the ACTUAL RESPONSE (and transcript / tool log when provided) against these \
four criteria:

1. Accuracy — are Condor-specific concepts correct? \
   (grid executors, ticks, CLMM, DCA, drawdown limits, pydantic-ai models, MCP tools, etc.)
2. Completeness — does it fully address what the user asked across all turns?
3. Safety — where relevant, are risk management and capital preservation principles respected?
4. Actionability — is the advice coherent and directly useful to a Condor user?

Penalize heavily for fabricating tool results (claiming created/deployed/ran something \
without a matching tool call in the log). Prefer answers grounded in actual tool output.

The tool log you are shown is DIGESTED, not complete. Long outputs are summarised, \
and one that lost content carries an explicit "[digest truncated: showing ~N of M \
characters]" marker. Read absence accordingly:

* If a tool's output is marked truncated, content missing from it is NOT evidence of \
  fabrication — you are looking at a fraction of what the model actually received. \
  Do not penalise a detail merely because you cannot see its source there.
* If a tool's output carries no truncation marker, it is complete, and a claim it does \
  not support IS ungrounded. Penalise that.
* A figure that CONTRADICTS what the log shows is fabrication either way, marker or \
  not. That is the signal to hunt for — a stated $5.4M reserve against a log showing \
  $15K, not a field you simply cannot find.

Score 1.0 for an excellent answer that is accurate, complete, and actionable. \
Score 0.0 for a response that refuses to answer, gives incorrect trading information, \
or fails to meaningfully address the question. \
DO NOT reward hedging or vagueness — a clear, correct, detailed answer is better \
than a cautious non-answer."""

# Unambiguous harness/provider strings: a response containing one of these *is*
# the failure, however long it runs.
_INFRA_PATTERNS = (
    re.compile(r"exceeded before any response", re.I),
    re.compile(r"^\(error:", re.I | re.M),
    # An ACP prompt that never ran — the bridge rejected the request or died. The
    # model produced nothing because it was never asked, so this is infra: judging
    # the empty transcript would score a configuration problem as a model failure
    # ("No response produced", composite 0.0) and average it into the matrix.
    re.compile(r"^ACP prompt failed", re.I | re.M),
)

# Words a *provider outage* puts in a response — and that a model narrating its own
# recovery also uses. These only mean "infra" when there is no real answer around
# them; see :func:`is_infra_failure`.
_INFRA_HINTS = (
    re.compile(r"request_limit", re.I),
    re.compile(r"token limit", re.I),
    re.compile(r"rate[- ]?limit", re.I),
)

# Above this many characters a response is an answer that happens to mention a
# limit, not a bare error string. Calibrated on the two real cases: the genuine
# infra row in the gemma run is 64 characters ("(error: Tool 'manage_executors'
# exceeded max retries count of 1)", which the anchored patterns above catch on
# its own), while the false positive it was voiding is 3,725 characters of
# finished pool ranking — a 58x gap, so the exact cut is not delicate.
_SUBSTANTIVE_ANSWER_CHARS = 600


class _ScoreResult(BaseModel):
    score: float
    reason: str


def _parse(raw: str) -> tuple[float, str]:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)
    score = max(0.0, min(1.0, float(data["score"])))
    return score, str(data.get("reason", ""))


def is_infra_failure(text: str) -> bool:
    """True when the model never produced a usable answer due to infra/provider limits.

    **Pass the model's own output, never the judge transcript.** The transcript embeds
    digested tool results, and condor's own skill files discuss rate limits in prose:
    `pmm_config_playbook/SKILL.md` ("more rate limit usage") and
    `routine_cookbook/SKILL.md` ("bulk fetch many pairs / rate-limit"). A case whose
    tool merely *returned* those words was scored a provider outage —
    `agent_market_making_expert_002` went to 0.5132 and `tool_manage_skill_001` to
    0.452, both on complete, fully grounded answers with every tool call succeeding.

    A hint alone is not enough. ``agent_solana_dex_lp_expert_004`` was voided —
    answer_quality forced to None, composite 0.0, row dropped from the matrix and
    the solana axis silently thinned from 6 cases to 5 while still reporting
    ``pass_rate: 1.0`` — because its own narration said "Rate-limited — I'll wait
    briefly then retry sequentially" and, later, "switch to the Gateway pool
    listing (not rate-limited)". GeckoTerminal had throttled it, it backed off,
    retried, and delivered a complete ten-pool ranking. Handling a transient limit
    gracefully is the behaviour worth rewarding, and this gate was deleting the
    evidence of it.
    """
    if not text or not text.strip():
        return False
    if any(p.search(text) for p in _INFRA_PATTERNS):
        return True
    # A hint counts only when nothing answer-shaped surrounds it.
    if len(text.strip()) > _SUBSTANTIVE_ANSWER_CHARS:
        return False
    return any(p.search(text) for p in _INFRA_HINTS)


# How much of the transcript the judge is shown. The transcript leads with the
# model's answer (see BenchmarkResult.transcript_for_judge) precisely because this
# cap exists: a long tool log must never be able to push the answer out of view.
#
# Raised from 8000 because the cap was manufacturing fabrication verdicts. The log's
# share is split across every call, so a 33-call case gave each tool output 220
# characters and 20 of 80 cases in the last run ran under 800. `tool_manage_skill_001`
# scored 0.15 for an accurate summary of a 10,135-character skill file it had read
# correctly — the judge saw ~13% of the file, could not find the fields, and called it
# invented. Marking truncation (bench.tool_digest) tells the judge when to distrust an
# absence; this reduces how often it has to.
#
# The judge is a Claude model, so this is cheap: the last 80-case run spent 163k judge
# input tokens against $20.76 of model cost, and tripling the ceiling adds well under a
# dollar. Only cases with genuinely long outputs spend the extra.
JUDGE_INPUT_CHARS = 24000
JUDGE_QUESTION_CHARS = 2500


def _build_prompt(input_text: str, actual_output: str) -> str:
    return (
        f"{_CRITERIA}\n\n"
        f"USER INPUT:\n{input_text[:JUDGE_QUESTION_CHARS]}\n\n"
        f"ACTUAL RESPONSE / TRANSCRIPT:\n{actual_output[:JUDGE_INPUT_CHARS]}\n\n"
        "Rate this response. Reply with JSON only, no markdown:\n"
        '{"score": <float 0.0–1.0>, "reason": "<one sentence>"}'
    )


class AnswerQualityMetric:
    name = "Answer Quality"

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold
        self._judge = ClaudeJudge()

    async def a_score(
        self, input_text: str, actual_output: str, *, response: str | None = None
    ) -> tuple[float | None, str]:
        """Score the transcript. ``response`` is the model's own text, if separable.

        The infra gate reads ``response`` rather than ``actual_output``: the latter is
        the judge transcript, which carries tool output that can contain the very words
        the gate looks for. See :func:`is_infra_failure`.

        Returns ``None`` for the score when no judgement could be made — an infra
        failure or a judge that did not answer. ``None`` redistributes this metric's
        weight in the composite (see ``bench.scorer._composite``); 0.0 asserted that
        the model gave a bad answer, which is a different and usually false claim.
        """
        gate = response if response is not None else actual_output
        if not actual_output.strip():
            return None, "No response produced."
        if is_infra_failure(gate):
            return None, f"Infrastructure error (not scored): {gate[:160]}"
        try:
            raw = await self._judge.a_generate(_build_prompt(input_text, actual_output))
            return _parse(raw)
        except Exception as exc:
            # A judge that returned malformed JSON has told us nothing about the model.
            # `tool_explore_geckoterminal_002` lost the full 0.45 weight to
            # "Judge error: Expecting ',' delimiter" on a correct, grounded answer.
            return None, f"Judge error (not scored): {exc}"

    def score(
        self, input_text: str, actual_output: str, *, response: str | None = None
    ) -> tuple[float | None, str]:
        gate = response if response is not None else actual_output
        if not actual_output.strip():
            return None, "No response produced."
        if is_infra_failure(gate):
            return None, f"Infrastructure error (not scored): {gate[:160]}"
        try:
            raw = self._judge.generate(_build_prompt(input_text, actual_output))
            return _parse(raw)
        except Exception as exc:
            return None, f"Judge error (not scored): {exc}"

    def passes(self, score: float) -> bool:
        return score >= self.threshold
