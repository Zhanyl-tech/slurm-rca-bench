"""Scenario and ground-truth schema.

This file defines what it *means* for a benchmark task to have a correct
answer, and it is the part of the benchmark most likely to be wrong in a way
that flatters an agent. Three decisions carry most of the weight.

**Root cause is a node in a causal chain, not a string.** Free-text answers
force either brittle exact matching or an LLM judge, and an LLM judge grading
LLM agents on reasoning quality is a conflict of interest the scores never
recover from. Answers here are node identifiers from a closed vocabulary
(:data:`NODES`), so scoring is deterministic and reproducible by anyone. The
same rule now applies to proposed actions (:data:`ACTION_VERBS`): they are
``verb:node`` pairs, not sentences, because the first version string-matched
free text and counted every paraphrase as a false positive.

**Partial credit is asymmetric and points downward.** Naming a *consequence*
earns less than naming the cause, and the further down the propagation chain the
answer sits, the less it earns. Naming the component the ticket already names
earns at most :data:`REPORTED_CREDIT_CAP`, because "sacct is hanging, so
slurmdbd is the problem" is reading the report, not diagnosing it. This is what
"RCA depth" has to mean if the number is going to be comparable across
scenarios.

**Undiagnosable scenarios have no full-credit answer at all.** They exist to
punish confident guessing. An agent that abstains scores well; an agent that
names a plausible cause scores zero and takes a calibration penalty. Without
these a benchmark rewards fluent overconfidence, which is the exact failure mode
that makes agents dangerous in an on-call rotation.

The parser is strict on purpose. An audit found that a string where a list was
expected became a tuple of single characters, that ``measured: "false"`` parsed
as true, and that a misspelled key was silently ignored. Each of those let a
malformed scenario pass validation, so unknown keys and wrong types are now
errors, and every membership check lives in :meth:`Scenario.validate` where a
third-party scenario author gets it too, not only in this repo's tests.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar


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


class Status(StrEnum):
    """Whether a scenario can currently be scored.

    A scenario is **blocked** when it is known that its injection cannot
    produce its ground truth on the emulated cluster: a measurement showed it
    (S08), or the cluster or its pinned configuration lacks something the
    chain needs (S07, S09, S10). It stays in the repo so the design and the
    reason are visible, but it is excluded from scoring and from every
    published denominator. Scoring an agent on a fault that never happened
    would make every agent look bad at diagnosing it, and nobody would notice
    why.

    **Runnable** means only "not known to be impossible". It does not mean the
    injection has been seen to work: whether it has is recorded in the
    scenario's ``verification`` block. "Never run" is therefore not a reason
    to block, and a blocked_reason must name what the cluster lacks. Nor is a
    run a condition for unblocking: once what was missing is in place, the
    scenario is runnable and a run is what later marks it measured. The two
    rules used to be mixed, with S10 blocked "until a run shows it working"
    while S02, never run either, was runnable, and every blocked_reason
    listing "a measurement" among what would unblock it.
    """

    RUNNABLE = "runnable"
    BLOCKED = "blocked"


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

#: Verbs a proposed action may use. An action is ``"<verb>:<node>"`` with the
#: node from :data:`NODES` (never :data:`ABSTAIN`). Kept coarse on purpose: the
#: question the metric asks is "would this change have helped", and that is
#: answerable at component granularity without a judge.
ACTION_VERBS: dict[str, str] = {
    "restart": "restart the component's process or service",
    "drain": "take the affected node(s) out of scheduling",
    "resume": "return node(s) to service",
    "repair": "fix the component's state (permissions, limits, hardware, link)",
    "reset": "reset or reload the component (for example a driver reload)",
    "investigate": "gather more evidence on the component without changing it",
}

#: Most credit the component named in the ticket may earn when it is not the
#: cause. Crediting it more would score reading the report as a diagnosis.
REPORTED_CREDIT_CAP = 0.2

#: Abstention on a diagnosable scenario must earn less than this, or refusing
#: to answer becomes a viable strategy.
ABSTAIN_CAP_DIAGNOSABLE = 0.2

_ID_RE = re.compile(r"^S\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
_OFFSET_RE = re.compile(r"^t\+(\d+)s$")


def parse_action(action: str) -> tuple[str, str]:
    """Split ``"verb:node"`` into its parts, or raise ValueError.

    Used for both ground truth and agent answers, so the two can never drift
    into different formats.
    """
    verb, sep, node = action.partition(":")
    if not sep or verb not in ACTION_VERBS or node not in NODES or node == ABSTAIN:
        raise ValueError(
            f"action {action!r} is not '<verb>:<node>' with verb in "
            f"{sorted(ACTION_VERBS)} and node from NODES (not {ABSTAIN!r})"
        )
    return verb, node


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
    #: Telemetry an agent could use to see this hop. For unmeasured scenarios
    #: this is the design, not a record of what the cluster produced.
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
    #: Observations, keyed by the time offset at which they were taken
    #: (``"t+60s"``) or by a label (``"baseline"``, ``"heal+10s"``).
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
    #: The component the ticket itself points at. Naming it, when it is not the
    #: cause, earns at most :data:`REPORTED_CREDIT_CAP`.
    reported_node: str = ABSTAIN
    #: Random UUID unique to this scenario. It lets anyone test whether a model
    #: has seen these files in training (ask it to complete the canary), and it
    #: seeds the opaque task id agents see instead of the scenario id.
    canary: str = ""
    #: Runnable or blocked; see :class:`Status`.
    status: Status = Status.RUNNABLE
    #: Why a blocked scenario cannot run, and what would unblock it.
    blocked_reason: str = ""
    #: Plausible-but-wrong answers the scenario is designed to bait, each with
    #: why it is tempting. Used to report *which* wrong answer an agent gave.
    distractors: dict[str, str] = field(default_factory=dict)
    #: Seconds the harness observes after injection before healing.
    observe_s: int = 120
    #: Free-text note on what the scenario is really testing.
    notes: str = ""
    #: How the fault is emulated, stated plainly. See README limitations.
    injection: str = ""
    #: Evidence the chain was observed. Unverified chains are marked as such.
    verification: Verification = field(default_factory=lambda: Verification(measured=False))
    #: Actions (``verb:node``) that genuinely address the root cause, or
    #: ``None`` when the action ground truth has not been written. ``None``
    #: excludes the scenario from the action metric; an empty tuple means "no
    #: action helps here", so every proposal counts against the agent.
    remediation_helpful: tuple[str, ...] | None = None

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
    def runnable(self) -> bool:
        """True when the scenario counts toward scores and baselines."""
        return self.status is Status.RUNNABLE

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

    @property
    def task_id(self) -> str:
        """Opaque id for the agent-facing view.

        Scenario ids carry words like ``undiagnosable``, and an agent that must
        echo the id back would otherwise be told which tasks to abstain on.
        Derived from the canary so it is stable across runs and machines.
        """
        return "T-" + hashlib.sha256(self.canary.encode()).hexdigest()[:10]

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
        nothing. Each one has a test that feeds it a scenario breaking exactly
        that rule (tests/test_validation.py), so a validator that silently
        stopped checking would fail the build.
        """
        problems: list[str] = []

        if not _ID_RE.match(self.id):
            problems.append(f"id {self.id!r} must look like 'S07-short-kebab-name'")

        for name, value in (
            ("title", self.title),
            ("symptom", self.symptom),
            ("injection", self.injection),
        ):
            if not value.strip():
                problems.append(f"{name} is empty")

        try:
            uuid.UUID(self.canary)
        except ValueError:
            problems.append(f"canary {self.canary!r} is not a UUID")

        if self.status is Status.BLOCKED and not self.blocked_reason.strip():
            problems.append("blocked scenario must say why in blocked_reason")
        if self.status is Status.RUNNABLE and self.blocked_reason.strip():
            problems.append("runnable scenario has a blocked_reason; remove it or mark blocked")

        problems.extend(self._chain_problems())
        problems.extend(self._scoring_problems())

        for node, why in self.distractors.items():
            if node not in NODES:
                problems.append(f"distractor {node!r} is not in NODES")
            if not why.strip():
                problems.append(f"distractor {node!r} has no explanation")
        if self.diagnosable and self.root_cause in self.distractors:
            problems.append(f"root cause {self.root_cause!r} is listed as a distractor")

        if self.remediation_helpful is not None:
            seen_actions: set[str] = set()
            for action in self.remediation_helpful:
                try:
                    parse_action(action)
                except ValueError as exc:
                    problems.append(f"remediation_helpful: {exc}")
                if action in seen_actions:
                    problems.append(f"remediation_helpful lists {action!r} twice")
                seen_actions.add(action)

        problems.extend(self._verification_problems())

        if self.observe_s < self.propagation_s:
            problems.append(
                f"observe_s={self.observe_s} is shorter than the "
                f"{self.propagation_s}s the chain needs to become observable; "
                "the harness would collect before the symptom exists"
            )

        return problems

    def _chain_problems(self) -> list[str]:
        problems: list[str] = []
        if not self.causal_chain:
            return ["causal_chain is empty"]

        # The chain must be ordered by when each hop becomes visible. If it is
        # not, "how many hops from the symptom" is undefined and the difficulty
        # label is meaningless.
        times = [link.observable_at_s for link in self.causal_chain]
        if times != sorted(times):
            problems.append(f"causal_chain not ordered by observable_at_s: {times}")
        if times[0] != 0:
            problems.append(f"causal_chain must start at observable_at_s 0, got {times[0]}")

        for link in self.causal_chain:
            if not link.evidence or not all(e.strip() for e in link.evidence):
                problems.append(f"hop {link.node!r} cites no evidence")

        if self.difficulty is Difficulty.MULTI_LAYER and len(self.causal_chain) < 3:
            problems.append(
                f"multi_layer scenario has only {len(self.causal_chain)} hops; "
                "use single_layer or add the intermediate components"
            )

        # Credit must not rise as the answer moves away from the cause. The
        # first version only checked this for S01, in a test.
        if self.diagnosable:
            credits = [self.credit_for(link.node) for link in self.causal_chain]
            if any(later > earlier for earlier, later in itertools.pairwise(credits)):
                problems.append(
                    f"credit rises down the causal chain {credits}; "
                    "a consequence must never outscore what caused it"
                )
        return problems

    def _scoring_problems(self) -> list[str]:
        problems: list[str] = []
        full = [c.node for c in self.scoring if c.credit >= 1.0]
        if len(full) > 1:
            problems.append(f"more than one full-credit answer: {full}")

        seen: set[str] = set()
        for credit in self.scoring:
            if credit.node in seen:
                problems.append(f"duplicate scoring entry for {credit.node!r}")
            seen.add(credit.node)
            if not credit.rationale.strip():
                problems.append(f"scoring entry {credit.node!r} has no rationale")

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
            if self.credit_for(ABSTAIN) >= ABSTAIN_CAP_DIAGNOSABLE:
                problems.append(
                    f"diagnosable scenario gives abstention {self.credit_for(ABSTAIN)}; "
                    f"it must stay below {ABSTAIN_CAP_DIAGNOSABLE} or refusing to "
                    "answer a solvable task becomes a strategy"
                )

        # The component the ticket names must not earn much unless it really is
        # the cause. The first version checked the chain's last hop instead,
        # which stopped being the reported component when S01 was corrected.
        if self.reported_node not in NODES:
            problems.append(f"reported_node {self.reported_node!r} is not in NODES")
        elif (
            self.reported_node != self.root_cause
            and self.credit_for(self.reported_node) > REPORTED_CREDIT_CAP
        ):
            problems.append(
                f"reported component {self.reported_node!r} earns "
                f"{self.credit_for(self.reported_node)}; the cap is "
                f"{REPORTED_CREDIT_CAP}, because naming it restates the ticket"
            )
        return problems

    def _verification_problems(self) -> list[str]:
        problems: list[str] = []
        verification = self.verification
        if verification.measured and not verification.observations:
            problems.append(
                "verification claims measured=true but records no observations; "
                "an unevidenced claim of evidence is worse than none"
            )
        if verification.measured and not verification.method.strip():
            problems.append("verification claims measured=true but has no method")
        if verification.measured:
            # Every hop after the root (which is the injection itself, true at
            # t+0 by construction) must be timed by a recorded observation.
            # Otherwise "measured" covers timings nobody measured, which is
            # exactly what the first README did for S01's storage hop.
            offsets = {
                int(m.group(1)) for key in verification.observations if (m := _OFFSET_RE.match(key))
            }
            for link in self.causal_chain[1:]:
                if link.observable_at_s not in offsets:
                    problems.append(
                        f"measured scenario times hop {link.node!r} at "
                        f"t+{link.observable_at_s}s but no observation is recorded "
                        "at that offset"
                    )
        return problems


