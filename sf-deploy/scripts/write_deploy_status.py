#!/usr/bin/env python3
"""
write_deploy_status.py — Fill the per-field Deployment-Status column (AI) on
object tabs, and (optionally) rename the AH / AI headers to the client's new
names. Gated by the sheet-write-confirmation rule (dry-run unless --apply).

For each field row on an object tab it writes column AI (`Deployment Status`):
  * "Deployed"     — custom field (__c) whose API name EXISTS in the target org
                     (checked LIVE via the Tooling `CustomField` object, which is
                     FLS-independent — never `sobject describe`).
  * "Not Deployed" — custom field (__c) whose API name is NOT in the org.
  * "Standard"     — standard field (no __c, e.g. OwnerId/Name/CreatedDate).
  * "Deleted"      — a row flagged IsDelete (AD) whose field is confirmed ABSENT
                     from the org (the destructive delete landed). Operation-based
                     status per sf-deploy-status-column-always.mdc.
WIP rows and blank-API/noise rows are skipped.

Header rename (with --set-headers):
  * AH -> "GDC AI Tool Comments"   (was FreeColumnGDC1)
  * AI -> "Deployment Status"      (was FreeColumnGDC2)

  # preview only (no write):
  python scripts/write_deploy_status.py --spreadsheet-id <ID> \
      --tabs "DeliveryDestination,SDS" --target-org ERPDEV01 --set-headers
  # apply:
  python scripts/write_deploy_status.py --spreadsheet-id <ID> \
      --tabs "DeliveryDestination,SDS" --target-org ERPDEV01 --set-headers --apply
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, "scripts")
from fetch_sheet import (  # noqa: E402
    get_sheets_service, find_header_row, build_col_map, find_helper_cols,
    norm, GRAY_GUARD_SUBSTR, is_field_list_end, WIP_TRUE, is_object_tab, load_object_index,
    parse_object_header,
)

AH_HEADER_NEW = "GDC AI Tool Comments"
AI_HEADER_NEW = "Deployment Status"

# HOME used for the `sf` CLI subprocesses (its auth lives under the .sfhome shim),
# distinct from the main process HOME which Google ADC needs. Set via --sf-home.
_SF_ENV = dict(os.environ)


def sf_run(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True, env=_SF_ENV)


# REST-token fallback: in sandboxed environments the `sf` CLI cannot rotate its
# auth file (~/.sfdx write blocked) so `sf data query` fails/returns empty. When a
# live token file exists (.build/orgauth.json), query the org directly over REST
# (FLS-independent Tooling API), matching the CLI's semantics.
def _rest_auth():
    try:
        import audit_lib  # noqa: E402
        return audit_lib, audit_lib.load_auth()
    except Exception:
        return None, None


def col_letter(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def write_service():
    from googleapiclient.discovery import build
    import google.auth
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def org_custom_fields(obj_api: str, target_org: str) -> set[str]:
    """LIVE set of custom field API names (with __c) on the object, via Tooling API."""
    q = ("SELECT DeveloperName FROM CustomField WHERE "
         f"EntityDefinition.QualifiedApiName='{obj_api}'")
    A, auth = _rest_auth()
    if A is not None:
        recs = A.rest_query(q, auth, tooling=True)
        return {r["DeveloperName"] + "__c" for r in recs}
    out = sf_run(
        ["sf", "data", "query", "--use-tooling-api", "--target-org", target_org,
         "--json", "--query", q])
    if out.returncode != 0:
        raise SystemExit(f"org query failed for {obj_api}: {out.stderr[:300]}")
    recs = json.loads(out.stdout).get("result", {}).get("records", [])
    return {r["DeveloperName"] + "__c" for r in recs}


def obj_exists(obj_api: str, target_org: str) -> bool:
    q = ("SELECT QualifiedApiName FROM EntityDefinition "
         f"WHERE QualifiedApiName='{obj_api}'")
    A, auth = _rest_auth()
    if A is not None:
        return len(A.rest_query(q, auth)) == 1
    out = sf_run(
        ["sf", "data", "query", "--target-org", target_org, "--json", "--query", q])
    if out.returncode != 0:
        return False
    return json.loads(out.stdout).get("result", {}).get("totalSize", 0) == 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill AI deploy-status column + rename AH/AI headers")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tabs", default="", help="comma-separated object tabs; default=all")
    ap.add_argument("--target-org", required=True)
    ap.add_argument("--set-headers", action="store_true",
                    help="also rename AH->'GDC AI Tool Comments', AI->'Deployment Status'")
    ap.add_argument("--apply", action="store_true", help="write (default: preview only)")
    ap.add_argument("--sf-home", default="",
                    help="HOME for the sf CLI subprocesses (its auth shim); the main "
                         "process HOME stays as-is for Google ADC")
    args = ap.parse_args()

    if args.sf_home:
        _SF_ENV["HOME"] = args.sf_home
        _SF_ENV["XDG_DATA_HOME"] = os.environ.get("XDG_DATA_HOME", "/Users/abhi.chauhan/.local/share")

    svc = get_sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=args.spreadsheet_id,
                                  includeGridData=False).execute()
    all_titles = [s["properties"]["title"] for s in meta["sheets"]]
    if args.tabs.strip():
        tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    else:
        tabs = [t for t in all_titles if is_object_tab(t)]

    obj_index = load_object_index(svc, args.spreadsheet_id)

    data = []            # cell writes
    summary = []
    for tab in tabs:
        grid = svc.spreadsheets().values().get(
            spreadsheetId=args.spreadsheet_id, range=f"'{tab}'",
            valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
        h = find_header_row(grid)
        if h is None:
            print(f"  ⚠️  {tab}: no header row — skipped"); continue
        cm = build_col_map(grid[h]); col = {v: k for k, v in cm.items()}
        helper = find_helper_cols(grid[h])
        c_lab = col.get("Field Label"); c_api = col.get("Field API Name")
        c_typ = col.get("Data Type")
        c_ai = helper.get("deploy_status"); c_ah = helper.get("ai_comment")
        c_del = helper.get("isdelete")

        def g(cells, i):
            return norm(cells[i]) if (i is not None and i < len(cells)) else ""

        # object API name: object-meta header block > index-tab hint
        obj_meta = parse_object_header(grid, h)
        obj_api = norm(obj_meta.get("Object API Name")) or norm(obj_index.get(tab, ""))
        # gather candidate rows
        field_rows = []
        for gi in range(h + 1, len(grid)):
            cells = grid[gi]
            if is_field_list_end(cells):
                break
            if g(cells, helper.get("wip")).lower() in WIP_TRUE:
                continue
            lab = g(cells, c_lab); api = g(cells, c_api); typ = g(cells, c_typ)
            if not api and not (lab and typ):
                continue
            isdel = g(cells, c_del).lower() in ("true", "x")
            field_rows.append((gi, api, lab, typ, isdel))

        exists = obj_exists(obj_api, args.target_org) if obj_api else False
        org_fields = org_custom_fields(obj_api, args.target_org) if exists else set()

        # header rename
        if args.set_headers:
            if c_ah is not None and norm(grid[h][c_ah] if c_ah < len(grid[h]) else "") != AH_HEADER_NEW:
                data.append({"range": f"'{tab}'!{col_letter(c_ah)}{h+1}", "values": [[AH_HEADER_NEW]]})
            if c_ai is not None and norm(grid[h][c_ai] if c_ai < len(grid[h]) else "") != AI_HEADER_NEW:
                data.append({"range": f"'{tab}'!{col_letter(c_ai)}{h+1}", "values": [[AI_HEADER_NEW]]})

        n_dep = n_not = n_std = n_del = 0
        for gi, api, lab, typ, isdel in field_rows:
            if not api:
                continue  # blank-api noise row — no status
            if not api.endswith("__c"):
                status = "Standard"; n_std += 1
            elif isdel and api not in org_fields:
                # IsDelete row whose field is confirmed ABSENT from the org =>
                # the destructive delete landed. Operation-based status per
                # sf-deploy-status-column-always.mdc.
                status = "Deleted"; n_del += 1
            elif api in org_fields:
                status = "Deployed"; n_dep += 1
            else:
                status = "Not Deployed"; n_not += 1
            cur = g(grid[gi], c_ai)
            if cur != status and c_ai is not None:
                data.append({"range": f"'{tab}'!{col_letter(c_ai)}{gi+1}", "values": [[status]]})
        summary.append((tab, obj_api, exists, n_dep, n_not, n_std, n_del))

    print("=" * 70)
    print(f"Deploy-status write  ({'APPLY' if args.apply else 'PREVIEW'})  target-org={args.target_org}")
    print("=" * 70)
    for tab, obj, ex, dep, notdep, std, deleted in summary:
        flag = "" if ex else "  [OBJECT MISSING]"
        print(f"  {tab:34} {obj:38} Deployed={dep} NotDeployed={notdep} Standard={std} Deleted={deleted}{flag}")
    print(f"\nTotal cell writes queued: {len(data)}"
          f"  (incl. header renames)" if args.set_headers else f"\nTotal cell writes queued: {len(data)}")

    if args.apply and data:
        resp = write_service().spreadsheets().values().batchUpdate(
            spreadsheetId=args.spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data}).execute()
        print("APPLIED. totalUpdatedCells =", resp.get("totalUpdatedCells"))
    elif not args.apply:
        print("(dry-run — nothing written; re-run with --apply after confirmation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
