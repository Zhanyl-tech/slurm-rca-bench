"""Command line interface."""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import click

from slurmrca import __version__
from slurmrca.cluster import Cluster, ClusterError
from slurmrca.inject import heal_all, injection_for, run_injection
from slurmrca.loader import (
    LEGACY_IDS,
    ScenarioError,
    compose_file,
    fingerprint,
    load_all,
    load_report,
    resolve_id,
    runnable,
)
from slurmrca.scoring import DEGENERACY_THRESHOLD, answer_from_dict, degenerate, score_run
from slurmrca.scoring import baselines as compute_baselines
from slurmrca.scoring import format_report as render_report
from slurmrca.spec import ABSTAIN, Difficulty, Scenario

P = ParamSpec("P")
R = TypeVar("R")


def _friendly(func: Callable[P, R]) -> Callable[P, R]:
    """Turn a broken scenario file or an unreachable cluster into a message.

    The first CLI printed a raw traceback for a ScenarioError from every
    command except ``validate``.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except (ScenarioError, ClusterError) as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


def _scenarios(ctx: click.Context) -> list[Scenario]:
    root: Path | None = ctx.obj.get("scenarios") if ctx.obj else None
    return load_all(root)


def _make_cluster() -> Cluster:
    """Build the real cluster handle. Tests replace this with a fake."""
    return Cluster(compose_file())


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
@click.option(
    "--scenarios",
    "scenarios_path",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Scenario directory (default: $SLURMRCA_SCENARIOS, then the bundled suite).",
)
@click.pass_context
def main(ctx: click.Context, scenarios_path: Path | None) -> None:
    """slurm-rca-bench — incident diagnosis benchmark for HPC schedulers."""
    ctx.ensure_object(dict)
    ctx.obj["scenarios"] = scenarios_path


@main.command(name="list")
@click.pass_context
@_friendly
def list_scenarios(ctx: click.Context) -> None:
    """Show every scenario with its shape and status."""
    scenarios = _scenarios(ctx)
    click.echo()
    click.secho(
        f"  {'ID':<42} {'FAMILY':<16} {'DIFFICULTY':<14} {'STATUS':<9} {'HOPS':>4} {'LAG':>6}",
        bold=True,
    )
    for scenario in scenarios:
        lag = "—" if not scenario.diagnosable else f"{scenario.propagation_s}s"
        status = scenario.status.value
        styled = status.ljust(9)
        if not scenario.runnable:
            styled = click.style(styled, fg="red")
        click.echo(
            f"  {scenario.id:<42} {scenario.family.value:<16} "
            f"{_difficulty_label(scenario, 14)} {styled} "
            f"{len(scenario.causal_chain):>4} {lag:>6}"
        )
    click.echo()
    runnable_count = len(runnable(scenarios))
    multi = sum(1 for s in scenarios if s.difficulty is Difficulty.MULTI_LAYER)
    undiag = sum(1 for s in scenarios if s.difficulty is Difficulty.UNDIAGNOSABLE)
    click.echo(
        f"  {len(scenarios)} scenarios · {runnable_count} runnable · "
        f"{len(scenarios) - runnable_count} blocked · "
        f"{multi} multi-layer · {undiag} undiagnosable\n"
    )


@main.command()
@click.argument("scenario_id")
@click.pass_context
@_friendly
def show(ctx: click.Context, scenario_id: str) -> None:
    """Show one scenario's causal chain and scoring (id prefix or legacy id)."""
    scenarios = _scenarios(ctx)
    try:
        scenario = resolve_id(scenario_id, scenarios)
    except KeyError:
        matches = [s for s in scenarios if s.id.startswith(scenario_id)]
        if not matches:
            raise click.ClickException(f"no scenario matching {scenario_id!r}") from None
        if len(matches) > 1:
            raise click.ClickException(f"ambiguous: {[s.id for s in matches]}") from None
        scenario = matches[0]

    click.echo()
    click.secho(scenario.id, bold=True)
    click.echo(f"  {scenario.title}")
    click.echo(
        f"  {scenario.family.value} · {_difficulty_label(scenario)} · {scenario.status.value}"
    )
    if not scenario.runnable:
        click.secho("  BLOCKED — not scored:", fg="red")
        click.echo(f"    {' '.join(scenario.blocked_reason.split())}")
    measured = "measured" if scenario.verification.measured else "not measured"
    click.echo(f"  chain {measured} · ticket points at {scenario.reported_node}")
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
@click.pass_context
@_friendly
def validate(ctx: click.Context) -> None:
    """Check every scenario's ground truth and report every problem found."""
    result = load_report(ctx.obj.get("scenarios"))
    for scenario in result.scenarios:
        click.secho(f"✓ {scenario.id}", fg="green")
    for error in result.errors:
        click.secho(f"✗ {error}", fg="red")
    if result.errors:
        click.echo(f"\n{len(result.errors)} problem(s); ground truth is NOT consistent")
        sys.exit(1)
    total = len(result.scenarios)
    ready = len(runnable(result.scenarios))
    click.echo(
        f"\n{total} scenarios ({ready} runnable, {total - ready} blocked), ground truth consistent"
    )


