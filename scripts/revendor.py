#!/usr/bin/env python3
"""Pull condor's prompt builders into ``condor_compat/`` — the whole file, every time.

    uv run python scripts/revendor.py [--condor PATH] [--check]

Four artefacts, all generated, none hand-edited:

  condor_compat/agents/condor/AGENT.md         agents/condor/AGENT.md (frontmatter stripped)
  condor_compat/agents/prompts.py              condor/agents/prompts.py
  condor_compat/runtime/context.py             the chat preload out of condor/runtime/context.py
  condor_compat/mcp_servers/condor_profiles.py       mcp_servers/condor/profiles.py
  condor_compat/mcp_servers/hummingbot_profiles.py   mcp_servers/hummingbot_api/profiles.py

Why a script and not a careful copy: the previous vendor was a careful copy, and
it lost the drift paragraph, the routine-authoring rule and the entire SESSION
CANVAS block out of prompt constants it claimed to carry — none of which any
test could see, because the checks compare tool *names* against the surface
snapshot and never prompt *text*. A copy that a machine performs cannot omit a
paragraph by accident.

Bench's own additions live in ``condor_compat/agents/_bench_prompts.py`` and are
appended below a marker. Re-vendoring regenerates everything above it and
touches nothing below, so pulling upstream is never a merge.

``--check`` regenerates into memory and exits non-zero on any difference. That is
what CI and ``tests/test_vendored_drift.py`` run; it turns "someone edited the
generated file" and "condor moved" into the same, loud failure.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARKER = (
    "# ═══════════════════════════════════════════════════════════════════════════\n"
    "# BENCH-SPECIFIC SECTION — generated from condor_compat/agents/_bench_prompts.py\n"
    "#\n"
    "# Everything ABOVE this line is condor's file with imports rewritten; do not\n"
    "# edit it, `scripts/revendor.py` overwrites it. Everything BELOW is bench's,\n"
    "# and re-vendoring re-appends it untouched. Edit _bench_prompts.py, not this.\n"
    "# ═══════════════════════════════════════════════════════════════════════════\n"
)

# Upstream import -> where the vendored copy reads it from instead. Applied in
# order, longest first, as plain prefix rewrites on the import line.
#
# Anything NOT matched here keeps its `condor.` import and fails at import time
# with the missing name. That is the design: a new upstream dependency must be
# acknowledged in upstream_shims.py, not silently dropped.
# Exact-line rewrites, applied before the prefix table below. These upstream
# imports rename on the way in (``profiles as condor_profiles``), so a prefix
# swap would mangle the tail of the statement.
LINE_REWRITES = {
    "from mcp_servers.condor import profiles as condor_profiles":
        "from condor_compat.mcp_servers import condor_profiles",
    "from mcp_servers.hummingbot_api import profiles as hummingbot_profiles":
        "from condor_compat.mcp_servers import hummingbot_profiles",
}

REWRITES = [
    ("from condor.acp.", "from condor_compat.acp."),
    ("from condor.frontmatter import", "from condor_compat.upstream_shims import"),
    ("from condor.runtime.state import", "from condor_compat.upstream_shims import"),
    ("from condor.runtime.toolsets import", "from condor_compat.upstream_shims import"),
    ("from condor.memory.paths import", "from condor_compat.upstream_shims import"),
    ("from routines.base import", "from condor_compat.upstream_shims import"),
    ("from .agent import", "from condor_compat.upstream_shims import"),
    ("from .strategy import", "from condor_compat.upstream_shims import"),
]

HEADER = '''"""GENERATED — do not edit. Vendored from condor by scripts/revendor.py.

Source: {src}
condor:  {rev}

Imports are rewritten to condor_compat/upstream_shims.py; the prompt text itself
is byte-for-byte condor's. Bench's additions are appended below the marker at the
end of this file — edit condor_compat/agents/_bench_prompts.py for those.

