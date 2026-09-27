"""Fault injection and healing, one implementation per scenario.

Every injection here is an **emulation**. None of them break real hardware, and
the README says so before it says anything else. What each one reproduces is the
*shape* of a failure — whether callers block or receive errors, whether the
signal is confined to one node or spread across the fabric, how long the
consequence takes to surface — because shape is what an agent has to reason
about. A benchmark that claimed physical fidelity here would be lying.

Each injection is paired with a heal, and the harness always heals, including on
failure. A scenario that leaves the cluster broken poisons every scenario after
it, and the resulting scores would drift in a direction nobody notices.

Rules every heal follows, each learned from a defect an audit found by reading
the first version (none of this has been re-run on a live cluster yet):

- **Idempotent.** A heal on a clean cluster changes nothing and succeeds, so
  :func:`heal_all` can run every heal after an interrupted run without knowing
  which scenario was active.
- **Restore what was recorded, not a guess.** The inject records the prior
  state (a directory mode, a qdisc) and the heal restores that record.
- **Never match by pattern what the healing process itself contains.**
  Processes are tracked by PID file and checked by name before being killed;
  database sessions exclude ``CONNECTION_ID()``.
- **Fail safe on destructive steps.** Anything that deletes, cancels or kills is
  scoped to objects the benchmark created (a marker in a file, a job name, an
  ``rca*`` account), and refuses otherwise.

The shell that runs inside the containers lives in ``slurmrca/scripts/`` as real
files, so it can be linted with shellcheck and several scripts can be executed
by the unit tests against temporary paths.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

from slurmrca.cluster import ClusterError, ClusterOps
from slurmrca.loader import LEGACY_IDS

#: The compose service names the injections target.
DATABASE = "mysql"
DBD = "slurmdbd"
CONTROLLER = "slurmctld"
WORKER = "cpu-worker"

_NODE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class Injection:
    """A reversible fault."""

    scenario_id: str
    #: Plain description of the mechanism, mirrored into the scenario YAML.
    mechanism: str
    inject: Callable[[ClusterOps], None]
    heal: Callable[[ClusterOps], None]
    #: Optional step run part-way through the observation window (S05 brings
    #: the killed node back after five minutes), and when to run it.
    recover: Callable[[ClusterOps], None] | None = None
    recover_after_s: int | None = None


def script(name: str) -> str:
    """Text of a packaged container-side script."""
    return resources.files("slurmrca").joinpath("scripts", name).read_text(encoding="utf-8")


def _run_script(
    cluster: ClusterOps, service: str, name: str, *args: str, timeout: float = 120.0
) -> str:
    """Run a packaged script with ``sh -c`` inside a service; raise on failure.

    ``$0`` is set to ``slurmrca-<script>`` so the process is identifiable in
    ``ps`` while it runs.
    """
    label = "slurmrca-" + name.removesuffix(".sh")
    result = cluster.exec(service, ["sh", "-c", script(name), label, *args], timeout=timeout)
    if not result.ok:
        detail = (result.stderr.strip() or result.stdout.strip()) or "no output"
        why = "timed out" if result.timed_out else f"exit {result.exit_code}"
        raise ClusterError(f"{name} on {service} failed ({why}): {detail}")
    return result.stdout


# ── S01: frozen accounting database ────────────────────────────────────────


def _inject_db_freeze(cluster: ClusterOps) -> None:
    """Freeze the database so accounting calls wait instead of failing.

    ``docker pause`` uses the cgroup freezer: every process in the container is
    suspended without being signalled. Nothing logs an error, so there is no
    exception to grep for; every caller simply waits on a backend that is
    neither dead nor answering.
    """
    state = cluster.state(DATABASE)
    if state == "paused":
        return
    if state != "running":
        raise ClusterError(f"{DATABASE} is {state!r}, not running; cannot inject S01")
    cluster.pause(DATABASE)
    after = cluster.state(DATABASE)
    if after != "paused":
        raise ClusterError(f"docker pause returned but {DATABASE} is {after!r}")


def _heal_db_freeze(cluster: ClusterOps) -> None:
    if cluster.state(DATABASE) == "paused":
        cluster.unpause(DATABASE)


# ── S02: accounting-DB lock contention ─────────────────────────────────────


def _inject_db_lock(cluster: ClusterOps) -> None:
    """Hold a WRITE lock on slurmdbd's job table; see s02_lock_inject.sh."""
    _run_script(cluster, DBD, "s02_lock_inject.sh", timeout=60.0)


