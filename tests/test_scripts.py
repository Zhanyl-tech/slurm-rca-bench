"""The container-side scripts, executed locally against temporary paths.

Each script reads its paths from environment variables whose defaults are the
real container paths, so these tests can run the exact files the harness sends
into the containers, pointed at a temporary directory, with stub `squeue`,
`sinfo`, `tc`, `sacctmgr`, `sbatch` and `scancel` commands where needed. They
run under `sh` and, where installed, `dash` (the MariaDB image's /bin/sh is
dash; the Slurm image's is bash).

What this proves: the scripts' own logic — recording, restoring, refusing,
idempotency, never touching what they did not create. What it does not prove:
that Slurm, MariaDB or the kernel behave as the scripts assume. That needs a
live cluster and has not been done for these versions.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from slurmrca.cluster import ClusterError
from slurmrca.inject import INJECTIONS, script
from tests.fakes import LocalShellCluster, write_stub

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "src" / "slurmrca" / "scripts"
ALL_SCRIPTS = sorted(p.name for p in SCRIPTS_DIR.glob("*.sh"))
SHELLS = [s for s in ("sh", "dash") if shutil.which(s)]


def run(
    name: str, *args: str, env: dict[str, str], shell: str = "sh"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, "-c", script(name), f"slurmrca-{name}", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, **env},
    )


@pytest.mark.parametrize("name", ALL_SCRIPTS)
@pytest.mark.parametrize("shell", SHELLS)
def test_script_parses(name: str, shell: str) -> None:
    result = subprocess.run(
        [shell, "-n", str(SCRIPTS_DIR / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _shellcheck() -> str | None:
    """shellcheck on PATH, or the one shellcheck-py installed next to Python."""
    beside = Path(sys.executable).parent / "shellcheck"
    return shutil.which("shellcheck") or (str(beside) if beside.exists() else None)


@pytest.mark.skipif(_shellcheck() is None, reason="shellcheck not installed")
def test_scripts_pass_shellcheck() -> None:
    result = subprocess.run(
        [
            str(_shellcheck()),
            "--shell=sh",
            *(str(SCRIPTS_DIR / n) for n in ALL_SCRIPTS),
            str(SCRIPTS_DIR.parent.parent.parent / "cluster" / "build-image.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_every_script_is_used_by_an_injection() -> None:
    """A script no injection sends is dead code that will drift."""
    import slurmrca.inject as inject_module

    source = Path(inject_module.__file__).read_text(encoding="utf-8")
    for name in ALL_SCRIPTS:
        assert f'"{name}"' in source, f"{name} is not referenced by inject.py"


# ── S06: record and restore the exact mode ─────────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("original", [0o755, 0o750, 0o700])
def test_s06_restores_the_recorded_mode(tmp_path: Path, shell: str, original: int) -> None:
    target = tmp_path / "statesave"
    target.mkdir()
    target.chmod(original)
    env = {"SLURMRCA_STATE_SAVE": str(target), "SLURMRCA_STATE_DIR": str(tmp_path / "st")}
    try:
        assert run("s06_state_save_inject.sh", env=env, shell=shell).returncode == 0
        assert stat.S_IMODE(target.stat().st_mode) == 0o500
        # A second inject must not record 500 as the original.
        assert run("s06_state_save_inject.sh", env=env, shell=shell).returncode == 0
        assert (tmp_path / "st" / "S06.mode").read_text().strip() == f"{original:o}"

        assert run("s06_state_save_heal.sh", env=env, shell=shell).returncode == 0
        assert stat.S_IMODE(target.stat().st_mode) == original
        assert not (tmp_path / "st" / "S06.mode").exists()
        # Idempotent: a second heal with no record changes nothing.
        again = run("s06_state_save_heal.sh", env=env, shell=shell)
        assert again.returncode == 0 and "nothing to restore" in again.stdout
        assert stat.S_IMODE(target.stat().st_mode) == original
    finally:
        target.chmod(0o755)


def test_s06_heal_refuses_a_corrupt_record(tmp_path: Path) -> None:
    target = tmp_path / "statesave"
    target.mkdir()
    target.chmod(0o500)
    record_dir = tmp_path / "st"
    record_dir.mkdir()
    (record_dir / "S06.mode").write_text("777; rm -rf /\n")
    env = {"SLURMRCA_STATE_SAVE": str(target), "SLURMRCA_STATE_DIR": str(record_dir)}
    try:
        result = run("s06_state_save_heal.sh", env=env)
        assert result.returncode != 0
        assert stat.S_IMODE(target.stat().st_mode) == 0o500
    finally:
        target.chmod(0o755)


def test_s06_through_the_python_injection(tmp_path: Path) -> None:
    """The real Python-to-shell path, not just the script on its own."""
    target = tmp_path / "statesave"
    target.mkdir()
    target.chmod(0o750)
    cluster = LocalShellCluster(
        env={"SLURMRCA_STATE_SAVE": str(target), "SLURMRCA_STATE_DIR": str(tmp_path / "st")}
    )
    try:
        INJECTIONS["S06-state-save-unwritable"].inject(cluster)
        assert stat.S_IMODE(target.stat().st_mode) == 0o500
        INJECTIONS["S06-state-save-unwritable"].heal(cluster)
        assert stat.S_IMODE(target.stat().st_mode) == 0o750
    finally:
        target.chmod(0o755)


# ── S04 / S09: synthetic nvidia-smi never replaces or deletes a real one ───


@pytest.mark.parametrize("shell", SHELLS)
def test_gpu_install_and_remove_round_trip(tmp_path: Path, shell: str) -> None:
    target = tmp_path / "bin" / "nvidia-smi"
    env = {"SLURMRCA_NVIDIA_SMI": str(target)}
    payload = script("fake_nvidia_smi_ecc.sh")
    assert run("gpu_smi_install.sh", payload, env=env, shell=shell).returncode == 0
    output = subprocess.run([str(target), "-q", "-d", "ECC"], capture_output=True, text=True)
    assert "DRAM Uncorrectable" in output.stdout
    assert run("gpu_smi_remove.sh", env=env, shell=shell).returncode == 0
    assert not target.exists()
    assert run("gpu_smi_remove.sh", env=env, shell=shell).returncode == 0  # idempotent


def test_gpu_install_refuses_to_replace_a_real_binary(tmp_path: Path) -> None:
    target = write_stub(tmp_path, "nvidia-smi", "echo real driver tool")
    env = {"SLURMRCA_NVIDIA_SMI": str(target)}
    result = run("gpu_smi_install.sh", script("fake_nvidia_smi_wedged.sh"), env=env)
    assert result.returncode != 0 and "not installed by slurm-rca-bench" in result.stderr
    assert "real driver tool" in target.read_text()


def test_gpu_remove_leaves_a_real_binary_alone(tmp_path: Path) -> None:
    target = write_stub(tmp_path, "nvidia-smi", "echo real driver tool")
    result = run("gpu_smi_remove.sh", env={"SLURMRCA_NVIDIA_SMI": str(target)})
    assert result.returncode == 0
    assert target.exists() and "real driver tool" in target.read_text()


def test_gpu_install_refuses_an_unmarked_payload(tmp_path: Path) -> None:
    target = tmp_path / "nvidia-smi"
    result = run(
        "gpu_smi_install.sh", "#!/bin/sh\necho hi", env={"SLURMRCA_NVIDIA_SMI": str(target)}
    )
    assert result.returncode != 0 and not target.exists()


def test_wedged_nvidia_smi_fails_for_every_invocation(tmp_path: Path) -> None:
    target = tmp_path / "nvidia-smi"
    env = {"SLURMRCA_NVIDIA_SMI": str(target)}
    assert run("gpu_smi_install.sh", script("fake_nvidia_smi_wedged.sh"), env=env).returncode == 0
    for args in ([], ["-L"], ["--query-gpu=name", "--format=csv"]):
        result = subprocess.run([str(target), *args], capture_output=True, text=True)
        assert result.returncode == 255 and "Driver/library version mismatch" in result.stderr


# ── S05: delete slurmd.log and nothing else ────────────────────────────────


def test_s05_deletes_only_slurmd_log(tmp_path: Path) -> None:
    logs = tmp_path / "log"
    logs.mkdir()
    for name in ("slurmd.log", "slurmctld.log", "slurmdbd.log", "jobcomp.log"):
        (logs / name).write_text("x")
    result = run("s05_clear_slurmd_log.sh", env={"SLURMRCA_SLURMD_LOG": str(logs / "slurmd.log")})
    assert result.returncode == 0
    assert sorted(p.name for p in logs.iterdir()) == [
        "jobcomp.log",
        "slurmctld.log",
        "slurmdbd.log",
    ]


@pytest.mark.parametrize("bad", ["/var/log/slurm/*.log", "/var/log/slurm/slurmctld.log", "/etc"])
def test_s05_refuses_anything_but_slurmd_log(tmp_path: Path, bad: str) -> None:
    result = run("s05_clear_slurmd_log.sh", env={"SLURMRCA_SLURMD_LOG": bad})
    assert result.returncode != 0


# ── S03: PID-file flood; the heal never kills itself or strangers ──────────


@pytest.fixture
def slurm_stubs(tmp_path: Path) -> Iterator[dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("squeue", "sinfo"):
        write_stub(bin_dir, tool, "sleep 0.05")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURMRCA_STATE_DIR": str(tmp_path / "st"),
        "SLURMRCA_S03_CLIENTS": "3",
    }
    yield env
    # Belt and braces: never leave loops running if an assertion failed.
    pidfile = tmp_path / "st" / "S03.pids"
    if pidfile.exists():
        for line in pidfile.read_text().split():
            # Never 0: os.kill(0, ...) signals this process's whole group.
            if line.isdigit() and int(line) > 0:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(line), signal.SIGTERM)


def _args(pid: int) -> str:
    """PID's whole command line as ps prints it, or "" once the process is gone.

    `-ww`, because without it these checks failed in CI while the processes
    ran. pytest imports readline; GNU readline, finding no terminal on stdin,
    exports COLUMNS=80 into the C environment; this ps inherits it, and procps
    cuts even piped output at $COLUMNS. Both markers below come after column
    80. The scripts' own checks passed in the same runs because `run` gives
    them an environment built from os.environ, which never sees that export.
    """
    out = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


def _alive(pid: int) -> bool:
    return "slurmrca-s03-flood" in _args(pid)


@pytest.mark.parametrize("shell", SHELLS)
def test_s03_flood_starts_and_heal_stops_exactly_its_loops(
    tmp_path: Path, slurm_stubs: dict[str, str], shell: str
) -> None:
    decoy = subprocess.Popen(["sleep", "30"])
    try:
        result = run("s03_flood_inject.sh", env=slurm_stubs, shell=shell)
        assert result.returncode == 0, result.stderr
        pidfile = tmp_path / "st" / "S03.pids"
        pids = [int(p) for p in pidfile.read_text().split()]
        assert len(pids) == 3 and all(_alive(p) for p in pids)

        again = run("s03_flood_inject.sh", env=slurm_stubs, shell=shell)
        assert "already running" in again.stdout
        assert [int(p) for p in pidfile.read_text().split()] == pids

        # Plant the decoy's PID in the record: the heal must check the name.
        with pidfile.open("a") as fh:
            fh.write(f"{decoy.pid}\nnot-a-pid\n")
        healed = run("s03_flood_heal.sh", env=slurm_stubs, shell=shell)
        assert healed.returncode == 0, healed.stderr
        deadline = time.monotonic() + 5
        while any(_alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not any(_alive(p) for p in pids)
        assert decoy.poll() is None, "the heal killed a process it did not start"
        assert not pidfile.exists()

        idle = run("s03_flood_heal.sh", env=slurm_stubs, shell=shell)
        assert idle.returncode == 0 and "nothing to heal" in idle.stdout
    finally:
        decoy.terminate()
        decoy.wait()


def test_s03_inject_fails_loudly_without_squeue(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "SLURMRCA_STATE_DIR": str(tmp_path)}
    if shutil.which("squeue", path=env["PATH"]):
        pytest.skip("a real squeue is installed")
    result = run("s03_flood_inject.sh", env=env)
    assert result.returncode != 0 and "squeue not found" in result.stderr


# ── S07: guarded tc ────────────────────────────────────────────────────────


def _tc_env(tmp_path: Path, root_qdisc: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "tc.log"
    write_stub(
        bin_dir,
        "tc",
        f'echo "$*" >> "{log}"\n'
        'if [ "$2" = "show" ]; then\n'
        f'  if grep -q "replace" "{log}" && ! grep -q " del " "{log}"; then\n'
        '    echo "qdisc netem 8001: root refcnt 2 limit 1000 delay 40ms"\n'
        f"  else echo 'qdisc {root_qdisc} 0: root refcnt 2'; fi\n"
        "fi",
    )
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURMRCA_STATE_DIR": str(tmp_path / "st"),
    }


def test_s07_records_then_removes_only_its_netem(tmp_path: Path) -> None:
    env = _tc_env(tmp_path, "noqueue")
    assert run("s07_netem_inject.sh", env=env).returncode == 0
    assert run("s07_netem_inject.sh", env=env).returncode == 0  # idempotent
    assert run("s07_netem_heal.sh", env=env).returncode == 0
    log = (tmp_path / "tc.log").read_text().splitlines()
    assert sum("replace" in line for line in log) == 1
    assert sum(line.startswith("qdisc del") for line in log) == 1
    assert run("s07_netem_heal.sh", env=env).returncode == 0  # no record: no-op


def test_s07_refuses_to_replace_a_custom_root_qdisc(tmp_path: Path) -> None:
    env = _tc_env(tmp_path, "htb")
    result = run("s07_netem_inject.sh", env=env)
    assert result.returncode != 0 and "could not restore" in result.stderr
    assert "replace" not in (tmp_path / "tc.log").read_text()


def test_s07_fails_loudly_without_tc(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "SLURMRCA_STATE_DIR": str(tmp_path)}
    if shutil.which("tc", path=env["PATH"]):
        pytest.skip("a real tc is installed")
    result = run("s07_netem_inject.sh", env=env)
    assert result.returncode != 0 and "iproute-tc" in result.stderr


# ── S08: only benchmark-owned accounts are ever modified or deleted ────────


def _sacctmgr_env(tmp_path: Path, *, exists: bool) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sacctmgr.log"
    listing = "echo exists" if exists else "true"
    write_stub(
        bin_dir,
        "sacctmgr",
        f'echo "$*" >> "{log}"\nif [ "$3" = "list" ]; then {listing}; fi',
    )
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("account", ["root", "physics", "rca-x", "RCAS08", "rca s08"])
@pytest.mark.parametrize("name", ["s08_limit_inject.sh", "s08_limit_heal.sh"])
def test_s08_refuses_accounts_it_does_not_own(tmp_path: Path, account: str, name: str) -> None:
    env = _sacctmgr_env(tmp_path, exists=True) | {"SLURMRCA_S08_ACCOUNT": account}
    result = run(name, env=env)
    assert result.returncode != 0 and "refusing account" in result.stderr
    assert not (tmp_path / "sacctmgr.log").exists(), "sacctmgr must not be called at all"


def test_s08_heal_deletes_only_its_own_account(tmp_path: Path) -> None:
    env = _sacctmgr_env(tmp_path, exists=True)
    assert run("s08_limit_heal.sh", env=env).returncode == 0
    mutations = [
        line
        for line in (tmp_path / "sacctmgr.log").read_text().splitlines()
        if " list " not in f" {line} "
    ]
    assert mutations == [
        "-i modify account where name=rcas08 set GrpJobs=-1",
        "-i delete user where name=root account=rcas08",
        "-i delete account where name=rcas08",
    ]


def test_s08_heal_is_a_no_op_when_the_account_is_absent(tmp_path: Path) -> None:
    env = _sacctmgr_env(tmp_path, exists=False)
    result = run("s08_limit_heal.sh", env=env)
    assert result.returncode == 0 and "nothing to heal" in result.stdout
    assert all(
        " list " in f" {line} " for line in (tmp_path / "sacctmgr.log").read_text().splitlines()
    )


# ── S10: workload that fails ~2% by job id; heal cancels only its jobs ──────


def _s10_formula_fails(job_id: int) -> bool:
    """The job script's failure rule, transcribed. Cross-checked below."""
    h = (job_id * 2654435761) % 4294967296
    return (h // 65536) % 50 == 0


def test_s10_job_script_matches_its_formula(tmp_path: Path) -> None:
    """Run the real job script for ids 1-2000 and compare with the formula."""
    job = tmp_path / "job.sh"
    job.write_text(script("s10_job.sh"))
    loop = (
        "i=1; while [ $i -le 2000 ]; do "
        'SLURM_JOB_ID=$i SLURMRCA_S10_WORK_S=0 sh "$0" 2>/dev/null || echo $i; '
        "i=$((i+1)); done"
    )
    result = subprocess.run(
        ["sh", "-c", loop, str(job)],
        capture_output=True,
        text=True,
        check=True,
    )
    failed = [int(line) for line in result.stdout.split()]
    assert failed == [i for i in range(1, 2001) if _s10_formula_fails(i)]


def test_s10_failure_rate_is_about_one_in_fifty() -> None:
    """The 2% rate is a property of the hash, checked over ids 1-10000."""
    failures = sum(_s10_formula_fails(i) for i in range(1, 10001))
    assert 150 <= failures <= 250, f"{failures}/10000 is not about 2%"


def test_s10_job_refuses_to_run_outside_slurm() -> None:
    env = {k: v for k, v in os.environ.items() if k != "SLURM_JOB_ID"}
    result = subprocess.run(
        ["sh", "-c", script("s10_job.sh")], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode != 0


def _slurm_client_env(tmp_path: Path, *, queued: bool) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    write_stub(bin_dir, "sbatch", f'echo "sbatch $*" >> "{log}"; echo 42')
    write_stub(bin_dir, "scancel", f'echo "scancel $*" >> "{log}"')
    write_stub(bin_dir, "squeue", f'echo "squeue $*" >> "{log}"' + ("; echo 42" if queued else ""))
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURMRCA_STATE_DIR": str(tmp_path / "st"),
        "SLURMRCA_JOBDIR": str(tmp_path),
        "SLURMRCA_S10_JOBS": "3",
    }


def test_s10_submits_named_jobs_and_heal_cancels_only_those(tmp_path: Path) -> None:
    env = _slurm_client_env(tmp_path, queued=True)
    assert run("s10_workload_inject.sh", script("s10_job.sh"), env=env).returncode == 0
    assert run("s10_workload_inject.sh", script("s10_job.sh"), env=env).returncode == 0
    assert (tmp_path / "slurmrca-s10-job.sh").exists()
    assert run("s10_workload_heal.sh", env=env).returncode == 0
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert sum(c.startswith("sbatch ") for c in calls) == 3, "second inject must not resubmit"
    assert all("--job-name=slurmrca-s10" in c for c in calls if c.startswith("sbatch "))
    cancels = [c for c in calls if c.startswith("scancel")]
    assert len(cancels) == 1
    assert "--name=slurmrca-s10" in cancels[0] and "--user=" in cancels[0]
    assert not (tmp_path / "slurmrca-s10-job.sh").exists()


def test_s10_heal_never_issues_scancel_when_nothing_is_queued(tmp_path: Path) -> None:
    env = _slurm_client_env(tmp_path, queued=False)
    assert run("s10_workload_heal.sh", env=env).returncode == 0
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert not [c for c in calls if c.startswith("scancel")]


def test_s10_heal_leaves_a_foreign_job_script(tmp_path: Path) -> None:
    env = _slurm_client_env(tmp_path, queued=False)
    foreign = tmp_path / "slurmrca-s10-job.sh"
    foreign.write_text("#!/bin/sh\necho somebody else's job\n")
    assert run("s10_workload_heal.sh", env=env).returncode == 0
    assert foreign.exists()


def test_local_shell_cluster_surfaces_script_failures(tmp_path: Path) -> None:
    cluster = LocalShellCluster(env={"SLURMRCA_S08_ACCOUNT": "root"})
    with pytest.raises(ClusterError, match="refusing account"):
        INJECTIONS["S08-partition-limit-starvation"].heal(cluster)


# ── cluster/build-image.sh: the pin is verified, not trusted ────────────────

BUILD_IMAGE = SCRIPTS_DIR.parent.parent.parent / "cluster" / "build-image.sh"


@pytest.mark.parametrize(
    ("lock", "message"),
    [
        ("repo = x\ncommit = 978c3de\nslurm = 25.11.4\n", "full 40-character SHA"),
        ("repo = x\ncommit = ZZZ\nslurm = 25.11.4\n", "not a lower-case hex SHA"),
        ("commit = " + "a" * 40 + "\nslurm = 25.11.4\n", "no repo"),
        ("repo = x\ncommit = " + "a" * 40 + "\n", "no slurm version"),
    ],
)
def test_build_image_refuses_a_bad_pin(tmp_path: Path, lock: str, message: str) -> None:
    shutil.copy(BUILD_IMAGE, tmp_path / "build-image.sh")
    (tmp_path / "upstream.lock").write_text(lock)
    result = subprocess.run(
        ["sh", str(tmp_path / "build-image.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FETCH_ONLY": "1", "VENDOR_DIR": str(tmp_path / "vendor")},
    )
    assert result.returncode != 0 and message in result.stderr
    assert not (tmp_path / "vendor").exists(), "nothing may be fetched for a bad pin"


def test_build_image_reads_the_real_pin() -> None:
    lock = (BUILD_IMAGE.parent / "upstream.lock").read_text()
    assert "commit = 978c3ded1f72c1855e528a56559a7a51142eb39c" in lock
    for shell in SHELLS:
        assert subprocess.run([shell, "-n", str(BUILD_IMAGE)], check=False).returncode == 0


def _git(*args: str) -> str:
    """git with a throwaway identity and no signing, whatever the user's config."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    """A local stand-in for upstream: one commit holding a Dockerfile."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-q", str(repo))
    (repo / "Dockerfile").write_text("FROM scratch\n")
    _git("-C", str(repo), "add", "Dockerfile")
    _git("-C", str(repo), "commit", "-qm", "upstream")
    return repo, _git("-C", str(repo), "rev-parse", "HEAD")


def _docker_stub(tmp_path: Path, *, image: bool, label: str | None) -> dict[str, str]:
    """A `docker` that records calls and remembers the label a build set.

    `image inspect` fails when there is no image and prints the label (empty
    when unset, as Go's template `index` does for a missing key) or an id.
    """
    state = tmp_path / "docker-state"
    state.mkdir()
    if image:
        (state / "image").touch()
    if label is not None:
        (state / "label").write_text(label + "\n")
    bin_dir = tmp_path / "docker-bin"
    bin_dir.mkdir()
    write_stub(
        bin_dir,
        "docker",
        f'state="{state}"\n'
        'echo "$*" >> "$state/calls.log"\n'
        'if [ "$1 $2" = "image inspect" ]; then\n'
        '  [ -e "$state/image" ] || exit 1\n'
        '  case "$*" in\n'
        '    *Labels*) cat "$state/label" 2>/dev/null || echo ;;\n'
        "    *) echo sha256:stub ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = build ]; then\n'
        '  prev=""\n'
        '  for arg in "$@"; do\n'
        '    if [ "$prev" = --label ]; then printf "%s\\n" "${arg#*=}" > "$state/label"; fi\n'
        '    prev="$arg"\n'
        "  done\n"
        '  touch "$state/image"\n'
        "fi\n",
    )
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _build_image(
    tmp_path: Path, repo: Path, sha: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    cluster = tmp_path / "cluster"
    cluster.mkdir(exist_ok=True)
    shutil.copy(BUILD_IMAGE, cluster / "build-image.sh")
    (cluster / "upstream.lock").write_text(f"repo = {repo}\ncommit = {sha}\nslurm = 25.11.4\n")
    return subprocess.run(
        ["sh", str(cluster / "build-image.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "VENDOR_DIR": str(tmp_path / "vendor"), **env},
    )


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker-state" / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_build_image_fetches_and_verifies_a_clean_checkout(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    repo, sha = upstream
    result = _build_image(tmp_path, repo, sha, {"FETCH_ONLY": "1"})
    assert result.returncode == 0, result.stderr
    assert f"upstream verified at {sha}" in result.stdout
    # A second run over the existing checkout is fine too.
    assert _build_image(tmp_path, repo, sha, {"FETCH_ONLY": "1"}).returncode == 0


@pytest.mark.parametrize(
    ("change", "expected"),
    [("edit", "M Dockerfile"), ("untracked", "?? extra.txt")],
)
def test_build_image_refuses_a_checkout_that_differs_from_the_commit(
    tmp_path: Path, upstream: tuple[Path, str], change: str, expected: str
) -> None:
    """The SHA check used to compare HEAD only, and `docker build` sends the tree."""
    repo, sha = upstream
    assert _build_image(tmp_path, repo, sha, {"FETCH_ONLY": "1"}).returncode == 0
    vendor = tmp_path / "vendor"
    if change == "edit":
        with (vendor / "Dockerfile").open("a") as fh:
            fh.write("RUN echo tampered\n")
    else:
        (vendor / "extra.txt").write_text("not from upstream\n")
    result = _build_image(tmp_path, repo, sha, {"FETCH_ONLY": "1"})
    assert result.returncode != 0
    assert "not a clean checkout" in result.stderr and expected in result.stderr
    assert "upstream verified" not in result.stdout
    # Refused, not repaired: the change is still there for whoever made it.
    assert vendor.exists() and (
        "tampered" in (vendor / "Dockerfile").read_text() or (vendor / "extra.txt").exists()
    )


def test_build_image_builds_with_a_commit_tag_and_label(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    repo, sha = upstream
    env = _docker_stub(tmp_path, image=False, label=None)
    result = _build_image(tmp_path, repo, sha, env)
    assert result.returncode == 0, result.stderr
    builds = [c for c in _docker_calls(tmp_path) if c.startswith("build ")]
    assert len(builds) == 1
    assert f"--label org.slurmrca.upstream-commit={sha}" in builds[0]
    assert f"-t slurmrca/slurm-docker-cluster:25.11.4-{sha[:7]}" in builds[0]


def test_build_image_reuses_only_an_image_built_from_the_pin(
    tmp_path: Path, upstream: tuple[Path, str]
) -> None:
    repo, sha = upstream
    env = _docker_stub(tmp_path, image=True, label=sha)
    result = _build_image(tmp_path, repo, sha, env)
    assert result.returncode == 0, result.stderr
    assert "was built from" in result.stdout
    assert not [c for c in _docker_calls(tmp_path) if c.startswith("build ")]
    assert not (tmp_path / "vendor").exists(), "nothing needs fetching for a verified image"


@pytest.mark.parametrize("label", [None, "0" * 40], ids=["no-label", "other-commit"])
def test_build_image_rebuilds_a_same_tag_image_from_elsewhere(
    tmp_path: Path, upstream: tuple[Path, str], label: str | None
) -> None:
    """The first version trusted any image with the right tag, fetch and SHA check skipped.

    `no-label` is what upstream's own `make build` would leave; `other-commit`
    is a pin bumped with the Slurm version unchanged.
    """
    repo, sha = upstream
    env = _docker_stub(tmp_path, image=True, label=label)
    result = _build_image(tmp_path, repo, sha, env)
    assert result.returncode == 0, result.stderr
    assert "rebuilding" in result.stdout
    assert f"upstream verified at {sha}" in result.stdout
    assert len([c for c in _docker_calls(tmp_path) if c.startswith("build ")]) == 1
    assert (tmp_path / "docker-state" / "label").read_text().strip() == sha


def test_compose_uses_the_image_build_image_builds() -> None:
    """The compose file cannot name one tag while the build script makes another."""
    cluster = BUILD_IMAGE.parent
    lock = dict(
        re.findall(r"^(\w+)\s*=\s*(\S+)\s*$", (cluster / "upstream.lock").read_text(), re.M)
    )
    expected = f"slurmrca/slurm-docker-cluster:{lock['slurm']}-{lock['commit'][:7]}"
    compose = yaml.safe_load((cluster / "docker-compose.yml").read_text())
    slurm_services = {
        name: service for name, service in compose["services"].items() if name != "mysql"
    }
    assert slurm_services, "no Slurm services found in the compose file"
    for name, service in slurm_services.items():
        assert service["image"] == expected, name
        # Never pulled: the only trustworthy copy is the one build-image.sh made.
        assert service["pull_policy"] == "never", name


# ── S02: the lock client is tracked; the heal finds it in every state ───────


@pytest.fixture
def mysql_stub(tmp_path: Path) -> Iterator[tuple[dict[str, str], Path]]:
    """A `mysql` that emulates the pieces of the server S02 relies on.

    With `-e` it answers the scripts' queries from files in a state directory:
    `session` exists while a lock session is connected (waiting or holding),
    `held` while it holds the lock, and KILL CONNECTION removes both. Without
    `-e` it is the lock client: it opens the session, takes the lock only if
    `grant` exists, and exits once the session is killed, as the real client
    does when the server ends its connection. `down` makes every call fail the
    way an unreachable server does.
    """
    state = tmp_path / "mysql-state"
    state.mkdir()
    bin_dir = tmp_path / "mysql-bin"
    bin_dir.mkdir()
    write_stub(
        bin_dir,
        "mysql",
        f'state="{state}"\n'
        'query=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -e ]; then query="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'echo "query: $query" >> "$state/calls.log"\n'
        'if [ -e "$state/down" ]; then\n'
        '  echo "ERROR 2002 (HY000): Can\'t connect to server" >&2; exit 1\n'
        "fi\n"
        'if [ -z "$query" ]; then\n'
        '  echo "$$" >> "$state/clients"\n'
        '  cat > "$state/client.sql"\n'
        '  touch "$state/session"\n'
        '  if [ -e "$state/grant" ]; then touch "$state/held"; fi\n'
        "  i=0\n"
        '  while [ -e "$state/session" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done\n'
        "  exit 1\n"
        "fi\n"
        'case "$query" in\n'
        '  *"KILL CONNECTION"*) rm -f "$state/session" "$state/held" ;;\n'
        "  *table_name*) echo linux_job_table ;;\n"
        '  *"WHERE id = "*) if [ -e "$state/session" ]; then echo 42; fi ;;\n'
        '  *_held*) if [ -e "$state/held" ]; then echo 42; fi ;;\n'
        '  *processlist*) if [ -e "$state/session" ]; then echo 42; fi ;;\n'
        "esac\n",
    )
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURMRCA_STATE_DIR": str(tmp_path / "st"),
        "MYSQL_USER": "slurm",
        "MYSQL_PASSWORD": "password",
    }
    yield env, state
    # Never leave a stub client behind if an assertion failed.
    clients = state / "clients"
    if clients.exists():
        for line in clients.read_text().split():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(line), signal.SIGKILL)


def _running(pid: int) -> bool:
    return "slurm_acct_db" in _args(pid)


def _clients(state: Path) -> list[int]:
    path = state / "clients"
    return [int(p) for p in path.read_text().split()] if path.exists() else []


@pytest.mark.parametrize("shell", SHELLS)
def test_s02_inject_holds_the_lock_and_heal_ends_client_and_session(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path], shell: str
) -> None:
    env, state = mysql_stub
    (state / "grant").touch()
    result = run("s02_lock_inject.sh", env=env, shell=shell)
    assert result.returncode == 0, result.stderr
    assert "holding a WRITE lock" in result.stdout
    sql = (state / "client.sql").read_text()
    # The marker is on the statement that waits for the lock, not only on the
    # one that holds it, and the wait is bounded.
    assert "LOCK TABLES `linux_job_table` AS slurmrca_s02_lock WRITE WAIT 10;" in sql
    assert "AS slurmrca_s02_lock_held" in sql
    [client] = _clients(state)
    assert (tmp_path / "st" / "S02.pid").read_text().strip() == str(client)
    assert _running(client)

    again = run("s02_lock_inject.sh", env=env, shell=shell)
    assert again.returncode == 0 and "already held" in again.stdout
    assert len(_clients(state)) == 1, "a second inject must not start a second client"

    healed = run("s02_lock_heal.sh", env=env, shell=shell)
    assert healed.returncode == 0, healed.stderr
    assert "stopped lock client" in healed.stdout and "killed lock session 42" in healed.stdout
    assert not _running(client)
    assert not (state / "session").exists()
    assert not list((tmp_path / "st").iterdir()), "the heal leaves no state behind"

    idle = run("s02_lock_heal.sh", env=env, shell=shell)
    assert idle.returncode == 0 and "nothing to heal" in idle.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_s02_inject_that_never_gets_the_lock_cleans_up_after_itself(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path], shell: str
) -> None:
    """The first inject exited 1 here and left its client running, unseen by the heal."""
    env, state = mysql_stub
    result = run("s02_lock_inject.sh", env=env | {"SLURMRCA_S02_WAIT_S": "1"}, shell=shell)
    assert result.returncode != 0 and "not held within 1s" in result.stderr
    [client] = _clients(state)
    assert not _running(client), "a failed inject must not leave its lock client running"
    assert not (state / "session").exists(), "nor the session it opened"
    assert not (tmp_path / "st" / "S02.pid").exists()


def test_s02_heal_stops_a_client_left_by_an_interrupted_inject(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path]
) -> None:
    """A client still waiting in LOCK TABLES has no SLEEP marker; the heal must find it anyway."""
    env, state = mysql_stub
    stub = Path(env["PATH"].split(os.pathsep)[0]) / "mysql"
    client = subprocess.Popen(
        [str(stub), "-h", "mysql", "-uslurm", "-ppassword", "slurm_acct_db"],
        stdin=subprocess.PIPE,
        text=True,
    )
    try:
        assert client.stdin is not None
        client.stdin.write("LOCK TABLES ...;\n")
        client.stdin.close()
        deadline = time.monotonic() + 5
        while not (state / "session").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        (tmp_path / "st").mkdir()
        (tmp_path / "st" / "S02.pid").write_text(f"{client.pid}\n")

        healed = run("s02_lock_heal.sh", env=env)
        assert healed.returncode == 0, healed.stderr
        assert f"stopped lock client {client.pid}" in healed.stdout
        assert "killed lock session 42" in healed.stdout
        assert client.wait(timeout=5) != 0
        assert not (state / "session").exists()
        assert not (tmp_path / "st" / "S02.pid").exists()
    finally:
        if client.poll() is None:
            client.kill()
            client.wait()


