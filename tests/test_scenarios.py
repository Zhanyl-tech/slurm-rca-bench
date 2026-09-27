"""Integrity of the shipped scenarios.

A benchmark's credibility rests entirely on its ground truth, and ground truth
is prose until something checks it. These tests are the check: they assert
properties the benchmark *claims* in its README, so the claims cannot quietly
stop being true. Most per-scenario invariants now live in
:meth:`Scenario.validate` (so third-party scenarios get them too) and are
tested rule by rule in test_validation.py; this file checks the shipped suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from slurmrca.cli import main
from slurmrca.loader import load_all, runnable
from slurmrca.spec import ABSTAIN, NODES, Difficulty, Family, Scenario, Status

SCENARIOS = load_all()
IDS = [s.id for s in SCENARIOS]
RUNNABLE = runnable(SCENARIOS)
FLAGSHIP = next(s for s in SCENARIOS if s.id.startswith("S01"))
ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
#: The README with line wrapping collapsed, for checks on prose sentences.
README_FLAT = " ".join(README.split())


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

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_changed_injections_say_they_have_not_been_rerun(self, scenario: Scenario) -> None:
        # Every injection or heal changed in the 2026-09 audit fixes; none has
        # been run on the emulated cluster since. Each YAML must say so.
        text = " ".join(scenario.injection.lower().split())
        assert "not yet run" in text or "not been re-run" in text, scenario.id


class TestBenchmarkComposition:
    """Properties of the suite as a whole, as promised in the README."""

    def test_has_a_runnable_multi_layer_scenario(self) -> None:
        multi = [s for s in RUNNABLE if s.difficulty is Difficulty.MULTI_LAYER]
        assert multi, "the thesis needs at least one multi-hop scenario"

    def test_has_a_runnable_undiagnosable_scenario(self) -> None:
        undiagnosable = [s for s in RUNNABLE if s.difficulty is Difficulty.UNDIAGNOSABLE]
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

    def test_designed_suite_covers_all_six_families(self) -> None:
        # The README says "ten scenarios across all six families". True of the
        # designed suite; the runnable subset is checked separately below.
        assert {s.family for s in SCENARIOS} == set(Family)

    def test_runnable_families_match_the_readme(self) -> None:
        covered = sorted(f.value for f in {s.family for s in RUNNABLE})
        match = re.search(r"runnable scenarios cover (\d) families: ([a-z_, `]+)\.", README_FLAT)
        assert match, "the README sentence naming the runnable families is gone"
        named = sorted(re.findall(r"`([a-z_]+)`", match.group(2)))
        assert int(match.group(1)) == len(covered) and named == covered

    def test_blocked_scenarios_say_why(self) -> None:
        for scenario in SCENARIOS:
            if scenario.status is Status.BLOCKED:
                assert len(scenario.blocked_reason.split()) > 10, scenario.id


class TestPropagationSemantics:
    """The delay between cause and symptom is the benchmark's core difficulty."""

    def test_flagship_lag_is_tied_to_a_measured_observation(self) -> None:
        # This used to hard-code propagation_s >= 600. The measured signal
        # appeared at t+180s on one run and t+840s on another, so the test now
        # requires the chain's timing to be one the record actually contains.
        observations = FLAGSHIP.verification.observations
        assert f"t+{FLAGSHIP.propagation_s}s" in observations
        assert FLAGSHIP.propagation_s >= 60, "a lag under a minute is an ordinary correlation task"

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

    @pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.diagnosable], ids=str)
    def test_credit_decreases_down_every_chain(self, scenario: Scenario) -> None:
        # Once tested for S01 only; now enforced by validate() for every
        # scenario, and checked here on the shipped suite.
        credits = [scenario.credit_for(link.node) for link in scenario.causal_chain]
        assert credits == sorted(credits, reverse=True), scenario.id
        assert credits[0] == 1.0

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_reported_component_earns_little(self, scenario: Scenario) -> None:
        # The README used to say "the symptom earns zero", which was false for
        # S01 (0.30) and S02 (0.2), and validate() checked the chain's last hop
        # rather than the component the ticket names.
        if scenario.reported_node != scenario.root_cause:
            assert scenario.credit_for(scenario.reported_node) <= 0.2, scenario.id

    def test_pure_symptom_restatement_earns_nothing(self) -> None:
        """Naming a consequence the measurement disproved must earn zero."""
        assert FLAGSHIP.credit_for("slurm.scheduler") == 0.0

    def test_flagship_full_credit_is_something_the_cluster_contains(self) -> None:
        # The emulated cluster (cluster/docker-compose.yml) has a database,
        # slurmdbd, slurmctld and one worker. The Slurm containers share
        # volumes, but nothing shared sits beneath the database.
        assert FLAGSHIP.root_cause == "db.mysql"
        assert FLAGSHIP.credit_for("storage.shared_fs") == 0.0
        compose = (
            Path(__file__).resolve().parent.parent / "cluster/docker-compose.yml"
        ).read_text()
        assert "mysql:" in compose

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_no_answer_outscores_the_root_cause(self, scenario: Scenario) -> None:
        top = max(c.credit for c in scenario.scoring)
        assert scenario.credit_for(scenario.root_cause) == top

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_unlisted_answers_earn_nothing(self, scenario: Scenario) -> None:
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


