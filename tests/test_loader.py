"""Where scenarios come from: explicit, environment, packaged, source — never a walk.

The first loader walked up every parent directory looking for a folder called
``scenarios/``. Installed from a wheel with an unrelated ``scenarios/`` in an
ancestor directory, it silently loaded that and reported "1 scenarios".
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import slurmrca.loader as loader
from slurmrca.loader import (
    SCENARIOS_ENV,
    ScenarioError,
    compose_file,
    fingerprint,
    load_all,
    resolve_id,
    scenarios_dir,
)

REPO = Path(__file__).resolve().parent.parent


def test_source_checkout_is_found() -> None:
    assert scenarios_dir() == REPO / "scenarios"
    assert compose_file() == REPO / "cluster" / "docker-compose.yml"


def test_explicit_path_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIOS_ENV, str(REPO / "scenarios"))
    assert scenarios_dir(tmp_path) == tmp_path


def test_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIOS_ENV, str(tmp_path))
    assert scenarios_dir() == tmp_path


def test_environment_pointing_nowhere_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SCENARIOS_ENV, str(tmp_path / "missing"))
    with pytest.raises(ScenarioError, match=SCENARIOS_ENV):
        scenarios_dir()


def test_explicit_path_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="does not exist"):
        scenarios_dir(tmp_path / "missing")


def test_no_parent_directory_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no packaged data and no source checkout, a stray scenarios/ is ignored."""
    (tmp_path / "scenarios" / "S01-stray").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(SCENARIOS_ENV, raising=False)
    monkeypatch.setattr(loader, "_packaged", lambda name: None)
    monkeypatch.setattr(loader, "_source_root", lambda: None)
    with pytest.raises(ScenarioError, match="could not find the scenarios"):
        scenarios_dir()
    with pytest.raises(ScenarioError, match="cluster"):
        loader.cluster_dir()


def test_packaged_data_is_preferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCENARIOS_ENV, raising=False)
    monkeypatch.setattr(loader, "_packaged", lambda name: tmp_path)
    assert scenarios_dir() == tmp_path


def test_empty_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="no scenarios found"):
        load_all(tmp_path)


def test_resolve_id_forms() -> None:
    scenarios = load_all()
    s01 = scenarios[0]
    assert resolve_id(s01.id, scenarios) is s01
    assert resolve_id(s01.task_id, scenarios) is s01
    assert resolve_id("S01-storage-stall-scheduling-halt", scenarios) is s01
    with pytest.raises(KeyError):
        resolve_id("S99-nope", scenarios)


def test_fingerprint_changes_with_the_ground_truth(tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "scenarios"
    shutil.copytree(REPO / "scenarios", copy)
    before = fingerprint(copy)
    assert before == fingerprint(copy), "must be deterministic"
    target = copy / "S02-dbd-lock-contention" / "scenario.yaml"
    target.write_text(target.read_text().replace("credit: 0.2", "credit: 0.15", 1))
    assert fingerprint(copy) != before


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("scripts/s03_flood_inject.sh", "SLURMRCA_S03_CLIENTS:-8", "SLURMRCA_S03_CLIENTS:-64"),
        ("scripts/s02_lock_inject.sh", "SLURMRCA_S02_HOLD_S:-600", "SLURMRCA_S02_HOLD_S:-60"),
        ("inject.py", "recover_after_s=300", "recover_after_s=30"),
    ],
)
def test_fingerprint_changes_with_the_injection_code(
    tmp_path: Path, relative: str, old: str, new: str
) -> None:
    """The fault an agent observes is defined by the code; the first hash ignored it."""
    code = tmp_path / "slurmrca"
    shutil.copytree(REPO / "src" / "slurmrca", code, ignore=shutil.ignore_patterns("__pycache__"))
    before = fingerprint(code=code)
    assert before == fingerprint(), "a copy of the package must hash like the package"
    target = code / relative
    assert old in target.read_text()
    target.write_text(target.read_text().replace(old, new))
    assert fingerprint(code=code) != before


def test_fingerprint_covers_every_cluster_file_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = tmp_path / "cluster"
    shutil.copytree(REPO / "cluster", cluster)
    monkeypatch.setattr(loader, "cluster_dir", lambda: cluster)
    before = fingerprint()
    # Editor and Finder litter must not change the hash between machines.
    (cluster / ".DS_Store").write_bytes(b"\0")
    (cluster / "docker-compose.yml~").write_text("backup")
    assert fingerprint() == before
    # The image's build arguments are part of the cluster.
    build = cluster / "build-image.sh"
    build.write_text(build.read_text().replace("LMOD_VERSION=9.1.2", "LMOD_VERSION=9.1.3"))
    assert fingerprint() != before
