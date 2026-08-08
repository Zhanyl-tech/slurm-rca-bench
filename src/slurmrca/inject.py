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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from slurmrca.cluster import Cluster, ClusterError


@dataclass(frozen=True, slots=True)
class Injection:
    """A reversible fault."""

    scenario_id: str
    #: Plain description of the mechanism, mirrored into the scenario YAML.
    mechanism: str
    inject: Callable[[Cluster], None]
    heal: Callable[[Cluster], None]


# ── S01: storage stall propagating to a scheduling halt ────────────────────


def _inject_storage_stall(cluster: Cluster) -> None:
    """Suspend the database so accounting writes block rather than fail.

    A real shared-filesystem stall makes operations *slow*, not *failed*, and
    that distinction is what makes the scenario hard: nothing logs an error, so
    there is no exception to grep for. Pausing the container reproduces exactly
    that — every caller waits on a backend that is neither dead nor answering.
    """
    cluster.pause("mysql")


def _heal_storage_stall(cluster: Cluster) -> None:
    cluster.unpause("mysql")


# ── S02: accounting-DB lock contention ─────────────────────────────────────

_LOCK_SQL = """
START TRANSACTION;
SELECT * FROM information_schema.tables LIMIT 1 FOR UPDATE;
SELECT SLEEP(600);
"""


def _inject_db_lock(cluster: Cluster) -> None:
    """Hold a long transaction so accounting reads queue behind it.

    Backgrounded inside the container: the point is a transaction that stays
    open while the harness observes, not one that completes.
    """
    result = cluster.exec(
        "mysql",
        ["sh", "-c", f"(mariadb -uroot -e {_LOCK_SQL!r} >/tmp/lock.log 2>&1 &) ; sleep 1"],
        timeout=60.0,
    )
    if not result.ok:
        raise ClusterError(f"could not open blocking transaction: {result.stderr.strip()}")


def _heal_db_lock(cluster: Cluster) -> None:
    """Kill the blocking session. Best effort — the sleep expires regardless."""
    cluster.exec(
        "mysql",
        ["sh", "-c", "pkill -f 'SLEEP(600)' || true; pkill mariadb || true"],
        timeout=60.0,
    )


# ── S03: controller RPC saturation ─────────────────────────────────────────


def _inject_rpc_flood(cluster: Cluster) -> None:
    """Saturate slurmctld's RPC threads with a client loop.

    Emulates the most common real cause of controller saturation: a monitoring
    agent or user script polling squeue in a tight loop.
    """
    flood = (
        "for i in $(seq 1 8); do "
        "(while true; do squeue >/dev/null 2>&1; sinfo >/dev/null 2>&1; done &) ; "
        "done; sleep 1"
    )
    result = cluster.exec("slurmctld", ["sh", "-c", flood], timeout=60.0)
    if not result.ok:
        raise ClusterError(f"could not start RPC flood: {result.stderr.strip()}")


def _heal_rpc_flood(cluster: Cluster) -> None:
    cluster.exec(
        "slurmctld",
        ["sh", "-c", "pkill -f 'while true' || true; pkill squeue || true; pkill sinfo || true"],
        timeout=60.0,
    )


# ── S04: GPU ECC fault ─────────────────────────────────────────────────────

_FAKE_SMI = r"""#!/bin/sh
# Synthetic nvidia-smi for slurm-rca-bench S04.
# The cluster has no GPUs; this reports a failing card so the scenario has
# something for an agent to find. Documented as synthetic in the README.
cat <<'OUT'
GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-00000000-0000-0000-0000-000000000000)
    Ecc Errors
        Volatile
            SRAM Uncorrectable            : 4
            DRAM Uncorrectable            : 2
        Aggregate
            SRAM Uncorrectable            : 4
    Remapped Rows
        Remapping Failure Occurred        : Yes
        Pending                           : No
OUT
"""


def _inject_gpu_ecc(cluster: Cluster) -> None:
    """Install a synthetic nvidia-smi reporting volatile uncorrectable ECC."""
    script = (
        "mkdir -p /usr/local/bin && "
        f"cat > /usr/local/bin/nvidia-smi <<'EOF'\n{_FAKE_SMI}EOF\n"
        "chmod +x /usr/local/bin/nvidia-smi"
    )
    result = cluster.exec("cpu-worker", ["sh", "-c", script], timeout=60.0)
    if not result.ok:
        raise ClusterError(f"could not install synthetic nvidia-smi: {result.stderr.strip()}")


def _heal_gpu_ecc(cluster: Cluster) -> None:
    cluster.exec("cpu-worker", ["sh", "-c", "rm -f /usr/local/bin/nvidia-smi"], timeout=60.0)


# ── S05: node disappears, evidence lost ────────────────────────────────────


def _inject_node_loss(cluster: Cluster) -> None:
    """Kill a worker uncleanly and clear its logs.

    The cleared logs are the scenario: a reboot destroys local evidence, and
    what remains genuinely does not determine why the node went away.
    """
    cluster.exec("cpu-worker", ["sh", "-c", "rm -f /var/log/slurm/*.log || true"], timeout=60.0)
    cluster.kill("cpu-worker", signal="KILL")


def _heal_node_loss(cluster: Cluster) -> None:
    cluster.start("cpu-worker")
    cluster.scontrol("update", "NodeName=ALL", "State=RESUME")


INJECTIONS: dict[str, Injection] = {
    "S01-storage-stall-scheduling-halt": Injection(
        scenario_id="S01-storage-stall-scheduling-halt",
        mechanism="SIGSTOP the database container so writes block without erroring",
        inject=_inject_storage_stall,
        heal=_heal_storage_stall,
    ),
    "S02-dbd-lock-contention": Injection(
        scenario_id="S02-dbd-lock-contention",
        mechanism="hold an open transaction so accounting reads queue behind it",
        inject=_inject_db_lock,
        heal=_heal_db_lock,
    ),
    "S03-controller-rpc-saturation": Injection(
        scenario_id="S03-controller-rpc-saturation",
        mechanism="flood slurmctld with squeue/sinfo RPCs from parallel clients",
        inject=_inject_rpc_flood,
        heal=_heal_rpc_flood,
    ),
    "S04-gpu-ecc-drain": Injection(
        scenario_id="S04-gpu-ecc-drain",
        mechanism="synthetic nvidia-smi reporting volatile uncorrectable ECC",
        inject=_inject_gpu_ecc,
        heal=_heal_gpu_ecc,
    ),
    "S05-undiagnosable-node-reboot": Injection(
        scenario_id="S05-undiagnosable-node-reboot",
        mechanism="SIGKILL a worker after clearing its logs, then restart it",
        inject=_inject_node_loss,
        heal=_heal_node_loss,
    ),
}


def injection_for(scenario_id: str) -> Injection:
    """Look up the injection for a scenario."""
    try:
        return INJECTIONS[scenario_id]
    except KeyError:
        raise ClusterError(f"no injection implemented for {scenario_id!r}") from None