class TestSymptomsDoNotLeakTheAnswer:
    """The ticket must not contain the evidence the agent is meant to find."""

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_symptom_does_not_quote_root_hop_evidence(self, scenario: Scenario) -> None:
        # S06's ticket used to quote the controller's exact log line.
        symptom = " ".join(scenario.symptom.lower().split())
        for evidence in scenario.causal_chain[0].evidence:
            for quoted in re.findall(r"'([^']{12,})'", evidence):
                fragment = quoted.split(": ")[-1].lower()
                assert fragment not in symptom, f"{scenario.id} ticket quotes {quoted!r}"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_symptom_does_not_name_the_root_cause_node(self, scenario: Scenario) -> None:
        if scenario.diagnosable:
            assert scenario.root_cause not in scenario.symptom

    def test_undiagnosable_tickets_do_not_announce_it(self) -> None:
        # S10's ticket used to say "nobody can find a pattern".
        for scenario in SCENARIOS:
            if scenario.difficulty is Difficulty.UNDIAGNOSABLE:
                text = scenario.symptom.lower()
                assert "no pattern" not in text and "find a pattern" not in text

    def test_no_ticket_uses_reason_codes_slurm_does_not_have(self) -> None:
        # S08 quoted QOSMaxJobs / AssocMaxJobs; job_reason_codes.html has
        # neither (it has AssocMaxJobsLimit, QOSMaxJobsPerUserLimit, ...).
        for scenario in SCENARIOS:
            assert not re.search(
                r"\b(QOSMaxJobs|AssocMaxJobs)\b(?!Limit|PerUser)", scenario.symptom
            )


class TestVerification:
    """Ground-truth claims are either measured or marked unverified.

    Added after S01's original chain — a plausible, widely-believed folk model —
    was refuted by measurement. A benchmark that ships unverified causal claims
    produces scores that look rigorous and are not.
    """

    def test_flagship_chain_is_measured(self) -> None:
        assert FLAGSHIP.verification.measured, (
            "the flagship scenario's chain must be observed, not asserted"
        )
        assert FLAGSHIP.verification.method.strip()
        assert FLAGSHIP.verification.observations

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
    def test_measured_claims_carry_observations(self, scenario: Scenario) -> None:
        if scenario.verification.measured:
            assert scenario.verification.observations, (
                f"{scenario.id} claims measured=true but records no observations"
            )
            assert scenario.verification.method.strip()

    def test_blocked_scenarios_are_never_marked_measured(self) -> None:
        for scenario in SCENARIOS:
            if not scenario.runnable:
                assert not scenario.verification.measured, scenario.id


