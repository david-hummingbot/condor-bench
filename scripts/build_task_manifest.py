#!/usr/bin/env python3
"""Regenerate datasets/manifest.json — the task bank's table of contents.

    uv run python scripts/build_task_manifest.py          # write
    uv run python scripts/build_task_manifest.py --check  # exit 1 if stale

The manifest is derived from the JSONL files (they stay the source of truth) and
records what a reviewer needs without reading every case: the task-set version,
per-category counts by split, split membership, the alias from each task_id to
its case id, and the gaps the bank knowingly does not cover yet.
``tests/test_dataset_floors.py`` fails when this file is out of date.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.dataset import (  # noqa: E402
    CATEGORY_FLOOR,
    SPLITS,
    TASK_CATEGORIES,
    TRADING_CATEGORIES,
    load_all_cases,
    task_problems,
)

MANIFEST_PATH = ROOT / "datasets" / "manifest.json"

# What the bank does not measure yet, and why. A category under the floor must
# appear here, so a thin category is a stated gap rather than a silent one.
GAPS = [
    {
        "area": "negative_safety: stale price",
        "reason": "the mock can serve a backdated quote, but no tool gives the model the "
        "current time, so staleness is unobservable to it; needs Condor to expose quote age",
    },
    {
        "area": "executor_lifecycle: stopping a real executor",
        "reason": "t002/t010/t011 narrate an open executor that is never seeded, so their end "
        "state cannot be asserted (dev split); needs per-case seeding on the mock",
    },
    {
        "area": "dex_gateway: Gateway configuration",
        "reason": "manage_gateway_config is unmeasured: staging runs no Gateway, and the mock "
        "does not implement it",
    },
    {
        "area": "bots_controllers: deploy / stop",
        "reason": "only read-side bot and controller tasks exist; deploying needs a disposable "
        "bot image on staging",
    },
    {
        "area": "holdout baselines",
        "reason": "holdout cases have no baseline latency by design (recording one means "
        "running them), so their latency score self-references on a release run",
    },
]


def build() -> dict:
    cases = load_all_cases(include_holdout=True)
    problems = [p for c in cases for p in task_problems(c)]
    if problems:
        raise SystemExit("task blocks are invalid:\n  " + "\n  ".join(problems))

    counts: dict[str, dict[str, int]] = {
        cat: {"total": 0, **{s: 0 for s in SPLITS}} for cat in TASK_CATEGORIES
    }
    splits: dict[str, list[str]] = defaultdict(list)
    aliases: dict[str, str] = {}
    keys = []
    for case in cases:
        task = case.task
        counts[task["category"]]["total"] += 1
        counts[task["category"]][task["split"]] += 1
        splits[task["split"]].append(task["task_id"])
        if task["task_id"] in aliases:
            raise SystemExit(f"duplicate task_id {task['task_id']}")
        aliases[task["task_id"]] = case.id
        keys.append(f"{task['task_id']}@{task['version']}")

    under = [
        cat for cat in TRADING_CATEGORIES
        if counts[cat]["total"] - counts[cat]["holdout"] < CATEGORY_FLOOR
    ]
    return {
        "_generated_by": "scripts/build_task_manifest.py — do not edit by hand",
        "task_set_version": hashlib.sha256(json.dumps(sorted(keys)).encode()).hexdigest(),
        "category_floor": CATEGORY_FLOOR,
        "categories_under_floor": under,
        "categories": counts,
        "splits": {s: sorted(splits.get(s, [])) for s in SPLITS},
        "aliases": dict(sorted(aliases.items())),
        "gaps": GAPS,
    }


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2) + "\n"


def main() -> int:
    text = render(build())
    if "--check" in sys.argv:
        current = MANIFEST_PATH.read_text() if MANIFEST_PATH.exists() else ""
        if current != text:
            print("datasets/manifest.json is stale — run scripts/build_task_manifest.py")
            return 1
        print("datasets/manifest.json is current")
        return 0
    MANIFEST_PATH.write_text(text)
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
