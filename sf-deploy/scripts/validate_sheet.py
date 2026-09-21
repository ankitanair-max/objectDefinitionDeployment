#!/usr/bin/env python3
"""
validate_sheet.py — Pre-deployment validation for Data Dictionary rows.

Reads `temp_updates.json` (produced by fetch_sheet.py) and validates every
field row against Salesforce metadata rules BEFORE any XML/package is built.
Emits a detailed execution log (PASS / FAIL per check + diagnostics) and a
machine-readable report. Exit code 0 = all clear, 1 = blocking ERROR(s).

Rule sources (reconciled + refined — see docs/FIELD_TYPE_VALIDATION_RULES.md,
DATADICTIONARY_VALIDATION.md, GOOGLE_SHEET_STRUCTURE.md):
  - Semicolon `;` is the picklist value delimiter.
  - Checkbox: defaultValue (TRUE/FALSE) required.
  - MultiselectPicklist: visibleLines required.
  - Lookup: relationshipName optional; MasterDetail: relationshipName required.
  - Formula Number/Currency/Percent: precision + scale required (SF Metadata API).
  - unique/externalId only for Text, Email, Phone, Url, Number, Currency, Percent.

Usage:
  python scripts/validate_sheet.py [--in temp_updates.json] [--json report.json] [--strict]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

# --------------------------------------------------------------------------- #
# Rule table  (data_type -> required / optional / forbidden / constraints)
# Keys reference temp_updates.json field keys.
# --------------------------------------------------------------------------- #
G = "Type Specific Value"  # polymorphic Column H (picklist / referenceTo / formula / displayFormat)

RULES: dict[str, dict] = {
    "Text":                {"required": ["Length"], "optional": ["Unique", "External ID"], "forbidden": ["Precision", "Scale", "Visible Lines", "Mask Character"], "len": ("Length", 1, 255)},
    "TextArea":            {"required": ["Length"], "optional": [], "forbidden": ["Precision", "Scale", "Visible Lines", "Mask Character", "Unique", "External ID"], "len": ("Length", 1, 255)},
    "LongTextArea":        {"required": ["Length", "Visible Lines"], "optional": [], "forbidden": ["Precision", "Scale", "Mask Character", "Unique", "External ID"], "len": ("Length", 256, 131072), "vl": ("Visible Lines", 1, 50)},
    "Html":                {"required": ["Length", "Visible Lines"], "optional": [], "forbidden": ["Precision", "Scale", "Mask Character", "Unique", "External ID"], "len": ("Length", 256, 131072), "vl": ("Visible Lines", 1, 50)},
    "EncryptedText":       {"required": ["Length", "Mask Character"], "optional": [], "forbidden": ["Precision", "Scale", "Visible Lines"], "len": ("Length", 1, 175)},
    "Number":              {"required": ["Precision", "Scale"], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Visible Lines", "Mask Character"], "num": True},
    "Currency":            {"required": ["Precision", "Scale"], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Visible Lines", "Mask Character"], "num": True},
    "Percent":             {"required": ["Precision", "Scale"], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Visible Lines", "Mask Character"], "num": True},
    "Picklist":            {"required": [G], "optional": ["Global Value Set", "Restricted"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", "Unique", "External ID"], "picklist": True},
    "MultiselectPicklist": {"required": [G, "Visible Lines"], "optional": ["Global Value Set", "Restricted"], "forbidden": ["Length", "Precision", "Scale", "Mask Character", "Unique", "External ID"], "picklist": True, "vl": ("Visible Lines", 1, 50)},
    "Lookup":              {"required": [G], "optional": ["Relationship Label", "Relationship Name", "Delete Constraint"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character"], "ref": True},
    "MasterDetail":        {"required": [G, "Relationship Name"], "optional": ["Relationship Label"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character"], "ref": True},
    "AutoNumber":          {"required": [G], "optional": [], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", "Unique"]},
    "Summary":             {"required": ["Summary Foreign Key", "Summary Operation"], "optional": ["Summarized Field"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", "Unique", "External ID"], "summary": True},
    "Checkbox":            {"required": [], "optional": ["Default Value"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G, "Unique", "External ID"], "checkbox": True},
    "Date":                {"required": [], "optional": [], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G, "Unique", "External ID"]},
    "DateTime":            {"required": [], "optional": [], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G, "Unique", "External ID"]},
    "Time":                {"required": [], "optional": [], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G, "Unique", "External ID"]},
    "Email":               {"required": [], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G]},
    "Phone":               {"required": [], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G]},
    "Url":                 {"required": [], "optional": ["Unique", "External ID"], "forbidden": ["Length", "Precision", "Scale", "Visible Lines", "Mask Character", G]},
    "Location":            {"required": ["Scale"], "optional": [], "forbidden": ["Length", "Visible Lines", "Mask Character", "Unique", "External ID"]},
}

# Formula return types → underlying rule (numeric ones require precision+scale).
FORMULA_TYPES = {
    "Formula Checkbox": [G], "Formula Text": [G], "Formula Date": [G],
    "Formula Date/Time": [G], "Formula DateTime": [G], "Formula Time": [G],
    "Formula Number": [G, "Precision", "Scale"],
    "Formula Currency": [G, "Precision", "Scale"],
    "Formula Percent": [G, "Precision", "Scale"],
}
FORMULA_NUMERIC = {"Formula Number", "Formula Currency", "Formula Percent"}

UNIQUE_EXTID_TYPES = {"Text", "Email", "Phone", "Url", "Number", "Currency", "Percent"}
API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
TRUTHY = {"true", "y", "yes", "1", "○", "〇"}
BOOLEANISH = TRUTHY | {"false", "n", "no", "0", "×", "-", ""}


def _validate_translation(rep: Report, obj: str, loc: str, ja: str, en: str, *, kind: str) -> None:
    """Hard-block blank/invalid English. Never warn-and-continue for in-scope labels."""
    try:
        from translate_enrich import invalid_english, norm as _n
    except ImportError:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
        from translate_enrich import invalid_english, norm as _n
    ja_n, en_n = _n(ja), _n(en)
    if not ja_n:
        # object/name JA blank is already covered by sheet structure; field.label covers fields.
        if kind != "field":
            rep.error(obj, loc, "translation.ja", f"{kind} Japanese source label is blank")
        return
    if not en_n:
        rep.error(obj, loc, "translation.en",
                  f"{kind} English label is blank — cannot deploy without en_US")
        return
    reason = invalid_english(en_n, ja=ja_n, api=loc if loc != "-" else obj)
    if reason:
        rep.error(obj, loc, "translation.en", f"{kind} English is invalid: {reason}")


def truthy(v) -> bool:
    return str(v or "").strip().lower() in TRUTHY


def nonblank(v) -> bool:
    return bool(str(v or "").strip())


class Report:
    def __init__(self):
        self.items: list[dict] = []
        self.counts = defaultdict(int)

    def add(self, sev, obj, field, check, msg):
        self.items.append({"severity": sev, "object": obj, "field": field, "check": check, "message": msg})
        self.counts[sev] += 1

    def error(self, *a): self.add("ERROR", *a)
    def warn(self, *a):  self.add("WARN", *a)
    def info(self, *a):  self.add("INFO", *a)


def normalize_type(raw: str):
    t = str(raw or "").strip()
    if t in FORMULA_TYPES:
        return t, True
    # normalize a few aliases (Boolean is the sheet's spelling for Checkbox)
    aliases = {"Formula DateTime": "Formula Date/Time", "Boolean": "Checkbox"}
    return aliases.get(t, t), False


def validate(rows: list[dict], rep: Report, org_objects: set[str] | None = None) -> None:
    # object set present in this deploy (for dependency hints)
    deploy_objects = {r.get("Object API Name", "").strip()
                      for r in rows if r.get("_type") == "object_meta"}
    deploy_objects |= {r.get("Object API Name", "").strip()
                       for r in rows if r.get("Field API Name", "").strip()}
    # case-insensitive view of what's in THIS deploy set (SF API names are
    # case-insensitive, so parents created in this same batch count as present).
    deploy_objects_lc = {o.lower() for o in deploy_objects if o}

    seen: dict[tuple, int] = defaultdict(int)
    # Count Lookup + MasterDetail rows per object to enforce the 40 custom-
    # relationship-per-object hard limit (see KB 2026-08-20).
    rel_count: dict[str, int] = defaultdict(int)

    for r in rows:
        if r.get("_type") == "object_meta":
            obj = r.get("Object API Name", "").strip()
            if not obj:
                rep.error(r.get("Object Label", "?"), "-", "object.api", "Object API Name is blank")
            elif not (API_NAME_RE.match(obj) and obj.endswith("__c")):
                rep.error(obj, "-", "object.api", f"Object API '{obj}' invalid (must match API name regex and end __c)")
            obj_en = r.get("Object Label (EN)", "")
            # Object EN is optional: only a labeled object-meta cell counts.
            # Field Label (EN) / AJ1 is field-scope and must not be used here.
            if nonblank(obj_en):
                _validate_translation(rep, obj, "-", r.get("Object Label", ""),
                                      obj_en, kind="object")
            _validate_translation(rep, obj, "Name", r.get("Name Field Label", ""),
                                  r.get("Name Field Label (EN)", ""), kind="name")
            continue

        obj = r.get("Object API Name", "").strip() or r.get("_SheetName", "").strip()
        fapi = r.get("Field API Name", "").strip()
        label = r.get("Field Label", "").strip()
        raw_type = r.get("Data Type", "").strip()
        loc = fapi or label or "(row)"

        # ---- schema: required trio -------------------------------------- #
        if not label:
            rep.error(obj, loc, "field.label", "Field Label (label) is missing")
        if not raw_type:
            rep.error(obj, loc, "field.type", "Data Type (type) is missing")
        if not fapi:
            rep.error(obj, loc, "field.api", f"Field API Name (fullName) is missing for '{label}'")
        else:
            if not fapi.endswith("__c"):
                rep.warn(obj, loc, "field.api", f"'{fapi}' has no __c suffix — treated as standard field, not deployed")
            core = fapi[:-3] if fapi.endswith("__c") else fapi
            if not API_NAME_RE.match(core) or "__" in core:
                rep.error(obj, loc, "field.api", f"'{fapi}' is not a valid Salesforce API name")
            if len(core) > 40:
                rep.error(obj, loc, "field.api", f"'{fapi}' exceeds 40 chars")
            # duplicate detection (per object)
            seen[(obj, fapi)] += 1
            if seen[(obj, fapi)] == 2:
                rep.error(obj, fapi, "field.duplicate", f"Duplicate field API '{fapi}' in object '{obj}'")

        if fapi.endswith("__c"):
            _validate_translation(rep, obj, fapi, label, r.get("Field Label (EN)", ""), kind="field")

        # Standard fields (no __c) are never deployed by generate_xml, so their
        # field-definition columns (Length, referenceTo, Precision, etc.) are
        # irrelevant. Emit only the standard-field WARN above and skip the rest
        # to avoid spurious required.column / constraint / forbidden errors on
        # sheet-provided system rows (OwnerId, CreatedDate, LastModifiedDate…).
        if fapi and not fapi.endswith("__c"):
            continue

        if not raw_type:
            continue

        sf_type, is_formula = normalize_type(raw_type)

        # ---- unknown type ----------------------------------------------- #
        if is_formula:
            required = FORMULA_TYPES[raw_type]
            rule = {"required": required, "forbidden": [] if raw_type in FORMULA_NUMERIC
                    else ["Precision", "Scale"], "optional": []}
        elif sf_type in RULES:
            rule = RULES[sf_type]
        else:
            rep.error(obj, loc, "field.type", f"Unknown/unsupported Data Type '{raw_type}'")
            continue

        # ---- required columns ------------------------------------------- #
        for col in rule.get("required", []):
            if not nonblank(r.get(col)):
                # Empty FORMULA body is not a hard blocker — generate_xml injects a
                # return-type-appropriate dummy (see sf-deploy-delta-and-blockers).
                if is_formula and col == G:
                    rep.warn(obj, loc, "formula.autofill",
                             f"{raw_type}: formula body (Column H) is empty — generator "
                             f"injects a dummy; real formula supplied later")
                    continue
                # LongTextArea/Html visibleLines blank — generator defaults to 3.
                if col == "Visible Lines" and sf_type in ("LongTextArea", "Html"):
                    rep.warn(obj, loc, "visiblelines.default",
                             f"{raw_type}: 'Visible Lines' empty — generator defaults to 3")
                    continue
                rep.error(obj, loc, "required.column",
                          f"{raw_type}: required column '{col}' is empty")

        # ---- forbidden columns ------------------------------------------ #
        for col in rule.get("forbidden", []):
            if nonblank(r.get(col)):
                rep.warn(obj, loc, "forbidden.column",
                         f"{raw_type}: column '{col}'='{r.get(col)}' should be blank for this type")

        # ---- numeric constraints ---------------------------------------- #
        if rule.get("num") or raw_type in FORMULA_NUMERIC:
            _check_int(rep, obj, loc, "Precision", r.get("Precision"), 1, 18)
            _check_int(rep, obj, loc, "Scale", r.get("Scale"), 0, 17)
            p, s = _to_int(r.get("Precision")), _to_int(r.get("Scale"))
            if p is not None and s is not None and s > p:
                rep.error(obj, loc, "constraint", f"scale ({s}) must be <= precision ({p})")
        if "len" in rule:
            col, lo, hi = rule["len"]
            _check_int(rep, obj, loc, col, r.get(col), lo, hi)
        if "vl" in rule:
            col, lo, hi = rule["vl"]
            if nonblank(r.get(col)):
                _check_int(rep, obj, loc, col, r.get(col), lo, hi)

        # ---- unique / externalId gate ----------------------------------- #
        for col in ("Unique", "External ID"):
            if truthy(r.get(col)) and sf_type not in UNIQUE_EXTID_TYPES:
                rep.error(obj, loc, "unique.gate",
                          f"'{col}'=true not allowed for type '{sf_type}'")

        # ---- required-flag applicability -------------------------------- #
        NO_REQUIRED = {"Checkbox", "AutoNumber", "Summary", "LongTextArea",
                       "Html", "Location", "MultiselectPicklist", "MasterDetail"}
        if truthy(r.get("Required")):
            if sf_type in NO_REQUIRED:
                rep.info(obj, loc, "required.gate",
                         f"'Required'=true is invalid for type '{sf_type}'; the "
                         f"generator drops it (enforce via page layout / validation rule)")
            elif sf_type == "Lookup" and not str(r.get("Delete Constraint") or "").strip():
                ref = str(r.get(G) or r.get("Reference To") or "").strip()
                if ref in {"User", "Group", "RecordType", "Profile"}:
                    rep.info(obj, loc, "required.lookup",
                             f"Required Lookup to '{ref}' — this standard object rejects "
                             f"Restrict/Cascade child lookups; generator degrades it to an "
                             f"optional SetNull lookup (enforce required via layout/validation rule)")
                else:
                    rep.info(obj, loc, "required.lookup",
                             "Required Lookup — generator adds "
                             "<deleteConstraint>Restrict</deleteConstraint> "
                             "(Salesforce requires a delete constraint for required lookups)")

        # ---- boolean-ish sanity ----------------------------------------- #
        for col in ("Required", "Unique", "External ID", "Track History", "Restricted"):
            v = str(r.get(col) or "").strip().lower()
            if v and v not in BOOLEANISH:
                rep.warn(obj, loc, "boolean", f"'{col}'='{r.get(col)}' is not a boolean-ish value")

        # ---- checkbox default ------------------------------------------- #
        if rule.get("checkbox"):
            dv = str(r.get("Default Value") or "").strip().lower()
            if dv and dv not in {"true", "false"}:
                rep.error(obj, loc, "checkbox.default",
                          f"Checkbox Default Value must be TRUE/FALSE (got '{r.get('Default Value')}')")
            elif not dv:
                # Empty is fine: generate_xml emits <defaultValue>false</defaultValue>.
                rep.info(obj, loc, "checkbox.default",
                         "Checkbox has no Default Value; generator defaults it to false")

        # ---- picklist values -------------------------------------------- #
        if rule.get("picklist"):
            gval = str(r.get(G) or "").strip()
            gvs = str(r.get("Global Value Set") or "").strip()
            if not gvs and gval:
                # normalize full-width JP punctuation the same way generate_xml
                # does, so the guards see what will actually be parsed.
                norm = gval.translate({0xFF1B: ord(";"), 0xFF1A: ord(":")})
                if ("；" in gval or "：" in gval):
                    rep.warn(obj, loc, "picklist.fullwidth",
                             "Full-width '；'/'：' detected in picklist values; "
                             "normalized to ';'/':' at generate time")
                if "," in norm and ";" not in norm:
                    rep.warn(obj, loc, "picklist.delim",
                             "Picklist values look comma-separated; delimiter must be ';'")
                entries = [e.strip() for e in norm.split(";") if e.strip()]
                if not entries:
                    rep.error(obj, loc, "picklist.values", "Picklist has no usable values")
                seen_api: set[str] = set()
                for e in entries:
                    # count colons that are NOT escaped as "\:"
                    unescaped = e.replace("\\:", "")
                    ncolon = unescaped.count(":")
                    if ncolon >= 2:
                        rep.warn(obj, loc, "picklist.colon",
                                 f"Value '{e}' has multiple ':' — only the first splits "
                                 "Label:ApiName; escape a literal label colon as '\\:'")
                    if ncolon >= 1:
                        label, api = [p.strip() for p in e.replace("\\:", ":").split(":", 1)]
                        if not api:
                            rep.error(obj, loc, "picklist.api_empty",
                                      f"Value '{e}' has an empty ApiName after ':'")
                    else:
                        label = api = e.replace("\\:", ":")
                        if not api.isascii():
                            rep.warn(obj, loc, "picklist.nonascii_api",
                                     f"Value '{label}' has no ApiName; its stored value "
                                     f"becomes non-ASCII '{api}'. Use 'Label:ApiName' for an ASCII code.")
                    key = api.lower()
                    if key in seen_api:
                        rep.error(obj, loc, "picklist.dup",
                                  f"Duplicate picklist ApiName '{api}'")
                    seen_api.add(key)

        # ---- dependency: reference target ------------------------------- #
        if rule.get("ref"):
            # Count this relationship toward the object's 40-relationship budget.
            rel_count[obj] += 1
            ref = str(r.get(G) or r.get("Reference To") or "").strip()
            if not ref:
                rep.error(obj, loc, "dependency.ref", f"{raw_type}: referenceTo (Column H) is empty")
            elif not API_NAME_RE.match(ref):
                rep.error(obj, loc, "dependency.ref",
                          f"{raw_type}: referenceTo '{ref}' is not a valid API name (placeholder like 要確認?)")
            elif ref.endswith("__c") and ref not in deploy_objects:
                # Live org-existence check when --target-org was supplied. SF API
                # names are case-insensitive, so compare lowercased. A referenceTo
                # that is neither in this deploy batch nor in the org is a hard
                # blocker (this is exactly how TI_Fnt_BusinessPartner__c /
                # TI_Fnt_PaymentTerms__c slipped past validation — see KB 2026-08-26).
                if org_objects is not None:
                    if ref.lower() not in org_objects and ref.lower() not in deploy_objects_lc:
                        twin = ref[:-3] + "Master__c"  # common shorthand: X__c vs XMaster__c
                        hint = (f" — did you mean '{twin}'?"
                                if twin.lower() in org_objects else "")
                        rep.error(obj, loc, "dependency.ref.org",
                                  f"referenceTo '{ref}' does NOT exist in the target org "
                                  f"and is not created in this deploy{hint}")
                else:
                    rep.info(obj, loc, "dependency.ref",
                             f"referenceTo '{ref}' is a custom object not in this deploy set — ensure it exists in the target org")
            # relationshipName: MasterDetail already requires it via RULES; a blank
            # Lookup relationshipName reaches the org blank (generate_xml omits it)
            # and Salesforce REJECTS it — flag it here (see KB 2026-08-20).
            if sf_type == "Lookup" and not nonblank(r.get("Relationship Name")):
                rep.error(obj, loc, "dependency.relationshipname",
                          "Lookup: Relationship Name is blank — Salesforce requires "
                          "it on every relationship; fill it (or run fill_relationship_name.py) "
                          "before deploy")

        # ---- deleteConstraint (Lookup only) ----------------------------- #
        dc_raw = str(r.get("Delete Constraint") or "").strip()
        if dc_raw:
            dc = dc_raw.lower()
            if dc not in {"setnull", "restrict", "cascade", "cascadedelete"}:
                rep.error(obj, loc, "deleteconstraint.value",
                          f"Delete Constraint '{dc_raw}' invalid (SetNull / Restrict / Cascade)")
            elif sf_type != "Lookup":
                rep.warn(obj, loc, "deleteconstraint.type",
                         f"Delete Constraint set on non-Lookup type '{sf_type}' — ignored "
                         f"(only Lookup uses deleteConstraint)")
            else:
                ref = str(r.get(G) or r.get("Reference To") or "").strip()
                if dc in {"restrict", "cascade", "cascadedelete"} and \
                        ref in {"User", "Group", "RecordType", "Profile"}:
                    rep.error(obj, loc, "deleteconstraint.ref",
                              f"Delete Constraint '{dc_raw}' is not allowed on a Lookup to "
                              f"'{ref}' (only SetNull) — use SetNull or leave blank")
                if dc == "setnull" and truthy(r.get("Required")):
                    rep.info(obj, loc, "deleteconstraint.required",
                             "SetNull lookup cannot be required — generator drops 'required'")

        # ---- summary dependency ----------------------------------------- #
        if rule.get("summary"):
            op = str(r.get("Summary Operation") or "").strip().upper()
            if op and op not in {"SUM", "MIN", "MAX", "COUNT"}:
                rep.error(obj, loc, "summary.op", f"Summary Operation '{op}' invalid (SUM/MIN/MAX/COUNT)")
            if op in {"SUM", "MIN", "MAX"} and not nonblank(r.get("Summarized Field")):
                rep.error(obj, loc, "summary.field", f"Summarized Field required when operation is {op}")

    # ---- per-object 40 custom-relationship limit (post-scan) ------------- #
    # Salesforce hard cap: an object may have at most 40 custom relationships
    # (Lookup + MasterDetail). Only counts fields IN THIS DEPLOY SET; the deploy
    # is delta-only so this is the count being ADDED. Existing org relationships
    # are not included here — when redeploying onto a populated object, subtract
    # already-present relationships from the 40 budget before trusting this.
    for obj, n in sorted(rel_count.items()):
        if n > 40:
            rep.error(obj, "-", "relationship.limit",
                      f"{n} custom relationships (Lookup+MasterDetail) in this deploy "
                      f"set exceeds the 40-per-object hard limit — park or defer "
                      f"{n - 40} relationship field(s)")
        elif n > 35:
            rep.warn(obj, "-", "relationship.limit",
                     f"{n} custom relationships approaching the 40-per-object limit "
                     f"(only counts this deploy set; org may already hold some)")


def collect_reference_targets(rows: list[dict]) -> set[str]:
    """Distinct custom (__c) referenceTo values used by Lookup/MasterDetail rows."""
    refs: set[str] = set()
    for r in rows:
        if r.get("_type") == "object_meta":
            continue
        raw_type = str(r.get("Data Type") or "").strip()
        if raw_type not in ("Lookup", "MasterDetail"):
            continue
        ref = str(r.get(G) or r.get("Reference To") or "").strip()
        if ref.endswith("__c") and API_NAME_RE.match(ref):
            refs.add(ref)
    return refs


def query_org_objects(target_org: str, ref_names: set[str]) -> set[str]:
    """Return the lowercased set of the given object API names that EXIST in the
    org (Tooling-independent EntityDefinition). Also includes the *Master twins so
    a 'did you mean' hint can be offered. Case-insensitive."""
    want = set(ref_names)
    # include the Master twin of each ref so we can suggest it if the base is absent
    want |= {r[:-3] + "Master__c" for r in ref_names if r.endswith("__c")}
    if not want:
        return set()
    present: set[str] = set()
    names = sorted(want)
    # chunk to keep the IN() clause reasonable
    for i in range(0, len(names), 190):
        chunk = names[i:i + 190]
        inlist = "','".join(chunk)
        q = (f"SELECT QualifiedApiName FROM EntityDefinition "
             f"WHERE QualifiedApiName IN ('{inlist}')")
        try:
            cp = subprocess.run(
                ["sf", "data", "query", "--target-org", target_org, "--json", "--query", q],
                capture_output=True, text=True, timeout=120)
            data = json.loads(cp.stdout or "{}")
            if data.get("status") not in (0, None):
                msg = data.get("message") or data.get("name") or "unknown"
                print(f"⚠️  org referenceTo check failed ({msg}) — skipping org existence "
                      f"validation for this run.")
                return None  # signal: could not check (do not fail-closed)
            for rec in data.get("result", {}).get("records", []) or []:
                present.add(str(rec.get("QualifiedApiName", "")).lower())
        except Exception as e:
            print(f"⚠️  org referenceTo check failed ({e}) — skipping org existence "
                  f"validation for this run.")
            return None  # signal: could not check (do not fail-closed)
    return present


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _check_int(rep, obj, loc, col, v, lo, hi):
    if not nonblank(v):
        return
    n = _to_int(v)
    if n is None:
        rep.error(obj, loc, "constraint", f"'{col}'='{v}' must be an integer")
    elif not (lo <= n <= hi):
        rep.error(obj, loc, "constraint", f"'{col}'={n} out of range [{lo}, {hi}]")


def print_log(rep: Report, total_rows: int) -> None:
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    icon = {"ERROR": "❌", "WARN": "⚠️ ", "INFO": "ℹ️ "}
    print("=" * 72)
    print("  PRE-DEPLOYMENT VALIDATION LOG")
    print("=" * 72)
    for it in sorted(rep.items, key=lambda x: (x["object"], order[x["severity"]])):
        print(f"{icon[it['severity']]} [{it['object']}::{it['field']}] "
              f"({it['check']}) {it['message']}")
    if not rep.items:
        print("  (no issues found)")
    print("-" * 72)
    e, w, i = rep.counts["ERROR"], rep.counts["WARN"], rep.counts["INFO"]
    status = "FAIL" if e else "PASS"
    print(f"  ROWS: {total_rows}   ERRORS: {e}   WARNINGS: {w}   INFO: {i}")
    print(f"  RESULT: {status}" + ("  — deployment BLOCKED" if e else "  — safe to build package"))
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate DD rows before packaging")
    ap.add_argument("--in", dest="inp", default="temp_updates.json")
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--strict", action="store_true", help="treat WARN as blocking too")
    ap.add_argument("--target-org", default="",
                    help="if set, live-verify every Lookup/MasterDetail referenceTo "
                         "custom object EXISTS in this org (ERROR on missing, with a "
                         "'…Master__c?' hint). Offline when omitted.")
    args = ap.parse_args()

    try:
        rows = json.load(open(args.inp, encoding="utf-8"))
    except FileNotFoundError:
        print(f"❌ input '{args.inp}' not found — run fetch_sheet.py first.")
        return 1

    org_objects = None
    if args.target_org:
        refs = collect_reference_targets(rows)
        if refs:
            print(f"🔎 checking {len(refs)} distinct referenceTo target(s) live "
                  f"against org '{args.target_org}'…")
            org_objects = query_org_objects(args.target_org, refs)

    rep = Report()
    validate(rows, rep, org_objects=org_objects)
    print_log(rep, len(rows))

    if args.json_out:
        json.dump({"counts": dict(rep.counts), "items": rep.items},
                  open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"  report written: {args.json_out}")

    blocking = rep.counts["ERROR"] + (rep.counts["WARN"] if args.strict else 0)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
