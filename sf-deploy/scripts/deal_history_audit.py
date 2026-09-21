#!/usr/bin/env python3
"""Audit Deal-tab revision changes (net oldest->newest) and confirm org parity."""
import sys, os, json, base64, io, urllib.request
sys.path.insert(0, "scripts")
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import google.auth
import openpyxl

SID = "1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
scopes = ["https://www.googleapis.com/auth/drive.readonly"]

sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
if sa:
    info = json.loads(sa) if sa.startswith("{") else (json.load(open(sa)) if (sa.startswith("/") or sa.lower().endswith(".json")) else json.loads(base64.b64decode(sa)))
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
else:
    creds, _ = google.auth.default(scopes=scopes)
creds.refresh(Request())
drive = build("drive", "v3", credentials=creds, cache_discovery=False)

OLD, NEW = sys.argv[1], sys.argv[2]

def fetch_deal_grid(rev):
    r = drive.revisions().get(fileId=SID, revisionId=rev, fields="exportLinks").execute()
    url = r["exportLinks"][XLSX]
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {creds.token}"})
    data = urllib.request.urlopen(req).read()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    if "Deal" not in wb.sheetnames:
        print(f"  rev {rev}: no 'Deal' tab (tabs sample: {wb.sheetnames[:5]}...)")
        return None
    ws = wb["Deal"]
    grid = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value not in (None, ""):
                grid[(c.row, c.column)] = str(c.value)
    return grid

def colletter(i):
    s = ""; n = i - 1
    while True:
        s = chr(65 + n % 26) + s; n = n // 26 - 1
        if n < 0: break
    return s

print(f"Downloading Deal tab from rev {OLD} (old) and {NEW} (new)...")
g_old = fetch_deal_grid(OLD)
g_new = fetch_deal_grid(NEW)
if not g_old or not g_new:
    sys.exit("could not load both revisions")

# find header row + fullName col in NEW to label changes by field
def header_and_full(grid):
    # header row = the one containing 'fullName'
    maxr = max(r for r, _ in grid)
    for r in range(1, min(maxr, 40)):
        rowvals = {c: grid.get((r, c), "") for c in range(1, 35)}
        if any(v.strip().lower() == "fullname" for v in rowvals.values()):
            fullc = [c for c, v in rowvals.items() if v.strip().lower() == "fullname"][0]
            labelc = [c for c, v in rowvals.items() if v.strip().lower() == "label"]
            return r, fullc, (labelc[0] if labelc else None)
    return None, None, None

hr, fullc, labelc = header_and_full(g_new)
def field_at_row(grid, r):
    return grid.get((r, fullc), "").strip() if fullc else ""

# Diff columns A..AE (1..31) only — skip AH+ (33+) discussion/AI-comment noise
COLS = range(1, 32)
allrows = sorted({r for (r, c) in set(g_old) | set(g_new) if c in COLS})
changes = []
for r in allrows:
    if hr and r <= hr:  # skip header/meta rows
        continue
    for c in COLS:
        ov = g_old.get((r, c), "")
        nv = g_new.get((r, c), "")
        if ov != nv:
            fld = field_at_row(g_new, r) or field_at_row(g_old, r)
            changes.append((r, colletter(c), fld, ov, nv))

print("\n" + "=" * 78)
print(f"DEAL-TAB CHANGES  rev {OLD} -> rev {NEW}  (field-def cols A–AE only)")
print("=" * 78)
if not changes:
    print("  (no field-definition changes in this window)")
# group by field
from collections import defaultdict
byf = defaultdict(list)
for r, col, fld, ov, nv in changes:
    byf[(r, fld)].append((col, ov, nv))
for (r, fld), lst in sorted(byf.items()):
    print(f"\n  row {r}  [{fld or '(no api)'}]")
    for col, ov, nv in lst:
        print(f"      {col}{r}: {ov!r}  ->  {nv!r}")
print(f"\n  total changed cells: {len(changes)}  across {len(byf)} row(s)")
