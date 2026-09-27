# Changelog

The suite version is the package version. A score is only comparable with
another computed against the same ground truth and the same faults, so
`slurm-rca score` prints the suite version and a fingerprint of the scenario
files, the files in `cluster/` and the injection code (`inject.py` and
`scripts/*.sh`) (`slurmrca.loader.fingerprint`).

## 0.2.0 — unreleased (prepared 2026-09-26)

Applies an external audit of 0.1.0. Everything below was done without a Docker
daemon or a Slurm cluster: injection and heal fixes come from reading the code,
the pinned upstream files and official documentation, and are checked with unit
tests, shellcheck and local runs of the scripts against temporary paths. **No
changed injection or heal has been run on the emulated cluster.** The README
marks the three with a recorded run from 0.1.0 (S01, S06, S08) "fixed, not yet
re-run on the emulated cluster" and the other seven "fixed; no recorded run on
the emulated cluster".

### Ground truth (breaking: scores from 0.1.0 are not comparable)

- **S01 full credit moved from `storage.shared_fs` to `db.mysql`.** The
  injection freezes the database container. Nothing shared sits beneath the
  emulated cluster's database (the Slurm containers share volumes for job
  files, configuration and logs; the database's data is its own volume), and
  there is no storage telemetry, so full credit sat on a layer that does not
  exist there. `storage.shared_fs` now earns 0 and is a named distractor.
  The storage hop is gone from the chain, which is now three hops (database,
  slurmdbd, slurmctld). Family changed from `storage` to `accounting_db`.
- **S01 renamed** `S01-storage-stall-scheduling-halt` →
  `S01-accounting-backend-stall`; the old id still resolves everywhere.
- **S01 `slurm.slurmdbd` credit 0.3 → 0.2**, because the component a ticket
  points at is now capped at 0.2 in every scenario (S02 already gave it 0.2).
- **Four scenarios are blocked** and excluded from scoring and baselines: S07
  (netem on the control-plane interface, one worker, no `tc` in the image), S08
  (already measured not to work: `AccountingStorageEnforce` unset), S09 (no
  `HealthCheckProgram` at the pinned commit, so nothing drains), S10 (one
  worker, one populated partition and one user cannot show that failures
  correlate with none of them). Six scenarios are runnable. Blocked means
  known not to produce the ground truth; runnable means only "not known to be
  impossible" (the `Status` docstring and the README say so), and four
  runnable scenarios (S02–S05) have never been seen to take effect. S10 was
  first blocked for "never run", a rule no other scenario followed.
- **Tickets rewritten** where they leaked the answer: S06 no longer quotes the
  controller's log line, S10 no longer says "nobody can find a pattern", S08
  no longer uses reason codes Slurm does not have (`QOSMaxJobs`,
  `AssocMaxJobs`; checked against job_reason_codes.html).
- Evidence and prose that asserted unmeasured things were narrowed or labelled
  (S01's "no error is logged anywhere", now also out of its title, S03/S02
  "storage telemetry is flat" in a cluster with no storage layer, S07's IB/RoCE
  counters, S09's dmesg lines).
  The StateSaveLocation scheduling-halt mechanism is now called a hypothesis.
- S05 and S10 no longer claim the `ruled_out` set is scored (it is not).
- Actions in `remediation_helpful` use the new closed vocabulary.

### Consequence, reported rather than hidden

- The runnable suite is **degenerate**: `always-db.mysql` scores 0.333 over the
  six runnable scenarios (`slurm-rca baselines`, run while preparing this
  release), above the 0.25 threshold. Over the designed ten it would be 0.200
  (`--include-blocked`). The degeneracy test is now a strict xfail, and a new
  test fails if any other constant answer crosses 0.25. Fix: more runnable
  scenarios whose causes lie elsewhere, not retuned weights.
- The README's baseline block changed accordingly (floor 0.240 always-abstain
  over ten → 0.333 always-db.mysql over six runnable).

### Scoring

- An unanswered scenario scores 0.0 depth and Brier 1.0 and is not an
  abstention. Previously an empty submission scored exactly the published floor
  with two "correct abstentions".
- Duplicate answers (100 copies once gave 0.939), answers for unknown ids
  (silently dropped before) and answers for blocked scenarios are errors.
