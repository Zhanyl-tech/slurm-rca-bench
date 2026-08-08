"""Command line interface."""

from __future__ import annotations

import json
import sys

import click

from slurmrca import __version__
from slurmrca.loader import ScenarioError, load_all
from slurmrca.scoring import baselines as compute_baselines
from slurmrca.spec import ABSTAIN, Difficulty, Scenario


def _difficulty_label(scenario: Scenario, width: int = 0) -> str:
    """Colourised difficulty, padded *before* styling.

    ANSI escapes count toward a format spec's width, so styling first and
    padding second silently misaligns every coloured column.
    """
    text, colour = {
        Difficulty.MULTI_LAYER: ("multi-layer", "yellow"),
        Difficulty.UNDIAGNOSABLE: ("undiagnosable", "red"),
        Difficulty.SINGLE_LAYER: ("single-layer", None),
    }[scenario.difficulty]
    padded = text.ljust(width) if width else text
    return click.style(padded, fg=colour) if colour else padded


@click.group()
@click.version_option(__version__)
def main() -> None:
    """slurm-rca-bench — incident diagnosis benchmark for HPC schedulers."""


@main.command(name="list")
def list_scenarios() -> None:
    """Show every scenario with its shape."""
    scenarios = load_all()
    click.echo()
    click.secho(
        f"  {'ID':<38} {'FAMILY':<15} {'DIFFICULTY':<14} {'HOPS':>4} {'LAG':>7}",
        bold=True,
    )
    for scenario in scenarios:
        lag = "—" if not scenario.diagnosable else f"{scenario.propagation_s}s"
        click.echo(
            f"  {scenario.id:<38} {scenario.family.value:<15} "
            f"{_difficulty_label(scenario, 14)} {len(scenario.causal_chain):>4} {lag:>7}"
        )
    click.echo()
    multi = sum(1 for s in scenarios if s.difficulty is Difficulty.MULTI_LAYER)
    undiag = sum(1 for s in scenarios if s.difficulty is Difficulty.UNDIAGNOSABLE)
    click.echo(f"  {len(scenarios)} scenarios · {multi} multi-layer · {undiag} undiagnosable\n")


@main.command()
@click.argument("scenario_id")
def show(scenario_id: str) -> None:
    """Show one scenario's causal chain and scoring."""
    scenarios = {s.id: s for s in load_all()}
    matches = [sid for sid in scenarios if sid.startswith(scenario_id)]
    if not matches:
        raise click.ClickException(f"no scenario matching {scenario_id!r}")
    if len(matches) > 1:
        raise click.ClickException(f"ambiguous: {matches}")
    scenario = scenarios[matches[0]]

    click.echo()
    click.secho(scenario.id, bold=True)
    click.echo(f"  {scenario.title}")
    click.echo(f"  {scenario.family.value} · {_difficulty_label(scenario)}")
    click.echo()
    click.secho("  Reported symptom", bold=True)
    for line in scenario.symptom.strip().splitlines():
        click.echo(f"    {line.strip()}")

    click.echo()
    click.secho("  Causal chain (root cause first)", bold=True)
    for depth, link in enumerate(scenario.causal_chain):
        indent = "    " + ("   " * depth) + ("└─ " if depth else "")
        state = click.style(link.state, fg="cyan")
        click.echo(f"{indent}{link.node}  {state}  t+{link.observable_at_s}s")

    click.echo()
    click.secho("  Scoring", bold=True)
    for credit in sorted(scenario.scoring, key=lambda c: -c.credit):
        colour = "green" if credit.credit >= 1.0 else ("yellow" if credit.credit > 0 else None)
        value = f"{credit.credit:>5.2f}"
        mark = click.style(value, fg=colour) if colour else value
        label = f"{credit.node} (abstain)" if credit.node == ABSTAIN else credit.node
        click.echo(f"    {mark}  {label}")

    if scenario.distractors:
        click.echo()
        click.secho("  Distractors", bold=True)
        for node in scenario.distractors:
            click.echo(f"    {node}")
    click.echo()


@main.command()
def validate() -> None:
    """Check every scenario's ground truth for internal consistency."""
    try:
        scenarios = load_all()
    except ScenarioError as exc:
        click.secho(f"✗ {exc}", fg="red")
        sys.exit(1)

    for scenario in scenarios:
        problems = scenario.validate()
        if problems:
            click.secho(f"✗ {scenario.id}", fg="red")
            for problem in problems:
                click.echo(f"    {problem}")
            sys.exit(1)
        click.secho(f"✓ {scenario.id}", fg="green")
    click.echo(f"\n{len(scenarios)} scenarios, ground truth consistent")


@main.command()
@click.option("--top", default=8, show_default=True, help="How many strategies to show.")
def baselines(top: int) -> None:
    """Show what strategies that ignore all telemetry would score.

    The floor any real agent must beat. A benchmark reporting only an agent's
    score tells the reader nothing, because they cannot know that answering the
    same node to every task scores nearly as well.
    """
    scenarios = load_all()
    rows = compute_baselines(scenarios)
    click.echo(f"\n  degenerate baselines over {len(scenarios)} scenarios\n")
    for row in rows[:top]:
        marker = click.style("  <- floor", fg="yellow") if row is rows[0] else ""
        click.echo(f"  {row.rca_depth:>6.3f}  {row.name:<28}{marker}")
    click.echo(
        "\n  An agent that does not clear the floor has demonstrated fluency, not diagnosis.\n"
    )


@main.command()
def export() -> None:
    """Emit every scenario as JSON, for agents and external scorers."""
    payload = [
        {
            "id": s.id,
            "title": s.title,
            "family": s.family.value,
            "difficulty": s.difficulty.value,
            "symptom": s.symptom.strip(),
            "observe_s": s.observe_s,
            "diagnosable": s.diagnosable,
            "propagation_s": s.propagation_s,
        }
        for s in load_all()
    ]
    click.echo(json.dumps(payload, indent=2))


if __name__ == "__main__":
    sys.exit(main())