def _decoy(kind: str, name: str) -> subprocess.Popen[bytes]:
    """A process that is not the injection's own, for a PID record to point at.

    `script-shell` has the command line the harness gives the shell running
    script `name` (`sh -c <the whole script> slurmrca-<name>`), so it carries
    every marker the script mentions, as a real inject or heal shell does. It
    only sleeps.
    """
    if kind == "sleep":
        return subprocess.Popen(["sleep", "30"])
    label = "slurmrca-" + name.removesuffix(".sh")
    return subprocess.Popen(["sh", "-c", "sleep 30; exit\n" + script(name), label])


@pytest.mark.parametrize("kind", ["sleep", "script-shell"])
def test_s02_heal_never_signals_a_reused_pid(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path], kind: str
) -> None:
    """`script-shell`: at full width the heal's own command line names mysql and the database."""
    env, state = mysql_stub
    decoy = _decoy(kind, "s02_lock_heal.sh")
    record = tmp_path / "st" / "S02.pid"
    try:
        record.parent.mkdir()
        record.write_text(f"{decoy.pid}\n")
        healed = run("s02_lock_heal.sh", env=env)
        assert healed.returncode == 0, healed.stderr
        assert "nothing to heal" in healed.stdout
        assert decoy.poll() is None, "the heal killed a process that is not its client"
        assert not record.exists(), "a stale record is dropped"

        # Nor may the inject take it for an earlier client still running.
        record.write_text(f"{decoy.pid}\n")
        (state / "grant").touch()
        injected = run("s02_lock_inject.sh", env=env)
        assert injected.returncode == 0, injected.stderr
        assert "holding a WRITE lock" in injected.stdout
        assert run("s02_lock_heal.sh", env=env).returncode == 0
        assert decoy.poll() is None
    finally:
        decoy.terminate()
        decoy.wait()


