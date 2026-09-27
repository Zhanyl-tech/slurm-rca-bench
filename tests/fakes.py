"""Test doubles for the cluster, so injections can be tested without Docker.

:class:`FakeCluster` records every call and mirrors the Docker refusals that
matter for idempotency: ``docker unpause`` on a container that is not paused is
an error, and so are ``docker pause`` and ``docker kill`` on one that is not
running, and ``docker compose exec`` into one that is paused or stopped. A heal
that forgets to check state first, or runs before the heal that restores its
container, fails against it the way it would fail against the real thing.

:class:`LocalShellCluster` goes one step further for scripts that can run on a
laptop: it executes the ``sh -c <script>`` the injection would send into the
container, locally, with paths redirected by environment variables. That
exercises the real Python-to-shell path, not a string comparison.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from slurmrca.cluster import ClusterError, ExecResult

Handler = Callable[[str, list[str]], ExecResult]

SERVICES = ("mysql", "slurmdbd", "slurmctld", "cpu-worker")


def ok(stdout: str = "") -> ExecResult:
    return ExecResult(0, stdout, "")


@dataclass
class FakeCluster:
    """Records calls; scripted exec results; Docker-like state rules."""

    states: dict[str, str] = field(default_factory=lambda: dict.fromkeys(SERVICES, "running"))
    handler: Handler | None = None
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def exec(
        self,
        service: str,
        command: list[str],
        *,
        timeout: float = 120.0,
        user: str | None = None,
    ) -> ExecResult:
        self.calls.append(("exec", service, *command))
        if (refused := self.exec_refusal(service)) is not None:
            return refused
        if self.handler is not None:
            return self.handler(service, command)
        return ok()

    def exec_refusal(self, service: str) -> ExecResult | None:
        """What ``docker compose exec`` returns for a container that is not running.

        Compose v2 execs only into running containers, and Docker refuses to
        exec into a paused one. This used to ignore state, so no test could see
        a heal exec into the worker that S05 had killed.
        """
        if self.states[service] == "running":
            return None
        return ExecResult(1, "", f"service {service!r} is not running")

    def state(self, service: str) -> str:
        return self.states[service]

    def pause(self, service: str) -> None:
        self.calls.append(("pause", service))
        if self.states[service] != "running":
            raise ClusterError(f"cannot pause {service}: {self.states[service]}")
        self.states[service] = "paused"

    def unpause(self, service: str) -> None:
        self.calls.append(("unpause", service))
        if self.states[service] != "paused":
            raise ClusterError(f"Container {service} is not paused")
        self.states[service] = "running"

    def kill(self, service: str, signal: str = "KILL") -> None:
        self.calls.append(("kill", service, signal))
        if self.states[service] != "running":
            raise ClusterError(f"cannot kill {service}: not running")
        self.states[service] = "exited"

    def start(self, service: str) -> None:
        self.calls.append(("start", service))
        self.states[service] = "running"

    def scripts_run(self) -> list[tuple[str, str]]:
        """(service, $0 label) for every packaged script executed."""
        return [(c[1], c[5]) for c in self.calls if c[0] == "exec" and c[2:4] == ("sh", "-c")]


@dataclass
class LocalShellCluster(FakeCluster):
    """Runs ``sh -c`` commands locally with a controlled environment."""

    env: dict[str, str] = field(default_factory=dict)
    shell: str = "sh"

    def exec(
        self,
        service: str,
        command: list[str],
        *,
        timeout: float = 120.0,
        user: str | None = None,
    ) -> ExecResult:
        self.calls.append(("exec", service, *command))
        if (refused := self.exec_refusal(service)) is not None:
            return refused
        if command[:2] != ["sh", "-c"]:
            if self.handler is not None:
                return self.handler(service, command)
            return ok()
        environment = {**os.environ, **self.env}
        result = subprocess.run(
            [self.shell, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        return ExecResult(result.returncode, result.stdout, result.stderr)


def write_stub(directory: Path, name: str, body: str) -> Path:
    """Create an executable stub command, e.g. a fake `squeue` or `tc`."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path
