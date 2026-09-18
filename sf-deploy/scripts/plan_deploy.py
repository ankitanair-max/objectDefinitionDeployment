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
import org_snapshot  # noqa: E402
from translation_lib import (  # noqa: E402
    BUILD_DIR, CONFLICT, DEFAULT_LANG, DEFAULT_SYNC_STATE, INVALID_LANG, NEW,
    PARSE_ERROR, SCHEMA_MISSING, apply_new_only, classify,
    entries_from_object_rows, has_translation_columns, is_delete,
    load_sync_state, norm, truthy,
)

DEFAULT_OUT = BUILD_DIR / "deploy_plan.json"


class DriftReportError(RuntimeError):
    """An attribute-drift report exists but cannot be read."""


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


def load_drift(objs: list[str], drift_dir: Path) -> dict[str, list[dict]]:
    """Read attr_drift.py output per object (already produced by the caller)."""
    out: dict[str, list[dict]] = {}
    for o in objs:
        p = drift_dir / f"attr_drift_{o}.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise DriftReportError(
                f"unreadable drift report {p}: {e} — a drift check that cannot "
                f"be read is NOT a clean one") from e
        if data:
            out[o] = data
    return out


def build_plan(rows: list[dict], snapshot: dict, *, sheet_id: str, tabs: str,
               lang: str = DEFAULT_LANG, new_only: bool = True,
               include_drift: set[str] | None = None,
               drift: dict[str, list[dict]] | None = None,
               sync_state: str | Path = DEFAULT_SYNC_STATE,
               conflict_policy: str = "park") -> dict:
    include_drift = include_drift or set()
    drift = drift or {}
    scope = sheet_scope(rows)
    org_fields = org_snapshot.org_schema(snapshot)
    exists = {o: bool(v.get("exists"))
              for o, v in (snapshot.get("objects") or {}).items()}

    objects: list[dict] = []
    new_objects: list[str] = []
    new_fields: dict[str, list[str]] = {}
    skipped_fields: dict[str, list[str]] = {}
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
        redeploy = sorted({f"{obj}.{norm(d.get('field'))}" for d in drifted
                           if f"{obj}.{norm(d.get('field'))}" in include_drift})

        if not in_org:
            new_objects.append(obj)
        if absent:
            new_fields[obj] = sorted(absent)
        if present:
            skipped_fields[obj] = sorted(present)
        delete_members += [f"{obj}.{f}" for f in sorted(b["delete"])]

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
            "deleteRequested": sorted(b["delete"]),
            "attributeDrift": drifted,
            "driftApproved": redeploy,
        })

    planned = {o: set(f) for o, f in new_fields.items()}
    for member in include_drift:
        if "." in member:
            o, f = member.split(".", 1)
            planned.setdefault(o, set()).add(f)
            new_fields.setdefault(o, [])
            if f not in new_fields[o]:
                new_fields[o] = sorted(new_fields[o] + [f])

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
        "CustomObject": sorted(new_objects),
        "CustomField": sorted(f"{o}.{f}" for o, fs in new_fields.items() for f in fs),
        "CustomObjectTranslation": sorted({
            f"{t['component']}-{lang}" for t in translations if t.get("package")}),
    }
    members = {k: v for k, v in members.items() if v}

    errors = [t for t in translations
              if t.get("code") in {PARSE_ERROR, INVALID_LANG}]
    plan = {
        "target": snapshot.get("target") or {},
        "sheet": {"id": sheet_id, "tabs": [t.strip() for t in tabs.split(",") if t.strip()]},
        "lang": lang if translated else "off",
        "translationState": tstate,
        "translationNote": snapshot.get("translationNote", ""),
        "newOnly": new_only,
        "objects": objects,
        "newObjects": sorted(new_objects),
        "newFields": {o: sorted(f) for o, f in sorted(new_fields.items()) if f},
        "skippedFields": {o: f for o, f in sorted(skipped_fields.items())},
        "attributeDrift": {o: drift[o] for o in sorted(drift)},
        "driftApproved": sorted(include_drift),
        "translations": translations,
        "translationPackage": [t["id"] for t in translations if t.get("package")],
        "deleteMembers": sorted(delete_members),
        "manifestMembers": members,
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
        "deletes": len(delete_members),
        "components": sum(len(v) for v in members.values()),
        "empty": not members,
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
        for d in o["attributeDrift"]:
            mark = "APPROVED" if f"{o['object']}.{d.get('field')}" in plan["driftApproved"] else "REPORT  "
            print(f"         drift[{mark}] {d.get('field')}: {d.get('reason', '')[:90]}")
    print("-" * 78)
    print(f"  new objects {s['newObjects']} · new fields {s['newFields']} · "
          f"skipped {s['skippedFields']} · drift {s['driftedFields']} "
          f"(approved {len(plan['driftApproved'])})")
    print(f"  new translations {s['newTranslations']} · conflicts {s['conflicts']} · "
          f"schema-missing {s['schemaMissingTranslations']} · deletes {s['deletes']}")
    for mtype, vals in plan["manifestMembers"].items():
        print(f"  manifest {mtype:26} {len(vals)}")
    if s["empty"]:
        print("  → nothing to deploy: the org already matches the sheet.")
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute the deployment plan (delta)")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--snapshot", default=str(org_snapshot.DEFAULT_OUT))
    ap.add_argument("--sheet-id", default="")
    ap.add_argument("--tabs", default="")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--new-only", action="store_true", default=True)
    ap.add_argument("--all-changed", dest="new_only", action="store_false",
                    help="also package CHANGED translations (default: report only)")
    ap.add_argument("--include-drift", default="",
                    help="comma-separated Obj__c.Field__c to redeploy despite "
                         "attribute drift (explicit decision, never automatic)")
    ap.add_argument("--drift-dir", default=str(BUILD_DIR))
    ap.add_argument("--sync-state", default=DEFAULT_SYNC_STATE)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    snapshot = org_snapshot.load(args.snapshot)
    objs = sorted({norm(r.get("Object API Name")) for r in rows
                   if norm(r.get("Object API Name"))})
    try:
        plan = build_plan(
            rows, snapshot, sheet_id=args.sheet_id, tabs=args.tabs, lang=args.lang,
            new_only=args.new_only,
            include_drift={m.strip() for m in args.include_drift.split(",") if m.strip()},
            drift=load_drift(objs, Path(args.drift_dir)),
            sync_state=args.sync_state)
    except DriftReportError as e:
        print(f"⛔ {e}")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report(plan)
    print(f"saved → {out}")
    if plan["validationErrors"]:
        for e in plan["validationErrors"]:
            print(f"⛔ {e['code']} {e['id']}: {e['reason']}")
        print("⛔ fix the sheet — unparseable EN cells / invalid language codes are "
              "validation errors, not missing translations.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
