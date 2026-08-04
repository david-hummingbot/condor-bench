#!/usr/bin/env python3
"""Regenerate dashboard/frontend/src/casePrompts.json from the datasets.

The dashboard imports this JSON at build time so a case row can show its question
even for older runs that didn't persist one. It was hand-maintained, which meant
that after every dataset change the UI silently showed a blank question for the new
cases. Run this instead:

    uv run python scripts/sync_case_prompts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.dataset import case_prompt_map  # noqa: E402

TARGET = ROOT / "dashboard" / "frontend" / "src" / "casePrompts.json"


def main() -> int:
    prompts = case_prompt_map()
    TARGET.write_text(json.dumps(prompts, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {TARGET.relative_to(ROOT)} — {len(prompts)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
