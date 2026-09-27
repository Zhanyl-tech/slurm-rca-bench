"""Scoring behaviour, and the anti-degeneracy property of the suite itself."""

from __future__ import annotations

import pytest

from slurmrca.loader import load_all, runnable
from slurmrca.scoring import (
    DEGENERACY_THRESHOLD,
    MISSING,
    Answer,
    Baseline,
    answer_from_dict,
    baseline_floor,
    baselines,
    degenerate,
    format_report,
    score_answer,
    score_run,
)
from slurmrca.spec import ABSTAIN, Difficulty, Scenario

SCENARIOS = load_all()
RUNNABLE = runnable(SCENARIOS)
BY_ID = {s.id: s for s in SCENARIOS}
S01 = "S01-accounting-backend-stall"


def scenario(prefix: str) -> Scenario:
    return next(s for s in SCENARIOS if s.id.startswith(prefix))


def oracle() -> list[Answer]:
    return [Answer(s.id, s.root_cause, confidence=0.9) for s in RUNNABLE]


class TestAnswerValidation:
    def test_rejects_answers_outside_the_vocabulary(self) -> None:
        # Free-text answers would force an LLM judge; the closed vocabulary is
        # what keeps grading a dictionary lookup.
        with pytest.raises(ValueError, match="not a known node"):
            Answer(S01, "the database is sad")

    def test_rejects_impossible_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Answer(S01, ABSTAIN, confidence=1.5)

    def test_rejects_mismatched_scenario(self) -> None:
        with pytest.raises(ValueError, match="answer is for"):
            score_answer(scenario("S01"), Answer("S02-dbd-lock-contention", ABSTAIN))

    @pytest.mark.parametrize(
        "ruled_out",
        [("not.a.node",), (ABSTAIN,), ("gpu.device", "gpu.device")],
    )
    def test_rejects_invalid_ruled_out(self, ruled_out: tuple[str, ...]) -> None:
        # ruled_out used to accept anything, including nodes that do not exist.
        with pytest.raises(ValueError, match="ruled_out"):
            Answer(S01, "db.mysql", ruled_out=ruled_out)

    def test_rejects_ruling_out_its_own_answer(self) -> None:
        with pytest.raises(ValueError, match="also listed in ruled_out"):
            Answer(S01, "db.mysql", ruled_out=("db.mysql",))

    @pytest.mark.parametrize(
        "action",
        ["restart slurmctld", "restart:not.a.node", "reboot:slurm.slurmd", "repair:unknown"],
    )
    def test_rejects_free_text_actions(self, action: str) -> None:
        with pytest.raises(ValueError, match="verb"):
            Answer(S01, "db.mysql", actions_proposed=(action,))


class TestAnswerParsing:
    def test_round_trip(self) -> None:
        answer = answer_from_dict(
            {
                "scenario_id": S01,
                "root_cause": "db.mysql",
                "confidence": 0.72,
                "ruled_out": ["slurm.config"],
                "time_to_hypothesis_s": 94,
                "actions_proposed": ["investigate:db.mysql"],
            }
        )
        assert answer.confidence == 0.72 and answer.time_to_hypothesis_s == 94.0

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ({"scenario_id": S01, "root_cause": "db.mysql", "confidance": 1}, "unknown answer key"),
            ({"scenario_id": S01}, "root_cause"),
            ({"scenario_id": S01, "root_cause": "db.mysql", "confidence": True}, "number"),
            ({"scenario_id": S01, "root_cause": "db.mysql", "ruled_out": "gpu.device"}, "list"),
            ([], "JSON object"),
        ],
    )
    def test_rejects_malformed(self, raw: object, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            answer_from_dict(raw)


class TestRcaDepth:
    def test_root_cause_earns_full_credit(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "db.mysql"))
        assert got.rca_depth == 1.0

    def test_downstream_answer_earns_partial(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "slurm.slurmdbd"))
        assert 0 < got.rca_depth < 1.0

    def test_the_layer_the_cluster_does_not_have_earns_nothing(self) -> None:
        # S01 used to give storage.shared_fs full credit. Nothing shared sits
        # beneath the emulated cluster's database; full credit must go to
        # something the injection actually changes.
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "storage.shared_fs"))
        assert got.rca_depth == 0.0
        assert got.distractor == "storage.shared_fs"

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


class TestRuledOut:
    """ruled_out is validated and recorded, and does not change RCA depth."""

    def test_does_not_change_depth(self) -> None:
        s = scenario("S05")
        bare = score_answer(s, Answer(s.id, ABSTAIN))
        with_work = score_answer(s, Answer(s.id, ABSTAIN, ruled_out=("slurm.config", "db.mysql")))
        assert bare.rca_depth == with_work.rca_depth

    def test_ruling_out_the_true_cause_is_flagged(self) -> None:
        s = scenario("S01")
        got = score_answer(s, Answer(s.id, "slurm.slurmdbd", ruled_out=("db.mysql",)))
        assert got.ruled_out_root_cause
        report = score_run(SCENARIOS, [Answer(s.id, "slurm.slurmdbd", ruled_out=("db.mysql",))])
        assert report.ruled_out_root_causes == 1


