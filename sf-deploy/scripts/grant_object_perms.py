#!/usr/bin/env python3
"""Auto-grant object-level permissions (CRUD) on a permission set for custom
objects (GATE 1c of sf-post-deploy-fls-and-flexipage).

Field-Level Security (FieldPermissions, GATE 1) only controls which FIELDS a user
can see/edit; it does NOT grant access to the OBJECT itself. Without an
ObjectPermissions row on the permission set, a user cannot open/list/create the
record at all — the fields are irrelevant because the tab/record is inaccessible.
This helper closes that gap: it inserts any MISSING ObjectPermissions on the
target permission set (default SalesFrontAdmin) via the REST composite endpoint,
and verifies live.

Object selection:
  --objects A__c,B__c        explicit list, OR
  --all-tifnt                every deployed custom object whose API starts TI_Fnt_

Permission level (default full CRUD):
  Read + Create + Edit + Delete  (add --view-all / --modify-all for those bits)

Usage:
  python scripts/grant_object_perms.py --all-tifnt --org ERPDEV01 [--apply]
  python scripts/grant_object_perms.py --objects TI_Fnt_Foo__c --org ERPDEV01 --apply
"""
import argparse, json, os, sys, urllib.request, urllib.parse, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def api_call(inst, ver, tok, method, path, body=None):
    url = f"{inst}/services/data/v{ver}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"__error__": e.read().decode()}


def soql(inst, ver, tok, q):
    return api_call(inst, ver, tok, "GET", "query/?q=" + urllib.parse.quote(q))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", help="comma-separated object API names")
    ap.add_argument("--all-tifnt", action="store_true",
                    help="grant on every deployed custom object starting TI_Fnt_")
    ap.add_argument("--org", required=True)
    ap.add_argument("--permset", default="SalesFrontAdmin")
    ap.add_argument("--view-all", action="store_true")
    ap.add_argument("--modify-all", action="store_true")
    ap.add_argument("--auth", default=".build/orgauth.json")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    os.system(f"python3 scripts/get_token.py --alias {a.org} --out {a.auth} >/dev/null 2>&1")
    auth = json.load(open(os.path.join(ROOT, a.auth)))["result"]
    inst = auth["instanceUrl"].rstrip("/"); tok = auth["accessToken"]; ver = auth["apiVersion"]

    # resolve object set
    if a.all_tifnt:
        r = soql(inst, ver, tok,
                 "SELECT QualifiedApiName FROM EntityDefinition "
                 "WHERE QualifiedApiName LIKE 'TI_Fnt_%__c' ORDER BY QualifiedApiName")
        objects = [rec["QualifiedApiName"] for rec in r.get("records", [])]
    elif a.objects:
        objects = [o.strip() for o in a.objects.split(",") if o.strip()]
    else:
        print("❌ provide --objects or --all-tifnt"); return 1
    if not objects:
        print("❌ no objects resolved"); return 1

    # permission set id
    r = soql(inst, ver, tok, f"SELECT Id FROM PermissionSet WHERE Name='{a.permset}'")
    if not r.get("records"):
        print(f"❌ permission set {a.permset} not found"); return 1
    psid = r["records"][0]["Id"]

    # existing object perms on this permset
    inlist = ",".join(f"'{o}'" for o in objects)
    r = soql(inst, ver, tok,
             "SELECT SobjectType,PermissionsRead,PermissionsCreate,PermissionsEdit,"
             "PermissionsDelete,PermissionsViewAllRecords,PermissionsModifyAllRecords "
             f"FROM ObjectPermissions WHERE ParentId='{psid}' AND SobjectType IN ({inlist})")
    existing = {rec["SobjectType"]: rec for rec in r.get("records", [])}

    want = {"PermissionsRead": True, "PermissionsCreate": True,
            "PermissionsEdit": True, "PermissionsDelete": True,
            "PermissionsViewAllRecords": a.view_all,
            "PermissionsModifyAllRecords": a.modify_all}

    todo = [o for o in objects if o not in existing]
    print(f"permset={a.permset} ({psid})  objects={len(objects)}")
    print(f"  already have object perms={len(existing)}  to-insert={len(todo)}")
    for o in todo:
        print(f"    + {o}")
    if not todo:
        print("  nothing to insert (all present)."); return 0
    if not a.apply:
        print("DRY RUN — re-run with --apply to insert."); return 0

    records = [dict({"attributes": {"type": "ObjectPermissions"},
                     "ParentId": psid, "SobjectType": o}, **want) for o in todo]
    ok = err = 0
    for i in range(0, len(records), 200):
        batch = records[i:i + 200]
        res = api_call(inst, ver, tok, "POST", "composite/sobjects",
                       {"allOrNone": False, "records": batch})
        if isinstance(res, dict) and res.get("__error__"):
            print("  ❌ batch error:", res["__error__"][:400]); err += len(batch); continue
        for item in res:
            if item.get("success"):
                ok += 1
            else:
                err += 1
                print("  ❌", item.get("errors"))
    print(f"  inserted ok={ok}  errors={err}")

    # live verify
    r = soql(inst, ver, tok,
             "SELECT SobjectType,PermissionsRead,PermissionsCreate,PermissionsEdit,"
             f"PermissionsDelete FROM ObjectPermissions WHERE ParentId='{psid}' "
             f"AND SobjectType IN ({inlist})")
    now = {rec["SobjectType"]: rec for rec in r.get("records", [])}
    missing = [o for o in objects if o not in now]
    print(f"\n  VERIFY: {len(now)}/{len(objects)} objects now have object permissions")
    if missing:
        print("  ❌ still missing:", ", ".join(missing))
    return 0 if err == 0 and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
