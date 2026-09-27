#!/bin/sh
# slurm-rca-bench S07 inject: add latency and loss to the worker's interface.
#
# S07 is BLOCKED (see its scenario file): this interface carries control-plane
# traffic, the compose file runs one worker, and the pinned upstream image
# installs no iproute-tc. The script stays correct and safe so the scenario can
# be unblocked without rewriting it: it fails loudly when tc is missing, records
# the interface's qdiscs before touching them, and refuses to run over a root
# qdisc it could not restore.
set -eu

dev="${SLURMRCA_S07_DEV:-eth0}"
state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
record="$state_dir/S07.qdisc"

if ! command -v tc >/dev/null 2>&1; then
    echo "tc not found: the pinned upstream image installs no iproute-tc" >&2
    exit 1
fi

mkdir -p "$state_dir"
if [ -e "$record" ]; then
    echo "S07 netem already recorded in $record; nothing to do"
    exit 0
fi

current=$(tc qdisc show dev "$dev")
root=$(printf '%s\n' "$current" | awk '/ root /{print $2; exit}')
# The heal restores by deleting the root qdisc, which returns the interface to
# the kernel default. That is only a restore if the default is what was there.
case "$root" in
    '' | noqueue | pfifo_fast | fq_codel | mq | fq) ;;
    *)
        echo "refusing: $dev has root qdisc '$root', which the heal could not restore" >&2
        exit 1
        ;;
esac

printf '%s\n' "$current" >"$record"
tc qdisc replace dev "$dev" root netem delay 40ms 10ms loss 3%
echo "netem applied on $dev"
