#!/usr/bin/env python3
"""Append (or refresh) one per-object tab in the deployment comparison workbook.

For a given object tab in the Google Sheet and a target org, this:
  1. Reads the object tab LIVE from the Google Sheet (source of truth).
  2. Categorises provided rows (WIP / standard / untyped / custom candidates).
  3. Queries the target org LIVE via the Tooling API (FieldDefinition) for the
     custom fields actually present on the object.
  4. Writes a single self-contained tab (named after the object) into
     reports/Object_Deployment_Report.xlsx, creating the workbook if needed and
     replacing the tab if it already exists.

Run one invocation per deployed object; each object gets its own tab.

Example:
  python3 scripts/build_object_report.py \
    --sheet-tab "成約:Sales_Deal" --object-api Sales_Deal__c \
    --target-org ERPDEV01 --deploy-id 0AfBK00000Bpi8j0AB \
    --fields-dir force-app/main/default/objects/Sales_Deal__c/fields
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SHEET_ID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
DEFAULT_WORKBOOK = os.path.join(ROOT, "reports", "Object_Deployment_Report.xlsx")
CUSTOM_PREFIX = "TI_Fnt_"
NCOLS = 4


def load_fetch_module():
    spec = importlib.util.spec_from_file_location("fetch_sheet", os.path.join(HERE, "fetch_sheet.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_sheet_rows(f, sheet_id: str, sheet_tab: str):
    svc = f.get_sheets_service()
    grid = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{sheet_tab}'", valueRenderOption="FORMATTED_VALUE")
        .execute()
        .get("values", [])
    )
    hi = f.find_header_row(grid)
    idx = dict(no=0, std=1, label=2, full=3, type=4, wip=27)
    rows = []
    for row in grid[hi + 1:]:
        c = [f.norm(x) for x in row]
        if f.is_field_list_end(c):
            break
        rows.append({k: (c[i] if i < len(c) else "") for k, i in idx.items()})
    return rows


def categorise(rows):
    truthy = lambda v: str(v).strip().lower() in ("true", "x", "yes", "1", "はい", "○", "o")
    is_wip = lambda r: r["wip"].strip().upper() == "X"
    wip = [r for r in rows if is_wip(r)]
    nw = [r for r in rows if not is_wip(r)]
    standard = [r for r in nw if truthy(r["std"]) or r["full"] == "Name" or (r["full"] and not r["full"].endswith("__c"))]
    untyped = [r for r in nw if r not in standard and (not r["type"] or r["type"] == "要確認")]
    custom = [r for r in nw if r not in standard and r not in untyped and r["full"].endswith("__c")]
    return wip, standard, untyped, custom


def query_org_fields(object_api: str, target_org: str, sf_home: str = "", xdg_data_home: str = ""):
    """Return {api_name: (label, dataType)} for all fields on the object (live Tooling API).

    The ``sf`` CLI needs a writable HOME + keychain access; in the sandbox that is
    provided via a workspace-local shim. That shim HOME must NOT leak into the main
    process (it would break Google ADC discovery), so it is applied only here, to
    the subprocess env.
    """
    q = (
        "SELECT QualifiedApiName, Label, DataType FROM FieldDefinition "
        f"WHERE EntityDefinition.QualifiedApiName='{object_api}'"
    )
    # REST-token fallback: in sandboxed envs the `sf` CLI cannot rotate its auth
    # file (~/.sfdx write blocked) so `sf data query` returns nothing, which made
    # the report's org column falsely show every field as "missing". When a live
    # token file exists (.build/orgauth.json), query the org directly over REST.
    try:
        sys.path.insert(0, "scripts")
        import audit_lib  # noqa: E402
        auth = audit_lib.load_auth()
        recs = audit_lib.rest_query(q, auth, tooling=True)
        if recs:
            return {r["QualifiedApiName"]: (r.get("Label") or "", r.get("DataType") or "")
                    for r in recs}
    except Exception:  # noqa: BLE001
        pass
    env = dict(os.environ, SF_DISABLE_LOG_FILE="true", SFDX_DISABLE_LOG_FILE="true")
    if sf_home:
        env["HOME"] = sf_home
        env["XDG_DATA_HOME"] = xdg_data_home or os.path.expanduser("~/.local/share")
    out = subprocess.run(
        ["sf", "data", "query", "--use-tooling-api", "--target-org", target_org, "--query", q, "--json"],
        capture_output=True, text=True, env=env,
    )
    try:
        d = json.loads(out.stdout)
        recs = d.get("result", {}).get("records", [])
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"WARN: could not parse org fields: {e}\n{out.stdout[:500]}\n{out.stderr[:500]}\n")
        recs = []
    return {r["QualifiedApiName"]: (r.get("Label") or "", r.get("DataType") or "") for r in recs}


def generated_fields(fields_dir: str):
    if not fields_dir or not os.path.isdir(fields_dir):
        return []
    suffix = ".field-meta.xml"
    return sorted(x[:-len(suffix)] for x in os.listdir(fields_dir) if x.endswith(suffix))


# ---- styling ----
HFILL = PatternFill("solid", fgColor="1F3864"); HFONT = Font(bold=True, color="FFFFFF", size=11)
SFILL = PatternFill("solid", fgColor="2E75B6"); SFONT = Font(bold=True, color="FFFFFF", size=12)
TITLE = Font(bold=True, size=15, color="1F3864")
CEN = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
_thin = Side(style="thin", color="D0D0D0")
BD = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def write_object_tab(ws, ctx):
    r = [1]

    def sect(title):
        ws.cell(r[0], 1, title).font = SFONT
        for j in range(1, NCOLS + 1):
            ws.cell(r[0], j).fill = SFILL
        r[0] += 1

    def head(cols):
        for j, c in enumerate(cols, 1):
            cell = ws.cell(r[0], j, c)
            cell.fill = HFILL; cell.font = HFONT; cell.alignment = CEN; cell.border = BD
        r[0] += 1

    def drow(vals, bold=False):
        for j, v in enumerate(vals, 1):
            cell = ws.cell(r[0], j, v); cell.border = BD; cell.alignment = LEFT
            if bold:
                cell.font = Font(bold=True)
        r[0] += 1

    def blank():
        r[0] += 1

    total = ctx["total"]; wip = ctx["wip"]; standard = ctx["standard"]
    untyped = ctx["untyped"]; custom = ctx["custom"]; gen = ctx["gen"]
    org_ti = ctx["org_ti"]; org_map = ctx["org_map"]
    miss = ctx["in_gen_not_org"]; extra = ctx["in_org_not_gen"]

    ws.cell(r[0], 1, f"{ctx['object_api']}  —  Sheet vs {ctx['target_org']}").font = TITLE; r[0] += 1
    ws.cell(r[0], 1, f"Source tab: {ctx['sheet_tab']} · Deploy ID {ctx['deploy_id']} · Generated {ctx['now']}").font = Font(italic=True, color="555555"); r[0] += 1
    blank()

    sect("1 · Summary")
    head(["Metric", "Count", "", ""])
    for k, v, b in [
        ("Field rows provided in sheet", total, 0),
        ("— WIP, excluded (col AB = X)", len(wip), 0),
        ("— Standard / non-custom (e.g. Name)", len(standard), 0),
        ("— Untyped / 要確認 (draft, blocked)", len(untyped), 0),
        ("— Custom fields to deploy", len(custom), 1),
        ("Fields generated (deploy set)", len(gen), 0),
        ("Custom fields now in org (TI_Fnt_)", len(org_ti), 1),
        ("Total fields on object in org (std+custom)", len(org_map), 0),
    ]:
        drow([k, v, "", ""], bold=b)
    blank()

    sect("2 · Reconciliation (generated vs org)")
    head(["Check", "Result", "", ""])
    for k, v in [
        ("Every generated field present in org?", "YES" if not miss else f"NO — {len(miss)} missing"),
        ("Generated count == org TI_Fnt_ count", "YES" if len(gen) == len(org_ti) else "NO"),
        ("Extra TI_Fnt_ in org (not from this deploy)", len(extra)),
        ("Add-up (WIP+Std+Untyped+Custom)", f"{len(wip)+len(standard)+len(untyped)+len(custom)} == {total}"),
    ]:
        drow([k, v, "", ""])
    blank()

    sect(f"3 · NOT deployed — by reason ({len(wip)+len(standard)+len(untyped)})")
    head(["Reason", "No.", "Label", "API / Type"])
    for x in wip:
        drow(["WIP (col AB=X)", x["no"] or "", x["label"], x["full"] or "(no api)"])
    for x in standard:
        drow(["Standard / non-custom", x["no"] or "", x["label"], x["full"] or "(blank)"])
    for x in untyped:
        drow(["Untyped / 要確認", x["no"] or "", x["label"], x["type"] or "(no type)"])
    blank()

    sect(f"4 · Deployed custom fields ({len(org_ti)})")
    head(["#", "API Name", "Label", "Type"])
    for i, n in enumerate(org_ti, 1):
        lbl, dt = org_map[n]
        drow([i, n, lbl, dt])

    # ---- Section 5: audit of Google-Sheet cell writes made during prep ----
    edits = ctx.get("edits") or []
    if edits:
        blank()
        sect(f"5 · Google Sheet edits — audit ({len(edits)})")
        cols6 = ["Cell", "Column", "Field / Label", "Old value", "New value", "Change set"]
        for j, c in enumerate(cols6, 1):
            cell = ws.cell(r[0], j, c)
            cell.fill = HFILL; cell.font = HFONT; cell.alignment = CEN; cell.border = BD
        r[0] += 1
        for e in edits:
            vals = [e.get("cell", ""), e.get("col", ""), e.get("field", ""),
                    e.get("old", ""), e.get("new", ""), e.get("set", "")]
            for j, v in enumerate(vals, 1):
                cell = ws.cell(r[0], j, v); cell.border = BD; cell.alignment = LEFT
            r[0] += 1

    ncols = 6 if edits else 4
    default_w = [30, 44, 40, 26, 26, 26]
    for i in range(1, ncols + 1):
        ws.column_dimensions[get_column_letter(i)].width = default_w[i - 1]
    ws.sheet_view.showGridLines = False


def main():
    ap = argparse.ArgumentParser(description="Append/refresh a per-object tab in the deployment report workbook.")
    ap.add_argument("--sheet-tab", required=True, help="Object tab name in the Google Sheet, e.g. 成約:Sales_Deal")
    ap.add_argument("--object-api", required=True, help="Salesforce object API name, e.g. Sales_Deal__c")
    ap.add_argument("--target-org", required=True, help="Target org alias/username, e.g. ERPDEV01")
    ap.add_argument("--deploy-id", default="-", help="Deploy ID to record in the tab header")
    ap.add_argument("--fields-dir", default="", help="Generated fields dir (force-app .../fields) for the deploy set count")
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--workbook", default=DEFAULT_WORKBOOK)
    ap.add_argument("--tab-name", default="", help="Override worksheet tab name (defaults to object short name)")
    ap.add_argument("--sf-home", default="", help="Workspace-local HOME shim for the sf CLI (applied to the org-query subprocess only)")
    ap.add_argument("--xdg-data-home", default="", help="XDG_DATA_HOME for the sf subprocess (defaults to ~/.local/share)")
    ap.add_argument("--edits", default="", help="Sheet-edit audit JSON to render as a section (default: auto .build/sheet_edits_<Object>.json)")
    args = ap.parse_args()

    f = load_fetch_module()
    rows = read_sheet_rows(f, args.sheet_id, args.sheet_tab)
    wip, standard, untyped, custom = categorise(rows)
    gen = generated_fields(args.fields_dir)
    genset = set(gen)
    org_map = query_org_fields(args.object_api, args.target_org, args.sf_home, args.xdg_data_home)
    # Count ALL custom fields (__c) on the object, not just the TI_Fnt_ prefix —
    # some objects carry legacy-named custom fields (QuotationAndWork__c, …) that a
    # prefix filter would wrongly report as "not deployed". Naming-agnostic.
    org_ti = sorted(n for n in org_map if n.endswith("__c"))
    orgset = set(org_ti)

    ctx = dict(
        object_api=args.object_api, target_org=args.target_org, sheet_tab=args.sheet_tab,
        deploy_id=args.deploy_id, now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        total=len(rows), wip=wip, standard=standard, untyped=untyped, custom=custom,
        gen=gen, org_ti=org_ti, org_map=org_map,
        in_gen_not_org=sorted(genset - orgset), in_org_not_gen=sorted(orgset - genset),
    )

    tab_name = (args.tab_name or args.object_api.replace("__c", ""))[:31]

    # Load the sheet-edit audit for this object (explicit path, else auto-discover).
    edits_path = args.edits or os.path.join(ROOT, ".build", f"sheet_edits_{tab_name}.json")
    if os.path.exists(edits_path):
        try:
            ctx["edits"] = json.load(open(edits_path, encoding="utf-8")).get("edits", [])
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"WARN: could not read edits {edits_path}: {e}\n")
            ctx["edits"] = []
    else:
        ctx["edits"] = []
    os.makedirs(os.path.dirname(args.workbook), exist_ok=True)
    if os.path.exists(args.workbook):
        wb = load_workbook(args.workbook)
        if tab_name in wb.sheetnames:
            del wb[tab_name]  # refresh existing object tab
        ws = wb.create_sheet(tab_name)
        # drop the default empty sheet if it lingers
        if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 and wb["Sheet"].max_column == 1:
            del wb["Sheet"]
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = tab_name

    write_object_tab(ws, ctx)
    wb.save(args.workbook)

    print(f"Wrote tab '{tab_name}' → {args.workbook}")
    print(f"  provided={len(rows)} wip={len(wip)} standard={len(standard)} untyped={len(untyped)} custom={len(custom)}")
    print(f"  generated={len(gen)} org_TI_Fnt_={len(org_ti)} gen==org={genset==orgset} "
          f"missing={ctx['in_gen_not_org']} extra={ctx['in_org_not_gen']}")
    print(f"  tabs now: {wb.sheetnames}")


if __name__ == "__main__":
    main()
