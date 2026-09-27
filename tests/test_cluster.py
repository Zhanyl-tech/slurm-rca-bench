"""The real :class:`Cluster`, driven against a stub ``docker`` executable.

No Docker daemon is needed: a shell script named ``docker`` on PATH records its
arguments and prints canned output. That is enough to check the commands the
harness builds, including the ``--all`` flag whose absence stranded the S05
heal, and the isolation guarantee (every compose call names the project).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from slurmrca.cluster import PROJECT, Cluster, ClusterError
from tests.fakes import write_stub


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "docker.log"
    write_stub(
        tmp_path,
        "docker",
        f'echo "$*" >> "{log}"\n'
        'case "$*" in\n'
        '  *" ps --all -q "*) echo cid123 ;;\n'
        '  *"inspect --format"*) echo paused ;;\n'
        '  *"exec -T slurmctld sinfo"*) printf "idle\\n" ;;\n'
        '  *"exec -T slurmctld false"*) exit 3 ;;\n'
        '  *" images --quiet"*) printf "img1\\nimg2\\n" ;;\n'
        '  "unpause cid123") exit 0 ;;\n'
        '  "kill --signal KILL cid123") echo "boom" >&2; exit 1 ;;\n'
        "esac",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return log


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines()


def test_docker_is_resolved_to_an_absolute_path(docker: Path) -> None:
    cluster = Cluster(Path("compose.yml"))
    assert Path(cluster.docker).is_absolute()


def test_missing_docker_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ClusterError, match="docker not found"):
        Cluster(Path("compose.yml"))


def test_container_lookup_includes_stopped_containers(docker: Path) -> None:
    cluster = Cluster(Path("compose.yml"))
    assert cluster.container("cpu-worker") == "cid123"
    assert f"compose --project-name {PROJECT} --file compose.yml ps --all -q cpu-worker" in calls(
        docker
    )


def test_state_and_unpause(docker: Path) -> None:
    cluster = Cluster(Path("compose.yml"))
    assert cluster.state("mysql") == "paused"
    cluster.unpause("mysql")
    assert "unpause cid123" in calls(docker)


def test_docker_failure_raises(docker: Path) -> None:
    with pytest.raises(ClusterError, match="boom"):
        Cluster(Path("compose.yml")).kill("cpu-worker")


def test_exec_and_healthy(docker: Path) -> None:
    cluster = Cluster(Path("compose.yml"))
    assert cluster.healthy()
    result = cluster.exec("slurmctld", ["false"])
    assert result.exit_code == 3 and not result.ok and not result.blocked
    assert all(f"--project-name {PROJECT}" in c for c in calls(docker) if "compose" in c)


def test_image_ids(docker: Path) -> None:
    assert Cluster(Path("compose.yml")).image_ids() == "img1 img2"
