#!/usr/bin/env python3
"""
verify_deploy.py — Post-deploy live verification against the target org.

The deploy CLI log is NOT trusted on its own (a stale/dry-run log once reported
"Succeeded" for objects that never landed). This script queries the org live and
confirms, without manual inspection, that:

  1. each expected object exists  (EntityDefinition), and
  2. each expected TI_Fnt_ field of the generated package is present
     (FieldDefinition).

It derives the expected object(s) + field(s) straight from the local generated
metadata under force-app (the exact thing that was packaged), so there is no
hand-maintained list to drift.

Exit code 0 = every object and field confirmed in the org. Non-zero = at least
one missing (the deploy did NOT fully land — treat as FAILURE regardless of any
"Succeeded" text in the deploy log).

Usage:
  python scripts/verify_deploy.py --target-org "ERPDEV01" \
      --objects Sales_IncidentalExpensesDetail__c
  # or verify everything currently generated:
  python scripts/verify_deploy.py --target-org "ERPDEV01" --all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from translation_lib import (  # noqa: E402
    DEFAULT_LANG, MetadataApiError, OrgAuthError, TranslationUnavailable, norm,
    org_auth, parse_object_translation, read_object_translations, soql_name,
)

FIELD_SUFFIX = ".field-meta.xml"


def expected_from_plan(plan_path: Path, only: set[str] | None) -> dict[str, list[str]]:
    """Expected objects + fields taken from the deployment PLAN.

    Verification then checks exactly what was planned and deployed — it cannot
    drift from the package the way a directory scan can (a leftover file would
    be "verified" even though it was never in the manifest).
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for obj in plan.get("objects") or []:
        api = obj["object"]
        if only and api not in only:
            continue
        out[api] = sorted(plan.get("newFields", {}).get(api, []))
    return out


def expected_from_source(source_root: Path, only: set[str] | None) -> dict[str, list[str]]:
    """Map <Obj>__c -> [custom field api names] from generated metadata."""
    out: dict[str, list[str]] = {}
    objects_dir = source_root / "objects"
    if not objects_dir.is_dir():
        return out
    for obj_dir in sorted(p for p in objects_dir.iterdir() if p.is_dir()):
        obj = obj_dir.name
        if only and obj not in only:
            continue
        fields_dir = obj_dir / "fields"
        fields: list[str] = []
        if fields_dir.is_dir():
            for f in sorted(fields_dir.glob(f"*{FIELD_SUFFIX}")):
                fields.append(f.name[: -len(FIELD_SUFFIX)])
        out[obj] = fields
    return out


def soql(query: str, org: str, tooling: bool = False) -> list[dict]:
    cmd = ["sf", "data", "query", "--query", query, "--target-org", org, "--json"]
    if tooling:
        cmd.append("--use-tooling-api")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"❌ could not parse org response for query:\n  {query}\n  stderr: {proc.stderr[:300]}")
    if data.get("status") != 0:
        raise SystemExit(f"❌ org query failed: {data.get('message','unknown')}\n  {query}")
    return data.get("result", {}).get("records", []) or []


def org_has_object(obj: str, org: str) -> bool:
    recs = soql(
        "SELECT QualifiedApiName FROM EntityDefinition "
        f"WHERE QualifiedApiName='{soql_name(obj)}'", org)
    return len(recs) >= 1


def org_fields(obj: str, org: str) -> set[str]:
    """Authoritative field-EXISTENCE check via the Tooling API `CustomField`.

    IMPORTANT: FieldDefinition / `sObject describe` / plain SOQL are all
    Field-Level-Security gated — a field that deployed successfully but whose
    FLS was not granted to the running user is INVISIBLE there and looks
    "missing", producing false failures. The Tooling `CustomField` object lists
    every custom field's metadata regardless of FLS, so it answers the real
    question "did this field deploy?". Tooling DeveloperName omits the __c
    suffix; we re-add it to match the generated file names.
    """
    # NB: do NOT filter by a naming-prefix (e.g. 'TI_Fnt_%'). Some objects carry
    # legacy-named custom fields (QuotationAndWork__c, Receiving__c, …) that would
    # be dropped by a prefix filter, producing a FALSE "missing" failure even
    # though they deployed. We fetch ALL custom fields for the object and let the
    # caller compare against the expected set derived from the generated package.
    recs = soql(
        "SELECT DeveloperName FROM CustomField "
        f"WHERE EntityDefinition.QualifiedApiName='{soql_name(obj)}'",
        org, tooling=True)
    return {f"{r['DeveloperName']}__c" for r in recs}


