#!/bin/sh
# slurm-rca-bench S03 heal: stop the client loops the injection recorded.
#
# Only PIDs listed in the injection's own record are candidates, and each is
# killed only if its command line still carries the injection's name, so a PID
# reused by an unrelated process is left alone. This shell is never a
# candidate, which is the bug the first heal had.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
pidfile="$state_dir/S03.pids"
name="slurmrca-s03-flood"

is_flood() {
    ps -p "$1" -o args= 2>/dev/null | grep -q "$name"
}

if [ ! -e "$pidfile" ]; then
    echo "no S03 flood recorded; nothing to heal"
    exit 0
fi

while IFS= read -r pid; do
    case "$pid" in
        '' | *[!0-9]*)
            echo "ignoring malformed PID entry: $pid" >&2
            continue
            ;;
    esac
    if is_flood "$pid"; then
        kill "$pid" 2>/dev/null || true
    fi
done <"$pidfile"

# The loops exit as soon as the signal lands; an in-flight squeue finishes on
# its own. Confirm rather than assume.
tries=0
while :; do
    remaining=""
    while IFS= read -r pid; do
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        if is_flood "$pid"; then
            remaining="$remaining $pid"
        fi
    done <"$pidfile"
    if [ -z "$remaining" ]; then
        break
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge 10 ]; then
        echo "flood loops still running after SIGTERM:$remaining" >&2
        exit 1
    fi
    sleep 1
done

rm -f "$pidfile"
echo "S03 flood stopped"
