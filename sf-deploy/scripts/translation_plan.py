#!/usr/bin/env python3
"""
translation_plan.py — sheet × org snapshot → immutable deploy plan.

The plan is the single source of deployment scope for translations (and the
schema delta that translations depend on). Manifest members are taken from
this file; generated directories are never scanned to decide scope.
"""
from __future__ import annotations

import json
from pathlib import Path

from translation_lib import (
    CHANGED,
    KIND_NAME_FIELD,
    KIND_OBJECT_FIELD,
    KIND_OBJECT_LABEL,
    MISSING,
    NEW,
    SCHEMA_MISSING,
    SF_LANG,
    UNCHANGED,
    WIP,
    add_member,
    content_hash,
    empty_plan,
    invalid_english,
    jsonable_translation,
    load_token,
    make_entry,
    norm,
    parse_object_translation_el,
    read_metadata,
)


def entries_from_rows(rows: list[dict], lang: str = SF_LANG) -> list[dict]:
    out = []
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if not obj:
            continue
        src = f"object_tab:{r.get('_SheetName') or obj}"
        if r.get("_type") == "object_meta":
            out.append(make_entry(
                kind=KIND_OBJECT_LABEL, component=obj, key=obj, language=lang,
                master=norm(r.get("Object Label")),
                translation=norm(r.get("Object Label (EN)")),
                source=src, origin=norm(r.get("Object Translation Origin")),
                source_hash=norm(r.get("Object Translation Source Hash")),
            ))
            out.append(make_entry(
                kind=KIND_NAME_FIELD, component=obj, key="Name", language=lang,
                master=norm(r.get("Name Field Label")),
                translation=norm(r.get("Name Field Label (EN)")),
                source=src, origin=norm(r.get("Name Translation Origin")),
                source_hash=norm(r.get("Name Translation Source Hash")),
            ))
            continue
        api = norm(r.get("Field API Name"))
        if not api.endswith("__c"):
            continue
        out.append(make_entry(
            kind=KIND_OBJECT_FIELD, component=obj, key=api, language=lang,
            master=norm(r.get("Field Label")),
            translation=norm(r.get("Field Label (EN)")),
            source=src, origin=norm(r.get("Translation Origin")),
            source_hash=norm(r.get("Translation Source Hash")),
            sheet_row=int(r.get("_SheetRow") or 0),
        ))
    return out


def classify_against_org(sheet_entries: list[dict], org_models: dict[str, dict],
                         org_fields: dict[str, set[str]],
                         org_objects: set[str]) -> list[dict]:
    out = []
    for e in sheet_entries:
        rec = dict(e)
        obj = e["component"]
        en = e.get("translation") or ""
        bad = invalid_english(en, ja=e.get("master", ""), api=e.get("key", "")) if en else "blank"
        org_model = org_models.get(f"{obj}-{e['language']}") or org_models.get(obj) or {}
        org_label = ""
        if e["kind"] == KIND_OBJECT_LABEL:
            org_label = org_model.get("object_label") or ""
            in_schema = True  # object itself may be new
        elif e["kind"] == KIND_NAME_FIELD:
            org_label = org_model.get("name_field_label") or ""
            fld = (org_model.get("fields") or {}).get("Name") or {}
            org_label = org_label or fld.get("label") or ""
            in_schema = True
        else:
            fld = (org_model.get("fields") or {}).get(e["key"]) or {}
            org_label = fld.get("label") or ""
            present_fields = org_fields.get(obj) or set()
            in_schema = (e["key"] in present_fields) or (obj not in org_objects)
            # new object → field is in THIS plan (not schema-missing)
            if obj not in org_objects:
                in_schema = True
            elif e["key"] not in present_fields:
                # field also being created in this run counts as in-schema
                rec["_field_is_new"] = True
                in_schema = True

        if not en or bad == "blank":
            rec["code"] = MISSING
            rec["package"] = False
            rec["reason"] = "blank or invalid English"
            out.append(rec)
            continue
        if bad and bad != "blank":
            rec["code"] = MISSING
            rec["package"] = False
            rec["reason"] = bad
            out.append(rec)
            continue
        if not in_schema:
            rec["code"] = SCHEMA_MISSING
            rec["package"] = False
            rec["reason"] = "target field absent from org and not in schema plan"
            out.append(rec)
            continue
        if not org_label:
            rec["code"] = NEW
            rec["package"] = True
            rec["reason"] = "no en_US translation in org"
            out.append(rec)
            continue
        if content_hash(en) == content_hash(org_label):
            rec["code"] = UNCHANGED
            rec["package"] = False
            rec["reason"] = "sheet matches org"
            out.append(rec)
            continue
        rec["code"] = CHANGED
        rec["package"] = True
        rec["org_translation"] = org_label
        rec["reason"] = "sheet English differs from org"
        out.append(rec)
    return out


