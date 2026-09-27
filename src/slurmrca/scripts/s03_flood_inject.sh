#!/bin/sh
# slurm-rca-bench S03 inject: saturate slurmctld with squeue/sinfo client loops.
#
# Each loop runs under a recognisable name ($0 = slurmrca-s03-flood) and its PID
# is written to a file. The heal kills exactly those PIDs, after checking each
# one's command line still ends with that name. The first heal used
# `pkill -f 'while true'`, which also matched the healing shell's own command
# line, so it killed itself before its remaining cleanup ran.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
clients="${SLURMRCA_S03_CLIENTS:-8}"
pidfile="$state_dir/S03.pids"
name="slurmrca-s03-flood"

case "$clients" in
    '' | *[!0-9]*)
        echo "SLURMRCA_S03_CLIENTS must be a whole number" >&2
        exit 1
        ;;
esac
for tool in squeue sinfo; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "$tool not found on PATH" >&2
        exit 1
    fi
done

is_flood() {
    # True while PID $1 is one of the loops: its command line ends with their
    # name. -ww because the name sits past column 80 and ps(1) leaves the
    # width of piped output undefined (procps cuts it at $COLUMNS). Ends with,
    # not contains: at full width this shell's own command line, which is the
    # whole script, contains the name too, so a recorded PID now held by an
    # inject or heal shell would otherwise pass for a loop.
    case "$1" in
        *[1-9]*) ;;
        *) return 1 ;; # PID 0 is no process, and `kill -0 0` tests this shell's group
    esac
    args=$(ps -ww -p "$1" -o args= 2>/dev/null || true)
    if [ -z "$args" ] && kill -0 "$1" 2>/dev/null; then
        # ps prints a line for every process that exists, zombies included, so
        # nothing for one that does means ps itself failed (missing, or it
        # rejected the options). Reading that as "gone" would drop the record
        # of loops still running and start more, or report none started.
        echo "ps could not read PID $1, which exists; not treating it as gone" >&2
        exit 1
    fi
    case "$args" in
        *" $name") return 0 ;;
        *) return 1 ;;
    esac
}

mkdir -p "$state_dir"
if [ -e "$pidfile" ]; then
    while IFS= read -r pid; do
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        if is_flood "$pid"; then
            echo "S03 flood already running (PID $pid); nothing to do"
            exit 0
        fi
    done <"$pidfile"
    # A record whose processes are all gone (the container restarted) is stale.
    rm -f "$pidfile"
fi

: >"$pidfile.tmp"
i=0
while [ "$i" -lt "$clients" ]; do
    nohup sh -c 'while :; do squeue >/dev/null 2>&1; sinfo >/dev/null 2>&1; done' \
        "$name" >/dev/null 2>&1 &
    echo "$!" >>"$pidfile.tmp"
    i=$((i + 1))
done
mv -f "$pidfile.tmp" "$pidfile"

sleep 1
alive=0
while IFS= read -r pid; do
    if is_flood "$pid"; then
        alive=$((alive + 1))
    fi
done <"$pidfile"
if [ "$alive" -eq 0 ] && [ "$clients" -gt 0 ]; then
    echo "no RPC client loop survived startup" >&2
    exit 1
fi
echo "started $alive of $clients RPC client loops"
