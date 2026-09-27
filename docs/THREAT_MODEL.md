# Threat model for agent access

Phase 3 gives an LLM agent tool access to the benchmark cluster. This page
states what that agent could reach today and what has to be true before it is
given any tools. It is written before Phase 3 on purpose: the harness as it
stands was built for a human operator, not an untrusted caller.

## What exists today

- **The worker runs `privileged: true`** (`cluster/docker-compose.yml`),
  because slurmd sets its own hostname and manages cgroups. A process that can
  execute inside that container has far more of the host kernel's capabilities
  than a normal container.
- **The harness drives everything through `docker compose exec` as root**, and
  through `docker pause`, `docker kill` and `docker start`. Anything that can
  call the harness, or the Docker CLI it uses, can do all of that.
- **Credentials are hard-coded.** The accounting database user's password is
  the literal `password`, and MariaDB's root password is random (generated when
  the data volume is first initialised) and unused by the harness. Acceptable
  only because the cluster is a local, isolated Compose project with no
  published ports; it must never be exposed.
- **Isolation is by Compose project name** (`slurmrca`), so injections cannot
  target a developer's other clusters by accident. That is a guard against
  mistakes, not against a hostile caller.

## Rules before an agent gets tools

1. **No Docker socket and no harness access for the agent.** An agent that can
   reach Docker, or exec into the privileged worker, effectively has host-level
   capability.
2. **Tools are an allowlist of read-only commands** (`sinfo`, `squeue`,
   `sacct`, `sdiag`, `scontrol show ...`, and reading named log files), run
   from a separate unprivileged container that has the Slurm client tools and
   munge credentials but no Docker socket and no write access to the cluster's
   volumes.
3. **Arguments are validated, not interpolated into a shell.** Commands are
   built as argument lists from the allowlist; free text from the model never
   reaches `sh -c`.
4. **Every tool call is logged with a timestamp.** The same log is what a
   measured time-to-hypothesis would be computed from.
5. **Proposed remediation is scored, never executed.** Answers carry
   `actions_proposed` as `verb:node` strings; nothing in the harness acts on
   them.

## Open questions

- Whether slurmd works in this image with specific capabilities instead of
  `privileged: true` is unverified. Worth trying before Phase 3.
- Whether any read-only Slurm command can still change state or leak other
  scenarios' ground truth has not been reviewed.
