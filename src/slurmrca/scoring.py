"""Scoring, and the degenerate baselines every score must be read against.

The baselines are the part that matters. A benchmark that reports only "the
agent scored 0.52" is not reporting anything, because the reader has no way to
know that answering ``db.mysql`` to every single task without looking at any
telemetry also scores 0.52. Published RCA numbers are quoted without this floor
almost universally, and it is the first thing a sceptical reader should ask for.

So :func:`baselines` computes, from the ground truth alone, what every
degenerate strategy would score: each constant answer, always abstaining, and a
uniform random guess, plus the best Brier score a constant answer with a
constant confidence can reach. Those numbers are printed next to the agent's,
and an agent that fails to beat them has demonstrated nothing.

Only **runnable** scenarios are scored. A blocked scenario's injection cannot
produce its ground truth, so scoring it would grade every agent on a fault that
never happened. Blocked scenarios are excluded from :func:`score_run`, from the
baselines, and from every published denominator.

The constant-answer floor is a weak floor. Several tickets describe the failure
closely enough that a model reading only the ticket, with no tools, may do far
better than any constant answer. That symptom-only baseline needs an LLM run and
has not been measured.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from slurmrca import __version__
from slurmrca.loader import resolve_id, runnable
from slurmrca.spec import ABSTAIN, NODES, Difficulty, Scenario, parse_action

#: Label recorded for a scenario the agent did not answer.
MISSING = "<missing>"

#: A constant component answer at or above this RCA depth means the suite can
#: be gamed without reading any telemetry.
DEGENERACY_THRESHOLD = 0.25


@dataclass(frozen=True, slots=True)
class Answer:
    """What an agent returns for one scenario.

    The stable contract between the benchmark and any agent. ``root_cause``
    must be a node identifier from :data:`~slurmrca.spec.NODES`, or
    :data:`~slurmrca.spec.ABSTAIN`. ``scenario_id`` may be the scenario id, a
    legacy id, or the opaque task id from ``slurm-rca export --view agent``.
    """

    scenario_id: str
    root_cause: str
    #: Stated probability the answer is right, in [0, 1]. Used for calibration,
    #: which is where abstention earns or loses its keep.
    confidence: float = 0.5
    #: Nodes the agent claims to have eliminated. Validated against the
    #: vocabulary and recorded. It does not change RCA depth: scoring it needs a
    #: per-scenario set of nodes the evidence really eliminates, and that ground
    #: truth has not been written. The one use today is flagging an agent that
    #: rules out the true root cause.
    ruled_out: tuple[str, ...] = ()
    #: Seconds to the first committed hypothesis, **as reported by the agent**.
    #: Nothing here measures it; treat it as self-reported until a runner
    #: timestamps the agent's calls itself.
    time_to_hypothesis_s: float | None = None
    #: Remediation the agent proposed, as ``verb:node`` actions from the closed
    #: vocabulary. Proposing nothing is always safe.
    actions_proposed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.root_cause not in NODES:
            raise ValueError(
                f"root_cause {self.root_cause!r} is not a known node; "
                f"answers must come from the closed vocabulary or be {ABSTAIN!r}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        for node in self.ruled_out:
            if node not in NODES or node == ABSTAIN:
                raise ValueError(f"ruled_out entry {node!r} is not a component from NODES")
        if len(set(self.ruled_out)) != len(self.ruled_out):
            raise ValueError("ruled_out lists a node twice")
        if self.root_cause in self.ruled_out:
            raise ValueError(f"root_cause {self.root_cause!r} is also listed in ruled_out")
        for action in self.actions_proposed:
            parse_action(action)
        if self.time_to_hypothesis_s is not None and self.time_to_hypothesis_s < 0:
            raise ValueError("time_to_hypothesis_s must be >= 0")


_ANSWER_KEYS = {
    "scenario_id",
    "root_cause",
    "confidence",
    "ruled_out",
    "time_to_hypothesis_s",
    "actions_proposed",
}


def answer_from_dict(raw: Any) -> Answer:
    """Parse one answer from JSON, rejecting unknown keys and wrong types."""
    if not isinstance(raw, dict):
        raise ValueError(f"an answer must be a JSON object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _ANSWER_KEYS)
    if unknown:
        raise ValueError(f"unknown answer key(s) {unknown}")
    for key in ("scenario_id", "root_cause"):
        if not isinstance(raw.get(key), str):
            raise ValueError(f"answer needs a string {key!r}")

    def number(key: str, default: float | None) -> float | None:
        value = raw.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{key} must be a number")
        return float(value)

    def strings(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of strings")
        return tuple(value)

    confidence = number("confidence", 0.5)
    return Answer(
        scenario_id=raw["scenario_id"],
        root_cause=raw["root_cause"],
        confidence=0.5 if confidence is None else confidence,
        ruled_out=strings("ruled_out"),
        time_to_hypothesis_s=number("time_to_hypothesis_s", None),
        actions_proposed=strings("actions_proposed"),
    )


@dataclass(frozen=True, slots=True)
class ScenarioScore:
    """How one answer scored on one scenario."""

    scenario_id: str
    #: The node the agent named, or :data:`MISSING`.
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
    #: Self-reported by the agent; see :attr:`Answer.time_to_hypothesis_s`.
    time_to_hypothesis_s: float | None
    action_false_positives: int
    actions_proposed: int
    #: Which documented distractor the agent fell for, if any.
    distractor: str | None
    #: True when the scenario was not answered at all.
    missing: bool = False
    #: True when the agent listed the true root cause in ``ruled_out``.
    ruled_out_root_cause: bool = False
    #: False when the scenario has no action ground truth, so its proposals
    #: are not counted either way.
    actions_scored: bool = True

    @property
    def action_fp_rate(self) -> float:
        """Fraction of proposed actions that were not known to help."""
        if not self.actions_proposed or not self.actions_scored:
            return 0.0
        return self.action_false_positives / self.actions_proposed


def score_answer(scenario: Scenario, answer: Answer) -> ScenarioScore:
    """Score one answer against one scenario's ground truth."""
    if resolve_or_none(answer.scenario_id, [scenario]) is None:
        raise ValueError(f"answer is for {answer.scenario_id!r}, scenario is {scenario.id!r}")

    depth = scenario.credit_for(answer.root_cause)

    # Correctness for calibration is deliberately binary and strict: partial
    # credit measures diagnostic progress, but a confidence statement is a claim
    # about being *right*, and half-right is not right.
    correct = answer.root_cause == scenario.root_cause
    brier = (answer.confidence - (1.0 if correct else 0.0)) ** 2

    undiagnosable = scenario.difficulty is Difficulty.UNDIAGNOSABLE
    abstained = answer.root_cause == ABSTAIN

    helpful = scenario.remediation_helpful
    action_fps = 0 if helpful is None else sum(a not in helpful for a in answer.actions_proposed)

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
        ruled_out_root_cause=scenario.diagnosable and scenario.root_cause in answer.ruled_out,
        actions_scored=helpful is not None,
    )


