"""Make the repo root importable for tests, whatever interpreter runs pytest.

`bench/` and `metrics/` import `config` as a top-level module, which only resolves
with the repo root on `sys.path`. An editable install provides that — but only for
the interpreter it was installed into, and `pytest` resolved off `PATH` can easily
be a different one (a system or conda pytest, say). That failed at *collection*,
which reads as "the test file is broken" rather than "wrong interpreter".

Prepending the root here makes the suite independent of how it was launched.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import os

import pytest


@pytest.fixture(autouse=True)
def _restore_process_environment():
    """Undo whatever a test leaves in ``os.environ``.

    Code under test writes there on purpose — ``settings_store.update_settings``
    applies a saved setting to the running process, which is what the dashboard
    needs. In a test that write outlived the test: a group chat id saved by one
    test mirrored into ``BENCH_USER_ID`` and made a later test read it as an
    explicit user id, so the suite passed or failed depending on which tests ran
    before it.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
