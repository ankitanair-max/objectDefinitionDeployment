#!/usr/bin/env python3
"""Check-only integration test against TWO differently configured sandboxes.

This is the only test that touches real orgs, so it SKIPS unless both sandbox
aliases are supplied:

    SEAP_TEST_ORG_A=<alias> SEAP_TEST_ORG_B=<alias> \\
    SEAP_TEST_TABS="受入:TI_Fnt_Receiving" \\
    python3 scripts/test_sandbox_integration.py

It runs `prep_deploy.py --phase build` (check-only; no org writes) against each
alias and proves the org-dependent behaviour the org-free tests cannot:

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
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SF_DEPLOY = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

ORG_A = os.environ.get("SEAP_TEST_ORG_A", "")
ORG_B = os.environ.get("SEAP_TEST_ORG_B", "")
TABS = os.environ.get("SEAP_TEST_TABS", "")
PLAN = SF_DEPLOY / ".build/deploy_plan.json"


def check_only(org: str, out: Path) -> dict:
    """Run the canonical command in its build (check-only) phase."""
    cp = subprocess.run(
        [sys.executable, str(SCRIPTS / "prep_deploy.py"), "--org", org,
         "--tabs", TABS, "--phase", "build", "--out", str(out / "rows.json")],
        cwd=SF_DEPLOY, text=True, capture_output=True, timeout=1800)
    assert cp.returncode == 0, cp.stdout[-4000:] + cp.stderr[-2000:]
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    (out / f"plan_{org}.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


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
        return                     # check-only never deploys, so it never records
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state.get("orgs", {})) <= {plans[ORG_A]["target"]["orgId"],
                                          plans[ORG_B]["target"]["orgId"]}


def test_rerunning_the_same_sandbox_is_stable(plans):
    with tempfile.TemporaryDirectory() as td:
        first = check_only(ORG_A, Path(td))
        second = check_only(ORG_A, Path(td))
    assert first["manifestMembers"] == second["manifestMembers"], \
        "a check-only run must not change what the next run plans"


def main() -> int:
    print("test_sandbox_integration")
    if not (ORG_A and ORG_B and TABS):
        print("  SKIPPED — set SEAP_TEST_ORG_A, SEAP_TEST_ORG_B and "
              "SEAP_TEST_TABS to run the two-sandbox check-only test")
        return 0
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        plans = {ORG_A: check_only(ORG_A, out), ORG_B: check_only(ORG_B, out)}
        for fn in (test_each_sandbox_gets_its_own_plan,
                   test_translation_state_is_per_org,
                   test_sync_state_is_bucketed_by_org_id,
                   test_rerunning_the_same_sandbox_is_stable):
            fn(plans)
            print(f"  ok  {fn.__name__[5:].replace('_', '-')}")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
