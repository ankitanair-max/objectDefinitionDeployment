#!/usr/bin/env python3
"""Auto-grant Field-Level Security (FLS) on a permission set for an object's
custom fields (GATE 1 of sf-post-deploy-fls-and-flexipage).

LIVE-DRIVEN (not delta / not local files): for each object it reads the org's
FULL current custom-field set via the Metadata API `readMetadata(CustomObject)`
(FLS-independent — never `sobject describe`/`FieldDefinition`), diffs it against
the `FieldPermissions` already on the permission set, and inserts ONLY the
MISSING ones. This means it self-heals historical gaps: any field that never got
FLS at its original deploy (older batch, partial failure, required→optional flip)
is backfilled on the next run, regardless of what is in the local force-app dir.

Why live, not local: the previous version granted from
force-app/main/default/objects/<Obj>/fields/*.xml — i.e. only the fields
generated in the CURRENT deploy. Fields from earlier batches were invisible to
it, so their missing FLS was never fixed. Driving off the live field set removes
that blind spot entirely.

Classification (from live metadata, FLS-independent):
  * required=true                      -> SKIP (implicitly visible, cannot carry FLS)
  * MasterDetail                       -> SKIP (implicitly visible, cannot carry FLS)
  * AutoNumber / Summary / Formula     -> read-only  (Read=true, Edit=false)
  * everything else                    -> read+edit  (Read=true, Edit=true)

Idempotent + non-destructive: a field already present in FieldPermissions is left
UNTOUCHED — so manually-set read-only overrides are preserved.

Usage:
  python scripts/grant_fls.py --object TI_Fnt_Foo__c --org ERPDEV01 [--apply]
  python scripts/grant_fls.py --all-tifnt --org ERPDEV01 [--apply]
"""
import argparse, json, os, sys, re, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

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
    """Query with pagination (REST)."""
    out, url = [], f"{inst}/services/data/v{ver}/query/?q=" + urllib.parse.quote(q)
    while url:
        d = json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"}), timeout=90))
        out += d.get("records", [])
        url = inst + d["nextRecordsUrl"] if not d.get("done") else None
    return out


def read_org_fields(api, tok, inst, ver):
    """readMetadata(CustomObject) -> {fieldApiName: {type, required, has_formula}}.
    FLS-independent live read of the object's CUSTOM fields."""
    soap = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:met="http://soap.sforce.com/2006/04/metadata"><soapenv:Header>'
        f'<met:SessionHeader><met:sessionId>{tok}</met:sessionId></met:SessionHeader>'
        '</soapenv:Header><soapenv:Body><met:readMetadata><met:type>CustomObject</met:type>'
        f'<met:fullNames>{api}</met:fullNames></met:readMetadata></soapenv:Body></soapenv:Envelope>'
    )
    req = urllib.request.Request(f"{inst}/services/Soap/m/{ver}", data=soap.encode(),
                                 headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})
    try:
        xml = urllib.request.urlopen(req, timeout=120).read().decode()
    except urllib.error.HTTPError as e:
        xml = e.read().decode()
        sys.exit(f"❌ readMetadata HTTP {e.code}: {xml[:300]}")
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', '', xml)
    xml = re.sub(r'<(/?)\w+:', r'<\1', xml)
    xml = re.sub(r'\s\w+:(\w+=)', r' \1', xml)
    root = ET.fromstring(xml)
    rec = root.find(".//records")
    if rec is None or rec.find("fullName") is None:
        return None  # object absent
    fields = {}
    for f in root.iter("fields"):
        d = {c.tag: (c.text or "") for c in f}
        name = d.get("fullName", "")
        if not name:
            continue
        fields[name] = {
            "type": d.get("type", ""),
            "required": d.get("required", "").lower() == "true",
            "has_formula": f.find("formula") is not None,
        }
    return fields


def classify(meta):
    """Return 'skip' | 'readonly' | 'edit'."""
    if meta["required"] or meta["type"] == "MasterDetail":
        return "skip"
    if meta["type"] in ("AutoNumber", "Summary") or meta["has_formula"]:
        return "readonly"
    return "edit"


