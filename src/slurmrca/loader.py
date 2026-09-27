"""Discover and validate scenarios on disk.

Where the scenarios come from is resolved in a fixed order, never by walking
up the directory tree. The first version walked every parent directory looking
for a folder called ``scenarios/``; installed from a wheel (which shipped no
scenarios) it either crashed or, with an unrelated ``scenarios/`` folder in an
ancestor directory, silently loaded that instead and reported "1 scenarios".

Resolution order:

1. an explicit path (``--scenarios PATH`` on the CLI, or ``root=`` here);
2. the ``SLURMRCA_SCENARIOS`` environment variable;
3. the copy packaged inside the wheel (``slurmrca/_data/scenarios``);
4. the source checkout this module lives in (``<repo>/scenarios``), recognised
   by the ``pyproject.toml`` next to it — the editable-install case.

Anything else is an error that says which of these was tried.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from slurmrca.spec import Scenario, scenario_from_dict

#: Environment variable that points the loader at a scenario directory.
SCENARIOS_ENV = "SLURMRCA_SCENARIOS"

#: Old scenario ids that still resolve, so scripts written against them keep
#: working. S01 was renamed because its id named the scheduling halt that the
#: measurement disproved.
LEGACY_IDS: dict[str, str] = {
    "S01-storage-stall-scheduling-halt": "S01-accounting-backend-stall",
}


class ScenarioError(ValueError):
    """A scenario file is missing, malformed, or internally inconsistent."""


def _source_root() -> Path | None:
    """The repo root when running from a source checkout or editable install."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "slurmrca").is_dir():
        return candidate
    return None


def _packaged(name: str) -> Path | None:
    """A directory shipped inside the wheel under ``slurmrca/_data``."""
    path = Path(str(resources.files("slurmrca"))) / "_data" / name
    return path if path.is_dir() else None


def scenarios_dir(root: Path | str | None = None) -> Path:
    """Locate the scenario directory. See the module docstring for the order."""
    if root is not None:
        path = Path(root)
        if not path.is_dir():
            raise ScenarioError(f"scenario directory {path} does not exist")
        return path

    env = os.environ.get(SCENARIOS_ENV)
    if env:
        path = Path(env)
        if not path.is_dir():
            raise ScenarioError(f"{SCENARIOS_ENV}={env} is not a directory")
        return path

    packaged = _packaged("scenarios")
    if packaged is not None:
        return packaged

    source = _source_root()
    if source is not None and (source / "scenarios").is_dir():
        return source / "scenarios"

    raise ScenarioError(
        "could not find the scenarios: pass --scenarios PATH, set "
        f"{SCENARIOS_ENV}, or install a wheel that includes them"
    )


def cluster_dir() -> Path:
    """Locate the directory holding the compose file and ``upstream.lock``."""
    packaged = _packaged("cluster")
    if packaged is not None:
        return packaged
    source = _source_root()
    if source is not None and (source / "cluster").is_dir():
        return source / "cluster"
    raise ScenarioError("could not find cluster/ (compose file and upstream.lock)")


