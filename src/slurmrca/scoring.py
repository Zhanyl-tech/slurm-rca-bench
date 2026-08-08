"""Scoring, and the degenerate baselines every score must be read against.

The baselines are the part that matters. A benchmark that reports only "the
agent scored 0.52" is not reporting anything, because the reader has no way to
know that answering ``db.mysql`` to every single task without looking at any
telemetry also scores 0.52. Published RCA numbers are quoted without this floor
almost universally, and it is the first thing a sceptical reader should ask for.

So :func:`baselines` computes, from the ground truth alone, what every
degenerate strategy would score: each constant answer, always abstaining, and a
uniform random guess. Those numbers are printed next to the agent's, and an
agent that fails to beat the best constant answer has demonstrated nothing.

This also settles a design question the scenarios raise on their own. S01 and
S02 both present as "accounting hangs", and ``db.mysql`` earns 0.45 on one and
1.0 on the other — so a constant ``db.mysql`` answerer does suspiciously well on
that pair. Whether that is a flaw or the intended signal is not a matter of
opinion once the constant-answer baseline is computed across the whole suite:
if the best constant answer scores close to a real agent, the suite is too
narrow, and the fix is more scenarios whose causes are elsewhere, not
hand-tuned credit weights.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from slurmrca.spec import ABSTAIN, NODES, Difficulty, Scenario


@dataclass(frozen=True, slots=True)
class Answer:
    """What an agent returns for one scenario.

    The stable contract between the benchmark and any agent. ``root_cause``
    must be a node identifier from :data:`~slurmrca.spec.NODES`, or
    :data:`~slurmrca.spec.ABSTAIN`.
    """

    scenario_id: str
    root_cause: str
    #: Stated probability the answer is right, in [0, 1]. Used for calibration,
    #: which is where abstention earns or loses its keep.
    confidence: float = 0.5
    #: Nodes the agent claims to have eliminated. On undiagnosable scenarios
    #: this is the actual diagnostic work, so it is scored.
    ruled_out: tuple[str, ...] = ()
    #: Wall-clock seconds to the first committed hypothesis.
    time_to_hypothesis_s: float | None = None
    #: Remediation the agent proposed. Anything not known to help is a false
    #: positive; proposing nothing is always safe.
    actions_proposed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.root_cause not in NODES:
            raise ValueError(
                f"root_cause {self.root_cause!r} is not a known node; "
                f"answers must come from the closed vocabulary or be {ABSTAIN!r}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True, slots=True)
class ScenarioScore:
    """How one answer scored on one scenario."""

    scenario_id: str
    answer: str
    #: Partial credit from the scenario's own table.
    rca_depth: float
    #: Squared error between stated confidence and whether the answer was right.
    #: Lower is better; 0.25 is what you get by always saying 0.5.
    brier: float
    #: True when the agent abstained and abstention was correct.
    correct_abstention: bool
    #: True when the agent named a cause on a scenario that has none.
    overconfident_guess: bool
    time_to_hypothesis_s: float | None
    action_false_positives: int
    actions_proposed: int
    #: Which documented distractor the agent fell for, if any.
    distractor: str | None

    @property
    def action_fp_rate(self) -> float:
        """Fraction of proposed actions that were not known to help."""
        if not self.actions_proposed:
            return 0.0
        return self.action_false_positives / self.actions_proposed


def score_answer(scenario: Scenario, answer: Answer) -> ScenarioScore:
    """Score one answer against one scenario's ground truth."""
    if answer.scenario_id != scenario.id:
        raise ValueError(f"answer is for {answer.scenario_id!r}, scenario is {scenario.id!r}")

    depth = scenario.credit_for(answer.root_cause)

    # Correctness for calibration is deliberately binary and strict: partial
    # credit measures diagnostic progress, but a confidence statement is a claim
    # about being *right*, and half-right is not right.
    correct = answer.root_cause == scenario.root_cause
    brier = (answer.confidence - (1.0 if correct else 0.0)) ** 2

    undiagnosable = scenario.difficulty is Difficulty.UNDIAGNOSABLE
    abstained = answer.root_cause == ABSTAIN

    action_fps = _action_false_positives(scenario, answer)

    return ScenarioScore(
        scenario_id=scenario.id,
        answer=answer.root_cause,
        rca_depth=depth,
        brier=brier,
        correct_abstention=undiagnosable and abstained,
        overconfident_guess=undiagnosable and not abstained,
        time_to_hypothesis_s=answer.time_to_hypothesis_s,
        action_false_positives=action_fps,
        actions_proposed=len(answer.actions_proposed),
        distractor=answer.root_cause if answer.root_cause in scenario.distractors else None,
    )


def _action_false_positives(scenario: Scenario, answer: Answer) -> int:
    """Count proposed actions not known to address the true cause.

    Conservative on purpose: an action is a false positive unless the scenario
    explicitly lists it as helpful. Under-specified remediation therefore
    penalises the *benchmark author* into documenting it, rather than quietly
    giving an agent credit for plausible-sounding changes.
    """
    helpful = {a.strip().lower() for a in scenario.remediation_helpful}
    return sum(1 for action in answer.actions_proposed if action.strip().lower() not in helpful)


