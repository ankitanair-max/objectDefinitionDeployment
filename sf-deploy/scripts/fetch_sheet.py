#!/usr/bin/env python3
"""
fetch_sheet.py — Local, GAS-free extraction of Data Dictionary object tabs
from a Google Sheet into `temp_updates.json` (the exact shape the existing
`generate_xml.py` generator expects).

Replaces the CI-only inline `fetch_data.js`. Runs fully locally using the
user's Google credentials:
  1. Application Default Credentials (ADC)  — `gcloud auth application-default
     login --scopes=...spreadsheets,drive`  (preferred, deploys as the user), OR
  2. a service-account JSON via GOOGLE_SERVICE_ACCOUNT_JSON.

Usage:
  python scripts/fetch_sheet.py \
      --spreadsheet-id <ID> \
      [--tabs "成約,輸入・入庫管理明細"]   # default: every object tab (auto-detected)
      [--out temp_updates.json]

Object tabs are auto-detected: any tab NOT in the known non-object set
(表紙 / オブジェクト一覧 / 変更履歴 / 変更ログ / フォーマット / Glossary / Data Glossary /
ユーザー一覧 / データ型のマッピング / IFログ など).

The field header row (row containing the API headers `fullName` + `type`) is
located dynamically per tab, because object tabs place it at different rows
(10, 11 or 12) depending on the object-header block size.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from translation_lib import EN_FLAG  # noqa: E402  (stdlib-only module)

# --------------------------------------------------------------------------- #
# Column mapping: Row-12 API header (case-insensitive)  ->  temp_updates.json key
# (the JSON keys are what generate_xml.py's build_field_xml/process_fields read)
# --------------------------------------------------------------------------- #
API_HEADER_TO_KEY = {
    "label": "Field Label",
    "fullname": "Field API Name",
    "type": "Data Type",
    "length": "Length",
    "required": "Required",
    "unique": "Unique",
    "externalid": "External ID",
    "defaultvalue": "Default Value",
    "trackhistory": "Track History",
    "inlinehelptext": "Help Text",
    "description": "Description",
    "valueset.restricted": "Restricted",
    "valueset.valuesetname": "Global Value Set",
    "visiblelines": "Visible Lines",
    "precision": "Precision",
    "scale": "Scale",
    "maskchar": "Mask Character",
    "relationshiplabel": "Relationship Label",
    "relationshipname": "Relationship Name",
    "deleteconstraint": "Delete Constraint",
    # Translation Workbench EN columns (header-driven; live on the object tab).
    "field label (en)": "Field Label (EN)",
    "label (en)": "Field Label (EN)",
    "label_en": "Field Label (EN)",
    "picklist values (en)": "Picklist Values (EN)",
    "picklist (en)": "Picklist Values (EN)",
    "help text (en)": "Help Text (EN)",
    "inlinehelptext (en)": "Help Text (EN)",
    "relationship label (en)": "Relationship Label (EN)",
}
# The polymorphic Column H header is long; match it by prefix (header-driven,
# so it follows the column regardless of its physical letter).
COL_H_PREFIX = "displayformat"  # "displayFormat / referenceTo / formula / valueSet"
COL_H_KEY = "Type Specific Value"

# Tabs that are never object metadata sheets.
NON_OBJECT_TABS = {
    "表紙", "オブジェクト一覧", "変更履歴", "変更ログ", "フォーマット",
    "glossary", "data glossary", "ユーザー一覧", "データ型のマッピング",
    "ifログ", "変更ログ", "jetファイル作成指示書",
}

GRAY_GUARD_SUBSTR = "行挿入する場合は当行より上部"  # LEGACY end-of-field-list marker (col C)
# PRIMARY end-of-field-list marker (confirmed by the sheet owner, Takeaki Yagai,
# 2026-09-01): every block on a "Format"-style tab is terminated by an END[XXX]
# cell in column A (No. column), where XXX is the block name — i.e. END[XXX] marks
# the end of the area corresponding to XXX. The FIELD list is always the FIRST
# block, so the first END[...] cell (END[項目]) marks the end of the fields;
# everything after it (END[レコードタイプ], END[入力規則], END[ルックアップ検索条件], …)
# must NOT be parsed as fields. Col A only ever holds row numbers or these
# markers, so matching on the leading "END[" in col A is safe. The gray-guard
# line above is kept only as a fallback for older tabs that predate this marker.
FIELD_LIST_END_PREFIX = "END["


def is_field_list_end(cells) -> bool:
    """Return True if this row terminates the FIELD list.

    Two accepted terminators (confirmed with the sheet owner, 2026-09-01):
      * PRIMARY  — an ``END[…]`` block marker in column A (e.g. ``END[項目]``);
      * LEGACY   — the gray-guard line (``行挿入する場合は当行より上部``) in col C,
                   kept only for older tabs that predate the ``END[…]`` convention.

    Robust to raw or already-normalized cells (substring / stripped-prefix match).
    Every script that scans an object tab row-by-row MUST use this so the
    field-list boundary can never drift between scripts.
    """
    if any(GRAY_GUARD_SUBSTR in str(c) for c in cells):
        return True
    return bool(cells) and str(cells[0]).strip().startswith(FIELD_LIST_END_PREFIX)

# Helper columns per the client "Format" sheet, located by HEADER NAME (the client
# keeps the order stable, but header-driven lookup is reshuffle-proof). Fallback
# 0-based indices: Z=25 Tab, AA=26 section, AD=29 IsDelete, AE=30 WIP.
#   * A row with WIP true/x is work-in-progress → ignored entirely.
#   * A row with IsDelete true is a deletion request → excluded from the create
#     package (routed to the destructive DELETE flow).
# See .cursor/rules/sf-sheet-columns.mdc.
HELPER_FALLBACK = {"tab": 25, "section": 26, "isdelete": 29, "wip": 30,
                   "ai_comment": 33, "deploy_status": 34}
WIP_TRUE = {"x", "true", "1", "yes", "○", "〇"}
DELETE_TRUE = {"true", "1", "yes", "x", "○", "〇"}

# Header aliases for the AI columns (AH review comments, AI deploy status). The
# client renamed AH FreeColumnGDC1 -> "GDC AI Tool Comments" and AI
# FreeColumnGDC2 -> "Deployment Status"; match the new names first, keep the old
# ones for backwards compatibility.
HELPER_HEADER_ALIASES = {
    "ai_comment": ("gdc ai tool comments", "freecolumngdc1"),
    "deploy_status": ("deployment status", "freecolumngdc2"),
}


def find_helper_cols(header_row: list) -> dict:
    """Locate Tab / section / IsDelete / WIP / AH / AI by header name (fallback to index)."""
    lowered = [norm(c).lower() for c in header_row]
    cols = {}
    for key, fb in HELPER_FALLBACK.items():
        idx = None
        # try aliases first (for the AI columns), then the literal key name
        for alias in HELPER_HEADER_ALIASES.get(key, ()):
            if alias in lowered:
                idx = lowered.index(alias)
                break
        if idx is None and key in lowered:
            idx = lowered.index(key)
        cols[key] = idx if idx is not None else fb
    return cols


def get_sheets_service():
    """Build a Sheets API client from ADC or a service-account JSON."""
    from googleapiclient.discovery import build

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    sa_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_raw:
        import base64
        from google.oauth2 import service_account

        if sa_raw.startswith("{"):
            info = json.loads(sa_raw)
        elif sa_raw.startswith("/") or sa_raw.lower().endswith(".json"):
            info = json.load(open(sa_raw))
        else:
            info = json.loads(base64.b64decode(sa_raw).decode())
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        import google.auth

        creds, _ = google.auth.default(scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def norm(s) -> str:
    return str(s or "").strip()


def is_object_tab(title: str) -> bool:
    return title.strip().lower() not in {t.lower() for t in NON_OBJECT_TABS}


def find_header_row(grid: list[list]) -> int | None:
    """Return 0-based index of the API header row (has 'fullName' and 'type')."""
    for i, row in enumerate(grid[:20]):
        lowered = [norm(c).lower() for c in row]
        if "fullname" in lowered and "type" in lowered:
            return i
    return None


def build_col_map(header_row: list, jp_header_row: list | None = None) -> dict[int, str]:
    """Map column index -> temp_updates.json key.

    The client "Format" sheet has TWO polymorphic value columns whose machine
    API-header row is ambiguous — it carries ``defaultValue`` on BOTH the
    ``数式(設定値)`` column (the REAL type-specific value: formula body /
    picklist values / referenceTo / displayFormat) AND the ``デフォルト値``
    column (the real field default). It also carries
    ``displayFormat / referenceTo / formula / valueSet`` on the
    ``データ型に応じて…`` column, which in this sheet is only a human DESCRIPTION
    column, not the deployed value.

    Per the sheet owner's layout (confirmed 2026-09-09), the deployed value
    lives in ``数式(設定値)`` (col H), NOT in ``データ型に応じて…`` (col G). Because
    the English API-header can't tell H from L (both say ``defaultValue``) and
    mislabels G, we disambiguate by the UNIQUE Japanese header substrings:
      * ``設定値`` → ``数式(設定値)`` (col H)  → Type Specific Value
      * ``デフォルト`` → ``デフォルト値`` (col L) → Default Value
    The ``データ型に応じて…`` column (col G) is a description → NOT read
    (no fall back to G when H is blank; a blank H is surfaced as a blocker).
    """
    col_map: dict[int, str] = {}
    jp = [norm(c) for c in (jp_header_row or [])]
    for idx, cell in enumerate(header_row):
        h = norm(cell).lower()
        jph = jp[idx] if idx < len(jp) else ""
        if not h and not jph:
            continue
        # 1) JP-header disambiguation wins (reshuffle-proof, unambiguous).
        if "設定値" in jph:                       # 数式(設定値)  (col H)
            col_map[idx] = COL_H_KEY              # Type Specific Value
            continue
        if "デフォルト" in jph:                    # デフォルト値  (col L)
            col_map[idx] = "Default Value"
            continue
        # 2) The 'displayFormat/referenceTo/formula/valueSet' header sits on the
        #    DESCRIPTION column (col G) in this sheet — do NOT read it as the
        #    value (client's real value is col H, handled above).
        if h.startswith(COL_H_PREFIX):
            continue
        # 3) Everything else maps by the English API header.
        if h in API_HEADER_TO_KEY:
            col_map[idx] = API_HEADER_TO_KEY[h]
            continue
        # JP header "表示ラベル (EN)" etc. when the English API-header row is blank
        jph_l = jph.lower()
        if "表示ラベル" in jph and "(en)" in jph_l:
            col_map[idx] = "Field Label (EN)"
        elif "項目ラベル名" in jph and "(en)" in jph_l:
            col_map[idx] = "Field Label (EN)"
        elif ("選択リスト" in jph or "picklist" in jph_l) and "(en)" in jph_l:
            col_map[idx] = "Picklist Values (EN)"
        elif ("ヘルプ" in jph or "help" in jph_l) and "(en)" in jph_l:
            col_map[idx] = "Help Text (EN)"
    return col_map


def parse_object_header(grid: list[list], header_idx: int) -> dict:
    """Extract object-level metadata (rows above the field header row).

    Anchors are the Japanese labels; the value sits a few columns to the right.
    """
    meta = {"_type": "object_meta"}
    label = api = desc = ""
    er = ea = eh = es = ""
    for row in grid[:header_idx]:
        cells = [norm(c) for c in row]
        joined = [c for c in cells]
        for j, c in enumerate(cells):
            if c == "表示ラベル":
                label = _first_nonblank(joined, j + 1)
                # オブジェクト名 label is usually further right on the same row
                if "オブジェクト名" in cells:
                    k = cells.index("オブジェクト名")
                    api = _first_nonblank(joined, k + 1)
            elif c.lower() in ("object label (en)", "表示ラベル (en)", "オブジェクトラベル (en)"):
                meta["Object Label (EN)"] = _first_nonblank(joined, j + 1)
            elif c == "説明":
                desc = _first_nonblank(joined, j + 1)
            elif c == "レポートを許可":
                # value row is typically the next grid row; handled below
                pass
        # enable flags: a row that literally holds the value under the label row
    # Enable flags: scan for the label row then read the row beneath it.
    for r, row in enumerate(grid[:header_idx]):
        cells = [norm(c) for c in row]
        if "レポートを許可" in cells and r + 1 < header_idx:
            vals = [norm(c) for c in grid[r + 1]]
            labelrow = cells
            for lbl, key in (("レポートを許可", "er"), ("活動を許可", "ea"),
                             ("項目履歴管理", "eh"), ("検索を許可", "es")):
                if lbl in labelrow:
                    ci = labelrow.index(lbl)
                    v = vals[ci] if ci < len(vals) else ""
                    if key == "er":
                        er = v
                    elif key == "ea":
                        ea = v
                    elif key == "eh":
                        eh = v
                    elif key == "es":
                        es = v
    meta.update({
        "Object Label": label,
        "Object API Name": api,
        "Object Description": desc,
        "enableReports": er,
        "enableActivities": ea,
        "enableHistory": eh,
        "enableSearch": es,
    })
    return meta


def _first_nonblank(cells: list[str], start: int) -> str:
    for c in cells[start:]:
        if norm(c):
            return norm(c)
    return ""


def parse_tab(title: str, grid: list[list], object_api_hint: str = "") -> list[dict]:
    """Parse one object tab into object_meta + field rows."""
    header_idx = find_header_row(grid)
    if header_idx is None:
        print(f"  ⚠️  {title}: no field header row (fullName/type) found — skipped.")
        return []

    col_map = build_col_map(grid[header_idx], grid[header_idx - 1] if header_idx > 0 else None)
    helper = find_helper_cols(grid[header_idx])
    obj_meta = parse_object_header(grid, header_idx)

    # Object-level English is row 1 of the Field Label (EN) column.
    # Locate by header via col_map.
    en_cols = [i for i, k in col_map.items() if k == "Field Label (EN)"]
    # Whether this tab is translated AT ALL. Downstream (plan_deploy /
    # generate_object_translation) skips untranslated tabs entirely, so an
    # ordinary field deploy never acquires a translation dependency.
    has_en = bool(en_cols)
    if en_cols and grid:
        c = en_cols[0]
        row1 = grid[0] if grid else []
        obj_en = norm(row1[c]) if c < len(row1) else ""
        if obj_en and not obj_meta.get("Object Label (EN)"):
            obj_meta["Object Label (EN)"] = obj_en

    # Resolve object API: header value > index-tab hint > (leave blank -> warn)
    obj_api = norm(obj_meta.get("Object API Name")) or norm(object_api_hint)
    obj_label = norm(obj_meta.get("Object Label")) or title
    obj_meta["Object API Name"] = obj_api
    obj_meta["Object Label"] = obj_label

    rows: list[dict] = []
    field_rows: list[dict] = []
    name_field = {}
    # Skipped rows never enter the field list (generate_xml must not see them),
    # but the deployment plan has to REPORT them, so their API names ride on the
    # object-meta row: WIP = ignore entirely, IsDelete = destructive set.
    wip_rows: list[str] = []
    delete_rows: list[str] = []

    def cell_at(cells, key):
        idx = helper[key]
        return cells[idx].strip() if idx < len(cells) else ""

    for row in grid[header_idx + 1:]:
        cells = [norm(c) for c in row]
        # stop at the end of the FIELD list — END[項目] (col A) or the legacy
        # gray-guard line (col C). Shared helper so the boundary never drifts.
        if is_field_list_end(cells):
            break
        rec = {"_SheetName": title, "Object API Name": obj_api,
               "Object Label": obj_label, EN_FLAG: has_en}
        for idx, key in col_map.items():
            rec[key] = cells[idx] if idx < len(cells) else ""
        # capture page-layout Tab (Z) + section (AA) for downstream page work
        rec["Tab"] = cell_at(cells, "tab")
        rec["Section"] = cell_at(cells, "section")
        # a real field row must have an API name or a label+type
        if not (norm(rec.get("Field API Name")) or
                (norm(rec.get("Field Label")) and norm(rec.get("Data Type")))):
            continue
        # WIP gate: skip rows flagged true/x in the WIP column (AE) — work in progress
        if cell_at(cells, "wip").lower() in WIP_TRUE:
            wip_rows.append(norm(rec.get("Field API Name"))
                            or f"<{norm(rec.get('Field Label')) or 'unnamed'}>")
            continue
        # IsDelete gate: a deletion request (AD) is excluded from the create package
        # (routed to the destructive DELETE flow; see sf-deploy-delta-and-blockers).
        if cell_at(cells, "isdelete").lower() in DELETE_TRUE:
            delete_rows.append(norm(rec.get("Field API Name"))
                               or f"<{norm(rec.get('Field Label')) or 'unnamed'}>")
            continue
        # capture Name field for the object's <nameField>
        if norm(rec.get("Field API Name")) == "Name":
            name_field = {
                "Name Field Label": rec.get("Field Label", ""),
                "Name Field Type": rec.get("Data Type", ""),
                "Name Field Display Format": rec.get("Type Specific Value", ""),
                "Name Field Label (EN)": rec.get("Field Label (EN)", ""),
            }
            continue
        field_rows.append(rec)

    obj_meta.update(name_field)
    obj_meta[EN_FLAG] = has_en
    obj_meta["_WipSkipped"] = wip_rows
    obj_meta["_DeleteRequested"] = delete_rows
    obj_meta.setdefault("_SheetName", title)
    if obj_api:
        rows.append(obj_meta)
    else:
        print(f"  ⚠️  {title}: Object API Name not found (header blank & no index hint).")
    rows.extend(field_rows)
    wip_note = f", {len(wip_rows)} WIP(AE) skipped" if wip_rows else ""
    del_note = f", {len(delete_rows)} IsDelete(AD) excluded" if delete_rows else ""
    print(f"  ✓ {title}: {len(field_rows)} field(s){wip_note}{del_note}, object_api='{obj_api or '?'}'")
    return rows


def load_object_index(svc, spreadsheet_id: str) -> dict[str, str]:
    """Read オブジェクト一覧 to map object label -> API name (F=label, G=API, row 3+)."""
    try:
        vals = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="'オブジェクト一覧'",
            valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    except Exception:
        return {}
    idx: dict[str, str] = {}
    for row in vals[2:]:
        # columns F (5) label, G (6) api per docs; be tolerant
        if len(row) > 6:
            label, api = norm(row[5]), norm(row[6])
            if label and api:
                idx[label] = api
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch DD object tabs -> temp_updates.json")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tabs", default="", help="comma-separated tab names; default=all object tabs")
    ap.add_argument("--list-tabs", action="store_true",
                    help="list the available object tabs and exit (scope-selection gate)")
    ap.add_argument("--out", default="temp_updates.json")
    args = ap.parse_args()

    svc = get_sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=args.spreadsheet_id,
                                  includeGridData=False).execute()
    all_titles = [s["properties"]["title"] for s in meta["sheets"]]

    if args.list_tabs:
        obj_tabs = [t for t in all_titles if is_object_tab(t)]
        print(f"Spreadsheet: {meta['properties']['title']}")
        print(f"Object tabs ({len(obj_tabs)}):")
        for i, t in enumerate(obj_tabs, 1):
            print(f"  [{i}] {t}")
        skipped = [t for t in all_titles if not is_object_tab(t)]
        if skipped:
            print(f"(non-object tabs, ignored: {', '.join(skipped)})")
        return 0

    if args.tabs.strip():
        wanted = [t.strip() for t in args.tabs.split(",") if t.strip()]
    else:
        wanted = [t for t in all_titles if is_object_tab(t)]

    print(f"Spreadsheet: {meta['properties']['title']}")
    print(f"Object tabs to fetch ({len(wanted)}): {', '.join(wanted)}")

    obj_index = load_object_index(svc, args.spreadsheet_id)

    out_rows: list[dict] = []
    for title in wanted:
        try:
            grid = svc.spreadsheets().values().get(
                spreadsheetId=args.spreadsheet_id, range=f"'{title}'",
                valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
        except Exception as e:
            print(f"  ⚠️  {title}: read failed — {e}")
            continue
        hint = obj_index.get(title, "")
        out_rows.extend(parse_tab(title, grid, object_api_hint=hint))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_rows, f, ensure_ascii=False, indent=2)

    n_obj = sum(1 for r in out_rows if r.get("_type") == "object_meta")
    n_fld = len(out_rows) - n_obj
    print(f"\nWrote {args.out}: {n_obj} object(s), {n_fld} field(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
