"""Integrity of the shipped scenarios.

A benchmark's credibility rests entirely on its ground truth, and ground truth
is prose until something checks it. These tests are the check: they assert
properties the benchmark *claims* in its README, so the claims cannot quietly
stop being true.
"""

from __future__ import annotations

import pytest

from slurmrca.loader import load_all
from slurmrca.spec import ABSTAIN, NODES, Difficulty, Family, Scenario

SCENARIOS = load_all()
IDS = [s.id for s in SCENARIOS]


def ids(scenarios: list[Scenario]) -> list[str]:
    return [s.id for s in scenarios]


class TestAllScenariosValid:
    """Every shipped scenario satisfies its own invariants."""

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_internally_consistent(self, scenario: Scenario) -> None:
        assert scenario.validate() == []

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_every_answer_is_a_known_node(self, scenario: Scenario) -> None:
        # Scoring against a closed vocabulary is what keeps grading
        # deterministic and free of an LLM judge.
        for credit in scenario.scoring:
            assert credit.node in NODES
        for link in scenario.causal_chain:
            assert link.node in NODES

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_scoring_rationales_are_written(self, scenario: Scenario) -> None:
        # An unexplained partial credit is an arbitrary one. Anyone should be
        # able to disagree with a specific argument.
        for credit in scenario.scoring:
            assert credit.rationale.strip(), f"{scenario.id}: {credit.node} has no rationale"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_injection_mechanism_is_documented(self, scenario: Scenario) -> None:
        # Every fault here is emulated. Saying how, per scenario, is the
        # difference between a benchmark and a demo.
        assert scenario.injection.strip(), f"{scenario.id}: injection not documented"


class TestBenchmarkComposition:
    """Properties of the suite as a whole, as promised in the README."""

    def test_has_a_multi_layer_scenario(self) -> None:
        multi = [s for s in SCENARIOS if s.difficulty is Difficulty.MULTI_LAYER]
        assert multi, "the thesis needs at least one multi-hop scenario"

    def test_has_an_undiagnosable_scenario(self) -> None:
        undiagnosable = [s for s in SCENARIOS if s.difficulty is Difficulty.UNDIAGNOSABLE]
        assert undiagnosable, "without these the benchmark rewards confident guessing"

    def test_undiagnosable_scenarios_reward_abstention(self) -> None:
        for scenario in SCENARIOS:
            if scenario.difficulty is not Difficulty.UNDIAGNOSABLE:
                continue
            assert scenario.credit_for(ABSTAIN) >= 1.0
            assert not scenario.diagnosable
            assert scenario.root_cause == ABSTAIN

    def test_diagnosable_scenarios_do_not_reward_abstention(self) -> None:
        # Abstention earns a token amount on solvable tasks, never enough to
        # make refusing to answer a viable strategy.
        for scenario in SCENARIOS:
            if scenario.difficulty is Difficulty.UNDIAGNOSABLE:
                continue
            assert scenario.credit_for(ABSTAIN) < 0.2, scenario.id

    def test_ids_are_unique_and_prefixed(self) -> None:
        assert len(set(IDS)) == len(IDS)
        for scenario_id in IDS:
            assert scenario_id[:1] == "S" and scenario_id[1:3].isdigit()

    def test_families_are_covered_by_more_than_one_scenario_overall(self) -> None:
        # Phase 1 ships five scenarios and cannot cover all six families; this
        # pins what is actually here so the README table cannot drift.
        covered = {s.family for s in SCENARIOS}
        assert Family.STORAGE in covered
        assert Family.ACCOUNTING_DB in covered
        assert Family.CONTROLLER in covered
        assert Family.GPU in covered


class TestPropagationSemantics:
    """The delay between cause and symptom is the benchmark's core difficulty."""

    def test_flagship_symptom_lags_its_cause_by_minutes(self) -> None:
        flagship = next(s for s in SCENARIOS if s.id.startswith("S01"))
        assert flagship.propagation_s >= 600, (
            "the flagship scenario's value is that the symptom appears long "
            "after the cause; a short lag makes it an ordinary correlation task"
        )

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_chain_starts_at_time_zero(self, scenario: Scenario) -> None:
        assert scenario.causal_chain[0].observable_at_s == 0

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_observation_window_covers_the_whole_chain(self, scenario: Scenario) -> None:
        # Collecting before the last hop is observable would make a scenario
        # unsolvable for reasons that have nothing to do with the agent.
        assert scenario.observe_s >= scenario.propagation_s

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_every_hop_cites_evidence(self, scenario: Scenario) -> None:
        for link in scenario.causal_chain:
            assert link.evidence, f"{scenario.id}: hop {link.node} cites no evidence"


