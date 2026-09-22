#!/usr/bin/env python3
"""
generate_object_translation.py — CustomObjectTranslation XML from the plan.

Consumes the org translation tree (fresh snapshot) and patches ONLY the
intended object / Name-field / custom-field label nodes. Unrelated picklist,
help, and relationship translations are preserved byte-for-node.

English is NEVER written into CustomField.label (Japanese master).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from translate_enrich import (
    KIND_NAME_FIELD,
    KIND_OBJECT_FIELD,
    KIND_OBJECT_LABEL,
    NS,
    SF_LANG,
    esc,
    invalid_english,
    english_plural_label,
    norm,
    parse_object_translation_el,
    starts_with_for,
    strip_soap_ns,
)

OUT_ROOT = Path("force-app/main/default/objectTranslations")
ET.register_namespace("", NS)


def qn(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def overlay_map(entries: list[dict]) -> dict[str, dict[str, dict]]:
    """obj → {kind/key → entry} for in-scope label overlays."""
    out: dict[str, dict[str, dict]] = {}
    for e in entries:
        if e.get("kind") not in {KIND_OBJECT_LABEL, KIND_NAME_FIELD, KIND_OBJECT_FIELD}:
            continue
        if not e.get("translation"):
            continue
        out.setdefault(e["component"], {})[e["id"]] = e
    return out


def _text(parent: ET.Element, tag: str, value: str, *, keep_empty: bool = False) -> None:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    if value or keep_empty:
        el.text = value


def patch_tree(org: dict | None, overlays: dict[str, dict], obj: str,
               object_master: str = "") -> dict:
    """Return a full in-memory COT model: org fields + overlay labels."""
    fields = {}
    if org:
        for name, f in (org.get("fields") or {}).items():
            if name == "Name":
                # Standard Name is <nameFieldLabel> only. Salesforce rejects
                # CustomFieldTranslation for Object.Name ("Cannot translate standard field").
                continue
            fields[name] = dict(f)
    object_label = (org or {}).get("object_label") or ""
    name_label = (org or {}).get("name_field_label") or ""
    starts = (org or {}).get("startsWith") or ""
    # Untranslated org object labels arrive as XML comments, not text. Salesforce
    # still requires a nonempty top-level caseValues value to apply nameFieldLabel.
    case_carrier = (org or {}).get("object_label_comment") or object_master or ""

    for e in overlays.values():
        if e["component"] != obj:
            continue
        en = norm(e.get("translation"))
        bad = invalid_english(en, ja=e.get("master", ""), api=e.get("key", ""))
        if bad:
            raise SystemExit(f"⛔ refusing to package invalid English for {e['id']}: {bad}")
        if e["kind"] == KIND_OBJECT_LABEL:
            object_label = en
            starts = starts_with_for(en)
        elif e["kind"] == KIND_NAME_FIELD or e.get("key") == "Name":
            name_label = en
        elif e["kind"] == KIND_OBJECT_FIELD:
            key = e["key"]
            fields.setdefault(key, {"name": key, "label": "", "help": "",
                                    "relationshipLabel": "", "xml": ""})
            fields[key]["label"] = en
    case_value = object_label or case_carrier
    if name_label and not case_value:
        # Last resort so Name English can land. Header EN longer than 40 is
        # already swapped to Name EN in the plan (sheet cells are not rewritten).
        case_value = name_label
    case_plural = english_plural_label(case_value) if case_value else ""
    if name_label and not starts:
        starts = starts_with_for(object_label or name_label)
    return {
        "object_label": object_label,
        "case_values_value": case_value,
        "case_values_plural": case_plural,
        "name_field_label": name_label,
        "startsWith": starts,
        "fields": fields,
    }


def render_object_file(model: dict) -> str:
    lines = [
        f'<CustomObjectTranslation xmlns="{NS}">',
    ]
    name_label = model.get("name_field_label") or ""
    case_value = model.get("case_values_value") or model.get("object_label") or ""
    case_plural = model.get("case_values_plural") or english_plural_label(case_value)
    if name_label and not case_value:
        raise SystemExit(
            "⛔ nameFieldLabel requires a nonempty top-level caseValues value "
            "(Salesforce otherwise Succeeds without applying the Name translation)."
        )
    if case_plural:
        bad_pl = invalid_english(case_plural, ja="", api="")
        if bad_pl:
            raise SystemExit(f"⛔ refusing to package invalid plural English: {bad_pl}")
        if case_plural == case_value:
            raise SystemExit(
                f"⛔ object plural English is identical to singular ({case_value!r})"
            )
    if case_value:
        lines += [
            "    <caseValues>",
            "        <plural>false</plural>",
            f"        <value>{esc(case_value)}</value>",
            "    </caseValues>",
            "    <caseValues>",
            "        <plural>true</plural>",
            f"        <value>{esc(case_plural or case_value)}</value>",
            "    </caseValues>",
        ]
    if name_label:
        lines.append(f"    <nameFieldLabel>{esc(name_label)}</nameFieldLabel>")
    if model.get("startsWith"):
        lines.append(f"    <startsWith>{esc(model['startsWith'])}</startsWith>")
    lines.append("</CustomObjectTranslation>")
    lines.append("")
    return "\n".join(lines)


def render_field_file(field: dict) -> str:
    """Preserve unrelated child nodes when original field XML is present."""
    xml = field.get("xml") or ""
    if xml:
        try:
            el = ET.fromstring(strip_soap_ns(xml))
            if field.get("label"):
                _text(el, "label", field["label"])
            if el.find("name") is None:
                n = ET.SubElement(el, "name")
                n.text = field["name"]
            # Re-emit as CustomFieldTranslation (SFDX file shape).
            el.tag = "CustomFieldTranslation"
            body = ET.tostring(el, encoding="unicode")
            body = body.replace(
                "<CustomFieldTranslation>",
                f'<CustomFieldTranslation xmlns="{NS}">',
                1,
            )
            return body if body.endswith("\n") else body + "\n"
        except ET.ParseError:
            pass
    lines = [f'<CustomFieldTranslation xmlns="{NS}">']
    if field.get("label"):
        lines.append(f"    <label>{esc(field['label'])}</label>")
    else:
        lines.append("    <label><!-- untranslated --></label>")
    if field.get("help"):
        lines.append(f"    <help>{esc(field['help'])}</help>")
    lines.append(f"    <name>{esc(field['name'])}</name>")
    if field.get("relationshipLabel"):
        lines.append(f"    <relationshipLabel>{esc(field['relationshipLabel'])}</relationshipLabel>")
    lines.append("</CustomFieldTranslation>")
    lines.append("")
    return "\n".join(lines)


def write_object(obj: str, lang: str, model: dict, root: Path) -> int:
    folder = root / f"{obj}-{lang}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{obj}-{lang}.objectTranslation-meta.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + render_object_file(model),
        encoding="utf-8",
    )
    name_tf = folder / "Name.fieldTranslation-meta.xml"
    if name_tf.exists():
        name_tf.unlink()
    n = 1
    for name, field in sorted(model["fields"].items()):
        if name == "Name":
            continue
        # Skip fields that would deploy with neither label nor preserved xml.
        if not field.get("label") and not field.get("xml"):
            continue
        (folder / f"{name}.fieldTranslation-meta.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n' + render_field_file(field),
            encoding="utf-8",
        )
        n += 1
    return n


def generate(
    *,
    plan: dict,
    org_translations: dict[str, dict],
    out_root: Path = OUT_ROOT,
    lang: str = SF_LANG,
) -> list[str]:
    """Write COT files for every object listed in the plan. Return member names."""
    overlays_by_obj: dict[str, dict] = {}
    for e in plan.get("translations") or []:
        if not e.get("package"):
            continue
        overlays_by_obj.setdefault(e["component"], {})[e["id"]] = e

    masters = plan.get("objectMasters") or {}
    members: list[str] = []
    for obj, overlays in overlays_by_obj.items():
        org = org_translations.get(f"{obj}-{lang}") or org_translations.get(obj)
        model = patch_tree(org, overlays, obj, object_master=masters.get(obj) or "")
        write_object(obj, lang, model, out_root)
        members.append(f"{obj}-{lang}")
        print(f"  translation XML: {obj}-{lang}  "
              f"(object_label={bool(model['object_label'])}, "
              f"fields={len(model['fields'])})")
    return members


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate CustomObjectTranslation XML from plan")
    ap.add_argument("--plan", default=".build/deploy_plan.json")
    ap.add_argument("--org-snapshot", default=".build/org_snapshot.json")
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--lang", default=SF_LANG)
    args = ap.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"generate_object_translation: no plan at {plan_path} — nothing to write.")
        return 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    snap = {}
    sp = Path(args.org_snapshot)
    if sp.exists():
        snap = json.loads(sp.read_text(encoding="utf-8"))
    org_t = snap.get("translations") or {}
    members = generate(plan=plan, org_translations=org_t, out_root=Path(args.out_root),
                       lang=args.lang)
    if not members:
        print("generate_object_translation: no packaged translations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
