#!/usr/bin/env python3
"""
fetch_translations.py — convert live object-tab rows into a translation catalog.

Source of truth: the Data Dictionary Google Sheet
  https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ

English lives on each object tab in **column D**, header ``Field Label (EN)``.
Japanese ``Field Label`` (col C) stays CustomField.label. This script does not
read a cached snapshot of the sheet: it consumes ``temp_updates.json`` produced
by a live ``fetch_sheet.py`` of those object tabs.

Usage:
  python scripts/fetch_translations.py --from-object-rows temp_updates.json \
      --out .build/translation_catalog.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from translation_lib import (  # noqa: E402
    KIND_NAME_FIELD, KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
    KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST, KIND_OBJECT_REL, DEFAULT_LANG,
    make_entry, parse_picklist_en, parse_picklist_entries, norm,
)

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
)


def entries_from_object_rows(rows: list[dict], lang: str = DEFAULT_LANG) -> list[dict]:
    """Convert fetch_sheet.py object/field rows into translation catalog entries."""
    out: list[dict] = []
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if not obj:
            continue
        source = f"object_tab:{r.get('_SheetName') or obj}"
        if r.get("_type") == "object_meta":
            en = norm(r.get("Object Label (EN)"))
            e = make_entry(kind=KIND_OBJECT_LABEL, component=obj, aspect="label",
                           key=obj, language=lang, master=norm(r.get("Object Label")),
                           translation=en, source=source)
            out.append(e)
            name_en = norm(r.get("Name Field Label (EN)"))
            e = make_entry(kind=KIND_NAME_FIELD, component=obj, aspect="label",
                           key="Name", language=lang,
                           master=norm(r.get("Name Field Label")),
                           translation=name_en, source=source)
            out.append(e)
            continue
        api = norm(r.get("Field API Name"))
        if not api.endswith("__c"):
            continue
        e = make_entry(kind=KIND_OBJECT_FIELD, component=obj, aspect="label",
                       key=api, language=lang, master=norm(r.get("Field Label")),
                       translation=norm(r.get("Field Label (EN)")), source=source)
        out.append(e)
        help_en = norm(r.get("Help Text (EN)"))
        if help_en or norm(r.get("Help Text")):
            out.append(make_entry(kind=KIND_OBJECT_HELP, component=obj, aspect="help",
                                  key=api, language=lang, master=norm(r.get("Help Text")),
                                  translation=help_en, source=source))
        rel_en = norm(r.get("Relationship Label (EN)"))
        if rel_en:
            out.append(make_entry(kind=KIND_OBJECT_REL, component=obj,
                                  aspect="relationshipLabel", key=api, language=lang,
                                  master=norm(r.get("Relationship Label")),
                                  translation=rel_en, source=source))
        dt = norm(r.get("Data Type")).lower()
        if "picklist" in dt:
            masters = [lbl for lbl, _api in parse_picklist_entries(
                r.get("Type Specific Value") or r.get("Picklist Values") or "")]
            pairs, err = parse_picklist_en(r.get("Picklist Values (EN)") or "", masters)
            if err and norm(r.get("Picklist Values (EN)")):
                out.append(make_entry(
                    kind=KIND_OBJECT_PICKLIST, component=obj, aspect="picklist",
                    key=f"{api}::__parse__", language=lang, master="",
                    translation="", source=source,
                    extra={"parse_error": err, "field": api}))
            for master, trans in pairs:
                out.append(make_entry(
                    kind=KIND_OBJECT_PICKLIST, component=obj, aspect="picklist",
                    key=f"{api}::{master}", language=lang, master=master,
                    translation=trans, source=source,
                    extra={"field": api}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build object-translation catalog from live fetch_sheet rows")
    ap.add_argument("--spreadsheet-id", default=DEFAULT_SHEET_ID,
                    help="Data Dictionary spreadsheet id (documented SoT; rows come from --from-object-rows)")
    ap.add_argument("--from-object-rows", required=True,
                    help="temp_updates.json from a live fetch_sheet.py of object tabs")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--out", default=".build/translation_catalog.json")
    args = ap.parse_args()

    print(f"Spreadsheet SoT: {DEFAULT_SHEET_URL}  id={args.spreadsheet_id}")
    rows = json.loads(Path(args.from_object_rows).read_text(encoding="utf-8"))
    entries = entries_from_object_rows(rows, lang=args.lang)
    print(f"  ✓ object-tab EN: {len(entries)} catalog entr(y/ies) from {args.from_object_rows}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}: {len(entries)} catalog entr(y/ies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
