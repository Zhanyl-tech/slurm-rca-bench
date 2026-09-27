"""Live smoke test for S01: inject, probe on a schedule, heal, keep a transcript.

Run with ``make smoke`` (quick schedule: t+5, 60, 180 s) or ``make smoke-full``
(the ~16-minute schedule) against a cluster from ``make cluster-up``.

What this is and is not. The measurement that refuted S01's original causal
chain was a roughly 16-minute session of sacct, sinfo, sdiag, sbatch and squeue
probes run by hand; its raw output was not committed, and the observations in
the scenario file are transcribed from it. The first version of this script
probed only sacct, sinfo and sdiag at three offsets and never submitted a job,
so it could not reproduce the decisive observation (jobs submitted during the
stall completed). It now probes all five commands, and every run writes a JSON
Lines transcript with the raw outputs, timestamps, the git commit, the suite
version and fingerprint, and the image ids, so the next measurement leaves an
artifact instead of a paraphrase.

The transcript goes to ``evidence/<scenario>/<UTC date>/``, a directory git
tracks and ``make clean`` does not touch, and it is meant to be committed. The
first version wrote to ``results/``, which is gitignored and which ``make
clean`` deletes, so the one raw record of a ~16-minute run was neither kept nor
committed by default: the same gap it was written to close.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from slurmrca import __version__
from slurmrca.cluster import Cluster, ClusterError, ClusterOps, ExecResult
from slurmrca.inject import injection_for
from slurmrca.loader import compose_file, fingerprint

SCENARIO = "S01-accounting-backend-stall"
QUICK = (5, 60, 180)
FULL = (5, 60, 180, 300, 600, 840, 900)
#: Default transcript root, relative to where ``make smoke`` runs (the repo).
EVIDENCE = Path("evidence")


def transcript_path(root: Path, scenario: str, when: datetime) -> Path:
    """``<root>/<scenario>/<YYYY-MM-DD>/smoke-<UTC timestamp>.jsonl``."""
    day = when.strftime("%Y-%m-%d")
    return root / scenario / day / f"smoke-{when.strftime('%Y%m%dT%H%M%SZ')}.jsonl"


def _state(result: ExecResult) -> str:
    if result.blocked:
        return "BLOCKED"
    return "ok" if result.ok else "err"


def probe(cluster: ClusterOps, label: str) -> dict[str, Any]:
    """One observation: is accounting blocked, and is scheduling still alive?"""
    commands: dict[str, tuple[list[str], float]] = {
        "sacct": (["sacct", "-n", "-X"], 10.0),
        "sinfo": (["sinfo", "--noheader", "-o", "%T"], 15.0),
        "sdiag": (["sh", "-c", "sdiag | grep 'DBD Agent queue size'"], 15.0),
        "squeue": (["squeue", "--noheader"], 15.0),
        "sbatch": (["sbatch", "--parsable", "--output=/dev/null", "--wrap=true"], 15.0),
    }
    record: dict[str, Any] = {"label": label, "time": datetime.now(UTC).isoformat()}
    for name, (command, timeout) in commands.items():
        result = cluster.exec("slurmctld", command, timeout=timeout)
        record[name] = {
            "state": _state(result),
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    return record


def summary(record: dict[str, Any]) -> str:
    """One human-readable line per probe."""
    sdiag = record["sdiag"]
    depth = sdiag["stdout"].strip().split(":")[-1].strip() if sdiag["state"] == "ok" else "?"
    sbatch = record["sbatch"]
    job = sbatch["stdout"].strip() if sbatch["state"] == "ok" else sbatch["state"]
    return (
        f"  {record['label']:>10}  sacct={record['sacct']['state']:<8} "
        f"sinfo={record['sinfo']['state']:<8} squeue={record['squeue']['state']:<8} "
        f"dbd_queue={depth}  sbatch={job}"
    )


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - read-only, informational
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _image_ids(cluster: Cluster) -> str:
    try:
        return cluster.image_ids() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def observe(
    cluster: ClusterOps,
    schedule: tuple[int, ...],
    out: TextIO,
    *,
    header: dict[str, Any],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> None:
    """Inject S01, probe at each offset in ``schedule``, always heal."""
    out.write(json.dumps({"header": header}) + "\n")
    injection = injection_for(SCENARIO)

    def record(label: str) -> None:
        entry = probe(cluster, label)
        out.write(json.dumps(entry) + "\n")
        out.flush()
        echo(summary(entry))

    echo("\nbaseline")
    record("t-0")
    echo(f"\ninjecting: {injection.mechanism}")
    # The inject is inside the try. It pauses the database and then checks the
    # state, and that check can raise after the pause has taken effect; with
    # the inject outside, smoke then exited with MariaDB still frozen. This is
    # the same rule run_injection follows: a half-applied injection is still
    # an injection.
    try:
        injection.inject(cluster)
        start = clock()
        for target in schedule:
            while (remaining := start + target - clock()) > 0:
                sleep(min(remaining, 2.0))
            record(f"t+{target}s")
    finally:
        # Always. An interrupted smoke test that leaves the database frozen
        # poisons every later run, and the failure is silent.
        echo("\nhealing")
        injection.heal(cluster)
        sleep(10)
        record("heal+10s")


def main(argv: list[str] | None = None) -> int:
    """Inject, observe, always heal, and keep the transcript."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="the ~16-minute schedule")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=EVIDENCE,
        help="where transcripts go (default: evidence/, tracked by git; commit the result)",
    )
    args = parser.parse_args(argv)

    try:
        cluster = Cluster(compose_file())
    except ClusterError as exc:
        print(f"cannot reach docker: {exc}", file=sys.stderr)
        return 2

    if not cluster.healthy():
        print("cluster is not healthy; run `make cluster-up` first", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = transcript_path(args.evidence_dir, SCENARIO, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "scenario": SCENARIO,
        "started": stamp,
        "git_commit": _git_commit(),
        "suite_version": __version__,
        "suite_fingerprint": fingerprint(),
        "images": _image_ids(cluster),
        "schedule_s": list(FULL if args.full else QUICK),
    }
    with path.open("w", encoding="utf-8") as out:
        observe(cluster, FULL if args.full else QUICK, out, header=header)

    ok = cluster.healthy()
    print(
        f"\ncluster healthy after heal: {ok}\ntranscript: {path}"
        "\nCommit it: a measurement counts only once its transcript is committed."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