- `ruled_out` is validated against the vocabulary and flags ruling out the true
  cause; it does not affect RCA depth. The docs no longer say it is scored.
- Actions are `verb:node` from a closed vocabulary. Scenarios without action
  ground truth are left out of the action metric instead of counting every
  proposal as a false positive. "Would have hurt" is not distinguished yet.
- Time to hypothesis is labelled self-reported everywhere.
- Baselines compute each constant strategy's best Brier score exactly, and
  `slurm-rca baselines` prints it next to RCA depth (the README quoted these
  numbers before any command printed them). This exposed that a never-right
  answer at confidence 0 scores a perfect 0.000, which the README now states.
- Answers may use the opaque task id, the scenario id or a legacy id.

### Injections and heals (none run on the emulated cluster since)

- Container-side shell moved out of Python strings into
  `src/slurmrca/scripts/*.sh`, shellchecked and syntax-checked under sh, dash
  and bash.
- **S01**: pause only a running database and verify it paused; unpause only a
  paused one (Docker refuses to unpause a running container).
- **S02**: connects as the accounting user from the slurmdbd container (root
  login was refused by the image), takes `LOCK TABLES <cluster>_job_table
  WRITE` instead of `SELECT ... FOR UPDATE` (which InnoDB consistent reads
  ignore), passes SQL on stdin, and fails if the lock is not held. The lock
  wait is bounded (`WAIT n`), the client's PID is recorded, and both statements
  carry the marker, so a failed inject stops its own client and session, and
  the heal finds the client whether it is connecting, waiting for the lock or
  holding it. The heal kills sessions by id; the old `pkill mariadb` also
  matched the database server.
- **S03**: client loops tracked by PID file and checked by name before being
  killed; the old `pkill -f 'while true'` killed the healing shell.
- **S02/S03 process checks** read the whole command line (`ps -ww`) and
  require the marker at its end. ps(1) leaves the width of piped output
  undefined and procps cuts it at `$COLUMNS`; the loops' name and the client's
  database name both sit past column 80. CI failed four tests on it: pytest
  imports `readline`, GNU readline exported `COLUMNS=80` with no terminal on
  stdin, and the tests' own `ps` inherited it (the scripts, run with an
  environment built from `os.environ`, did not, so their checks passed). At
  full width the inject and heal shells' own command lines, which hold the
  whole script, contain the markers as well, so a recorded PID reused by one
  of them passed for a loop or client: the heal signalled it, and the S03
  inject reported "already running" and started nothing. A recorded PID that
  exists but that `ps` prints nothing for (a missing or failing `ps`) now stops
  the inject or heal with an error; before, the heal read it as gone, reported
  success and dropped the record while the loops or client ran on. Tested
  under an 80-column `ps`, a failing `ps` and with such PIDs; not run on the
  emulated cluster.
- **S04/S09**: never overwrite or delete an `nvidia-smi` the benchmark did not
  install.
- **S05**: deletes only `slurmd.log` (not the controller's and dbd's logs on
  the shared volume); the heal finds stopped containers (`ps --all`); the
  five-minute restart is implemented in the run lifecycle; the heal resumes only
  nodes Slurm reports DOWN, not `NodeName=ALL`.
- **S06**: records the original mode and restores it (was always 755).
- **S07**: fails loudly without `tc`, records qdiscs, refuses to replace a
  custom root qdisc, heals only its own netem; a heal whose `tc` fails exits
  non-zero and keeps the record.
- **S08**: uses a benchmark-owned `rcas08` account instead of the parent
  `root` account; heal deletes only `rca*` accounts. Both scripts stop when
  `sacctmgr` fails; the heal used to report "nothing to heal" when it could not
  reach slurmdbd.
- **S10**: failure moved from a never-executed prolog into a submitted workload
  that fails by a hash of the job id (198 of ids 1–10000, computed by the unit
  tests; not observed on a cluster). Heal cancels only its own named jobs.
- `slurm-rca heal --all` runs every heal, idempotently, and reports each
  failure; `make heal` uses it (the old list missed five injections). The
  heals that restore a container (S01, S05) run first, so the worker-side
  heals can reach a worker an interrupted S05 run left stopped.