def compose_file() -> Path:
    """The benchmark's own compose file."""
    return cluster_dir() / "docker-compose.yml"


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario file.

    Validation is not optional here. A scenario whose ground truth contradicts
    itself yields scores that look precise and mean nothing, so an invalid
    scenario is an error at load time rather than a warning someone ignores.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path}: expected a mapping at the top level")

    try:
        scenario = scenario_from_dict(raw)
    except (ValueError, TypeError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc

    problems = scenario.validate()
    if scenario.id != path.parent.name:
        problems.append(
            f"id {scenario.id!r} does not match directory {path.parent.name!r}; "
            "the id is used to name result files"
        )
    if problems:
        joined = "\n  - ".join(problems)
        raise ScenarioError(f"{path}: ground truth is inconsistent:\n  - {joined}")
    return scenario


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Every scenario that loaded, plus every error, so none hides another."""

    scenarios: list[Scenario]
    errors: list[str]


def load_report(root: Path | str | None = None) -> LoadResult:
    """Load every scenario, collecting errors instead of stopping at the first.

    ``validate`` used to report only the first broken file because loading
    raised on it; with two broken scenarios the second stayed invisible until
    the first was fixed.
    """
    directory = scenarios_dir(root)
    found = sorted(directory.glob("*/scenario.yaml"))
    if not found:
        raise ScenarioError(f"no scenarios found under {directory}")

    scenarios: list[Scenario] = []
    errors: list[str] = []
    for path in found:
        try:
            scenarios.append(load_scenario(path))
        except ScenarioError as exc:
            errors.append(str(exc))

    for label, values in (
        ("scenario ids", [s.id for s in scenarios]),
        ("canaries", [s.canary for s in scenarios]),
        ("task ids", [s.task_id for s in scenarios]),
    ):
        duplicates = sorted({v for v in values if values.count(v) > 1})
        if duplicates:
            errors.append(f"duplicate {label}: {duplicates}")
    return LoadResult(scenarios=sorted(scenarios, key=lambda s: s.id), errors=errors)


def load_all(root: Path | str | None = None) -> list[Scenario]:
    """Load every scenario (runnable and blocked), sorted by id.

    Raises one ScenarioError listing every problem found.
    """
    result = load_report(root)
    if result.errors:
        raise ScenarioError("\n".join(result.errors))
    return result.scenarios


def runnable(scenarios: list[Scenario]) -> list[Scenario]:
    """The scenarios that count toward scores and baselines."""
    return [s for s in scenarios if s.runnable]


def resolve_id(identifier: str, scenarios: list[Scenario]) -> Scenario:
    """Map a scenario id, legacy id or opaque task id to its scenario."""
    canonical = LEGACY_IDS.get(identifier, identifier)
    for scenario in scenarios:
        if canonical in (scenario.id, scenario.task_id):
            return scenario
    raise KeyError(identifier)


#: Files in ``cluster/`` that define the emulated cluster, for :func:`fingerprint`.
_CLUSTER_SUFFIXES = (".yml", ".yaml", ".lock", ".sh")


def _package_dir() -> Path:
    """The installed ``slurmrca`` package directory."""
    return Path(str(resources.files("slurmrca")))


def fingerprint(root: Path | str | None = None, *, code: Path | None = None) -> str:
    """Hash of everything a published score depends on in this repo.

    Three parts, each named in the digest so a file cannot move between them
    unnoticed:

    - the scenario files (the ground truth);
    - every file in ``cluster/``: the compose file, the upstream pin and the
      script that builds the image, including its build arguments;
    - the injection code, ``inject.py`` and every script in ``scripts/``
      (``code`` is the package directory holding them; tests point it at a
      copy). This defines the fault an agent actually observes: the S03 client
      count, the S02 hold, the S10 job count and failure hash. The first
      version hashed only the YAML, the compose file and the pin, so two runs
      against different faults carried the same hash.

    A score reported without it cannot be matched to what it was computed
    against once any of those change. It does not cover the built image or the
    MariaDB image, which are not pinned by digest yet.
    """
    digest = hashlib.sha256()

    def add(label: str, data: bytes) -> None:
        # Length-prefixed, so the boundary between a name and its bytes is
        # never ambiguous.
        for part in (label.encode(), data):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)

    directory = scenarios_dir(root)
    for path in sorted(directory.glob("*/scenario.yaml")):
        add(f"scenarios/{path.parent.name}", path.read_bytes())
    try:
        cluster: Path | None = cluster_dir()
    except ScenarioError:
        cluster = None
    if cluster is not None:
        # By suffix, not every file: a .DS_Store or an editor backup in a
        # checkout must not make one machine's hash differ from another's.
        for path in sorted(cluster.iterdir()):
            if path.is_file() and path.suffix in _CLUSTER_SUFFIXES:
                add(f"cluster/{path.name}", path.read_bytes())
    package = _package_dir() if code is None else code
    add("inject.py", (package / "inject.py").read_bytes())
    for path in sorted((package / "scripts").glob("*.sh")):
        add(f"scripts/{path.name}", path.read_bytes())
    return digest.hexdigest()[:16]