@dataclass(frozen=True, slots=True)
class RunReport:
    """Aggregate scores for one agent configuration over a scenario suite."""

    config: str
    scores: tuple[ScenarioScore, ...]
    #: Scenarios in the suite that the agent did not answer at all.
    missing: tuple[str, ...] = ()

    @property
    def rca_depth(self) -> float:
        """Mean partial credit. The headline number."""
        return statistics.fmean(s.rca_depth for s in self.scores) if self.scores else 0.0

    @property
    def brier(self) -> float:
        """Mean Brier score. Lower is better."""
        return statistics.fmean(s.brier for s in self.scores) if self.scores else 1.0

    @property
    def median_time_to_hypothesis_s(self) -> float | None:
        times = [s.time_to_hypothesis_s for s in self.scores if s.time_to_hypothesis_s is not None]
        return statistics.median(times) if times else None

    @property
    def action_fp_rate(self) -> float:
        proposed = sum(s.actions_proposed for s in self.scores)
        if not proposed:
            return 0.0
        return sum(s.action_false_positives for s in self.scores) / proposed

    @property
    def correct_abstentions(self) -> int:
        return sum(1 for s in self.scores if s.correct_abstention)

    @property
    def overconfident_guesses(self) -> int:
        """Answers naming a cause on a scenario that has none.

        The number to look at first. An agent with a good RCA depth and a
        non-zero count here is one that will confidently invent a cause at 3am.
        """
        return sum(1 for s in self.scores if s.overconfident_guess)

    @property
    def distractors_hit(self) -> dict[str, int]:
        hits: dict[str, int] = {}
        for score in self.scores:
            if score.distractor:
                hits[score.distractor] = hits.get(score.distractor, 0) + 1
        return hits


def score_run(
    scenarios: list[Scenario], answers: list[Answer], *, config: str = "unnamed"
) -> RunReport:
    """Score a full run.

    Unanswered scenarios are recorded rather than skipped. Averaging over only
    the scenarios an agent chose to answer would let it inflate its score by
    declining the hard ones, so a missing answer is scored as a zero-credit
    abstention with maximally-wrong confidence.
    """
    by_id = {s.id: s for s in scenarios}
    answered = {a.scenario_id for a in answers}
    missing = tuple(sorted(set(by_id) - answered))

    scores = [score_answer(by_id[a.scenario_id], a) for a in answers if a.scenario_id in by_id]
    for scenario_id in missing:
        scenario = by_id[scenario_id]
        scores.append(score_answer(scenario, Answer(scenario_id, ABSTAIN, confidence=0.0)))

    return RunReport(config=config, scores=tuple(scores), missing=missing)


# ── baselines ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Baseline:
    """What a strategy that ignores all telemetry would score."""

    name: str
    rca_depth: float
    #: Correct abstentions, for the always-abstain strategy.
    correct_abstentions: int = 0
    overconfident_guesses: int = 0
    detail: str = ""


def baselines(scenarios: list[Scenario]) -> list[Baseline]:
    """Score every degenerate strategy against the suite.

    Returned best-first. The top entry is the floor: an agent that does not
    clear it has not demonstrated diagnostic ability, only fluency.
    """
    results: list[Baseline] = []

    for node in sorted(NODES):
        depth = statistics.fmean(s.credit_for(node) for s in scenarios)
        if node == ABSTAIN:
            correct = sum(1 for s in scenarios if s.difficulty is Difficulty.UNDIAGNOSABLE)
            results.append(
                Baseline(
                    name="always-abstain",
                    rca_depth=depth,
                    correct_abstentions=correct,
                    detail="never looks at telemetry; always says 'I cannot tell'",
                )
            )
        else:
            over = sum(1 for s in scenarios if s.difficulty is Difficulty.UNDIAGNOSABLE)
            results.append(
                Baseline(
                    name=f"always-{node}",
                    rca_depth=depth,
                    overconfident_guesses=over,
                    detail=f"answers {node} to every scenario",
                )
            )

    # A uniform guess over the vocabulary, computed exactly rather than sampled.
    uniform = statistics.fmean(
        statistics.fmean(s.credit_for(node) for node in NODES) for s in scenarios
    )
    results.append(
        Baseline(
            name="uniform-random",
            rca_depth=uniform,
            detail=f"expected score of a uniform guess over {len(NODES)} nodes",
        )
    )

    return sorted(results, key=lambda b: -b.rca_depth)


def baseline_floor(scenarios: list[Scenario]) -> Baseline:
    """The single best degenerate strategy. Any agent must beat this."""
    return baselines(scenarios)[0]


def format_report(report: RunReport, scenarios: list[Scenario]) -> str:
    """Render a run against the baselines, as the README leaderboard shows it."""
    floor = baseline_floor(scenarios)
    lines = [
        f"config: {report.config}",
        f"  RCA depth              {report.rca_depth:.3f}",
        f"  best degenerate floor  {floor.rca_depth:.3f}  ({floor.name})",
        f"  margin over floor      {report.rca_depth - floor.rca_depth:+.3f}",
        f"  Brier (lower better)   {report.brier:.3f}",
        f"  correct abstentions    {report.correct_abstentions}",
        f"  overconfident guesses  {report.overconfident_guesses}",
        f"  action FP rate         {report.action_fp_rate:.3f}",
    ]
    if report.median_time_to_hypothesis_s is not None:
        lines.append(f"  median time-to-hyp     {report.median_time_to_hypothesis_s:.0f}s")
    if report.missing:
        lines.append(f"  unanswered             {len(report.missing)} (scored as abstentions)")
    hits = report.distractors_hit
    if hits:
        joined = ", ".join(f"{k} x{v}" for k, v in sorted(hits.items()))
        lines.append(f"  distractors hit        {joined}")
    return "\n".join(lines)
