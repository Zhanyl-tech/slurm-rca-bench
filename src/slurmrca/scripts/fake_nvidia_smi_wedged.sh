#!/bin/sh
# slurm-rca-bench synthetic nvidia-smi (S09: wedged driver, every device fails).
# Driver-level failure: every invocation fails to reach any device, rather than
# one device reporting bad counters. That wholesale-versus-per-device
# distinction is what separates S09 from S04. The message text is written for
# the benchmark and has not been checked against a real driver failure.
echo "Unable to determine the device handle for GPU0000:00:00.0: Unknown Error" >&2
echo "NVML: Driver/library version mismatch" >&2
exit 255
