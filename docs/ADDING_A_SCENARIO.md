# Adding a scenario

The `slurm-rca` commands below assume the venv is active
(`source .venv/bin/activate` after `make install`), or prefix them with
`.venv/bin/`.

The order matters. The failure this benchmark exists to avoid is a scenario
whose ground truth describes something the cluster never does, graded as if it
were true. S01 nearly shipped a scheduling halt that a measurement refuted
before release, and then did ship giving full credit to a storage layer
beneath the database that the emulated cluster does not have.

1. **Write the injection first, then ask what it can produce.** Look at what
   the compose file actually runs (`cluster/docker-compose.yml`) and what the
   pinned upstream configuration enables (`cluster/upstream.lock`; for example
   `Prolog`, `HealthCheckProgram` and `AccountingStorageEnforce` are all unset
   at the pinned commit). Full credit may only go to a component that exists in
   the emulated system and that the injection actually changes.

2. **Put container-side shell in `src/slurmrca/scripts/`.** Read paths from
   environment variables whose defaults are the real container paths, so the
   unit tests can run the same file against a temporary directory. Then:
   - make the heal idempotent: on a clean cluster it changes nothing and
     exits 0;
   - record prior state in the inject and restore that record in the heal,
     rather than guessing the original;
   - never kill, delete or cancel by a pattern the healing process itself
     contains; track what you create (PID file, marker in a file, job name,
     `rca*` account) and touch only that;
   - fail loudly: check the injection took effect and exit non-zero if not;
   - assign a command's output to a variable before testing it:
     `[ -z "$(tool ...)" ]` hides the tool's failure from `set -e`, and a heal
     that cannot reach its daemon then reports "nothing to heal".

   Register the inject and heal in `INJECTIONS` in `src/slurmrca/inject.py`,
   and add a script test in `tests/test_scripts.py` and a wiring test in
   `tests/test_injections.py`. A heal that restores a container belongs in
   `CONTAINER_HEALS`, which `heal --all` runs first and `slurm-rca run` runs
   before the scenario's own first heal. Run `make check`, which includes
   shellcheck.

3. **Write the YAML with `verification.measured: false`** and a plain
   `injection:` text that says what is *not* reproduced. The schema is in
   [SCENARIO_SCHEMA.md](SCENARIO_SCHEMA.md). If the injection cannot produce the
   ground truth, set `status: blocked` with a `blocked_reason` that says what
   the cluster lacks and what would unblock it. "Not yet run" is not a reason
   to block, and a run is not a condition for unblocking: runnable means "not
   known to be impossible", and whether it has been seen to work is what
   `verification` records. Write the ticket the way a user would file it: no
   operator log lines, no conclusions ("nobody can find a pattern").

4. **Measure.** Bring the cluster up (`make image && make cluster-up`), run
   `slurm-rca run <id>` while probing by hand or with a script modelled on
   `slurmrca/smoke.py`, and commit the raw transcript under
   `evidence/<id>/<date>/` (git tracks that directory and `make clean` leaves
   it alone; `results/` is gitignored and deleted by `make clean`). A
   measurement counts only once its transcript is committed there. Only then
   set `measured: true`, record `method` and `observations`, and make every
   hop's `observable_at_s` after the first match an observation offset. The
   validator enforces that last part.

5. **Check the suite still means something.** Run `slurm-rca baselines`. If a
   constant answer reaches 0.25, the suite has become easier to game; the fix
   is another scenario whose cause lies elsewhere, not a retuned weight.

6. **Record it** in `CHANGELOG.md`. A change to ground truth changes the
   fingerprint every score carries, so say why it changed.
