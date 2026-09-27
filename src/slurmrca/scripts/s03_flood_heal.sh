#!/bin/sh
# slurm-rca-bench S03 heal: stop the client loops the injection recorded.
#
# Only PIDs listed in the injection's own record are candidates, and each is
# killed only if its command line still ends with the injection's name, so a
# PID reused by an unrelated process, or by this shell, is left alone. Killing
# its own shell is the bug the first heal had.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
pidfile="$state_dir/S03.pids"
name="slurmrca-s03-flood"

is_flood() {
    # Same check as the inject's: full width (-ww), and the name must end the
    # command line, because this shell's own command line contains it too.
    case "$1" in
        *[1-9]*) ;;
        *) return 1 ;; # PID 0 is no process, and `kill -0 0` tests this shell's group
    esac
    args=$(ps -ww -p "$1" -o args= 2>/dev/null || true)
    if [ -z "$args" ] && kill -0 "$1" 2>/dev/null; then
        # ps prints a line for every process that exists, zombies included, so
        # nothing for one that does means ps itself failed (missing, or it
        # rejected the options). Reading that as "gone" would report the
        # loops stopped, and drop the record, while they run on.
        echo "ps could not read PID $1, which exists; not treating it as gone" >&2
        exit 1
    fi
    case "$args" in
        *" $name") return 0 ;;
        *) return 1 ;;
    esac
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