@pytest.mark.parametrize("name", ["s02_lock_inject.sh", "s02_lock_heal.sh"])
def test_s02_scripts_fail_when_the_database_cannot_be_reached(
    mysql_stub: tuple[dict[str, str], Path], name: str
) -> None:
    """An unreachable database is a failure, never "nothing to heal"."""
    env, state = mysql_stub
    (state / "down").touch()
    result = run(name, env=env)
    assert result.returncode != 0
    assert "nothing to heal" not in result.stdout


# ── ps reads whole command lines, whatever width it assumes ────────────────
#
# ps(1): "If ps can not determine the display width, as when output is
# redirected (piped) into a file or another command, the output width is
# undefined"; procps cuts it at $COLUMNS, and `-ww` makes it unlimited on
# procps and BSD ps alike. The S03 loops' name and the S02 client's database
# name both come after column 80.


def test_every_ps_in_the_scripts_is_full_width() -> None:
    calls = [
        (name, line.strip())
        for name in ALL_SCRIPTS
        for line in (SCRIPTS_DIR / name).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and re.search(r"(^|[\s$(|;&])ps\s", line)
    ]
    assert len(calls) >= 4, calls  # S02 inject and heal, S03 inject and heal
    for name, line in calls:
        assert re.search(r"(^|[\s$(|;&])ps -ww ", line), f"{name}: {line}"


