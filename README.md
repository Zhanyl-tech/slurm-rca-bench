# slurm-rca-bench

**An incident-diagnosis benchmark for the Slurm control plane.** Ground truth is
measured where it says so and marked unverified where it is not.

> **Phase 2 of 6.** Ten scenarios designed: six are runnable and four are
> blocked. The scoring library and degenerate baselines are done. An audit in
> September 2026 found that the flagship gave full credit to a layer the
> emulated cluster does not have, that several injections could not produce
> their ground truth, and that the scorer rewarded empty submissions. The fixes
> are in 0.2.0 ([CHANGELOG](CHANGELOG.md)). **No changed injection has been
> run on the emulated cluster since.** Three scenarios (S01, S06, S08) have
> recorded runs from before the fixes; the other seven have no recorded run at
> all. The leaderboard is empty until an agent has actually been measured.

---

## The thesis

Published RCA scores for LLM agents are poor — but they are improving fast with
model generation, and quoting a stale number would misrepresent both facts.
Dated and attributed:

| Benchmark | Model | Result | When |
|---|---|---|---|
| [OpenRCA](https://github.com/microsoft/OpenRCA) (335 failures) | Claude 3.5 + RCA-agent | 11.34% | ICLR'25 |
| OpenRCA | Claude Opus 4.5 | 90/335 = **27%** | [Opus 4.6 system card](https://www.anthropic.com/claude-opus-4-6-system-card) |
| OpenRCA | Claude Opus 4.6 | 117/335 = **35%** | Feb 2026 |
| [ORCA-bench](https://arxiv.org/abs/2607.28545) (884 incident tasks)¹ | Claude Sonnet 4.6 | 30.6% strict RCA accuracy | Jul 2026 |
| ORCA-bench¹ | GPT-5.5 | **48.8%** RCA depth (partial credit) | Jul 2026 |

¹ ORCA-bench's scores are assigned by an LLM judge (GPT-5.4, per the paper
body; the abstract reports that humans independently re-scored its judgements
with Cohen's κ_w = 0.90). That is the kind of grading this benchmark avoids;
see [How ground truth works](#how-ground-truth-works). The figures above are
from the paper body for its 884 incident tasks. The abstract's headline is
different: 1,079 RCA tasks, best RCA accuracy 25.3% on Medium and 10.0% on Hard.

**Read the trend, not the floor.** OpenRCA went 11% → 27% → 35% across three
Claude generations on an unchanged task set. The 11% comes from the OpenRCA
paper's RCA-agent scaffold and the 27% and 35% from Anthropic's system card,
whose evaluation harness need not be the same, so each step may mix a model
change with a harness change; how comparable they are is unverified. Anyone
citing "LLM agents get 11% at RCA" in 2026 is still quoting a
model two generations old.

Even so, the best current strict numbers leave most incidents misdiagnosed: 35%
on OpenRCA and 30.6% on ORCA-bench's incident tasks (48.8% with partial
credit). That is nowhere near dependable for an on-call rotation.

**The structural reason is the graph.** Cloud dependency graphs are huge,
dynamic, and undocumented; a microservice mesh changes shape between deploys and
nobody has written down how failures propagate through it. An agent has to infer
the topology and diagnose the fault at the same time.

**HPC control planes are the opposite.** The Slurm dependency chain

```
shared filesystem → accounting DB → slurmdbd → slurmctld → scheduling
```

is small, static, and documented: `slurm.conf` and `slurmdbd.conf` name the
Slurm edges (slurmctld to slurmdbd, slurmdbd to its database), the storage
under the database is set in the database's own mount configuration, and none
of it changes between deploys. It has maybe a dozen components. (In the
emulated cluster in this repo nothing shared sits beneath the database; see
S01.)

> **Hypothesis:** a meaningful share of what general agents lose on RCA is lost
> to graph inference, not reasoning. Write the graph down and some of it returns.

**The generational trend is the control that makes this worth testing.** If
scores climb this fast on raw capability alone, then "does an explicit graph
help *beyond* scaling?" is exactly the question that needs an ablation rather
than an opinion — which is what
[cluster-sre-agent](https://github.com/Zhanyl-tech/cluster-sre-agent) is, five
configurations differing by one variable at a time.

**A negative result is a result.** If configuration C — the one with the graph —
does not beat configuration B, that is the finding and it gets published as the
finding. The scenarios and their ground truth were written before any agent
existed, and the ground truth is version-controlled and test-enforced so it
cannot quietly move toward a conclusion. When it does move, the reason is in
[CHANGELOG.md](CHANGELOG.md) and every score carries a fingerprint of the ground
truth it was computed against.

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
records the observations that back it — and, since 0.2.0, that every hop it
times after the first is timed by one of those observations.

These probes were run by hand and their raw transcript was not committed; the
observations above are transcribed from it. `make smoke` now writes a JSON Lines
transcript with raw outputs, timestamps and the suite fingerprint to
`evidence/<scenario>/<date>/`, which git tracks and `make clean` leaves alone.
A measurement counts only once its transcript is committed there. (The first
version wrote to `results/`, which is gitignored and which `make clean`
deletes.)

This is the failure mode that makes a benchmark worse than useless: had it
shipped unverified, every score computed against it would have looked rigorous
and been graded against a fiction. One candidate mechanism for a
storage-induced scheduling halt remains: slurmctld might block on state writes
if the filesystem under `StateSaveLocation` stalls. That is a hypothesis. S06
measured a write *failure* there, not a stall, so it is untested here, and no
scenario claims it.

## The second finding: the flagship still credited a layer that was not there

The first correction kept `storage.shared_fs` as S01's full-credit answer: a
stalled shared filesystem beneath the database. An audit in September 2026
pointed out that nothing shared sits beneath the emulated cluster's database
and that there is no storage telemetry. The Slurm containers do share Docker
volumes (job files at `/data`, configuration and logs), but the database keeps
its data on a volume of its own and no injection slows any of them. The
injection freezes the database container itself. An agent that
read this cluster faithfully could only ever find a frozen database, and would
have scored 0.45 for the correct reading while full credit sat on a layer that
does not exist here.

S01 now gives full credit to `db.mysql`, gives `storage.shared_fs` zero (as a
named distractor), and has a new id, `S01-accounting-backend-stall` (the old id
named the halt the measurement disproved; it still resolves). The
storage-beneath-the-database story needs an injection that really slows the
storage under MariaDB, and a measurement of it, before any scenario may claim
it. This correction has a cost, reported in [Baselines](#baselines--the-number-to-ask-for-first).

## Limitations — read these before the results

Stated here rather than at the bottom, because they bound every number this
benchmark will ever produce.

- **The faults are emulated, not real.** The accounting stall is a
  `docker pause` of the database container, which on Linux uses the cgroup
  freezer; nothing beneath the database is emulated. GPU faults are a synthetic
  `nvidia-smi`. Each scenario documents its own mechanism in an `injection:`
  field, and a test asserts that field is non-empty. What is reproduced is the
  *shape* of the failure — unbounded waits rather than errors, per-device
  counters confined to one node — not the physics.
- **Four of ten scenarios are blocked.** Blocked means it is known that the
  injection cannot produce its ground truth on this cluster, by measurement or
  because the cluster lacks something the chain needs (details below). They
  are excluded from scoring and from every published denominator.
- **Runnable means "not known to be impossible", not "seen to work".** Of the
  six runnable scenarios, four (S02, S03, S04, S05) have injections that have
  never been seen to take effect on the cluster; only S01 and S06 have measured
  chains. Every runnable number below is computed over all six.
- **The injection fixes are unrun.** Every injection or heal changed in 0.2.0
  was fixed by reading the code and upstream sources, then checked with unit
  tests, shellcheck and local runs of the scripts against temporary paths. None
  has been run against the emulated cluster since.
- **There is no workload generator.** Tickets that describe failing jobs (S04,
  S05) are not reproduced in that respect; only the diagnostic signal is.
- **There are no real GPUs.** The cluster is CPU-only Docker with one worker.
  All GPU telemetry in the GPU family is synthetic by construction.
- **Ten scenarios is not enough to rank models, and only six are runnable.**
  Phase 3 takes it to 15–20. Until then, treat any number as a smoke test, not
  a measurement.
- **Single Slurm version.** Everything is pinned to Slurm 25.11.4. Behaviour on
  other versions is untested.
- **Ground truth is one engineer's judgement.** The partial-credit weights
  encode opinions about what counts as diagnostic progress. Every weight carries
  a written rationale so you can disagree with a specific argument rather than a
  number, and a test requires that rationale to exist. Nobody else has reviewed
  the weights yet.
- **Emulation makes some scenarios cleaner than production.** Real incidents
  arrive with concurrent unrelated noise. These do not, yet. That biases scores
  *upward* relative to real on-call.

## Scenarios

Ten scenarios are designed, across all six families. Six are runnable and four
are blocked. The runnable scenarios cover 4 families: `accounting_db`,
`controller`, `gpu`, `storage`. `measured` means the causal chain was observed
on a live cluster, not asserted. In the last column, "not yet re-run" marks the
three scenarios with a recorded run from before the 0.2.0 fixes; "no recorded
run" marks the seven that have none.

| ID | Title | Family | Difficulty | Status | Measured | Injection / heal in 0.2.0 |
|---|---|---|---|---|---|---|
| **S01** | Accounting goes dark; `sacct` hangs without an error | accounting_db | **multi-layer** | runnable | ✅ hops 2–3 | fixed, not yet re-run on the emulated cluster |
| S02 | `sacct` hangs but jobs keep running | accounting_db | single-layer | runnable | — | fixed; no recorded run on the emulated cluster |
| S03 | Everything is slow and the controller is pinned | controller | single-layer | runnable | — | fixed; no recorded run on the emulated cluster |
| S04 | One node keeps failing jobs, others are fine | gpu | single-layer | runnable | — | fixed; no recorded run on the emulated cluster |
| **S05** | A node rebooted and the evidence is gone | controller | **undiagnosable** | runnable | — | fixed; no recorded run on the emulated cluster |
| S06 | Every new submission rejected, running jobs fine | storage | single-layer | runnable | ✅ | fixed, not yet re-run on the emulated cluster |
| S07 | Multi-node jobs crawl, single-node jobs fine | fabric | single-layer | ⛔ blocked | — | fixed; no recorded run on the emulated cluster |
| S08 | One account starves while the cluster idles | scheduler_config | single-layer | ⛔ blocked (measured not to work) | — | fixed, not yet re-run on the emulated cluster |
| S09 | A node drains itself repeatedly | gpu | **multi-layer** | ⛔ blocked | — | fixed; no recorded run on the emulated cluster |
| **S10** | 1 job in 50 fails | scheduler_config | **undiagnosable** | ⛔ blocked | — | fixed; no recorded run on the emulated cluster |

What changed in each injection or heal is in its scenario file and in the
CHANGELOG. In short: S02's lock used a login the image refuses and a lock type
that does not block reads, and its heal killed the database server; S03's heal
killed its own shell; S05 deleted the controller's log and its heal could not
find the stopped node; S06's heal guessed the original mode; S04/S09's heal
deleted any `nvidia-smi`; S08 limited the parent of every account.

Why the four are blocked:

- **S07** — netem lands on the worker's only interface, which carries
  control-plane traffic, so the evidence points at `network.control_plane`, not
  the fabric; there is one worker, so "multi-node jobs" cannot exist; the pinned
  image installs no `tc`.
- **S08** — measured not to work: the cluster runs with
  `AccountingStorageEnforce` unset, so association limits are recorded and never
  enforced. Recorded rather than dropped — a scenario whose injection silently
  does nothing would score every agent on a fault that never happened.
- **S09** — the drain loop needs a node health check, and the pinned
  `slurm.conf` has `#HealthCheckProgram=` commented out, so nothing ever calls
  the synthetic `nvidia-smi`.
- **S10** — the chain's evidence is that failures correlate with no node,
  partition, job size or user, which one worker, one populated partition and
  one submitting user cannot show. (The original prolog also never ran:
  `#Prolog=` is commented out, and a failing Prolog would drain the node,
  which is diagnosable. The replacement puts the failure in a submitted
  workload.)

### S01 is the flagship

```
db.mysql          frozen_no_error          t+0s     ← the cause (the injected pause)
  └─ slurmdbd     waiting_on_db            t+5s     ← what the user reports
     └─ slurmctld dbd_agent_queue_growing  t+840s   ← the only positive signal
```

The report says "`sacct` hangs." The difficulty is the *shape* of the failure,
not depth alone: a broken database returns an error, while a frozen one returns
nothing, so every caller just waits. `sacct` returned no error at any point in
the measurement. Whether any log line mentions the stall was not recorded, so
the claim that a grep for `ERROR` finds nothing is untested; a grep-for-ERROR
baseline agent is the planned way to test it.

The one positive signal found is a queue depth climbing in `sdiag` output
nobody is watching: 1 at t+60s, and 6 at t+180s in one run and t+840s in the
other. Hops two and three are timed by those recorded observations; hop one is
the injection itself, true at t+0 by construction.

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
S10 is the second undiagnosable scenario; it is blocked until the cluster can
show that its failures correlate with nothing (see above).

## Baselines — the number to ask for first

A benchmark reporting only "the agent scored 0.52" is not reporting anything,
because the reader cannot know that answering the same node to every task
scores 0.50. `slurm-rca baselines` computes that floor from the ground truth
alone, over the runnable scenarios (depth, then the best Brier score a constant
answer can reach; see below):

```
  degenerate baselines over 6 runnable scenarios

   depth   brier  strategy
   0.333   0.222  always-db.mysql               <- floor
   0.208   0.139  always-abstain
   0.200   0.139  always-slurm.slurmctld
   0.167   0.139  always-gpu.device
   0.167   0.139  always-storage.state_save
   0.101       —  uniform-random
   0.067   0.000  always-slurm.slurmdbd
   0.067   0.000  always-storage.shared_fs
```

**The runnable suite is degenerate right now.** Answering `db.mysql` to
everything scores 0.333 without reading a log line, above the 0.25 threshold a
test enforces. S01 and S02 both have the database as their root cause, and
there are only six runnable scenarios for them to be two of.

These numbers come from the ground truth alone, and so does their caveat: four
of the six runnable scenarios (S02–S05) have injections never seen to take
effect on the cluster (runnable means "not known to be impossible"). If a
first run shows one of them cannot produce its ground truth, it becomes
blocked and every number here changes.

This is also how the suite audits itself. At five scenarios, `always-db.mysql`
scored **0.290** — because S01 and S02 both rewarded it. Adding scenarios whose
causes lie elsewhere dropped it to **0.145** over ten. It is back up for two
reasons, both corrections: S01's full credit moved to `db.mysql`, the only
layer its injection touches, and the four blocked scenarios left the
denominator. Over the designed ten it would be 0.200
(`slurm-rca baselines --include-blocked`), but blocked scenarios are not
scored, so 0.333 is the honest number. The degeneracy test is now a strict
expected failure, which turns into a real failure the moment the suite is
fixed, and a second test fails the build if any *other* constant answer crosses
0.25. The fix for a degenerate suite is more runnable scenarios whose causes lie
elsewhere, never retuned weights.

**An agent that does not clear the floor has demonstrated fluency, not
diagnosis.** Clearing it is necessary, not sufficient. The floor ignores the
ticket, and several tickets describe their failure closely, so a model given
only the ticket and no tools may clear it comfortably. That symptom-only
baseline needs an LLM run and has not been measured; until it has, compare
agents with each other and not only with this floor.

The other metrics have floors too, computed the same way:

- **Calibration.** The `brier` column above is each constant answer's best
  Brier score: at the constant confidence p equal to the share of runnable
  scenarios it gets right, the score is p(1-p) (`slurmrca.scoring.baselines`;
  `slurm-rca baselines --top 20` lists every strategy). Brier has a trivial
  floor: a constant answer that is never right, stated at confidence 0, scores
  a perfect 0.000 (for example `always-network.control_plane`). Brier measures
  whether confidence matches correctness, not correctness, so it is only
  reported next to RCA depth. `always-abstain` (best confidence 1/6) scores
  Brier 0.139, and the depth floor `always-db.mysql` (best confidence 1/3)
  scores Brier 0.222.
- **Action false positives.** Proposing nothing scores 0.000.

`always-abstain` scoring meaningfully (0.208) is intentional: refusing to
answer is *safe*, so it should score something, and it must stay beatable or
the benchmark rewards refusing to work.

## How ground truth works

Answers are **node identifiers from a closed vocabulary**, not free text. That
choice is deliberate: free-text answers force either brittle string matching or
an LLM judge, and an LLM judge grading LLM agents on reasoning quality is a
conflict of interest the scores never recover from. Grading here is a dictionary
lookup — deterministic, and reproducible by anyone. Proposed actions follow the
same rule since 0.2.0: `verb:node`, for example `repair:storage.state_save`.

Partial credit **never rises down the propagation chain**:

| Answer on S01 | Credit | Why |
|---|---|---|
| `db.mysql` | **1.00** | The injected fault: frozen, neither answering nor erroring |
| `slurm.slurmdbd` | 0.20 | The component the ticket points at; capped at 0.2 |
| `slurm.slurmctld` | 0.10 | Found the queue in `sdiag`, then stopped |
| `slurm.scheduler` | **0.00** | Asserts the halt the measurement disproved |
| `storage.shared_fs` | **0.00** | The folk-model answer; nothing shared sits beneath this cluster's database |
| `unknown` (abstain) | 0.05 | Honest, but this scenario *is* solvable |

The invariants are enforced by `Scenario.validate()`, so a third-party scenario
gets them too, and `tests/test_validation.py` breaks each one on purpose to
prove it is checked:

- credit never rises from cause to consequence along the chain, in every
  scenario
- the component the ticket points at earns at most 0.2 unless it is the cause
- undiagnosable scenarios have **no** full-credit cause and *must* award full
  credit to abstention
- diagnosable scenarios must **not** make abstention viable (< 0.2)
- the causal chain is ordered by observability, starts at t+0, and every hop
  cites evidence; in a measured scenario every hop after the first must be
  timed by a recorded observation. For unmeasured scenarios the evidence is the
  design, and nothing yet checks that an agent could actually retrieve it
- the observation window covers the full propagation delay — otherwise a
  scenario is unsolvable for reasons unrelated to the agent
- every credit weight and every distractor carries a written rationale
- unknown keys and wrong types in a scenario file are errors; the schema is in
  [docs/SCENARIO_SCHEMA.md](docs/SCENARIO_SCHEMA.md)

Adding a scenario: [docs/ADDING_A_SCENARIO.md](docs/ADDING_A_SCENARIO.md).

## Scoring your own agent

```bash
source .venv/bin/activate                # after `make install`; puts slurm-rca on PATH
slurm-rca export > tasks.json            # agent view: opaque task ids and tickets only
# run your agent; write one JSON answer per line
slurm-rca score answers.jsonl --config my-agent
```

The agent view carries no scenario ids, titles, difficulty or "diagnosable"
flags: the first export included them, and two of the ids contain the word
"undiagnosable". `slurm-rca export --view scorer` has every field of every
scenario (credit table, causal chain, distractors, action ground truth,
verification) plus the task-id and legacy-id mappings: the ground truth needed
to compute RCA depth, Brier and the action metric without this package. The report `slurm-rca score` prints ends with the suite
version and a fingerprint of the scenario files, the cluster files and the
injection code, so a score can be matched to exactly what produced it.

```json
{
  "scenario_id": "T-8018cbddf2",
  "root_cause": "db.mysql",
  "confidence": 0.72,
  "ruled_out": ["slurm.config", "gpu.device"],
  "time_to_hypothesis_s": 94,
  "actions_proposed": ["investigate:db.mysql"]
}
```

`scenario_id` may be the opaque task id, the scenario id or a legacy id. The
scorer is strict, because each leniency was measured to distort the headline:
an answer for an unknown or blocked scenario, or a second answer for the same
scenario, is an error; an unanswered runnable scenario scores 0.0 with Brier
1.0 and does not count as an abstention (it used to, which let an empty
submission tie the floor). `ruled_out` is validated against the vocabulary and
recorded, and flags an agent that rules out the true cause, but it does **not**
change RCA depth yet: scoring it needs a per-scenario list of what the evidence
really eliminates, and that ground truth has not been written.

Four metrics, of which only the first is common in existing benchmarks:

| Metric | What it measures |
|---|---|
| **RCA depth** | Partial credit per the table above, averaged over the runnable scenarios |
| **Time to hypothesis** | Self-reported by the agent. Nothing measures it yet, so it is shown, never ranked on |
| **Calibration** | Brier score of stated confidence against exact correctness — this is what S05 is for |
| **Action false-positive rate** | Proposed `verb:node` actions not in the scenario's helpful set, over scenarios that define one. Only S06 does among the runnable six; "would have hurt" is not distinguished yet |

## Leaderboard

*Empty. Nothing has been measured yet.*

| Config | RCA depth | Time-to-hyp (self-reported) | Calibration | Action FP |
|---|---|---|---|---|
| — | — | — | — | — |

Populated in Phase 4, when the ablation first runs end to end.

## Install and run

```bash
make install       # venv + dev tools; needs Python >= 3.11 (uses uv when present)
make check         # ruff, mypy --strict, pytest with coverage, shellcheck, validate
make wheel-check   # CI's other job: build the wheel, run it outside the checkout (needs uv)
make list          # the scenario table
source .venv/bin/activate   # `make install` puts slurm-rca in .venv/bin, not on PATH
```

`make` targets call `.venv/bin/slurm-rca` directly; the bare `slurm-rca`
commands in this README and in `docs/` assume the venv is activated.

Validation and scoring need only Python. The live cluster needs Docker, and
none of the 0.2.0 injection changes has been run this way yet:

```bash
make image         # fetch upstream at the pinned commit, verify SHA and clean tree, build
make cluster-up
slurm-rca run S01-accounting-backend-stall   # heal, inject, observe, heal; no bundle yet
make heal          # slurm-rca heal --all: every heal, idempotent
make smoke         # S01 probes; transcript in evidence/S01-.../<date>/ (make smoke-full: ~16 min)
make cluster-down
```

Commit the transcript `make smoke` writes: a measurement counts only once its
raw transcript is in `evidence/`.

Before an agent gets tool access to this cluster, read
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): the worker runs privileged and the
harness executes commands as root.

## Related work

- [OpenRCA](https://github.com/microsoft/OpenRCA): 335 failure cases from
  telecom, banking and online-marketplace systems, with logs, metrics and
  traces. Enterprise software, not a scheduler control plane.
- [ORCA-bench](https://arxiv.org/abs/2607.28545): on-call RCA for language
  model agents, scored by an LLM judge.
- [AIOpsLab](https://arxiv.org/abs/2501.06706),
  [ITBench](https://arxiv.org/abs/2502.05352),
  [SREGym](https://arxiv.org/abs/2605.07161) and
  [Cloud-OpsBench](https://arxiv.org/abs/2603.00468): SRE and root-cause
  benchmarks for agents on cloud-native, microservice or Kubernetes systems.
- [NIKA](https://arxiv.org/abs/2512.16381) and
  [FaulT-Bench](https://arxiv.org/abs/2608.27021): network troubleshooting for
  LLM agents. FaulT-Bench includes tickets that report no real fault or blame
  the wrong component, which is close in spirit to the undiagnosable scenarios
  here.
- [AOBench](https://github.com/MSKazemi/aobench): agents operating HPC systems,
  with mock Slurm tools and an AIOps task category. Whether it includes graded
  root-cause tasks was not established.

What this benchmark adds is the combination: Slurm control-plane incidents on a
live emulated cluster, ground truth graded against a closed vocabulary without a
judge, undiagnosable tasks that reward abstention, and a measured-or-unverified
label on every causal chain. A search in September 2026 found no other public
benchmark with that combination. A search is not proof, so this README makes no
claim to be first.

## Built on

[giovtorres/slurm-docker-cluster](https://github.com/giovtorres/slurm-docker-cluster),
pinned at commit `978c3de` (Slurm 25.11.4) — a real `slurmctld` / `slurmdbd` /
MariaDB control plane in Docker. That project is not vendored here: `make image`
(`cluster/build-image.sh`) fetches it at the pinned commit into `vendor/`,
checks the SHA and that the checkout has no local changes, and builds the image
the compose file uses, tagged `slurmrca/slurm-docker-cluster:25.11.4-978c3de`
and labelled with the full commit. An existing image with that tag is reused
only if its label names the pinned commit, and Compose never pulls it
(`pull_policy: never`). The first version tagged it `slurm-docker-cluster:25.11.4`,
the name upstream's own build uses for any commit, and reused any image with
that tag without fetching or checking anything. The `docker build` step has not
been run (no Docker daemon was available while preparing 0.2.0). See
[NOTICE](NOTICE).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| **1** | **Harness + scenario schema + 5 scenarios** | **done** |
| **2** | **Scenarios to 10, scoring library, degenerate baselines** | **done; 4 of 10 blocked** |
| 3 | Unblock S07–S10; scenarios to 15–20; telemetry bundles; agent configs A (raw LLM) and B (+ read-only MCP) | next |
| 4 | Dependency graph, config C, **first real comparison** | |
| 5 | Configs D (multi-agent) and E (calibrated abstention) | |
| 6 | Guardrails, blast-radius policy, verifier agent | |

## License

MIT
