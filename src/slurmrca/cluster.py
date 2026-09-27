"""Lifecycle control over an isolated Slurm cluster.

Isolation is the point. The harness runs its own Compose project with its own
name, network, and volumes, so it can suspend a database or kill a node daemon
without touching anything else on the machine. A benchmark that depends on
ambient state — or that damages it — is not reproducible and will not be run
twice by anyone.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Compose project name. Distinct from the upstream default (``slurm``) so a
#: developer's own slurm-docker-cluster is never targeted by an injection.
PROJECT = "slurmrca"

DEFAULT_TIMEOUT = 120.0


class ClusterError(RuntimeError):
    """Docker or Compose refused, or a container is not where we expect."""


#: Sentinel exit code for a command that never returned.
TIMED_OUT = -1


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of running a command inside a container.

    ``timed_out`` is a first-class outcome, not an error. The distinction
    between a command that *failed* and one that *never came back* is the whole
    substance of the flagship scenario: a broken database returns an error,
    while a frozen one returns nothing at all and the caller simply waits.
    Collapsing both into "it didn't work" would erase the signal S01 is built
    on.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def blocked(self) -> bool:
        """True when the command hung rather than returning a failure."""
        return self.timed_out


class ClusterOps(Protocol):
    """What an injection or heal may do to the cluster.

    Injections are written against this rather than :class:`Cluster` so they
    can be exercised against a recording fake in tests. There is no Docker in
    CI's unit job, and the audit that prompted this found five injections whose
    defects (a heal that killed the database server, a heal that could not find
    a stopped container) were visible from the commands alone.
    """

    def exec(
        self,
        service: str,
        command: list[str],
        *,
        timeout: float = ...,
        user: str | None = ...,
    ) -> ExecResult: ...

    def state(self, service: str) -> str: ...

    def pause(self, service: str) -> None: ...

    def unpause(self, service: str) -> None: ...

    def kill(self, service: str, signal: str = ...) -> None: ...

    def start(self, service: str) -> None: ...


class Cluster:
    """A Compose project running slurmctld, slurmdbd, MariaDB and workers."""

    def __init__(self, compose_file: Path, project: str = PROJECT) -> None:
        self.compose_file = compose_file
        self.project = project
        # Resolve once, to an absolute path. Invoking by bare name would let a
        # `docker` earlier on PATH intercept every command the harness runs —
        # and this harness pauses databases and kills daemons for a living.
        found = shutil.which("docker")
        if found is None:
            raise ClusterError("docker not found on PATH")
        self.docker = found

    # ── compose ────────────────────────────────────────────────────────────

    def _compose(self, *args: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        cmd = [
            self.docker,
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.compose_file),
            *args,
        ]
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )

    def up(self, timeout: float = 900.0) -> None:
        """Start the cluster and wait for Compose to report healthy."""
        result = self._compose("up", "-d", "--wait", timeout=timeout)
        if result.returncode != 0:
            raise ClusterError(f"compose up failed:\n{result.stderr.strip()}")

    def down(self, *, volumes: bool = True) -> None:
        """Stop and remove the project. Volumes go too, by default.

        Leaving volumes behind would let one scenario's accounting rows leak
        into the next run, which is exactly the kind of cross-contamination that
        makes benchmark numbers untrustworthy.
        """
        args = ["down", "--remove-orphans"]
        if volumes:
            args.append("--volumes")
        self._compose(*args, timeout=300.0)

    def services(self) -> list[str]:
        """Names of running services in this project."""
        result = self._compose("ps", "--services", "--status", "running", timeout=60.0)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # ── container operations ───────────────────────────────────────────────

    def container(self, service: str) -> str:
        """Resolve a service name to its container id in this project.

        ``--all`` matters: by default ``docker compose ps`` shows only running
        containers (https://docs.docker.com/reference/cli/docker/compose/ps/),
        so the S05 heal could never find the worker it had just killed and the
        cluster stayed down.
        """
        result = self._compose("ps", "--all", "-q", service, timeout=60.0)
        cid = result.stdout.strip().splitlines()
        if result.returncode != 0 or not cid:
            raise ClusterError(f"service {service!r} has no container in project {self.project!r}")
        return cid[0]

    def state(self, service: str) -> str:
        """Docker's state for the service's container.

        One of ``created``, ``running``, ``paused``, ``restarting``,
        ``removing``, ``exited`` or ``dead`` (the ContainerState.Status enum of
        the Docker Engine API). Heals check it first so they are idempotent:
        ``docker unpause`` on a container that is not paused is an error.
        """
        result = subprocess.run(  # noqa: S603
            [self.docker, "inspect", "--format", "{{.State.Status}}", self.container(service)],
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
        if result.returncode != 0:
            raise ClusterError(f"docker inspect {service} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def exec(
        self,
        service: str,
        command: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        user: str | None = None,
    ) -> ExecResult:
        """Run a command inside a service container."""
        args = ["exec", "-T"]
        if user:
            args += ["--user", user]
        try:
            result = self._compose(*args, service, *command, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Not a harness failure. A command that hangs is the observation.
            return ExecResult(TIMED_OUT, "", f"timed out after {timeout}s", timed_out=True)
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def pause(self, service: str) -> None:
        """Freeze every process in a container with ``docker pause``.

        On Linux this uses the cgroup freezer, not SIGSTOP: the processes are
        suspended without being signalled, so they cannot notice or react
        (https://docs.docker.com/reference/cli/docker/container/pause/). The
        service neither fails nor answers, so callers block on an unbounded
        wait rather than receiving an error. Errors are easy to diagnose;
        silence is what makes the flagship scenario hard.
        """
        self._run_docker("pause", self.container(service))

    def unpause(self, service: str) -> None:
        """Resume a paused container."""
        self._run_docker("unpause", self.container(service))

    def kill(self, service: str, signal: str = "KILL") -> None:
        """Send a signal to a container's main process."""
        self._run_docker("kill", "--signal", signal, self.container(service))

    def start(self, service: str) -> None:
        """Start a stopped container."""
        self._run_docker("start", self.container(service))

    def image_ids(self) -> str:
        """Image ids of this project's containers, for stamping transcripts."""
        result = self._compose("images", "--quiet", timeout=60.0)
        return " ".join(result.stdout.split())

    def logs(self, service: str, *, tail: int = 500) -> str:
        """Recent logs for a service."""
        result = self._compose("logs", "--no-color", "--tail", str(tail), service, timeout=120.0)
        return result.stdout

    def _run_docker(self, *args: str) -> None:
        result = subprocess.run(  # noqa: S603
            [self.docker, *args], capture_output=True, text=True, timeout=120.0, check=False
        )
        if result.returncode != 0:
            raise ClusterError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")

    # ── Slurm conveniences ─────────────────────────────────────────────────

    def scontrol(self, *args: str) -> ExecResult:
        """Run scontrol on the controller."""
        return self.exec("slurmctld", ["scontrol", *args])

    def sinfo(self, *args: str) -> ExecResult:
        """Run sinfo on the controller."""
        return self.exec("slurmctld", ["sinfo", *args])

    def sdiag(self) -> ExecResult:
        """Scheduler diagnostics — the primary controller-health signal."""
        return self.exec("slurmctld", ["sdiag"])

    def squeue(self, *args: str) -> ExecResult:
        """Run squeue on the controller."""
        return self.exec("slurmctld", ["squeue", *args])

    def healthy(self) -> bool:
        """True when the controller answers and at least one node is usable."""
        result = self.sinfo("--noheader", "-o", "%T")
        if not result.ok:
            return False
        states = {line.strip().rstrip("*") for line in result.stdout.splitlines() if line.strip()}
        return bool(states & {"idle", "mix", "alloc", "allocated", "mixed"})
