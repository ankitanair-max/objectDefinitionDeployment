#!/usr/bin/env python3
"""
deploy.py — THE deployment command (sheet → plan → package → org).

One public entry point. It computes the delta, generates only what the delta
contains, packages it, deploys every package part, verifies the result live and
records the verified translation state:

    python scripts/deploy.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>" --start

Modes (all of them run the same plan, so a check-only run and the real deploy
can never disagree about scope):

    --plan          plan only: fetch, validate, snapshot, delta. No files built,
                    no org writes.
    (default)       check-only: additionally generate, package and validate the
                    package(s) against the org. Still no org writes.
    --start         the real deploy: every package part in order, then live
                    verification, destructive deletes (with --deletes), verified
                    translation state and the per-object report.

Pipeline (nothing recomputes scope on its own — the plan is the only scope):

    live sheet fetch
      → static validation (hard gate)
      → ONE target-org snapshot   (.build/org_snapshot.json: existence, fields,
                                   CustomObject metadata, translations)
      → attribute drift, computed locally from that snapshot
      → deployment plan / delta   (.build/deploy_plan.json, incl. the manifest
                                   index: every package part that will deploy)
      → staged generation         (.build/staging/force-app)
      → manifest FROM THE PLAN    (package.xml, or package.part1..N.xml)
      → check-only deploy of every part
      → real deploy of every part, each verified before the next
      → destructive deploy of the IsDelete set (--start --deletes)
      → live verification (objects, fields AND translations)
      → verified translation state + report refresh

Delta rules: a new object ships the CustomObject, all its deployable fields and
all applicable new translations; an EXISTING object ships only fields the org
does not have, plus its CustomObject when a new field forces an object-level
change (history tracking, Master-Detail sharing). Fields that already exist are
never silently redeployed — attribute drift is reported and needs
`--include-drift Obj__c.Field__c`. WIP rows are ignored. IsDelete rows never
enter the additive package; they go through the destructive flow.

The pipeline stages are imported as modules (org_snapshot, plan_deploy,
generate_xml, generate_object_translation, build_manifest, sf_deployer,
verify_deploy, build_destructive), not shelled out one by one. The two steps
that keep a separate process are the ones needing different credentials in the
environment: the Google-authenticated sheet fetch and the report refresh.

Re-running with no sheet changes is a no-op by design: the plan comes out
empty, no package is built and no org write is attempted.

Authentication is whatever the Salesforce CLI is already authorized with for
`--org`; `--sf-home` / `--xdg-data-home` are opt-in sandbox overrides.
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
import build_destructive  # noqa: E402
import build_manifest  # noqa: E402
import generate_object_translation  # noqa: E402
import generate_xml  # noqa: E402
import org_snapshot  # noqa: E402
import plan_deploy  # noqa: E402
import sf_deployer  # noqa: E402
import validate_sheet  # noqa: E402
import verify_deploy  # noqa: E402
from translation_lib import (  # noqa: E402
    DEFAULT_LANG, MetadataApiError, OrgAuthError, TranslationUnavailable,
    normalize_lang, save_sync_state,
)

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
# Live Data Dictionary: https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
# Anchored on this file, so the command behaves the same from any cwd.
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
MANIFEST_DIR = STAGING / "manifest"
PACKAGE = MANIFEST_DIR / "package.xml"
DESTRUCTIVE = MANIFEST_DIR / "destructiveChanges.xml"
DESTRUCTIVE_PACKAGE = MANIFEST_DIR / "destructive_package.xml"
LAST_DEPLOY_LOG = ROOT / ".build/last_deploy.log"
DEFAULT_API_VERSION = "60.0"


class DeployError(RuntimeError):
    """A pipeline stage failed; the command stops and reports it."""


# --------------------------------------------------------------------------- #
# environment. Org steps inherit the environment by default, so whatever the
# Salesforce CLI is already authorized with works — laptop, build agent or
# container. `--sf-home` / `--xdg-data-home` are opt-in overrides for sandboxes
# that cannot let the CLI write into the real HOME.
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


class sf_environment:
    """Apply the sf-CLI env overrides around an in-process stage."""

    def __init__(self, args):
        self.env = sf_env(args)

    def __enter__(self):
        self.saved = os.environ.copy()
        os.environ.update(self.env)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)
        return False


def stage(module, argv: list[str], what: str) -> None:
    """Run one pipeline module in-process; a non-zero return stops the run."""
    print(f"  · {module.__name__} {' '.join(argv)}")
    rc = module.main(argv)
    if rc != 0:
        raise DeployError(f"{what} failed (exit {rc}) — see the output above.")


def init_staging(api_version: str = DEFAULT_API_VERSION) -> None:
    """A minimal SFDX project around the staged source, so the CLI resolves the
    manifest against the STAGED tree and not the working copy."""
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    (STAGING / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app", "default": True}],
        "namespace": "",
        "sfdcLoginUrl": "https://test.salesforce.com",
        "sourceApiVersion": api_version,
    }, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# sheet input
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
        raise DeployError(
            f"validate_sheet.py wrote no report at {VALIDATION_REPORT} — the "
            f"validation gate could not be evaluated, so the run stops. Check "
            f"the validator output above (it exited before writing).")
    try:
        data = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))
        return int(data["counts"]["ERROR"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise DeployError(f"validation report {VALIDATION_REPORT} is unreadable "
                          f"({e}) — cannot confirm the gate passed.")


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def plan_stage(args, temp_path: Path) -> tuple[dict, dict[str, str]]:
    """Sheet → validation → ONE org snapshot → local drift → the plan."""
    print("=" * 72)
    print(f"  PLAN   org={args.org}   tabs={args.tabs}")
    print("=" * 72)
    (ROOT / ".build").mkdir(parents=True, exist_ok=True)

    print("\n[1/4] fetch sheet tabs (live)")
    cp = subprocess.run(
        ["python3", str(SCRIPTS / "fetch_sheet.py"),
         "--spreadsheet-id", args.sheet_id, "--tabs", args.tabs,
         "--out", str(temp_path)],
        env=google_env(args), text=True)
    if cp.returncode != 0:
        raise DeployError("fetch_sheet.py failed — the sheet is the source of "
                          "truth, so the run stops rather than using stale rows.")
    tab_of = patch_object_apis(temp_path)
    objs = object_list(temp_path)
    if not objs:
        raise DeployError("no objects parsed from the sheet — check --tabs names.")
    print(f"      objects: {', '.join(objs)}")

    # validation gate (with the LIVE referenceTo org-existence check)
    print("\n[2/4] validate (gate: 0 errors, incl. live referenceTo org check)")
    with sf_environment(args):
        validate_sheet.main(["--in", str(temp_path), "--json", str(VALIDATION_REPORT),
                             "--target-org", args.org])
    errs = validation_error_count()
    if errs != 0:
        raise DeployError(f"validation reports {errs} ERROR(s) — fix the sheet "
                          f"before deploying. See {VALIDATION_REPORT}")
    print("      validation PASS")

    # ONE bulk org read: existence + fields + CustomObject metadata + translations
    print("\n[3/4] org snapshot (one bulk read: existence, fields, object "
          "metadata, translations)")
    with sf_environment(args):
        stage(org_snapshot,
              ["--org", args.org, "--objects", ",".join(objs), "--lang", args.lang,
               "--on-unavailable", args.on_translation_unavailable,
               "--out", str(SNAPSHOT)],
              "org snapshot")
    snapshot = org_snapshot.load(SNAPSHOT)

    # the PLAN: object/field delta + local attribute drift + translation delta
    # + the manifest index (which package parts exist).
    print("\n[4/4] deployment plan (delta; attribute drift computed from the snapshot)")
    plan_argv = ["--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
                 "--sheet-id", args.sheet_id, "--tabs", args.tabs,
                 "--lang", args.lang, "--sync-state", str(SYNC_STATE),
                 "--max-components", str(args.max_components), "--out", str(PLAN)]
    if args.include_drift:
        plan_argv += ["--include-drift", args.include_drift]
    stage(plan_deploy, plan_argv, "deployment plan")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    return plan, tab_of


def generate_stage(args, temp_path: Path, plan: dict, snapshot: dict) -> list[dict]:
    """Staged generation + the manifest. Returns the manifest index (parts)."""
    print(f"\n[build] generate metadata XML (staged → {STAGING_ROOT})")
    shutil.rmtree(STAGING, ignore_errors=True)
    init_staging((snapshot.get("target") or {}).get("apiVersion") or DEFAULT_API_VERSION)
    stage(generate_xml,
          ["--in", str(temp_path), "--source-root", str(STAGING_ROOT),
           "--plan", str(PLAN)], "metadata generation")

    if plan["lang"] == "off":
        print("[build] object translations: disabled (--lang off)")
    elif plan["translationState"] != "ok":
        print(f"[build] object translations: {plan['translationState']} — "
              f"{plan['translationNote'][:160]}")
    elif not plan["translationPackage"]:
        print("[build] object translations: nothing new to translate")
    else:
        print(f"[build] object translations ({plan['lang']}, "
              f"{len(plan['translationPackage'])} new) — patched onto the org's "
              f"own translation tree")
        stage(generate_object_translation,
              ["--rows", str(temp_path), "--snapshot", str(SNAPSHOT),
               "--lang", plan["lang"], "--delta", str(PLAN),
               "--on-unavailable", args.on_translation_unavailable,
               "--out-root", str(STAGING_TRANSLATIONS)], "translation generation")

    print("\n[build] manifest from the plan")
    stage(build_manifest,
          ["--plan", str(PLAN), "--project-root", str(STAGING),
           "--max-components", str(args.max_components), "--out", str(PACKAGE)],
          "manifest build")
    parts = package_parts(plan)
    for p in parts:
        if not p["path"].exists():
            raise DeployError(f"planned package {p['path']} was not written — "
                              f"the manifest and the plan disagree.")
    return parts


def package_parts(plan: dict) -> list[dict]:
    """Every package file the plan says must be deployed, in order."""
    parts = plan.get("manifestParts") or []
    if not parts and plan.get("manifestMembers"):
        parts = [{"file": "package.xml", "members": plan["manifestMembers"],
                  "components": sum(len(v) for v in plan["manifestMembers"].values())}]
    return [{**p, "path": MANIFEST_DIR / Path(p["file"]).name} for p in parts]


def deploy_packages(args, parts: list[dict], *, start: bool) -> None:
    """Deploy EVERY package part, in order.

    A split plan that deploys only `package.xml` silently drops every component
    in parts 2..N — the whole point of splitting is that all parts ship.
    """
    mode = "real deploy" if start else "check-only"
    for i, part in enumerate(parts, 1):
        label = f"{part['file']} ({part['components']} component(s))"
        print(f"\n[{mode} {i}/{len(parts)}] {label}")
        argv = ["--package", str(part["path"]), "--target-org", args.org,
                "--test-level", args.test_level,
                "--validation-report", str(VALIDATION_REPORT),
                "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)]
        if start:
            argv.append("--start")
        with sf_environment(args):
            rc = sf_deployer.main(argv)
        if rc != 0:
            raise DeployError(
                f"{mode} of {part['file']} failed (exit {rc}). "
                + (f"Parts {i + 1}..{len(parts)} were NOT deployed."
                   if start and i < len(parts) else ""))
        if start:
            first = (LAST_DEPLOY_LOG.read_text(encoding="utf-8").splitlines() or [""])[0]
            if "deploy start" not in first or "--dry-run" in first:
                raise DeployError(f"deploy log is not a real 'deploy start' run:\n  {first}")
            verify_part(args, part, i, len(parts))


def verify_part(args, part: dict, i: int, total: int) -> None:
    """Confirm a part landed before the next one is sent."""
    objs = sorted({m.split(".")[0] for vals in part["members"].values() for m in vals})
    objs = [o.split("-")[0] for o in objs]
    with sf_environment(args):
        rc = verify_deploy.main(["--target-org", args.org, "--plan", str(PLAN),
                                 "--objects", ",".join(sorted(set(objs)))])
    if rc != 0:
        raise DeployError(
            f"part {i}/{total} ({part['file']}) did NOT fully land — stopping "
            f"before the remaining parts so the org is not left half-updated.")


def destructive_stage(args, plan: dict, *, start: bool) -> None:
    """Route the IsDelete set through the destructive flow.

    IsDelete rows never join the additive package. They are built into
    destructiveChanges.xml here and — because deleting a field also destroys
    its data — only executed when `--deletes` says so explicitly.
    """
    members = plan.get("deleteMembers") or []
    if not members:
        return
    print(f"\n[deletes] {len(members)} field(s) flagged IsDelete on the sheet")
    for m in members:
        print(f"      🗑️  {m}")
    tabs = ",".join(plan.get("sheet", {}).get("tabs") or [])
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    with sf_environment(args):
        rc = build_destructive.main(
            ["--spreadsheet-id", plan.get("sheet", {}).get("id", ""), "--tabs", tabs,
             "--target-org", args.org, "--out-dir", str(MANIFEST_DIR)])
    if rc != 0:
        raise DeployError("could not build destructiveChanges.xml for the "
                          "IsDelete set.")
    if not DESTRUCTIVE.exists():
        print("      all IsDelete fields are already absent from the org — no-op.")
        return
    if not args.deletes:
        print("      ⚠️  NOT deleted: deleting a field destroys its data, so the "
              "destructive deploy runs only with --deletes.")
        return
    argv = ["--package", str(DESTRUCTIVE_PACKAGE),
            "--pre-destructive", str(DESTRUCTIVE),
            "--target-org", args.org, "--test-level", args.test_level,
            "--validation-report", str(VALIDATION_REPORT),
            "--project-dir", str(STAGING), "--log", str(LAST_DEPLOY_LOG)]
    if start:
        argv.append("--start")
    print(f"\n[deletes] {'DESTRUCTIVE deploy' if start else 'check-only'} "
          f"of {len(members)} field deletion(s)")
    with sf_environment(args):
        rc = sf_deployer.main(argv)
    if rc != 0:
        raise DeployError("the destructive deploy failed — the additive deploy "
                          "already landed; investigate before retrying.")


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


def report_stage(args, plan: dict, tab_of: dict[str, str]) -> None:
    """Refresh the per-object report tab (mandatory, non-fatal if it errors)."""
    for o in [obj["object"] for obj in plan["objects"]]:
        cmd = ["python3", str(SCRIPTS / "build_object_report.py"),
               "--object-api", o, "--sheet-tab", tab_of.get(o, ""),
               "--sheet-id", args.sheet_id, "--target-org", args.org,
               "--fields-dir", str(STAGING_ROOT / "objects" / o / "fields"),
               "--workbook", args.workbook]
        if args.sf_home:
            cmd += ["--sf-home", args.sf_home]
        if args.xdg_data_home:
            cmd += ["--xdg-data-home", args.xdg_data_home]
        if subprocess.run(cmd, env=google_env(args), text=True).returncode != 0:
            print(f"      ⚠️  report refresh failed for {o} (deploy still verified).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deploy sheet-defined objects, fields and translations")
    ap.add_argument("--org", required=True, help="target org alias/username")
    ap.add_argument("--tabs", required=True, help="comma-separated object tab names")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", dest="plan_only", action="store_true",
                      help="compute the delta only: no files built, no org writes")
    mode.add_argument("--check-only", action="store_true",
                      help="default: build + validate the package(s) against the "
                           "org without writing")
    mode.add_argument("--start", action="store_true",
                      help="REAL deploy: every package part, verified live")
    ap.add_argument("--deletes", action="store_true",
                    help="also execute the IsDelete set (destructive: deleting a "
                         "field destroys its data)")
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--out", default=str(ROOT / "temp_updates.json"),
                    help="where the fetched sheet rows are written")
    ap.add_argument("--workbook", default="reports/Object_Deployment_Report.xlsx")
    ap.add_argument("--lang", default=DEFAULT_LANG,
                    help="Translation Workbench language for the object "
                         "translations (default en_US; 'off' to skip them)")
    ap.add_argument("--include-drift", default="",
                    help="comma-separated Obj__c.Field__c whose ATTRIBUTE DRIFT "
                         "you have reviewed and want redeployed. Drift is never "
                         "packaged automatically: some type changes require "
                         "delete+recreate and destroy the field's data.")
    ap.add_argument("--max-components", type=int, default=9000,
                    help="component cap per package; larger plans split "
                         "deterministically into package.partN.xml and EVERY "
                         "part is deployed, in order")
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
    args = ap.parse_args(argv)

    if args.lang != "off":
        lang, lang_err = normalize_lang(args.lang)
        if lang_err:
            print(f"❌ --lang: {lang_err} — use a Salesforce Translation Workbench "
                  f"code such as en_US, ja, zh_CN, or 'off'.")
            return 2
        args.lang = lang

    temp_path = Path(args.out)
    try:
        plan, tab_of = plan_stage(args, temp_path)

        if plan["summary"]["empty"] and not plan.get("deleteMembers"):
            print("\n  EMPTY DELTA — the org already matches the sheet. Nothing to "
                  "build, nothing to deploy. (Re-running is a no-op by design.)")
            return 0
        if args.plan_only:
            print(f"\n  plan only → {PLAN}. Re-run with --start to deploy it.")
            return 0

        snapshot = org_snapshot.load(SNAPSHOT)
        parts = generate_stage(args, temp_path, plan, snapshot) \
            if not plan["summary"]["empty"] else []

        if not args.start:
            deploy_packages(args, parts, start=False)
            destructive_stage(args, plan, start=False)
            print("\n" + "-" * 72)
            print(f"  CHECK-ONLY OK — {len(parts)} package(s) validated against "
                  f"{args.org}; nothing was written.")
            print(f"  plan: {PLAN}")
            print("  To deploy for real, re-run with --start (same --tabs/--org).")
            print("-" * 72)
            return 0

        # real deploy: every part, each verified before the next
        deploy_packages(args, parts, start=True)
        destructive_stage(args, plan, start=True)

        print("\n[verify] live verification (objects, fields AND translations)")
        objs = ",".join(o["object"] for o in plan["objects"])
        with sf_environment(args):
            rc = verify_deploy.main(["--target-org", args.org, "--plan", str(PLAN),
                                     "--objects", objs])
        if rc != 0:
            raise DeployError("VERIFICATION FAILED — the deploy did NOT fully "
                              "land. Do not report success; investigate first.")

        print("\n[state] persist verified translation state")
        persist_sync_state(plan)

        print("\n[report] refresh deployment report tabs")
        report_stage(args, plan, tab_of)

        print("\n" + "=" * 72)
        print(f"  ✅ DEPLOYED & VERIFIED: {len(plan['objects'])} object(s), "
              f"{len(parts)} package(s) confirmed in {args.org}")
        print("=" * 72)
        return 0
    except DeployError as e:
        print(f"\n⛔ {e}")
        return 1
    except (OrgAuthError, TranslationUnavailable, MetadataApiError) as e:
        print(f"\n⛔ {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