- `slurm-rca run <id>`: heal, inject, observe (with S05's recovery step), heal.
  The first heal restores containers too (the S01 and S05 heals), so `run S04`
  no longer fails at its first step on a worker an interrupted S05 run left
  stopped. No telemetry bundle yet.

### Harness, packaging and CLI

- The wheel now ships the scenarios and the compose directory; the loader no
  longer walks parent directories (explicit `--scenarios`, then
  `SLURMRCA_SCENARIOS`, then packaged data, then the source checkout).
- `make image` / `cluster/build-image.sh` fetches upstream at the pinned SHA,
  refuses a mismatch or a checkout with local changes, and builds the image
  `make cluster-up` uses as `slurmrca/slurm-docker-cluster:25.11.4-978c3de`,
  labelled with the full commit; Compose never pulls it. An existing image is
  reused only when its label names the pin (the first version reused anything
  tagged `slurm-docker-cluster:25.11.4`, the name upstream's own build uses,
  without fetching or checking). The fetch, SHA and clean-tree checks were run
  against GitHub while preparing this release (`FETCH_ONLY=1`); the
  `docker build` step was not (no daemon).
- `validate` reports every broken file; other commands print messages instead
  of tracebacks. New commands: `score`, `heal`, `run`. `export` defaults to an
  agent view with only an opaque task id and the ticket; `--view scorer` has
  every scenario field, credit table and causal chain included, plus the
  task-id and legacy-id mappings.
- Every scenario has a UUID canary.
- `make smoke` probes sacct, sinfo, sdiag, squeue and sbatch and writes a JSON
  Lines transcript to `evidence/<scenario>/<date>/` (tracked by git, untouched
  by `make clean`; commit it); `make smoke-full` runs the ~16-minute schedule.
  It heals even when the inject itself fails after pausing the database.
- The strict parser rejects unknown keys and wrong types (`"false"` for a
  boolean, a string for a list, `null` chain).
- `make install` uses uv when present, and otherwise checks the Python version.
- Docs: `docker pause` is described as the cgroup freezer, not a stop signal.
  README: no "first public" claim, a Related work section, the system-card
  link, ORCA-bench's LLM-judge and abstract figures, and the harness caveat on
  the OpenRCA trend.

### Tests and CI

- 578 tests pass and 1 is a strict expected failure (the degeneracy above), up
  from 187, measured with `pytest` on Python 3.12.13 and 3.11.15 on 2026-09-27
  while preparing this release. Replacing `Scenario.validate()` with
  `return []` in a scratch copy now fails 34 tests; in 0.1.0 it failed none.
  Coverage is 94.66% with branch coverage on (`pytest --cov`, as `make test`
  runs it); the audit measured 55% line coverage for 0.1.0.
- CI: read-only token, actions pinned by commit SHA, shellcheck, a coverage
  gate at 90%, and a job that builds the wheel and runs it outside the
  checkout. A manual `integration` workflow runs every runnable scenario's
  lifecycle on a Docker runner; it has **never been run**.
- mypy excludes `build/` and `vendor/`, so `mypy .` no longer type-checks the
  virtualenv `make wheel-check` leaves in `build/`.
- New docs: `docs/SCENARIO_SCHEMA.md` (checked against the parser by a test),
  `docs/ADDING_A_SCENARIO.md`, `docs/THREAT_MODEL.md`.

### Not done (see README and the scenario files)

- No re-measurement of anything; no raw transcripts for the 0.1.0
  measurements exist to commit.
- Symptom-only (ticket, no tools) baseline: needs an LLM run.
- Unblocking S07–S10: needs cluster changes (second worker, fabric network,
  `tc`, slurm.conf overlays for enforcement and health checks, a workload
  generator). Measuring them is separate: like S02–S05, an unblocked scenario
  is runnable before any run has shown it working. The blocked reasons used to
  list "a measurement" among what would unblock them.
- Telemetry bundles, measured time-to-hypothesis, per-scenario helpful and
  harmful action sets for S01–S05, scored `ruled_out`, confidence intervals and
  repeated trials, pinning the MariaDB image by digest, external review of the
  weights.

## 0.1.0

Phase 1 and 2: harness, schema, ten scenarios, scoring library, degenerate
baselines. S01's original chain was refuted by measurement and rewritten
before release.