@main.command()
@click.option("--top", default=8, show_default=True, help="How many strategies to show.")
@click.option(
    "--include-blocked",
    is_flag=True,
    help="Compute over the designed suite, blocked scenarios included (planning only).",
)
@click.pass_context
@_friendly
def baselines(ctx: click.Context, top: int, include_blocked: bool) -> None:
    """Show what strategies that ignore all telemetry would score.

    The floor any real agent must beat. A benchmark reporting only an agent's
    score tells the reader nothing, because they cannot know that answering the
    same node to every task scores nearly as well.
    """
    scenarios = _scenarios(ctx)
    suite = scenarios if include_blocked else runnable(scenarios)
    rows = compute_baselines(scenarios, include_blocked=include_blocked)
    label = "designed" if include_blocked else "runnable"
    click.echo(f"\n  degenerate baselines over {len(suite)} {label} scenarios\n")
    # Brier is each constant answer's best, at its best constant confidence.
    # The README quoted those numbers while this printed depth only, so they
    # could not be reproduced from any command.
    click.echo(f"  {'depth':>6}  {'brier':>6}  strategy")
    for row in rows[:top]:
        marker = click.style("  <- floor", fg="yellow") if row is rows[0] else ""
        brier = "—" if row.brier is None else f"{row.brier:.3f}"
        click.echo(f"  {row.rca_depth:>6.3f}  {brier:>6}  {row.name:<28}{marker}")
    if include_blocked:
        click.echo("\n  Includes blocked scenarios. Not a floor; do not quote it as one.")
    else:
        offenders = degenerate(scenarios)
        if offenders:
            names = ", ".join(f"{b.name} {b.rca_depth:.3f}" for b in offenders)
            click.secho(
                f"\n  DEGENERATE: {names} (threshold {DEGENERACY_THRESHOLD}). "
                "A constant answer scores this without reading any telemetry.",
                fg="red",
            )
    click.echo(
        "\n  An agent that does not clear the floor has demonstrated fluency, not diagnosis."
        "\n  Clearing it is necessary, not sufficient: the floor ignores the ticket text.\n"
    )


@main.command()
@click.option(
    "--view",
    type=click.Choice(["agent", "scorer"]),
    default="agent",
    show_default=True,
    help=(
        "agent: opaque task id and ticket only. scorer: every scenario field "
        "(credit table, causal chain, distractors, action ground truth, "
        "verification) plus the task-id and legacy-id mappings."
    ),
)
@click.pass_context
@_friendly
def export(ctx: click.Context, view: str) -> None:
    """Emit scenarios as JSON.

    The agent view carries only what an on-call engineer would be told: an
    opaque task id and the ticket, for runnable scenarios. The first export gave
    agents the difficulty and a ``diagnosable`` flag, and the scenario ids
    contain "undiagnosable", so an agent could abstain on exactly the right two
    tasks without looking at anything.

    The scorer view is every field of every scenario, so an external scorer
    can compute RCA depth and the action metric from it alone. It used to be
    described as "everything" while it left out the credit table and the
    causal chain.
    """
    scenarios = _scenarios(ctx)
    payload: list[dict[str, Any]]
    if view == "agent":
        payload = [
            {"task_id": s.task_id, "symptom": " ".join(s.symptom.split())}
            for s in runnable(scenarios)
        ]
    else:
        payload = [_scorer_record(s) for s in scenarios]
    click.echo(json.dumps(payload, indent=2))


