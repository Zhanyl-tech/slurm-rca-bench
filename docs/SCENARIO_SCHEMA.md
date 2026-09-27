# Scenario schema

One scenario is one file, `scenarios/<id>/scenario.yaml`, whose `id` must equal
its directory name. The parser (`slurmrca.spec.scenario_from_dict`) is strict:
unknown keys, missing required keys and wrong types are errors, not warnings.
`tests/test_validation.py` checks that every key the parser accepts is listed
on this page, so the page cannot drift from the code.

Validate a directory of your own with (after `make install` and
`source .venv/bin/activate`, or with `.venv/bin/slurm-rca`):

```bash
slurm-rca --scenarios path/to/scenarios validate
```

## Top level

| Key | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | string | `S<two digits>-<kebab-case>`; must match the directory name |
| `title` | yes | string | Short human title. Not shown to agents |
| `family` | yes | enum | `storage`, `accounting_db`, `controller`, `gpu`, `fabric`, `scheduler_config` |
| `difficulty` | yes | enum | `single_layer`, `multi_layer` (at least 3 hops), `undiagnosable` |
| `status` | no | enum | `runnable` (default) or `blocked`. Blocked scenarios are not scored. Runnable means only "not known to be impossible" |
| `blocked_reason` | if blocked | string | What the cluster lacks, or what a measurement showed, such that the injection cannot produce the ground truth; and what would unblock it. "Not yet run" is not a reason, and a run is not an unblocking condition |
| `reported_node` | yes | node | The component the ticket points at. Earns at most 0.2 unless it is the cause |
| `canary` | yes | UUID string | Unique per scenario. Contamination canary; also seeds the opaque task id |
| `symptom` | yes | string | The ticket, as a user would file it. The only text an agent is given |
| `notes` | no | string | What the scenario is really testing |
| `injection` | yes | string | How the fault is emulated, plainly, including anything not reproduced |
| `observe_s` | no | integer | Seconds to observe after injecting (default 120). Must cover the chain |
| `verification` | no | mapping | See below. Absent means not measured |
| `causal_chain` | yes | list | Hops, root cause first. See below |
| `scoring` | yes | list | Credit per answer. See below |
| `distractors` | no | mapping node → string | Tempting wrong answers and why they tempt |
| `remediation_helpful` | no | list of actions | Actions that help. Absent: the scenario is left out of the action metric. Empty list: no action helps, so every proposal is a false positive |

A *node* is one of the identifiers in `slurmrca.spec.NODES`. An *action* is
`<verb>:<node>` with a verb from `slurmrca.spec.ACTION_VERBS` (`restart`,
`drain`, `resume`, `repair`, `reset`, `investigate`) and a node other than
`unknown`.

## `causal_chain[]`

| Key | Type | Meaning |
|---|---|---|
| `node` | node | The component at this hop |
| `state` | string | What is observably true of it |
| `observable_at_s` | integer | Seconds after injection when the hop becomes visible. The first hop is 0; the list is ordered by this |
| `evidence` | list of strings | What an agent could look at to see this hop. At least one entry |

## `scoring[]`

| Key | Type | Meaning |
|---|---|---|
| `node` | node | The answer being credited |
| `credit` | number in [0, 1] | What that answer earns. Unlisted answers earn 0 |
| `rationale` | string | Why, written for a reader who disagrees. Required and non-empty |

## `verification`

| Key | Type | Meaning |
|---|---|---|
| `measured` | boolean | True only if the chain was observed on a live cluster. The string `"false"` is rejected, not read as true |
| `method` | string | How, precisely enough to repeat. Required when measured |
| `observations` | mapping string → string | Keyed by offset (`"t+60s"`) or label (`"baseline"`). When measured, every hop after the first must have an observation at its `observable_at_s` |
| `caveats` | string | What the measurement contradicted or left open |

## Rules `validate()` enforces

Each has a test that breaks it on purpose (`tests/test_validation.py`):

- exactly one full-credit answer; it is the first hop, unless the scenario is
  undiagnosable, in which case the only full-credit answer is `unknown`
- abstention earns less than 0.2 on a diagnosable scenario
- credit never rises down the causal chain
- the reported component earns at most 0.2 unless it is the root cause
- the chain starts at 0, is ordered, and every hop cites evidence
- `multi_layer` scenarios have at least three hops
- `observe_s` covers the last hop
- every scoring entry has a rationale; no node is scored twice
- distractors are nodes, have an explanation, and are not the root cause
- remediation entries are valid actions and not repeated
- a blocked scenario says why; a runnable one has no `blocked_reason`
- `measured: true` needs a method, observations, and an observation for every
  timed hop after the first
- the canary is a UUID; ids, canaries and task ids are unique across the suite
