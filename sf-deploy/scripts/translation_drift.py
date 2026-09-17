#!/usr/bin/env python3
"""
translation_drift.py — sheet (live catalog) vs org (live Metadata API) translation delta.

Same role as attr_drift.py, for translations:
  name-existence is not enough — compare the TRANSLATION TEXT (hash) too.

Usage:
  python scripts/translation_drift.py --catalog .build/translation_catalog.json --org ERPDEV01 \
      [--lang en_US] [--conflict park] [--out .build/translation_drift.json]

Exit 0 always (report-only) unless --fail-on-conflict.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "scripts")
from translation_lib import (  # noqa: E402
    CHANGED, CONFLICT, DEFAULT_LANG, INVALID_LANG,
    KIND_OBJECT_FIELD, KIND_OBJECT_HELP, KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST,
    KIND_OBJECT_REL, KIND_NAME_FIELD, MISSING, NEW, ORG_ONLY, UNCHANGED,
    apply_new_only, classify, load_sync_state, load_token, parse_object_translation,
    read_metadata,
)


def org_index(entries: list[dict], org: str, lang: str) -> dict[str, dict]:
    objs = sorted({e["component"] for e in entries
                   if e["kind"] in {KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
                                    KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST,
                                    KIND_OBJECT_REL, KIND_NAME_FIELD}})

    tokinfo = load_token(org)
    tok, inst, ver = (tokinfo["accessToken"],
                      tokinfo["instanceUrl"].rstrip("/"),
                      tokinfo["apiVersion"])
    idx: dict[str, dict] = {}

    if objs:
        members = [f"{o}-{lang}" for o in objs]
        recs = read_metadata("CustomObjectTranslation", members, tok, inst, ver)
        for rec in recs:
            fn = (rec.findtext("fullName") or "").strip()
            obj_api = fn.rsplit("-", 1)[0] if fn else ""
            if not obj_api:
                continue
            idx.update(parse_object_translation(rec, obj_api, lang))
        print(f"  org CustomObjectTranslation {lang}: {len(idx)} key(s)")
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheet vs org translation drift")
    ap.add_argument("--catalog", default=".build/translation_catalog.json")
    ap.add_argument("--org", required=True)
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--conflict", default="park",
                    choices=["park", "sheet-wins", "org-wins"])
    ap.add_argument("--new-only", action="store_true",
                    help="package NEW_TRANSLATION only (future field adds on "
                         "already-translated objects). CHANGED is reported, not packaged.")
    ap.add_argument("--sync-state", default=".build/translation_sync_state.json")
    ap.add_argument("--out", default=".build/translation_drift.json")
    ap.add_argument("--fail-on-conflict", action="store_true")
    args = ap.parse_args()

    sheet = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    # Only compare the requested language
    sheet = [e for e in sheet if e.get("language", args.lang) == args.lang]
    print(f"translation_drift  org={args.org}  lang={args.lang}  sheet={len(sheet)}")

    org_by_id = org_index(sheet, args.org, args.lang)
    sync = load_sync_state(args.sync_state).get("entries") or {}
    classified = classify(sheet, org_by_id, sync, conflict_policy=args.conflict)
    if args.new_only:
        classified = apply_new_only(classified)

    counts = defaultdict(int)
    packaged = 0
    for c in classified:
        counts[c["code"]] += 1
        if c.get("package"):
            packaged += 1

    print("=" * 88)
    print(f"  NEW={counts[NEW]}  CHANGED={counts[CHANGED]}  UNCHANGED={counts[UNCHANGED]}  "
          f"MISSING={counts[MISSING]}  CONFLICT={counts[CONFLICT]}  "
          f"ORG_ONLY={counts[ORG_ONLY]}  INVALID_LANG={counts[INVALID_LANG]}")
    print(f"  packaging {packaged} translation(s)  conflict policy={args.conflict}")
    print("=" * 88)
    for c in classified:
        if c["code"] in {UNCHANGED, ORG_ONLY}:
            continue
        flag = "PKG" if c.get("package") else "   "
        print(f"  [{flag}] {c['code']:<20} {c.get('id','')}")
        if c.get("reason"):
            print(f"           {c['reason']}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(classified, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\nsaved → {args.out}")
    if args.fail_on_conflict and counts[CONFLICT]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
