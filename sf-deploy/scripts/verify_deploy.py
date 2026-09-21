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

FIELD_SUFFIX = ".field-meta.xml"


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
        f"SELECT QualifiedApiName FROM EntityDefinition WHERE QualifiedApiName='{obj}'", org)
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
        f"WHERE EntityDefinition.QualifiedApiName='{obj}'",
        org, tooling=True)
    return {f"{r['DeveloperName']}__c" for r in recs}


def org_translation_labels(obj: str, org: str, lang: str = "en_US") -> dict:
    """Live CustomObjectTranslation labels for exact-English verification."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from translate_enrich import jsonable_translation, load_token, parse_object_translation_el, read_metadata
    tokinfo = load_token(org)
    recs = read_metadata(
        "CustomObjectTranslation", [f"{obj}-{lang}"],
        tokinfo["accessToken"], tokinfo["instanceUrl"].rstrip("/"), tokinfo["apiVersion"])
    if not recs:
        return {}
    rec, raw = recs[0]
    model = jsonable_translation(parse_object_translation_el(rec, obj, lang, raw_xml=raw))
    labels = {"__object__": model.get("object_label") or "", "Name": model.get("name_field_label") or ""}
    for name, f in (model.get("fields") or {}).items():
        labels[name] = f.get("label") or ""
    return labels


def verify_translations(plan: dict, org: str) -> bool:
    """Compare EVERY in-scope sheet English label to the live org.

    Name-existence is not enough: an existing field whose Field Label (EN) or
    provenance changed must still match. Checking only packaged rows is how a
    later Google/manual EN edit was reported 'complete' while the org stayed on
    the previous translation.
    """
    skip_codes = {"MISSING_TRANSLATION", "SCHEMA_MISSING", "WIP", "ISDELETE"}
    entries = []
    for t in plan.get("translations") or []:
        if t.get("code") in skip_codes:
            continue
        if not (t.get("translation") or "").strip():
            continue
        entries.append(t)
    if not entries:
        print("      (no sheet English to verify)")
        return True
    ok = True
    by_obj: dict[str, list] = {}
    for t in entries:
        by_obj.setdefault(t["component"], []).append(t)
    for obj, obj_entries in by_obj.items():
        live = org_translation_labels(obj, org, plan.get("language") or "en_US")
        print(f"\n  [EN] {obj}")
        for t in obj_entries:
            expected = (t.get("translation") or "").strip()
            if t["kind"] == "ObjectLabel":
                actual = live.get("__object__") or ""
                loc = "object label"
            elif t["kind"] == "NameField" or t.get("key") == "Name":
                actual = live.get("Name") or ""
                loc = "Name"
            else:
                actual = live.get(t["key"]) or ""
                loc = t["key"]
            tag = "pkg" if t.get("package") else (t.get("code") or "")
            if actual != expected:
                ok = False
                print(f"      ✗ {loc}: org {actual!r} != sheet {expected!r}  [{tag}]")
            else:
                print(f"      ✓ {loc}: {expected!r}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Live post-deploy verification")
    ap.add_argument("--target-org", required=True)
    ap.add_argument("--source-root", default="force-app/main/default")
    ap.add_argument("--objects", default="", help="comma-separated <Obj>__c to verify")
    ap.add_argument("--all", action="store_true", help="verify every generated object")
    ap.add_argument("--plan", default="", help="deploy plan JSON (exact English verification)")
    ap.add_argument("--org-snapshot", default="")
    args = ap.parse_args()

    only = {o.strip() for o in args.objects.split(",") if o.strip()} or None
    if not only and not args.all:
        print("❌ pass --objects <Api,...> or --all")
        return 2

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

    trans_ok = True
    if args.plan:
        plan_path = Path(args.plan)
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            trans_ok = verify_translations(plan, args.target_org)
            if not trans_ok:
                overall_ok = False

    print("\n" + "=" * 72)
    print(f"  RESULT: {'ALL CONFIRMED IN ORG' if overall_ok else 'VERIFICATION FAILED — deploy did NOT fully land'}")
    print("=" * 72)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
