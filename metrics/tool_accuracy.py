"""Tool Accuracy metric: F1 score between test model's tool calls and baseline.

We compare tool *names* (not arguments) since the exact args can legitimately
differ while still representing the correct decision.

Score range: 0.0 (no overlap) to 1.0 (identical tool call sets).
When both test and baseline call no tools, score is 1.0 (both made the same
decision to not use tools).
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
    ) -> float:
        """Return F1 score between actual and expected tool call lists."""
        if not actual_tools and not expected_tools:
            return 1.0

        actual_counts = _counts(actual_tools)
        expected_counts = _counts(expected_tools)

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


def _counts(tools: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in tools:
        counts[t] = counts.get(t, 0) + 1
    return counts
