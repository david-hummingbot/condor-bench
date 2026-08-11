#!/usr/bin/env python3
"""Regenerate the dashboard's case lookup JSONs from the datasets.

The dashboard imports these at build time so a case row can show its question and
its dataset layer even for older runs that didn't persist them. They were
hand-maintained, which meant that after every dataset change the UI silently showed
a blank question for the new cases. Run this instead:

    uv run python scripts/sync_case_prompts.py

The layer map matters for the same reason: an id-prefix guess cannot tell a
chat-scoped Layer 3 case (merged into the consult layer, still named ``agent_*``)
from a Layer 3 one, so without it the Type column mislabels them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.dataset import case_prompt_map, load_all_cases  # noqa: E402

SRC = ROOT / "dashboard" / "frontend" / "src"
TARGET = SRC / "casePrompts.json"
TYPES_TARGET = SRC / "caseTypes.json"


def main() -> int:
    prompts = case_prompt_map()
    TARGET.write_text(json.dumps(prompts, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {TARGET.relative_to(ROOT)} — {len(prompts)} cases")

    types = {case.id: case.type for case in load_all_cases()}
    TYPES_TARGET.write_text(json.dumps(types, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {TYPES_TARGET.relative_to(ROOT)} — {len(types)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