class TestActions:
    def test_unlisted_actions_count_as_false_positives(self) -> None:
        s = scenario("S06")
        got = score_answer(
            s,
            Answer(
                s.id,
                s.root_cause,
                actions_proposed=("restart:slurm.slurmctld", "drain:slurm.slurmd"),
            ),
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

    def test_scenarios_without_action_ground_truth_are_not_scored(self) -> None:
        # S01 has no remediation list. The first metric counted every action
        # there as a false positive, correct ones included.
        s = scenario("S01")
        assert s.remediation_helpful is None
        got = score_answer(s, Answer(s.id, "db.mysql", actions_proposed=("restart:db.mysql",)))
        assert not got.actions_scored
        assert got.action_false_positives == 0

    def test_an_empty_helpful_list_means_every_action_is_unhelpful(self) -> None:
        s = scenario("S10")
        assert s.remediation_helpful == ()
        got = score_answer(s, Answer(s.id, ABSTAIN, actions_proposed=("drain:slurm.slurmd",)))
        assert got.actions_scored and got.action_false_positives == 1

    def test_run_rate_counts_only_scenarios_with_ground_truth(self) -> None:
        answers = [
            Answer(s.id, s.root_cause, actions_proposed=("restart:slurm.slurmctld",))
            for s in RUNNABLE
        ]
        report = score_run(SCENARIOS, answers)
        with_truth = [s for s in RUNNABLE if s.remediation_helpful is not None]
        assert report.action_scenarios == len(with_truth)
        assert report.action_fp_rate == 1.0


class TestRunAggregation:
    def test_unanswered_scenarios_score_zero_not_abstention(self) -> None:
        # The first version scored a missing answer as an abstention, which
        # earns full credit on undiagnosable scenarios.
        report = score_run(SCENARIOS, [])
        assert report.rca_depth == 0.0
        assert report.brier == 1.0
        assert report.correct_abstentions == 0
        assert len(report.missing) == len(RUNNABLE)
        assert all(s.answer == MISSING and s.missing for s in report.scores)

    def test_an_empty_submission_does_not_tie_the_floor(self) -> None:
        # Measured by the audit: `score_run(S, [])` used to score exactly the
        # published floor (0.240) with two "correct abstentions".
        assert score_run(SCENARIOS, []).rca_depth < baseline_floor(SCENARIOS).rca_depth

    def test_partial_run_is_averaged_over_the_whole_runnable_suite(self) -> None:
        one = RUNNABLE[0]
        report = score_run(SCENARIOS, [Answer(one.id, one.root_cause, confidence=0.9)])
        assert len(report.scores) == len(RUNNABLE)
        assert len(report.missing) == len(RUNNABLE) - 1
        assert report.rca_depth == pytest.approx(1.0 / len(RUNNABLE))

    def test_duplicate_answers_are_rejected(self) -> None:
        # 100 copies of one correct answer once lifted the mean to 0.939.
        s = scenario("S01")
        with pytest.raises(ValueError, match="more than one answer"):
            score_run(SCENARIOS, [Answer(s.id, s.root_cause)] * 100)

    def test_duplicates_are_caught_across_id_forms(self) -> None:
        s = scenario("S01")
        answers = [Answer(s.id, s.root_cause), Answer(s.task_id, s.root_cause)]
        with pytest.raises(ValueError, match="more than one answer"):
            score_run(SCENARIOS, answers)

    def test_unknown_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"not in the suite.*S99-nope"):
            score_run(SCENARIOS, [Answer("S99-nope", ABSTAIN)])

    def test_blocked_scenarios_are_not_scored(self) -> None:
        blocked = next(s for s in SCENARIOS if not s.runnable)
        with pytest.raises(ValueError, match="blocked"):
            score_run(SCENARIOS, [Answer(blocked.id, blocked.root_cause)])
        report = score_run(SCENARIOS, oracle())
        assert blocked.id in report.excluded_blocked
        assert blocked.id not in {sc.scenario_id for sc in report.scores}

    @pytest.mark.parametrize("form", ["legacy", "task"])
    def test_legacy_and_opaque_ids_resolve(self, form: str) -> None:
        s = scenario("S01")
        ident = "S01-storage-stall-scheduling-halt" if form == "legacy" else s.task_id
        report = score_run(SCENARIOS, [Answer(ident, "db.mysql", confidence=1.0)])
        got = next(sc for sc in report.scores if sc.scenario_id == s.id)
        assert got.rca_depth == 1.0 and not got.missing

    def test_perfect_run_scores_one(self) -> None:
        report = score_run(SCENARIOS, oracle(), config="oracle")
        assert report.rca_depth == pytest.approx(1.0)
        assert report.overconfident_guesses == 0
        assert not report.missing

    def test_distractors_are_reported(self) -> None:
        s = scenario("S01")
        distractor = next(iter(s.distractors))
        report = score_run(SCENARIOS, [Answer(s.id, distractor)])
        assert report.distractors_hit.get(distractor) == 1

    def test_format_report_mentions_the_floor_and_caveats(self) -> None:
        answers = [
            Answer(s.id, s.root_cause, confidence=0.9, time_to_hypothesis_s=30.0)
            for s in RUNNABLE[:-1]
        ]
        text = format_report(
            score_run(SCENARIOS, answers, config="partial", fingerprint="abc123"), SCENARIOS
        )
        assert "floor" in text and "margin over floor" in text
        assert "self-reported" in text
        assert "unanswered             1 (scored 0.0 depth, Brier 1.0)" in text
        assert "blocked, excluded" in text
        assert "abc123" in text


