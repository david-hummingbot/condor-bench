#!/usr/bin/env python3
"""Track condor's `acp/` against the vendored copy, per function.

    uv run python scripts/acp_drift.py [--condor PATH] [--check | --update]

`condor_compat/acp/` is the one vendored area `revendor.py` cannot regenerate.
Its files are not condor's-plus-an-appendix; bench diverges *inside* functions —
permissions are auto-approved, `PromptDone` carries an `error`, MCP servers are
built with `include_instructions=True` and a per-server `cwd`, a failed prompt
reports on `PromptDone` instead of in the transcript, `resolve_acp` ignores the
`default` sentinel, `parse_session_models` reads two wire shapes. Regenerating
would delete all of it, so this pulls nothing: it only tells you what moved.

Granularity is per function, and on the body rather than the file, because the
drift that motivated this was neither a new file nor a new function. condor's
ffe9e5af added `include_instructions=True` inside an existing `start()`; bench
went on scoring pydantic-ai models that had never received ~15KB of the condor
server's own instructions, while ACP models on the same cases received them.
A file hash is too coarse to survive condor's commit rate, a name inventory too
coarse to see that change at all.

Docstrings are excluded from the hash. condor's carry the reasoning and churn
independently of behaviour; a prose edit should not cost a review.

`--update` re-pins to the current checkout. Run it only once you have read the
diff and decided what bench does about it — a pin refreshed without that is
worse than no pin, because the next reader believes it was reviewed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIN = ROOT / "tests" / "acp_upstream_pin.json"

# vendored file -> upstream file, relative to each repo root.
FILES = {
    "condor_compat/acp/client.py": "condor/acp/client.py",
    "condor_compat/acp/acp_client.py": "condor/acp/client.py",
    "condor_compat/acp/pydantic_ai_client.py": "condor/acp/pydantic_ai_client.py",
    "condor_compat/acp/jsonrpc.py": "condor/acp/jsonrpc.py",
}


def _strip_docstring(node: ast.AST) -> ast.AST:
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:] or [ast.Pass()]
    return node


def functions(path: Path) -> dict[str, str]:
    """`qualname -> hash of the body`, docstrings excluded."""
    out: dict[str, str] = {}

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in node.body:  # type: ignore[attr-defined]
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                dumped = ast.dump(_strip_docstring(child), annotate_fields=False)
                out[prefix + child.name] = hashlib.sha1(dumped.encode()).hexdigest()[:12]
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")

    walk(ast.parse(path.read_text()))
    return out


def upstream_state(condor: Path) -> dict:
    seen: dict[str, dict[str, str]] = {}
    for upstream in dict.fromkeys(FILES.values()):
        path = condor / upstream
        seen[upstream] = functions(path) if path.is_file() else {}
    return {"condor": _describe(condor), "files": seen}


def _describe(condor: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(condor), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"  # A provenance label must never break the check.


def compare(condor: Path) -> tuple[list[str], dict]:
    """Return (report lines, current state). Empty lines means no drift."""
    state = upstream_state(condor)
    if not PIN.is_file():
        return [f"no pin yet — create one with --update ({state['condor']})"], state
    pinned = json.loads(PIN.read_text())
    lines: list[str] = []
    for upstream, now in state["files"].items():
        was = pinned.get("files", {}).get(upstream, {})
        added = sorted(set(now) - set(was))
        removed = sorted(set(was) - set(now))
        changed = sorted(k for k in set(now) & set(was) if now[k] != was[k])
        for name in added:
            lines.append(f"  + {upstream}::{name}  (new upstream — does bench need it?)")
        for name in removed:
            lines.append(f"  - {upstream}::{name}  (gone upstream)")
        for name in changed:
            lines.append(f"  ~ {upstream}::{name}  (body changed upstream)")
    if lines:
        lines.insert(0, f"condor moved: {pinned.get('condor')} -> {state['condor']}")
    return lines, state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--condor", default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args()

    from config import condor_path

    condor = Path(args.condor) if args.condor else condor_path()
    if not condor or not (condor / "condor" / "acp").is_dir():
        print("no condor checkout — set CONDOR_PATH")
        return 0

    lines, state = compare(condor)
    if args.update:
        PIN.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        print(f"pinned to condor {state['condor']} ({sum(len(v) for v in state['files'].values())} functions)")
        return 0
    if lines:
        print("condor_compat/acp/ is behind condor:")
        print("\n".join(lines))
        print("\nReview each, port what bench needs, then: uv run python scripts/acp_drift.py --update")
        return 1 if args.check else 0
    print(f"up to date with condor {state['condor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
