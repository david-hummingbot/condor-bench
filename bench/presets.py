"""Experiment presets (``experiments/*.yaml``) and how a suite takes one on.

A preset is the declaration of an experiment: which question it answers (A — did
Condor improve? B — which model is best?), what it freezes, and what it is allowed
to vary. Attaching one to a suite (``suite["preset"]`` or ``suite run --preset``)
sets the suite's ``experiment_type`` and ``freeze`` block, and
:func:`bench.suite_runner.check_experiment_freeze` then refuses to start a run
that breaks it. A malformed preset is refused at load, before anything runs: a
preset that says "A" but varies the model is not an experiment A.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config import ROOT

EXPERIMENTS_DIR = ROOT / "experiments"

#: What each experiment type may vary. Anything else in ``vary`` is refused.
VARIABLE = {
    "A": {"condor_checkout", "contract_version", "prompt", "routing"},
    "B": {"model_id", "inference_config"},
}
_INFERENCE_KEYS = {"temperature", "top_p", "max_tokens", "reasoning", "seed"}


class PresetError(ValueError):
    """The preset file does not describe a valid experiment."""


def preset_path(name: str) -> Path:
    path = Path(name)
    if path.suffix in (".yaml", ".yml") and path.is_file():
        return path
    return EXPERIMENTS_DIR / f"{name}.yaml"


def list_presets() -> list[str]:
    return sorted(p.stem for p in EXPERIMENTS_DIR.glob("*.yaml"))


def load_preset(name: str) -> dict[str, Any]:
    """Read and validate a preset. Returns ``{name, experiment_type, freeze, vary, trials}``."""
    path = preset_path(name)
    if not path.is_file():
        raise PresetError(f"No preset {name!r} (looked for {path}). Known: {', '.join(list_presets())}")
    data = yaml.safe_load(path.read_text()) or {}
    exp = str(data.get("experiment_type") or "")
    if exp not in VARIABLE:
        raise PresetError(f"{path.name}: experiment_type must be A or B, got {exp!r}")
    freeze = dict(data.get("freeze") or {})
    vary = [str(v) for v in (data.get("vary") or [])]
    illegal = sorted(set(vary) - VARIABLE[exp])
    if illegal:
        raise PresetError(
            f"{path.name}: experiment {exp} cannot vary {illegal} — it may vary only "
            f"{sorted(VARIABLE[exp])}"
        )
    if exp == "A":
        if not freeze.get("model_id"):
            raise PresetError(f"{path.name}: experiment A must pin freeze.model_id")
        unknown = set(freeze.get("inference_config") or {}) - _INFERENCE_KEYS
        if unknown:
            raise PresetError(f"{path.name}: unknown inference_config keys {sorted(unknown)}")
    if exp == "B" and freeze.get("require_clean") is False:
        raise PresetError(f"{path.name}: experiment B freezes Condor, so require_clean cannot be false")
    trials = data.get("trials")
    if trials is not None and (not isinstance(trials, int) or trials < 1):
        raise PresetError(f"{path.name}: trials must be a positive integer")
    return {"name": path.stem, "experiment_type": exp, "freeze": freeze, "vary": vary, "trials": trials}


def apply_preset(
    suite: dict[str, Any], name: str, *, experiment_type: str | None = None, adhoc: bool = False
) -> dict[str, Any]:
    """The suite as the preset declares it. Refuses contradictions with the caller."""
    preset = load_preset(name)
    if adhoc:
        raise PresetError(
            f"preset {preset['name']!r} declares experiment {preset['experiment_type']}; "
            "--adhoc would discard its freeze rules — run without the preset instead"
        )
    if experiment_type and experiment_type != preset["experiment_type"]:
        raise PresetError(
            f"--experiment {experiment_type} contradicts preset {preset['name']!r} "
            f"(experiment {preset['experiment_type']})"
        )
    out = dict(suite)
    out["preset"] = preset["name"]
    out["experiment_type"] = preset["experiment_type"]
    out["freeze"] = preset["freeze"]
    if preset["trials"]:
        out["trials"] = preset["trials"]
    return out
