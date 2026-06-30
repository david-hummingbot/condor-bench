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


@app.command()
def test(
    model: str = typer.Argument(..., help="Model to benchmark, e.g. ollama:llama3.1:8b"),
    category: Optional[str] = typer.Option(None, "-c", help="Filter by category"),
    consult_only: bool = typer.Option(False, help="Only consult cases"),
    tick_only: bool = typer.Option(False, help="Only tick cases"),
) -> None:
    """Run all benchmarks against a model and score vs baseline latency."""
    from bench.baseline import BaselineStore
    from bench.client import run_consult, run_tick, build_tick_prompt_for_case
    from bench.dataset import load_all_cases
    from bench.reporter import save_run
    from bench.scorer import score
    import uuid

    store = BaselineStore()
    cases = load_all_cases()

    if consult_only:
        cases = [c for c in cases if c.type == "consult"]
    elif tick_only:
        cases = [c for c in cases if c.type == "tick"]
    if category:
        cases = [c for c in cases if c.category == category]

    if not cases:
        console.print("[red]No cases matched the filters.[/red]")
        raise typer.Exit(1)

    missing = store.missing([c.id for c in cases])
    if missing:
        console.print(f"[yellow]Warning: {len(missing)} cases have no baseline latency ({', '.join(missing[:5])}{'...' if len(missing)>5 else ''}). Run 'make baseline' first.[/yellow]")

    run_id = uuid.uuid4().hex[:8]
    scorecards = []
    responses: dict[str, str] = {}

    async def _run():
        for case in cases:
            console.print(f"  [dim]{case.id}[/dim] ({case.type})", end=" ")
            try:
                if case.type == "tick":
                    prompt = build_tick_prompt_for_case(case, model)
                    result = await run_tick(case.id, prompt, model, case.mock_tools)
                    expected_tools = case.expected_tool_calls
                else:
                    result = await run_consult(
                        case.id, case.question, model,
                        extra_turns=getattr(case, "turns", []),
                        mock_tools=getattr(case, "mock_tools", {}),
                    )
                    expected_tools = getattr(case, "expected_tools", [])

                baseline = store.load(case.id)
                baseline_latency = baseline.latency_s if baseline else result.latency_s

                sc = await score(result, case.question, expected_tools, baseline_latency)
                scorecards.append(sc)
                responses[case.id] = result.response
                status = f"[green]{sc.composite:.2f}[/green]" if sc.composite >= 0.7 else f"[red]{sc.composite:.2f}[/red]"
                console.print(f"→ {status} ({result.latency_s:.1f}s)")
            except Exception as exc:
                console.print(f"[red]ERROR: {exc}[/red]")

    console.print(f"\nBenchmarking [bold]{model}[/bold] on {len(cases)} cases (run {run_id})")
    asyncio.run(_run())

    run_dir = save_run(model, scorecards, responses, run_id)
    console.print(f"\n[bold]Run saved:[/bold] {run_dir}")
    _print_summary(scorecards, model)


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
    table.add_column("Composite", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Latency score", justify="right")
    table.add_column("Cases", justify="right")
    for r in sorted(runs, key=lambda x: x.get("composite_avg", 0), reverse=True):
        table.add_row(
            r.get("run_dir", ""),
            r.get("model", ""),
            f"{r.get('composite_avg', 0):.2f}",
            f"{r.get('answer_quality_avg', 0):.2f}",
            f"{r.get('tool_accuracy_avg', 0):.2f}",
            f"{r.get('latency_score_avg', 0):.2f}",
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


def _print_summary(scorecards, model: str) -> None:
    if not scorecards:
        return
    valid = [sc for sc in scorecards if sc.error is None]
    n = len(valid) or 1
    console.print(f"\n[bold]{model}[/bold] — {len(valid)}/{len(scorecards)} cases scored")
    console.print(f"  Composite:      {sum(s.composite for s in valid)/n:.3f}")
    console.print(f"  Answer quality: {sum(s.answer_quality for s in valid)/n:.3f}")
    console.print(f"  Tool accuracy:  {sum(s.tool_accuracy for s in valid)/n:.3f}")
    console.print(f"  Latency score:  {sum(s.latency_score for s in valid)/n:.3f}")
    console.print(f"  Avg latency:    {sum(s.latency_s for s in valid)/n:.1f}s")


if __name__ == "__main__":
    app()
