#!/usr/bin/env python3
"""
label_sync.py — the sheet's Japanese (master) labels overwrite the org.

The name delta skips fields that already exist, and the translation delta only
compares English, so an org-side edit of a Japanese label used to survive every
deploy. This module compares, for EXISTING objects:

  * the object label            (sheet `Object Label`     vs CustomObject.label)
  * the Name field label        (sheet `Name Field Label` vs nameField.label)
  * each custom field label     (sheet `Field Label`      vs CustomField.label)

and packages every difference. Only the LABEL changes: patched files are built
from the org's current metadata (readMetadata, FLS-independent), so a relabel
never redeploys a type/formula/picklist drift — that stays with attr_drift.py.

Deploying CustomObject from source also pushes every local child field file, so
when an object is relabelled all of its existing field files are rewritten from
the org definition as well (a no-op for fields without a label change).
"""
from __future__ import annotations

import json
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from translate_enrich import add_member, load_token, read_metadata

NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", NS)

# CustomObject children that source format stores as separate files.
DECOMPOSED_CHILDREN = {
    "fields", "listViews", "recordTypes", "validationRules", "webLinks",
    "compactLayouts", "businessProcesses", "fieldSets", "sharingReasons", "indexes",
}

KIND_OBJECT = "ObjectLabel"
KIND_NAME = "NameFieldLabel"
KIND_FIELD = "FieldLabel"

SNAPSHOT_PATH = Path(".build/org_master_labels.json")


def label_norm(s) -> str:
    """Exact label comparison: NFC + trim only (full-width vs half-width differs)."""
    return unicodedata.normalize("NFC", str(s or "")).strip()


def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    return el.findtext(tag) or ""


# --------------------------------------------------------------------------- #
# Org snapshot
# --------------------------------------------------------------------------- #
def snapshot_master_labels(objs: list[str], org: str) -> dict:
    """Live readMetadata(CustomObject) → {obj: {label, name_field_label, fields, xml}}."""
    if not objs:
        return {}
    tok = load_token(org)
    recs = read_metadata("CustomObject", list(objs), tok["accessToken"],
                         tok["instanceUrl"].rstrip("/"), tok["apiVersion"])
    out: dict[str, dict] = {}
    for rec, raw in recs:
        obj = label_norm(rec.findtext("fullName"))
        if not obj:
            continue
        out[obj] = {
            "label": rec.findtext("label") or "",
            "name_field_label": _text(rec.find("nameField"), "label"),
            "fields": {
                label_norm(f.findtext("fullName")): f.findtext("label") or ""
                for f in rec.findall("fields") if f.findtext("fullName")
            },
            "xml": raw,
        }
    return out


