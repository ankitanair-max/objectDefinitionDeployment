#!/usr/bin/env python3
"""
validate_i18n.py — HARD GATE for object-tab English (CustomObjectTranslation).

Reads `.build/i18n_catalog.json` (from fetch_i18n.py) and reports ERROR/WARN
before any XML is generated. Exit 1 when any ERROR is present.

Checks:
  * invalid / blank Salesforce language codes
  * blank object / field API name
  * picklist EN parse errors (count mismatch / unknown masterLabel)
  * duplicate catalog ids
  * optional --org: field / picklist value existence (Tooling CustomField)

Blank translation cells are WARN (MISSING_TRANSLATION): do not invent English.
Flip to ERROR with --strict-missing.

Usage:
  python scripts/validate_i18n.py --in .build/i18n_catalog.json \
      --json .build/i18n_validation.json [--org ERPDEV01] [--strict-missing]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "scripts")
from i18n_lib import (  # noqa: E402
    KIND_OBJECT_FIELD, KIND_OBJECT_PICKLIST,
    load_token, read_metadata, norm,
)


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def error(self, loc: str, check: str, msg: str):
        self.items.append({"sev": "ERROR", "loc": loc, "check": check, "message": msg})
        print(f"  ERROR  {loc}  [{check}] {msg}")

    def warn(self, loc: str, check: str, msg: str):
        self.items.append({"sev": "WARN", "loc": loc, "check": check, "message": msg})
        print(f"  WARN   {loc}  [{check}] {msg}")

    def counts(self) -> dict:
        c = defaultdict(int)
        for i in self.items:
            c[i["sev"]] += 1
        return dict(c)


def _soql_tooling(query: str, org: str) -> list[dict]:
    cp = subprocess.run(
        ["sf", "data", "query", "--use-tooling-api", "--query", query,
         "--target-org", org, "--json"],
        capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return []
    if data.get("status") != 0:
        return []
    return data.get("result", {}).get("records", []) or []


def org_custom_fields(obj: str, org: str) -> set[str]:
    recs = _soql_tooling(
        "SELECT DeveloperName FROM CustomField "
        f"WHERE EntityDefinition.QualifiedApiName='{obj}'", org)
    return {r["DeveloperName"] + "__c" for r in recs if r.get("DeveloperName")}


def org_picklist_masters(obj: str, field: str, tok, inst, ver) -> set[str] | None:
    recs = read_metadata("CustomObject", [obj], tok, inst, ver)
    if not recs:
        return None
    for f in recs[0].findall("fields"):
        if norm(f.findtext("fullName")) != field:
            continue
        masters = set()
        for v in f.iter("value"):
            masters.add(norm(v.findtext("label")) or norm(v.findtext("fullName")))
        return masters
    return set()  # field missing


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate object-translation catalog (hard gate)")
    ap.add_argument("--in", dest="inp", default=".build/i18n_catalog.json")
    ap.add_argument("--json", dest="out", default=".build/i18n_validation.json")
    ap.add_argument("--org", default="")
    ap.add_argument("--strict-missing", action="store_true",
                    help="blank translation cells become ERROR instead of WARN")
    args = ap.parse_args()

    entries = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    rep = Report()
    print(f"validate_i18n: {len(entries)} catalog entr(y/ies)")

    seen_ids: dict[str, str] = {}
    for e in entries:
        loc = f"{e.get('source','?')} row {e.get('sheet_row') or '-'} {e.get('id','')}"
        if e.get("parse_error"):
            rep.error(loc, "picklist.parse", e["parse_error"])
            continue
        if e.get("lang_error"):
            rep.error(loc, "language", e["lang_error"])
        if not e.get("component"):
            rep.error(loc, "component", "blank object API name")
        if not e.get("translation"):
            (rep.error if args.strict_missing else rep.warn)(
                loc, "translation.blank", "blank translation — not packaged, not invented")
        eid = e.get("id")
        if eid and eid in seen_ids:
            rep.error(loc, "duplicate", f"duplicate catalog id (also {seen_ids[eid]})")
        elif eid:
            seen_ids[eid] = loc

    if args.org:
        print(f"\n  live org checks against {args.org} …")
        tokinfo = load_token(args.org)
        tok, inst, ver = tokinfo["accessToken"], tokinfo["instanceUrl"].rstrip("/"), tokinfo["apiVersion"]
        field_cache: dict[str, set[str]] = {}
        pick_cache: dict[tuple[str, str], set[str] | None] = {}

        for e in entries:
            loc = f"{e.get('source','?')} {e.get('id','')}"
            kind = e.get("kind")
            if kind == KIND_OBJECT_FIELD and e.get("translation"):
                obj, field = e["component"], e["key"]
                if obj not in field_cache:
                    field_cache[obj] = org_custom_fields(obj, args.org)
                if field not in field_cache[obj]:
                    rep.error(loc, "schema.field",
                              f"field {obj}.{field} not in org — cannot translate a missing field")
            if kind == KIND_OBJECT_PICKLIST and e.get("translation"):
                obj = e["component"]
                field = (e.get("field") or e["key"].split("::", 1)[0])
                master = e.get("master") or (e["key"].split("::", 1)[1] if "::" in e["key"] else "")
                ck = (obj, field)
                if ck not in pick_cache:
                    pick_cache[ck] = org_picklist_masters(obj, field, tok, inst, ver)
                masters = pick_cache[ck]
                if masters is None:
                    rep.error(loc, "schema.object", f"object {obj} not in org")
                elif master and master not in masters:
                    rep.error(loc, "schema.picklist",
                              f"picklist masterLabel {master!r} not on {obj}.{field} in org")

    counts = rep.counts()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(
        {"counts": counts, "items": rep.items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    n_err = counts.get("ERROR", 0)
    n_warn = counts.get("WARN", 0)
    print(f"\n{'FAIL' if n_err else 'PASS'}: {n_err} ERROR  {n_warn} WARN  → {args.out}")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