# ── parsing ────────────────────────────────────────────────────────────────

_TOP_REQUIRED = {
    "id",
    "title",
    "family",
    "difficulty",
    "symptom",
    "reported_node",
    "canary",
    "injection",
    "causal_chain",
    "scoring",
}
_TOP_OPTIONAL = {
    "status",
    "blocked_reason",
    "notes",
    "observe_s",
    "verification",
    "distractors",
    "remediation_helpful",
}
_LINK_KEYS = ({"node", "state", "observable_at_s", "evidence"}, set[str]())
_CREDIT_KEYS = ({"node", "credit", "rationale"}, set[str]())
_VERIFICATION_KEYS = ({"measured"}, {"method", "observations", "caveats"})

#: Every key a scenario file may contain, by section. docs/SCENARIO_SCHEMA.md is
#: checked against this in tests so the published schema cannot drift.
SCHEMA_KEYS: dict[str, tuple[set[str], set[str]]] = {
    "scenario": (_TOP_REQUIRED, _TOP_OPTIONAL),
    "causal_chain[]": _LINK_KEYS,
    "scoring[]": _CREDIT_KEYS,
    "verification": _VERIFICATION_KEYS,
}


def _check_keys(raw: dict[str, Any], section: str) -> None:
    required, optional = SCHEMA_KEYS[section]
    unknown = sorted(set(raw) - required - optional)
    if unknown:
        raise ValueError(f"{section}: unknown key(s) {unknown}; check the spelling")
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{section}: missing required key(s) {missing}")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {type(value).__name__}")
    return value


