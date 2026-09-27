#!/bin/sh
# slurm-rca-bench S04/S09 heal: remove the synthetic nvidia-smi.
#
# Deletes the file only when it carries the benchmark's marker. The first heal
# ran `rm -f /usr/local/bin/nvidia-smi` unconditionally, which on a machine with
# a real driver would have deleted the real tool.
set -eu

target="${SLURMRCA_NVIDIA_SMI:-/usr/local/bin/nvidia-smi}"
marker="slurm-rca-bench synthetic nvidia-smi"

if [ ! -e "$target" ]; then
    echo "no nvidia-smi at $target; nothing to heal"
    exit 0
fi

if grep -q "$marker" "$target"; then
    rm -f "$target"
    echo "removed synthetic nvidia-smi from $target"
else
    echo "leaving $target in place: it was not installed by slurm-rca-bench" >&2
fi
