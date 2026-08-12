"""Verify what a case left behind, by asking the API rather than the model.

``live_expected`` inspects the responses the *model* received, which answers "did
the calls work" but not "did the thing get built". For a build case that is the
only question that matters: a model can call ``manage_routines(create_routine)``,
get a cheerful response, and leave nothing behind — or leave a routine that exists
but returns an error when run.

So a post-condition calls the tool itself, after the run, and checks the result:

    "post_conditions": {
        "manage_routines": {"action": "list", "contains": ["bench_btc_price"]}
    }

Each entry is ``tool -> {args…, assertions…}``. Recognised assertion keys are the
same ones :mod:`metrics.live_validity` already understands (``nonempty``,
``contains``, ``fields``); everything else is passed to the tool as an argument.

**Ordering matters.** These must run before ``bench.cleanup.teardown``, which
deletes exactly the artefacts being asserted. :func:`bench.client.run_case` calls
this before returning, and teardown is invoked by the caller afterwards, so the
ordering is structural rather than a convention someone has to remember.

Failures here are the model's, not the harness's: a probe that cannot reach the API
returns ``reachable: False`` and is reported rather than scored, so a staging blip
does not read as a model that failed to build something.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Keys consumed as assertions rather than forwarded to the tool as arguments.
_ASSERTION_KEYS = ("nonempty", "contains", "fields")


def _split(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate tool arguments from assertions in one post-condition spec."""
    args = {k: v for k, v in spec.items() if k not in _ASSERTION_KEYS}
    assertions = {k: v for k, v in spec.items() if k in _ASSERTION_KEYS}
    return args, assertions


async def verify(
    post_conditions: dict[str, Any],
    *,
    model: str,
    agent_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Call each declared tool and score its assertions. Never raises.

    Returns one row per condition::

        {"tool": …, "reachable": bool, "score": float | None, "detail": str}

    ``score`` is None when the probe could not be run at all — the caller must not
    fold that into a model score.
    """
    if not post_conditions:
        return []

    from bench.cleanup import _call_tool
    from metrics.live_validity import _score_response

    rows: list[dict[str, Any]] = []
    for tool, spec in post_conditions.items():
        if not isinstance(spec, dict):
            continue
        args, assertions = _split(spec)
        try:
            output = await _call_tool(str(tool), args, agent_slug=agent_slug, model=model)
        except Exception as exc:
            log.warning("post-condition probe failed for %s: %s", tool, exc)
            rows.append(
                {
                    "tool": str(tool),
                    "reachable": False,
                    "score": None,
                    "detail": f"probe could not run: {exc}",
                }
            )
            continue
        score = _score_response(output, assertions)
        rows.append(
            {
                "tool": str(tool),
                "reachable": True,
                "score": score,
                "detail": (
                    "post-condition satisfied"
                    if score >= 1.0
                    else f"post-condition not met (scored {score:.2f})"
                ),
            }
        )
    return rows


def post_condition_score(rows: list[dict[str, Any]]) -> float | None:
    """Mean score over probes that actually ran, or None when none did."""
    scored = [r["score"] for r in rows if r.get("score") is not None]
    if not scored:
        return None
    return max(0.0, min(1.0, sum(scored) / len(scored)))
