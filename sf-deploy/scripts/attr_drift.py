#!/usr/bin/env python3
"""
attr_drift.py — GRANULAR, attribute-level drift check between the sheet's
intended field DEFINITIONS and the org's ACTUAL field metadata, for any object.

Why this exists: the delta step (prep_deploy/fetch_sheet) compares field API
*names* only. A field that already exists in the org by name is treated as
"already deployed" and skipped — even if the sheet later changed its TYPE,
FORMULA, referenceTo, or PICKLIST values. This tool closes that blind spot by
comparing the actual definitions for fields present in BOTH sheet and org.

In the pipeline this file is also used as a LIBRARY: `prep_deploy.py` already
has every object's CustomObject metadata in the bulk org snapshot, so
`plan_deploy.py` calls `parse_org_object()` + `compute_drift()` locally — no
per-object authentication and no per-object Metadata API call. Each drift entry
carries a `component` ("CustomField", or "CustomObject" for the standard Name
field), because Obj__c.Name is not a deployable CustomField member.

The standalone CLI below still does its own live `readMetadata(CustomObject)`
with a session from the supported CLI (`sf org display`), for ad-hoc checks.

Usage (standalone, live fetch):
  python scripts/attr_drift.py --object TI_Fnt_Deal__c --tab Deal \
      --spreadsheet-id <ID> --org ERPDEV01 --out .build/deal_attr_drift.json

Usage (inside the pipeline, reuse already-fetched rows):
  python scripts/attr_drift.py --object TI_Fnt_Deal__c --rows temp_updates.json \
      --org ERPDEV01 --out .build/attr_drift_<obj>.json

Exit code: 0 = no drift (or report-only). With --fail-on-drift, exits 2 when any
field differs (useful as a gate). Missing org object -> exit 0 with a note (a NEW
object has nothing to drift against).
"""
from __future__ import annotations
import argparse, json, os, pathlib, re, subprocess, sys, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from translation_lib import (  # noqa: E402
    MetadataApiError, OrgAuthError, org_auth, parse_soap,
)

# sheet "Data Type" -> (expected org base type, is_formula).
# Keys are matched case-insensitively and after light normalization (see norm_dt).
_RAW_SHEET_MAP = {
    # plain
    "text": ("Text", False),
    "textarea": ("TextArea", False), "text area": ("TextArea", False),
    "longtextarea": ("LongTextArea", False), "long text area": ("LongTextArea", False),
    "richtextarea": ("Html", False), "rich text area": ("Html", False), "html": ("Html", False),
    "encryptedtext": ("EncryptedText", False), "encrypted text": ("EncryptedText", False),
    "lookup": ("Lookup", False),
    "masterdetail": ("MasterDetail", False), "master-detail": ("MasterDetail", False),
    "picklist": ("Picklist", False),
    "multiselectpicklist": ("MultiselectPicklist", False),
    "picklist (multi-select)": ("MultiselectPicklist", False),
    "multi-select picklist": ("MultiselectPicklist", False),
    "checkbox": ("Checkbox", False), "boolean": ("Checkbox", False), "bool": ("Checkbox", False),
    "flag": ("Checkbox", False),
    "date": ("Date", False), "datetime": ("DateTime", False), "date/time": ("DateTime", False),
    "time": ("Time", False),
    "number": ("Number", False), "currency": ("Currency", False), "percent": ("Percent", False),
    "email": ("Email", False), "phone": ("Phone", False), "url": ("Url", False),
    "autonumber": ("AutoNumber", False), "auto number": ("AutoNumber", False),
    "geolocation": ("Location", False), "location": ("Location", False),
    "rollupsummary": ("Summary", False), "roll-up summary": ("Summary", False),
    "rollup summary": ("Summary", False), "summary": ("Summary", False),
    # formulas (return type + is_formula flag)
    "formula text": ("Text", True), "formula number": ("Number", True),
    "formula date": ("Date", True), "formula datetime": ("DateTime", True),
    "formula currency": ("Currency", True), "formula checkbox": ("Checkbox", True),
    "formula percent": ("Percent", True), "formula time": ("Time", True),
    "formula (text)": ("Text", True), "formula (number)": ("Number", True),
    "formula (date)": ("Date", True), "formula (checkbox)": ("Checkbox", True),
}


def norm_dt(dt: str) -> str:
    return re.sub(r"\s+", " ", (dt or "").strip().lower())


def map_dt(dt: str):
    """Return (base_type, is_formula, mapped?) for a sheet Data Type string."""
    k = norm_dt(dt)
    if k in _RAW_SHEET_MAP:
        b, f = _RAW_SHEET_MAP[k]
        return b, f, True
    return dt.strip(), False, False  # unmapped: literal compare + flagged


