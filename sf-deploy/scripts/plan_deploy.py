#!/usr/bin/env python3
"""
plan_deploy.py — compute the deployment PLAN (the delta) before anything is built.

The plan is the single source of scope: generation, the manifest, the check-only
run, the real deploy and the verification all consume `.build/deploy_plan.json`
instead of each re-deciding what is in the deploy. Nothing here writes to the
org; it only reads the sheet rows and the org snapshot.

Delta rules (see sf-deploy-delta-and-blockers):
  * object MISSING  → package the CustomObject + ALL its deployable custom
                      fields + all applicable new translations
  * object EXISTS   → package ONLY fields absent from the org (Tooling
                      CustomField, FLS-independent). Fields present by name are
                      skipped and never silently redeployed; a changed
                      DEFINITION is reported as attribute drift and needs an
                      explicit decision (--include-drift) before it is packaged
  * WIP rows (AE)   → skipped entirely (never named, generated, deployed, deleted)
  * IsDelete rows   → a separate destructive set, never in the additive package

Usage:
  python scripts/plan_deploy.py --rows temp_updates.json \
      --snapshot .build/org_snapshot.json --sheet-id <ID> --tabs "<tabs>" \
      [--lang en_US] [--new-only] [--include-drift Obj__c.Field__c,...] \
      [--out .build/deploy_plan.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attr_drift  # noqa: E402
import build_manifest  # noqa: E402
import org_snapshot  # noqa: E402
from translation_lib import (  # noqa: E402
    BUILD_DIR, CHANGED, CONFLICT, DEFAULT_LANG, DEFAULT_SYNC_STATE, INVALID_LANG,
    MISSING, NEW, PARSE_ERROR, SCHEMA_MISSING, UNCHANGED, apply_new_only, classify,
    entries_from_object_rows, has_translation_columns, is_delete,
    load_sync_state, norm, truthy,
)

DEFAULT_OUT = BUILD_DIR / "deploy_plan.json"


# Standard fields cannot be deployed as CustomField metadata.
def _deployable(api: str) -> bool:
    return api.endswith("__c")


def sheet_scope(rows: list[dict]) -> dict:
    """Split the sheet rows into create / delete / skip sets, per object."""
    scope: dict[str, dict] = {}

    def bucket(obj: str) -> dict:
        return scope.setdefault(obj, {
            "tab": "", "sheetFields": [], "wip": [], "delete": [],
            "standard": [], "blocked": [],
        })

    for r in rows:
        obj = norm(r.get("Object API Name"))
        if not obj:
            continue
        b = bucket(obj)
        if not b["tab"]:
            b["tab"] = norm(r.get("_SheetName"))
        if r.get("_type") == "object_meta":
            # fetch_sheet already applied the WIP / IsDelete gates and parked
            # those API names here; the plan reports them without ever letting
            # them reach the additive package.
            b["wip"] += [norm(x) for x in (r.get("_WipSkipped") or [])]
            b["delete"] += [norm(x) for x in (r.get("_DeleteRequested") or [])
                            if _deployable(norm(x))]
            continue
        api = norm(r.get("Field API Name"))
        # Defensive: honour the flags again if a caller hands us raw rows.
        if truthy(r.get("WIP")):
            b["wip"].append(api or "<unnamed>")
            continue
        if is_delete(r.get("IsDelete")):
            if api and _deployable(api):
                b["delete"].append(api)
            continue
        if not api:
            b["blocked"].append("<blank Field API Name>")
            continue
        if not _deployable(api):
            b["standard"].append(api)
            continue
        if api not in b["sheetFields"]:
            b["sheetFields"].append(api)
    return scope


def compute_drift(rows: list[dict], snapshot: dict) -> dict[str, list[dict]]:
    """Attribute drift for every EXISTING object — computed LOCALLY.

    The CustomObject metadata already came down with the bulk snapshot, so no
    object costs an extra authentication or Metadata API call here. Each entry
    carries its `component`, because the standard Name field is part of the
    CustomObject and must never be packaged as a CustomField member.
    """
    out: dict[str, list[dict]] = {}
    for obj in sorted((snapshot.get("objects") or {})):
        rec = org_snapshot.object_record(snapshot, obj)
        if rec is None:
            continue                      # new object: nothing to drift against
        org = attr_drift.parse_org_object(rec)
        if not org:
            continue
        drift, _insync, warnings = attr_drift.compute_drift(obj, rows, org)
        for w in warnings:
            print(f"⚠️  {obj}: {w}")
        # "not in org yet" is the NEW-field delta, not drift — reporting it as
        # drift would double-count every field the plan is already creating.
        drift = [d for d in drift if d.get("org") != "(absent)"]
        if drift:
            out[obj] = drift
    return out


# A new field can force a change on the OBJECT itself. Packaging only the field
# then fails (or silently under-deploys), so the plan has to carry the object.
def object_update_reasons(obj: str, new_fields: list[str], rows: list[dict],
                          snapshot: dict) -> list[str]:
    rec = org_snapshot.object_record(snapshot, obj)
    org_obj = attr_drift.parse_org_object(rec) or {}
    meta = org_obj.get("__object__") or {}
    planned = set(new_fields)
    reasons: list[str] = []
    rows_for = [r for r in rows
                if norm(r.get("Object API Name")) == obj
                and norm(r.get("Field API Name")) in planned]
    if any(truthy(r.get("Track History")) for r in rows_for) and \
            norm(meta.get("enableHistory")).lower() != "true":
        reasons.append("a new field tracks history; the object needs enableHistory=true")
    if any("masterdetail" in norm(r.get("Data Type")).lower().replace("-", "").replace(" ", "")
           or norm(r.get("Data Type")) in ("主従関係",) for r in rows_for) and \
            norm(meta.get("sharingModel")) != "ControlledByParent":
        reasons.append("a new Master-Detail field forces sharingModel=ControlledByParent")
    return reasons


def build_attribute_expectations(
    objects: list[dict], rows: list[dict], include_drift: set[str],
    object_updates: dict[str, list[str]],
) -> dict[str, dict]:
    """Live-verify targets for approved drift and object-level patches.

    Existence-only verification cannot catch a type/formula/Name/sharing
    update that failed to land. These expectations are compared to a live
    CustomObject read after deploy.
    """
    out: dict[str, dict] = {}
    for o in objects:
        obj = o["object"]
        exp: dict = {}
        reasons = object_updates.get(obj) or []
        oexp: dict = {}
        if any("enableHistory" in r for r in reasons):
            oexp["enableHistory"] = "true"
        if any("ControlledByParent" in r for r in reasons):
            oexp["sharingModel"] = "ControlledByParent"
        if oexp:
            exp["object"] = oexp
        if f"{obj}.Name" in include_drift:
            meta = attr_drift.object_meta_row(obj, rows)
            if meta:
                exp["nameField"] = attr_drift.expected_name(meta)
        fields: dict = {}
        prefix = f"{obj}."
        for key in include_drift:
            if not key.startswith(prefix):
                continue
            field = key[len(prefix):]
            if field == "Name":
                continue
            row = next((r for r in rows
                        if norm(r.get("Object API Name")) == obj
                        and norm(r.get("Field API Name")) == field), None)
            if row:
                fields[field] = attr_drift.expected_from_row(row)
        if fields:
            exp["fields"] = fields
        if exp:
            out[obj] = exp
    return out


def build_plan(rows: list[dict], snapshot: dict, *, sheet_id: str, tabs: str,
               lang: str = DEFAULT_LANG, new_only: bool = False,
               include_drift: set[str] | None = None,
               drift: dict[str, list[dict]] | None = None,
               sync_state: str | Path = DEFAULT_SYNC_STATE,
               conflict_policy: str = "park",
               max_components: int = 9000) -> dict:
    include_drift = include_drift or set()
    drift = drift or {}
    scope = sheet_scope(rows)
    org_fields = org_snapshot.org_schema(snapshot)
    exists = {o: bool(v.get("exists"))
              for o, v in (snapshot.get("objects") or {}).items()}

    objects: list[dict] = []
    object_updates: dict[str, list[str]] = {}
    new_objects: list[str] = []
    new_fields: dict[str, list[str]] = {}
    skipped_fields: dict[str, list[str]] = {}
    skipped_deletes: dict[str, list[str]] = {}
    delete_members: list[str] = []

    for obj in sorted(scope):
        b = scope[obj]
        in_org = exists.get(obj, False)
        have = org_fields.get(obj, set())
        # object missing ⇒ every deployable sheet field is new
        absent = [f for f in b["sheetFields"] if f not in have]
        present = [f for f in b["sheetFields"] if f in have]
        drifted = [d for d in drift.get(obj, [])]
        # a drifted field is only packaged when the operator explicitly opts in
        approved = [d for d in drifted
                    if f"{obj}.{norm(d.get('field'))}" in include_drift]
        redeploy = sorted({f"{obj}.{norm(d.get('field'))}" for d in approved})
        # classify by COMPONENT: the standard Name field is defined inside the
        # CustomObject, so `Obj__c.Name` is not a deployable CustomField member.
        drift_fields = sorted({norm(d.get("field")) for d in approved
                               if d.get("component", "CustomField") == "CustomField"})
        if any(d.get("component") == "CustomObject" for d in approved):
            object_updates.setdefault(obj, []).append(
                "approved drift on the standard Name field (CustomObject metadata)")

        if not in_org:
            new_objects.append(obj)
        if absent:
            new_fields[obj] = sorted(absent)
        if drift_fields:
            new_fields[obj] = sorted(set(new_fields.get(obj, [])) | set(drift_fields))
        if in_org:
            for why in object_update_reasons(obj, new_fields.get(obj, []), rows, snapshot):
                object_updates.setdefault(obj, []).append(why)
        if present:
            skipped_fields[obj] = sorted(present)
        requested_deletes = [f for f in sorted(b["delete"])]
        live_deletes = [f for f in requested_deletes if f in have]
        absent_deletes = [f for f in requested_deletes if f not in have]
        delete_members += [f"{obj}.{f}" for f in live_deletes]
        if absent_deletes:
            skipped_deletes[obj] = absent_deletes

        objects.append({
            "object": obj,
            "tab": b["tab"],
            "exists": in_org,
            "sheetFieldCount": len(b["sheetFields"]),
            "orgFieldCount": len(have),
            "newFields": sorted(absent),
            "existingFields": sorted(present),
            "wipSkipped": sorted(b["wip"]),
            "standardSkipped": sorted(b["standard"]),
            "deleteRequested": requested_deletes,
            "deleteSkippedAbsent": absent_deletes,
            "attributeDrift": drifted,
            "driftApproved": redeploy,
            "objectUpdate": object_updates.get(obj, []),
        })

    planned = {o: set(f) for o, f in new_fields.items()}

    # ---- translations ---------------------------------------------------- #
    translations: list[dict] = []
    translated = has_translation_columns(rows) and lang not in ("", "off")
    tstate = snapshot.get("translationState", "off")
    if translated and tstate == "ok":
        entries = [e for e in entries_from_object_rows(rows, lang=lang)
                   if e.get("language", lang) == lang]
        org_by_id: dict[str, dict] = {}
        from translation_lib import parse_object_translation
        for obj in sorted(scope):
            rec = org_snapshot.translation_record(snapshot, obj)
            if rec is not None:
                org_by_id.update(parse_object_translation(rec, obj, lang))
        sync = load_sync_state(
            sync_state, org_id=(snapshot.get("target") or {}).get("orgId", "")
        ).get("entries") or {}
        translations = classify(entries, org_by_id, sync,
                                conflict_policy=conflict_policy,
                                org_schema=org_fields, planned_fields=planned)
        if new_only:
            translations = apply_new_only(translations)

    # ---- manifest members ------------------------------------------------ #
    members: dict[str, list[str]] = {
        "CustomObject": sorted(set(new_objects) | set(object_updates)),
        "CustomField": sorted(f"{o}.{f}" for o, fs in new_fields.items() for f in fs),
        "CustomObjectTranslation": sorted({
            f"{t['component']}-{lang}" for t in translations if t.get("package")}),
    }
    members = {k: v for k, v in members.items() if v}

    errors = [t for t in translations
              if t.get("code") in {PARSE_ERROR, INVALID_LANG}
              or (t.get("code") == CONFLICT and not t.get("package"))]
    plan = {
        "target": snapshot.get("target") or {},
        "sheet": {"id": sheet_id, "tabs": [t.strip() for t in tabs.split(",") if t.strip()]},
        "lang": lang if translated else "off",
        "translationState": tstate,
        "translationNote": snapshot.get("translationNote", ""),
        "newOnly": new_only,
        "objects": objects,
        "newObjects": sorted(new_objects),
        "objectUpdates": {o: v for o, v in sorted(object_updates.items())},
        "newFields": {o: sorted(f) for o, f in sorted(new_fields.items()) if f},
        "skippedFields": {o: f for o, f in sorted(skipped_fields.items())},
        "attributeDrift": {o: drift[o] for o in sorted(drift)},
        "driftApproved": sorted(include_drift),
        "translations": translations,
        "translationPackage": [t["id"] for t in translations if t.get("package")],
        "deleteMembers": sorted(delete_members),
        "skippedDeletes": {o: f for o, f in sorted(skipped_deletes.items())},
        "attributeExpectations": build_attribute_expectations(
            objects, rows, include_drift, object_updates),
        "manifestMembers": members,
        "manifestParts": build_manifest.plan_parts(members, max_components),
        "validationErrors": [{"id": e["id"], "code": e["code"],
                              "reason": e.get("reason", "")} for e in errors],
    }
    plan["summary"] = {
        "objects": len(objects),
        "newObjects": len(new_objects),
        "newFields": sum(len(v) for v in plan["newFields"].values()),
        "skippedFields": sum(len(v) for v in skipped_fields.values()),
        "driftedFields": sum(len(v) for v in drift.values()),
        "newTranslations": len(plan["translationPackage"]),
        "schemaMissingTranslations": sum(
            1 for t in translations if t.get("code") == SCHEMA_MISSING),
        "conflicts": sum(1 for t in translations if t.get("code") == CONFLICT),
        "translationsNew": sum(1 for t in translations if t.get("code") == NEW),
        "translationsChanged": sum(1 for t in translations if t.get("code") == CHANGED),
        "translationsUnchanged": sum(1 for t in translations if t.get("code") == UNCHANGED),
        "translationsMissing": sum(1 for t in translations if t.get("code") == MISSING),
        "deletes": len(delete_members),
        "skippedDeletes": sum(len(v) for v in skipped_deletes.values()),
        "components": sum(len(v) for v in members.values()),
        "packages": len(build_manifest.plan_parts(members, max_components)),
        "objectUpdates": len(object_updates),
        "additiveEmpty": not members,
        "empty": not members and not delete_members,
    }
    return plan


def report(plan: dict) -> None:
    s = plan["summary"]
    t = plan.get("target") or {}
    print("=" * 78)
    print(f"  DEPLOY PLAN   org={t.get('alias', '?')} ({t.get('orgId') or 'id unknown'})"
          f"   lang={plan['lang']}")
    print("=" * 78)
    for o in plan["objects"]:
        state = "EXISTS" if o["exists"] else "NEW   "
        print(f"  {state} {o['object']}   sheet={o['sheetFieldCount']} "
              f"org={o['orgFieldCount']} → new={len(o['newFields'])} "
              f"skipped={len(o['existingFields'])} drift={len(o['attributeDrift'])} "
              f"wip={len(o['wipSkipped'])} delete={len(o['deleteRequested'])}")
        for why in o.get("objectUpdate", []):
            print(f"         object-update  {why}")
        for d in o["attributeDrift"]:
            mark = "APPROVED" if f"{o['object']}.{d.get('field')}" in plan["driftApproved"] else "REPORT  "
            print(f"         drift[{mark}] {d.get('component', 'CustomField')} "
                  f"{d.get('field')}: {d.get('reason', '')[:70]}")
    print("-" * 78)
    print(f"  new objects {s['newObjects']} · new fields {s['newFields']} · "
          f"skipped {s['skippedFields']} · drift {s['driftedFields']} "
          f"(approved {len(plan['driftApproved'])})")
    print(f"  new translations {s['newTranslations']} · conflicts {s['conflicts']} · "
          f"schema-missing {s['schemaMissingTranslations']} · deletes {s['deletes']}")
    if plan.get("lang") not in ("", "off"):
        print(f"  English translations ({plan['lang']}): "
              f"{s.get('translationsUnchanged', 0)} unchanged · "
              f"{s.get('translationsNew', 0)} new · "
              f"{s.get('translationsChanged', 0)} changed · "
              f"{s.get('translationsMissing', 0)} missing EN"
              + ("  [new-only: changed not packaged]" if plan.get("newOnly") else ""))
    for mtype, vals in plan["manifestMembers"].items():
        print(f"  manifest {mtype:26} {len(vals)}")
    parts = plan.get("manifestParts") or []
    if len(parts) > 1:
        print(f"  → {s['components']} components split into {len(parts)} packages; "
              f"all of them are deployed, in order:")
        for part in parts:
            print(f"      {part['file']:26} {part['components']} component(s)")
    if s.get("empty"):
        print("  → nothing to deploy: the org already matches the sheet.")
    elif s.get("additiveEmpty"):
        print(f"  → no additive package; {s['deletes']} IsDelete field(s) pending "
              f"(run with --deletes to execute).")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compute the deployment plan (delta)")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--snapshot", default=str(org_snapshot.DEFAULT_OUT))
    ap.add_argument("--sheet-id", default="")
    ap.add_argument("--tabs", default="")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--new-only", action="store_true", dest="new_only",
                    help="package NEW_TRANSLATION only; CHANGED English is "
                         "reported, not packaged (default: package both)")
    ap.add_argument("--all-changed", dest="new_only", action="store_false",
                    help="package CHANGED translations too (this is the default)")
    ap.set_defaults(new_only=False)
    ap.add_argument("--include-drift", default="",
                    help="comma-separated Obj__c.Field__c to redeploy despite "
                         "attribute drift (explicit decision, never automatic)")
    ap.add_argument("--max-components", type=int, default=9000,
                    help="component cap per package; larger plans split into "
                         "package.partN.xml and every part is deployed")
    ap.add_argument("--sync-state", default=DEFAULT_SYNC_STATE)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    snapshot = org_snapshot.load(args.snapshot)
    plan = build_plan(
        rows, snapshot, sheet_id=args.sheet_id, tabs=args.tabs, lang=args.lang,
        new_only=args.new_only,
        include_drift={m.strip() for m in args.include_drift.split(",") if m.strip()},
        drift=compute_drift(rows, snapshot),
        sync_state=args.sync_state, max_components=args.max_components)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report(plan)
    print(f"saved → {out}")
    if plan["validationErrors"]:
        for e in plan["validationErrors"]:
            print(f"⛔ {e['code']} {e['id']}: {e['reason']}")
        print("⛔ fix the sheet — unparseable EN cells / invalid language codes / "
              "unresolved translation CONFLICTS are validation errors, not missing "
              "translations.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
