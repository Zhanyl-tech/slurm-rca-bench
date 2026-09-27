"""The docs, the Makefile and CI say the same thing as the code.

Each check here exists because a statement drifted from what the repo does:
`make check` kept the label "Everything CI runs" after CI gained a wheel job,
the README gave bare `slurm-rca` commands that `make install` never puts on
PATH, and the scenario guide said S01 had shipped a claim it never shipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from slurmrca.loader import load_all
from slurmrca.spec import Status

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _subcommands(text: str) -> list[str]:
    """The slurm-rca subcommands a block of shell runs, in order."""
    return re.findall(r"slurm-rca (\w+)", text)


def test_wheel_check_runs_what_the_ci_wheel_job_runs() -> None:
    ci_steps = " ".join(str(step.get("run", "")) for step in CI["jobs"]["wheel"]["steps"])
    recipe = MAKEFILE.split("\nwheel-check:", 1)[1].split("\n\n", 1)[0]
    assert _subcommands(recipe) == _subcommands(ci_steps)
    assert _subcommands(ci_steps), "CI's wheel job no longer runs slurm-rca"


def test_make_check_does_not_claim_to_be_all_of_ci() -> None:
    line = next(entry for entry in MAKEFILE.splitlines() if entry.startswith("check:"))
    prerequisites = line.split(":", 1)[1].split("##", 1)[0].split()
    if set(CI["jobs"]) - {"check"} and "wheel-check" not in prerequisites:
        assert "Everything CI runs" not in line, line


def test_docs_with_bare_commands_say_how_to_get_them_on_path() -> None:
    # `make install` puts slurm-rca in .venv/bin only.
    for path in (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))):
        text = path.read_text(encoding="utf-8")
        blocks = "\n".join(re.findall(r"```(?:bash)?\n(.*?)```", text, re.S))
        if re.search(r"^slurm-rca ", blocks, re.M):
            assert "source .venv/bin/activate" in text, path.name


def test_the_scenario_guide_does_not_say_the_halt_shipped() -> None:
    # The refuted scheduling halt was rewritten before the first commit; only
    # the storage credit shipped (CHANGELOG 0.1.0, README).
    text = " ".join((ROOT / "docs" / "ADDING_A_SCENARIO.md").read_text().split())
    assert "shipped that way twice" not in text
    assert "nearly shipped a scheduling halt" in text


def test_blocked_reasons_name_what_is_missing_not_that_nothing_ran() -> None:
    # Runnable means "not known to be impossible". S10 was blocked for "never
    # run" while S02, never run either, was runnable.
    for scenario in load_all():
        if scenario.status is Status.BLOCKED:
            reason = " ".join(scenario.blocked_reason.lower().split())
            for phrase in ("never been run", "never run", "until a run shows it working"):
                assert phrase not in reason, (scenario.id, phrase)
            # Nor may a run be a condition for unblocking: with what the
            # cluster lacks in place, a scenario is no longer known to be
            # impossible, which is all runnable means. Every blocked_reason
            # listed "and a measurement" among its unblocking needs.
            unblock = re.search(r"unblocking needs (.*?)\.(?:\s|$)", reason)
            assert unblock, (scenario.id, "say what would unblock it")
            assert not re.search(r"\bmeasurement\b|\brun\b", unblock.group(1)), (
                scenario.id,
                unblock.group(1),
            )


def test_s10_blocked_reason_does_not_misquote_its_ticket() -> None:
    # The ticket (the symptom) used to say failures spread across "every node,
    # every partition, every user"; that sentence was removed, and the
    # blocked_reason kept citing it. The claim lives in the chain's evidence.
    s10 = next(s for s in load_all() if s.id.startswith("S10"))
    assert "ticket speaks of" not in s10.blocked_reason
    assert "every" not in s10.symptom.lower()
    assert "correlat" in " ".join(e for link in s10.causal_chain for e in link.evidence)
