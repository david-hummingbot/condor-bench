# Handoff: merging in the router/matrix dashboard pages

**Status:** Unlocated. This PR does not contain the router/matrix work — it was built by a different LLM session on a local branch on a different machine, not pushed anywhere this session had access to. Whoever picks this up needs to physically get that branch (ask the user, or check that machine) before anything below is actionable.

## Context — what this PR actually contains

This branch (`sync/condor-agent-redesign-and-everyday-dataset`) is a sync + dataset refactor, unrelated in content to the router/matrix pages:

- Re-vendored `condor_compat/agents/condor/AGENT.md` from condor's current `agents/condor/AGENT.md` (condor deleted its `routine_builder` agent and moved routine authoring to `delegate(agent="condor")` + the `routine_cookbook` skill — 189 commits of drift since the last sync).
- Synced `mock_mcp/hummingbot_server.py` and `mock_mcp/condor_server.py` to condor's current MCP tool surface; regenerated `datasets/tool_surface.json`.
- Replaced the old 23-case `datasets/consult.jsonl` with 15 new "everyday usage" cases (`c001`–`c015`) — simple, single-tool-call status lookups (portfolio balance, active server, market price, etc.), rewrote `datasets/EXPECTED_RESPONSES.md` to match.
- Fixed a corrupted `node_modules/vite` install that was blocking `make dashboard`.
- Fixed dashboard staleness: `dashboard/frontend/src/casePrompts.json` was showing wrong questions for the reused `c001`–`c015` ids; `RunConfig.jsx` had dead category filters (`strategy-creation`/`routine-builder`) wired to categories that no longer exist; the Anthropic model list in `dashboard/backend/app.py` and the baseline/judge defaults in `config.py` were a generation behind (`claude-opus-4-8`/`claude-sonnet-4-6` → `claude-opus-5`/`claude-sonnet-5`).
- Regenerated `baseline/*.json` against the new dataset (old files held latency timings for different questions under the same filenames).
- Fixed README drift (dataset table, project-layout tree, stale CLI examples).

Added a roadmap section to `FRAMEWORK_IMPROVEMENTS.md` §10 proposing (not building) two future dashboard views:

- **Per-tool competency matrix** — per-model × per-tool accuracy table.
- **Capability → recommendation report** — cross-tabulate scores by tool/agent/category to answer "what's the cheapest model that's reliably correct for X."

**This is almost certainly what "the router and matrix page" refers to.** If the other LLM's branch built dashboard UI for either of these, it's realizing this roadmap item — check whether it's named after "matrix" (the per-tool table) and/or "router" (possibly a view of condor's `[SKILLS]`/`[AGENTS]` routing behavior, or a "which model routes to which tool" recommendation view — the roadmap doesn't specify a name, so confirm against the actual code once you have it).

## What the next agent needs to do

1. **Locate the branch.** Ask the user directly — as of this handoff it's known to be local-only on a different machine, not pushed to `origin` (verified: `git branch -a` and `git ls-remote origin` on this repo show no other branches at the time of this PR). Get it pushed somewhere fetchable (a branch on `origin`, a patch file, or a zip of the changed files) before attempting a merge.
2. **Diff it against this PR's base**, not against a stale `master` — this PR changes `dashboard/frontend/src/components/RunConfig.jsx`, `dashboard/frontend/src/casePrompts.json`, and `dashboard/backend/app.py`, all of which a new dashboard page might also touch (e.g. adding a new tab to `App.jsx`, a new `PROVIDERS`-adjacent endpoint). Rebase or merge carefully — don't blindly overwrite the fixes in this PR with the other branch's older copies of those files.
3. **Check `dashboard/backend/app.py` for a new endpoint** the matrix/router page would need (e.g. something aggregating `results/*/summary.json` across runs by tool/category) and confirm it's compatible with the current `/api/runs` shape.
4. **Check `dashboard/frontend/src/App.jsx`** for how tabs are registered (currently: Run, Live, Leaderboard, Runs) and add the new tab(s) there if the other branch didn't already wire them in cleanly.
5. **Re-run `make check-drift` and the frontend build** (`cd dashboard/frontend && npm install && npm run build`) after merging — this PR left both green; confirm the merge doesn't reintroduce a build break.
6. **Delete this file** once the router/matrix work has been located, merged, and verified — it's a one-time pointer, not permanent documentation.

## Known-good baseline to merge onto

Commit `0b4eb04` on this branch (`sync/condor-agent-redesign-and-everyday-dataset`) — full test suite passes (`uv run pytest -q --ignore=tests/test_scoring_fixes.py`, 8/8), frontend builds clean, `make check-drift` is green.
