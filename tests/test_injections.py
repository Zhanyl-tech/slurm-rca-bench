"""Every scenario has a reversible, documented injection, and heals are safe.

These run without Docker. The registry checks are the original ones. The rest
drive each injection and heal against :class:`~tests.fakes.FakeCluster`, which
refuses what Docker refuses, so the defects an audit found by reading the first
version (a heal that could not find a stopped container, heals that were not
idempotent, a lifecycle with no recovery step) are regression-tested here.
Nothing in this file shows that an injection *works* on a real cluster; that
needs `make cluster-up` and `slurm-rca run`, and has not been done for the
2026-09 changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slurmrca.cluster import ClusterError, ExecResult
from slurmrca.inject import (
    CONTAINER_HEALS,
    INJECTIONS,
    heal_all,
    heal_order,
    injection_for,
    run_injection,
    script,
)
from slurmrca.loader import load_all
from slurmrca.spec import Scenario
from tests.fakes import FakeCluster, ok

SCENARIOS = load_all()
IDS = [s.id for s in SCENARIOS]
INJECTION_SCRIPTS = sorted(
    p.name for p in (Path(__file__).resolve().parent.parent / "src/slurmrca/scripts").glob("*.sh")
)
S01 = "S01-accounting-backend-stall"
S05 = "S05-undiagnosable-node-reboot"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_every_scenario_has_an_injection(scenario: Scenario) -> None:
    assert scenario.id in INJECTIONS, f"{scenario.id} has no injection implementation"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_every_injection_has_a_heal(scenario: Scenario) -> None:
    # An injection without a heal poisons every scenario that runs after it.
    injection = injection_for(scenario.id)
    assert callable(injection.inject)
    assert callable(injection.heal)
    assert injection.inject is not injection.heal


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_mechanism_is_described(scenario: Scenario) -> None:
    assert injection_for(scenario.id).mechanism.strip()


def test_no_orphan_injections() -> None:
    """An injection with no scenario is dead code that will drift."""
    assert set(INJECTIONS) == set(IDS)


def test_unknown_scenario_raises() -> None:
    with pytest.raises(ClusterError, match="no injection implemented"):
        injection_for("S99-does-not-exist")


def test_legacy_s01_id_still_resolves() -> None:
    assert injection_for("S01-storage-stall-scheduling-halt").scenario_id == S01


class TestHealsAreIdempotent:
    """Every heal succeeds on a clean cluster and changes nothing there."""

    @pytest.mark.parametrize("scenario_id", sorted(INJECTIONS))
    def test_heal_on_clean_cluster_is_harmless(self, scenario_id: str) -> None:
        cluster = FakeCluster()
        INJECTIONS[scenario_id].heal(cluster)
        INJECTIONS[scenario_id].heal(cluster)
        assert set(cluster.states.values()) == {"running"}
        # No Docker-level action on a clean cluster: only scripts and queries.
        assert not [c for c in cluster.calls if c[0] in ("pause", "unpause", "kill", "start")]

    def test_heal_all_on_clean_cluster_reports_no_failures(self) -> None:
        cluster = FakeCluster()
        messages: list[str] = []
        assert heal_all(cluster, log=messages.append) == []
        assert len(messages) == len(INJECTIONS)

    def test_heal_all_runs_s01_before_s02(self) -> None:
        # S02's heal talks to the database that S01 may have frozen.
        cluster = FakeCluster(states={"mysql": "paused", **_running("slurmdbd", "slurmctld")})
        cluster.states["cpu-worker"] = "running"
        heal_all(cluster, log=lambda _: None)
        unpause = cluster.calls.index(("unpause", "mysql"))
        s02 = next(
            i
            for i, c in enumerate(cluster.calls)
            if c[0] == "exec" and c[5:6] == ("slurmrca-s02_lock_heal",)
        )
        assert unpause < s02

    def test_heal_all_restores_containers_before_any_heal_execs_into_them(self) -> None:
        # An S05 run killed before its own heal leaves the worker exited. In
        # plain scenario order S04's heal exec'd into it before S05's heal
        # started it, so `heal --all` failed on a cluster it could have healed.
        cluster = FakeCluster()
        cluster.states["cpu-worker"] = "exited"
        cluster.states["mysql"] = "paused"
        assert heal_all(cluster, log=lambda _: None) == []
        start = cluster.calls.index(("start", "cpu-worker"))
        unpause = cluster.calls.index(("unpause", "mysql"))
        execs = [i for i, c in enumerate(cluster.calls) if c[0] == "exec"]
        worker_execs = [i for i in execs if cluster.calls[i][1] == "cpu-worker"]
        assert worker_execs and min(worker_execs) > start
        assert min(execs) > unpause
        assert set(cluster.states.values()) == {"running"}

    def test_heal_order_puts_container_heals_first_and_covers_every_scenario(self) -> None:
        order = heal_order()
        assert order[: len(CONTAINER_HEALS)] == list(CONTAINER_HEALS)
        assert sorted(order) == sorted(INJECTIONS) and len(order) == len(set(order))

    def test_worker_side_heal_fails_on_a_stopped_worker(self) -> None:
        # What the fake now mirrors from `docker compose exec`; without it the
        # ordering bug above was invisible to every test.
        cluster = FakeCluster()
        cluster.states["cpu-worker"] = "exited"
        with pytest.raises(ClusterError, match="not running"):
            INJECTIONS["S04-gpu-ecc-drain"].heal(cluster)

    def test_heal_all_keeps_going_and_reports_every_failure(self) -> None:
        def failing(service: str, command: list[str]) -> ExecResult:
            if command[:2] == ["sh", "-c"] and command[3] in (
                "slurmrca-s03_flood_heal",
                "slurmrca-s06_state_save_heal",
            ):
                return ExecResult(1, "", "boom")
            return ok()

        cluster = FakeCluster(handler=failing)
        failures = heal_all(cluster, log=lambda _: None)
        assert [sid for sid, _ in failures] == [
            "S03-controller-rpc-saturation",
            "S06-state-save-unwritable",
        ]
        assert "boom" in failures[0][1]


def _running(*services: str) -> dict[str, str]:
    return dict.fromkeys(services, "running")


class TestS01:
    def test_inject_pauses_and_verifies(self) -> None:
        cluster = FakeCluster()
        INJECTIONS[S01].inject(cluster)
        assert cluster.states["mysql"] == "paused"
        INJECTIONS[S01].inject(cluster)  # second inject is a no-op, not an error
        assert cluster.calls.count(("pause", "mysql")) == 1

    def test_heal_unpauses_once(self) -> None:
        cluster = FakeCluster()
        INJECTIONS[S01].inject(cluster)
        INJECTIONS[S01].heal(cluster)
        INJECTIONS[S01].heal(cluster)
        assert cluster.states["mysql"] == "running"
        assert cluster.calls.count(("unpause", "mysql")) == 1

    def test_inject_refuses_a_stopped_database(self) -> None:
        cluster = FakeCluster()
        cluster.states["mysql"] = "exited"
        with pytest.raises(ClusterError, match="not running"):
            INJECTIONS[S01].inject(cluster)


class TestS05:
    def test_inject_clears_only_the_slurmd_log_then_kills(self) -> None:
        cluster = FakeCluster()
        INJECTIONS[S05].inject(cluster)
        assert cluster.scripts_run() == [("cpu-worker", "slurmrca-s05_clear_slurmd_log")]
        assert cluster.calls[-1] == ("kill", "cpu-worker", "KILL")
        assert cluster.states["cpu-worker"] == "exited"
        INJECTIONS[S05].inject(cluster)  # already down: no second kill
        assert cluster.calls.count(("kill", "cpu-worker", "KILL")) == 1

    def test_heal_starts_a_stopped_worker(self) -> None:
        # The first heal resolved containers with `docker compose ps -q`, which
        # lists only running containers, so it could never find this one.
        cluster = FakeCluster()
        INJECTIONS[S05].inject(cluster)
        INJECTIONS[S05].heal(cluster)
        assert cluster.states["cpu-worker"] == "running"
        assert ("start", "cpu-worker") in cluster.calls

    def test_heal_resumes_only_nodes_slurm_reports_down(self) -> None:
        def sinfo(service: str, command: list[str]) -> ExecResult:
            if command[0] == "sinfo":
                assert "--states=down" in command
                return ok("c1\nc1\n")
            return ok()

        cluster = FakeCluster(handler=sinfo)
        INJECTIONS[S05].heal(cluster)
        resumes = [c for c in cluster.calls if c[2:4] == ("scontrol", "update")]
        assert resumes == [
            ("exec", "slurmctld", "scontrol", "update", "NodeName=c1", "State=RESUME")
        ]
        assert not any("NodeName=ALL" in part for c in cluster.calls for part in c)

    def test_heal_refuses_a_suspicious_node_name(self) -> None:
        cluster = FakeCluster(
            handler=lambda s, c: ok("c1;scontrol shutdown\n") if c[0] == "sinfo" else ok()
        )
        with pytest.raises(ClusterError, match="unexpected node name"):
            INJECTIONS[S05].heal(cluster)
        assert not [c for c in cluster.calls if c[2:3] == ("scontrol",)]

    def test_lifecycle_restarts_the_node_at_300s_then_heals(self) -> None:
        cluster = FakeCluster()
        now = [0.0]
        events: list[tuple[float, str]] = []

        def sleep(seconds: float) -> None:
            now[0] += seconds

        def log(message: str) -> None:
            events.append((now[0], message))

        run_injection(cluster, INJECTIONS[S05], 420, sleep=sleep, clock=lambda: now[0], log=log)
        starts = [i for i, c in enumerate(cluster.calls) if c == ("start", "cpu-worker")]
        assert len(starts) == 1, "recovery should start the worker exactly once"
        recovery = next(t for t, m in events if "recovery step" in m)
        assert recovery == pytest.approx(300.0)
        assert now[0] == pytest.approx(420.0)
        assert cluster.states["cpu-worker"] == "running"


class TestLifecycle:
    def test_heals_first_and_last_even_when_inject_fails(self) -> None:
        cluster = FakeCluster()
        cluster.states["mysql"] = "exited"  # S01 inject will refuse
        with pytest.raises(ClusterError):
            run_injection(
                cluster, INJECTIONS[S01], 10, sleep=lambda _: None, clock=lambda: 0.0, log=print
            )
        # Heal ran before and after; neither tried to unpause a stopped DB.
        assert ("unpause", "mysql") not in cluster.calls

    @pytest.mark.parametrize(
        ("scenario_id", "service", "left", "restore"),
        [
            # An S05 run killed mid-window leaves the worker exited, and S04's
            # heal execs into the worker: `run S04` used to fail at its first step.
            ("S04-gpu-ecc-drain", "cpu-worker", "exited", ("start", "cpu-worker")),
            # An S01 run killed mid-window leaves the database frozen under S02.
            ("S02-dbd-lock-contention", "mysql", "paused", ("unpause", "mysql")),
        ],
    )
    def test_first_heal_restores_containers_another_run_left_down(
        self, scenario_id: str, service: str, left: str, restore: tuple[str, str]
    ) -> None:
        cluster = FakeCluster()
        cluster.states[service] = left
        run_injection(
            cluster,
            INJECTIONS[scenario_id],
            0,
            sleep=lambda _: None,
            clock=lambda: 0.0,
            log=lambda _: None,
        )
        restored = cluster.calls.index(restore)
        execs = [i for i, c in enumerate(cluster.calls) if c[0] == "exec"]
        assert execs and min(execs) > restored
        assert set(cluster.states.values()) == {"running"}

    def test_observes_for_the_whole_window(self) -> None:
        cluster = FakeCluster()
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        run_injection(
            cluster, INJECTIONS[S01], 900, sleep=sleep, clock=lambda: now[0], log=lambda _: None
        )
        assert now[0] == pytest.approx(900.0)
        assert cluster.states["mysql"] == "running"


class TestScriptWiring:
    """The Python side sends the right script, to the right container."""

    @pytest.mark.parametrize(
        ("scenario_id", "service", "inject_label", "heal_label"),
        [
            ("S02-dbd-lock-contention", "slurmdbd", "s02_lock_inject", "s02_lock_heal"),
            ("S03-controller-rpc-saturation", "slurmctld", "s03_flood_inject", "s03_flood_heal"),
            ("S04-gpu-ecc-drain", "cpu-worker", "gpu_smi_install", "gpu_smi_remove"),
            (
                "S06-state-save-unwritable",
                "slurmctld",
                "s06_state_save_inject",
                "s06_state_save_heal",
            ),
            ("S07-fabric-degraded-collectives", "cpu-worker", "s07_netem_inject", "s07_netem_heal"),
            ("S08-partition-limit-starvation", "slurmctld", "s08_limit_inject", "s08_limit_heal"),
            ("S09-gpu-driver-node-drain", "cpu-worker", "gpu_smi_install", "gpu_smi_remove"),
            (
                "S10-undiagnosable-intermittent-failures",
                "slurmctld",
                "s10_workload_inject",
                "s10_workload_heal",
            ),
        ],
    )
    def test_inject_and_heal_scripts(
        self, scenario_id: str, service: str, inject_label: str, heal_label: str
    ) -> None:
        cluster = FakeCluster()
        INJECTIONS[scenario_id].inject(cluster)
        INJECTIONS[scenario_id].heal(cluster)
        assert cluster.scripts_run() == [
            (service, f"slurmrca-{inject_label}"),
            (service, f"slurmrca-{heal_label}"),
        ]

    def test_failed_script_raises_with_its_output(self) -> None:
        cluster = FakeCluster(handler=lambda s, c: ExecResult(3, "", "lock never appeared"))
        with pytest.raises(ClusterError, match=r"exit 3.*lock never appeared"):
            INJECTIONS["S02-dbd-lock-contention"].inject(cluster)

    def test_timed_out_script_says_so(self) -> None:
        cluster = FakeCluster(handler=lambda s, c: ExecResult(-1, "", "", timed_out=True))
        with pytest.raises(ClusterError, match="timed out"):
            INJECTIONS["S06-state-save-unwritable"].heal(cluster)

    def test_gpu_payloads_are_passed_as_arguments(self) -> None:
        cluster = FakeCluster()
        INJECTIONS["S04-gpu-ecc-drain"].inject(cluster)
        INJECTIONS["S09-gpu-driver-node-drain"].inject(cluster)
        payloads = [c[6] for c in cluster.calls if c[5:6] == ("slurmrca-gpu_smi_install",)]
        assert payloads == [script("fake_nvidia_smi_ecc.sh"), script("fake_nvidia_smi_wedged.sh")]


def _code(name: str) -> str:
    """A script's executable lines, without the comments that explain history."""
    return "\n".join(
        line for line in script(name).splitlines() if not line.lstrip().startswith("#")
    )