def _heal_db_lock(cluster: ClusterOps) -> None:
    _run_script(cluster, DBD, "s02_lock_heal.sh", timeout=60.0)


# ── S03: controller RPC saturation ─────────────────────────────────────────


def _inject_rpc_flood(cluster: ClusterOps) -> None:
    """Saturate slurmctld's RPC threads with client loops.

    Emulates the most common real cause of controller saturation: a monitoring
    agent or user script polling squeue in a tight loop.
    """
    _run_script(cluster, CONTROLLER, "s03_flood_inject.sh", timeout=60.0)


def _heal_rpc_flood(cluster: ClusterOps) -> None:
    _run_script(cluster, CONTROLLER, "s03_flood_heal.sh", timeout=60.0)


# ── S04 / S09: synthetic nvidia-smi ────────────────────────────────────────


def _inject_gpu_ecc(cluster: ClusterOps) -> None:
    """Install a synthetic nvidia-smi reporting volatile uncorrectable ECC."""
    _run_script(cluster, WORKER, "gpu_smi_install.sh", script("fake_nvidia_smi_ecc.sh"))


def _inject_gpu_driver(cluster: ClusterOps) -> None:
    """Install a synthetic nvidia-smi that fails for every device."""
    _run_script(cluster, WORKER, "gpu_smi_install.sh", script("fake_nvidia_smi_wedged.sh"))


def _heal_gpu(cluster: ClusterOps) -> None:
    """Remove the synthetic nvidia-smi, only if the benchmark installed it."""
    _run_script(cluster, WORKER, "gpu_smi_remove.sh")


# ── S05: node disappears, evidence lost ────────────────────────────────────


def _inject_node_loss(cluster: ClusterOps) -> None:
    """Delete the worker's slurmd log, then kill the worker uncleanly.

    The cleared log is the scenario: a reboot destroys local evidence, and what
    remains does not determine why the node went away. Only slurmd.log is
    deleted; the controller's log, which records the node going away, survives.
    """
    state = cluster.state(WORKER)
    if state in ("exited", "dead"):
        return
    if state != "running":
        raise ClusterError(f"{WORKER} is {state!r}, not running; cannot inject S05")
    _run_script(cluster, WORKER, "s05_clear_slurmd_log.sh")
    cluster.kill(WORKER, signal="KILL")


def _recover_node(cluster: ClusterOps) -> None:
    """Bring the killed worker back, as the ticket's node came back at 02:19."""
    if cluster.state(WORKER) != "running":
        cluster.start(WORKER)


def _resume_down_nodes(cluster: ClusterOps) -> None:
    """Resume nodes Slurm reports DOWN, and only those.

    The first heal resumed ``NodeName=ALL``, which would also undrain a node an
    operator had drained on purpose. The pinned slurm.conf sets
    ``ReturnToService=1``, so a node that went DOWN for not responding should
    return on its own when slurmd re-registers; this is the fallback.
    """
    result = cluster.exec(
        CONTROLLER,
        ["sinfo", "--noheader", "--Node", "--states=down", "--format=%N"],
        timeout=60.0,
    )
    if not result.ok:
        raise ClusterError(f"sinfo failed while healing S05: {result.stderr.strip()}")
    names = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    for name in names:
        if not _NODE_NAME.fullmatch(name):
            raise ClusterError(f"refusing to resume unexpected node name {name!r}")
        resumed = cluster.exec(
            CONTROLLER, ["scontrol", "update", f"NodeName={name}", "State=RESUME"], timeout=60.0
        )
        if not resumed.ok:
            raise ClusterError(f"could not resume {name}: {resumed.stderr.strip()}")


def _heal_node_loss(cluster: ClusterOps) -> None:
    _recover_node(cluster)
    _resume_down_nodes(cluster)


# ── S06: StateSaveLocation unwritable ──────────────────────────────────────