def resolve_or_none(identifier: str, scenarios: list[Scenario]) -> Scenario | None:
    """:func:`~slurmrca.loader.resolve_id`, returning None instead of raising."""
    try:
        return resolve_id(identifier, scenarios)
    except KeyError:
        return None


def _missing_score(scenario: Scenario) -> ScenarioScore:
    """An unanswered scenario: no credit, worst calibration, not an abstention.

    The first version scored a missing answer as an abstention. On an
    undiagnosable scenario abstention earns full credit, so an agent that
    crashed on every task tied the published floor and was credited with
    "correct abstentions" it never made.
    """
    return ScenarioScore(
        scenario_id=scenario.id,
        answer=MISSING,
        rca_depth=0.0,
        brier=1.0,
        correct_abstention=False,
        overconfident_guess=False,
        time_to_hypothesis_s=None,
        action_false_positives=0,
        actions_proposed=0,
        distractor=None,
        missing=True,
        actions_scored=scenario.remediation_helpful is not None,
    )


@dataclass(frozen=True, slots=True)
class RunReport:
    """Aggregate scores for one agent configuration over the runnable suite."""

    config: str
    scores: tuple[ScenarioScore, ...]
    #: Runnable scenarios the agent did not answer at all.
    missing: tuple[str, ...] = ()
    #: Blocked scenarios left out of every number in this report.
    excluded_blocked: tuple[str, ...] = ()
    #: :func:`~slurmrca.loader.fingerprint` of what was scored against: the
    #: ground truth, the cluster files and the injection code.
    fingerprint: str = ""

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
        """Median of the agent's self-reported times. Not a measurement."""
        times = [s.time_to_hypothesis_s for s in self.scores if s.time_to_hypothesis_s is not None]
        return statistics.median(times) if times else None

    @property
    def action_scenarios(self) -> int:
        """Scenarios with action ground truth, i.e. the action metric's base."""
        return sum(1 for s in self.scores if s.actions_scored)

    @property
    def action_fp_rate(self) -> float:
        scored = [s for s in self.scores if s.actions_scored]
        proposed = sum(s.actions_proposed for s in scored)
        if not proposed:
            return 0.0
        return sum(s.action_false_positives for s in scored) / proposed

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
    def ruled_out_root_causes(self) -> int:
        """Answers that listed the true root cause as eliminated."""
        return sum(1 for s in self.scores if s.ruled_out_root_cause)

    @property
    def distractors_hit(self) -> dict[str, int]:
        hits: dict[str, int] = {}
        for score in self.scores:
            if score.distractor:
                hits[score.distractor] = hits.get(score.distractor, 0) + 1
        return hits


