"""Scoring behaviour, and the anti-degeneracy property of the suite itself."""

from __future__ import annotations

import pytest

from slurmrca.loader import load_all
from slurmrca.scoring import (
    Answer,
    Baseline,
    baseline_floor,
    baselines,
    format_report,
    score_answer,
    score_run,
)
from slurmrca.spec import ABSTAIN, Difficulty, Scenario

SCENARIOS = load_all()
BY_ID = {s.id: s for s in SCENARIOS}


def scenario(prefix: str) -> Scenario:
    return next(s for s in SCENARIOS if s.id.startswith(prefix))


class TestAnswerValidation:
    def test_rejects_answers_outside_the_vocabulary(self) -> None:
        # Free-text answers would force an LLM judge; the closed vocabulary is
        # what keeps grading a dictionary lookup.
        with pytest.raises(ValueError, match="not a known node"):
            Answer("S01-storage-stall-scheduling-halt", "the database is sad")

    def test_rejects_impossible_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Answer("S01-storage-stall-scheduling-halt", ABSTAIN, confidence=1.5)

    def test_rejects_mismatched_scenario(self) -> None:
        with pytest.raises(ValueError, match="answer is for"):
            score_answer(scenario("S01"), Answer("S02-dbd-lock-contention", ABSTAIN))


class TestRcaDepth:
    def test_root_cause_earns_full_credit(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "storage.shared_fs"))
        assert got.rca_depth == 1.0

    def test_downstream_answer_earns_partial(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "db.mysql"))
        assert 0 < got.rca_depth < 1.0

    def test_unlisted_answer_earns_nothing(self) -> None:
        s = scenario("S01")
        assert score_answer(s, Answer(s.id, "gpu.device")).rca_depth == 0.0


class TestCalibration:
    def test_confident_and_right_scores_well(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, s.root_cause, confidence=0.95))
        assert got.brier < 0.01

    def test_confident_and_wrong_is_punished(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "gpu.device", confidence=0.95))
        assert got.brier > 0.9

    def test_hedged_and_wrong_beats_confident_and_wrong(self) -> None:
        # The whole point of calibration: being wrong is survivable, being
        # *certain* and wrong is what gets a node drained at 3am.
        s = scenario("S01")
        hedged = score_answer(s, Answer(s.id, "gpu.device", confidence=0.2)).brier
        certain = score_answer(s, Answer(s.id, "gpu.device", confidence=0.95)).brier
        assert hedged < certain


class TestUndiagnosable:
    def test_abstention_is_scored_correct(self) -> None:
        for s in SCENARIOS:
            if s.difficulty is not Difficulty.UNDIAGNOSABLE:
                continue
            got = score_answer(s, Answer(s.id, ABSTAIN, confidence=0.9))
            assert got.rca_depth == 1.0
            assert got.correct_abstention
            assert not got.overconfident_guess

    def test_naming_any_cause_is_flagged_and_scores_zero(self) -> None:
        for s in SCENARIOS:
            if s.difficulty is not Difficulty.UNDIAGNOSABLE:
                continue
            for guess in ("gpu.device", "storage.shared_fs", "fabric.interconnect"):
                got = score_answer(s, Answer(s.id, guess, confidence=0.8))
                assert got.rca_depth == 0.0, f"{s.id}: {guess} must earn nothing"
                assert got.overconfident_guess

    def test_diagnosable_abstention_is_not_flagged_as_overconfident(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, ABSTAIN))
        assert not got.overconfident_guess
        assert not got.correct_abstention


class TestActions:
    def test_unlisted_actions_count_as_false_positives(self) -> None:
        s = scenario("S06")
        got = score_answer(
            s,
            Answer(s.id, s.root_cause, actions_proposed=("restart slurmctld", "reboot the node")),
        )
        assert got.action_false_positives == 2
        assert got.action_fp_rate == 1.0

    def test_documented_helpful_actions_are_not_penalised(self) -> None:
        s = scenario("S06")
        assert s.remediation_helpful, "S06 must document its remediation"
        got = score_answer(
            s, Answer(s.id, s.root_cause, actions_proposed=(s.remediation_helpful[0],))
        )
        assert got.action_false_positives == 0

    def test_proposing_nothing_is_never_penalised(self) -> None:
        s = scenario("S01")
        assert score_answer(s, Answer(s.id, s.root_cause)).action_fp_rate == 0.0


class TestRunAggregation:
    def test_unanswered_scenarios_are_scored_not_skipped(self) -> None:
        # Otherwise an agent inflates its average by declining hard scenarios.
        one = SCENARIOS[0]
        report = score_run(SCENARIOS, [Answer(one.id, one.root_cause, confidence=0.9)])
        assert len(report.scores) == len(SCENARIOS)
        assert len(report.missing) == len(SCENARIOS) - 1
        assert report.rca_depth < 1.0

    def test_perfect_run_scores_one(self) -> None:
        answers = [Answer(s.id, s.root_cause, confidence=0.9) for s in SCENARIOS]
        report = score_run(SCENARIOS, answers, config="oracle")
        assert report.rca_depth == pytest.approx(1.0)
        assert report.overconfident_guesses == 0

    def test_distractors_are_reported(self) -> None:
        s = scenario("S01")
        distractor = next(iter(s.distractors))
        report = score_run(SCENARIOS, [Answer(s.id, distractor)])
        assert report.distractors_hit.get(distractor) == 1

    def test_format_report_mentions_the_floor(self) -> None:
        answers = [Answer(s.id, s.root_cause, confidence=0.9) for s in SCENARIOS]
        text = format_report(score_run(SCENARIOS, answers, config="oracle"), SCENARIOS)
        assert "floor" in text and "margin over floor" in text


class TestSuiteIsNotDegenerate:
    """The suite must not be solvable by a constant answer.

    This is the property that makes every published number meaningful, and it is
    checked rather than assumed. When the suite held five scenarios,
    ``always-db.mysql`` scored 0.290 — because S01 and S02 both rewarded it —
    which made a large fraction of any agent's score reachable without looking
    at telemetry at all. Adding scenarios whose causes lie elsewhere dropped it
    to 0.145. These thresholds keep it there.
    """

    def test_no_constant_component_answer_scores_well(self) -> None:
        component_baselines = [b for b in baselines(SCENARIOS) if b.name != "always-abstain"]
        worst: Baseline = max(component_baselines, key=lambda b: b.rca_depth)
        assert worst.rca_depth < 0.25, (
            f"{worst.name} scores {worst.rca_depth:.3f} without reading any telemetry; "
            "add scenarios whose root causes lie elsewhere rather than retuning weights"
        )

    def test_always_abstain_is_competitive_but_beatable(self) -> None:
        # Abstention should score meaningfully — a system that says "I don't
        # know" is safe — but must not be a winning strategy, or the benchmark
        # rewards refusing to work.
        abstain = next(b for b in baselines(SCENARIOS) if b.name == "always-abstain")
        assert 0.1 < abstain.rca_depth < 0.4

    def test_an_oracle_clearly_beats_the_floor(self) -> None:
        answers = [Answer(s.id, s.root_cause, confidence=0.9) for s in SCENARIOS]
        oracle = score_run(SCENARIOS, answers).rca_depth
        assert oracle - baseline_floor(SCENARIOS).rca_depth > 0.5

    def test_random_guessing_is_near_zero(self) -> None:
        uniform = next(b for b in baselines(SCENARIOS) if b.name == "uniform-random")
        assert uniform.rca_depth < 0.15
