#!/usr/bin/env python3
"""
write_back.py — Write generated field API names into an object tab's fullName
column. Gated by the user (sheet-write-confirmation rule) BEFORE running.

Safety:
  * Re-reads the tab LIVE right now (never trusts a cached snapshot).
  * Matches each proposed name to its field by SHEET ROW + LABEL (never by
    label alone — labels repeat). Aborts on any label mismatch.
  * Only writes cells that are currently BLANK; if a target cell already has a
    value, it is skipped and reported (no overwrite without a fresh decision).
  * Writes values as RAW text (no formula interpretation).

  python scripts/write_back.py --spreadsheet-id <ID> --tab "成約:Sales_Deal" \
      --final .build/sales_deal_FINAL.json [--apply]

Without --apply it performs a dry run (verifies alignment, writes nothing).
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "scripts")
from fetch_sheet import (  # noqa: E402
    find_header_row, norm, GRAY_GUARD_SUBSTR, is_field_list_end, find_helper_cols, WIP_TRUE,
)


def get_write_service():
    from googleapiclient.discovery import build
    import google.auth
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds, _ = google.auth.default(scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def live_field_rows(svc, sid: str, tab: str):
    grid = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'",
        valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    hidx = find_header_row(grid)
    header = [norm(c).lower() for c in grid[hidx]]
    label_col = header.index("label")
    full_col = header.index("fullname")
    wip_idx = find_helper_cols(grid[hidx])["wip"]  # header-driven (AE fallback)
    rows = []
    skipped_wip = 0
    for i in range(hidx + 1, len(grid)):
        cells = [norm(c) for c in grid[i]]
        if is_field_list_end(cells):
            break
        label = cells[label_col] if label_col < len(cells) else ""
        full = cells[full_col] if full_col < len(cells) else ""
        if not (label or full):
            continue
        # Never name a WIP row (WIP col = true/x); fetch_sheet excludes them too, so
        # skipping here keeps blank-row alignment with the naming proposal.
        wip = cells[wip_idx] if wip_idx < len(cells) else ""
        if wip.strip().lower() in WIP_TRUE:
            skipped_wip += 1
            continue
        rows.append({"row": i + 1, "label": label, "full": full})
    if skipped_wip:
        print(f"(skipped {skipped_wip} WIP row(s) — not named/written)")
    return rows, full_col


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--final", default=".build/sales_deal_FINAL.json")
    ap.add_argument("--apply", action="store_true", help="actually write (else dry run)")
    args = ap.parse_args()

    final = json.load(open(args.final, encoding="utf-8"))
    proposals = [f for f in final["fields"] if f.get("api")]

    svc = get_write_service()
    rows, full_col = live_field_rows(svc, args.spreadsheet_id, args.tab)
    col = col_letter(full_col)
    blank_rows = [r for r in rows if not r["full"]]

    # alignment check: same count and label-by-label match, in order
    if len(blank_rows) != len(proposals):
        print(f"ABORT: {len(blank_rows)} blank rows live but {len(proposals)} proposals.")
        return 1
    mismatches = [(br["row"], br["label"], p["label"])
                  for br, p in zip(blank_rows, proposals) if br["label"] != p["label"]]
    if mismatches:
        print(f"ABORT: {len(mismatches)} label mismatch(es). First few:")
        for row, live, prop in mismatches[:10]:
            print(f"  row {row}: live '{live}' != proposal '{prop}'")
        return 1

    data = [{"range": f"'{args.tab}'!{col}{br['row']}", "values": [[p["api"]]]}
            for br, p in zip(blank_rows, proposals)]
    first, last = blank_rows[0]["row"], blank_rows[-1]["row"]
    contiguous = last - first + 1 == len(blank_rows)

    print(f"tab: {args.tab} | fullName col: {col}")
    print(f"aligned OK: {len(data)} cells, rows {first}..{last}, contiguous={contiguous}")
    print(f"sample: {col}{first}={proposals[0]['api']}  ...  {col}{last}={proposals[-1]['api']}")

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    resp = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=args.spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"WROTE: {resp.get('totalUpdatedCells')} cells "
          f"across {resp.get('totalUpdatedRanges')} ranges.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
