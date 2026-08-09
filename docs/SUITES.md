# Suites — Condor branch A/B

Dashboard-first Environments and Suites. See the **Suites** section in [README.md](../README.md).

## Worker contract

Each Environment member runs in `scripts/suite_worker.py` with:

| Env / arg | Meaning |
|-----------|---------|
| `CONDOR_PATH` | Absolute Condor checkout (required; no silent `../condor` fallback in the worker) |
| `BENCH_MODE` | `live` \| `mock` |
| `BENCH_SERVER_NAME` | Staging server name in that checkout's `config.yml` |
| `--job path.json` | Suite id, environment id, run_group_id, model, case_ids, branch pins |

The API process must **not** import condor's `_shared` for a second checkout in-process — that is the bug subprocess isolation fixes.

## Future MCP server

Wrap the same Python modules as `/api/environments`, `/api/suites`, `/api/suites/{id}/run`, `/api/compare`. Do not duplicate business logic in the MCP layer.
