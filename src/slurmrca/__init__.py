"""slurm-rca-bench — incident-diagnosis benchmark for the Slurm control plane.

Provides the scenario schema, the loader that enforces ground-truth
consistency, the scoring library with its degenerate baselines, and the harness
that injects a fault into an isolated Slurm cluster and heals it. Collecting a
telemetry bundle for an agent is Phase 3 and does not exist yet.
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
    Status,
)

__version__ = "0.2.0"

__all__ = [
    "ABSTAIN",
    "NODES",
    "ChainLink",
    "Credit",
    "Difficulty",
    "Family",
    "Scenario",
    "ScenarioError",
    "Status",
    "__version__",
    "load_all",
    "load_scenario",
    "scenarios_dir",
]
