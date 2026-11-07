"""Every eval scenario is also a pytest case (outcome + invariant assertions)."""
import os

import pytest

from evals.harness import run_scenario
from evals.scenarios import SCENARIOS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _cwd(monkeypatch):
    monkeypatch.chdir(ROOT)


def test_at_least_20_scenarios():
    assert len(SCENARIOS) >= 20


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(sc):
    r = run_scenario(sc)
    failed = [f"{c['check']}: {c['detail']}" for c in r.checks if not c["ok"]]
    assert r.passed, "\n".join(failed)