def save_snapshot(snapshot: dict, path: Path = SNAPSHOT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Sheet labels (same defaults as generate_xml.py; blank = not specified)
# --------------------------------------------------------------------------- #
def sheet_master_labels(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        obj = label_norm(r.get("Object API Name"))
        if not obj:
            continue
        rec = out.setdefault(obj, {"label": "", "name_field_label": "", "fields": {}})
        if r.get("_type") == "object_meta":
            rec["label"] = label_norm(r.get("Object Label"))
            rec["name_field_label"] = label_norm(r.get("Name Field Label"))
            continue
        api = label_norm(r.get("Field API Name"))
        if not api.endswith("__c"):
            continue
        label = label_norm(r.get("Field Label") or r.get("Field Label (JA)"))
        if label:
            rec["fields"][api] = label
    return out


def label_deltas(sheet: dict, org: dict, present_objects: set[str]) -> list[dict]:
    """Every sheet label that differs from an existing org component."""
    out: list[dict] = []
    for obj in sorted(present_objects):
        s, o = sheet.get(obj), org.get(obj)
        if not s or not o:
            continue
        if s["label"] and label_norm(o["label"]) != s["label"]:
            out.append({"kind": KIND_OBJECT, "object": obj, "key": obj,
                        "sheet": s["label"], "org": o["label"]})
        if s["name_field_label"] and label_norm(o["name_field_label"]) != s["name_field_label"]:
            out.append({"kind": KIND_NAME, "object": obj, "key": "Name",
                        "sheet": s["name_field_label"], "org": o["name_field_label"]})
        for api, label in s["fields"].items():
            if api not in o["fields"]:
                continue  # new field — created from the sheet by the schema delta
            if label_norm(o["fields"][api]) != label:
                out.append({"kind": KIND_FIELD, "object": obj, "key": api,
                            "sheet": label, "org": o["fields"][api]})
    return out


def add_label_members(plan: dict, deltas: list[dict]) -> None:
    plan["labels"] = deltas
    for d in deltas:
        if d["kind"] == KIND_FIELD:
            add_member(plan, "CustomField", f"{d['object']}.{d['key']}")
        else:
            add_member(plan, "CustomObject", d["object"])


def print_label_delta(plan: dict) -> None:
    deltas = plan.get("labels") or []
    print(f"      LABELS (JA)  sheet overwrites org={len(deltas)}")
    for d in deltas:
        where = d["object"] if d["kind"] == KIND_OBJECT else f"{d['object']}.{d['key']}"
        print(f"        RELABEL {where}: org {d['org']!r} → sheet {d['sheet']!r}")


# --------------------------------------------------------------------------- #
# Patch generated source from the org definition
# --------------------------------------------------------------------------- #
def _qualify(el: ET.Element) -> ET.Element:
    out = ET.Element(f"{{{NS}}}{el.tag}")
    out.text, out.tail = el.text, None
    for child in el:
        if child.get("nil") != "true":
            out.append(_qualify(child))
    return out


def _set_child_text(root: ET.Element, tag: str, text: str, *, after: str = "") -> None:
    el = root.find(f"{{{NS}}}{tag}")
    if el is None:
        el = ET.Element(f"{{{NS}}}{tag}")
        anchor = root.find(f"{{{NS}}}{after}") if after else None
        idx = list(root).index(anchor) + 1 if anchor is not None else len(root)
        root.insert(idx, el)
    el.text = text


def _write(root: ET.Element, path: Path) -> None:
    ET.indent(root, space="    ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        ET.ElementTree(root).write(f, encoding="utf-8", xml_declaration=False)
        f.write(b"\n")


def _field_from_org(field_el: ET.Element, label: str | None) -> ET.Element:
    root = ET.Element(f"{{{NS}}}CustomField")
    for child in field_el:
        if child.get("nil") != "true":
            root.append(_qualify(child))
    if label is not None:
        _set_child_text(root, "label", label, after="fullName")
    return root


def apply_label_patches(plan: dict, snapshot: dict, objects_root: Path) -> list[str]:
    """Rewrite generated files so the package changes ONLY the planned labels."""
    deltas = plan.get("labels") or []
    if not deltas:
        return []
    new_fields = set((plan.get("schema") or {}).get("new_fields") or [])
    written: list[str] = []
    by_obj: dict[str, list[dict]] = {}
    for d in deltas:
        by_obj.setdefault(d["object"], []).append(d)

    for obj, obj_deltas in by_obj.items():
        org = snapshot.get(obj)
        if not org or not org.get("xml"):
            raise SystemExit(f"❌ label patch: no org snapshot for {obj}")
        rec = ET.fromstring(org["xml"])
        org_fields = {label_norm(f.findtext("fullName")): f for f in rec.findall("fields")}
        field_labels = {d["key"]: d["sheet"] for d in obj_deltas if d["kind"] == KIND_FIELD}
        object_patch = [d for d in obj_deltas if d["kind"] != KIND_FIELD]
        obj_dir = objects_root / obj

        if object_patch:
            root = ET.Element(f"{{{NS}}}CustomObject")
            for child in rec:
                if child.tag not in DECOMPOSED_CHILDREN and child.get("nil") != "true":
                    root.append(_qualify(child))
            for d in object_patch:
                if d["kind"] == KIND_OBJECT:
                    _set_child_text(root, "label", d["sheet"], after="fullName")
                    # Japanese has no plural form; the plural follows the label.
                    _set_child_text(root, "pluralLabel", d["sheet"], after="label")
                else:
                    nf = root.find(f"{{{NS}}}nameField")
                    if nf is None:
                        raise SystemExit(f"❌ label patch: {obj} has no nameField in org")
                    _set_child_text(nf, "label", d["sheet"])
            _write(root, obj_dir / f"{obj}.object-meta.xml")
            written.append(f"{obj} (object-meta)")
            # The CustomObject deploy carries every local field file, so pin
            # each existing one to its org definition (plus planned relabels).
            fields_dir = obj_dir / "fields"
            for fpath in sorted(fields_dir.glob("*.field-meta.xml")) if fields_dir.is_dir() else []:
                api = fpath.name[: -len(".field-meta.xml")]
                if f"{obj}.{api}" in new_fields or api not in org_fields:
                    continue
                _write(_field_from_org(org_fields[api], field_labels.get(api)), fpath)
            written.extend(f"{obj}.{a}" for a in field_labels)
            continue

        for api, label in field_labels.items():
            if api not in org_fields:
                raise SystemExit(f"❌ label patch: {obj}.{api} missing from org snapshot")
            _write(_field_from_org(org_fields[api], label),
                   obj_dir / "fields" / f"{api}.field-meta.xml")
            written.append(f"{obj}.{api}")
    return written


# --------------------------------------------------------------------------- #
# Post-deploy verification
# --------------------------------------------------------------------------- #
def verify_labels(plan: dict, org: str) -> bool:
    """Re-read the org and confirm every planned Japanese label landed exactly."""
    deltas = plan.get("labels") or []
    if not deltas:
        return True
    live = snapshot_master_labels(sorted({d["object"] for d in deltas}), org)
    ok = True
    print("\n  [JA labels] sheet → org")
    for d in deltas:
        o = live.get(d["object"]) or {}
        if d["kind"] == KIND_OBJECT:
            actual, loc = o.get("label", ""), d["object"]
        elif d["kind"] == KIND_NAME:
            actual, loc = o.get("name_field_label", ""), f"{d['object']}.Name"
        else:
            actual, loc = (o.get("fields") or {}).get(d["key"], ""), f"{d['object']}.{d['key']}"
        if label_norm(actual) != label_norm(d["sheet"]):
            ok = False
            print(f"      ✗ {loc}: org {actual!r} != sheet {d['sheet']!r}")
        else:
            print(f"      ✓ {loc}: {d['sheet']!r}")
    return ok