Narrow = Callable[[dict[str, str]], dict[str, str]]


@pytest.fixture(params=["COLUMNS=80", "80-column-ps"])
def narrow_ps(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Narrow:
    """Make every ps, unless given -ww, print at most 80 columns.

    `COLUMNS=80` is the condition CI had. procps honours it with its output
    piped; BSD ps ignores it when piped, so on macOS that case passes either
    way. `80-column-ps` puts a ps first on PATH that cuts at 80 columns unless
    given -ww, so the width bites on every platform. Both apply to the test's
    own checks (through os.environ) and to the scripts (through the returned
    function, which adjusts a script environment).
    """
    if request.param == "COLUMNS=80":
        monkeypatch.setenv("COLUMNS", "80")
        return lambda env: {**env, "COLUMNS": "80"}
    real = shutil.which("ps")
    assert real is not None
    bin_dir = tmp_path / "narrow-ps"
    bin_dir.mkdir()
    write_stub(
        bin_dir,
        "ps",
        'for arg in "$@"; do\n'
        f'  if [ "$arg" = -ww ]; then exec "{real}" "$@"; fi\n'
        "done\n"
        f'out=$("{real}" -ww "$@")\n'
        "status=$?\n"
        '[ -z "$out" ] || printf "%s\\n" "$out" | cut -c1-80\n'
        'exit "$status"',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda env: {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}"}


@pytest.mark.parametrize("shell", SHELLS)
def test_s03_finds_its_loops_under_an_80_column_ps(
    tmp_path: Path, slurm_stubs: dict[str, str], shell: str, narrow_ps: Narrow
) -> None:
    """The narrow ps that failed CI's own checks, applied to the scripts.

    A loop's command line is 88 characters with its name last. In CI only the
    tests' ps ran narrow (see `_args`), but under one the scripts before `-ww`
    were wrong too: the inject counted no loop alive and failed with all of
    them running, and the heal found none, reported success and left them
    running.
    """
    env = narrow_ps(slurm_stubs)
    result = run("s03_flood_inject.sh", env=env, shell=shell)
    assert result.returncode == 0, result.stderr
    assert "started 3 of 3 RPC client loops" in result.stdout
    pids = [int(p) for p in (tmp_path / "st" / "S03.pids").read_text().split()]
    assert all(len(_args(p)) > 80 and _alive(p) for p in pids)

    healed = run("s03_flood_heal.sh", env=env, shell=shell)
    assert healed.returncode == 0, healed.stderr
    deadline = time.monotonic() + 5
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(p) for p in pids)


@pytest.mark.parametrize("shell", SHELLS)
def test_s02_finds_its_client_under_an_80_column_ps(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path], shell: str, narrow_ps: Narrow
) -> None:
    """The client's command line has the database name last, past column 80.

    Under an 80-column ps the heal did not recognise the client it recorded:
    it left the client to the server ending its session, and a client not yet
    connected ran on.
    """
    env, state = mysql_stub
    env = narrow_ps(env)
    (state / "grant").touch()
    result = run("s02_lock_inject.sh", env=env, shell=shell)
    assert result.returncode == 0, result.stderr
    [client] = _clients(state)
    assert len(_args(client)) > 80 and _running(client)

    healed = run("s02_lock_heal.sh", env=env, shell=shell)
    assert healed.returncode == 0, healed.stderr
    assert f"stopped lock client {client}" in healed.stdout
    assert not _running(client)


