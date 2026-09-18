#!/usr/bin/env python3
"""
prep_deploy.py — THE CANONICAL DEPLOY ENTRY POINT.

This is the ONE command that deploys sheet-defined objects, fields AND their
object translations. `deploy.py` is a low-level sf wrapper (no plan, no
translations) and `run.py` now delegates here; neither is a deployment entry
point on its own.

    python scripts/prep_deploy.py --org "<TARGET_ORG>" \
        --tabs "<OBJECT_TABS>" --phase deploy

Pipeline (one pass, in this order — nothing recomputes scope on its own):

    live sheet fetch
      → static validation (hard gate)
      → ONE target-org snapshot        (.build/org_snapshot.json)
      → attribute drift (existing objects)
      → deployment plan / delta        (.build/deploy_plan.json)
      → staged metadata generation     (.build/staging/force-app)
      → one explicit manifest FROM THE PLAN
      → check-only deployment
      → real deployment                (--phase deploy)
      → live post-deployment verification
      → verified translation state + report refresh

Delta rules: a new object ships the CustomObject, all its deployable fields and
all applicable new translations; an EXISTING object ships only fields the org
does not have. Fields that already exist are never silently redeployed —
attribute drift is reported and needs `--include-drift Obj__c.Field__c`. WIP
rows are ignored; IsDelete rows stay in the separate destructive flow.

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
  --phase build   (default, NO org writes): fetch, validate, snapshot, drift,
                  plan, staged generation, manifest, check-only dry-run.
  --phase deploy  re-runs the build steps (idempotent) then the REAL
                  `deploy start`, live verification, and report refresh.

Object translations ride along automatically for tabs that have a
`Field Label (EN)` column; tabs without one are untranslated and the step is
skipped, so a plain field deploy needs no Translation Workbench. `--lang`
selects the Translation Workbench language (default en_US, `off` to skip).

Re-running with no sheet changes is a no-op by design: the plan comes out empty,
no package is built and no org write is attempted.

Authentication is whatever the Salesforce CLI is already authorized with for
`--org`; `--sf-home` / `--xdg-data-home` are opt-in sandbox overrides.

Usage:
  # safe: prepare + validate + dry-run a batch (no org writes)
  python scripts/prep_deploy.py --org <ORG> \
      --tabs "諸掛明細: Sales_IncidentalExpensesDetail,単独諸掛:Sales_StandaloneIncidentalExpenses"

  # REAL deploy of the same batch
  python scripts/prep_deploy.py --org <ORG> --phase deploy \
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from translation_lib import (  # noqa: E402
    DEFAULT_LANG, normalize_lang, save_sync_state,
)

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
# Live Data Dictionary: https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
# Anchored on this file, so the orchestrator behaves the same from any cwd.
SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
# Generation is STAGED: a build never mutates the tracked force-app tree, and a
# stale artifact from an earlier run can never leak into a package.
STAGING = ROOT / ".build/staging"
STAGING_ROOT = STAGING / "force-app/main/default"
STAGING_TRANSLATIONS = STAGING_ROOT / "objectTranslations"
VALIDATION_REPORT = ROOT / ".build/validation_report.json"
SNAPSHOT = ROOT / ".build/org_snapshot.json"
PLAN = ROOT / ".build/deploy_plan.json"
SYNC_STATE = ROOT / ".build/translation_sync_state.json"
PACKAGE = STAGING / "manifest/package.xml"
LAST_DEPLOY_LOG = ROOT / ".build/last_deploy.log"
DEFAULT_API_VERSION = "60.0"


def init_staging(api_version: str = DEFAULT_API_VERSION) -> None:
    """A minimal SFDX project around the staged source, so the CLI resolves the
    manifest against the STAGED tree and not the working copy."""
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    PACKAGE.parent.mkdir(parents=True, exist_ok=True)
    (STAGING / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app", "default": True}],
        "namespace": "",
        "sfdcLoginUrl": "https://test.salesforce.com",
        "sourceApiVersion": api_version,
    }, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# env helpers. By default org steps inherit the environment, so whatever the
# Salesforce CLI is already authorized with works — on a laptop, a build agent,
# or a container. `--sf-home` / `--xdg-data-home` are opt-in overrides for
# sandboxes that cannot let the CLI write into the real HOME; leaving them
# unset is the machine-agnostic path.
# --------------------------------------------------------------------------- #
def google_env(args) -> dict:
    e = os.environ.copy()
    if args.google_home:
        e["HOME"] = args.google_home
        e.pop("XDG_DATA_HOME", None)
    return e


def sf_env(args) -> dict:
    e = os.environ.copy()
    if args.sf_home:
        e["HOME"] = args.sf_home
    if args.xdg_data_home:
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


# Org reads live in org_snapshot.py: the orchestrator queries the org exactly
# once, and every later step reads that snapshot instead of asking again.


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


def attr_drift_phase(args, temp_path: Path, objs: list[str],
                     existing: list[str]) -> None:
    """Attribute drift for objects that already exist, BEFORE generation.

    A non-zero exit is a FAILED comparison, not a clean one: treating it as
    "no drift" would silently hide changed field definitions, so the build
    stops instead.
    """
    # drop reports from earlier runs first, so the plan can never read a stale
    # drift result for an object we did not just compare
    for o in objs:
        (ROOT / f".build/attr_drift_{o}.json").unlink(missing_ok=True)

    for o in existing:
        drift_out = ROOT / f".build/attr_drift_{o}.json"
        cp = subprocess.run(
            ["python3", str(SCRIPTS / "attr_drift.py"), "--object", o,
             "--rows", str(temp_path), "--org", args.org, "--out", str(drift_out)],
            env=sf_env(args), text=True)
        if cp.returncode != 0:
            raise SystemExit(
                f"⛔ attr_drift.py failed for {o} (exit {cp.returncode}). A failed "
                f"drift check is NOT a clean one — fix it before deploying, or the "
                f"build would skip changed field definitions silently.")
        if not drift_out.exists():
            raise SystemExit(f"⛔ attr_drift.py wrote no report for {o} — "
                             f"cannot confirm the object is drift-free.")


def build_phase(args, temp_path: Path) -> tuple[dict, dict[str, str]]:
    print("=" * 72)
    print(f"  BUILD PHASE (no org writes)   org={args.org}")
    print("=" * 72)
    (ROOT / ".build").mkdir(parents=True, exist_ok=True)

    # 1) live sheet fetch — all target tabs together (Google creds)
    print("\n[1/8] fetch sheet tabs (live)")
    run(["python3", str(SCRIPTS / "fetch_sheet.py"),
         "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
         "--out", str(temp_path)], google_env(args), capture=True)
    tab_of = patch_object_apis(temp_path)
    objs = object_list(temp_path)
    if not objs:
        raise SystemExit("❌ no objects parsed from the sheet — check --tabs names.")
    print(f"      objects: {', '.join(objs)}")

    # 2) validation gate (with LIVE referenceTo org-existence check — catches a
    #    Lookup/MasterDetail pointing at an object that doesn't exist in the org,
    #    e.g. the X__c-vs-XMaster__c shorthand; see KB 2026-08-26).
    print("\n[2/8] validate (gate: 0 errors, incl. live referenceTo org check)")
    run(["python3", str(SCRIPTS / "validate_sheet.py"),
         "--in", str(temp_path), "--json", str(VALIDATION_REPORT),
         "--target-org", args.org],
        sf_env(args), capture=True, check=False)
    errs = validation_error_count()
    if errs != 0:
        raise SystemExit(f"⛔ validation reports {errs} ERROR(s) — fix the sheet before deploy. "
                         f"See {VALIDATION_REPORT}")
    print("      validation PASS")

    # 3) ONE bulk org snapshot: identity + object existence + existing fields +
    #    the translation snapshot. Everything downstream reuses this.
    print("\n[3/8] org snapshot (one bulk read: existence + fields + translations)")
    run(["python3", str(SCRIPTS / "org_snapshot.py"),
         "--org", args.org, "--objects", ",".join(objs), "--lang", args.lang,
         "--on-unavailable", args.on_translation_unavailable,
         "--out", str(SNAPSHOT)],
        sf_env(args), capture=True)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    existing = [o for o, v in snapshot["objects"].items() if v.get("exists")]

    # 4) attribute drift for existing objects — BEFORE generation, so the plan
    #    can carry it and the operator decides before anything is built.
    print("\n[4/8] attribute drift (existing objects: sheet definition vs org)")
    attr_drift_phase(args, temp_path, objs, existing)

    # 5) the PLAN: object/field delta + translation delta + manifest members.
    print("\n[5/8] deployment plan (delta)")
    plan_cmd = ["python3", str(SCRIPTS / "plan_deploy.py"),
                "--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
                "--sheet-id", args.sheet_id, "--tabs", args.tabs,
                "--lang", args.lang, "--drift-dir", str(ROOT / ".build"),
                "--sync-state", str(SYNC_STATE), "--out", str(PLAN)]
    if args.include_drift:
        plan_cmd += ["--include-drift", args.include_drift]
    run(plan_cmd, os.environ.copy(), capture=True)
    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    if plan["summary"]["empty"]:
        print("\n" + "-" * 72)
        print("  EMPTY DELTA — the org already matches the sheet. Nothing to build,")
        print("  nothing to deploy. (Re-running is a no-op by design.)")
        print("-" * 72)
        return plan, tab_of

    # 6) staged generation — never touches the tracked force-app tree.
    print(f"\n[6/8] generate metadata XML (staged → {STAGING_ROOT})")
    shutil.rmtree(STAGING, ignore_errors=True)
    init_staging(snapshot["target"].get("apiVersion") or DEFAULT_API_VERSION)
    run(["python3", str(SCRIPTS / "generate_xml.py"),
         "--in", str(temp_path), "--source-root", str(STAGING_ROOT),
         "--plan", str(PLAN)], os.environ.copy(), capture=True)

    if plan["lang"] == "off":
        print("\n[6b] object translations: disabled (--lang off)")
    elif plan["translationState"] != "ok":
        print(f"\n[6b] object translations: {plan['translationState']} — "
              f"{plan['translationNote'][:160]}")
    elif not plan["translationPackage"]:
        print("\n[6b] object translations: nothing new to translate")
    else:
        print(f"\n[6b] object translations ({plan['lang']}, "
              f"{len(plan['translationPackage'])} new) — patched onto the org's own "
              f"translation tree")
        run(["python3", str(SCRIPTS / "generate_object_translation.py"),
             "--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
             "--lang", plan["lang"], "--delta", str(PLAN),
             "--on-unavailable", args.on_translation_unavailable,
             "--out-root", str(STAGING_TRANSLATIONS)],
            os.environ.copy(), capture=True)

    # 7) ONE manifest built from the PLAN's members (not a directory scan, which
    #    can pick up stale metadata left behind by an earlier run).
    print("\n[7/8] build manifest from the plan")
    run(["python3", str(SCRIPTS / "build_manifest.py"),
         "--plan", str(PLAN), "--project-root", str(STAGING),
         "--max-components", str(args.max_components),
         "--out", str(PACKAGE)],
        os.environ.copy(), capture=True)

    # 8) check-only dry-run of exactly that package, from the staged project
    print("\n[8/8] check-only dry-run (writes nothing)")
    run(["python3", str(SCRIPTS / "deploy.py"),
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION_REPORT),
         "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)],
        sf_env(args), capture=True)

    print("\n" + "-" * 72)
    print("  BUILD OK — planned, staged, packaged, dry-run PASSED (no org writes).")
    print(f"  plan: {PLAN}")
    print("  To deploy for real, re-run with --phase deploy (same --tabs/--org).")
    print("-" * 72)
    return plan, tab_of


def persist_sync_state(plan: dict) -> None:
    """Record the deployed translation hashes — only AFTER live verification.

    Writing them earlier would make a failed deploy look "already deployed" on
    the next run and silently drop the translation from the delta. The state is
    keyed by the target org's immutable Id, so it can never be read back
    against a different sandbox.
    """
    packaged = [t for t in plan.get("translations") or [] if t.get("package")]
    org_id = (plan.get("target") or {}).get("orgId", "")
    if not packaged or not org_id:
        return
    save_sync_state(packaged, (plan.get("target") or {}).get("alias", ""),
                    SYNC_STATE, org_id=org_id)
    print(f"      recorded {len(packaged)} verified translation hash(es) for org "
          f"{org_id} → {SYNC_STATE}")


def deploy_phase(args, temp_path: Path, plan: dict, tab_of: dict[str, str]) -> int:
    objs = [o["object"] for o in plan["objects"]]
    print("=" * 72)
    print(f"  DEPLOY PHASE (REAL — writes to org)   org={args.org}")
    print("=" * 72)

    if plan["summary"]["empty"]:
        print("\n  EMPTY DELTA — nothing to deploy. The org already matches the "
              "sheet, so no package was built and no org write is attempted.")
        return 0

    # REAL deploy of the single planned package, from the staged project
    print("\n[1/4] real deploy (sf project deploy start)")
    run(["python3", str(SCRIPTS / "deploy.py"), "--start",
         "--package", str(PACKAGE), "--target-org", args.org,
         "--test-level", args.test_level,
         "--validation-report", str(VALIDATION_REPORT),
         "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)],
        sf_env(args), capture=True)
    # sanity: the log must be a real start, not a stale dry-run
    first = LAST_DEPLOY_LOG.read_text(encoding="utf-8").splitlines()[0] if LAST_DEPLOY_LOG.exists() else ""
    if "deploy start" not in first or "--dry-run" in first:
        raise SystemExit(f"❌ deploy log is not a real 'deploy start' run:\n  {first}")

    # MANDATORY live verification (Tooling API, FLS-independent)
    print("\n[2/4] live verification (verify_deploy.py, Tooling API)")
    cp = subprocess.run(
        ["python3", str(SCRIPTS / "verify_deploy.py"), "--target-org", args.org,
         "--objects", ",".join(objs), "--plan", str(PLAN)],
        env=sf_env(args), text=True)
    if cp.returncode != 0:
        raise SystemExit("⛔ VERIFICATION FAILED — the deploy did NOT fully land. "
                         "Do not report success; investigate before retrying.")

    # translation hashes are recorded only now that the deploy is verified
    print("\n[3/4] persist verified translation state")
    persist_sync_state(plan)

    # report refresh per object (mandatory) — non-fatal if it errors
    print("\n[4/4] refresh deployment report tabs")
    for o in objs:
        tab = tab_of.get(o, "")
        fields_dir = STAGING_ROOT / "objects" / o / "fields"
        cmd = ["python3", str(SCRIPTS / "build_object_report.py"),
               "--object-api", o, "--sheet-tab", tab,
               "--sheet-id", args.sheet_id, "--target-org", args.org,
               "--fields-dir", str(fields_dir),
               "--workbook", args.workbook]
        if args.sf_home:
            cmd += ["--sf-home", args.sf_home]
        if args.xdg_data_home:
            cmd += ["--xdg-data-home", args.xdg_data_home]
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
    ap.add_argument("--out", default=str(ROOT / "temp_updates.json"))
    ap.add_argument("--workbook", default="reports/Object_Deployment_Report.xlsx")
    ap.add_argument("--lang", default=DEFAULT_LANG,
                    help="Translation Workbench language for the object "
                         "translations generated in step 4b (default en_US; "
                         "pass 'off' to skip translations entirely)")
    ap.add_argument("--include-drift", default="",
                    help="comma-separated Obj__c.Field__c whose ATTRIBUTE DRIFT "
                         "you have reviewed and want redeployed. Drift is never "
                         "packaged automatically: some type changes require "
                         "delete+recreate and destroy the field's data.")
    ap.add_argument("--max-components", type=int, default=9000,
                    help="component cap per package; larger plans split "
                         "deterministically, keeping each object with its own "
                         "fields and translations")
    ap.add_argument("--on-translation-unavailable", choices=["error", "skip"],
                    default="error",
                    help="org without Translation Workbench / --lang active: "
                         "fail with an actionable message (default), or skip "
                         "translations and deploy the fields only")
    ap.add_argument("--google-home", default=os.environ.get("SEAP_GOOGLE_HOME", ""),
                    help="override HOME for the Google (ADC) steps; default: inherit")
    ap.add_argument("--sf-home", default=os.environ.get("SEAP_SF_HOME", ""),
                    help="override HOME for the sf CLI steps; default: inherit, "
                         "i.e. use however this machine authorized the CLI")
    ap.add_argument("--xdg-data-home", default=os.environ.get("SEAP_XDG_DATA_HOME", ""),
                    help="override XDG_DATA_HOME for the sf CLI steps; default: inherit")
    args = ap.parse_args()

    if args.lang != "off":
        lang, lang_err = normalize_lang(args.lang)
        if lang_err:
            raise SystemExit(f"❌ --lang: {lang_err} — use a Salesforce Translation "
                             f"Workbench code such as en_US, ja, zh_CN, or 'off'.")
        args.lang = lang

    temp_path = Path(args.out)

    # Build always runs first (idempotent) so deploy has a fresh, validated
    # package — and the PLAN it produces is what the deploy phase consumes.
    plan, tab_of = build_phase(args, temp_path)

    if args.phase == "build":
        return 0

    return deploy_phase(args, temp_path, plan, tab_of)


if __name__ == "__main__":
    sys.exit(main())
