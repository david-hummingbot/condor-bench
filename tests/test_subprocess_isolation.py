"""Spike: two CONDOR_PATH values in separate subprocesses load distinct modules.

Run with:
  CONDOR_PATH_A=/path/to/condor-a CONDOR_PATH_B=/path/to/condor-b \\
    uv run python -m pytest tests/test_subprocess_isolation.py -q

Skips when the two paths are not provided or identical.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["BENCH_ROOT"])
from config import build_run_pin
from bench.mcp_provider import load_condor_shared
load_condor_shared()
pin = build_run_pin(run_type="spike", shared_loaded=True)
print(json.dumps(pin["condor"]["loaded"]))
"""


def _probe(condor_path: str) -> dict:
    env = os.environ.copy()
    env["CONDOR_PATH"] = condor_path
    env["BENCH_ROOT"] = str(ROOT)
    proc = subprocess.run(
        ["uv", "run", "python", "-c", PROBE],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    line = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
    return json.loads(line)


@pytest.mark.skipif(
    not (os.environ.get("CONDOR_PATH_A") and os.environ.get("CONDOR_PATH_B")),
    reason="Set CONDOR_PATH_A and CONDOR_PATH_B to two distinct condor checkouts",
)
def test_two_subprocesses_load_distinct_shared_py():
    path_a = os.environ["CONDOR_PATH_A"]
    path_b = os.environ["CONDOR_PATH_B"]
    if Path(path_a).resolve() == Path(path_b).resolve():
        pytest.skip("CONDOR_PATH_A and CONDOR_PATH_B must be different checkouts")

    loaded_a = _probe(path_a)
    loaded_b = _probe(path_b)

    assert loaded_a["shared_py"] != loaded_b["shared_py"]
    assert loaded_a["config_yml"] != loaded_b["config_yml"]
    assert str(Path(path_a).resolve()) in loaded_a["shared_py"]
    assert str(Path(path_b).resolve()) in loaded_b["shared_py"]
