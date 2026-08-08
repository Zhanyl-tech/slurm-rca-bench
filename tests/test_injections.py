"""Every scenario must have a reversible, documented injection.

These run without Docker. They check the *registry* is complete and coherent —
the live behaviour of each injection is exercised by `make smoke`, which needs a
cluster.
"""

from __future__ import annotations

import pytest

from slurmrca.inject import INJECTIONS, injection_for
from slurmrca.loader import load_all
from slurmrca.spec import Scenario

SCENARIOS = load_all()
IDS = [s.id for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_every_scenario_has_an_injection(scenario: Scenario) -> None:
    assert scenario.id in INJECTIONS, f"{scenario.id} has no injection implementation"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_every_injection_has_a_heal(scenario: Scenario) -> None:
    # An injection without a heal poisons every scenario that runs after it.
    injection = injection_for(scenario.id)
    assert callable(injection.inject)
    assert callable(injection.heal)
    assert injection.inject is not injection.heal


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_mechanism_is_described(scenario: Scenario) -> None:
    assert injection_for(scenario.id).mechanism.strip()


def test_no_orphan_injections() -> None:
    """An injection with no scenario is dead code that will drift."""
    assert set(INJECTIONS) == set(IDS)


def test_unknown_scenario_raises() -> None:
    with pytest.raises(Exception, match="no injection implemented"):
        injection_for("S99-does-not-exist")