def grant_object(obj, inst, ver, tok, psid, apply):
    """Grant missing FLS for one object. Returns (inserted, errors, still_missing)."""
    fields = read_org_fields(obj, tok, inst, ver)
    if fields is None:
        print(f"  {obj:<44} ⚠ object absent in org — skipped")
        return 0, 0, 0
    edit, ro, skip = [], [], []
    for name, meta in fields.items():
        k = classify(meta)
        (edit if k == "edit" else ro if k == "readonly" else skip).append(name)

    existing = {r["Field"] for r in soql(
        inst, ver, tok,
        f"SELECT Field FROM FieldPermissions WHERE SobjectType='{obj}' AND ParentId='{psid}'")}

    want = {f"{obj}.{a}": (True, True) for a in edit}
    want.update({f"{obj}.{a}": (True, False) for a in ro})
    todo = {k: v for k, v in want.items() if k not in existing}

    print(f"  {obj:<44} live={len(fields):>4} edit={len(edit):>4} ro={len(ro):>3} "
          f"skip={len(skip):>3} granted={len(existing):>4} missing={len(todo):>4}")

    if not todo or not apply:
        return 0, 0, len(todo)

    records = [{"attributes": {"type": "FieldPermissions"}, "ParentId": psid,
                "SobjectType": obj, "Field": k,
                "PermissionsRead": rd, "PermissionsEdit": ed}
               for k, (rd, ed) in todo.items()]
    ok = err = 0
    for i in range(0, len(records), 200):
        batch = records[i:i + 200]
        res = api_call(inst, ver, tok, "POST", "composite/sobjects",
                       {"allOrNone": False, "records": batch})
        if isinstance(res, dict) and res.get("__error__"):
            print("    ❌ batch error:", res["__error__"][:300]); err += len(batch); continue
        for item in res:
            if item.get("success"):
                ok += 1
            else:
                err += 1
                print("    ❌", item.get("errors"))
    # live re-verify
    now = {r["Field"] for r in soql(
        inst, ver, tok,
        f"SELECT Field FROM FieldPermissions WHERE SobjectType='{obj}' AND ParentId='{psid}'")}
    still = [k for k in want if k not in now]
    print(f"      inserted ok={ok} errors={err}  still-missing={len(still)}")
    return ok, err, len(still)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--object", help="single object API name")
    ap.add_argument("--all-tifnt", action="store_true",
                    help="grant on every deployed custom object starting TI_Fnt_")
    ap.add_argument("--org", required=True)
    ap.add_argument("--permset", default="SalesFrontAdmin")
    ap.add_argument("--auth", default=".build/orgauth.json")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    os.system(f"python3 scripts/get_token.py --alias {a.org} --out {a.auth} >/dev/null 2>&1")
    auth = json.load(open(os.path.join(ROOT, a.auth)))["result"]
    inst = auth["instanceUrl"].rstrip("/"); tok = auth["accessToken"]; ver = auth["apiVersion"]

    if a.all_tifnt:
        objects = [r["QualifiedApiName"] for r in soql(
            inst, ver, tok,
            "SELECT QualifiedApiName FROM EntityDefinition "
            "WHERE QualifiedApiName LIKE 'TI_Fnt_%__c' ORDER BY QualifiedApiName")]
    elif a.object:
        objects = [a.object]
    else:
        print("❌ provide --object or --all-tifnt"); return 1

    r = soql(inst, ver, tok, f"SELECT Id FROM PermissionSet WHERE Name='{a.permset}'")
    if not r:
        print(f"❌ permission set {a.permset} not found"); return 1
    psid = r[0]["Id"]

    print(f"permset={a.permset} ({psid})  objects={len(objects)}  "
          f"mode={'APPLY' if a.apply else 'DRY-RUN'}")
    tot_ok = tot_err = tot_missing = 0
    for o in objects:
        ok, err, missing = grant_object(o, inst, ver, tok, psid, a.apply)
        tot_ok += ok; tot_err += err; tot_missing += missing
    if a.apply:
        print(f"\nTOTAL inserted={tot_ok}  errors={tot_err}  still-missing={tot_missing}")
        return 0 if tot_err == 0 and tot_missing == 0 else 1
    print(f"\nTOTAL missing (to grant)={tot_missing}   DRY RUN — re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
