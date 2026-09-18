#!/usr/bin/env python3
"""
prep_deploy.py — THE CANONICAL DEPLOY ENTRY POINT.

This is the ONE command that deploys sheet-defined objects, fields AND their
object translations. `deploy.py` is the low-level Salesforce CLI wrapper.
`run.py` is the org-free sheet→package path (fetch → validate → generate_xml →
build_manifest) and does not require Salesforce access.

    python scripts/prep_deploy.py --org "<TARGET_ORG>" \
        --tabs "<OBJECT_TABS>" --phase deploy

Pipeline (same seams as main, with field/translation delta inside them):

    live sheet fetch
      → static validation (hard gate)
      → EN-column gate (lang=off when no tab has Field Label (EN))
      → org snapshot (existence + fields + CustomObject; COT only for
        translated objects)
      → generate_xml.py (plan-filtered fields; existing CustomObject is
        PATCHed from the org snapshot, never reconstructed)
      → build_manifest.py (members from the plan)
      → deploy.py check-only
      → deploy.py --start
      → live post-deployment verification
      → verified translation state + report refresh

Delta rules: a new object ships the CustomObject, all its deployable fields and
all applicable new translations; an EXISTING object ships only fields the org
does not have. Fields that already exist are never silently redeployed —
attribute drift is reported and needs `--include-drift Obj__c.Field__c`. WIP
rows are ignored; IsDelete rows stay in the separate destructive flow (a
delete-only plan is not an empty no-op).

It runs the whole chain for one OR many object tabs in a single invocation while
keeping EVERY existing safety gate in place:

  * validation gate            — build stops unless validate_sheet reports 0 ERRORs
  * object-existence pre-check  — live, per object (rule sf-object-existence-precheck)
  * check-only dry-run          — writes nothing to the org
  * live post-deploy verify     — verify_deploy.py (Tooling CustomField, FLS-independent)
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
    DEFAULT_LANG, has_translation_columns, normalize_lang, save_sync_state,
    translated_tabs,
)
import build_destructive  # noqa: E402
import verify_deploy  # noqa: E402

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
DESTRUCTIVE = STAGING / "manifest/destructiveChanges.xml"
DESTRUCTIVE_PACKAGE = STAGING / "manifest/destructive_package.xml"
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


def translation_scope(rows: list[dict], objs: list[str], tab_of: dict[str, str],
                      lang: str) -> tuple[str, list[str]]:
    """Decide Translation Workbench work BEFORE the org snapshot.

    Returns (effective_lang, translation_objects). ``off`` / empty objects means
    org_snapshot must not read CustomObjectTranslation — a sandbox without the
    Workbench can still deploy fields from untranslated tabs.
    """
    if lang in ("", "off"):
        return "off", []
    if not has_translation_columns(rows):
        return "off", []
    en_tabs = translated_tabs(rows)
    return lang, [o for o in objs if tab_of.get(o) in en_tabs]


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
    """ERROR count from the validation report.

    Raises instead of returning a sentinel when the report is absent or
    unreadable: "we could not read the gate" is a different situation from
    "the gate counted N errors", and reporting it as a count produced the
    nonsensical "-1 ERROR(s)" message.
    """
    if not VALIDATION_REPORT.exists():
        raise SystemExit(
            f"⛔ validate_sheet.py wrote no report at {VALIDATION_REPORT} — the "
            f"validation gate could not be evaluated, so the build stops. Check "
            f"the validator output above (it exited before writing).")
    try:
        data = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))
        return int(data["counts"]["ERROR"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise SystemExit(f"⛔ validation report {VALIDATION_REPORT} is unreadable "
                         f"({e}) — cannot confirm the gate passed.")


# Attribute drift is no longer a per-object subprocess: org_snapshot.py brings
# every existing object's CustomObject metadata down in the SAME bulk read, and
# plan_deploy.py compares it locally (attr_drift.parse_org_object +
# attr_drift.compute_drift). One authentication, one Metadata call per batch.


def package_parts(plan: dict) -> list[dict]:
    """Every package file the plan says must be deployed, in order.

    A plan bigger than --max-components splits into package.part1..N.xml; the
    plan records that INDEX so the deploy cannot stop after the first file and
    silently drop parts 2..N.
    """
    parts = plan.get("manifestParts") or []
    if not parts and plan.get("manifestMembers"):
        parts = [{"file": "package.xml", "members": plan["manifestMembers"],
                  "components": sum(len(v) for v in plan["manifestMembers"].values())}]
    return [{**part, "path": PACKAGE.parent / Path(part["file"]).name}
            for part in parts]


def deploy_packages(args, parts: list[dict], *, start: bool) -> None:
    """Deploy EVERY package part, in order, through deploy.py."""
    mode = "real deploy" if start else "check-only"
    for i, part in enumerate(parts, 1):
        if not part["path"].exists():
            raise SystemExit(f"⛔ planned package {part['path']} was not written "
                             f"— the manifest and the plan disagree.")
        print(f"      [{mode} {i}/{len(parts)}] {part['file']} "
              f"({part['components']} component(s))")
        cmd = ["python3", str(SCRIPTS / "deploy.py"),
               "--package", str(part["path"]), "--target-org", args.org,
               "--test-level", args.test_level,
               "--validation-report", str(VALIDATION_REPORT),
               "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)]
        if start:
            cmd.append("--start")
        cp = subprocess.run(cmd, env=sf_env(args), text=True)
        if cp.returncode != 0:
            tail = (f" Parts {i + 1}..{len(parts)} were NOT deployed."
                    if start and i < len(parts) else "")
            raise SystemExit(f"⛔ {mode} of {part['file']} failed "
                             f"(exit {cp.returncode}).{tail}")
        if start:
            # never trust the log alone, and never send the next part until this
            # one is confirmed in the org
            first = (LAST_DEPLOY_LOG.read_text(encoding="utf-8").splitlines()
                     or [""])[0]
            if "deploy start" not in first or "--dry-run" in first:
                raise SystemExit(f"❌ deploy log is not a real 'deploy start' "
                                 f"run:\n  {first}")
            verify_part(args, part, i, len(parts))


def verify_part(args, part: dict, i: int, total: int) -> None:
    """Confirm one part landed before the next is sent.

    Scope is the part's exact members, not every planned field on those objects
    — one object can be split across packages, and part 1 must not require
    part 2's fields (or translations) to already be in the org.
    """
    cp = subprocess.run(
        ["python3", str(SCRIPTS / "verify_deploy.py"), "--target-org", args.org,
         "--plan", str(PLAN), "--part-members", json.dumps(part["members"])],
        env=sf_env(args), text=True)
    if cp.returncode != 0:
        raise SystemExit(
            f"⛔ part {i}/{total} ({part['file']}) did NOT fully land — stopping "
            f"before the remaining parts so the org is not left half-updated.")


def destructive_phase(args, plan: dict, *, start: bool) -> None:
    """Route the IsDelete set through the destructive flow.

    IsDelete rows never join the additive package. Destructive XML is built from
    the reviewed plan's ``deleteMembers`` — the live sheet is not re-scanned, so
    the payload cannot diverge from what was shown.
    """
    members = plan.get("deleteMembers") or []
    if not members:
        return
    print(f"\n[deletes] {len(members)} field(s) flagged IsDelete on the sheet")
    for m in members:
        print(f"      🗑️  {m}")
    DESTRUCTIVE.parent.mkdir(parents=True, exist_ok=True)
    api = (plan.get("target") or {}).get("apiVersion") or DEFAULT_API_VERSION
    build_destructive.write_from_members(members, DESTRUCTIVE.parent, api)
    if not DESTRUCTIVE.exists():
        print("      all IsDelete fields are already absent from the org — no-op.")
        return
    if not args.deletes:
        print("      ⚠️  NOT deleted: deleting a field destroys its data, so the "
              "destructive deploy runs only with --deletes.")
        return
    cmd = ["python3", str(SCRIPTS / "deploy.py"),
           "--package", str(DESTRUCTIVE_PACKAGE),
           "--pre-destructive", str(DESTRUCTIVE),
           "--target-org", args.org, "--test-level", args.test_level,
           "--validation-report", str(VALIDATION_REPORT),
           "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)]
    if start:
        cmd.append("--start")
    print(f"\n[deletes] {'DESTRUCTIVE deploy' if start else 'check-only'} of "
          f"{len(members)} field deletion(s)")
    if subprocess.run(cmd, env=sf_env(args), text=True).returncode != 0:
        raise SystemExit("⛔ the destructive deploy failed — the additive deploy "
                         "already landed; investigate before retrying.")
    if not start:
        return
    leftover = verify_deploy.verify_deletes(plan, args.org)
    if leftover:
        raise SystemExit(
            "⛔ IsDelete verification failed — these fields are still in the org "
            f"(Tooling CustomField): {', '.join(leftover)}")
    print(f"      confirmed absent: {len(members)} field(s)")


def build_phase(args, temp_path: Path) -> tuple[dict, dict[str, str]]:
    print("=" * 72)
    print(f"  BUILD PHASE (no org writes)   org={args.org}")
    print("=" * 72)
    (ROOT / ".build").mkdir(parents=True, exist_ok=True)

    # 1) live sheet fetch — all target tabs together (Google creds)
    print("\n[1/7] fetch sheet tabs (live)")
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
    print("\n[2/7] validate (gate: 0 errors, incl. live referenceTo org check)")
    run(["python3", str(SCRIPTS / "validate_sheet.py"),
         "--in", str(temp_path), "--json", str(VALIDATION_REPORT),
         "--target-org", args.org],
        sf_env(args), capture=True, check=False)
    errs = validation_error_count()
    if errs != 0:
        raise SystemExit(f"⛔ validation reports {errs} ERROR(s) — fix the sheet before deploy. "
                         f"See {VALIDATION_REPORT}")
    print("      validation PASS")

    rows_live = json.loads(temp_path.read_text(encoding="utf-8"))
    effective_lang, translation_objects = translation_scope(
        rows_live, objs, tab_of, args.lang)
    if args.lang not in ("", "off") and effective_lang == "off":
        print("      no Field Label (EN) on selected tabs — Translation Workbench skipped")
    elif translation_objects:
        print(f"      translated objects (COT snapshot): {', '.join(translation_objects)}")

    # 3) org snapshot: existence + fields + CustomObject. COT only for tabs
    #    that actually have Field Label (EN); lang=off skips Workbench entirely.
    extra = "" if effective_lang == "off" else ", translations"
    print(f"\n[3/7] org snapshot (existence, fields, object metadata{extra})")
    snap_cmd = ["python3", str(SCRIPTS / "org_snapshot.py"),
                "--org", args.org, "--objects", ",".join(objs),
                "--lang", effective_lang,
                "--on-unavailable", args.on_translation_unavailable,
                "--out", str(SNAPSHOT)]
    if effective_lang not in ("", "off"):
        snap_cmd += ["--translation-objects", ",".join(translation_objects)]
    run(snap_cmd, sf_env(args), capture=True)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    # 4) the PLAN: object/field delta + attribute drift (computed LOCALLY from
    #    the snapshot) + translation delta + the manifest index.
    print("\n[4/7] deployment plan (delta; attribute drift from the snapshot)")
    plan_cmd = ["python3", str(SCRIPTS / "plan_deploy.py"),
                "--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
                "--sheet-id", args.sheet_id, "--tabs", args.tabs,
                "--lang", effective_lang, "--sync-state", str(SYNC_STATE),
                "--max-components", str(args.max_components), "--out", str(PLAN)]
    if args.include_drift:
        plan_cmd += ["--include-drift", args.include_drift]
    if args.new_only:
        plan_cmd.append("--new-only")
    run(plan_cmd, os.environ.copy(), capture=True)
    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    additive_empty = (plan["summary"].get("additiveEmpty")
                      or not plan.get("manifestMembers"))
    if plan["summary"].get("empty"):
        print("\n" + "-" * 72)
        print("  EMPTY DELTA — the org already matches the sheet. Nothing to build,")
        print("  nothing to deploy. (Re-running is a no-op by design.)")
        print("-" * 72)
        return plan, tab_of

    shutil.rmtree(STAGING, ignore_errors=True)
    init_staging(snapshot["target"].get("apiVersion") or DEFAULT_API_VERSION)

    if additive_empty:
        print("\n[5/7] additive package empty — IsDelete-only; skipping field generation")
        destructive_phase(args, plan, start=False)
        print("\n" + "-" * 72)
        print(f"  BUILD OK — no additive package; {plan['summary']['deletes']} "
              f"IsDelete member(s) staged.")
        print(f"  plan: {PLAN}")
        print("  To execute deletions, re-run with --phase deploy --deletes.")
        print("-" * 72)
        return plan, tab_of

    # 5) staged generation — never touches the tracked force-app tree.
    print(f"\n[5/7] generate metadata XML (staged → {STAGING_ROOT})")
    run(["python3", str(SCRIPTS / "generate_xml.py"),
         "--in", str(temp_path), "--source-root", str(STAGING_ROOT),
         "--plan", str(PLAN), "--snapshot", str(SNAPSHOT)],
        os.environ.copy(), capture=True)

    if plan["lang"] == "off":
        print("\n[5b] object translations: skipped (no Field Label (EN) column, "
              "or --lang off)")
    elif plan["translationState"] != "ok":
        print(f"\n[5b] object translations: {plan['translationState']} — "
              f"{plan['translationNote'][:160]}")
    else:
        s = plan.get("summary") or {}
        print(f"\n[5b] English translations ({plan['lang']})")
        print(f"      {s.get('translationsUnchanged', 0)} unchanged  "
              f"+ {s.get('translationsNew', 0)} new  "
              f"~ {s.get('translationsChanged', 0)} changed  "
              f"{s.get('translationsMissing', 0)} missing EN")
        if not plan["translationPackage"]:
            print("      nothing to deploy — sheet EN already matches the org")
        else:
            print(f"      packaging {len(plan['translationPackage'])} "
                  f"translation(s) onto the org's CustomObjectTranslation tree")
            run(["python3", str(SCRIPTS / "generate_object_translation.py"),
                 "--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
                 "--lang", plan["lang"], "--delta", str(PLAN),
                 "--on-unavailable", args.on_translation_unavailable,
                 "--out-root", str(STAGING_TRANSLATIONS)],
                os.environ.copy(), capture=True)

    # 6) ONE manifest built from the PLAN's members (not a directory scan, which
    #    can pick up stale metadata left behind by an earlier run).
    print("\n[6/7] build manifest from the plan")
    run(["python3", str(SCRIPTS / "build_manifest.py"),
         "--plan", str(PLAN), "--project-root", str(STAGING),
         "--max-components", str(args.max_components),
         "--out", str(PACKAGE)],
        os.environ.copy(), capture=True)

    # 7) check-only dry-run of EVERY package the plan produced, from the staged
    #    project — a plan that splits must be validated in full, not just part 1.
    parts = package_parts(plan)
    print(f"\n[7/7] check-only dry-run of {len(parts)} package(s) (writes nothing)")
    deploy_packages(args, parts, start=False)
    destructive_phase(args, plan, start=False)

    print("\n" + "-" * 72)
    print(f"  BUILD OK — planned, staged, packaged into {len(parts)} package(s), "
          f"dry-run PASSED (no org writes).")
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

    if plan["summary"].get("empty"):
        print("\n  EMPTY DELTA — nothing to deploy. The org already matches the "
              "sheet, so no package was built and no org write is attempted.")
        return 0

    additive_empty = (plan["summary"].get("additiveEmpty")
                      or not plan.get("manifestMembers"))
    parts = []
    if not additive_empty:
        # REAL deploy of EVERY planned package part, in order, from the staged
        # project. Each part is verified before the next is sent.
        parts = package_parts(plan)
        print(f"\n[1/5] real deploy of {len(parts)} package(s) (sf project deploy start)")
        deploy_packages(args, parts, start=True)
    else:
        print("\n[1/5] additive package empty — IsDelete-only; skipping field deploy")

    # the IsDelete set is a separate, destructive deploy (never additive)
    print("\n[2/5] IsDelete set (destructive flow)")
    destructive_phase(args, plan, start=True)

    report_objs = objs
    if additive_empty:
        report_objs = sorted({m.split(".", 1)[0]
                              for m in (plan.get("deleteMembers") or []) if "." in m})
        if not (args.deletes and plan.get("deleteMembers")):
            print("\n" + "=" * 72)
            print("  IsDelete package built, not executed (pass --deletes to delete).")
            print("=" * 72)
            return 0
    else:
        # MANDATORY live verification: objects, fields AND every packaged
        # translation (Tooling API for fields, readMetadata for translations)
        print("\n[3/5] live verification (objects, fields and translations)")
        cp = subprocess.run(
            ["python3", str(SCRIPTS / "verify_deploy.py"), "--target-org", args.org,
             "--objects", ",".join(objs), "--plan", str(PLAN)],
            env=sf_env(args), text=True)
        if cp.returncode != 0:
            raise SystemExit("⛔ VERIFICATION FAILED — the deploy did NOT fully land. "
                             "Do not report success; investigate before retrying.")

        print("\n[4/5] persist verified translation state")
        persist_sync_state(plan)

    # report refresh per object (mandatory) — non-fatal if it errors
    print("\n[5/5] refresh deployment report tabs")
    refresh_object_reports(args, report_objs, tab_of)

    print("\n" + "=" * 72)
    if additive_empty:
        print(f"  ✅ DELETE-ONLY DEPLOYED & VERIFIED: "
              f"{len(plan.get('deleteMembers') or [])} field(s) gone from {args.org}")
    else:
        print(f"  ✅ BATCH DEPLOYED & VERIFIED: {len(objs)} object(s), "
              f"{len(parts)} package(s) confirmed in {args.org}")
    print("=" * 72)
    return 0


def refresh_object_reports(args, objs: list[str], tab_of: dict[str, str]) -> None:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Gated batch Sheet->org deploy orchestrator")
    ap.add_argument("--org", required=True, help="target org alias/username")
    ap.add_argument("--tabs", required=True, help="comma-separated object tab names")
    ap.add_argument("--phase", choices=["build", "deploy"], default="build")
    ap.add_argument("--deletes", action="store_true",
                    help="also execute the IsDelete set (DESTRUCTIVE: deleting a "
                         "field destroys its data). Without it the destructive "
                         "package is still built and reported, never deployed.")
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--out", default=str(ROOT / "temp_updates.json"))
    ap.add_argument("--workbook", default="reports/Object_Deployment_Report.xlsx")
    ap.add_argument("--lang", default=DEFAULT_LANG,
                    help="Translation Workbench language for the object "
                         "translations (default en_US; "
                         "pass 'off' to skip translations entirely)")
    ap.add_argument("--include-drift", default="",
                    help="comma-separated Obj__c.Field__c whose ATTRIBUTE DRIFT "
                         "you have reviewed and want redeployed. Drift is never "
                         "packaged automatically: some type changes require "
                         "delete+recreate and destroy the field's data.")
    ap.add_argument("--new-only", action="store_true",
                    help="package NEW English translations only; CHANGED labels "
                         "are reported, not packaged. Default packages both "
                         "(label updates are not destructive).")
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
