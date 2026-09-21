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
  --phase build   (default, NO org writes): fetch, translate-enrich, validate,
                  plan, generate, manifest, existence pre-check, check-only.
  --phase deploy  (GATED): re-runs build steps (idempotent) then the REAL
                  `deploy start`, live verification, and report refresh.

Translations (object / Name / custom-field English labels) are produced,
validated, planned, deployed, and verified through THIS command. There is no
parallel translation pipeline.

Usage:
  python scripts/prep_deploy.py --org ERPDEV01 --tabs "Deal,Shipping"

  python scripts/prep_deploy.py --org ERPDEV01 --phase deploy \
      --tabs "Deal,Shipping" --apply-translations
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
OBJECTS_ROOT = Path("force-app/main/default/objects")
TRANSLATIONS_ROOT = Path("force-app/main/default/objectTranslations")
VALIDATION_REPORT = Path(".build/validation_report.json")
PACKAGE = Path("manifest/package.xml")
LAST_DEPLOY_LOG = Path(".build/last_deploy.log")
DEPLOY_PLAN = Path(".build/deploy_plan.json")
ORG_SNAPSHOT = Path(".build/org_snapshot.json")
TRANSLATION_PREVIEW = Path(".build/translation_preview.json")
SYNC_STATE = Path(".build/translation_sync_state.json")


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


def tooling_fields(obj: str, args) -> set[str]:
    recs = soql(
        "SELECT DeveloperName FROM CustomField "
        f"WHERE EntityDefinition.QualifiedApiName='{obj}'",
        args, tooling=True)
    return {f"{r['DeveloperName']}__c" for r in recs}


