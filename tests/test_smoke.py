"""The smoke run's probe schedule and transcript, against a fake cluster.

It does not show that the cluster behaves as S01 says; only a live run does.
It shows that a live run would probe all five commands the original measurement
used, heal even when interrupted or when the inject itself fails part-way, and
leave a transcript where git keeps it.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import slurmrca.smoke as smoke
from slurmrca.cluster import ExecResult
from slurmrca.smoke import EVIDENCE, FULL, QUICK, SCENARIO, observe, summary, transcript_path
from tests.fakes import FakeCluster, ok

REPO = Path(__file__).resolve().parent.parent


def blocked_sacct(service: str, command: list[str]) -> ExecResult:
    if command[0] == "sacct":
        return ExecResult(-1, "", "timed out", timed_out=True)
    if command[0] == "sbatch":
        return ok("7\n")
    if command[:2] == ["sh", "-c"]:
        return ok("DBD Agent queue size: 3\n")
    return ok("idle\n")


def run(schedule: tuple[int, ...], cluster: FakeCluster) -> list[dict[str, object]]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    out = io.StringIO()
    observe(
        cluster,
        schedule,
        out,
        header={"scenario": "S01"},
        clock=lambda: now[0],
        sleep=sleep,
        echo=lambda _: None,
    )
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_transcript_has_every_probe_and_command() -> None:
    cluster = FakeCluster(handler=blocked_sacct)
    records = run(QUICK, cluster)
    assert records[0] == {"header": {"scenario": "S01"}}
    labels = [r["label"] for r in records[1:]]
    assert labels == ["t-0", "t+5s", "t+60s", "t+180s", "heal+10s"]
    for record in records[1:]:
        assert {"sacct", "sinfo", "sdiag", "squeue", "sbatch"} <= set(record)
    assert records[2]["sacct"] == {
        "state": "BLOCKED",
        "exit_code": -1,
        "stdout": "",
        "stderr": "timed out",
    }
    assert cluster.states["mysql"] == "running"


def test_full_schedule_reaches_the_measured_offsets() -> None:
    assert 840 in FULL and 900 in FULL
    labels = [r.get("label") for r in run(FULL, FakeCluster(handler=blocked_sacct))]
    assert "t+840s" in labels and "t+900s" in labels


def test_heals_when_a_probe_raises() -> None:
    cluster = FakeCluster()
    count = [0]

    def explode(service: str, command: list[str]) -> ExecResult:
        count[0] += 1
        if count[0] > 6:  # after the baseline probe
            raise KeyboardInterrupt
        return ok()

    cluster.handler = explode
    with pytest.raises(KeyboardInterrupt):
        run(QUICK, cluster)
    assert ("unpause", "mysql") in cluster.calls
    assert cluster.states["mysql"] == "running"


def test_summary_line() -> None:
    record = {
        "label": "t+60s",
        "sacct": {"state": "BLOCKED"},
        "sinfo": {"state": "ok"},
        "squeue": {"state": "ok"},
        "sdiag": {"state": "ok", "stdout": "DBD Agent queue size: 1\n"},
        "sbatch": {"state": "ok", "stdout": "9\n"},
    }
    line = summary(record)
    assert "sacct=BLOCKED" in line and "dbd_queue=1" in line and "sbatch=9" in line


class PauseThenTimeout(FakeCluster):
    """`docker pause` takes effect, then the CLI call times out anyway."""

    def pause(self, service: str) -> None:
        super().pause(service)
        raise subprocess.TimeoutExpired(["docker", "pause", service], 120)


def test_heals_when_the_inject_raises_after_the_pause() -> None:
    # The inject used to run before the try, so this left MariaDB frozen.
    cluster = PauseThenTimeout(handler=blocked_sacct)
    with pytest.raises(subprocess.TimeoutExpired):
        run(QUICK, cluster)
    assert ("unpause", "mysql") in cluster.calls
    assert cluster.states["mysql"] == "running"


def test_transcript_path_is_dated_under_the_scenario() -> None:
    when = datetime(2026, 9, 27, 8, 5, 3, tzinfo=UTC)
    assert transcript_path(EVIDENCE, SCENARIO, when) == Path(
        "evidence/S01-accounting-backend-stall/2026-09-27/smoke-20260927T080503Z.jsonl"
    )


def test_transcripts_are_neither_gitignored_nor_cleaned() -> None:
    """The first default, results/, was gitignored and deleted by `make clean`."""
    example = transcript_path(EVIDENCE, SCENARIO, datetime(2026, 9, 27, tzinfo=UTC))
    clean = (REPO / "Makefile").read_text().split("\nclean:", 1)[1].split("\n\n", 1)[0]
    assert "rm -rf" in clean and "evidence" not in clean
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", str(example)], check=False
    )
    assert ignored.returncode == 1, f"{example} is gitignored"


class HealthyFake(FakeCluster):
    def healthy(self) -> bool:
        return True

    def image_ids(self) -> str:
        return "sha256:stub"


def test_main_writes_the_transcript_under_the_evidence_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = HealthyFake()
    monkeypatch.setattr(smoke, "Cluster", lambda compose: cluster)

    def fake_observe(
        cluster: object, schedule: tuple[int, ...], out: io.TextIOBase, *, header: dict[str, object]
    ) -> None:
        out.write(json.dumps({"header": header}) + "\n")

    monkeypatch.setattr(smoke, "observe", fake_observe)
    assert smoke.main(["--evidence-dir", str(tmp_path)]) == 0
    [written] = list(tmp_path.glob(f"{SCENARIO}/*/smoke-*.jsonl"))
    header = json.loads(written.read_text().splitlines()[0])["header"]
    assert header["scenario"] == SCENARIO and header["images"] == "sha256:stub"
