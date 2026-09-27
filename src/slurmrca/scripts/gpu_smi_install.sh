#!/bin/sh
# slurm-rca-bench S04/S09 inject: install a synthetic nvidia-smi on the worker.
#
# $1 is the script to install. It must carry the benchmark's marker, and an
# existing nvidia-smi without that marker is never replaced: the heal deletes
# only files that carry the marker, so a real binary overwritten here could
# never be restored. The cluster has no GPUs; this file is the only GPU
# evidence the scenario has.
set -eu

target="${SLURMRCA_NVIDIA_SMI:-/usr/local/bin/nvidia-smi}"
marker="slurm-rca-bench synthetic nvidia-smi"
payload="${1:?usage: gpu_smi_install.sh <script-to-install>}"

case "$payload" in
    *"$marker"*) ;;
    *)
        echo "payload does not carry the marker '$marker'; refusing" >&2
        exit 1
        ;;
esac

if [ -e "$target" ] && ! grep -q "$marker" "$target"; then
    echo "refusing to replace $target: it was not installed by slurm-rca-bench" >&2
    exit 1
fi

mkdir -p "$(dirname "$target")"
printf '%s\n' "$payload" >"$target.slurmrca-tmp"
chmod 755 "$target.slurmrca-tmp"
mv -f "$target.slurmrca-tmp" "$target"
echo "installed synthetic nvidia-smi at $target"
