#!/usr/bin/env python3
"""
create_i18n_tabs.py — add I18N_LWC and I18N_Flows tabs to the Data Dictionary
Google Sheet (same workbook as the object-definition tabs).

GATED sheet write (sheet-write-confirmation.mdc): default is dry-run. Prints
the tab names + header row. Writes only with --apply AFTER the user confirms.

Usage:
  python scripts/create_i18n_tabs.py --spreadsheet-id <ID>          # preview
  python scripts/create_i18n_tabs.py --spreadsheet-id <ID> --apply  # create
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"

LWC_TAB = "I18N_LWC"
FLOW_TAB = "I18N_Flows"

LWC_HEADERS = [
    "No.", "Label API Name", "LWC Bundle", "Categories", "Short Description",
    "Language", "Master Value (JA)", "Translation", "Protected",
    "IsDelete", "WIP", "GDC AI Tool Comments", "Deployment Status",
]
FLOW_HEADERS = [
    "No.", "Flow API Name", "Component", "Aspect", "Key",
    "Language", "Master Value (JA)", "Translation",
    "IsDelete", "WIP", "GDC AI Tool Comments", "Deployment Status",
]

# Frozen header row (navy) — matches object-tab API header styling loosely.
HEADER_BG = {"red": 0.15, "green": 0.25, "blue": 0.45}


def get_write_service():
    from googleapiclient.discovery import build
    import google.auth
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds, _ = google.auth.default(scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _header_format_requests(sheet_id: int, n_cols: int) -> list[dict]:
    return [
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": {
                    "red": 1, "green": 1, "blue": 1}},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": n_cols},
        }},
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Create I18N_LWC / I18N_Flows tabs (gated)")
    ap.add_argument("--spreadsheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from googleapiclient.discovery import build
    import google.auth
    # read-only is enough to list; write service only on --apply
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    read = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = read.spreadsheets().get(spreadsheetId=args.spreadsheet_id,
                                   includeGridData=False).execute()
    titles = {s["properties"]["title"] for s in meta["sheets"]}
    title_of_book = meta["properties"]["title"]

    plan = []
    if LWC_TAB not in titles:
        plan.append((LWC_TAB, LWC_HEADERS))
    if FLOW_TAB not in titles:
        plan.append((FLOW_TAB, FLOW_HEADERS))

    print(f"spreadsheet: {title_of_book}  ({args.spreadsheet_id})")
    print("=" * 72)
    if not plan:
        print(f"  both {LWC_TAB!r} and {FLOW_TAB!r} already exist — nothing to create.")
        return 0
    for name, headers in plan:
        print(f"  NEW TAB  {name}")
        print(f"    headers ({len(headers)}): {', '.join(headers)}")
    print("=" * 72)

    if not args.apply:
        print("DRY-RUN — no sheet write. Re-run with --apply after confirmation.")
        return 0

    svc = get_write_service()
    add_reqs = [{"addSheet": {"properties": {
        "title": name,
        "gridProperties": {"rowCount": 2000, "columnCount": max(26, len(headers))},
    }}} for name, headers in plan]
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=args.spreadsheet_id,
        body={"requests": add_reqs}).execute()
    replies = resp.get("replies") or []
    fmt_reqs = []
    value_data = []
    for (name, headers), reply in zip(plan, replies):
        sid = reply["addSheet"]["properties"]["sheetId"]
        print(f"  created {name!r} (sheetId={sid})")
        fmt_reqs.extend(_header_format_requests(sid, len(headers)))
        value_data.append({"range": f"'{name}'!A1", "values": [headers]})
    if fmt_reqs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=args.spreadsheet_id,
            body={"requests": fmt_reqs}).execute()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=args.spreadsheet_id,
        body={"valueInputOption": "RAW", "data": value_data}).execute()
    print("  header rows written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
