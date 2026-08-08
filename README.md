# slurm-rca-bench

**The first public incident-diagnosis benchmark for HPC schedulers.** Every
existing root-cause-analysis benchmark for LLM agents is cloud microservices.

> **Phase 1 of 6.** Scenario schema, ground-truth invariants, and five
> scenarios. The scoring library lands in Phase 2 and the leaderboard is empty
> until an agent has actually been measured. Published early on purpose.

---

## The thesis

Published RCA scores for LLM agents are poor, and consistently so:

| Benchmark | Best reported result | Scale |
|---|---|---|
| [ORCA-bench](https://arxiv.org/abs/2607.28545) | **48.8%** RCA depth (GPT-5.5) | 884 incident tasks |
| [OpenRCA](https://github.com/microsoft/OpenRCA) (ICLR'25) | **11.34%** accuracy (Claude 3.5 + RCA-agent) | 335 failures, 68 GB telemetry |

On ORCA-bench, the stricter *RCA accuracy* metric — naming every root cause
correctly — tops out at 30.6%. These are strong models on carefully-built
benchmarks, so the shortfall is not a prompting problem.

**The structural reason is the graph.** Cloud dependency graphs are huge,
dynamic, and undocumented; a microservice mesh changes shape between deploys and
nobody has written down how failures propagate through it. An agent has to infer
the topology and diagnose the fault at the same time.

**HPC control planes are the opposite.** The Slurm dependency chain

```
shared filesystem → accounting DB → slurmdbd → slurmctld → scheduling
```

is small, static, documented, and *the same at every Slurm site on earth*. It
does not change between deploys. It has maybe a dozen components.

> **Hypothesis:** most of the accuracy general agents lose on RCA is lost to
> graph inference, not to reasoning. Write the dependency graph down and much of
> it comes back.

This repo is the benchmark that tests that claim. The agent measured against it
lives in [cluster-sre-agent](https://github.com/Zhanyl-tech/cluster-sre-agent),
built as five ablatable configurations so the graph's contribution can be
isolated rather than asserted.

**A negative result is a result.** If configuration C — the one with the graph —
does not beat configuration B, that is the finding and it gets published as the
finding. The scenarios and their ground truth were written before any agent
existed, and the ground truth is version-controlled and test-enforced so it
cannot quietly move toward a conclusion.

## The first finding is about the benchmark itself

Before any agent was measured, writing S01 produced a result worth reporting.

The scenario originally encoded a chain every Slurm operator will recognise:
a stalled storage layer blocks the accounting database, slurmdbd backs up,
slurmctld's queue fills, and **scheduling halts about thirteen minutes later**
with jobs stuck in PENDING. It is a plausible, widely-believed folk model.

Measured against a live cluster, **it does not happen.**

```
t+0s      baseline — sacct ok, node idle, DBD Agent queue size 0
t+5s      sacct BLOCKS (no return, no error); sinfo still fine
t+60s     DBD Agent queue size 1
t+840s    DBD Agent queue size 6 and climbing; sbatch still accepted
t+900s    submitted jobs reach the node and run; queue drains
heal+10s  sacct returns; queued records flush — jobs 4 and 5 both COMPLETED
```

slurmctld keeps scheduling with a degraded accounting path. Job start latency
degrades to tens of seconds, which is real — but nothing halts, and two jobs
submitted *during* the stall ran to completion.

So S01 was rewritten to claim only what was observed, and every scenario now
carries a `verification:` block that is either **measured**, with the commands
and observations recorded, or explicitly marked unverified. A test enforces that
the flagship chain is measured, and that any scenario claiming `measured: true`
records the observations that back it.

This is the failure mode that makes a benchmark worse than useless: had it
shipped unverified, every score computed against it would have looked rigorous
and been graded against a fiction. A genuine storage-induced scheduling halt is
achievable — via `StateSaveLocation`, where slurmctld really does block on state
writes — and that is a Phase 2 scenario, not a claim this one gets to make.

## Limitations — read these before the results

Stated here rather than at the bottom, because they bound every number this
benchmark will ever produce.

- **The faults are emulated, not real.** Storage stalls are emulated by
  suspending the database process; GPU faults by a synthetic `nvidia-smi`. Each
  scenario documents its own mechanism in an `injection:` field, and a test
  asserts that field is non-empty. What is reproduced is the *shape* of the
  failure — unbounded waits rather than errors, per-device counters confined to
  one node — not the physics.
- **There are no real GPUs.** The cluster is CPU-only Docker. All GPU telemetry
  in the GPU family is synthetic by construction.
- **Five scenarios is not enough to rank models.** Phase 2 takes it to 15–20.
  Until then, treat any number as a smoke test, not a measurement.
- **Single Slurm version.** Everything is pinned to Slurm 25.11.4. Behaviour on
  other versions is untested.
- **Ground truth is one engineer's judgement.** The partial-credit weights
  encode opinions about what counts as diagnostic progress. Every weight carries
  a written rationale so you can disagree with a specific argument rather than a
  number, and a test requires that rationale to exist.
- **Emulation makes some scenarios cleaner than production.** Real incidents
  arrive with concurrent unrelated noise. These do not, yet. That biases scores
  *upward* relative to real on-call.

## Scenarios

Five in Phase 1, across four of the six planned families.

| ID | Title | Family | Difficulty | Hops | Cause → symptom |
|---|---|---|---|---|---|
| **S01** | Accounting goes dark, no error anywhere | storage | **multi-layer** | 4 | **14 min** |
| S02 | `sacct` hangs but jobs keep running | accounting_db | single-layer | 2 | 30 s |
| S03 | Everything is slow and the controller is pinned | controller | single-layer | 1 | 0 s |
| S04 | One node keeps failing jobs, others are fine | gpu | single-layer | 1 | 0 s |
| **S05** | A node rebooted and the evidence is gone | controller | **undiagnosable** | 1 | — |

### S01 is the flagship

```
storage.shared_fs  slow_ops                  t+0s     ← the cause
  └─ db.mysql      unresponsive_no_error     t+5s
     └─ slurmdbd   waiting_on_db             t+60s    ← what the user reports
        └─ slurmctld dbd_agent_queue_growing t+840s   ← the only positive signal
```

The report says "`sacct` hangs." The difficulty is the *shape* of the failure,
not depth alone: a broken database returns an error a grep can find, while a
stalled one returns nothing, so every caller just waits. **No component in the
path logs an error at any point.** An agent that greps for `ERROR` finds nothing
and concludes the cluster is healthy.

The single positive signal anywhere is a queue depth climbing slowly in `sdiag`
output nobody is watching — and it only becomes obvious fourteen minutes in,
long after the report. Every timing above is measured, not asserted.

### S05 is the honesty scenario

A node vanished for five minutes at 02:14 and came back. Local logs did not
survive the reboot. The surviving evidence is consistent with a kernel panic, a
power event, or a hardware fault, with nothing to separate them.

**There is no full-credit cause.** Abstention scores 1.0. Naming any of the three
plausible causes scores **zero — including the one a human would consider most
likely.** Correct localisation ("it was that node") earns 0.15, because it is
real work and still not a diagnosis.

Without scenarios like this, a benchmark rewards fluent overconfidence, which is
precisely the failure mode that makes an agent dangerous on an on-call rotation.
At minimum one more undiagnosable scenario lands in Phase 2.

## How ground truth works

Answers are **node identifiers from a closed vocabulary**, not free text. That
choice is deliberate: free-text answers force either brittle string matching or
an LLM judge, and an LLM judge grading LLM agents on reasoning quality is a
conflict of interest the scores never recover from. Grading here is a dictionary
lookup — deterministic, and reproducible by anyone.

Partial credit **decreases monotonically down the propagation chain**:

| Answer on S01 | Credit | Why |
|---|---|---|
| `storage.shared_fs` | **1.00** | The cause |
| `db.mysql` | 0.45 | One hop down; defensible, but restarting MySQL fixes nothing |
| `slurm.slurmdbd` | 0.30 | Right transmission path, names a victim |
| `slurm.slurmctld` | 0.15 | Traced one hop back from the report, then stopped |
| `slurm.scheduler` | **0.00** | This is the report restated |
| `unknown` (abstain) | 0.05 | Honest, but this scenario *is* solvable |

The invariants are enforced by tests, not by good intentions
(`tests/test_scenarios.py`):

- credit falls monotonically from cause to symptom, and the symptom earns zero
- undiagnosable scenarios have **no** full-credit cause and *must* award full
  credit to abstention
- diagnosable scenarios must **not** make abstention viable (< 0.2)
- the causal chain is ordered by observability, starts at t+0, and every hop
  cites evidence an agent could actually retrieve
- the observation window covers the full propagation delay — otherwise a
  scenario is unsolvable for reasons unrelated to the agent
- every credit weight and every distractor carries a written rationale

## Scoring your own agent

The scoring library ships in Phase 2. The stable contract is the answer format:

```json
{
  "scenario_id": "S01-storage-stall-scheduling-halt",
  "root_cause": "storage.shared_fs",
  "confidence": 0.72,
  "ruled_out": ["slurm.config", "gpu.device"],
  "time_to_hypothesis_s": 94,
  "actions_proposed": []
}
```

Four metrics, of which only the first is common in existing benchmarks:

| Metric | What it measures |
|---|---|
| **RCA depth** | Partial credit per the table above |
| **Time to hypothesis** | Wall-clock to first committed answer |
| **Calibration** | Whether stated confidence tracks correctness — this is what S05 is for |
| **Action false-positive rate** | Proposed changes that would not have helped, or would have hurt |

## Leaderboard

*Empty. Nothing has been measured yet.*

| Config | RCA depth | Time-to-hyp | Calibration | Action FP |
|---|---|---|---|---|
| — | — | — | — | — |

Populated in Phase 4, when the ablation first runs end to end.

## Install and run

```bash
make install
make validate      # ground-truth invariants across all scenarios
make list          # the scenario table
make test
```

Requires Docker for live fault injection. Validation and scoring need only
Python.

## Built on

[giovtorres/slurm-docker-cluster](https://github.com/giovtorres/slurm-docker-cluster),
pinned at commit `978c3de` (Slurm 25.11.4) — a real `slurmctld` / `slurmdbd` /
MariaDB control plane in Docker. That project is not vendored here; the harness
clones it at the pinned commit. See [NOTICE](NOTICE).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| **1** | **Harness + scenario schema + 5 scenarios** | **done** |
| 2 | Scenarios to 15–20, scoring library | next |
| 3 | Agent configs A (raw LLM) and B (+ read-only MCP) | |
| 4 | Dependency graph, config C, **first real comparison** | |
| 5 | Configs D (multi-agent) and E (calibrated abstention) | |
| 6 | Guardrails, blast-radius policy, verifier agent | |

## License

MIT