def score_run(
    scenarios: list[Scenario],
    answers: list[Answer],
    *,
    config: str = "unnamed",
    fingerprint: str = "",
) -> RunReport:
    """Score a full run over the runnable scenarios.

    Strict about the answer set, because each leniency was measured to distort
    the headline number:

    - an answer for an id that is not in the suite raises (it used to be
      dropped silently);
    - two answers for the same scenario raise (100 copies of one correct answer
      once lifted the mean to 0.939);
    - an answer for a blocked scenario raises, since blocked scenarios are not
      scored;
    - a runnable scenario with no answer scores 0.0 depth and Brier 1.0 and is
      not an abstention (see :func:`_missing_score`).
    """
    scored = runnable(scenarios)
    blocked = [s for s in scenarios if not s.runnable]

    by_id: dict[str, Answer] = {}
    unknown: list[str] = []
    blocked_answers: list[str] = []
    duplicates: list[str] = []
    for answer in answers:
        target = resolve_or_none(answer.scenario_id, scenarios)
        if target is None:
            unknown.append(answer.scenario_id)
            continue
        if not target.runnable:
            blocked_answers.append(target.id)
            continue
        if target.id in by_id:
            duplicates.append(target.id)
            continue
        by_id[target.id] = answer

    problems = []
    if unknown:
        problems.append(f"answers for scenario ids not in the suite: {sorted(unknown)}")
    if duplicates:
        problems.append(f"more than one answer for: {sorted(set(duplicates))}")
    if blocked_answers:
        problems.append(f"answers for blocked (unscored) scenarios: {sorted(blocked_answers)}")
    if problems:
        raise ValueError("; ".join(problems))

    scores = [score_answer(s, by_id[s.id]) if s.id in by_id else _missing_score(s) for s in scored]
    return RunReport(
        config=config,
        scores=tuple(scores),
        missing=tuple(s.id for s in scored if s.id not in by_id),
        excluded_blocked=tuple(s.id for s in blocked),
        fingerprint=fingerprint,
    )


# ── baselines ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Baseline:
    """What a strategy that ignores all telemetry would score."""

    name: str
    rca_depth: float
    #: Correct abstentions, for the always-abstain strategy.
    correct_abstentions: int = 0
    overconfident_guesses: int = 0
    #: Best Brier a constant answer can reach with a constant confidence, or
    #: None for strategies where that is not defined.
    brier: float | None = None
    detail: str = ""


def _constant_brier(scenarios: list[Scenario], node: str) -> float:
    """Best mean Brier for always answering ``node`` with one fixed confidence.

    With k correct out of n, the Brier score of confidence p is
    (k(1-p)^2 + (n-k)p^2)/n, minimised at p = k/n where it equals p(1-p).
    Computed exactly, like the depth baselines.

    One consequence is worth stating plainly: a constant answer that is never
    right, stated at confidence 0, scores a perfect 0.000. Brier measures
    whether confidence matches correctness, not whether the agent is correct,
    so it only means something read next to RCA depth.
    """
    if not scenarios:
        return 1.0
    p = sum(1 for s in scenarios if s.root_cause == node) / len(scenarios)
    return p * (1.0 - p)


