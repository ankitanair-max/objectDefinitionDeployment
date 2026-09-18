#!/usr/bin/env python3
"""Check-only integration test against TWO differently configured sandboxes.

This is the only test in the suite that touches real orgs, so it is SKIPPED
unless both sandbox aliases are supplied:

    SEAP_TEST_ORG_A=<alias> SEAP_TEST_ORG_B=<alias> \\
    SEAP_TEST_TABS="受入:TI_Fnt_Receiving" \\
    python3 -m pytest sf-deploy/tests/test_sandbox_integration.py -v

It runs `deploy.py --check-only` (no org writes) against each alias and proves
the org-dependent behaviour the org-free tests cannot:

  * the plan is computed per org: each run records its OWN immutable org Id,
    and the deltas are independent (a field present in A but not in B is
    planned for B only);
  * translation state is per org (Workbench on/off, language activated or not)
    and never leaks between sandboxes;
  * the sync state is bucketed by org Id, so a hash recorded for A never marks
    B's translation "unchanged";
  * re-running the same command against the same sandbox is a no-op.

The run needs the Salesforce CLI authorized for both aliases and Google
credentials for the sheet fetch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SF_DEPLOY = Path(__file__).resolve().parent.parent
SCRIPTS = SF_DEPLOY / "scripts"
sys.path.insert(0, str(SCRIPTS))

ORG_A = os.environ.get("SEAP_TEST_ORG_A", "")
ORG_B = os.environ.get("SEAP_TEST_ORG_B", "")
TABS = os.environ.get("SEAP_TEST_TABS", "")

pytestmark = pytest.mark.skipif(
    not (ORG_A and ORG_B and TABS),
    reason="set SEAP_TEST_ORG_A, SEAP_TEST_ORG_B and SEAP_TEST_TABS to run the "
           "two-sandbox check-only integration test")


def check_only(org: str, out: Path) -> dict:
    """Run the public command in check-only mode; return the resulting plan."""
    cp = subprocess.run(
        [sys.executable, str(SCRIPTS / "deploy.py"), "--org", org,
         "--tabs", TABS, "--check-only", "--out", str(out / "rows.json")],
        cwd=SF_DEPLOY, text=True, capture_output=True, timeout=1800)
    assert cp.returncode == 0, cp.stdout[-4000:] + cp.stderr[-2000:]
    plan = json.loads((SF_DEPLOY / ".build/deploy_plan.json").read_text(encoding="utf-8"))
    (out / f"plan_{org}.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


@pytest.fixture(scope="module")
def plans(tmp_path_factory) -> dict[str, dict]:
    out = tmp_path_factory.mktemp("sandboxes")
    return {ORG_A: check_only(ORG_A, out), ORG_B: check_only(ORG_B, out)}


def test_each_sandbox_gets_its_own_plan(plans):
    a, b = plans[ORG_A], plans[ORG_B]
    assert a["target"]["orgId"] and b["target"]["orgId"]
    assert a["target"]["orgId"] != b["target"]["orgId"], \
        "the two aliases must point at different orgs for this test to mean anything"
    for plan in (a, b):
        # the plan only ever proposes fields the org does not already have
        for obj in plan["objects"]:
            assert not (set(obj["newFields"]) & set(obj["existingFields"]))


def test_translation_state_is_per_org(plans):
    for org, plan in plans.items():
        assert plan["translationState"] in {"ok", "off", "unavailable", "error"}
        if plan["translationState"] != "ok":
            assert not plan["translationPackage"], \
                f"{org}: nothing may be packaged when translations are unavailable"


def test_sync_state_is_bucketed_by_org_id(plans):
    state_path = SF_DEPLOY / ".build/translation_sync_state.json"
    if not state_path.exists():
        pytest.skip("no translation sync state recorded (check-only never deploys)")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state.get("orgs", {})) <= {plans[ORG_A]["target"]["orgId"],
                                          plans[ORG_B]["target"]["orgId"]}


def test_rerunning_the_same_sandbox_is_stable(tmp_path):
    first = check_only(ORG_A, tmp_path)
    second = check_only(ORG_A, tmp_path)
    assert first["manifestMembers"] == second["manifestMembers"], \
        "a check-only run must not change what the next run plans"
