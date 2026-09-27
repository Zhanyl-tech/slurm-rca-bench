#!/bin/sh
# slurm-rca-bench S10 workload job.
# Fails for about one job in fifty. Which jobs fail is decided by a hash of the
# Slurm job id, so the failure set is the same whenever the same job ids run,
# and it correlates with nothing about the node, the user or the time of day.
: "${SLURM_JOB_ID:?not running under Slurm}"
sleep "${SLURMRCA_S10_WORK_S:-2}"
h=$(( (SLURM_JOB_ID * 2654435761) % 4294967296 ))
if [ $(( (h / 65536) % 50 )) -eq 0 ]; then
    echo "worker: exited unexpectedly" >&2
    exit 1
fi
exit 0