class TestReadmeMatchesReality:
    """The README's own claims, checked against the shipped scenarios.

    This class exists because the counts drifted. The suite went 5 -> 10 and
    three README call-sites kept saying five: the limitations bullet, the
    roadmap's phase-1 row, and a roadmap that marked phase 2 "next" while the
    banner said phase 2 was current. Every one of those is a claim about the
    benchmark that a reader would reasonably believe, and nothing checked them.
    The baseline block and the S01 credit table drifted the same way (the
    table said slurm.slurmctld earned 0.15 when the YAML said 0.1), so both are
    now checked too.
    """

    WORDS: ClassVar[dict[str, int]] = {
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

    def test_not_enough_to_rank_claim_names_the_real_count(self) -> None:
        match = re.search(r"\*\*(\w+) scenarios is not enough to rank models", README_FLAT)
        assert match, "the limitations bullet naming the scenario count is gone"
        claimed = self.WORDS.get(match.group(1))
        assert claimed == len(SCENARIOS), (
            f"README says {match.group(1)} ({claimed}) scenarios, the suite ships {len(SCENARIOS)}"
        )

    def test_runnable_and_blocked_counts(self) -> None:
        match = re.search(r"(\w+) are runnable and (\w+) are blocked", README_FLAT)
        assert match, "the README sentence with runnable/blocked counts is gone"
        assert self.WORDS[match.group(1).capitalize()] == len(RUNNABLE)
        assert self.WORDS[match.group(2).capitalize()] == len(SCENARIOS) - len(RUNNABLE)

    def test_scenario_table_status_column_matches(self) -> None:
        for scenario in SCENARIOS:
            short = scenario.id.split("-")[0]
            row = next(
                line
                for line in README.splitlines()
                if line.startswith("|") and re.search(rf"\b{short}\b", line)
            )
            if scenario.runnable:
                assert "blocked" not in row, row
            else:
                assert "blocked" in row, row
            assert ("✅" in row) == scenario.verification.measured, row
            # "Not yet re-run" only where a run was recorded before the fixes;
            # on the others it implied live runs that never happened.
            if scenario.verification.observations:
                assert "fixed, not yet re-run on the emulated cluster" in row, row
            else:
                assert "fixed; no recorded run on the emulated cluster" in row, row

    def test_banner_names_the_scenarios_with_recorded_runs(self) -> None:
        unquoted = " ".join(re.sub(r"^> ?", "", README, flags=re.M).split())
        match = re.search(r"(\w+) scenarios \(((?:S\d\d, )*S\d\d)\) have recorded runs", unquoted)
        assert match, "the banner sentence naming the scenarios with recorded runs is gone"
        named = re.findall(r"S\d\d", match.group(2))
        expected = [s.id[:3] for s in SCENARIOS if s.verification.observations]
        assert named == expected
        assert self.WORDS[match.group(1)] == len(expected)

    def test_runnable_but_unmeasured_scenarios_are_named(self) -> None:
        # Runnable means "not known to be impossible"; the README must say
        # which runnable scenarios have never been seen to work, because the
        # published floor is computed over them.
        match = re.search(
            r"(\w+) \(((?:S\d\d, )*S\d\d)\) have injections that have never been seen", README_FLAT
        )
        assert match, "the limitations bullet naming unmeasured runnable scenarios is gone"
        named = re.findall(r"S\d\d", match.group(2))
        expected = [s.id[:3] for s in RUNNABLE if not s.verification.measured]
        assert named == expected
        assert self.WORDS[match.group(1).capitalize()] == len(expected)

    def test_brier_floors_in_the_prose_match_the_baselines(self) -> None:
        # They were quoted with no command that printed them.
        from slurmrca.scoring import baselines

        by_name = {b.name: b.brier for b in baselines(SCENARIOS)}
        quoted = re.findall(
            r"`(always-[a-z._]+)` \(best confidence [0-9/]+\) scores Brier (\d\.\d{3})",
            README_FLAT,
        )
        assert len(quoted) >= 2, "the Brier floors in the Calibration bullet are gone"
        for name, value in quoted:
            brier = by_name[name]
            assert brier is not None and f"{brier:.3f}" == value, name
        assert by_name["always-network.control_plane"] == 0.0
        assert "perfect 0.000 (for example `always-network.control_plane`" in README_FLAT

    def test_no_claim_the_cluster_has_no_shared_filesystem(self) -> None:
        # The Slurm containers share volumes (/data, /etc/slurm, /etc/munge,
        # /var/log/slurm). What is true is narrower: nothing shared sits
        # beneath the database. Several files said the cluster had no shared
        # filesystem at all.
        compose = (ROOT / "cluster" / "docker-compose.yml").read_text()
        assert "slurm_jobdir:/data" in compose, "the premise of this test changed"
        pattern = re.compile(r"(has|is) no shared filesystem|no shared filesystem in this", re.I)
        texts = {"README.md": README, "CHANGELOG.md": (ROOT / "CHANGELOG.md").read_text()}
        for scenario in SCENARIOS:
            texts[scenario.id] = (ROOT / "scenarios" / scenario.id / "scenario.yaml").read_text()
        for name, text in texts.items():
            assert not pattern.search(" ".join(text.split())), name

    def test_no_untested_claims_in_the_flagship_title(self) -> None:
        # Whether any log line mentions the stall was never recorded.
        row = next(line for line in README.splitlines() if line.startswith("| **S01**"))
        for text in (FLAGSHIP.title, row):
            assert "no error anywhere" not in text

    def test_config_files_are_not_said_to_name_every_edge(self) -> None:
        # Neither slurm.conf nor slurmdbd.conf names the storage under the
        # database, the chain's first edge.
        assert "name every edge" not in README_FLAT

    def test_baseline_block_matches_the_cli(self) -> None:
        block = re.search(r"```\n(  degenerate baselines over .*?)```", README, re.DOTALL)
        assert block, "the README baseline block is gone"
        result = CliRunner().invoke(main, ["baselines"], color=False)
        assert result.exit_code == 0
        readme_rows = [line.rstrip() for line in block.group(1).splitlines() if line.strip()]
        cli_rows = [line.rstrip() for line in result.output.splitlines() if line.strip()]
        assert cli_rows[: len(readme_rows)] == readme_rows

    def test_flagship_credit_table_matches_the_yaml(self) -> None:
        section = README.split("| Answer on S01 |", 1)[1].split("\n\n", 1)[0]
        rows = re.findall(r"^\| `([a-z_.]+)`(?: \(abstain\))? \| \**([0-9.]+)\** \|", section, re.M)
        assert rows, "the S01 credit table is gone"
        table = {node: float(credit) for node, credit in rows}
        yaml = {c.node: c.credit for c in FLAGSHIP.scoring}
        assert table == yaml

    def test_no_roadmap_phase_is_both_done_and_next(self) -> None:
        for line in README.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            status = cells[-1].lower()
            assert not ("done" in status and "next" in status), (
                f"roadmap row is marked both done and next: {line}"
            )
        nexts = re.findall(r"^\|.*\|\s*next\s*\|$", README, re.MULTILINE)
        assert len(nexts) <= 1, f"{len(nexts)} roadmap rows marked 'next'"

    def test_no_first_public_claim(self) -> None:
        # "The first public incident-diagnosis benchmark for HPC schedulers"
        # could not be verified, and the next sentence was contradicted by the
        # benchmark the README cites first.
        assert "first public" not in README_FLAT.lower()
        assert "every existing root-cause-analysis benchmark" not in README_FLAT.lower()

    def test_no_sigstop_description(self) -> None:
        # docker pause uses the cgroup freezer, not SIGSTOP.
        assert "SIGSTOP" not in README
        for scenario in SCENARIOS:
            assert "SIGSTOP" not in scenario.injection
