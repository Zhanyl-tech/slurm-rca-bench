#!/bin/sh
# slurm-rca-bench S05 inject, step 1 of 2: delete the worker's slurmd log.
#
# Only slurmd.log (SlurmdLogFile in the pinned upstream slurm.conf).
# /var/log/slurm is one volume shared by slurmdbd, slurmctld and the worker,
# and the first version ran `rm -f /var/log/slurm/*.log` from the worker, which
# also deleted slurmctld.log, slurmdbd.log and jobcomp.log: the controller's
# "not responding" lines are the evidence the scenario says survives.
set -eu

log="${SLURMRCA_SLURMD_LOG:-/var/log/slurm/slurmd.log}"

case "$log" in
    *'*'* | *'?'* | *'['*)
        echo "refusing a glob pattern: $log" >&2
        exit 1
        ;;
esac
if [ "$(basename "$log")" != "slurmd.log" ]; then
    echo "refusing to delete $log: only slurmd.log is in scope" >&2
    exit 1
fi

rm -f -- "$log"
echo "removed $log"
