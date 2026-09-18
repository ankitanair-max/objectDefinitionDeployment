#!/usr/bin/env python3
"""
verify_deploy.py — Post-deploy live verification against the target org.

The deploy CLI log is NOT trusted on its own (a stale/dry-run log once reported
"Succeeded" for objects that never landed). This script queries the org live and
confirms, without manual inspection, that:

  1. each expected object exists  (EntityDefinition), and
  2. each expected custom field of the generated package is present
     (Tooling ``CustomField`` — FLS-independent; never FieldDefinition).
  3. each packaged CustomObjectTranslation entry is live in the org
     (filtered to ``--objects`` during per-part verification).
  4. approved attribute updates (field type/formula/reference/picklist,
     standard Name type/displayFormat, object history/sharing) match the
     live CustomObject metadata — existence of the API name is not enough.

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
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attr_drift  # noqa: E402
import org_snapshot  # noqa: E402
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


def expected_from_part_members(members: dict) -> dict[str, list[str]]:
    """Expected objects + fields from ONE package part's exact members.

    Object-scoped verification is wrong when one object is split across parts:
    part 1 would try to confirm fields that only land in part 2. CustomField
    members are ``Obj__c.Field__c``; a CustomObject member with no fields still
    records the object so we confirm it exists.
    """
    out: dict[str, list[str]] = {}
    for m in members.get("CustomObject") or []:
        out.setdefault(str(m), [])
    for m in members.get("CustomField") or []:
        if "." not in str(m):
            continue
        obj, field = str(m).split(".", 1)
        out.setdefault(obj, []).append(field)
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


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


def verify_translations(plan: dict, org: str,
                        objects: set[str] | None = None,
                        translation_members: set[str] | None = None) -> list[str]:
    """Read CustomObjectTranslation LIVE and confirm every packaged entry.

    Without this, a translation-only deploy passes verification on the strength
    of its object/field check alone (there are no new fields to check), and the
    sync state then records those hashes as verified. Returns a list of
    failures; empty means every packaged translation is live in the org with
    the value we deployed.

    ``objects`` restricts the check to named objects. ``translation_members``
    (``Obj__c-en_US`` values from the current package part) is stricter and
    must be used for per-part verification so later parts are not required yet.
    """
    wanted = packaged_translations(plan)
    if objects is not None:
        wanted = [t for t in wanted if t.get("component") in objects]
    if translation_members is not None:
        lang = plan.get("lang") or DEFAULT_LANG
        wanted = [t for t in wanted
                  if f"{t.get('component')}-{lang}" in translation_members]
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


def verify_deletes(plan: dict, org: str) -> list[str]:
    """Tooling CustomField members that IsDelete asked to remove but are still live.

    An empty list means every planned deletion is actually absent. Never infer
    this from the destructive deploy's exit code.
    """
    leftover: list[str] = []
    by_obj: dict[str, list[str]] = {}
    for m in plan.get("deleteMembers") or []:
        if "." not in str(m):
            continue
        obj, field = str(m).split(".", 1)
        by_obj.setdefault(obj, []).append(field)
    for obj, fields in by_obj.items():
        present = org_fields(obj, org)
        leftover.extend(f"{obj}.{f}" for f in fields if f in present)
    return leftover


def _norm_formula(body: str) -> str:
    import re
    return re.sub(r"\s+", "", str(body or "")).lower()


def _field_attr_failures(obj: str, field: str, expect: dict, om: dict) -> list[str]:
    fails: list[str] = []
    loc = f"{obj}.{field}"
    if not om:
        return [f"{loc}: field missing from live CustomObject metadata"]
    exp_type = expect.get("type") or ""
    got_type = om.get("type") or ""
    if exp_type and got_type != exp_type:
        fails.append(f"{loc}: type expected={exp_type} org={got_type or '(none)'}")
    if expect.get("formula"):
        if not (om.get("formula") or "").strip():
            fails.append(f"{loc}: expected a formula body, org is not a formula")
        elif expect.get("formulaBody") and _norm_formula(expect["formulaBody"]) != _norm_formula(om.get("formula")):
            fails.append(f"{loc}: formula body did not land")
    elif (om.get("formula") or "").strip() and expect.get("formula") is False:
        fails.append(f"{loc}: expected non-formula, org still has a formula")
    if expect.get("referenceTo") and (om.get("referenceTo") or "") != expect["referenceTo"]:
        fails.append(
            f"{loc}: referenceTo expected={expect['referenceTo']} "
            f"org={om.get('referenceTo') or '(none)'}")
    if expect.get("picklist"):
        got = list(om.get("_picklist") or [])
        if set(expect["picklist"]) != set(got):
            fails.append(
                f"{loc}: picklist expected={expect['picklist']} org={got}")
    return fails


def verify_attribute_updates(plan: dict, org: str,
                             members: dict | None = None) -> list[str]:
    """Confirm approved definition updates against live CustomObject metadata.

    Name-existence is not enough: a type/formula/reference/picklist change, a
    standard Name type/displayFormat change, or an object history/sharing
    patch can all exist-as-name while the org still has the old definition.
    """
    expectations = plan.get("attributeExpectations") or {}
    if not expectations:
        return []

    field_by_obj: dict[str, set[str]] = {}
    part_objects: set[str] | None = None
    if members is not None:
        part_objects = set()
        for m in members.get("CustomObject") or []:
            part_objects.add(str(m))
        for m in members.get("CustomField") or []:
            if "." not in str(m):
                continue
            o, f = str(m).split(".", 1)
            part_objects.add(o)
            field_by_obj.setdefault(o, set()).add(f)
        objs = [o for o in expectations if o in part_objects]
    else:
        objs = list(expectations)

    if not objs:
        return []

    auth = org_auth(org)
    xmls = org_snapshot.object_snapshot(sorted(objs), auth)
    failures: list[str] = []
    for obj in sorted(objs):
        xml = xmls.get(obj)
        if not xml:
            failures.append(f"{obj}: live CustomObject metadata was not returned")
            continue
        parsed = attr_drift.parse_org_object(ET.fromstring(xml)) or {}
        exp = expectations.get(obj) or {}
        check_object = members is None or obj in set(members.get("CustomObject") or [])
        if check_object:
            ometa = parsed.get("__object__") or {}
            for key, want in (exp.get("object") or {}).items():
                got = (ometa.get(key) or "").strip()
                if str(got).lower() != str(want).lower():
                    failures.append(
                        f"{obj}.{key}: expected={want} org={got or '(none)'}")
            nexp = exp.get("nameField") or {}
            if nexp:
                onf = parsed.get("__nameField__") or {}
                want_type = nexp.get("type") or ""
                got_type = onf.get("type") or ""
                if want_type and want_type.lower() != got_type.lower():
                    failures.append(
                        f"{obj}.Name type: expected={want_type} org={got_type or '(none)'}")
                if "displayFormat" in nexp:
                    want_df = nexp.get("displayFormat") or ""
                    got_df = onf.get("displayFormat") or ""
                    if want_df != got_df:
                        failures.append(
                            f"{obj}.Name displayFormat: expected={want_df!r} org={got_df!r}")
        fields_to_check = set((exp.get("fields") or {}))
        if members is not None:
            fields_to_check &= field_by_obj.get(obj, set())
        for field in sorted(fields_to_check):
            failures.extend(_field_attr_failures(
                obj, field, exp["fields"][field], parsed.get(field) or {}))
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
    ap.add_argument("--part-members", default="",
                    help="JSON of THIS package part's members; verification is "
                         "limited to those CustomField / CustomObjectTranslation "
                         "entries instead of every planned field on the object")
    args = ap.parse_args(argv)

    only = {o.strip() for o in args.objects.split(",") if o.strip()} or None
    part_members = json.loads(args.part_members) if args.part_members else None
    if not only and not args.all and part_members is None:
        print("❌ pass --objects <Api,...>, --all, or --part-members")
        return 2

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else {}
    cot_members: set[str] | None = None
    if part_members is not None:
        expected = expected_from_part_members(part_members)
        cot_members = set(part_members.get("CustomObjectTranslation") or [])
        if not expected and not cot_members:
            print("❌ --part-members has no CustomObject/CustomField/translation entries")
            return 2
    elif args.plan:
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
    if plan and plan.get("translationPackage") and cot_members != set():
        try:
            if cot_members is not None:
                failures = verify_translations(
                    plan, args.target_org, translation_members=cot_members)
            else:
                failures = verify_translations(
                    plan, args.target_org, objects=only)
        except (OrgAuthError, TranslationUnavailable, MetadataApiError) as e:
            failures = [f"could not read translations: {e}"]
        count = (len(cot_members) if cot_members is not None
                 else len(plan["translationPackage"]))
        print(f"\n  [{'OK' if not failures else 'FAIL'}] translations ({plan.get('lang')})")
        print(f"      packaged: {count}  |  confirmed: {count - len(failures)}  "
              f"|  failed: {len(failures)}")
        for f in failures:
            print(f"        ✗ {f}")
        if failures:
            overall_ok = False

    # approved type/formula/Name/object-level updates — existence is not enough
    if plan.get("attributeExpectations"):
        try:
            attr_failures = verify_attribute_updates(
                plan, args.target_org, members=part_members)
        except (OrgAuthError, MetadataApiError) as e:
            attr_failures = [f"could not read CustomObject metadata: {e}"]
        print(f"\n  [{'OK' if not attr_failures else 'FAIL'}] attribute updates")
        print(f"      objects with expectations: "
              f"{len(plan.get('attributeExpectations') or {})}  |  "
              f"mismatches: {len(attr_failures)}")
        for f in attr_failures:
            print(f"        ✗ {f}")
        if attr_failures:
            overall_ok = False

    print("\n" + "=" * 72)
    print(f"  RESULT: {'ALL CONFIRMED IN ORG' if overall_ok else 'VERIFICATION FAILED — deploy did NOT fully land'}")
    print("=" * 72)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
