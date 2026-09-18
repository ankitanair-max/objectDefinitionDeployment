"""Shared pytest fixtures for the deployment-pipeline tests.

Every test here is ORG-FREE: the target-org snapshot is a fixture, so the
suite proves the pipeline's decisions rather than one machine's org state.
The only exception is `tests/test_sandbox_integration.py`, which is skipped
unless real sandbox aliases are supplied.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SF_DEPLOY = Path(__file__).resolve().parent.parent
SCRIPTS = SF_DEPLOY / "scripts"
sys.path.insert(0, str(SCRIPTS))

OBJ = "TI_Fnt_Receiving__c"
ORG_A = "00D0000000000A"
ORG_B = "00D0000000000B"


@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    return SCRIPTS


@pytest.fixture(scope="session")
def sf_deploy_root() -> Path:
    return SF_DEPLOY
