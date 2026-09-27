"""Every validation rule, broken one at a time.

An audit replaced the first line of ``Scenario.validate()`` with ``return []``
and all 187 tests still passed: nothing fed the validator an invalid scenario.
Each case below starts from a minimal valid scenario, makes exactly one
rule-breaking edit, and asserts the specific problem is reported. A validator
that silently stops checking a rule now fails the build.

The parser cases do the same for :func:`scenario_from_dict`, which used to turn
a string into a tuple of characters, ``"false"`` into True, and ignore
misspelled keys.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from slurmrca.loader import ScenarioError, load_all, load_report, load_scenario
from slurmrca.spec import SCHEMA_KEYS, Scenario, parse_action, scenario_from_dict

Raw = dict[str, Any]


def minimal() -> Raw:
    """The smallest scenario that passes every rule. Diagnosable, 3 hops."""
    return {
        "id": "S99-minimal-example",
        "title": "A minimal example",
        "family": "accounting_db",
        "difficulty": "multi_layer",
        "reported_node": "slurm.slurmdbd",
        "canary": "00000000-0000-4000-8000-000000000000",
        "symptom": "sacct hangs.",
        "injection": "docker pause on the database.",
        "observe_s": 120,
        "causal_chain": [
            {"node": "db.mysql", "state": "frozen", "observable_at_s": 0, "evidence": ["a"]},
            {"node": "slurm.slurmdbd", "state": "waiting", "observable_at_s": 5, "evidence": ["b"]},
            {"node": "slurm.slurmctld", "state": "queue", "observable_at_s": 60, "evidence": ["c"]},
        ],
        "scoring": [
            {"node": "db.mysql", "credit": 1.0, "rationale": "the cause"},
            {"node": "slurm.slurmdbd", "credit": 0.2, "rationale": "the report"},
            {"node": "slurm.slurmctld", "credit": 0.1, "rationale": "downstream"},
            {"node": "unknown", "credit": 0.05, "rationale": "abstain"},
        ],
        "distractors": {"slurm.config": "tempting"},
        "remediation_helpful": ["repair:db.mysql"],
    }


def build(raw: Raw) -> Scenario:
    return scenario_from_dict(raw)


def test_minimal_scenario_is_valid() -> None:
    # Without this, every case below could "pass" against a scenario that was
    # already invalid for some other reason.
    assert build(minimal()).validate() == []


def _set(path: str, value: Any) -> Callable[[Raw], None]:
    def edit(raw: Raw) -> None:
        target: Any = raw
        *parents, last = path.split(".")
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        if last.isdigit():
            target[int(last)] = value
        else:
            target[last] = value

    return edit


def _append(path: str, value: Any) -> Callable[[Raw], None]:
    def edit(raw: Raw) -> None:
        raw[path].append(value)

    return edit


def _undiagnosable(raw: Raw) -> None:
    raw["difficulty"] = "undiagnosable"
    raw["causal_chain"] = raw["causal_chain"][:1]
    raw["reported_node"] = "db.mysql"


VALIDATE_CASES: list[tuple[str, Callable[[Raw], None], str]] = [
    ("bad id", _set("id", "s99_bad"), "must look like"),
    ("empty symptom", _set("symptom", "  "), "symptom is empty"),
    ("empty injection", _set("injection", ""), "injection is empty"),
    ("bad canary", _set("canary", "not-a-uuid"), "is not a UUID"),
    ("blocked without reason", _set("status", "blocked"), "must say why"),
    ("runnable with reason", _set("blocked_reason", "x"), "has a blocked_reason"),
    ("empty chain", _set("causal_chain", []), "causal_chain is empty"),
    ("unordered chain", _set("causal_chain.1.observable_at_s", 90), "not ordered"),
    ("chain not at zero", _set("causal_chain.0.observable_at_s", 1), "must start at"),
    ("hop without evidence", _set("causal_chain.1.evidence", []), "cites no evidence"),
    ("blank evidence", _set("causal_chain.1.evidence", [" "]), "cites no evidence"),
    (
        "multi-layer too short",
        _set("causal_chain", minimal()["causal_chain"][:2]),
        "only 2 hops",
    ),
    ("credit rises down chain", _set("scoring.2.credit", 0.3), "credit rises"),
    ("two full credits", _set("scoring.1.credit", 1.0), "more than one full-credit"),
    (
        "duplicate scoring",
        _append("scoring", {"node": "db.mysql", "credit": 0.5, "rationale": "x"}),
        "duplicate scoring entry",
    ),
    ("missing rationale", _set("scoring.2.rationale", " "), "has no rationale"),
    ("no full credit", _set("scoring.0.credit", 0.9), "no full-credit answer"),
    (
        "root cause not first",
        _set("causal_chain.0.node", "storage.shared_fs"),
        "is not the first link",
    ),
    ("abstention too generous", _set("scoring.3.credit", 0.2), "must stay below"),
    ("reported node too generous", _set("scoring.1.credit", 0.3), "the cap is 0.2"),
    ("reported node unknown", _set("reported_node", "slurm.nope"), "not in NODES"),
    ("distractor not a node", _set("distractors", {"slurm.nope": "x"}), "not in NODES"),
    ("distractor unexplained", _set("distractors", {"slurm.config": " "}), "no explanation"),
    (
        "root cause as distractor",
        _set("distractors", {"db.mysql": "tempting"}),
        "listed as a distractor",
    ),
    (
        "free-text remediation",
        _set("remediation_helpful", ["restore write permissions"]),
        "remediation_helpful",
    ),
    (
        "duplicate remediation",
        _set("remediation_helpful", ["repair:db.mysql", "repair:db.mysql"]),
        "twice",
    ),
    (
        "measured without observations",
        _set("verification", {"measured": True, "method": "ran it"}),
        "records no observations",
    ),
    (
        "measured without method",
        _set("verification", {"measured": True, "observations": {"t+5s": "x", "t+60s": "y"}}),
        "has no method",
    ),
    (
        "measured timing not observed",
        _set("verification", {"measured": True, "method": "m", "observations": {"t+5s": "x"}}),
        "no observation is recorded at that offset",
    ),
    ("window too short", _set("observe_s", 30), "shorter than"),
]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [(edit, expected) for _, edit, expected in VALIDATE_CASES],
    ids=[name for name, _, _ in VALIDATE_CASES],
)
def test_each_rule_is_enforced(edit: Callable[[Raw], None], expected: str) -> None:
    raw = copy.deepcopy(minimal())
    edit(raw)
    problems = build(raw).validate()
    assert any(expected in p for p in problems), problems


class TestUndiagnosableRules:
    def test_minimal_undiagnosable_is_valid(self) -> None:
        raw = minimal()
        _undiagnosable(raw)
        raw["scoring"] = [
            {"node": "unknown", "credit": 1.0, "rationale": "correct"},
            {"node": "db.mysql", "credit": 0.15, "rationale": "localised"},
        ]
        raw["distractors"] = {}
        assert build(raw).validate() == []

    def test_named_full_credit_cause_is_rejected(self) -> None:
        raw = minimal()
        _undiagnosable(raw)
        problems = build(raw).validate()
        assert any("has a full-credit cause" in p for p in problems)
        assert any(f"award full credit to {'unknown'!r}" in p for p in problems)

    def test_diagnosable_full_credit_abstention_is_rejected(self) -> None:
        raw = minimal()
        raw["scoring"][3]["credit"] = 1.0
        problems = build(raw).validate()
        assert any("must stay below" in p for p in problems)
        assert any("more than one full-credit" in p for p in problems)


PARSE_CASES: list[tuple[str, Callable[[Raw], None], str]] = [
    ("misspelled key", _set("remediation_helpfull", ["repair:db.mysql"]), "unknown key"),
    ("missing key", lambda raw: raw.pop("canary"), "missing required key"),
    ("string for a list", _set("remediation_helpful", "repair db"), "list of strings"),
    ("string for evidence", _set("causal_chain.0.evidence", "x"), "list of strings"),
    ("string for measured", _set("verification", {"measured": "false"}), "true or false"),
    ("null chain", _set("causal_chain", None), "must be a list"),
    ("bool for observe_s", _set("observe_s", True), "must be an integer"),
    ("string credit", _set("scoring.0.credit", "1.0"), "must be a number"),
    ("unknown family", _set("family", "kubernetes"), "not one of"),
    ("unknown status", _set("status", "maybe"), "not one of"),
    ("unknown chain node", _set("causal_chain.0.node", "k8s.pod"), "unknown node"),
    ("credit out of range", _set("scoring.0.credit", 1.5), "credit must be in"),
    ("chain entry key typo", _set("causal_chain.0.evidance", ["x"]), "unknown key"),
    (
        "non-string observation",
        _set("verification", {"measured": False, "observations": {"t": 1}}),
        "string",
    ),
]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [(edit, expected) for _, edit, expected in PARSE_CASES],
    ids=[name for name, _, _ in PARSE_CASES],
)
def test_parser_rejects(edit: Callable[[Raw], None], expected: str) -> None:
    raw = copy.deepcopy(minimal())
    edit(raw)
    with pytest.raises(ValueError, match=expected):
        build(raw)


def test_parse_action() -> None:
    assert parse_action("drain:slurm.slurmd") == ("drain", "slurm.slurmd")
    for bad in ("drain", "drain:", ":slurm.slurmd", "drain:unknown", "fix:db.mysql"):
        with pytest.raises(ValueError):
            parse_action(bad)


def test_schema_doc_lists_every_key() -> None:
    """docs/SCENARIO_SCHEMA.md must document exactly the keys the parser accepts."""
    doc = (Path(__file__).resolve().parent.parent / "docs" / "SCENARIO_SCHEMA.md").read_text()
    for section, (required, optional) in SCHEMA_KEYS.items():
        for key in required | optional:
            assert f"`{key}`" in doc, f"{section}.{key} is not documented"


# ── loader: every error, not just the first ─────────────────────────────────


def _write(root: Path, raw: Raw) -> Path:
    directory = root / str(raw["id"])
    directory.mkdir(parents=True)
    path = directory / "scenario.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_loader_reports_every_broken_file(tmp_path: Path) -> None:
    good = minimal()
    bad_one = minimal() | {"id": "S98-bad-one", "canary": "00000000-0000-4000-8000-000000000001"}
    bad_one["observe_s"] = 1
    bad_two = minimal() | {"id": "S97-bad-two", "canary": "00000000-0000-4000-8000-000000000002"}
    bad_two["typo"] = 1
    for raw in (good, bad_one, bad_two):
        _write(tmp_path, raw)
    report = load_report(tmp_path)
    assert [s.id for s in report.scenarios] == [good["id"]]
    assert len(report.errors) == 2
    with pytest.raises(ScenarioError) as caught:
        load_all(tmp_path)
    assert "S98-bad-one" in str(caught.value) and "S97-bad-two" in str(caught.value)


def test_loader_rejects_id_directory_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path, minimal())
    renamed = path.parent.with_name("S99-other-name")
    path.parent.rename(renamed)
    with pytest.raises(ScenarioError, match="does not match directory"):
        load_scenario(renamed / "scenario.yaml")


def test_loader_rejects_duplicate_canaries(tmp_path: Path) -> None:
    _write(tmp_path, minimal())
    _write(tmp_path, minimal() | {"id": "S98-copy"})
    assert any("duplicate canaries" in e for e in load_report(tmp_path).errors)


@pytest.mark.parametrize("content", ["- just\n- a list\n", "id: [unclosed\n"])
def test_loader_rejects_non_mapping_and_bad_yaml(tmp_path: Path, content: str) -> None:
    directory = tmp_path / "S99-x"
    directory.mkdir()
    (directory / "scenario.yaml").write_text(content)
    with pytest.raises(ScenarioError):
        load_scenario(directory / "scenario.yaml")
