# Experiment Presets

Experiments are disciplined runs that answer one of two questions.

## Experiment A — "Did Condor improve?"

Freeze the model and its inference config.  Vary **one of**: Condor checkout,
contract version supplied by that checkout, prompt, or routing.

| What is frozen                        | What is varied                              |
|---------------------------------------|---------------------------------------------|
| `model_id` and `inference_config`     | One of: checkout, contract version, prompt, routing |

**Fail closed**: start refuses to run if `model_id` or `inference_config`
differs across suite members.

**Output**: uplift on a fixed model.

## Experiment B — "Which model is best?"

Freeze Condor: clean tree, pinned commit, contract version, prompt and guide
hashes, runtime settings.  Vary the model and its supported inference
parameters.

| What is frozen                                                                 | What is varied                  |
|--------------------------------------------------------------------------------|---------------------------------|
| Condor commit, clean tree, `contract_version`, prompt hashes, runtime settings | Model and its inference params  |

**Fail closed**: start refuses to run if the tree is dirty or the commit does
not match the pin.  A local `--adhoc` opt-out exists and marks the run
`experiment_type: adhoc`.

**Output**: model-by-task efficiency frontier.  Not one global winner.

## Ad-hoc runs

Dashboard runs that do not use a preset are `experiment_type: adhoc`.  They
stay out of published frontiers.  Use them for iteration and debugging.

## Presets

- [`a_example.yaml`](a_example.yaml) — Experiment A template
- [`b_example.yaml`](b_example.yaml) — Experiment B template

Attach a preset with `uv run python runner.py suite run <suite> --preset a_example`,
or save it on the suite as `"preset": "a_example"`. `bench/presets.py` validates
the file at load — the type must be A or B, `vary` may only name what that type
varies, A must pin `model_id`, B must keep `require_clean` — and the suite
orchestrator (`bench/suite_runner.py`) enforces the freeze block at start time.
A preset cannot be combined with `--adhoc`, or with an `--experiment` of the
other type. Runs record the preset name in their manifest.