def snapshot_translations(objs: list[str], org: str, lang: str = SF_LANG) -> dict:
    """Live CustomObjectTranslation snapshot, keyed by '<Obj>-en_US'."""
    if not objs:
        return {}
    tokinfo = load_token(org)
    tok, inst, ver = tokinfo["accessToken"], tokinfo["instanceUrl"].rstrip("/"), tokinfo["apiVersion"]
    members = [f"{o}-{lang}" for o in objs]
    recs = read_metadata("CustomObjectTranslation", members, tok, inst, ver)
    out = {}
    for rec, obj in zip(recs, objs):
        # readMetadata may return fewer records when language/object missing
        pass
    # Map by fullName when present.
    for rec in recs:
        full = norm(rec.findtext("fullName") or "")
        obj = full.rsplit("-", 1)[0] if full else ""
        if not obj:
            continue
        model = parse_object_translation_el(rec, obj, lang)
        out[f"{obj}-{lang}"] = jsonable_translation(model)
    return out


def build_plan(
    *,
    rows: list[dict],
    org: str,
    org_id: str,
    tabs: list[str],
    provider: str,
    present_objects: set[str],
    present_fields: dict[str, set[str]],
    org_translations: dict,
    lang: str = SF_LANG,
) -> dict:
    plan = empty_plan(org=org, org_id=org_id, tabs=tabs, provider=provider)
    plan["language"] = lang

    objs = []
    for r in rows:
        if r.get("_type") == "object_meta":
            api = norm(r.get("Object API Name"))
            if api and api not in objs:
                objs.append(api)

    new_objects, existing = [], []
    for o in objs:
        if o in present_objects:
            existing.append(o)
        else:
            new_objects.append(o)
            add_member(plan, "CustomObject", o)
    plan["schema"]["new_objects"] = new_objects
    plan["schema"]["existing_objects"] = existing

    sheet_fields: dict[str, list[str]] = {}
    for r in rows:
        if r.get("_type") == "object_meta":
            continue
        obj = norm(r.get("Object API Name"))
        api = norm(r.get("Field API Name"))
        if obj and api.endswith("__c"):
            sheet_fields.setdefault(obj, []).append(api)

    new_fields = []
    for obj, fields in sheet_fields.items():
        have = present_fields.get(obj) or set()
        for f in fields:
            if obj in new_objects or f not in have:
                new_fields.append(f"{obj}.{f}")
                add_member(plan, "CustomField", f"{obj}.{f}")
                if obj in new_objects:
                    add_member(plan, "CustomObject", obj)
    plan["schema"]["new_fields"] = new_fields

    entries = entries_from_rows(rows, lang=lang)
    classified = classify_against_org(entries, org_translations, present_fields, present_objects)
    plan["translations"] = classified

    packaged_objs = set()
    for rec in classified:
        if rec.get("package"):
            packaged_objs.add(rec["component"])
            # Keep dependent schema + translation in the SAME package.
            if rec["component"] in new_objects:
                add_member(plan, "CustomObject", rec["component"])
            if rec["kind"] == KIND_OBJECT_FIELD and rec.get("_field_is_new"):
                add_member(plan, "CustomField", f"{rec['component']}.{rec['key']}")
    for obj in packaged_objs:
        add_member(plan, "CustomObjectTranslation", f"{obj}-{lang}")
        # New field + its translation must travel together; if this object also
        # has schema members they are already on the plan.
        if obj in new_objects:
            add_member(plan, "CustomObject", obj)

    plan["empty"] = not any(plan.get("members", {}).values())
    return plan


def write_plan(plan: dict, path: str | Path = ".build/deploy_plan.json") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
