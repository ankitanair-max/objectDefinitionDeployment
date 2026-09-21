#!/usr/bin/env python3
"""
write_ai_comments.py — Write the AI's review comments into an object tab's
AI-comment column (AH / header `FreeColumnGDC1`), gated by the sheet-write
confirmation rule.

Two sources of comments (the two stages from the requirement):
  * STAGE 1 — sheet validation: a `validate_sheet.py --json <report>` file. Every
    ERROR/WARN row becomes a `[VALIDATION <date>] <check>: <message>` note on that
    field's row.
  * STAGE 2 — on-the-go deploy fixes: a `--fixes <json>` file shaped as
    [{"field": "<API or label>", "note": "<what was fixed>"}, ...]. Each becomes a
    `[FIX <date>] <note>` note on that field's row.

Safety (mirrors write_back.py):
  * Re-reads the tab LIVE right now (never a cached snapshot).
  * Locates columns by HEADER NAME (fullName / label / WIP / FreeColumnGDC1),
    robust to the client's column reshuffle; falls back to fixed indices.
  * Skips WIP rows (header `WIP`, now col AE) — value true/x (any case).
  * APPENDS to any existing AH text (never clobbers colleague notes); de-dupes a
    line that is already present.
  * Default is a DRY RUN preview (cell: old -> new). Only `--apply` writes, and the
    write itself is still gated by the user's confirmation per sheet-write rule.

Usage:
  python scripts/write_ai_comments.py --spreadsheet-id <ID> --tab Shipping \
      --report .build/ship_recv_validation.json [--fixes .build/fixes.json] [--apply]
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

sys.path.insert(0, "scripts")
from fetch_sheet import find_header_row, norm, GRAY_GUARD_SUBSTR, is_field_list_end  # noqa: E402

# Client renamed AH header FreeColumnGDC1 -> "GDC AI Tool Comments"; match the new
# name first, keep the old one for backwards compatibility.
AH_HEADERS = ("gdc ai tool comments", "freecolumngdc1")
AH_FALLBACK_IDX = 33           # column AH, 0-based
WIP_FALLBACK_IDX = 30          # column AE, 0-based (new Format)
WIP_TRUE = {"x", "true", "1", "yes", "○", "〇"}


def get_write_service():
    from googleapiclient.discovery import build
    import google.auth
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def header_index(header: list[str], name: str, fallback: int) -> int:
    try:
        return header.index(name)
    except ValueError:
        return fallback


def live_rows(svc, sid: str, tab: str):
    grid = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'",
        valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    hidx = find_header_row(grid)
    if hidx is None:
        raise SystemExit(f"{tab}: no field header row (fullName/type) found.")
    header = [norm(c).lower() for c in grid[hidx]]
    label_col = header_index(header, "label", 0)
    full_col = header_index(header, "fullname", 1)
    wip_col = header_index(header, "wip", WIP_FALLBACK_IDX)
    ah_col = next((header.index(h) for h in AH_HEADERS if h in header), AH_FALLBACK_IDX)
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
        wip = cells[wip_col] if wip_col < len(cells) else ""
        if wip.strip().lower() in WIP_TRUE:
            skipped_wip += 1
            continue
        ah = cells[ah_col] if ah_col < len(cells) else ""
        rows.append({"row": i + 1, "label": label, "full": full, "ah": ah})
    return rows, ah_col, skipped_wip


def build_comment_map(rows: list[dict], report: dict | None,
                      fixes: list[dict] | None, today: str,
                      obj_filter: str = "") -> dict[int, list[str]]:
    """Return {row_number: [comment_line, ...]} matched to field rows."""
    per_row: dict[int, list[str]] = {}

    def candidates(field_token: str) -> list[dict]:
        """Rows whose fullName == token, else blank-fullName rows whose label == token."""
        exact = [r for r in rows if r["full"] and r["full"] == field_token]
        if exact:
            return exact
        return [r for r in rows if not r["full"] and r["label"] == field_token]

    def assign(field_token: str, line: str):
        cands = candidates(field_token)
        if not cands:
            per_row.setdefault(-1, []).append(f"[UNMATCHED {field_token}] {line}")
            return
        # attach to first candidate not already carrying this exact line
        for r in cands:
            existing = per_row.get(r["row"], [])
            if line in existing or line in (r["ah"] or ""):
                return
            per_row.setdefault(r["row"], []).append(line)
            return

    if report:
        for it in report.get("items", []):
            if it.get("severity") not in ("ERROR", "WARN"):
                continue
            if obj_filter and str(it.get("object") or "").strip() != obj_filter:
                continue
            token = str(it.get("field") or "").strip()
            line = f"[VALIDATION {today}] {it.get('severity')} {it.get('check')}: {it.get('message')}"
            assign(token, line)

    for fx in (fixes or []):
        token = str(fx.get("field") or "").strip()
        line = f"[FIX {today}] {fx.get('note')}"
        assign(token, line)

    return per_row


def main() -> int:
    ap = argparse.ArgumentParser(description="Write AI review comments into AH (FreeColumnGDC1)")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--report", default="", help="validate_sheet.py --json output")
    ap.add_argument("--object", default="", help="only use report items for this object API (e.g. TI_Fnt_Shipping__c)")
    ap.add_argument("--fixes", default="", help="JSON list [{field, note}] of on-the-go fixes")
    ap.add_argument("--apply", action="store_true", help="actually write (else dry-run preview)")
    args = ap.parse_args()

    if not args.report and not args.fixes:
        print("nothing to do: pass --report and/or --fixes")
        return 1

    report = json.load(open(args.report, encoding="utf-8")) if args.report else None
    fixes = json.load(open(args.fixes, encoding="utf-8")) if args.fixes else None
    today = datetime.date.today().isoformat()

    svc = get_write_service()
    rows, ah_col, skipped_wip = live_rows(svc, args.spreadsheet_id, args.tab)
    col = col_letter(ah_col)
    per_row = build_comment_map(rows, report, fixes, today, obj_filter=args.object)

    unmatched = per_row.pop(-1, [])
    if not per_row and not unmatched:
        print(f"{args.tab}: no ERROR/WARN/fix comments to write. (WIP skipped: {skipped_wip})")
        return 0

    row_ah = {r["row"]: r["ah"] for r in rows}
    data = []
    print(f"tab: {args.tab} | AI-comment col: {col} (header FreeColumnGDC1) | WIP skipped: {skipped_wip}")
    print("=" * 72)
    for rn in sorted(per_row):
        old = row_ah.get(rn, "")
        new_lines = per_row[rn]
        merged = "\n".join([old] + new_lines) if old else "\n".join(new_lines)
        data.append({"range": f"'{args.tab}'!{col}{rn}", "values": [[merged]]})
        print(f"  {col}{rn}:  OLD={old!r}")
        for ln in new_lines:
            print(f"           + {ln}")
    if unmatched:
        print("-" * 72)
        print(f"⚠️  {len(unmatched)} comment(s) could not be matched to a field row:")
        for u in unmatched[:15]:
            print(f"     {u}")
    print("=" * 72)
    print(f"total cells to write: {len(data)}")

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply (after user confirmation) to write.")
        return 0

    resp = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=args.spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    print(f"WROTE: {resp.get('totalUpdatedCells')} cells across {resp.get('totalUpdatedRanges')} ranges.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
