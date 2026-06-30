"""Latency metric: how fast the test model is relative to the baseline.

Score = min(1.0, baseline_latency / test_latency)

- Test model matches baseline speed → 1.0
- Test model is 2x slower → 0.5
- Test model is faster than baseline → 1.0 (capped)
- A floor (LATENCY_FLOOR) prevents very slow models from scoring 0.

This is most meaningful for local models where hardware matters. Cloud models
tend to cluster near the baseline since they're also backed by cloud infra.
"""

from __future__ import annotations

from config import LATENCY_FLOOR


class LatencyMetric:
    name = "Latency Score"

    def score(self, test_latency: float, baseline_latency: float) -> float:
        if test_latency <= 0:
            return 1.0
        if baseline_latency <= 0:
            return 1.0
        raw = baseline_latency / test_latency
        return max(LATENCY_FLOOR, min(1.0, raw))
