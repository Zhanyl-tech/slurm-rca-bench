#!/bin/sh
# slurm-rca-bench synthetic nvidia-smi (S04: uncorrectable ECC on one device).
# The cluster has no GPUs; this reports a failing card so the scenario has
# something for an agent to find. It prints the same report whatever arguments
# it is given, and the layout approximates `nvidia-smi -q -d ECC` without having
# been checked against a real device. Documented as synthetic in the README.
cat <<'OUT'
GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-00000000-0000-0000-0000-000000000000)
    Ecc Errors
        Volatile
            SRAM Uncorrectable            : 4
            DRAM Uncorrectable            : 2
        Aggregate
            SRAM Uncorrectable            : 4
    Remapped Rows
        Remapping Failure Occurred        : Yes
        Pending                           : No
OUT
