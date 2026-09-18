#!/usr/bin/env python3
"""
mdapi_deploy.py — Deploy a small metadata package via the SOAP Metadata API
directly over HTTPS, using a live access token (from `sf org display --verbose
--json`). This exists ONLY to work around sandboxed environments where the sf
CLI cannot rotate/persist its auth file (~/.sfdx write is blocked): here we
reuse the already-valid session token and never touch the auth file.

Usage:
  python scripts/mdapi_deploy.py \
      --auth-json .build/orgauth.json \
      --package manifest/tab_sqr.xml \
      --file tabs/TI_Fnt_ShippingAndQuoteAndWorkRelation__c.tab=force-app/main/default/tabs/TI_Fnt_ShippingAndQuoteAndWorkRelation__c.tab-meta.xml \
      [--file <arcname>=<localpath> ...] \
      [--test-level NoTestRun]

--file maps an ARCHIVE path inside the MDAPI zip (e.g. tabs/Foo.tab) to a LOCAL
source file (whose bytes are copied verbatim). Source-format "*-meta.xml"
content for these simple types is identical to MDAPI content, so we just rename.
"""
from __future__ import annotations
import argparse, base64, html, io, json, os, re, subprocess, sys, time, urllib.request, urllib.error, zipfile

NS = "http://soap.sforce.com/2006/04/metadata"
ENV = "http://schemas.xmlsoap.org/soap/envelope/"


def _customfield_objects(package_path: str) -> list[str]:
    """Return the distinct objects that have CustomField members in package.xml.

    A field member looks like ``Obj__c.Field__c`` inside the <types> block whose
    <name> is CustomField. Returns [] when the package deploys no fields (e.g. a
    FlexiPage / CustomTab / CustomObject-only package), so the drift gate becomes
    a harmless no-op for non-field deploys.
    """
    try:
        pkg = open(package_path, encoding="utf-8").read()
    except Exception:
        return []
    objs: list[str] = []
    for block in re.findall(r"<types>(.*?)</types>", pkg, re.S):
        if not re.search(r"<name>\s*CustomField\s*</name>", block):
            continue
        for mem in re.findall(r"<members>(.*?)</members>", block, re.S):
            mem = mem.strip()
            if "." in mem:
                obj = mem.split(".", 1)[0].strip()
                if obj and obj not in objs:
                    objs.append(obj)
    return objs


def _drift_gate(package_path: str, rows_path: str, auth_json: str,
                ack: bool, real_deploy: bool) -> bool:
    """MANDATORY attribute-drift gate for the SOAP deploy path.

    The name-based package only proves a field is being created/updated; it does
    NOT catch a field that already EXISTS in the org but whose DEFINITION (type /
    formula / referenceTo / picklist / precision …) diverges from the sheet. That
    check lived only in prep_deploy.py (the sf-CLI path); this makes it run on the
    CLI-free SOAP path too, so it can never be silently skipped again.

    Returns True to proceed, False to abort. Absent fields (genuinely new,
    reason='field not in org …') are ignored — only real definition drift counts.
    """
    objs = _customfield_objects(package_path)
    if not objs:
        return True  # no fields in this package -> nothing to drift-check
    if not os.path.exists(rows_path):
        print(f"⚠️  drift-gate: rows file '{rows_path}' not found — cannot verify "
              f"attribute drift. Re-fetch the sheet (fetch_sheet.py) or pass "
              f"--rows, or --no-drift-check to bypass intentionally.")
        return bool(ack)
    print(f"[drift-gate] attribute-drift check on {len(objs)} object(s) in package: {', '.join(objs)}")
    real_drift: dict[str, list] = {}
    for o in objs:
        out = f".build/_driftgate_{o}.json"
        # attr_drift with no --org reads the token file directly (CLI-free).
        subprocess.run(["python3", "scripts/attr_drift.py", "--object", o,
                        "--rows", rows_path, "--token-file", auth_json, "--out", out],
                       text=True)
        try:
            entries = json.loads(open(out).read())
        except Exception:
            entries = []
        drifted = [e for e in entries
                   if "not in org" not in (e.get("reason", "") or "")]
        if drifted:
            real_drift[o] = drifted
    if not real_drift:
        print("[drift-gate] ✅ no attribute drift on existing fields.")
        return True
    total = sum(len(v) for v in real_drift.values())
    print("=" * 88)
    print(f"[drift-gate] ⚠️  {total} EXISTING field(s) DRIFT from the sheet definition:")
    for o, lst in real_drift.items():
        print(f"  {o}  ({len(lst)}):")
        for e in lst:
            print(f"    - {e['field']:<40} {e['reason']}")
    print("=" * 88)
    if real_deploy and not ack:
        print("❌ ABORTED: real deploy blocked by attribute drift. Review each field —\n"
              "   many type/formula changes need delete+recreate (DATA LOSS), so this is a\n"
              "   deliberate decision. Re-run with --ack-drift once reviewed, or\n"
              "   --no-drift-check to bypass the gate entirely.")
        return False
    if ack:
        print("[drift-gate] proceeding: drift acknowledged via --ack-drift.")
    return True