def baselines(scenarios: list[Scenario], *, include_blocked: bool = False) -> list[Baseline]:
    """Score every degenerate strategy against the runnable suite.

    Returned best-first. The top entry is the floor: an agent that does not
    clear it has not demonstrated diagnostic ability, only fluency.
    ``include_blocked`` computes the same numbers over the designed suite,
    which is useful for planning and must not be quoted as a floor.
    """
    suite = scenarios if include_blocked else runnable(scenarios)
    if not suite:
        return []
    results: list[Baseline] = []
    undiagnosable = sum(1 for s in suite if s.difficulty is Difficulty.UNDIAGNOSABLE)

    for node in sorted(NODES):
        depth = statistics.fmean(s.credit_for(node) for s in suite)
        if node == ABSTAIN:
            results.append(
                Baseline(
                    name="always-abstain",
                    rca_depth=depth,
                    correct_abstentions=undiagnosable,
                    brier=_constant_brier(suite, node),
                    detail="never looks at telemetry; always says 'I cannot tell'",
                )
            )
        else:
            results.append(
                Baseline(
                    name=f"always-{node}",
                    rca_depth=depth,
                    overconfident_guesses=undiagnosable,
                    brier=_constant_brier(suite, node),
                    detail=f"answers {node} to every scenario",
                )
            )

    # A uniform guess over the vocabulary, computed exactly rather than sampled.
    uniform = statistics.fmean(
        statistics.fmean(s.credit_for(node) for node in NODES) for s in suite
    )
    results.append(
        Baseline(
            name="uniform-random",
            rca_depth=uniform,
            detail=f"expected score of a uniform guess over {len(NODES)} nodes",
        )
    )

    return sorted(results, key=lambda b: (-b.rca_depth, b.name))


def baseline_floor(scenarios: list[Scenario]) -> Baseline:
    """The single best degenerate strategy on the runnable suite."""
    return baselines(scenarios)[0]


def degenerate(scenarios: list[Scenario]) -> list[Baseline]:
    """Constant *component* answers scoring at or above the threshold.

    Abstention is excluded: scoring meaningfully by refusing to answer is
    intended (see the README), as long as it stays beatable.
    """
    return [
        b
        for b in baselines(scenarios)
        if b.name.startswith("always-")
        and b.name != "always-abstain"
        and b.rca_depth >= DEGENERACY_THRESHOLD
    ]


def format_report(report: RunReport, scenarios: list[Scenario]) -> str:
    """Render a run against the baselines, as the README leaderboard shows it."""
    floor = baseline_floor(scenarios)
    excluded = len(report.excluded_blocked)
    lines = [
        f"config: {report.config}",
        f"  scored scenarios       {len(report.scores)} runnable"
        + (f" ({excluded} blocked, excluded)" if excluded else ""),
        f"  RCA depth              {report.rca_depth:.3f}",
        f"  best degenerate floor  {floor.rca_depth:.3f}  ({floor.name})",
        f"  margin over floor      {report.rca_depth - floor.rca_depth:+.3f}",
        f"  Brier (lower better)   {report.brier:.3f}",
        f"  floor's best Brier     {floor.brier if floor.brier is not None else float('nan'):.3f}"
        "  (Brier alone is gameable; read it with RCA depth)",
        f"  correct abstentions    {report.correct_abstentions}",
        f"  overconfident guesses  {report.overconfident_guesses}",
        f"  ruled out true cause   {report.ruled_out_root_causes}",
        f"  action FP rate         {report.action_fp_rate:.3f}"
        f"  (over {report.action_scenarios} scenarios with action ground truth)",
    ]
    if report.median_time_to_hypothesis_s is not None:
        lines.append(
            f"  median time-to-hyp     {report.median_time_to_hypothesis_s:.0f}s (self-reported)"
        )
    if report.missing:
        lines.append(
            f"  unanswered             {len(report.missing)} (scored 0.0 depth, Brier 1.0)"
        )
    hits = report.distractors_hit
    if hits:
        joined = ", ".join(f"{k} x{v}" for k, v in sorted(hits.items()))
        lines.append(f"  distractors hit        {joined}")
    # The version too: 0.2.0 changed how a missing answer scores, which no
    # file hash can show.
    lines.append(f"  suite version          {__version__}")
    if report.fingerprint:
        lines.append(
            f"  suite fingerprint      {report.fingerprint}"
            "  (ground truth, cluster files, injection code)"
        )
    return "\n".join(lines)