@pytest.mark.parametrize("kind", ["sleep", "script-shell"])
def test_s03_never_takes_a_reused_pid_for_a_loop(
    tmp_path: Path, slurm_stubs: dict[str, str], kind: str
) -> None:
    """`script-shell`: at full width an inject or heal shell's command line contains the name.

    A "contains the name" check took such a PID for a loop: the inject said
    "already running" and started nothing, and the heal signalled it.
    """
    decoy = _decoy(kind, "s03_flood_heal.sh")
    record = tmp_path / "st" / "S03.pids"
    try:
        record.parent.mkdir()
        record.write_text(f"{decoy.pid}\n")
        injected = run("s03_flood_inject.sh", env=slurm_stubs)
        assert injected.returncode == 0, injected.stderr
        assert "started 3 of 3 RPC client loops" in injected.stdout

        with record.open("a") as fh:
            fh.write(f"{decoy.pid}\n")
        healed = run("s03_flood_heal.sh", env=slurm_stubs)
        assert healed.returncode == 0, healed.stderr
        assert decoy.poll() is None, "the heal signalled a process that is not one of its loops"
    finally:
        decoy.terminate()
        decoy.wait()


# ── A tool that fails is a failure, not an empty answer ─────────────────────


def _failing_tool(tmp_path: Path, name: str) -> dict[str, str]:
    """A stub that logs its call, prints an error and exits 1, like a daemon that is down."""
    bin_dir = tmp_path / "failing-bin"
    bin_dir.mkdir()
    write_stub(
        bin_dir,
        name,
        f'echo "{name} $*" >> "{tmp_path / "calls.log"}"\n'
        'echo "Problem talking to the database: Connection refused" >&2\n'
        "exit 1",
    )
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURMRCA_STATE_DIR": str(tmp_path / "st"),
    }


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", ["s08_limit_inject.sh", "s08_limit_heal.sh"])
def test_s08_fails_when_sacctmgr_cannot_reach_slurmdbd(
    tmp_path: Path, name: str, shell: str
) -> None:
    """`[ -z "$(sacctmgr ...)" ]` hid the failure: the heal said "nothing to heal", exit 0."""
    result = run(name, env=_failing_tool(tmp_path, "sacctmgr"), shell=shell)
    assert result.returncode != 0
    assert "nothing to heal" not in result.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert len(calls) == 1 and " list " in f" {calls[0]} ", "stop at the failed listing"