Re-pull with:  uv run python scripts/revendor.py
"""
'''


def rewrite_imports(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        exact = LINE_REWRITES.get(stripped.rstrip())
        if exact:
            out.append(line[: len(line) - len(stripped)] + exact + "\n")
            continue
        for old, new in REWRITES:
            if stripped.startswith(old):
                indent = line[: len(line) - len(stripped)]
                line = indent + stripped.replace(old, new, 1)
                break
        out.append(line)
    return "".join(out)


def strip_module_docstring(text: str) -> str:
    """Drop the upstream docstring; the generated header replaces it."""
    tree = ast.parse(text)
    if not (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        return text
    return "".join(text.splitlines(keepends=True)[tree.body[0].end_lineno:])


def bench_section() -> str:
    """``_bench_prompts.py`` minus its docstring, imports and ``from __future__``.

    Its body is appended into a module that already has them. Only top-level
    statements survive, so the file reads as a normal module on its own and as a
    tail section here.
    """
    text = (ROOT / "condor_compat" / "agents" / "_bench_prompts.py").read_text()
    body = strip_module_docstring(text)
    keep = [
        ln for ln in body.splitlines(keepends=True)
        if not ln.startswith(("from __future__", "from typing import", "from typing "))
    ]
    return "".join(keep).strip("\n") + "\n"


def extract_symbols(text: str, names: list[str]) -> str:
    """The named top-level symbols, in source order, with their comments.

    Used for the chat preload: condor keeps it in ``runtime/context.py`` next to
    a builder that pulls in the agent store, the server registry and the memory
    layer — none of which bench wants. Taking the four symbols by AST keeps the
    copy mechanical without vendoring the module around them.
    """
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    chunks = []
    for node in tree.body:
        got = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            got = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    got = t.id
        if not got:
            continue
        # Walk back over the comment block immediately above the symbol.
        start = node.lineno - 1
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        chunks.append("".join(lines[start:node.end_lineno]))
    missing = [n for n in names if f"{n}" not in "".join(chunks)]
    if missing:
        raise SystemExit(
            f"revendor: {missing} not found upstream — condor moved or renamed them. "
            "Find where they live now and update PRELOAD_SYMBOLS."
        )
    return "\n\n".join(c.rstrip("\n") for c in chunks) + "\n"


PRELOAD_SYMBOLS = [
    "_CHAT_MCP_SERVERS",
    "_CHAT_PRELOAD_PROFILE",
    "_chat_mcp_tools",
    "chat_tool_preload",
]

CONTEXT_EXTRA = '''

def build_consult_preload(agent_slug: str | None, agent_key: str) -> str:
    """The chat seat's preload for a bench consult case.

    Consult cases are chat-shaped, so they must get the *chat* preload — the 42
    tools ``chat_tool_preload`` names — and not the tick seat's deliberately
    narrower ring, which condor says in as many words must not be unified with
    it. Running a consult on the tick preload is what cost the portfolio case a
    second ToolSearch: ``get_portfolio_overview`` is not in the tick list.
    """
    return chat_tool_preload(agent_key, agent_slug)
'''


def render_profiles(src: Path, rev: str) -> str:
    return HEADER.format(src=src, rev=rev) + strip_module_docstring(src.read_text())


def generate(condor: Path) -> dict[Path, str]:
    rev = describe(condor)
    out: dict[Path, str] = {}

    src = condor / "condor" / "agents" / "prompts.py"
    body = rewrite_imports(strip_module_docstring(src.read_text()))
    out[ROOT / "condor_compat" / "agents" / "prompts.py"] = (
        HEADER.format(src="condor/agents/prompts.py", rev=rev)
        + body.rstrip("\n") + "\n\n\n" + MARKER + "\n" + bench_section()
    )

    ctx = condor / "condor" / "runtime" / "context.py"
    out[ROOT / "condor_compat" / "runtime" / "context.py"] = (
        HEADER.format(src="condor/runtime/context.py (chat preload only)", rev=rev)
        + "from __future__ import annotations\n\n\n"
        + rewrite_imports(extract_symbols(ctx.read_text(), PRELOAD_SYMBOLS))
        + CONTEXT_EXTRA
    )

    # AGENT.md is injected verbatim as the Condor system prompt, body only — the
    # frontmatter is condor's own loader metadata and is not part of the prompt.
    md = (condor / "agents" / "condor" / "AGENT.md").read_text()
    out[ROOT / "condor_compat" / "agents" / "condor" / "AGENT.md"] = (
        md.split("---", 2)[2].lstrip("\n") if md.startswith("---") else md
    )

    for rel, dest in (
        (("mcp_servers", "condor", "profiles.py"), "condor_profiles.py"),
        (("mcp_servers", "hummingbot_api", "profiles.py"), "hummingbot_profiles.py"),
    ):
        p = condor.joinpath(*rel)
        out[ROOT / "condor_compat" / "mcp_servers" / dest] = render_profiles(p, rev)

    return out


def describe(condor: Path) -> str:
    import subprocess

    try:
        r = subprocess.run(
            ["git", "-C", str(condor), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10,
        )
        branch = subprocess.run(
            ["git", "-C", str(condor), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return f"{r.stdout.strip()} ({branch.stdout.strip()})"
    except Exception:
        return "unknown"


def resolve_condor(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    from config import condor_path

    p = condor_path()
    if p is None:
        raise SystemExit(
            "revendor: no condor checkout. Pass --condor PATH or set CONDOR_PATH."
        )
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--condor", help="condor checkout (default: config.condor_path())")
    ap.add_argument("--check", action="store_true", help="diff only; non-zero if stale")
    args = ap.parse_args()

    condor = resolve_condor(args.condor)
    if not (condor / "condor" / "agents" / "prompts.py").is_file():
        raise SystemExit(f"revendor: {condor} is not a condor checkout")

    generated = generate(condor)
    stale = False
    for path, text in generated.items():
        current = path.read_text() if path.exists() else ""
        if current == text:
            continue
        stale = True
        rel = path.relative_to(ROOT)
        if args.check:
            print(f"--- {rel} is stale")
            print("".join(difflib.unified_diff(
                current.splitlines(keepends=True), text.splitlines(keepends=True),
                "current", "regenerated", n=1))[:4000])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {rel}")

    if args.check:
        print("stale — run: uv run python scripts/revendor.py" if stale
              else f"up to date with condor {describe(condor)}")
        return 1 if stale else 0

    if not stale:
        print(f"already up to date with condor {describe(condor)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