def _soap(endpoint: str, token: str, body: str) -> str:
    envelope = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{ENV}" xmlns:met="{NS}">'
        f'<soapenv:Header><met:SessionHeader><met:sessionId>{html.escape(token)}'
        f'</met:sessionId></met:SessionHeader></soapenv:Header>'
        f'<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>'
    ).encode()
    req = urllib.request.Request(endpoint, data=envelope, headers={
        "Content-Type": "text/xml; charset=UTF-8",
        "SOAPAction": '""',
    })
    try:
        return urllib.request.urlopen(req, timeout=120).read().decode()
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        print(f"HTTP {e.code}\n{txt[:2000]}")
        raise


def _tag(xml: str, name: str) -> str | None:
    o, c = f"<{name}>", f"</{name}>"
    i = xml.find(o)
    if i < 0:
        return None
    j = xml.find(c, i)
    return xml[i + len(o):j]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-json", default=".build/orgauth.json")
    ap.add_argument("--package", required=True, help="package.xml manifest (MDAPI)")
    ap.add_argument("--file", action="append", default=[],
                    help="arcname=localpath (repeatable)")
    ap.add_argument("--test-level", default="NoTestRun")
    ap.add_argument("--check-only", action="store_true",
                    help="validation-only dry run (checkOnly=true; no write)")
    ap.add_argument("--purge-on-delete", action="store_true",
                    help="purgeOnDelete=true — permanently erase deleted components "
                         "(bypass recycle bin; sandbox only)")
    ap.add_argument("--rows", default="temp_updates.json",
                    help="fetch_sheet rows JSON used by the attribute-drift gate")
    ap.add_argument("--ack-drift", action="store_true",
                    help="acknowledge attribute drift on existing fields and proceed")
    ap.add_argument("--no-drift-check", action="store_true",
                    help="bypass the attribute-drift gate entirely (use only for "
                         "non-field deploys or when drift was already reviewed)")
    args = ap.parse_args()

    a = json.load(open(args.auth_json))["result"]
    token, inst = a["accessToken"], a["instanceUrl"].rstrip("/")
    ver = str(a.get("apiVersion", "60.0"))
    endpoint = f"{inst}/services/Soap/m/{ver}"

    # ---- MANDATORY attribute-drift gate (runs on this CLI-free SOAP path too) ----
    if not args.no_drift_check:
        if not _drift_gate(args.package, args.rows, args.auth_json,
                           ack=args.ack_drift, real_deploy=not args.check_only):
            return 2

    # ---- build the MDAPI zip in memory ----
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", open(args.package, "rb").read())
        for spec in args.file:
            arc, local = spec.split("=", 1)
            z.writestr(arc, open(local, "rb").read())
    zip_b64 = base64.b64encode(buf.getvalue()).decode()

    mode = "CHECK-ONLY (dry run)" if args.check_only else "REAL DEPLOY"
    print(f"→ deploy endpoint {endpoint}  [{mode}]")
    print(f"  zip members: package.xml + {len(args.file)} file(s)")
    body = (
        f'<met:deploy><met:ZipFile>{zip_b64}</met:ZipFile>'
        f'<met:DeployOptions>'
        f'<met:checkOnly>{"true" if args.check_only else "false"}</met:checkOnly>'
        f'<met:purgeOnDelete>{"true" if args.purge_on_delete else "false"}</met:purgeOnDelete>'
        f'<met:singlePackage>true</met:singlePackage>'
        f'<met:rollbackOnError>true</met:rollbackOnError>'
        f'<met:testLevel>{args.test_level}</met:testLevel>'
        f'</met:DeployOptions></met:deploy>'
    )
    resp = _soap(endpoint, token, body)
    async_id = _tag(resp, "id")
    if not async_id:
        print("❌ no async id in deploy response:\n", resp[:1500])
        return 1
    print(f"  async id {async_id} — polling…")

    # ---- poll ----
    for _ in range(120):
        time.sleep(3)
        st = _soap(endpoint, token,
                   f'<met:checkDeployStatus><met:asyncProcessId>{async_id}'
                   f'</met:asyncProcessId><met:includeDetails>true'
                   f'</met:includeDetails></met:checkDeployStatus>')
        done = (_tag(st, "done") or "false").strip()
        status = (_tag(st, "status") or "").strip()
        if done == "true":
            success = (_tag(st, "success") or "false").strip()
            n_ok = _tag(st, "numberComponentsDeployed")
            n_err = _tag(st, "numberComponentErrors")
            print(f"  status={status} success={success} deployed={n_ok} errors={n_err}")
            if success == "true":
                print("✅ MDAPI deploy SUCCEEDED")
                return 0
            # surface first problem
            prob = _tag(st, "problem")
            cn = _tag(st, "componentType") or _tag(st, "fullName")
            print(f"❌ MDAPI deploy FAILED: {cn}: {prob}")
            print(st[:2500])
            return 1
        print(f"  … {status}")
    print("❌ timed out waiting for deploy")
    return 1


if __name__ == "__main__":
    sys.exit(main())
