"""Harness skill patches applied to CONDOR_PATH before a run."""

from __future__ import annotations

from pathlib import Path

from bench.skill_patches import (
    _GET_PRICES_BAD,
    _GET_PRICES_GOOD,
    apply_skill_patches,
)


def _write_cookbook(tmp_path: Path, body: str) -> Path:
    path = (
        tmp_path
        / "agents"
        / "_shared"
        / "skills"
        / "routine_cookbook"
        / "hummingbot_client.md"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Hummingbot Client API\n\n### Prices\n```python\n" + body + "\n```\n",
        encoding="utf-8",
    )
    return path


def test_get_prices_shape_patch_rewrites_the_flat_map(tmp_path: Path):
    """The failure this patch exists to prevent.

    agent_condor_routine_001 followed hummingbot_client.md's flat
    ``prices.get("BTC-USDT")`` example, got None from a live nested response, and
    was scored down for an env issue that was really a cookbook lie.
    """
    path = _write_cookbook(tmp_path, _GET_PRICES_BAD)
    results = apply_skill_patches(tmp_path)
    assert len(results) == 1
    assert results[0].status == "applied"
    text = path.read_text(encoding="utf-8")
    assert _GET_PRICES_BAD not in text
    assert 'prices.get("prices", {}).get("BTC-USDT", 0)' in text


def test_get_prices_shape_patch_is_idempotent(tmp_path: Path):
    _write_cookbook(tmp_path, _GET_PRICES_GOOD)
    first = apply_skill_patches(tmp_path)
    second = apply_skill_patches(tmp_path)
    assert first[0].status == "already_correct"
    assert second[0].status == "already_correct"


def test_get_prices_shape_patch_reports_missing_file(tmp_path: Path):
    results = apply_skill_patches(tmp_path)
    assert results[0].status == "missing"