def load_token(org: str | None, token_file: str) -> dict:
    """Session for `org` from the supported CLI (`sf org display`).

    No keychain decryption and no assumption about where the CLI stores its
    auth, so this behaves the same on a laptop, a build agent or a container.
    """
    if org:
        return org_auth(org)
    return json.load(open(token_file))["result"]


def parse_org_object(rec: ET.Element) -> dict | None:
    """CustomObject <records> element -> {fieldFullName: {type, formula, ...}}.

    Pure: no network. The caller supplies the element, so the same comparison
    runs against a live read OR against the bulk org snapshot.
    """
    if rec is None or rec.find("fullName") is None:
        return None  # object absent in org
    org = {}
    for f in rec.iter("fields"):
        d = {c.tag: (c.text or "") for c in f}
        vals = [v.findtext("fullName", "") for v in f.iter("value")]
        if vals:
            d["_picklist"] = [x for x in vals if x]
        org[d.get("fullName", "")] = d
    # capture the standard Name field (nameField block on the CustomObject) so the
    # drift check can compare it too — it is NOT under <fields> and would otherwise
    # be invisible (Text vs AutoNumber + displayFormat drift silently missed).
    # object-level attributes a FIELD can depend on: a field with
    # trackHistory=true needs enableHistory on the object, a Master-Detail
    # field forces sharingModel=ControlledByParent.
    org["__object__"] = {
        "enableHistory": rec.findtext("enableHistory", "") or "",
        "sharingModel": rec.findtext("sharingModel", "") or "",
    }
    nf = rec.find("nameField")
    if nf is not None:
        org["__nameField__"] = {
            "type": nf.findtext("type", "") or "",
            "displayFormat": nf.findtext("displayFormat", "") or "",
            "label": nf.findtext("label", "") or "",
        }
    return org