def _inject_state_save(cluster: ClusterOps) -> None:
    """Remove write permission from StateSaveLocation.

    A real permission failure rather than an emulation of one. Measured
    behaviour: slurmctld rejects every submission immediately with an I/O
    error, and running jobs are unaffected. Note this is a write *failure*, not
    a write *stall* — see the scenario's caveats.
    """
    _run_script(cluster, CONTROLLER, "s06_state_save_inject.sh", timeout=30.0)


def _heal_state_save(cluster: ClusterOps) -> None:
    _run_script(cluster, CONTROLLER, "s06_state_save_heal.sh", timeout=30.0)


# ── S07: degraded network (blocked) ────────────────────────────────────────


def _inject_fabric(cluster: ClusterOps) -> None:
    """tc netem on the worker's only interface. S07 is blocked; see its YAML."""
    _run_script(cluster, WORKER, "s07_netem_inject.sh", timeout=60.0)


def _heal_fabric(cluster: ClusterOps) -> None:
    _run_script(cluster, WORKER, "s07_netem_heal.sh", timeout=60.0)


# ── S08: association limit starvation (blocked) ────────────────────────────


def _inject_limit(cluster: ClusterOps) -> None:
    """Limit a benchmark-owned account to GrpJobs=0. S08 is blocked."""
    _run_script(cluster, CONTROLLER, "s08_limit_inject.sh", timeout=60.0)


def _heal_limit(cluster: ClusterOps) -> None:
    _run_script(cluster, CONTROLLER, "s08_limit_heal.sh", timeout=60.0)


# ── S10: uncorrelated intermittent failures (blocked) ──────────────────────


def _inject_flaky(cluster: ClusterOps) -> None:
    """Submit a workload whose jobs fail by a hash of their job id."""
    _run_script(cluster, CONTROLLER, "s10_workload_inject.sh", script("s10_job.sh"), timeout=600.0)


def _heal_flaky(cluster: ClusterOps) -> None:
    _run_script(cluster, CONTROLLER, "s10_workload_heal.sh", timeout=120.0)


INJECTIONS: dict[str, Injection] = {
    "S01-accounting-backend-stall": Injection(
        scenario_id="S01-accounting-backend-stall",
        mechanism="docker pause (cgroup freezer) on the database container",
        inject=_inject_db_freeze,
        heal=_heal_db_freeze,
    ),
    "S02-dbd-lock-contention": Injection(
        scenario_id="S02-dbd-lock-contention",
        mechanism="a session holds LOCK TABLES ... WRITE on slurmdbd's job table",
        inject=_inject_db_lock,
        heal=_heal_db_lock,
    ),
    "S03-controller-rpc-saturation": Injection(
        scenario_id="S03-controller-rpc-saturation",
        mechanism="flood slurmctld with squeue/sinfo RPCs from parallel client loops",
        inject=_inject_rpc_flood,
        heal=_heal_rpc_flood,
    ),
    "S04-gpu-ecc-drain": Injection(
        scenario_id="S04-gpu-ecc-drain",
        mechanism="synthetic nvidia-smi reporting volatile uncorrectable ECC",
        inject=_inject_gpu_ecc,
        heal=_heal_gpu,
    ),
    "S05-undiagnosable-node-reboot": Injection(
        scenario_id="S05-undiagnosable-node-reboot",
        mechanism="delete the worker's slurmd.log, SIGKILL the worker, restart it after 300s",
        inject=_inject_node_loss,
        heal=_heal_node_loss,
        recover=_recover_node,
        recover_after_s=300,
    ),
    "S06-state-save-unwritable": Injection(
        scenario_id="S06-state-save-unwritable",
        mechanism="chmod 500 on StateSaveLocation so the controller cannot write job state",
        inject=_inject_state_save,
        heal=_heal_state_save,
    ),
    "S07-fabric-degraded-collectives": Injection(
        scenario_id="S07-fabric-degraded-collectives",
        mechanism="tc netem delay and loss on the worker's only (control-plane) interface",
        inject=_inject_fabric,
        heal=_heal_fabric,
    ),
    "S08-partition-limit-starvation": Injection(
        scenario_id="S08-partition-limit-starvation",
        mechanism="sacctmgr limits a benchmark-owned account to GrpJobs=0",
        inject=_inject_limit,
        heal=_heal_limit,
    ),
    "S09-gpu-driver-node-drain": Injection(
        scenario_id="S09-gpu-driver-node-drain",
        mechanism="synthetic nvidia-smi failing wholesale for every device (driver signature)",
        inject=_inject_gpu_driver,
        heal=_heal_gpu,
    ),
    "S10-undiagnosable-intermittent-failures": Injection(
        scenario_id="S10-undiagnosable-intermittent-failures",
        mechanism="a submitted workload whose jobs fail by a hash of their job id (~2%)",
        inject=_inject_flaky,
        heal=_heal_flaky,
    ),
}


