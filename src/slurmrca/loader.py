"""Discover and validate scenarios on disk."""

from __future__ import annotations

from pathlib import Path

import yaml

from slurmrca.spec import Scenario, scenario_from_dict


class ScenarioError(ValueError):
    """A scenario file is missing, malformed, or internally inconsistent."""


def scenarios_dir() -> Path:
    """Locate the repo's ``scenarios/`` directory.

    Walks up from this file so the package works from a source checkout, an
    editable install, and a container mount without configuration.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scenarios"
        if candidate.is_dir():
            return candidate
    raise ScenarioError(f"could not locate scenarios/ above {here}")


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario file.

    Validation is not optional here. A scenario whose ground truth contradicts
    itself yields scores that look precise and mean nothing, so an invalid
    scenario is an error at load time rather than a warning someone ignores.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: expected a mapping at the top level")

    try:
        scenario = scenario_from_dict(raw)
    except ValueError as exc:
        raise ScenarioError(f"{path}: {exc}") from exc

    problems = scenario.validate()
    if problems:
        joined = "\n  - ".join(problems)
        raise ScenarioError(f"{path}: ground truth is inconsistent:\n  - {joined}")

    if scenario.id != path.parent.name:
        raise ScenarioError(
            f"{path}: id {scenario.id!r} does not match directory "
            f"{path.parent.name!r}; the id is used to name result files"
        )
    return scenario


def load_all(root: Path | None = None) -> list[Scenario]:
    """Load every scenario, sorted by id."""
    root = root or scenarios_dir()
    found = sorted(root.glob("*/scenario.yaml"))
    if not found:
        raise ScenarioError(f"no scenarios found under {root}")
    scenarios = [load_scenario(path) for path in found]

    ids = [s.id for s in scenarios]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ScenarioError(f"duplicate scenario ids: {sorted(duplicates)}")
    return scenarios
