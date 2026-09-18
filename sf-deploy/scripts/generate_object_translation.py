#!/usr/bin/env python3
"""
generate_object_translation.py — CustomObjectTranslation XML from Field Label (EN).

Source of truth: the object-definition tab on the live Data Dictionary sheet
https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
English is the header ``Field Label (EN)``. Japanese in ``Field Label`` stays
CustomField.label. Locate EN by header name (same as fullName / WIP / IsDelete).

Future delta (Japan adds a field to an already-translated object):
  1. They add the row (JA in Field Label, EN in Field Label (EN), API in fullName).
  2. translation_drift --new-only marks that field NEW_TRANSLATION.
  3. This script retrieves the org's existing <Obj>-en_US translation, MERGES
     only the new field's EN, and writes the combined file so siblings are not
     untranslated. Existing translations are not redeployed as a change set.

Usage:
  python scripts/generate_object_translation.py --rows temp_updates.json \
      --org ERPDEV01 --lang en_US --delta .build/translation_drift_objects.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "scripts")
from translation_lib import (  # noqa: E402
    DEFAULT_LANG, KIND_NAME_FIELD, KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
    KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST, KIND_OBJECT_REL,
    entries_from_object_rows, esc, load_token, parse_object_translation,
    read_metadata, write_xml,
)

OUT_ROOT = Path("force-app/main/default/objectTranslations")


def _group(entries: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e["kind"] in {KIND_OBJECT_LABEL, KIND_NAME_FIELD, KIND_OBJECT_FIELD,
                         KIND_OBJECT_HELP, KIND_OBJECT_PICKLIST, KIND_OBJECT_REL}:
            g[e["component"]].append(e)
    return g


def _merge_org(sheet_entries: list[dict], org_by_id: dict[str, dict],
               overlay_ids: set[str] | None) -> dict[str, dict]:
    """Org translations stay; overlay only the NEW (packaged) sheet entries.

    Deploying CustomObjectTranslation is a whole object-language member. A file
    that contains only the new field would untranslate every sibling. So we
    retrieve the org file, add the new EN labels, and write the union.
    """
    merged = dict(org_by_id)
    for e in sheet_entries:
        if not e.get("translation"):
            continue
        if overlay_ids is None or e["id"] in overlay_ids:
            merged[e["id"]] = e
    return merged


def render_object_file(obj: str, lang: str, entries: list[dict]) -> str:
    obj_label = next((e["translation"] for e in entries
                      if e["kind"] == KIND_OBJECT_LABEL and e.get("translation")), "")
    name_en = next((e["translation"] for e in entries
                    if e["kind"] == KIND_NAME_FIELD and e.get("translation")), "")
    lines = [
        f'<CustomObjectTranslation xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    if obj_label:
        lines += [
            "    <caseValues>",
            "        <plural>false</plural>",
            f"        <value>{esc(obj_label)}</value>",
            "    </caseValues>",
        ]
    if name_en:
        # Standard Name field translation rides on the object file via <nameFieldLabel>
        # in some API versions; source format uses a sibling Name.fieldTranslation.
        pass
    lines.append("</CustomObjectTranslation>")
    lines.append("")
    return "\n".join(lines)


def render_field_file(field: str, entries: list[dict]) -> str:
    label = next((e["translation"] for e in entries
                  if e["kind"] == KIND_OBJECT_FIELD and e.get("translation")), "")
    help_ = next((e["translation"] for e in entries
                  if e["kind"] == KIND_OBJECT_HELP and e.get("translation")), "")
    rel = next((e["translation"] for e in entries
                if e["kind"] == KIND_OBJECT_REL and e.get("translation")), "")
    picks = [e for e in entries if e["kind"] == KIND_OBJECT_PICKLIST and e.get("translation")]
    lines = [
        f'<CustomFieldTranslation xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    if label:
        lines.append(f"    <label>{esc(label)}</label>")
    else:
        # Keep the required <label> element; empty comment preserves org master.
        lines.append("    <label><!-- untranslated --></label>")
    if help_:
        lines.append(f"    <help>{esc(help_)}</help>")
    lines.append(f"    <name>{esc(field)}</name>")
    if rel:
        lines.append(f"    <relationshipLabel>{esc(rel)}</relationshipLabel>")
    for p in picks:
        master = p.get("master") or (p["key"].split("::", 1)[1] if "::" in p["key"] else "")
        lines += [
            "    <picklistValues>",
            f"        <masterLabel>{esc(master)}</masterLabel>",
            f"        <translation>{esc(p['translation'])}</translation>",
            "    </picklistValues>",
        ]
    lines.append("</CustomFieldTranslation>")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate CustomObjectTranslation XML")
    ap.add_argument("--rows", default="", help="temp_updates.json from fetch_sheet.py")
    ap.add_argument("--catalog", default="", help="translation_catalog.json")
    ap.add_argument("--delta", default="", help="translation_drift.json — only objects with PKG rows")
    ap.add_argument("--org", default="")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args()

    entries: list[dict] = []
    if args.catalog:
        entries.extend(json.loads(Path(args.catalog).read_text(encoding="utf-8")))
    if args.rows:
        rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
        entries.extend(entries_from_object_rows(rows, lang=args.lang))
    entries = [e for e in entries if e.get("language", args.lang) == args.lang]
    if not entries:
        print("generate_object_translation: no catalog entries — nothing to write.")
        return 0

    package_ids: set[str] | None = None
    if args.delta:
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        package_ids = {d["id"] for d in delta if d.get("package")}

    grouped = _group(entries)
    tok = inst = ver = None
    if args.org:
        a = load_token(args.org)
        tok, inst, ver = a["accessToken"], a["instanceUrl"].rstrip("/"), a["apiVersion"]

    written = 0
    root = Path(args.out_root)
    for obj, ents in grouped.items():
        # skip objects with no EN at all
        if not any(e.get("translation") for e in ents):
            continue
        if package_ids is not None:
            if not any(e["id"] in package_ids for e in ents):
                continue

        org_by_id: dict[str, dict] = {}
        if tok:
            recs = read_metadata("CustomObjectTranslation", [f"{obj}-{args.lang}"],
                                 tok, inst, ver)
            if recs:
                org_by_id = parse_object_translation(recs[0], obj, args.lang)

        merged = _merge_org(ents, org_by_id, package_ids)
        merged_list = list(merged.values())

        folder = root / f"{obj}-{args.lang}"
        write_xml(folder / f"{obj}-{args.lang}.objectTranslation-meta.xml",
                  render_object_file(obj, args.lang, merged_list))

        # Name field
        name_ents = [e for e in merged_list if e.get("key") == "Name"
                     and e["kind"] in {KIND_NAME_FIELD, KIND_OBJECT_FIELD}]
        if any(e.get("translation") for e in name_ents):
            write_xml(folder / "Name.fieldTranslation-meta.xml",
                      render_field_file("Name", name_ents))

        fields = sorted({e["key"].split("::", 1)[0] for e in merged_list
                         if e["kind"] in {KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
                                          KIND_OBJECT_PICKLIST, KIND_OBJECT_REL}
                         and e["key"] != "Name"})
        for field in fields:
            fents = [e for e in merged_list
                     if e.get("key") == field
                     or e.get("key", "").startswith(field + "::")
                     or e.get("field") == field]
            if not any(e.get("translation") for e in fents):
                continue
            write_xml(folder / f"{field}.fieldTranslation-meta.xml",
                      render_field_file(field, fents))
            written += 1
        print(f"  ✓ {obj}-{args.lang}: {len(fields)} field translation file(s)")
        written += 1

    print(f"generate_object_translation: wrote under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