@pytest.mark.parametrize("shell", SHELLS)
def test_s07_heal_fails_and_keeps_its_record_when_tc_fails(tmp_path: Path, shell: str) -> None:
    env = _failing_tool(tmp_path, "tc")
    record = tmp_path / "st" / "S07.qdisc"
    record.parent.mkdir()
    record.write_text("qdisc noqueue 0: root refcnt 2\n")
    result = run("s07_netem_heal.sh", env=env, shell=shell)
    assert result.returncode != 0
    assert record.exists(), "the record is what the next heal needs"


def _failing_ps(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """`env` with a ps first on PATH that fails, as a missing one or one that rejects -ww would."""
    bin_dir = tmp_path / "failing-ps"
    bin_dir.mkdir()
    write_stub(bin_dir, "ps", 'echo "ps: illegal option -- w" >&2\nexit 1')
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}"}


def _gone(pids: list[int]) -> bool:
    """No process has any of these PIDs, zombie or not."""
    return not any(_args(p) for p in pids)


@pytest.mark.parametrize("shell", SHELLS)
def test_s03_fails_when_ps_cannot_read_a_recorded_loop(
    tmp_path: Path, slurm_stubs: dict[str, str], shell: str
) -> None:
    """A failed ps prints nothing, as it does for a process that is gone.

    The heal read that as every loop gone: it printed "S03 flood stopped",
    exited 0 and deleted the record, and every loop ran on. The inject would
    have dropped the record as stale and started more.
    """
    started = run("s03_flood_inject.sh", env=slurm_stubs, shell=shell)
    assert started.returncode == 0, started.stderr
    pidfile = tmp_path / "st" / "S03.pids"
    record = pidfile.read_text()
    pids = [int(p) for p in record.split()]
    failing = _failing_ps(tmp_path, slurm_stubs)
    try:
        for name in ("s03_flood_heal.sh", "s03_flood_inject.sh"):
            result = run(name, env=failing, shell=shell)
            assert result.returncode != 0, result.stdout
            assert f"ps could not read PID {pids[0]}" in result.stderr
            assert pidfile.read_text() == record, "the record of running loops is kept"
            assert all(_alive(p) for p in pids)
    finally:
        # The fixture stops only what the record lists, and a heal that failed
        # this test may have deleted it.
        pidfile.write_text(record)

    healed = run("s03_flood_heal.sh", env=slurm_stubs, shell=shell)
    assert healed.returncode == 0, healed.stderr
    deadline = time.monotonic() + 5
    while not _gone(pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _gone(pids)

    # Only a PID that exists is a failure: a stale record, or a 0 in it, is not.
    pidfile.write_text("0\n" + record)
    stale = run("s03_flood_heal.sh", env=failing, shell=shell)
    assert stale.returncode == 0, stale.stderr
    assert "S03 flood stopped" in stale.stdout and not pidfile.exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_s02_fails_when_ps_cannot_read_the_recorded_client(
    tmp_path: Path, mysql_stub: tuple[dict[str, str], Path], shell: str
) -> None:
    """The heal skipped a client ps could not read and could report "nothing to heal".

    The inject would have dropped the record and started a second client.
    """
    env, state = mysql_stub
    stub = Path(env["PATH"].split(os.pathsep)[0]) / "mysql"
    # A client still waiting for the lock, so the inject gets as far as its record.
    client = subprocess.Popen(
        [str(stub), "-h", "mysql", "-uslurm", "-ppassword", "slurm_acct_db"],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not (state / "session").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        record = tmp_path / "st" / "S02.pid"
        record.parent.mkdir()
        record.write_text(f"{client.pid}\n")
        failing = _failing_ps(tmp_path, env)

        for name in ("s02_lock_heal.sh", "s02_lock_inject.sh"):
            result = run(name, env=failing, shell=shell)
            assert result.returncode != 0, result.stdout
            assert f"ps could not read PID {client.pid}" in result.stderr
            assert "nothing to heal" not in result.stdout
            assert client.poll() is None and record.exists()
            assert _clients(state) == [client.pid], "no second client"

        healed = run("s02_lock_heal.sh", env=env, shell=shell)
        assert healed.returncode == 0, healed.stderr
        assert f"stopped lock client {client.pid}" in healed.stdout
        assert client.wait(timeout=5) != 0

        record.write_text("0\n")
        stale = run("s02_lock_heal.sh", env=failing, shell=shell)
        assert stale.returncode == 0, stale.stderr
        assert "nothing to heal" in stale.stdout and not record.exists()
    finally:
        if client.poll() is None:
            client.kill()
            client.wait()
