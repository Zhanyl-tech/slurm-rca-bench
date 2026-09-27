"""The command line, end to end, without Docker.

cli.py had no tests at all (0% coverage), so a raw traceback on a broken
scenario, a validate that stopped at the first error, and an export that told
agents which tasks to abstain on all went unnoticed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import slurmrca.cli as cli
from slurmrca import __version__
from slurmrca.loader import load_all, runnable
from slurmrca.spec import Scenario
from tests.fakes import FakeCluster

SCENARIOS = load_all()
RUNNABLE = runnable(SCENARIOS)
ROOT = Path(__file__).resolve().parent.parent


def invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.main, list(args), color=False)
    return result.exit_code, result.output


def test_version() -> None:
    code, out = invoke("--version")
    assert code == 0 and "0.2.0" in out


def test_list_shows_status_and_counts() -> None:
    code, out = invoke("list")
    assert code == 0
    assert f"{len(SCENARIOS)} scenarios · {len(RUNNABLE)} runnable" in out
    assert "blocked" in out


def test_validate_passes_on_the_shipped_suite() -> None:
    code, out = invoke("validate")
    assert code == 0 and "ground truth consistent" in out


def test_validate_reports_every_broken_scenario(tmp_path: Path) -> None:
    # The first validate stopped at the first broken file.
    for name in ("S01-accounting-backend-stall", "S09-gpu-driver-node-drain"):
        raw = yaml.safe_load((ROOT / "scenarios" / name / "scenario.yaml").read_text())
        raw["observe_s"] = 1
        (tmp_path / name).mkdir()
        (tmp_path / name / "scenario.yaml").write_text(yaml.safe_dump(raw))
    code, out = invoke("--scenarios", str(tmp_path), "validate")
    assert code == 1
    assert "S01-accounting-backend-stall" in out and "S09-gpu-driver-node-drain" in out
    assert "2 problem(s)" in out


@pytest.mark.parametrize("command", ["list", "baselines", "export", "show S01"])
def test_broken_scenarios_give_a_message_not_a_traceback(tmp_path: Path, command: str) -> None:
    bad = tmp_path / "S01-x"
    bad.mkdir()
    (bad / "scenario.yaml").write_text("id: S01-x\n")
    result = CliRunner().invoke(cli.main, ["--scenarios", str(tmp_path), *command.split()])
    assert result.exit_code == 1
    assert "Error:" in result.output and "Traceback" not in result.output
    assert not isinstance(result.exception, ValueError)


def test_missing_scenario_directory_is_a_clean_error(tmp_path: Path) -> None:
    code, out = invoke("--scenarios", str(tmp_path / "nope"), "list")
    assert code != 0 and "Traceback" not in out


def test_show_accepts_prefix_and_legacy_id() -> None:
    for ident in ("S01", "S01-storage-stall-scheduling-halt"):
        code, out = invoke("show", ident)
        assert code == 0 and "S01-accounting-backend-stall" in out


def test_show_explains_blocked() -> None:
    code, out = invoke("show", "S08")
    assert code == 0 and "BLOCKED" in out and "AccountingStorageEnforce" in out


def test_show_rejects_unknown_and_ambiguous() -> None:
    assert invoke("show", "S99")[0] != 0
    code, out = invoke("show", "S0")
    assert code != 0 and "ambiguous" in out


def test_baselines_flag_the_known_degeneracy() -> None:
    code, out = invoke("baselines")
    assert code == 0
    assert f"over {len(RUNNABLE)} runnable scenarios" in out
    assert "DEGENERATE: always-db.mysql" in out


def test_baselines_include_blocked_is_labelled_as_not_a_floor() -> None:
    code, out = invoke("baselines", "--include-blocked")
    assert code == 0 and f"over {len(SCENARIOS)} designed" in out and "Not a floor" in out


class TestExport:
    def test_agent_view_hides_everything_but_the_ticket(self) -> None:
        code, out = invoke("export")
        assert code == 0
        payload = json.loads(out)
        assert len(payload) == len(RUNNABLE)
        assert all(set(item) == {"task_id", "symptom"} for item in payload)
        text = out.lower()
        for leak in ("undiagnosable", "diagnosable", "difficulty", "s05-", "s10-"):
            assert leak not in text

    def test_scorer_view_maps_task_ids_back(self) -> None:
        code, out = invoke("export", "--view", "scorer")
        assert code == 0
        payload = {item["task_id"]: item for item in json.loads(out)}
        for scenario in SCENARIOS:
            assert payload[scenario.task_id]["id"] == scenario.id
            assert payload[scenario.task_id]["canary"] == scenario.canary

    def test_scorer_view_carries_all_the_ground_truth_a_scorer_needs(self) -> None:
        # It was called "everything" while it left out the credit table and
        # the chain, so an external scorer could not compute RCA depth from it.
        code, out = invoke("export", "--view", "scorer")
        assert code == 0
        by_id = {item["id"]: item for item in json.loads(out)}
        for scenario in SCENARIOS:
            item = by_id[scenario.id]
            credits = {c["node"]: c["credit"] for c in item["scoring"]}
            for node in credits:
                assert credits[node] == scenario.credit_for(node)
            assert [link["node"] for link in item["causal_chain"]] == [
                link.node for link in scenario.causal_chain
            ]
            assert item["distractors"] == scenario.distractors
            helpful = scenario.remediation_helpful
            assert item["remediation_helpful"] == (None if helpful is None else list(helpful))
            assert item["verification"]["measured"] == scenario.verification.measured
            assert item["root_cause"] == scenario.root_cause
            assert item["status"] == scenario.status.value
        # Answers may use a retired id, so the mapping is exported too.
        assert by_id["S01-accounting-backend-stall"]["legacy_ids"] == [
            "S01-storage-stall-scheduling-halt"
        ]


def test_baselines_print_the_brier_the_readme_quotes() -> None:
    from slurmrca.scoring import baselines

    code, out = invoke("baselines", "--top", "50")
    assert code == 0
    printed = {
        match.group(3): match.group(2)
        for match in re.finditer(r"^\s+(\d\.\d{3})\s+(\d\.\d{3}|—)\s+(\S+)", out, re.M)
    }
    rows = baselines(SCENARIOS)
    assert len(printed) == len(rows)
    for row in rows:
        assert printed[row.name] == ("—" if row.brier is None else f"{row.brier:.3f}")


class TestScore:
    def _answers(self, tmp_path: Path, rows: list[dict[str, object]], jsonl: bool) -> Path:
        path = tmp_path / ("answers.jsonl" if jsonl else "answers.json")
        text = "\n".join(json.dumps(r) for r in rows) if jsonl else json.dumps(rows)
        path.write_text(text)
        return path

    @pytest.mark.parametrize("jsonl", [True, False])
    def test_scores_an_oracle_by_task_id(self, tmp_path: Path, jsonl: bool) -> None:
        rows: list[dict[str, object]] = [
            {"scenario_id": s.task_id, "root_cause": s.root_cause, "confidence": 0.9}
            for s in RUNNABLE
        ]
        code, out = invoke("score", str(self._answers(tmp_path, rows, jsonl)), "--config", "oracle")
        assert code == 0, out
        assert "RCA depth              1.000" in out
        assert "suite fingerprint" in out
        assert f"suite version          {__version__}" in out

    def test_empty_file_scores_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        code, out = invoke("score", str(path))
        assert code == 0 and "RCA depth              0.000" in out

    def test_rejects_duplicates_with_a_message(self, tmp_path: Path) -> None:
        s: Scenario = RUNNABLE[0]
        rows: list[dict[str, object]] = [{"scenario_id": s.id, "root_cause": s.root_cause}] * 2
        code, out = invoke("score", str(self._answers(tmp_path, rows, True)))
        assert code != 0 and "more than one answer" in out

    def test_rejects_bad_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("{not json")
        code, out = invoke("score", str(path))
        assert code != 0 and "Traceback" not in out


class TestHealAndRun:
    @pytest.fixture
    def fake(self, monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
        cluster = FakeCluster()
        monkeypatch.setattr(cli, "_make_cluster", lambda: cluster)
        return cluster

    def test_heal_all(self, fake: FakeCluster) -> None:
        code, out = invoke("heal", "--all")
        assert code == 0 and out.strip().endswith("healed")

    def test_heal_one_by_legacy_id(self, fake: FakeCluster) -> None:
        fake.states["mysql"] = "paused"
        code, out = invoke("heal", "S01-storage-stall-scheduling-halt")
        assert code == 0 and fake.states["mysql"] == "running"
        assert "healed S01-accounting-backend-stall" in out

    @pytest.mark.parametrize("args", [[], ["S01-accounting-backend-stall", "--all"]])
    def test_heal_needs_exactly_one_target(self, fake: FakeCluster, args: list[str]) -> None:
        assert invoke("heal", *args)[0] != 0

    def test_heal_all_reports_failures(self, fake: FakeCluster) -> None:
        from slurmrca.cluster import ExecResult

        fake.handler = lambda service, command: ExecResult(1, "", "nope")
        code, out = invoke("heal", "--all")
        assert code != 0 and "heal(s) failed" in out

    def test_run_refuses_blocked_scenarios(self, fake: FakeCluster) -> None:
        code, out = invoke("run", "S08")
        assert code != 0
        code, out = invoke("run", "S08-partition-limit-starvation")
        assert code != 0 and "blocked" in out
        assert not fake.calls

    def test_run_executes_the_lifecycle(self, fake: FakeCluster) -> None:
        code, out = invoke("run", "S01-accounting-backend-stall", "--observe-s", "0")
        assert code == 0, out
        assert ("pause", "mysql") in fake.calls and ("unpause", "mysql") in fake.calls
        assert fake.states["mysql"] == "running"