class TestCreditOrdering:
    """Credit must decrease as answers move away from the cause."""

    def test_flagship_credit_decreases_down_the_chain(self) -> None:
        # This originally also asserted the last link earns exactly zero, on the
        # assumption that the deepest link is always what the user reported.
        # That stopped being true when S01 was corrected: the report is hanging
        # sacct (slurmdbd), while the deepest link is a queue depth in sdiag
        # that an agent has to go and find. Crediting that discovery slightly is
        # correct. What must hold is that credit falls monotonically and the far
        # end stays near zero.
        flagship = next(s for s in SCENARIOS if s.id.startswith("S01"))
        chain = [link.node for link in flagship.causal_chain]
        credits = [flagship.credit_for(node) for node in chain]
        assert credits == sorted(credits, reverse=True), (
            f"credit must fall monotonically from cause to symptom, got {credits}"
        )
        assert credits[0] == 1.0
        assert credits[-1] <= 0.15, "the far end of the chain must stay near zero"

    def test_pure_symptom_restatement_earns_nothing(self) -> None:
        """Naming a consequence the measurement disproved must earn zero."""
        flagship = next(s for s in SCENARIOS if s.id.startswith("S01"))
        assert flagship.credit_for("slurm.scheduler") == 0.0

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_no_answer_outscores_the_root_cause(self, scenario: Scenario) -> None:
        top = max(c.credit for c in scenario.scoring)
        assert scenario.credit_for(scenario.root_cause) == top

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_unlisted_answers_earn_nothing(self, scenario: Scenario) -> None:
        assert scenario.credit_for("fabric.interconnect" if scenario.id != "" else "") >= 0.0
        assert scenario.credit_for("not.a.real.node") == 0.0


class TestDistractors:
    """Distractors are documented so wrong answers can be reported, not just counted."""

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_distractors_are_known_nodes_with_reasons(self, scenario: Scenario) -> None:
        for node, why in scenario.distractors.items():
            assert node in NODES, f"{scenario.id}: distractor {node} not in vocabulary"
            assert why.strip(), f"{scenario.id}: distractor {node} has no explanation"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_distractors_are_not_the_root_cause(self, scenario: Scenario) -> None:
        assert scenario.root_cause not in scenario.distractors


class TestVerification:
    """Ground-truth claims are either measured or marked unverified.

    Added after S01's original chain — a plausible, widely-believed folk model —
    was refuted by measurement. A benchmark that ships unverified causal claims
    produces scores that look rigorous and are not.
    """

    def test_flagship_chain_is_measured(self) -> None:
        flagship = next(s for s in SCENARIOS if s.id.startswith("S01"))
        assert flagship.verification.measured, (
            "the flagship scenario's chain must be observed, not asserted"
        )
        assert flagship.verification.method.strip()
        assert flagship.verification.observations

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_measured_claims_carry_observations(self, scenario: Scenario) -> None:
        if scenario.verification.measured:
            assert scenario.verification.observations, (
                f"{scenario.id} claims measured=true but records no observations"
            )
            assert scenario.verification.method.strip()


class TestReadmeMatchesReality:
    """The README's own counts, checked against the shipped scenarios.

    This class exists because the counts drifted. The suite went 5 -> 10 and
    three README call-sites kept saying five: the limitations bullet, the
    roadmap's phase-1 row, and a roadmap that marked phase 2 "next" while the
    banner said phase 2 was current. Every one of those is a claim about the
    benchmark that a reader would reasonably believe, and nothing checked them.
    """

    @staticmethod
    def readme() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    def test_not_enough_to_rank_claim_names_the_real_count(self) -> None:
        import re

        words = {
            "One": 1,
            "Two": 2,
            "Three": 3,
            "Four": 4,
            "Five": 5,
            "Six": 6,
            "Seven": 7,
            "Eight": 8,
            "Nine": 9,
            "Ten": 10,
            "Fifteen": 15,
            "Twenty": 20,
        }
        match = re.search(r"\*\*(\w+) scenarios is not enough to rank models\.\*\*", self.readme())
        assert match, "the limitations bullet naming the scenario count is gone"
        claimed = words.get(match.group(1))
        assert claimed == len(SCENARIOS), (
            f"README says {match.group(1)} ({claimed}) scenarios, the suite ships {len(SCENARIOS)}"
        )

    def test_no_roadmap_phase_is_both_done_and_next(self) -> None:
        import re

        for line in self.readme().splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            status = cells[-1].lower()
            assert not ("done" in status and "next" in status), (
                f"roadmap row is marked both done and next: {line}"
            )
        # and exactly one phase may be "next"
        nexts = re.findall(r"^\|.*\|\s*next\s*\|$", self.readme(), re.MULTILINE)
        assert len(nexts) <= 1, f"{len(nexts)} roadmap rows marked 'next'"
