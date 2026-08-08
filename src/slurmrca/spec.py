"""Scenario and ground-truth schema.

This file defines what it *means* for a benchmark task to have a correct
answer, and it is the part of the benchmark most likely to be wrong in a way
that flatters an agent. Three decisions carry most of the weight.

**Root cause is a node in a causal chain, not a string.** Free-text answers
force either brittle exact matching or an LLM judge, and an LLM judge grading
LLM agents on reasoning quality is a conflict of interest the scores never
recover from. Answers here are node identifiers from a closed vocabulary
(:data:`NODES`), so scoring is deterministic and reproducible by anyone.

**Partial credit is asymmetric and points downward.** Naming a *consequence*
earns less than naming the cause, and the further down the propagation chain the
answer sits, the less it earns. Naming the symptom layer earns nothing, because
"jobs are not starting" is the report, not a diagnosis. This is what "RCA depth"
has to mean if the number is going to be comparable across scenarios.

**Undiagnosable scenarios have no full-credit answer at all.** They exist to
punish confident guessing. An agent that abstains scores well; an agent that
names a plausible cause scores zero and takes a calibration penalty. Without
these a benchmark rewards fluent overconfidence, which is the exact failure mode
that makes agents dangerous in an on-call rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Family(StrEnum):
    """Fault families the benchmark covers.

    Chosen to span the Slurm control plane rather than to be exhaustive: each
    family fails through a different path, so an agent that only knows one
    cannot score well by pattern-matching.
    """

    STORAGE = "storage"
    ACCOUNTING_DB = "accounting_db"
    CONTROLLER = "controller"
    GPU = "gpu"
    FABRIC = "fabric"
    SCHEDULER_CONFIG = "scheduler_config"


class Difficulty(StrEnum):
    """How many hops separate the visible symptom from the cause."""

    #: Symptom and cause are in the same component.
    SINGLE_LAYER = "single_layer"
    #: The cause is several components upstream of the symptom.
    MULTI_LAYER = "multi_layer"
    #: The evidence does not determine the cause. Abstention is correct.
    UNDIAGNOSABLE = "undiagnosable"


#: The closed vocabulary of components an answer may name.
#:
#: Deliberately small and fixed. This is the same graph the agent under test is
#: allowed to be given in configuration C — the hypothesis being that writing it
#: down is worth more than making a model infer it. Keeping the answer space
#: identical to the graph is what makes that comparison meaningful.
NODES: dict[str, str] = {
    "storage.shared_fs": "Shared filesystem backing job scratch and Slurm state",
    "storage.state_save": "slurmctld StateSaveLocation",
    "db.mysql": "Accounting database (MariaDB/MySQL)",
    "slurm.slurmdbd": "Slurm database daemon",
    "slurm.slurmctld": "Slurm controller",
    "slurm.scheduler": "Scheduling loop (main + backfill)",
    "slurm.slurmd": "Compute node daemon",
    "slurm.config": "Slurm configuration (slurm.conf, partitions, limits)",
    "gpu.device": "GPU hardware (XID, ECC, thermal)",
    "gpu.driver": "NVIDIA driver / NVML",
    "fabric.interconnect": "InfiniBand / RoCE fabric",
    "network.control_plane": "Network between control-plane services",
    "unknown": "Cause not determined by available evidence",
}

#: Reserved answer meaning "I cannot tell from this evidence".
ABSTAIN = "unknown"


@dataclass(frozen=True, slots=True)
class ChainLink:
    """One hop in the propagation chain, ordered root cause first."""

    node: str
    #: What is observably true of this node during the incident.
    state: str
    #: Seconds after injection at which this hop becomes observable. The
    #: benchmark's most interesting property: a multi-layer fault's symptom can
    #: appear many minutes after its cause, and an agent that only looks at the
    #: last few minutes of telemetry structurally cannot find it.
    observable_at_s: int
    #: Telemetry an agent could actually use to see this hop.
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.node not in NODES:
            raise ValueError(f"unknown node {self.node!r}; add it to NODES first")
        if self.observable_at_s < 0:
            raise ValueError(f"observable_at_s must be >= 0, got {self.observable_at_s}")


@dataclass(frozen=True, slots=True)
class Credit:
    """Partial credit for naming a particular node as the root cause."""

    node: str
    credit: float
    #: Why this answer earns what it earns. Written for a reader who disagrees.
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.node not in NODES:
            raise ValueError(f"unknown node {self.node!r}")
        if not 0.0 <= self.credit <= 1.0:
            raise ValueError(f"credit must be in [0, 1], got {self.credit}")


@dataclass(frozen=True, slots=True)
class Verification:
    """Evidence that a scenario's causal chain was actually observed.

    Added after the first version of S01 shipped a chain that did not
    reproduce. Its ground truth asserted that a stalled accounting backend halts
    scheduling roughly thirteen minutes later. Measured against a live cluster,
    it does not: the DBD agent queue grows and accounting queries block, but
    slurmctld keeps scheduling and jobs run to completion the whole time.

    The claim was plausible, widely believed, and wrong — which is exactly the
    kind of error that makes a benchmark worse than useless, because every score
    computed against it looks rigorous. So a chain is now either **measured**,
    with the command and observation recorded here, or explicitly marked
    unverified so no one mistakes assertion for evidence.
    """

    #: True when the chain below was observed on a live cluster.
    measured: bool
    #: How it was measured, precisely enough to repeat.
    method: str = ""
    #: Observations, keyed by the time offset at which they were taken.
    observations: dict[str, str] = field(default_factory=dict)
    #: Anything the measurement contradicted or left open.
    caveats: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One benchmark task: an injected fault plus its documented ground truth."""

    id: str
    title: str
    family: Family
    difficulty: Difficulty
    #: The natural-language report an on-call engineer would receive. This is
    #: all the agent is told; everything else it must go and find.
    symptom: str
    causal_chain: tuple[ChainLink, ...]
    scoring: tuple[Credit, ...]
    #: Plausible-but-wrong answers the scenario is designed to bait, each with
    #: why it is tempting. Used to report *which* wrong answer an agent gave.
    distractors: dict[str, str] = field(default_factory=dict)
    #: Seconds the harness observes after injection before collecting.
    observe_s: int = 120
    #: Free-text note on what the scenario is really testing.
    notes: str = ""
    #: How the fault is emulated, stated plainly. See README limitations.
    injection: str = ""
    #: Evidence the chain was observed. Unverified chains are marked as such.
    verification: Verification = field(default_factory=lambda: Verification(measured=False))
    #: Actions that genuinely address the root cause. Anything an agent proposes
    #: that is not listed here counts as an action false positive, so an
    #: under-specified list penalises the benchmark author rather than silently
    #: crediting an agent for plausible-sounding changes.
    remediation_helpful: tuple[str, ...] = ()

    # ── derived ────────────────────────────────────────────────────────────

    @property
    def diagnosable(self) -> bool:
        """True when a real component earns full credit.

        Abstention is excluded deliberately. On an undiagnosable scenario
        ``unknown`` earns full credit because it is the correct answer, but that
        does not make the scenario diagnosable — it makes it honest.
        """
        return any(c.credit >= 1.0 and c.node != ABSTAIN for c in self.scoring)

    @property
    def root_cause(self) -> str:
        """The node earning full credit, or ABSTAIN when undiagnosable."""
        for credit in self.scoring:
            if credit.credit >= 1.0:
                return credit.node
        return ABSTAIN

    @property
    def propagation_s(self) -> int:
        """Seconds from injection until the *last* hop becomes observable."""
        return max((link.observable_at_s for link in self.causal_chain), default=0)

    def credit_for(self, answer: str) -> float:
        """Credit an answer earns. Unlisted answers earn nothing."""
        for credit in self.scoring:
            if credit.node == answer:
                return credit.credit
        return 0.0

    # ── validation ─────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return a list of problems with this scenario. Empty means valid.

        These invariants exist because a benchmark whose ground truth is
        internally inconsistent produces numbers that look precise and mean
        nothing. They are asserted in CI for every scenario in the repo.
        """
        problems: list[str] = []

        if not self.causal_chain:
            problems.append("causal_chain is empty")

        # The chain must be ordered by when each hop becomes visible. If it is
        # not, "how many hops from the symptom" is undefined and the difficulty
        # label is meaningless.
        times = [link.observable_at_s for link in self.causal_chain]
        if times != sorted(times):
            problems.append(f"causal_chain not ordered by observable_at_s: {times}")

        full = [c.node for c in self.scoring if c.credit >= 1.0]
        if len(full) > 1:
            problems.append(f"more than one full-credit answer: {full}")

        if self.difficulty is Difficulty.UNDIAGNOSABLE:
            # Abstention is the *correct answer* here, so it is the one node
            # allowed full credit. Any real component earning full credit means
            # the evidence does determine a cause, and the scenario is
            # mislabelled.
            named = [node for node in full if node != ABSTAIN]
            if named:
                problems.append(
                    f"undiagnosable scenario has a full-credit cause {named}; "
                    "if the evidence determines the cause it is not undiagnosable"
                )
            if not any(c.node == ABSTAIN and c.credit >= 1.0 for c in self.scoring):
                problems.append(
                    "undiagnosable scenario must award full credit to "
                    f"{ABSTAIN!r} so abstention is the scored-correct answer"
                )
        else:
            if not full:
                problems.append("diagnosable scenario has no full-credit answer")
            # The full-credit answer must be where the chain starts, or the
            # chain is not describing the cause it claims to.
            if full and self.causal_chain and full[0] != self.causal_chain[0].node:
                problems.append(
                    f"root cause {full[0]!r} is not the first link in the chain "
                    f"({self.causal_chain[0].node!r})"
                )
            if any(c.node == ABSTAIN and c.credit >= 1.0 for c in self.scoring):
                problems.append(
                    "diagnosable scenario awards full credit to abstention; "
                    "that would reward refusing to answer a solvable task"
                )

        if self.difficulty is Difficulty.MULTI_LAYER and len(self.causal_chain) < 3:
            problems.append(
                f"multi_layer scenario has only {len(self.causal_chain)} hops; "
                "use single_layer or add the intermediate components"
            )

        # Symptom-layer answers must not earn much. The last hop is what the
        # user reported, so crediting it heavily would score reading the ticket.
        if self.causal_chain and self.diagnosable:
            tail = self.causal_chain[-1].node
            if self.credit_for(tail) > 0.2 and tail != self.root_cause:
                problems.append(
                    f"symptom-layer node {tail!r} earns {self.credit_for(tail)}; "
                    "that credits restating the report as a diagnosis"
                )

        if self.verification.measured and not self.verification.observations:
            problems.append(
                "verification claims measured=true but records no observations; "
                "an unevidenced claim of evidence is worse than none"
            )

        seen: set[str] = set()
        for credit in self.scoring:
            if credit.node in seen:
                problems.append(f"duplicate scoring entry for {credit.node!r}")
            seen.add(credit.node)

        if self.observe_s < self.propagation_s:
            problems.append(
                f"observe_s={self.observe_s} is shorter than the "
                f"{self.propagation_s}s the chain needs to become observable; "
                "the harness would collect before the symptom exists"
            )

        return problems


def _verification_from(raw: dict[str, Any] | None) -> Verification:
    """Parse the verification block, defaulting to 'not measured'."""
    if not raw:
        return Verification(measured=False)
    return Verification(
        measured=bool(raw.get("measured", False)),
        method=raw.get("method", ""),
        observations={str(k): str(v) for k, v in (raw.get("observations") or {}).items()},
        caveats=raw.get("caveats", ""),
    )


def scenario_from_dict(raw: dict[str, Any]) -> Scenario:
    """Build a :class:`Scenario` from parsed YAML."""
    try:
        chain = tuple(
            ChainLink(
                node=link["node"],
                state=link["state"],
                observable_at_s=int(link["observable_at_s"]),
                evidence=tuple(link.get("evidence", ())),
            )
            for link in raw["causal_chain"]
        )
        scoring = tuple(
            Credit(
                node=entry["node"],
                credit=float(entry["credit"]),
                rationale=entry.get("rationale", ""),
            )
            for entry in raw["scoring"]
        )
        return Scenario(
            id=raw["id"],
            title=raw["title"],
            family=Family(raw["family"]),
            difficulty=Difficulty(raw["difficulty"]),
            symptom=raw["symptom"],
            causal_chain=chain,
            scoring=scoring,
            distractors=dict(raw.get("distractors", {})),
            observe_s=int(raw.get("observe_s", 120)),
            notes=raw.get("notes", ""),
            injection=raw.get("injection", ""),
            verification=_verification_from(raw.get("verification")),
            remediation_helpful=tuple(raw.get("remediation_helpful", ())),
        )
    except KeyError as exc:
        raise ValueError(f"scenario missing required field: {exc}") from exc