def read_org_object(api: str, tok: str, inst: str, ver: str) -> dict | None:
    """Live readMetadata(CustomObject) for ONE object (standalone CLI path).

    The pipeline does not use this: `org_snapshot.py` reads every target
    object's CustomObject in one bulk pass and the planner compares locally.
    """
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
        xml = urllib.request.urlopen(req).read().decode()
    except urllib.error.HTTPError as e:
        raise MetadataApiError(f"readMetadata HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise MetadataApiError(f"readMetadata: cannot reach {inst} ({e.reason})")
    root = parse_soap(xml)
    return parse_org_object(root.find(".//records"))


def sheet_rows_for(object_api: str, rows: list[dict]) -> list[dict]:
    """Custom-field rows of `rows` that belong to `object_api`."""
    obj_norm = object_api.replace("__c", "")
    out = []
    for r in rows:
        api = (r.get("Field API Name") or "").strip()
        if not api.endswith("__c"):
            continue
        own = str(r.get("Object API Name") or "").replace("__c", "")
        if own and own != obj_norm:
            continue
        out.append(r)
    return out


def object_meta_row(object_api: str, all_rows: list[dict]) -> dict | None:
    """The object-meta / Name-field row that belongs to `object_api`.

    Multi-object batches put every object's header in `all_rows`. Picking the
    first Name-type row in the whole list attributes object A's Name definition
    to object B.
    """
    obj_norm = (object_api or "").replace("__c", "")
    for r in all_rows:
        own = str(r.get("Object API Name") or "").replace("__c", "")
        if own != obj_norm:
            continue
        if (r.get("Name Field Type") or r.get("Name Field Display Format")
                or str(r.get("_type") or "").lower() in ("object_meta", "object")):
            return r
    return None


def compute_drift(object_api: str, all_rows: list[dict], org: dict
                  ) -> tuple[list[dict], int, list[str]]:
    """Compare the sheet's field DEFINITIONS against the org's metadata.

    Pure: `org` is a parsed CustomObject (see parse_org_object), so this runs
    identically against a live read or the bulk snapshot. Returns
    (drift, in_sync_count, warnings). Every entry carries a `component`
    ("CustomField" or "CustomObject") because the standard Name field lives on
    the CustomObject, not on a CustomField — packaging `Obj__c.Name` as a
    CustomField member is invalid metadata.
    """
    sheet_rows = sheet_rows_for(object_api, all_rows)
    warnings: list[str] = []
    # helpers for secondary-attribute compare
    def _num(v):
        s = str(v or "").strip()
        if not s:
            return None
        try:
            return int(float(s))
        except Exception:
            return s
    def _sbool(v):  # sheet truthiness (TRUE/x/yes/1)
        return str(v or "").strip().lower() in ("true", "x", "yes", "1")
    def _obool(v):  # org metadata boolean
        return str(v or "").strip().lower() == "true"

    # 3) compare
    drift, insync = [], 0
    for r in sheet_rows:
        api = r["Field API Name"].strip()
        dt = (r.get("Data Type") or "").strip()
        tsv = (r.get("Type Specific Value") or "").strip()
        om = org.get(api)
        if om is None:
            drift.append({"field": api, "sheet": dt, "org": "(absent)",
                          "reason": "field not in org (not yet deployed)"})
            continue
        exp_base, exp_formula, mapped = map_dt(dt)
        ot = om.get("type", "")
        ohf = bool(om.get("formula"))
        oref = om.get("referenceTo", "")
        why = []
        if not mapped:
            why.append(f"unmapped sheet Data Type '{dt}' — verify manually (org type={ot})")
        # --- primary: type / formula / referenceTo / picklist ---
        if ot != exp_base:
            why.append(f"type: sheet={exp_base} org={ot}")
        if exp_formula and not ohf:
            why.append("sheet=Formula, org=NOT formula")
        if not exp_formula and ohf:
            why.append("sheet=non-formula, org=HAS formula")
        # formula BODY compare (both sides are formulas): the original deploy may
        # have shipped a DUMMY (e.g. "TBD"/0/false) when the real formula was in a
        # column we weren't reading. Presence-only checks miss that, so compare the
        # normalized bodies. Sheet formula body is Type Specific Value (col H).
        if exp_formula and ohf and tsv:
            def _nf(x):  # case-insensitive, whitespace-insensitive
                return re.sub(r"\s+", "", str(x or "")).lower()
            if _nf(tsv) != _nf(om.get("formula")):
                _of = (om.get("formula") or "").strip()
                why.append(f"formula body differs (org={_of[:30]!r})")
        if exp_base in ("Lookup", "MasterDetail") and tsv and oref and oref != tsv:
            why.append(f"referenceTo: sheet={tsv} org={oref}")
        if exp_base in ("Picklist", "MultiselectPicklist") and om.get("_picklist"):
            # Normalize full-width JP punctuation (；->; ：->:) exactly like
            # generate_xml before splitting entries on ';'/newline, then take the
            # API side (after the label:api colon) — the org fullName IS the api
            # side, so comparing the label kept it perpetually "drifted".
            _tsv = tsv.translate({0xFF1B: ord(";"), 0xFF1A: ord(":")})
            sv = [p.split(":")[-1].strip() for p in re.split(r"[;\n]", _tsv) if p.strip()]
            if sv and set(sv) != set(om["_picklist"]):
                why.append(f"picklist values differ ({len(sv)} sheet / {len(om['_picklist'])} org)")
        # --- secondary attributes (only when the sheet specifies a value) ---
        if exp_base in ("Text", "TextArea", "LongTextArea", "EncryptedText", "Html") and not exp_formula:
            sl, ol = _num(r.get("Length")), _num(om.get("length"))
            if sl is not None and ol is not None and sl != ol:
                why.append(f"length: sheet={sl} org={ol}")
            svl, ovl = _num(r.get("Visible Lines")), _num(om.get("visibleLines"))
            if svl is not None and ovl is not None and svl != ovl:
                why.append(f"visibleLines: sheet={svl} org={ovl}")
        if exp_base in ("Number", "Currency", "Percent") and not exp_formula:
            sp, op = _num(r.get("Precision")), _num(om.get("precision"))
            ssc, osc = _num(r.get("Scale")), _num(om.get("scale"))
            if sp is not None and op is not None and sp != op:
                why.append(f"precision: sheet={sp} org={op}")
            if ssc is not None and osc is not None and ssc != osc:
                why.append(f"scale: sheet={ssc} org={osc}")
        if exp_base not in ("MasterDetail", "Summary", "AutoNumber") and not exp_formula:
            sreq = str(r.get("Required") or "").strip()
            if sreq and _sbool(sreq) != _obool(om.get("required")):
                why.append(f"required: sheet={_sbool(sreq)} org={_obool(om.get('required'))}")
        for scol, ocol in (("Unique", "unique"), ("External ID", "externalId")):
            sv = str(r.get(scol) or "").strip()
            if sv and _sbool(sv) != _obool(om.get(ocol)):
                why.append(f"{ocol}: sheet={_sbool(sv)} org={_obool(om.get(ocol))}")
        if why:
            od = ot + (" +formula" if ohf else "") + (f" ->{oref}" if oref else "")
            drift.append({"field": api, "sheet": dt, "org": od, "reason": "; ".join(why)})
        else:
            insync += 1

    # --- MANDATORY: standard Name field drift (per sf-drift-includes-standard-fields) ---
    # The Name field lives in the CustomObject <nameField> block, NOT under <fields>,
    # and is not a __c custom field, so the custom-field loop above never sees it.
    # This is exactly how Receiving/ReceivingDetail shipped Name=Text while the sheet
    # declared AutoNumber. Compare it explicitly, every run.
    meta = object_meta_row(object_api, all_rows)
    onf = org.get("__nameField__")
    if meta is not None and onf is not None:
        s_nt_raw = (meta.get("Name Field Type") or "").strip()
        s_df = (meta.get("Name Field Display Format") or "").strip()
        exp_nt, _isf, _m = map_dt(s_nt_raw)          # e.g. "Autonumber"->"AutoNumber","Text"->"Text"
        o_nt = (onf.get("type") or "").strip()
        o_df = (onf.get("displayFormat") or "").strip()
        why = []
        if s_nt_raw and exp_nt and exp_nt.lower() != o_nt.lower():
            why.append(f"type: sheet={exp_nt} org={o_nt or '(none)'}")
        # displayFormat only matters for AutoNumber; compare when the sheet gives one
        if exp_nt.lower() == "autonumber" and s_df and s_df != o_df:
            why.append(f"displayFormat: sheet={s_df!r} org={o_df!r}")
        if why:
            drift.append({"field": "Name", "sheet": (s_nt_raw or "?") + (f" {s_df}" if s_df else ""),
                          "org": (o_nt or "?") + (f" {o_df}" if o_df else ""),
                          "reason": "STANDARD Name field; " + "; ".join(why)})
        else:
            insync += 1
    elif meta is None:
        print("⚠️  no object-meta row with 'Name Field Type' found — Name drift NOT checked "
              "(re-fetch with the object-meta row so the standard Name field is verified).")
    elif onf is None:
        warnings.append("org CustomObject returned no <nameField> — Name drift NOT checked")


    for d in drift:
        d.setdefault("component", "CustomObject" if d["field"] == "Name" else "CustomField")
    drift.sort(key=lambda d: d["field"])
    return drift, insync, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Attribute-level sheet<->org drift")
    ap.add_argument("--object", required=True, help="object API name, e.g. TI_Fnt_Deal__c")
    ap.add_argument("--rows", help="pre-fetched fetch_sheet JSON (skip live fetch)")
    ap.add_argument("--tab", help="sheet tab (for live fetch when --rows absent)")
    ap.add_argument("--spreadsheet-id")
    ap.add_argument("--org", help="alias/username to mint a live session token")
    ap.add_argument("--token-file", default=".build/orgauth.json")
    ap.add_argument("--out", default=".build/attr_drift.json")
    ap.add_argument("--fail-on-drift", action="store_true")
    args = ap.parse_args()

    # 1) sheet rows
    if args.rows:
        rows = json.loads(pathlib.Path(args.rows).read_text(encoding="utf-8"))
    else:
        if not (args.tab and args.spreadsheet_id):
            print("❌ need --rows OR (--tab and --spreadsheet-id)")
            return 2
        tmp = pathlib.Path(".build/_attr_rows.json")
        subprocess.run(["python3", str(pathlib.Path(__file__).with_name("fetch_sheet.py")),
                        "--spreadsheet-id", args.spreadsheet_id, "--tabs", args.tab,
                        "--out", str(tmp)], check=True)
        rows = json.loads(tmp.read_text(encoding="utf-8"))

    # 2) org metadata (standalone: one live read for this object)
    try:
        a = load_token(args.org, args.token_file)
        org = read_org_object(args.object, a["accessToken"],
                              a["instanceUrl"].rstrip("/"), a["apiVersion"])
    except (OrgAuthError, MetadataApiError) as e:
        print(f"❌ {e}")
        return 1
    if org is None:
        print(f"ℹ️  {args.object} not in org (NEW object) — no attribute drift to check.")
        pathlib.Path(args.out).write_text("[]", encoding="utf-8")
        return 0

    # 3) compare (same pure function the planner uses)
    drift, insync, warnings = compute_drift(args.object, rows, org)
    for w in warnings:
        print(f"⚠️  {w}")
    print(report(args.object, drift, insync))
    pathlib.Path(args.out).write_text(
        json.dumps(drift, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved -> {args.out}")
    if drift and args.fail_on_drift:
        return 2
    return 0


def report(object_api: str, drift: list[dict], insync: int) -> str:
    """Human-readable drift table (shared by the CLI and the planner)."""
    out = ["=" * 96,
           f"ATTRIBUTE DRIFT — {object_api}   (in-sync: {insync}   drifted: {len(drift)})",
           "=" * 96]
    if drift:
        out.append(f"{'FIELD':<40}{'COMPONENT':<16}{'SHEET':<14}{'ORG':<20}REASON")
        out.append("-" * 96)
        for d in drift:
            out.append(f"{d['field']:<40}{d.get('component', ''):<16}"
                       f"{d['sheet']:<14}{d['org']:<20}{d['reason']}")
    else:
        out.append("  ✅ every existing field matches the sheet definition.")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