def build_phase(args, temp_path: Path) -> tuple[list[str], dict[str, str], dict]:
    print("=" * 72)
    print(f"  BUILD PHASE (no org writes)   org={args.org}")
    print("=" * 72)

    sys.path.insert(0, "scripts")
    from fetch_sheet import parse_tab_list
    tab_list = parse_tab_list(args.tabs)
    if not tab_list:
        raise SystemExit("❌ --tabs is required (comma-separated, e.g. Deal,Shipping).")

    # 1) fetch all target tabs together
    print("\n[1/8] fetch sheet tabs (selected --tabs only)")
    run(["python3", "scripts/fetch_sheet.py",
         "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
         "--out", str(temp_path)], google_env(args), capture=True)

    # 2) automatic translation enrichment (DeepL or Google — one provider/batch)
    print("\n[2/8] translation enrichment (JA → en_US)")
    from translate_enrich import (
        NEEDS_CONFIRMATION, TranslationAbort, merge_into_rows, preview, run_enrichment,
        org_id_from_display, plan_has_members, save_sync_state,
        build_plan, snapshot_translations, write_plan,
    )

    rows = json.loads(temp_path.read_text(encoding="utf-8"))
    fail_hook = bool(args.fail_deepl_after_preflight or
                     os.environ.get("SF_FAIL_DEEPL_AFTER_PREFLIGHT"))
    try:
        enr = run_enrichment(
            spreadsheet_id=args.sheet_id,
            tabs=tab_list,
            rows=rows,
            apply=bool(args.apply_translations),
            force_provider=args.force_provider,
            fail_after_preflight=fail_hook,
        )
    except TranslationAbort as e:
        print(e)
        raise SystemExit(1) from e

    TRANSLATION_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    preview_payload = {
        "provider": enr.provider,
        "tabs": enr.tabs,
        "spreadsheet_id": enr.spreadsheet_id,
        "headers_to_create": enr.headers_to_create,
        "writes": [w.__dict__ if hasattr(w, "__dict__") else w for w in enr.writes],
        "translated": enr.translated,
        "backfilled": enr.backfilled,
        "objects_affected": enr.objects_affected,
        "fields_affected": enr.fields_affected,
    }
    TRANSLATION_PREVIEW.write_text(json.dumps(preview_payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    print(preview(enr))
    if enr.writes and not args.apply_translations:
        raise SystemExit(
            f"⛔ translation batch requires confirmation ({len(enr.writes)} cell(s)). "
            f"Preview: {TRANSLATION_PREVIEW}\n"
            f"Re-run the SAME command with --apply-translations after confirming. "
            f"exit={NEEDS_CONFIRMATION}"
        )

    if enr.applied or enr.rows_patch:
        rows = merge_into_rows(rows, enr.rows_patch)
        temp_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        # Re-fetch selected tabs so calculated Google values (and provenance)
        # are what validation/planning see — never the formula text.
        if enr.applied:
            run(["python3", "scripts/fetch_sheet.py",
                 "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
                 "--out", str(temp_path)], google_env(args), capture=True)
            # Overlay calculated EN from enrichment (Sheets formatted values)
            fetched = json.loads(temp_path.read_text(encoding="utf-8"))
            fetched = merge_into_rows(fetched, enr.rows_patch)
            temp_path.write_text(json.dumps(fetched, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    # 3) patch object API names -> __c ; derive object->tab map
    print("\n[3/8] patch object API names (__c)")
    tab_of = patch_object_apis(temp_path)
    objs = object_list(temp_path)
    if not objs:
        raise SystemExit("❌ no objects parsed from the sheet — check --tabs names.")
    print(f"      objects: {', '.join(objs)}")

    # 4) validation gate (existing + translation) BEFORE any metadata generation
    print("\n[4/8] validate (gate: 0 errors, incl. translation + live referenceTo)")
    run(["python3", "scripts/validate_sheet.py",
         "--in", str(temp_path), "--json", str(VALIDATION_REPORT),
         "--target-org", args.org],
        sf_env(args), capture=True, check=False)
    errs = validation_error_count()
    if errs != 0:
        raise SystemExit(f"⛔ validation reports {errs} ERROR(s) — fix the sheet before deploy. "
                         f"See {VALIDATION_REPORT}")
    print("      validation PASS")

    # 5) one fresh org snapshot + immutable plan
    print("\n[5/8] org snapshot + delta plan (schema + translations)")
    inlist = "','".join(objs)
    present = {r["QualifiedApiName"] for r in
               soql(f"SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName IN ('{inlist}')", args)}
    present_fields = {o: (tooling_fields(o, args) if o in present else set()) for o in objs}
    org_id = org_id_from_display(args.org)
    org_t = snapshot_translations(list(present), args.org)
    ORG_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    ORG_SNAPSHOT.write_text(json.dumps({
        "org": args.org, "orgId": org_id, "objects": sorted(present),
        "fields": {k: sorted(v) for k, v in present_fields.items()},
        "translations": org_t,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = json.loads(temp_path.read_text(encoding="utf-8"))
    plan = build_plan(
        rows=rows, org=args.org, org_id=org_id, tabs=tab_list,
        provider=enr.provider, present_objects=present,
        present_fields=present_fields, org_translations=org_t,
    )
    write_plan(plan, DEPLOY_PLAN)
    print(f"      provider={plan.get('provider')}  empty={plan.get('empty')}  "
          f"members={ {k: len(v) for k, v in (plan.get('members') or {}).items()} }")

    if plan.get("empty"):
        print("\n" + "-" * 72)
        print("  BUILD OK — empty plan (unchanged sheet + org). No metadata, no deploy.")
        print("-" * 72)
        return objs, tab_of, plan

    # 6) regenerate metadata for planned objects/fields + translations
    print("\n[6/8] generate metadata XML (schema + translations from plan)")
    for o in objs:
        shutil.rmtree(OBJECTS_ROOT / o, ignore_errors=True)
        shutil.rmtree(TRANSLATIONS_ROOT / f"{o}-en_US", ignore_errors=True)
    run(["python3", "scripts/generate_xml.py"], os.environ.copy(), capture=True)
    run(["python3", "scripts/generate_object_translation.py",
         "--plan", str(DEPLOY_PLAN), "--org-snapshot", str(ORG_SNAPSHOT)],
        os.environ.copy(), capture=True)

    # 7) manifest FROM THE PLAN (not a directory scan)
    print("\n[7/8] build manifest from immutable plan")
    run(["python3", "scripts/build_manifest.py",
         "--out", str(PACKAGE), "--plan", str(DEPLOY_PLAN)],
        os.environ.copy(), capture=True)

    # 8) existence pre-check + check-only dry-run
    print("\n[8/8] object-existence pre-check + check-only dry-run")
    for o in objs:
        print(f"      {'EXISTS ' if o in present else 'NEW    '} {o}"
              + ("" if o in present else "  (object + fields will be created)"))

    run(["python3", "scripts/deploy.py",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION_REPORT)],
        sf_env(args), capture=True)

    # 8b) attribute-level drift for EXISTING objects
    print("\n[8b] attribute-drift check (existing objects: sheet definition vs org)")
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
    print("  To deploy for real: run again with --phase deploy (same --tabs/--org")
    print("     and --apply-translations if a translation write was already confirmed).")
    print("-" * 72)
    return objs, tab_of, plan


def deploy_phase(args, temp_path: Path, objs: list[str], tab_of: dict[str, str],
                 plan: dict) -> int:
    print("=" * 72)
    print(f"  DEPLOY PHASE (REAL — writes to org)   org={args.org}")
    print("=" * 72)

    if plan.get("empty"):
        print("  empty plan — no org write (idempotent re-run).")
        print("=" * 72)
        return 0

    sys.path.insert(0, "scripts")
    from translate_enrich import save_sync_state

    # REAL deploy of the single batch package
    print("\n[1/3] real deploy (sf project deploy start)")
    try:
        run(["python3", "scripts/deploy.py", "--start",
             "--package", str(PACKAGE), "--target-org", args.org,
             "--test-level", args.test_level,
             "--validation-report", str(VALIDATION_REPORT)],
            sf_env(args), capture=True)
    except SystemExit:
        print("\n⛔ Salesforce deploy failed AFTER sheet translations were written.")
        print("   English + provenance are RETAINED on the sheet.")
        print("   Translation sync state is NOT marked complete.")
        print("   Set affected fields' Deployment Status to Not Deployed and retry")
        print("   through this same command (unchanged Japanese will not be retranslated).")
        raise

    first = LAST_DEPLOY_LOG.read_text(encoding="utf-8").splitlines()[0] if LAST_DEPLOY_LOG.exists() else ""
    if "deploy start" not in first or "--dry-run" in first:
        raise SystemExit(f"❌ deploy log is not a real 'deploy start' run:\n  {first}")

    print("\n[2/3] live verification (objects, fields, exact English labels)")
    cp = subprocess.run(
        ["python3", "scripts/verify_deploy.py", "--target-org", args.org,
         "--objects", ",".join(objs), "--plan", str(DEPLOY_PLAN),
         "--org-snapshot", str(ORG_SNAPSHOT)],
        env=sf_env(args), text=True)
    if cp.returncode != 0:
        print("\n⛔ VERIFICATION FAILED — translations stay on the sheet; sync is NOT complete.")
        raise SystemExit("⛔ VERIFICATION FAILED — the deploy did NOT fully land. "
                         "Do not report success; investigate before retrying.")

    packaged = [t for t in (plan.get("translations") or []) if t.get("package")]
    if packaged:
        save_sync_state(SYNC_STATE, plan.get("orgId") or args.org, packaged)
        print(f"      translation sync state written for orgId={plan.get('orgId')}")

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
    ap.add_argument("--tabs", required=True,
                    help="comma-separated object tab names (e.g. Deal,Shipping)")
    ap.add_argument("--phase", choices=["build", "deploy"], default="build")
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--out", default="temp_updates.json")
    ap.add_argument("--workbook", default="reports/Object_Deployment_Report.xlsx")
    ap.add_argument("--google-home", default=os.environ.get("SEAP_GOOGLE_HOME", str(Path.home())))
    ap.add_argument("--sf-home", default=str(Path(".sfhome").resolve()))
    ap.add_argument("--xdg-data-home", default=str(Path.home() / ".local" / "share"))
    ap.add_argument("--apply-translations", action="store_true",
                    help="write the confirmed translation batch (gated sheet write)")
    ap.add_argument("--force-provider", default="", choices=["", "deepl", "google"],
                    help="force DeepL or Google for this batch (tests / failover)")
    ap.add_argument("--fail-deepl-after-preflight", action="store_true",
                    help="test hook: pretends DeepL died mid-batch after a healthy preflight")
    args = ap.parse_args()

    temp_path = Path(args.out)

    objs, tab_of, plan = build_phase(args, temp_path)

    if args.phase == "build":
        return 0

    return deploy_phase(args, temp_path, objs, tab_of, plan)


if __name__ == "__main__":
    sys.exit(main())