def _integer(value: Any, where: str) -> int:
    # bool is an int subclass; `observe_s: true` must not become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer, got {type(value).__name__}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where} must be a number, got {type(value).__name__}")
    return float(value)


def _boolean(value: Any, where: str) -> bool:
    # `bool("false")` is True. Only a real YAML boolean is accepted.
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false, got {value!r}")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    # A bare string is iterable, and `tuple("abc")` is ('a', 'b', 'c'). That is
    # how a one-line remediation once became 46 one-character actions.
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of strings, got {type(value).__name__}")
    return tuple(_string(item, f"{where}[{i}]") for i, item in enumerate(value))


def _string_map(value: Any, where: str) -> dict[str, str]:
    mapping = _mapping(value, where)
    return {_string(k, f"{where} key"): _string(v, f"{where}[{k!r}]") for k, v in mapping.items()}


def _verification_from(raw: Any) -> Verification:
    """Parse the verification block, defaulting to 'not measured'."""
    if raw is None:
        return Verification(measured=False)
    block = _mapping(raw, "verification")
    _check_keys(block, "verification")
    return Verification(
        measured=_boolean(block["measured"], "verification.measured"),
        method=_string(block.get("method", ""), "verification.method"),
        observations=_string_map(block.get("observations", {}), "verification.observations"),
        caveats=_string(block.get("caveats", ""), "verification.caveats"),
    )


