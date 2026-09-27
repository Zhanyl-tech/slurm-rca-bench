#!/bin/sh
# slurm-rca-bench S06 inject: make StateSaveLocation unwritable (mode 500).
#
# Records the directory's mode before changing it, once, so the heal restores
# exactly what was there. The first heal always set 755. A second inject never
# overwrites the record, or it would save 500 as the "original".
set -eu

dir="${SLURMRCA_STATE_SAVE:-/var/lib/slurm}"
state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
record="$state_dir/S06.mode"

if [ ! -d "$dir" ]; then
    echo "$dir is not a directory" >&2
    exit 1
fi

mkdir -p "$state_dir"
if [ ! -e "$record" ]; then
    # GNU stat first (the container), BSD stat as a fallback (the unit tests).
    mode=$(stat -c %a "$dir" 2>/dev/null || stat -f %Lp "$dir")
    case "$mode" in
        [0-7][0-7][0-7] | [0-7][0-7][0-7][0-7]) ;;
        *)
            echo "could not read the mode of $dir (got '$mode'); not changing it" >&2
            exit 1
            ;;
    esac
    printf '%s\n' "$mode" >"$record"
fi

chmod 500 "$dir"
echo "$dir set to 500; original mode $(cat "$record") recorded in $record"
