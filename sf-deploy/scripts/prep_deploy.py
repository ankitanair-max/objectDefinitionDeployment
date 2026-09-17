#!/usr/bin/env python3
"""
prep_deploy.py — One-command, gated, BATCH orchestrator for the
Sheet -> validate -> generate -> package -> deploy -> verify -> report pipeline.

It runs the whole chain for one OR many object tabs in a single invocation while
keeping EVERY existing safety gate in place:

  * validation gate            — build stops unless validate_sheet reports 0 ERRORs
  * object-existence pre-check  — live, per object (rule sf-object-existence-precheck)
  * check-only dry-run          — writes nothing to the org
  * SHOOT deploy gate           — a REAL deploy only runs in --phase deploy, which
                                  the assistant may invoke ONLY after the user typed
                                  SHOOT (this script never bypasses that)
  * live post-deploy verify     — verify_deploy.py (Tooling API, FLS-independent)
  * report refresh              — build_object_report.py tab per object (mandatory)

Speed: all target objects are fetched together, validated together, generated
together, packaged into ONE manifest, and deployed in ONE `deploy start`, then
verified + reported per object. N CLI round-trips collapse to ~1.

Two phases:
  --phase build   (default, NO org writes): fetch, patch, validate, generate,
                  manifest, existence pre-check, and a check-only dry-run.
  --phase deploy  (GATED): re-runs build steps (idempotent) then the REAL
                  `deploy start`, live verification, and report refresh.

Usage:
  # safe: prepare + validate + dry-run a batch (no org writes)
  python scripts/prep_deploy.py --org ERPDEV01 \
      --tabs "諸掛明細: Sales_IncidentalExpensesDetail,単独諸掛:Sales_StandaloneIncidentalExpenses"

  # REAL deploy of the same batch (assistant runs this only after SHOOT)
  python scripts/prep_deploy.py --org ERPDEV01 --phase deploy \
      --tabs "諸掛明細: Sales_IncidentalExpensesDetail,単独諸掛:Sales_StandaloneIncidentalExpenses"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
# Live Data Dictionary: https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
OBJECTS_ROOT = Path("force-app/main/default/objects")
VALIDATION_REPORT = Path(".build/validation_report.json")
PACKAGE = Path("manifest/package.xml")
LAST_DEPLOY_LOG = Path(".build/last_deploy.log")


# --------------------------------------------------------------------------- #
# env helpers: Google steps need the real HOME (ADC); org steps need the sfhome
# shim (keychain-linked) so the sf CLI can write its lock/cache files.
# --------------------------------------------------------------------------- #
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


def run(cmd: list[str], env: dict, *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    cp = subprocess.run(cmd, env=env, text=True,
                        capture_output=capture)
    if capture and cp.stdout:
        # echo a trimmed tail so the run is transparent but not noisy
        tail = "\n".join(cp.stdout.strip().splitlines()[-8:])
        if tail:
            print("    " + tail.replace("\n", "\n    "))
    if check and cp.returncode != 0:
        if capture and cp.stderr:
            print(cp.stderr[-800:])
        raise SystemExit(f"❌ step failed (exit {cp.returncode}): {' '.join(cmd)}")
    return cp


def soql(query: str, args, tooling: bool = False) -> list[dict]:
    cmd = ["sf", "data", "query", "--query", query, "--target-org", args.org, "--json"]
    if tooling:
        cmd.append("--use-tooling-api")
    cp = subprocess.run(cmd, env=sf_env(args), text=True, capture_output=True)
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"❌ org query parse error:\n{cp.stdout[:300]}\n{cp.stderr[:300]}")
    if data.get("status") != 0:
        raise SystemExit(f"❌ org query failed: {data.get('message','unknown')}")
    return data.get("result", {}).get("records", []) or []


# --------------------------------------------------------------------------- #
# pipeline steps
# --------------------------------------------------------------------------- #
def patch_object_apis(temp_path: Path) -> dict[str, str]:
    """Ensure object API names end with __c. Return {objectApi: sourceTab}."""
    rows = json.loads(temp_path.read_text(encoding="utf-8"))
    tab_of: dict[str, str] = {}
    for r in rows:
        api = str(r.get("Object API Name") or "").strip()
        if api and not api.endswith("__c"):
            api = api + "__c"
            r["Object API Name"] = api
        # a field row carries _SheetName; use it to map object -> tab
        tab = str(r.get("_SheetName") or "").strip()
        if api and tab:
            tab_of.setdefault(api, tab)
    temp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return tab_of


def object_list(temp_path: Path) -> list[str]:
    rows = json.loads(temp_path.read_text(encoding="utf-8"))
    objs = []
    for r in rows:
        if r.get("_type") == "object_meta":
            api = str(r.get("Object API Name") or "").strip()
            if api and api not in objs:
                objs.append(api)
    return objs


def validation_error_count() -> int:
    if not VALIDATION_REPORT.exists():
        return -1
    try:
        return int(json.loads(VALIDATION_REPORT.read_text()).get("counts", {}).get("ERROR", 0))
    except Exception:
        return -1


def build_phase(args, temp_path: Path) -> tuple[list[str], dict[str, str]]:
    print("=" * 72)
    print(f"  BUILD PHASE (no org writes)   org={args.org}")
    print("=" * 72)

    # 1) fetch all target tabs together (Google creds)
    print("\n[1/6] fetch sheet tabs")
    run(["python3", "scripts/fetch_sheet.py",
         "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
         "--out", str(temp_path)], google_env(args), capture=True)

    # 2) patch object API names -> __c ; derive object->tab map
    print("\n[2/6] patch object API names (__c)")
    tab_of = patch_object_apis(temp_path)
    objs = object_list(temp_path)
    if not objs:
        raise SystemExit("❌ no objects parsed from the sheet — check --tabs names.")
    print(f"      objects: {', '.join(objs)}")

    # 3) validation gate (with LIVE referenceTo org-existence check — catches a
    #    Lookup/MasterDetail pointing at an object that doesn't exist in the org,
    #    e.g. the X__c-vs-XMaster__c shorthand; see KB 2026-08-26).
    print("\n[3/6] validate (gate: 0 errors, incl. live referenceTo org check)")
    run(["python3", "scripts/validate_sheet.py",
         "--in", str(temp_path), "--json", str(VALIDATION_REPORT),
         "--target-org", args.org],
        sf_env(args), capture=True, check=False)
    errs = validation_error_count()
    if errs != 0:
        raise SystemExit(f"⛔ validation reports {errs} ERROR(s) — fix the sheet before deploy. "
                         f"See {VALIDATION_REPORT}")
    print("      validation PASS")

    # 4) regenerate metadata for the target objects
    print("\n[4/6] generate metadata XML")
    for o in objs:
        shutil.rmtree(OBJECTS_ROOT / o, ignore_errors=True)
    run(["python3", "scripts/generate_xml.py"], os.environ.copy(), capture=True)

    # 4b) CustomObjectTranslation from Field Label (EN) / Object Label (EN)
    #     on the SAME object tabs — automatic, no extra operator step.
    print("\n[4b] generate object translations (en_US CustomObjectTranslation)")
    for o in objs:
        for p in Path("force-app/main/default/objectTranslations").glob(f"{o}-*"):
            shutil.rmtree(p, ignore_errors=True)
    run(["python3", "scripts/fetch_translations.py",
         "--spreadsheet-id", args.sheet_id,
         "--from-object-rows", str(temp_path),
         "--out", ".build/translation_catalog_objects.json"],
        google_env(args), capture=True)
    run(["python3", "scripts/translation_drift.py",
         "--catalog", ".build/translation_catalog_objects.json",
         "--org", args.org, "--lang", "en_US", "--new-only",
         "--out", ".build/translation_drift_objects.json"],
        sf_env(args), capture=True)
    run(["python3", "scripts/generate_object_translation.py",
         "--rows", str(temp_path), "--org", args.org, "--lang", "en_US",
         "--delta", ".build/translation_drift_objects.json"],
        sf_env(args), capture=True)

    # 5) build ONE manifest for the whole batch
    print("\n[5/6] build manifest (single package for the batch)")
    run(["python3", "scripts/build_manifest.py",
         "--out", str(PACKAGE), "--only", ",".join(objs)],
        os.environ.copy(), capture=True)

    # 6) existence pre-check + check-only dry-run
    print("\n[6/6] object-existence pre-check + check-only dry-run")
    inlist = "','".join(objs)
    present = {r["QualifiedApiName"] for r in
               soql(f"SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName IN ('{inlist}')", args)}
    for o in objs:
        print(f"      {'EXISTS ' if o in present else 'NEW    '} {o}"
              + ("" if o in present else "  (object + fields will be created)"))

    run(["python3", "scripts/deploy.py",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION_REPORT)],
        sf_env(args), capture=True)

    # 6b) attribute-level drift for EXISTING objects — the name-based delta only
    #     checks whether a field EXISTS; this compares the sheet's DEFINITION
    #     (type / formula / referenceTo / picklist) against the org's ACTUAL
    #     metadata for fields present in both, surfacing changed-but-existing
    #     fields the delta would otherwise silently skip. Report-only (never
    #     blocks the build): type changes usually need a delete+recreate decision.
    print("\n[6b] attribute-drift check (existing objects: sheet definition vs org)")
    total_drift = 0
    for o in objs:
        if o not in present:
            print(f"      NEW    {o}: skipped (nothing to compare)")
            continue
        drift_out = f".build/attr_drift_{o}.json"
        subprocess.run(["python3", "scripts/attr_drift.py", "--object", o,
                        "--rows", str(temp_path), "--org", args.org, "--out", drift_out],
                       env=google_env(args), text=True)
        try:
            total_drift += len(json.loads(Path(drift_out).read_text()))
        except Exception:
            pass
    if total_drift:
        print(f"\n      ⚠️  {total_drift} field(s) differ in DEFINITION between sheet and org "
              f"(see .build/attr_drift_*.json).\n"
              f"          These are NOT redeployed by the name-based delta. Review whether they\n"
              f"          need an update (some type changes require delete+recreate = data loss).")

    print("\n" + "-" * 72)
    print("  BUILD OK — validated, packaged, dry-run PASSED (nothing written to org).")
    print("  To deploy for real (GATED): after the user types SHOOT, run again with")
    print("     --phase deploy   (same --tabs/--org).")
    print("-" * 72)
    return objs, tab_of


def deploy_phase(args, temp_path: Path, objs: list[str], tab_of: dict[str, str]) -> int:
    print("=" * 72)
    print(f"  DEPLOY PHASE (REAL — writes to org)   org={args.org}")
    print("=" * 72)

    # REAL deploy of the single batch package
    print("\n[1/3] real deploy (sf project deploy start)")
    run(["python3", "scripts/deploy.py", "--start",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION_REPORT)],
        sf_env(args), capture=True)
    # sanity: the log must be a real start, not a stale dry-run
    first = LAST_DEPLOY_LOG.read_text(encoding="utf-8").splitlines()[0] if LAST_DEPLOY_LOG.exists() else ""
    if "deploy start" not in first or "--dry-run" in first:
        raise SystemExit(f"❌ deploy log is not a real 'deploy start' run:\n  {first}")

    # MANDATORY live verification (Tooling API, FLS-independent)
    print("\n[2/3] live verification (verify_deploy.py, Tooling API)")
    cp = subprocess.run(
        ["python3", "scripts/verify_deploy.py", "--target-org", args.org,
         "--objects", ",".join(objs)],
        env=sf_env(args), text=True)
    if cp.returncode != 0:
        raise SystemExit("⛔ VERIFICATION FAILED — the deploy did NOT fully land. "
                         "Do not report success; investigate before retrying.")

    # report refresh per object (mandatory) — non-fatal if it errors
    print("\n[3/3] refresh deployment report tabs")
    for o in objs:
        tab = tab_of.get(o, "")
        fields_dir = OBJECTS_ROOT / o / "fields"
        cmd = ["python3", "scripts/build_object_report.py",
               "--object-api", o, "--sheet-tab", tab,
               "--sheet-id", args.sheet_id, "--target-org", args.org,
               "--fields-dir", str(fields_dir),
               "--sf-home", args.sf_home, "--xdg-data-home", args.xdg_data_home,
               "--workbook", args.workbook]
        rc = subprocess.run(cmd, env=google_env(args), text=True).returncode
        if rc != 0:
            print(f"      ⚠️  report refresh failed for {o} (deploy still verified).")

    print("\n" + "=" * 72)
    print(f"  ✅ BATCH DEPLOYED & VERIFIED: {len(objs)} object(s) confirmed in {args.org}")
    print("=" * 72)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Gated batch Sheet->org deploy orchestrator")
    ap.add_argument("--org", required=True, help="target org alias/username")
    ap.add_argument("--tabs", required=True, help="comma-separated object tab names")
    ap.add_argument("--phase", choices=["build", "deploy"], default="build")
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--out", default="temp_updates.json")
    ap.add_argument("--workbook", default="reports/Object_Deployment_Report.xlsx")
    ap.add_argument("--google-home", default=os.environ.get("SEAP_GOOGLE_HOME", str(Path.home())))
    ap.add_argument("--sf-home", default=str(Path(".sfhome").resolve()))
    ap.add_argument("--xdg-data-home", default=str(Path.home() / ".local" / "share"))
    args = ap.parse_args()

    temp_path = Path(args.out)

    # Build always runs first (idempotent) so deploy has a fresh, validated package.
    objs, tab_of = build_phase(args, temp_path)

    if args.phase == "build":
        return 0

    return deploy_phase(args, temp_path, objs, tab_of)


if __name__ == "__main__":
    sys.exit(main())
