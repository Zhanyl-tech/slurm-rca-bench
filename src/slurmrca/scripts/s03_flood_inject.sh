#!/bin/sh
# slurm-rca-bench S03 inject: saturate slurmctld with squeue/sinfo client loops.
#
# Each loop runs under a recognisable name ($0 = slurmrca-s03-flood) and its PID
# is written to a file. The heal kills exactly those PIDs, after checking each
# one still carries that name. The first heal used `pkill -f 'while true'`,
# which also matched the healing shell's own command line, so it killed itself
# before its remaining cleanup ran.
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
    ps -p "$1" -o args= 2>/dev/null | grep -q "$name"
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