class TestNoPatternKills:
    """The first heals killed by pattern and hit themselves or PID 1."""

    @pytest.mark.parametrize("name", sorted(p for p in INJECTION_SCRIPTS if "heal" in p))
    def test_heals_do_not_kill_by_pattern(self, name: str) -> None:
        code = _code(name)
        assert "pkill" not in code and "killall" not in code

    def test_s02_heal_excludes_its_own_connection(self) -> None:
        code = _code("s02_lock_heal.sh")
        assert "id <> CONNECTION_ID()" in code
        assert "KILL CONNECTION" in code

    def test_s02_inject_uses_a_table_write_lock_not_for_update(self) -> None:
        code = _code("s02_lock_inject.sh")
        assert "LOCK TABLES" in code and "WRITE" in code
        assert "FOR UPDATE" not in code
        assert "-uroot" not in code and '-u"$MYSQL_USER"' in code


def test_every_injection_mechanism_matches_its_yaml() -> None:
    """The registry's one-liner and the YAML must describe the same thing."""
    keywords = {
        "S01-accounting-backend-stall": "docker pause",
        "S02-dbd-lock-contention": "LOCK TABLES",
        "S03-controller-rpc-saturation": "squeue",
        "S04-gpu-ecc-drain": "nvidia-smi",
        "S05-undiagnosable-node-reboot": "slurmd.log",
        "S06-state-save-unwritable": "chmod 500",
        "S07-fabric-degraded-collectives": "netem",
        "S08-partition-limit-starvation": "GrpJobs=0",
        "S09-gpu-driver-node-drain": "nvidia-smi",
        "S10-undiagnosable-intermittent-failures": "hash of",
    }
    by_id = {s.id: s for s in SCENARIOS}
    for scenario_id, word in keywords.items():
        assert word in INJECTIONS[scenario_id].mechanism, scenario_id
        assert word in " ".join(by_id[scenario_id].injection.split()), scenario_id
