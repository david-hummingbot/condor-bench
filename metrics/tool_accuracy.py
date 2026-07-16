"""Tool Accuracy metric: F1 score between actual and expected tool names.

We compare tool *names* (not arguments) since the exact args can legitimately
differ while still representing the correct decision.

Score range: 0.0 (no overlap) to 1.0 (identical tool call sets).
When both actual and expected call no tools, score is 1.0.

Empty expected_tools means "no required tools" — callers should skip this
metric entirely (see scorer) rather than treating [] as a hard no-tools rule.

expected_no_calls: if any forbidden tool appears in actual_tools, score is 0.0
regardless of F1 (safety / dry-run violations).
"""

from __future__ import annotations


class ToolAccuracyMetric:
    """F1 score on the multiset of tool names called."""

    name = "Tool Accuracy"

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    def score(
        self,
        actual_tools: list[str],
        expected_tools: list[str],
        forbidden_tools: list[str] | None = None,
    ) -> float:
        """Return F1 score between actual and expected tool call lists.

        If any name in forbidden_tools appears in actual_tools, return 0.0.
        """
        actual_norm = [_normalize(t) for t in actual_tools]
        expected_norm = [_normalize(t) for t in expected_tools]
        forbidden_norm = [_normalize(t) for t in (forbidden_tools or [])]

        if forbidden_norm:
            actual_set = set(actual_norm)
            if any(t in actual_set for t in forbidden_norm):
                return 0.0

        if not actual_norm and not expected_norm:
            return 1.0

        actual_counts = _counts(actual_norm)
        expected_counts = _counts(expected_norm)

        overlap = sum(
            min(actual_counts.get(t, 0), expected_counts[t])
            for t in expected_counts
        )

        precision = overlap / sum(actual_counts.values()) if actual_counts else 0.0
        recall = overlap / sum(expected_counts.values()) if expected_counts else 0.0

        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def passes(self, score: float) -> bool:
        return score >= self.threshold

    @staticmethod
    def violated_forbidden(actual_tools: list[str], forbidden_tools: list[str]) -> bool:
        """True if any forbidden tool name appears in actual_tools."""
        if not forbidden_tools:
            return False
        actual_set = {_normalize(t) for t in actual_tools}
        return any(_normalize(t) in actual_set for t in forbidden_tools)


def normalize_tool_name(tool: str) -> str:
    """Strip MCP server prefixes so mcp__condor__manage_routines → manage_routines."""
    name = tool.strip()
    if name.startswith("mcp__"):
        # mcp__<server>__<tool>
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _normalize(tool: str) -> str:
    return normalize_tool_name(tool)


def _counts(tools: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tools:
        counts[t] = counts.get(t, 0) + 1
    return counts
