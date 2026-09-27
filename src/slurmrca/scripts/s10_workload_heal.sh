#!/bin/sh
# slurm-rca-bench S10 heal: cancel the benchmark's workload and remove its script.
#
# Cancels only jobs named slurmrca-s10 owned by the current user, and only when
# squeue shows some, so it never issues an unfiltered scancel. The job script
# is removed only if it carries the S10 marker.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
jobdir="${SLURMRCA_JOBDIR:-/data}"
name="slurmrca-s10"
script="$jobdir/slurmrca-s10-job.sh"
user=$(id -un)

if command -v squeue >/dev/null 2>&1; then
    pending=$(squeue --noheader --name="$name" --user="$user" --format=%i)
    if [ -n "$pending" ]; then
        scancel --name="$name" --user="$user"
        echo "cancelled jobs named $name"
    fi
elif [ -s "$state_dir/S10.jobs" ]; then
    echo "an S10 record exists but squeue is missing; cannot cancel the workload" >&2
    exit 1
fi

if [ -e "$script" ]; then
    if grep -q "slurm-rca-bench S10 workload job" "$script"; then
        rm -f "$script"
    else
        echo "leaving $script in place: it was not written by slurm-rca-bench" >&2
    fi
fi
rm -f "$state_dir/S10.jobs" "$state_dir/S10.jobs.tmp"
echo "S10 workload healed"
