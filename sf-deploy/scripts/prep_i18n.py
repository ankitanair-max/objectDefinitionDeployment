#!/usr/bin/env python3
"""
prep_i18n.py — orchestrator for LWC Custom Label + Flow translation deploys.

Same gates as prep_deploy.py:
  fetch live → validate (0 ERROR) → drift vs live org → generate (retrieve-merge)
  → manifest → check-only dry-run.  --phase deploy writes to the org.

Object/field EN columns ride along with prep_deploy.py (Step 4b), not here.
This command is for the I18N_LWC / I18N_Flows tabs.

Usage:
  python scripts/prep_i18n.py --org ERPDEV01 --tabs I18N_LWC,I18N_Flows
  python scripts/prep_i18n.py --org ERPDEV01 --tabs I18N_LWC,I18N_Flows --phase deploy
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
DEFAULT_TABS = "I18N_LWC,I18N_Flows"
CATALOG = Path(".build/i18n_catalog.json")
VALIDATION = Path(".build/i18n_validation.json")
DRIFT = Path(".build/i18n_drift.json")
PACKAGE = Path("manifest/i18n_package.xml")


def google_env(args) -> dict:
    e = os.environ.copy()
    e["HOME"] = args.google_home
    e.pop("XDG_DATA_HOME", None)
    return e


def sf_env(args) -> dict:
    e = os.environ.copy()
    e["HOME"] = args.sf_home
    e["XDG_DATA_HOME"] = args.xdg_data_home
    e["SF_DISABLE_LOG_FILE"] = "true"
    e["SFDX_DISABLE_LOG_FILE"] = "true"
    return e


def run(cmd: list[str], env: dict, *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    cp = subprocess.run(cmd, env=env, text=True)
    if check and cp.returncode != 0:
        raise SystemExit(f"❌ step failed (exit {cp.returncode}): {' '.join(cmd)}")
    return cp


def build_phase(args) -> None:
    print("=" * 72)
    print(f"  I18N BUILD (no org writes)   org={args.org}  tabs={args.tabs}")
    print("=" * 72)

    print("\n[1/6] fetch I18N catalog tabs (live)")
    run(["python3", "scripts/fetch_i18n.py",
         "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
         "--out", str(CATALOG)], google_env(args))

    print("\n[2/6] validate (0 ERROR gate)")
    cp = run(["python3", "scripts/validate_i18n.py",
              "--in", str(CATALOG), "--json", str(VALIDATION),
              "--org", args.org],
             sf_env(args), check=False)
    if cp.returncode != 0:
        raise SystemExit(f"⛔ i18n validation has ERRORs — see {VALIDATION}")

    print("\n[3/6] drift vs live org Translations / CustomLabels")
    run(["python3", "scripts/i18n_drift.py",
         "--catalog", str(CATALOG), "--org", args.org,
         "--lang", args.lang, "--conflict", args.conflict,
         "--out", str(DRIFT)],
         sf_env(args))

    print("\n[4/6] generate CustomLabels + Translations (retrieve-merge)")
    run(["python3", "scripts/generate_translations.py",
         "--catalog", str(CATALOG), "--delta", str(DRIFT),
         "--org", args.org, "--lang", args.lang],
         sf_env(args))

    print("\n[5/6] build i18n-only manifest")
    run(["python3", "scripts/build_manifest.py",
         "--out", str(PACKAGE),
         "--types", "CustomLabels,Translations"],
         os.environ.copy())

    print("\n[6/6] check-only dry-run")
    run(["python3", "scripts/deploy.py",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION),
         "--skip-validation-gate"],
         sf_env(args))
    print("\n  BUILD OK — dry-run passed. Nothing written to the org.")
    print("  Real deploy:  --phase deploy  (same --tabs/--org)")


def deploy_phase(args) -> int:
    print("=" * 72)
    print(f"  I18N DEPLOY (REAL)   org={args.org}")
    print("=" * 72)
    run(["python3", "scripts/deploy.py", "--start",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION),
         "--skip-validation-gate"],
         sf_env(args))
    print("\n[post] verify live Translations / CustomLabels")
    run(["python3", "scripts/verify_i18n.py",
         "--delta", str(DRIFT), "--org", args.org, "--lang", args.lang],
         sf_env(args))
    # persist hashes of what we packaged so the next run can detect Workbench conflicts
    sys.path.insert(0, "scripts")
    from i18n_lib import save_sync_state  # noqa: E402
    delta = json.loads(DRIFT.read_text(encoding="utf-8"))
    save_sync_state(delta, args.org)
    print("  sync state updated → .build/i18n_sync_state.json")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="I18N_LWC / I18N_Flows deploy orchestrator")
    ap.add_argument("--org", required=True)
    ap.add_argument("--tabs", default=DEFAULT_TABS)
    ap.add_argument("--phase", choices=["build", "deploy"], default="build")
    ap.add_argument("--lang", default="en_US")
    ap.add_argument("--conflict", default="park",
                    choices=["park", "sheet-wins", "org-wins"])
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--google-home", default=os.environ.get("SEAP_GOOGLE_HOME", str(Path.home())))
    ap.add_argument("--sf-home", default=str(Path(".sfhome").resolve()))
    ap.add_argument("--xdg-data-home", default=str(Path.home() / ".local" / "share"))
    args = ap.parse_args()

    build_phase(args)
    if args.phase == "build":
        return 0
    return deploy_phase(args)


if __name__ == "__main__":
    sys.exit(main())
