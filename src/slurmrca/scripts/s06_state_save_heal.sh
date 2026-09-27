#!/bin/sh
# slurm-rca-bench S06 heal: restore StateSaveLocation's recorded mode.
#
# With no record there is nothing this heal knows how to undo, so it changes
# nothing. A corrupt record is an error rather than a guess.
set -eu

dir="${SLURMRCA_STATE_SAVE:-/var/lib/slurm}"
state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
record="$state_dir/S06.mode"

if [ ! -e "$record" ]; then
    echo "no S06 record; nothing to restore"
    exit 0
fi

mode=$(cat "$record")
case "$mode" in
    [0-7][0-7][0-7] | [0-7][0-7][0-7][0-7]) ;;
    *)
        echo "record $record holds '$mode', not an octal mode; leaving $dir unchanged" >&2
        exit 1
        ;;
esac

chmod "$mode" "$dir"
rm -f "$record"
echo "$dir restored to $mode"