_E = TypeVar("_E", bound=StrEnum)


def _enum(cls: type[_E], value: Any, where: str) -> _E:
    text = _string(value, where)
    try:
        return cls(text)
    except ValueError:
        allowed = [member.value for member in cls]
        raise ValueError(f"{where} {text!r} is not one of {allowed}") from None


def scenario_from_dict(raw: dict[str, Any]) -> Scenario:
    """Build a :class:`Scenario` from parsed YAML, rejecting anything malformed.

    Raises ValueError on unknown keys, missing keys and wrong types. Semantic
    problems (credit ordering, caps, membership) are left to
    :meth:`Scenario.validate` so they are reported together.
    """
    top = _mapping(raw, "scenario")
    _check_keys(top, "scenario")

    chain_raw = top["causal_chain"]
    if not isinstance(chain_raw, list):
        raise ValueError(f"causal_chain must be a list, got {type(chain_raw).__name__}")
    chain: list[ChainLink] = []
    for i, item in enumerate(chain_raw):
        where = f"causal_chain[{i}]"
        link = _mapping(item, where)
        _check_keys(link, "causal_chain[]")
        chain.append(
            ChainLink(
                node=_string(link["node"], f"{where}.node"),
                state=_string(link["state"], f"{where}.state"),
                observable_at_s=_integer(link["observable_at_s"], f"{where}.observable_at_s"),
                evidence=_strings(link["evidence"], f"{where}.evidence"),
            )
        )

    scoring_raw = top["scoring"]
    if not isinstance(scoring_raw, list):
        raise ValueError(f"scoring must be a list, got {type(scoring_raw).__name__}")
    scoring: list[Credit] = []
    for i, item in enumerate(scoring_raw):
        where = f"scoring[{i}]"
        entry = _mapping(item, where)
        _check_keys(entry, "scoring[]")
        scoring.append(
            Credit(
                node=_string(entry["node"], f"{where}.node"),
                credit=_number(entry["credit"], f"{where}.credit"),
                rationale=_string(entry["rationale"], f"{where}.rationale"),
            )
        )

    remediation_raw = top.get("remediation_helpful")
    remediation = (
        None if remediation_raw is None else _strings(remediation_raw, "remediation_helpful")
    )

    return Scenario(
        id=_string(top["id"], "id"),
        title=_string(top["title"], "title"),
        family=_enum(Family, top["family"], "family"),
        difficulty=_enum(Difficulty, top["difficulty"], "difficulty"),
        symptom=_string(top["symptom"], "symptom"),
        causal_chain=tuple(chain),
        scoring=tuple(scoring),
        reported_node=_string(top["reported_node"], "reported_node"),
        canary=_string(top["canary"], "canary"),
        status=_enum(Status, top.get("status", Status.RUNNABLE.value), "status"),
        blocked_reason=_string(top.get("blocked_reason", ""), "blocked_reason"),
        distractors=_string_map(top.get("distractors", {}), "distractors"),
        observe_s=_integer(top.get("observe_s", 120), "observe_s"),
        notes=_string(top.get("notes", ""), "notes"),
        injection=_string(top["injection"], "injection"),
        verification=_verification_from(top.get("verification")),
        remediation_helpful=remediation,
    )
