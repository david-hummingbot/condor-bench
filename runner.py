"""CLI for condor-bench."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

app = typer.Typer(add_completion=False)
console = Console()

LAYER_CHOICES = ("consult", "tick", "tool", "agent")


def _resolve_layers(
    consult_only: bool, tick_only: bool, layers: Optional[str]
) -> Optional[list[str]]:
    if layers:
        chosen = [layer.strip() for layer in layers.split(",") if layer.strip()]
        unknown = [layer for layer in chosen if layer not in LAYER_CHOICES]
        if unknown:
            console.print(
                f"[red]Unknown layer(s): {', '.join(unknown)}. "
                f"Choose from {', '.join(LAYER_CHOICES)}.[/red]"
            )
            raise typer.Exit(2)
        return chosen
    if consult_only:
        return ["consult"]
    if tick_only:
        return ["tick"]
    return None


def _max_risk_for_mode() -> tuple[str | None, str | None]:
    """Risk ceiling for this run, plus a note when cases are being dropped.

    Live mode without ``BENCH_ALLOW_MUTATING`` is limited to read-only cases. That
    is not a silent filter: it changes which domains have enough evidence to earn a
    routing recommendation, so the caller is told.
    """
    from config import bench_mode, staging_config

    if bench_mode() != "live":
        return None, None
    if staging_config()["allow_mutating"]:
        return None, None
    return (
        "read_only",
        "live mode with BENCH_ALLOW_MUTATING unset — running read-only cases only",
    )


@app.command()
def baseline(
    overwrite: bool = typer.Option(False, help="Regenerate existing baselines"),
    model: str = typer.Option(None, help="Override the baseline model"),
) -> None:
    """Generate baseline latency records using the benchmark model."""
    from config import BASELINE_MODEL
    from bench.baseline import BaselineStore, generate_baselines
    from bench.dataset import load_all_cases

    m = model or BASELINE_MODEL
    cases = load_all_cases()
    store = BaselineStore()

    async def _run():
        await generate_baselines(cases, store, model=m, overwrite=overwrite)

    asyncio.run(_run())


@app.command("staging-check")
def staging_check() -> None:
    """Run the live-mode pre-flight and print every check.

    Exits non-zero when a blocking check fails, so CI and the Makefile can gate a
    live run on it.
    """
    from bench.staging_health import check_staging, format_report

    report = asyncio.run(check_staging())
    console.print(format_report(report))
    if report.mode != "live":
        return
    if not report.ok:
        console.print("\n[red]Blocking checks failed — live runs are refused.[/red]")
        raise typer.Exit(1)
    if report.allow_mutating and not report.mutating_ok:
        console.print(
            "\n[yellow]Read-only runs are allowed; mutating cases stay blocked.[/yellow]"
        )
        raise typer.Exit(1)
    console.print("\n[green]Staging pre-flight passed.[/green]")


@app.command()
def test(
    model: str = typer.Argument(..., help="Model to benchmark, e.g. ollama:llama3.1:8b"),
    category: Optional[str] = typer.Option(None, "-c", help="Filter by category"),
    domain: Optional[str] = typer.Option(None, "-d", help="Filter by routing domain"),
    layers: Optional[str] = typer.Option(
        None, help=f"Comma-separated dataset layers ({', '.join(LAYER_CHOICES)})"
    ),
    consult_only: bool = typer.Option(False, help="Only consult cases"),
    tick_only: bool = typer.Option(False, help="Only tick cases"),
    mode: Optional[str] = typer.Option(None, help="Override BENCH_MODE (live | mock)"),
) -> None:
    """Run benchmarks against a model and score vs baseline latency."""
    import os
    import uuid

    if mode:
        os.environ["BENCH_MODE"] = mode

    from bench.baseline import BaselineStore
    from bench.dataset import case_prompt_map, filter_cases, load_all_cases
    from bench.mcp_provider import mode_banner
    from bench.reporter import save_run
    from config import PASS_THRESHOLD, bench_mode, build_run_pin

    resolved_mode = bench_mode()
    if resolved_mode == "live":
        from bench.staging_health import StagingUnhealthy, assert_ready

        try:
            assert_ready(mutating=False)
        except StagingUnhealthy as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    max_risk, risk_note = _max_risk_for_mode()
    cases = filter_cases(
        load_all_cases(),
        domain=domain,
        category=category,
        layers=_resolve_layers(consult_only, tick_only, layers),
        max_risk=max_risk,
    )
    if not cases:
        console.print("[red]No cases matched the filters.[/red]")
        raise typer.Exit(1)
    if risk_note:
        console.print(f"[yellow]{risk_note}[/yellow]")

    store = BaselineStore()
    prompts = case_prompt_map()
    missing = store.missing([c.id for c in cases])
    if missing:
        console.print(
            f"[yellow]Warning: {len(missing)} cases have no baseline latency "
            f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}). "
            "Run 'make baseline' first.[/yellow]"
        )

    run_id = uuid.uuid4().hex[:8]
    console.print(f"\nBenchmarking [bold]{model}[/bold] on {len(cases)} cases (run {run_id})")
    console.print(f"  mode: {mode_banner()}\n")

    scorecards, responses = asyncio.run(_run_cases(cases, model, store, resolved_mode))

    pin = build_run_pin(
        run_type="adhoc",
        case_ids=[c.id for c in cases],
        models=[model],
        mode=resolved_mode,
        risk_ceiling=max_risk,
        shared_loaded=resolved_mode == "live",
    )
    run_dir = save_run(
        model,
        scorecards,
        responses,
        run_id,
        prompts=prompts,
        extra_summary=pin,
    )
    console.print(f"\n[bold]Run saved:[/bold] {run_dir}")
    _print_summary(scorecards, model, PASS_THRESHOLD)


async def _run_cases(cases, model: str, store, mode: str):
    """Run and score every case, returning (scorecards, responses)."""
    from bench.cleanup import teardown
    from bench.client import run_case
    from bench.dataset import is_mutating
    from bench.scorer import score_case
    from config import PASS_THRESHOLD

    scorecards, responses = [], {}
    for case in cases:
        console.print(f"  [dim]{case.id}[/dim] ({case.type})", end=" ")
        try:
            result = await run_case(case, model, mode=mode)
            baseline = store.load(case.id)
            baseline_latency = baseline.latency_s if baseline else result.latency_s
            card = await score_case(case, result, baseline_latency, mode=mode)
            scorecards.append(card)
            responses[case.id] = result.response

            status = (
                f"[green]{card.composite:.2f}[/green]"
                if card.composite >= PASS_THRESHOLD
                else f"[red]{card.composite:.2f}[/red]"
            )
            extra = ""
            if card.usage.get("total_tokens"):
                extra = f", {card.usage['total_tokens']:,} tok"
            console.print(f"→ {status} ({result.latency_s:.1f}s{extra})")
            if card.harness_artifact:
                console.print(f"      [yellow]harness: {card.harness_artifact}[/yellow]")

            # Undo what a mutating live case created, whatever its score — an
            # aborted half-created resource still needs removing.
            if mode == "live" and is_mutating(case):
                report = await teardown(
                    result, model, agent_slug=getattr(case, "agent_slug", None), mode=mode
                )
                if report.removed:
                    console.print(f"      [dim]cleaned up {len(report.removed)} resource(s)[/dim]")
                for row in report.failed + report.manual:
                    console.print(
                        f"      [yellow]left behind: {row.get('tool')} "
                        f"{row.get('identifier')} — {row.get('error') or row.get('reason', 'manual')}[/yellow]"
                    )
        except Exception as exc:
            console.print(f"[red]ERROR: {exc}[/red]")
    return scorecards, responses


@app.command()
def sweep(
    models: Path = typer.Option(
        Path("datasets/models.json"), help="Model registry to sweep"
    ),
    domain: Optional[str] = typer.Option(None, "-d", help="Only this routing domain"),
    layers: Optional[str] = typer.Option(
        None, help=f"Comma-separated dataset layers ({', '.join(LAYER_CHOICES)})"
    ),
    only: Optional[str] = typer.Option(
        None, help="Comma-separated model keys — sweep just these from the registry"
    ),
    mode: Optional[str] = typer.Option(None, help="Override BENCH_MODE (live | mock)"),
    max_params_b: Optional[float] = typer.Option(
        None, help="Skip models larger than this (cloud models are never skipped)"
    ),
) -> None:
    """Benchmark every model in the registry, smallest first."""
    import os
    import uuid

    if mode:
        os.environ["BENCH_MODE"] = mode

    from bench.baseline import BaselineStore
    from bench.dataset import case_prompt_map, filter_cases, load_all_cases
    from bench.matrix import load_models
    from bench.mcp_provider import mode_banner
    from bench.reporter import save_run
    from config import PASS_THRESHOLD, bench_mode, build_run_pin

    resolved_mode = bench_mode()
    if resolved_mode == "live":
        from bench.staging_health import StagingUnhealthy, assert_ready

        try:
            assert_ready(mutating=False)
        except StagingUnhealthy as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    registry = load_models(models)
    if not registry:
        console.print(f"[red]No models found in {models}.[/red]")
        raise typer.Exit(1)

    if only:
        wanted = {k.strip() for k in only.split(",") if k.strip()}
        unknown = wanted - {m.key for m in registry}
        if unknown:
            console.print(f"[red]Not in the registry: {', '.join(sorted(unknown))}[/red]")
            raise typer.Exit(2)
        registry = [m for m in registry if m.key in wanted]
    if max_params_b is not None:
        registry = [
            m for m in registry if m.params_b is None or m.params_b <= max_params_b
        ]

    max_risk, risk_note = _max_risk_for_mode()
    cases = filter_cases(
        load_all_cases(),
        domain=domain,
        layers=_resolve_layers(False, False, layers),
        max_risk=max_risk,
    )
    if not cases:
        console.print("[red]No cases matched the filters.[/red]")
        raise typer.Exit(1)
    if risk_note:
        console.print(f"[yellow]{risk_note}[/yellow]")

    store = BaselineStore()
    prompts = case_prompt_map()
    console.print(
        f"\nSweeping [bold]{len(registry)}[/bold] model(s) × {len(cases)} case(s)"
    )
    console.print(f"  mode: {mode_banner()}\n")

    completed: list[tuple[str, Path]] = []
    for entry in registry:
        size = f"{entry.params_b}B" if entry.params_b else entry.provider
        console.print(f"[bold]{entry.key}[/bold] ({size})")
        scorecards, responses = asyncio.run(
            _run_cases(cases, entry.key, store, resolved_mode)
        )
        if not scorecards:
            console.print("  [yellow]no cases scored — skipping save[/yellow]")
            continue
        pin = build_run_pin(
            run_type="adhoc",
            case_ids=[c.id for c in cases],
            models=[entry.key],
            mode=resolved_mode,
            risk_ceiling=max_risk,
            shared_loaded=resolved_mode == "live",
        )
        pin["sweep"] = True
        run_dir = save_run(
            entry.key,
            scorecards,
            responses,
            uuid.uuid4().hex[:8],
            prompts=prompts,
            extra_summary=pin,
        )
        completed.append((entry.key, run_dir))
        _print_summary(scorecards, entry.key, PASS_THRESHOLD)
        console.print("")

    console.print(f"[bold]Sweep complete:[/bold] {len(completed)} run(s) saved")
    if completed:
        console.print("Build the matrix with:  uv run python runner.py matrix")


@app.command()
def matrix(
    mode: Optional[str] = typer.Option(
        None, help="Only aggregate runs from this mode (live | mock)"
    ),
    models: Path = typer.Option(Path("datasets/models.json"), help="Model registry"),
    axis: str = typer.Option("domains", help="Rows: domains | tools"),
    all_models: bool = typer.Option(
        False,
        "--all-models",
        help="Include models absent from the registry (the JSON always has them)",
    ),
) -> None:
    """Aggregate saved runs into a model × domain/tool matrix."""
    from bench.matrix import build_matrix, save_matrix

    if axis not in ("domains", "tools"):
        console.print("[red]--axis must be 'domains' or 'tools'.[/red]")
        raise typer.Exit(2)

    data = build_matrix(mode=mode, models_path=models)
    if not data["models"]:
        console.print("[yellow]No runs found. Run 'sweep' or 'test' first.[/yellow]")
        raise typer.Exit(1)

    path = save_matrix(data)
    console.print(f"[bold]Matrix saved:[/bold] {path}\n")

    ordered = _ordered_models(data)
    unranked = [k for k, v in data["models"].items() if not v.get("in_registry")]
    if not all_models:
        # A terminal can't render 15 model columns legibly, and the models that
        # matter for a routing decision are the ranked ones. The saved JSON and the
        # dashboard heatmap keep every model either way.
        ordered = [k for k in ordered if data["models"][k].get("in_registry")]
    if not ordered:
        console.print(
            f"[yellow]None of the benchmarked models are in {models}, so there is "
            "nothing rankable to show. Add them, or pass --all-models.[/yellow]"
        )
        ordered = _ordered_models(data)

    table = Table(title=f"Pass rate by {axis[:-1]} ({data['mode']} runs)")
    table.add_column("Domain" if axis == "domains" else "Tool", no_wrap=True)
    for key in ordered:
        info = data["models"][key]
        size = f"{info['params_b']}B" if info.get("params_b") else "cloud"
        table.add_column(f"{_short_model(key)}\n{size}", justify="right", no_wrap=True)

    for row_name, cells in data[axis].items():
        if not any(cells.get(k) for k in ordered):
            continue
        table.add_row(row_name, *[_cell_label(cells.get(k)) for k in ordered])
    console.print(table)
    console.print(
        "[dim]cell = pass rate (scored cases). "
        "'excl' = excluded infra failures / harness artifacts. "
        "'!' = a destructive case below the floor.[/dim]"
    )

    if unranked and not all_models:
        console.print(
            f"\n[yellow]{len(unranked)} model(s) hidden — not in {models}, so they "
            "cannot be ranked by size or routed. Pass --all-models to show them:[/yellow]"
        )
        console.print(f"  [dim]{', '.join(unranked)}[/dim]")


def _short_model(key: str) -> str:
    """Drop the provider prefix so a column header fits a terminal."""
    return key.split(":", 1)[1] if ":" in key else key


def _ordered_models(data: dict) -> list[str]:
    """Registry order (smallest first), with unranked models last."""
    def sort_key(key: str):
        info = data["models"][key]
        params = info.get("params_b")
        in_registry = bool(info.get("in_registry"))
        return (
            0 if in_registry else 1,
            float(params) if params is not None else float("inf"),
            key,
        )

    return sorted(data["models"], key=sort_key)


def _cell_label(cell: dict | None) -> str:
    if not cell or not cell.get("scored"):
        excluded = (cell or {}).get("excluded") or 0
        return "[dim]—[/dim]" if not excluded else f"[yellow]{excluded} excl[/yellow]"
    rate = cell.get("pass_rate")
    if rate is None:
        return "[dim]—[/dim]"
    colour = "green" if rate >= 0.8 else ("yellow" if rate >= 0.5 else "red")
    suffix = f" [dim]({cell['scored']})[/dim]"
    if cell.get("destructive_failures"):
        suffix += " [red]![/red]"
    return f"[{colour}]{rate:.0%}[/{colour}]{suffix}"


@app.command()
def route(
    min_pass_rate: float = typer.Option(
        0.80, help="Pass rate a model needs to own a domain"
    ),
    min_cases: int = typer.Option(
        3, help="Minimum scored cases before a domain verdict counts as evidence"
    ),
    prefer_lower_tokens: bool = typer.Option(
        False,
        help="Break ties between equally-small models by average token use "
        "(never rejects a passing model)",
    ),
    mode: Optional[str] = typer.Option(None, help="Only use runs from this mode"),
    models: Path = typer.Option(Path("datasets/models.json"), help="Model registry"),
) -> None:
    """Generate routing recommendations: the smallest model that passes each domain."""
    from bench.matrix import save_matrix
    from bench.routing import generate, save_routing

    matrix_data, routing = generate(
        mode=mode,
        min_pass_rate=min_pass_rate,
        min_cases=min_cases,
        prefer_lower_tokens=prefer_lower_tokens,
        models_path=models,
    )
    if not matrix_data["models"]:
        console.print("[yellow]No runs found. Run 'sweep' or 'test' first.[/yellow]")
        raise typer.Exit(1)

    save_matrix(matrix_data)
    path = save_routing(routing)
    console.print(f"[bold]Routing saved:[/bold] {path}\n")

    table = Table(title="Recommended model per domain")
    table.add_column("Domain")
    table.add_column("Model")
    table.add_column("Size", justify="right")
    table.add_column("Pass", justify="right")
    table.add_column("Avg tokens", justify="right")
    table.add_column("Rationale")
    for domain, rec in routing["recommendations"].items():
        tokens = rec.get("avg_total_tokens")
        note = rec["rationale"]
        if rec.get("no_local_passed"):
            note += " (no local model passed)"
        table.add_row(
            domain,
            rec["model"],
            f"{rec['params_b']}B" if rec.get("params_b") else "cloud",
            f"{rec['pass_rate']:.0%}" if rec.get("pass_rate") is not None else "—",
            f"{tokens:,.0f}" if tokens else "[dim]n/a[/dim]",
            note,
        )
    if routing["recommendations"]:
        console.print(table)
    else:
        console.print("[yellow]No domain earned a recommendation.[/yellow]")

    for domain, gap in routing["unmet_domains"].items():
        best = gap.get("best_attempt") or {}
        console.print(
            f"[yellow]unmet[/yellow] {domain}: {gap['reason']}"
            + (
                f" — best was {best.get('model')} at "
                f"{(best.get('pass_rate') or 0):.0%}"
                if best
                else ""
            )
        )

    for domain, info in routing.get("stale_domains", {}).items():
        console.print(
            f"[dim]stale[/dim] {domain}: {info['reason']} "
            f"({', '.join(info['models_with_results']) or 'no scored results'})"
        )

    gaps = routing["tool_gaps"]["unhandled"]
    if gaps:
        console.print(f"\n[yellow]No model passes these tools:[/yellow] {', '.join(gaps)}")
    if routing["unranked_models"]:
        console.print(f"\n[yellow]{routing['unranked_note']}[/yellow]")
        console.print(f"  {', '.join(routing['unranked_models'])}")

    if routing["condor_config_snippet"]:
        console.print("\n[bold]Condor config:[/bold]")
        for key, value in routing["condor_config_snippet"].items():
            console.print(f"  {key} = {value}")
    for key, conflict in routing.get("config_conflicts", {}).items():
        per_domain = ", ".join(f"{d}→{m}" for d, m in conflict["per_domain"].items())
        console.print(
            f"\n[yellow]{key} is shared by domains that disagree ({per_domain}); "
            f"used {conflict['chosen']}.[/yellow]"
        )


@app.command()
def report() -> None:
    """Print a summary table of all benchmark runs."""
    from bench.reporter import load_all_runs
    runs = load_all_runs()
    if not runs:
        console.print("[yellow]No runs found. Run 'make test MODEL=...' first.[/yellow]")
        return
    table = Table(title="Benchmark runs")
    table.add_column("Run dir")
    table.add_column("Model")
    table.add_column("Mode")
    table.add_column("Composite", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Params", justify="right")
    table.add_column("Latency score", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cases", justify="right")
    for r in sorted(runs, key=lambda x: x.get("composite_avg", 0), reverse=True):
        usage = r.get("usage") or {}
        avg_tokens = usage.get("avg_total_tokens")
        table.add_row(
            r.get("run_dir", ""),
            r.get("model", ""),
            r.get("mode", "mock"),
            f"{r.get('composite_avg', 0):.2f}",
            f"{r.get('answer_quality_avg', 0):.2f}",
            f"{r.get('tool_accuracy_avg', 0):.2f}" if r.get("tool_accuracy_avg") is not None else "—",
            f"{r.get('tool_params_avg', 0):.2f}" if r.get("tool_params_avg") is not None else "—",
            f"{r.get('latency_score_avg', 0):.2f}",
            f"{avg_tokens:,.0f}" if avg_tokens else "—",
            str(r.get("cases_scored", "?")),
        )
    console.print(table)


@app.command()
def dashboard() -> None:
    """Launch the React dashboard (builds frontend then starts FastAPI)."""
    import subprocess, sys, os
    root = Path(__file__).parent
    subprocess.run(["npm", "install", "--silent"], cwd=root / "dashboard" / "frontend", check=True)
    subprocess.run(["npm", "run", "build"], cwd=root / "dashboard" / "frontend", check=True)
    console.print("\n  Dashboard → [link]http://localhost:8001[/link]\n")
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "dashboard.backend.app:app", "--port", "8001"])


def _print_summary(scorecards, model: str, pass_threshold: float) -> None:
    if not scorecards:
        return
    valid = [sc for sc in scorecards if sc.error is None]
    n = len(valid) or 1
    console.print(f"\n[bold]{model}[/bold] — {len(valid)}/{len(scorecards)} cases scored")
    console.print(f"  Composite:      {sum(s.composite for s in valid)/n:.3f}")
    console.print(f"  Answer quality: {sum(s.answer_quality for s in valid)/n:.3f}")

    def _avg(attr: str) -> str:
        rows = [getattr(s, attr) for s in valid if getattr(s, attr) is not None]
        return f"{sum(rows)/len(rows):.3f}" if rows else "n/a"

    console.print(f"  Tool accuracy:  {_avg('tool_accuracy')}")
    console.print(f"  Tool params:    {_avg('tool_params')}")
    console.print(f"  Live validity:  {_avg('live_validity')}")
    console.print(f"  Latency score:  {sum(s.latency_score for s in valid)/n:.3f}")
    console.print(f"  Avg latency:    {sum(s.latency_s for s in valid)/n:.1f}s")
    console.print(f"  Pass rate:      {sum(1 for s in valid if s.composite >= pass_threshold)/n:.0%}")

    tokens = [s.usage.get("total_tokens") for s in valid if s.usage.get("total_tokens")]
    if tokens:
        console.print(f"  Avg tokens:     {sum(tokens)/len(tokens):,.0f}")
    costs = [s.usage.get("cost_usd") for s in valid if s.usage.get("cost_usd") is not None]
    if costs:
        console.print(f"  Model cost:     ${sum(costs):.4f}")
    judge_costs = [
        s.judge_usage.get("cost_usd")
        for s in valid
        if s.judge_usage.get("cost_usd") is not None
    ]
    if judge_costs:
        console.print(f"  Judge cost:     ${sum(judge_costs):.4f} [dim](not part of the score)[/dim]")

    artifacts = [s for s in scorecards if s.harness_artifact]
    if artifacts:
        console.print(
            f"  [yellow]Harness artifacts: {len(artifacts)} case(s) excluded from "
            "routing[/yellow]"
        )


suite_app = typer.Typer(help="Suite / Environment commands (dashboard is primary)")
app.add_typer(suite_app, name="suite")


@suite_app.command("list")
def suite_list() -> None:
    """List suites and environments."""
    from bench.suites import list_environments, list_suites

    envs = {e["id"]: e for e in list_environments()}
    suites = list_suites()
    if not suites:
        console.print("[dim]No suites yet — create them in the dashboard Suites tab.[/dim]")
    for s in suites:
        env_names = ", ".join(
            envs.get(eid, {}).get("name", eid) for eid in (s.get("environment_ids") or [])
        )
        console.print(
            f"[bold]{s['id']}[/bold]  {s.get('name')}  envs=[{env_names}]  "
            f"models={s.get('models')}"
        )


@suite_app.command("run")
def suite_run_cmd(
    suite_id: str = typer.Argument(..., help="Suite id"),
    case_ids: Optional[str] = typer.Option(None, help="Comma-separated case ids"),
) -> None:
    """Run a suite across its environments via subprocess workers."""
    import uuid

    from bench.suite_runner import run_suite

    run_id = uuid.uuid4().hex[:8]
    ids = [c.strip() for c in case_ids.split(",")] if case_ids else None

    async def emit(event: dict) -> None:
        console.print(f"  [dim]{event.get('type')}[/dim] { {k:v for k,v in event.items() if k != 'type'} }")

    async def _go():
        return await run_suite(
            suite_id,
            parent_run_id=run_id,
            emit=emit,
            case_ids=ids,
        )

    result = asyncio.run(_go())
    console.print(f"[bold]run_group_id[/bold] {result['run_group_id']}")
    for m in result["members"]:
        console.print(f"  {m}")


if __name__ == "__main__":
    app()
