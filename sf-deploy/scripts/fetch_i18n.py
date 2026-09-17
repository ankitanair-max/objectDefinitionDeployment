#!/usr/bin/env python3
"""
fetch_i18n.py — Live-read the I18N_LWC and I18N_Flows tabs from the SAME
Google Sheet used as the object-definition source of truth.

Produces `.build/i18n_catalog.json` (list of catalog entries). Never reads a
cached snapshot. Object-tab EN columns are fetched via fetch_sheet.py and
converted here with `--from-object-rows`.

Usage:
  python scripts/fetch_i18n.py --spreadsheet-id <ID> --tabs I18N_LWC,I18N_Flows
  python scripts/fetch_i18n.py --spreadsheet-id <ID> --from-object-rows temp_updates.json
  python scripts/fetch_i18n.py --spreadsheet-id <ID> --tabs I18N_LWC,I18N_Flows \
      --from-object-rows temp_updates.json --out .build/i18n_catalog.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from fetch_sheet import (  # noqa: E402
    get_sheets_service, find_helper_cols, is_field_list_end, norm as sheet_norm,
)
from i18n_lib import (  # noqa: E402
    KIND_CUSTOM_LABEL, KIND_NAME_FIELD, KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
    KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST, KIND_OBJECT_REL, DEFAULT_LANG,
    FLOW_COMPONENT_TO_KIND, DELETE_TRUE, WIP_TRUE, make_entry, parse_picklist_en,
    parse_picklist_entries, norm,
)

# Import DEFAULT_SHEET_ID from prep_deploy without pulling the whole module.
DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"

CANONICAL_TABS = ("I18N_LWC", "I18N_Flows")

# Header aliases (lowercased) → canonical key.
LWC_HEADERS = {
    "label api name": "label_api",
    "fullname": "label_api",
    "api name": "label_api",
    "customlabel": "label_api",
    "lwc bundle": "lwc_bundle",
    "bundle": "lwc_bundle",
    "component": "lwc_bundle",
    "categories": "categories",
    "short description": "short_description",
    "shortdescription": "short_description",
    "language": "language",
    "master value (ja)": "master",
    "master value": "master",
    "master (ja)": "master",
    "value (ja)": "master",
    "translation": "translation",
    "translation (en)": "translation",
    "protected": "protected",
}
FLOW_HEADERS = {
    "flow api name": "flow_api",
    "flow": "flow_api",
    "fullname": "flow_api",
    "component": "component",
    "flow component": "component",
    "aspect": "aspect",
    "key": "key",
    "element": "key",
    "language": "language",
    "master value (ja)": "master",
    "master value": "master",
    "master (ja)": "master",
    "translation": "translation",
    "translation (en)": "translation",
}


def _header_map(header_row: list, aliases: dict) -> dict[str, int]:
    lowered = [sheet_norm(c).lower() for c in header_row]
    cols = {}
    for i, h in enumerate(lowered):
        if h in aliases:
            cols[aliases[h]] = i
    return cols


def _find_catalog_header(grid: list[list], aliases: dict) -> int | None:
    for i, row in enumerate(grid[:15]):
        lowered = [sheet_norm(c).lower() for c in row]
        hits = sum(1 for h in lowered if h in aliases)
        if hits >= 3:
            return i
    return None


def _cell(cells: list, cols: dict, key: str) -> str:
    idx = cols.get(key)
    if idx is None or idx >= len(cells):
        return ""
    return sheet_norm(cells[idx])


def parse_lwc_tab(title: str, grid: list[list]) -> list[dict]:
    hidx = _find_catalog_header(grid, LWC_HEADERS)
    if hidx is None:
        print(f"  ⚠️  {title}: no LWC catalog header (need Label API Name / Translation) — skipped.")
        return []
    cols = _header_map(grid[hidx], LWC_HEADERS)
    helper = find_helper_cols(grid[hidx])
    out = []
    skipped_wip = skipped_del = 0
    for i, row in enumerate(grid[hidx + 1:], start=hidx + 2):
        cells = [sheet_norm(c) for c in row]
        if is_field_list_end(cells) or (cells and cells[0].startswith("END[")):
            break
        wip_idx = helper.get("wip", 99)
        del_idx = helper.get("isdelete", 99)
        wip = cells[wip_idx] if wip_idx < len(cells) else ""
        dele = cells[del_idx] if del_idx < len(cells) else ""
        if wip.lower() in WIP_TRUE:
            skipped_wip += 1
            continue
        if dele.lower() in DELETE_TRUE:
            skipped_del += 1
            continue
        api = _cell(cells, cols, "label_api")
        if not api:
            continue
        e = make_entry(
            kind=KIND_CUSTOM_LABEL, component=api, aspect="label", key=api,
            language=_cell(cells, cols, "language") or DEFAULT_LANG,
            master=_cell(cells, cols, "master"),
            translation=_cell(cells, cols, "translation"),
            source=f"tab:{title}", sheet_row=i,
            extra={
                "lwc_bundle": _cell(cells, cols, "lwc_bundle"),
                "categories": _cell(cells, cols, "categories") or "LWC",
                "short_description": _cell(cells, cols, "short_description") or api,
                "protected": _cell(cells, cols, "protected") or "false",
            },
        )
        out.append(e)
    print(f"  ✓ {title}: {len(out)} Custom Label row(s)"
          + (f", {skipped_wip} WIP skipped" if skipped_wip else "")
          + (f", {skipped_del} IsDelete excluded" if skipped_del else ""))
    return out


def parse_flow_tab(title: str, grid: list[list]) -> list[dict]:
    hidx = _find_catalog_header(grid, FLOW_HEADERS)
    if hidx is None:
        print(f"  ⚠️  {title}: no Flow catalog header — skipped.")
        return []
    cols = _header_map(grid[hidx], FLOW_HEADERS)
    helper = find_helper_cols(grid[hidx])
    out = []
    skipped_wip = skipped_del = 0
    for i, row in enumerate(grid[hidx + 1:], start=hidx + 2):
        cells = [sheet_norm(c) for c in row]
        if is_field_list_end(cells) or (cells and cells[0].startswith("END[")):
            break
        wip_idx = helper.get("wip", 99)
        del_idx = helper.get("isdelete", 99)
        wip = cells[wip_idx] if wip_idx < len(cells) else ""
        dele = cells[del_idx] if del_idx < len(cells) else ""
        if wip.lower() in WIP_TRUE:
            skipped_wip += 1
            continue
        if dele.lower() in DELETE_TRUE:
            skipped_del += 1
            continue
        flow = _cell(cells, cols, "flow_api")
        if not flow:
            continue
        comp_raw = _cell(cells, cols, "component") or "Definition"
        kind = FLOW_COMPONENT_TO_KIND.get(comp_raw.lower().replace("_", "").replace("-", ""), "")
        if not kind:
            print(f"  ⚠️  {title} row {i}: unknown Flow Component {comp_raw!r} — skipped")
            continue
        aspect = _cell(cells, cols, "aspect") or "label"
        key = _cell(cells, cols, "key") or flow
        e = make_entry(
            kind=kind, component=flow, aspect=aspect, key=key,
            language=_cell(cells, cols, "language") or DEFAULT_LANG,
            master=_cell(cells, cols, "master"),
            translation=_cell(cells, cols, "translation"),
            source=f"tab:{title}", sheet_row=i,
        )
        out.append(e)
    print(f"  ✓ {title}: {len(out)} Flow translation row(s)"
          + (f", {skipped_wip} WIP skipped" if skipped_wip else "")
          + (f", {skipped_del} IsDelete excluded" if skipped_del else ""))
    return out


def entries_from_object_rows(rows: list[dict], lang: str = DEFAULT_LANG) -> list[dict]:
    """Convert fetch_sheet.py object/field rows into i18n catalog entries."""
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
    # drop empty object-label/name rows that were never filled
    return out


def classify_tab(title: str) -> str:
    t = title.strip().lower()
    if "flow" in t:
        return "flow"
    if "lwc" in t or "label" in t:
        return "lwc"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Live-fetch I18N catalog tabs + object EN columns")
    ap.add_argument("--spreadsheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--tabs", default="",
                    help="comma-separated I18N tab names (default: I18N_LWC,I18N_Flows if present)")
    ap.add_argument("--from-object-rows", default="",
                    help="temp_updates.json from fetch_sheet.py (object-tab EN columns)")
    ap.add_argument("--no-catalog-tabs", action="store_true",
                    help="do not fetch I18N_LWC / I18N_Flows (object-tab EN only)")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--out", default=".build/i18n_catalog.json")
    ap.add_argument("--list-tabs", action="store_true")
    args = ap.parse_args()

    svc = get_sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=args.spreadsheet_id,
                                  includeGridData=False).execute()
    all_titles = [s["properties"]["title"] for s in meta["sheets"]]

    i18n_present = [t for t in all_titles
                    if t.strip().lower() in {x.lower() for x in CANONICAL_TABS}
                    or t.strip().lower().startswith("i18n_")]

    if args.list_tabs:
        print(f"Spreadsheet: {meta['properties']['title']}")
        print(f"I18N tabs present ({len(i18n_present)}):")
        for t in i18n_present or ["(none — run create_i18n_tabs.py)"]:
            print(f"  - {t}")
        return 0

    wanted = [t.strip() for t in args.tabs.split(",") if t.strip()]
    if args.no_catalog_tabs:
        wanted = []
    elif not wanted:
        wanted = i18n_present

    print(f"Spreadsheet: {meta['properties']['title']}")
    entries: list[dict] = []

    for title in wanted:
        if title not in all_titles:
            print(f"  ⚠️  tab {title!r} not in workbook — skipped "
                  f"(create it with scripts/create_i18n_tabs.py)")
            continue
        grid = svc.spreadsheets().values().get(
            spreadsheetId=args.spreadsheet_id, range=f"'{title}'",
            valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
        kind = classify_tab(title)
        if kind == "flow":
            entries.extend(parse_flow_tab(title, grid))
        else:
            entries.extend(parse_lwc_tab(title, grid))

    if args.from_object_rows:
        rows = json.loads(Path(args.from_object_rows).read_text(encoding="utf-8"))
        obj_entries = entries_from_object_rows(rows, lang=args.lang)
        print(f"  ✓ object-tab EN: {len(obj_entries)} catalog entr(y/ies) from {args.from_object_rows}")
        entries.extend(obj_entries)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}: {len(entries)} catalog entr(y/ies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