def packaged_translations(plan: dict) -> list[dict]:
    """The translation entries the plan actually packaged."""
    ids = set(plan.get("translationPackage") or [])
    return [t for t in (plan.get("translations") or []) if t.get("id") in ids]


def verify_translations(plan: dict, org: str) -> list[str]:
    """Read CustomObjectTranslation LIVE and confirm every packaged entry.

    Without this, a translation-only deploy passes verification on the strength
    of its object/field check alone (there are no new fields to check), and the
    sync state then records those hashes as verified. Returns a list of
    failures; empty means every packaged translation is live in the org with
    the value we deployed.
    """
    wanted = packaged_translations(plan)
    if not wanted:
        return []
    lang = plan.get("lang") or DEFAULT_LANG
    objs = sorted({t["component"] for t in wanted})
    auth = org_auth(org)
    live: dict[str, dict] = {}
    for rec in read_object_translations(objs, lang, auth):
        full = norm(rec.findtext("fullName"))
        obj = full.rsplit("-", 1)[0] if full else ""
        if obj:
            live.update(parse_object_translation(rec, obj, lang))

    failures = []
    for t in wanted:
        got = live.get(t["id"])
        if got is None:
            failures.append(f"{t['id']}: not present in the org's {lang} translation")
        elif norm(got.get("translation")) != norm(t.get("translation")):
            failures.append(f"{t['id']}: org={got.get('translation')!r} "
                            f"deployed={t.get('translation')!r}")
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live post-deploy verification")
    ap.add_argument("--target-org", required=True)
    ap.add_argument("--source-root", default="force-app/main/default")
    ap.add_argument("--plan", default="",
                    help="deploy_plan.json — verify exactly the PLANNED members "
                         "(preferred over scanning the generated tree)")
    ap.add_argument("--objects", default="", help="comma-separated <Obj>__c to verify")
    ap.add_argument("--all", action="store_true", help="verify every generated object")
    args = ap.parse_args(argv)

    only = {o.strip() for o in args.objects.split(",") if o.strip()} or None
    if not only and not args.all:
        print("❌ pass --objects <Api,...> or --all")
        return 2

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else {}
    if args.plan:
        expected = expected_from_plan(Path(args.plan), only)
        if not expected:
            print(f"❌ no objects in plan {args.plan}")
            return 2
    else:
        expected = expected_from_source(Path(args.source_root), only)
        if not expected:
            print("❌ no generated objects found under", args.source_root)
            return 2

    print("=" * 72)
    print(f"  LIVE POST-DEPLOY VERIFICATION  (org: {args.target_org})")
    print("=" * 72)

    overall_ok = True
    for obj, fields in expected.items():
        obj_ok = org_has_object(obj, args.target_org)
        present = org_fields(obj, args.target_org) if obj_ok else set()
        missing = [f for f in fields if f not in present]
        status = "OK" if (obj_ok and not missing) else "FAIL"
        if status == "FAIL":
            overall_ok = False
        print(f"\n  [{status}] {obj}")
        print(f"      object in org : {'yes' if obj_ok else 'NO — object missing'}")
        print(f"      fields expected: {len(fields)}  |  present: {len([f for f in fields if f in present])}  |  missing: {len(missing)}")
        for m in missing:
            print(f"        ✗ MISSING field: {m}")

    # translations are a deployed component too — verify them live, not by
    # trusting that the package contained them.
    if plan and plan.get("translationPackage"):
        try:
            failures = verify_translations(plan, args.target_org)
        except (OrgAuthError, TranslationUnavailable, MetadataApiError) as e:
            failures = [f"could not read translations: {e}"]
        count = len(plan["translationPackage"])
        print(f"\n  [{'OK' if not failures else 'FAIL'}] translations ({plan.get('lang')})")
        print(f"      packaged: {count}  |  confirmed: {count - len(failures)}  "
              f"|  failed: {len(failures)}")
        for f in failures:
            print(f"        ✗ {f}")
        if failures:
            overall_ok = False

    print("\n" + "=" * 72)
    print(f"  RESULT: {'ALL CONFIRMED IN ORG' if overall_ok else 'VERIFICATION FAILED — deploy did NOT fully land'}")
    print("=" * 72)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
