"""Live smoke test: inject S01, watch it propagate, heal.

Run with ``make smoke`` against a cluster from ``make cluster-up``. This is the
script that refuted S01's original causal chain, so it stays in the repo as the
reproduction rather than living in someone's shell history.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from slurmrca.cluster import Cluster, ClusterError, ExecResult
from slurmrca.inject import injection_for

COMPOSE = Path(__file__).resolve().parents[2] / "cluster" / "docker-compose.yml"
SCENARIO = "S01-storage-stall-scheduling-halt"


def probe(cluster: Cluster, label: str) -> None:
    """One observation: is accounting blocked, and is scheduling still alive?"""
    sacct = cluster.exec("slurmctld", ["sacct", "-n", "-X"], timeout=10.0)
    sinfo = cluster.sinfo("--noheader", "-o", "%T")
    queue = cluster.exec(
        "slurmctld", ["sh", "-c", "sdiag | grep 'DBD Agent queue size'"], timeout=15.0
    )

    def state(result: ExecResult) -> str:
        if result.blocked:
            return "BLOCKED"
        return "ok" if result.ok else "err"

    depth = queue.stdout.strip().split(":")[-1].strip() if queue.ok else "?"
    print(
        f"  {label:>10}  sacct={state(sacct):<8} sinfo={state(sinfo):<8} "
        f"nodes={sinfo.stdout.split() or ['-']}  dbd_queue={depth}",
        flush=True,
    )


def main() -> int:
    """Inject, observe, always heal."""
    try:
        cluster = Cluster(COMPOSE)
    except ClusterError as exc:
        print(f"cannot reach docker: {exc}", file=sys.stderr)
        return 2

    if not cluster.healthy():
        print("cluster is not healthy; run `make cluster-up` first", file=sys.stderr)
        return 2

    injection = injection_for(SCENARIO)
    print("\nbaseline")
    probe(cluster, "t-0")

    print(f"\ninjecting: {injection.mechanism}")
    injection.inject(cluster)
    start = time.time()
    try:
        for target in (5, 60, 180):
            while time.time() - start < target:
                time.sleep(2)
            probe(cluster, f"t+{target}s")
    finally:
        # Always. An interrupted smoke test that leaves the database suspended
        # poisons every later run, and the failure is silent.
        print("\nhealing")
        injection.heal(cluster)
        time.sleep(10)
        probe(cluster, "healed")

    ok = cluster.healthy()
    print(f"\ncluster healthy after heal: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
