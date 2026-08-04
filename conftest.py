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