class TestBaselines:
    def test_computed_over_runnable_scenarios_only(self) -> None:
        runnable_rows = {b.name: b.rca_depth for b in baselines(SCENARIOS)}
        designed_rows = {b.name: b.rca_depth for b in baselines(SCENARIOS, include_blocked=True)}
        assert runnable_rows != designed_rows
        expected = sum(s.credit_for("db.mysql") for s in RUNNABLE) / len(RUNNABLE)
        assert runnable_rows["always-db.mysql"] == pytest.approx(expected)

    def test_constant_brier_is_exact(self) -> None:
        # always-abstain is right on the undiagnosable runnable scenarios only.
        rows = {b.name: b for b in baselines(SCENARIOS)}
        p = sum(1 for s in RUNNABLE if s.root_cause == ABSTAIN) / len(RUNNABLE)
        assert rows["always-abstain"].brier == pytest.approx(p * (1 - p))

    def test_a_never_right_answer_at_zero_confidence_has_perfect_brier(self) -> None:
        # Documented, not fixed: Brier measures calibration, not correctness,
        # which is why it is only ever reported next to RCA depth.
        rows = {b.name: b for b in baselines(SCENARIOS)}
        assert rows["always-network.control_plane"].rca_depth == 0.0
        assert rows["always-network.control_plane"].brier == 0.0
        answers = [Answer(s.id, "network.control_plane", confidence=0.0) for s in RUNNABLE]
        assert score_run(SCENARIOS, answers).brier == 0.0

    def test_empty_suite_has_no_baselines(self) -> None:
        assert baselines([]) == []


class TestSuiteIsNotDegenerate:
    """The suite must not be solvable by a constant answer.

    This is the property that makes every published number meaningful, and it is
    checked rather than assumed. When the suite held five scenarios,
    ``always-db.mysql`` scored 0.290 because S01 and S02 both rewarded it.
    Adding scenarios whose causes lie elsewhere dropped it to 0.145.

    It is violated again, knowingly. Correcting S01's full credit from
    storage.shared_fs (nothing shared sits beneath this database) to db.mysql, and
    excluding the four blocked scenarios from scoring, puts ``always-db.mysql``
    at 0.333 over the six runnable scenarios. Retuning weights to hide that
    would be exactly the wrong fix; the right one is more runnable scenarios
    whose causes lie elsewhere. Until then the invariant is a strict xfail (it
    turns into a failure the moment it starts passing, forcing this note to be
    removed) and a second test fails the build if any *other* constant answer
    crosses the threshold.
    """

    KNOWN = frozenset({"always-db.mysql"})

    @pytest.mark.xfail(
        strict=True,
        reason="S01 and S02 both have db.mysql as root cause; 2 of 6 runnable. See README.",
    )
    def test_no_constant_component_answer_scores_well(self) -> None:
        component_baselines = [b for b in baselines(SCENARIOS) if b.name != "always-abstain"]
        worst: Baseline = max(component_baselines, key=lambda b: b.rca_depth)
        assert worst.rca_depth < DEGENERACY_THRESHOLD, (
            f"{worst.name} scores {worst.rca_depth:.3f} without reading any telemetry; "
            "add scenarios whose root causes lie elsewhere rather than retuning weights"
        )

    def test_no_new_constant_answer_crosses_the_threshold(self) -> None:
        offenders = {b.name for b in degenerate(SCENARIOS)}
        assert offenders <= self.KNOWN, f"new degenerate constant answers: {offenders - self.KNOWN}"

    def test_the_known_degeneracy_is_still_real(self) -> None:
        # If this fails, the suite has been fixed: delete KNOWN and the xfail.
        assert {b.name for b in degenerate(SCENARIOS)} == self.KNOWN

    def test_always_abstain_is_competitive_but_beatable(self) -> None:
        # Abstention should score meaningfully — a system that says "I don't
        # know" is safe — but must not be a winning strategy, or the benchmark
        # rewards refusing to work.
        abstain = next(b for b in baselines(SCENARIOS) if b.name == "always-abstain")
        assert 0.1 < abstain.rca_depth < 0.4

    def test_an_oracle_clearly_beats_the_floor(self) -> None:
        oracle_depth = score_run(SCENARIOS, oracle()).rca_depth
        assert oracle_depth - baseline_floor(SCENARIOS).rca_depth > 0.5

    def test_random_guessing_is_near_zero(self) -> None:
        uniform = next(b for b in baselines(SCENARIOS) if b.name == "uniform-random")
        assert uniform.rca_depth < 0.15