def _scorer_record(scenario: Scenario) -> dict[str, Any]:
    """Every field of a scenario, plus the derived values a scorer needs."""
    record: dict[str, Any] = dataclasses.asdict(scenario)
    record.update(
        status=scenario.status.value,
        family=scenario.family.value,
        difficulty=scenario.difficulty.value,
        symptom=" ".join(scenario.symptom.split()),
        remediation_helpful=(
            None if scenario.remediation_helpful is None else list(scenario.remediation_helpful)
        ),
        task_id=scenario.task_id,
        # Answers may name a scenario by a retired id; a scorer outside this
        # package cannot resolve those without the mapping.
        legacy_ids=sorted(old for old, new in LEGACY_IDS.items() if new == scenario.id),
        root_cause=scenario.root_cause,
        diagnosable=scenario.diagnosable,
        propagation_s=scenario.propagation_s,
        measured=scenario.verification.measured,
    )
    return record


def _read_answers(path: Path) -> list[dict[str, Any]]:
    """Read answers as a JSON array or as JSON Lines."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise click.ClickException("expected a JSON array of answers")
        return data
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@main.command()
@click.argument("answers_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--config", "config_name", default="unnamed", show_default=True)
@click.pass_context
@_friendly
def score(ctx: click.Context, answers_file: Path, config_name: str) -> None:
    """Score an answers file (JSON array or JSON Lines) against the runnable suite."""
    scenarios = _scenarios(ctx)
    try:
        answers = [answer_from_dict(raw) for raw in _read_answers(answers_file)]
        report = score_run(
            scenarios,
            answers,
            config=config_name,
            fingerprint=fingerprint(ctx.obj.get("scenarios")),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"{answers_file}: {exc}") from exc
    click.echo(render_report(report, scenarios))


@main.command()
@click.argument("scenario_id", required=False)
@click.option("--all", "heal_everything", is_flag=True, help="Run every scenario's heal.")
@_friendly
def heal(scenario_id: str | None, heal_everything: bool) -> None:
    """Undo an injection. Idempotent; safe to run on a clean cluster.

    ``--all`` runs every heal, which is what to do after an interrupted run:
    a laptop sleeping mid-scenario is enough to leave the cluster injected.
    """
    if heal_everything and scenario_id is None:
        failures = heal_all(_make_cluster(), log=click.echo)
        if failures:
            raise click.ClickException(
                f"{len(failures)} heal(s) failed: {', '.join(sid for sid, _ in failures)}"
            )
        click.echo("healed")
    elif scenario_id is not None and not heal_everything:
        injection = injection_for(scenario_id)
        injection.heal(_make_cluster())
        click.echo(f"healed {injection.scenario_id}")
    else:
        raise click.UsageError("give exactly one of SCENARIO_ID or --all")


@main.command()
@click.argument("scenario_id")
@click.option("--observe-s", type=int, default=None, help="Override the scenario's observe_s.")
@click.option("--allow-blocked", is_flag=True, help="Run a blocked scenario (for measuring fixes).")
@click.pass_context
@_friendly
def run(ctx: click.Context, scenario_id: str, observe_s: int | None, allow_blocked: bool) -> None:
    """Heal, inject, observe, heal — against the live benchmark cluster.

    Collects no telemetry bundle yet (Phase 3). Use it to watch a scenario by
    hand or to check that an injection takes effect.
    """
    scenarios = _scenarios(ctx)
    try:
        scenario = resolve_id(scenario_id, scenarios)
    except KeyError:
        raise click.ClickException(f"no scenario {scenario_id!r}") from None
    if not scenario.runnable and not allow_blocked:
        raise click.ClickException(
            f"{scenario.id} is blocked: {' '.join(scenario.blocked_reason.split())}"
        )
    window = scenario.observe_s if observe_s is None else observe_s
    run_injection(_make_cluster(), injection_for(scenario.id), window, log=click.echo)


if __name__ == "__main__":
    sys.exit(main())