def injection_for(scenario_id: str) -> Injection:
    """Look up the injection for a scenario (legacy ids resolve too)."""
    try:
        return INJECTIONS[LEGACY_IDS.get(scenario_id, scenario_id)]
    except KeyError:
        raise ClusterError(f"no injection implemented for {scenario_id!r}") from None


#: Heals that bring a container back: S01 unpauses the database and S05 starts
#: the worker. Every other heal runs a script inside a container, and
#: ``docker compose exec`` fails on a paused or stopped one, so these go first.
#: In plain scenario order S04's heal exec'd into a worker that only S05's heal,
#: later, would start, and ``heal --all`` failed after an interrupted S05 run.
CONTAINER_HEALS = ("S01-accounting-backend-stall", "S05-undiagnosable-node-reboot")


def run_injection(
    cluster: ClusterOps,
    injection: Injection,
    observe_s: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> None:
    """Heal, inject, observe for ``observe_s`` (running any recovery step), heal.

    This is the lifecycle only. It does not collect a telemetry bundle; that is
    Phase 3 work. The heal in ``finally`` runs even when the inject itself
    fails part-way, because a half-applied injection is still an injection.

    The first heal also runs the :data:`CONTAINER_HEALS`. A scenario's own heal
    may exec into a container another interrupted run left paused or stopped;
    ``run S04`` after an S05 run killed mid-window used to fail at its first
    step because S04's heal could not reach the worker S05 had killed.
    """
    log(f"{injection.scenario_id}: restoring containers and healing first so the run starts clean")
    for scenario_id in CONTAINER_HEALS:
        if INJECTIONS[scenario_id] is not injection:
            INJECTIONS[scenario_id].heal(cluster)
    injection.heal(cluster)
    start = clock()

    def wait_until(offset: float) -> None:
        while (remaining := start + offset - clock()) > 0:
            sleep(min(remaining, 5.0))

    try:
        log(f"{injection.scenario_id}: injecting — {injection.mechanism}")
        injection.inject(cluster)
        start = clock()
        if (
            injection.recover is not None
            and injection.recover_after_s is not None
            and injection.recover_after_s < observe_s
        ):
            wait_until(injection.recover_after_s)
            log(f"{injection.scenario_id}: recovery step at t+{injection.recover_after_s}s")
            injection.recover(cluster)
        wait_until(observe_s)
    finally:
        log(f"{injection.scenario_id}: healing")
        injection.heal(cluster)


def heal_order() -> list[str]:
    """Scenario ids in the order :func:`heal_all` heals them."""
    return [*CONTAINER_HEALS, *sorted(set(INJECTIONS) - set(CONTAINER_HEALS))]


def heal_all(cluster: ClusterOps, *, log: Callable[[str], None] = print) -> list[tuple[str, str]]:
    """Run every heal and return the ones that failed.

    Every heal is idempotent, so this is safe on a clean cluster. A failure in
    one heal does not stop the others; the caller reports all of them. The
    heals that restore a container run first (see :data:`CONTAINER_HEALS`), so
    S02's heal can reach the database S01 may have frozen and the worker-side
    heals (S04, S07, S09) can reach the worker S05 may have killed.
    """
    failures: list[tuple[str, str]] = []
    for scenario_id in heal_order():
        try:
            INJECTIONS[scenario_id].heal(cluster)
            log(f"healed {scenario_id}")
        except (ClusterError, OSError, subprocess.SubprocessError) as exc:
            failures.append((scenario_id, str(exc)))
            log(f"FAILED to heal {scenario_id}: {exc}")
    return failures
