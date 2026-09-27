#!/bin/sh
# slurm-rca-bench S07 heal: remove the netem qdisc the injection added.
#
# Acts only when the injection left a record, and only deletes a root qdisc
# that is netem, so running it on a clean worker changes nothing.
set -eu

dev="${SLURMRCA_S07_DEV:-eth0}"
state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
record="$state_dir/S07.qdisc"

if [ ! -e "$record" ]; then
    echo "no S07 record; nothing to heal"
    exit 0
fi

if ! command -v tc >/dev/null 2>&1; then
    echo "an S07 record exists but tc is missing; cannot verify $dev" >&2
    exit 1
fi

# Assigned first, as the inject does: in `tc ... | awk` the pipeline's status is
# awk's, so a failing tc read as "no netem" and the record was dropped.
current=$(tc qdisc show dev "$dev")
root=$(printf '%s\n' "$current" | awk '/ root /{print $2; exit}')
if [ "$root" = "netem" ]; then
    tc qdisc del dev "$dev" root
    echo "netem removed from $dev"
fi
rm -f "$record"
