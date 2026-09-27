#!/bin/sh
# slurm-rca-bench S10 inject: submit a workload whose jobs fail about 2% of the time.
#
# S10 is BLOCKED until this has been measured (see its scenario file). The
# first version wrote a flaky script to /etc/slurm/prolog.d, but the pinned
# slurm.conf has `#Prolog=` commented out, so it never ran; and had it run,
# Slurm drains a node whose Prolog fails (prolog_epilog.html), which is a loud,
# attributable signal rather than an undiagnosable one. The failure therefore
# lives in the workload itself, as it would for a flaky application.
#
# $1 is the job script. It is written to the job directory shared with the
# worker, and every job carries the name slurmrca-s10 so the heal can cancel
# exactly these jobs and nothing else.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
jobdir="${SLURMRCA_JOBDIR:-/data}"
jobs="${SLURMRCA_S10_JOBS:-300}"
name="slurmrca-s10"
script="$jobdir/slurmrca-s10-job.sh"
record="$state_dir/S10.jobs"
payload="${1:?usage: s10_workload_inject.sh <job-script>}"

case "$jobs" in
    '' | *[!0-9]*)
        echo "SLURMRCA_S10_JOBS must be a whole number" >&2
        exit 1
        ;;
esac
case "$payload" in
    *"slurm-rca-bench S10 workload job"*) ;;
    *)
        echo "job script does not carry the S10 marker; refusing" >&2
        exit 1
        ;;
esac
if ! command -v sbatch >/dev/null 2>&1; then
    echo "sbatch not found on PATH" >&2
    exit 1
fi

mkdir -p "$state_dir"
if [ -s "$record" ]; then
    echo "S10 workload already submitted (see $record); nothing to do"
    exit 0
fi

printf '%s\n' "$payload" >"$script.slurmrca-tmp"
chmod 755 "$script.slurmrca-tmp"
mv -f "$script.slurmrca-tmp" "$script"

: >"$record.tmp"
i=0
while [ "$i" -lt "$jobs" ]; do
    sbatch --parsable --job-name="$name" --output=/dev/null --chdir="$jobdir" "$script" >>"$record.tmp"
    i=$((i + 1))
done
mv -f "$record.tmp" "$record"
echo "submitted $jobs jobs named $name"
