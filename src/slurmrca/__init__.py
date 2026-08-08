"""slurm-rca-bench — incident-diagnosis benchmark for HPC schedulers.

Phase 1 provides the scenario schema, the loader that enforces ground-truth
consistency, and the harness that injects a fault into an isolated Slurm cluster
and collects telemetry.
"""

from __future__ import annotations

from slurmrca.loader import ScenarioError, load_all, load_scenario, scenarios_dir
from slurmrca.spec import (
    ABSTAIN,
    NODES,
    ChainLink,
    Credit,
    Difficulty,
    Family,
    Scenario,
)

__version__ = "0.1.0"

__all__ = [
    "ABSTAIN",
    "NODES",
    "ChainLink",
    "Credit",
    "Difficulty",
    "Family",
    "Scenario",
    "ScenarioError",
    "__version__",
    "load_all",
    "load_scenario",
    "scenarios_dir",
]
