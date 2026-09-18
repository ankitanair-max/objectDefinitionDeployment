#!/usr/bin/env python3
"""
translation_drift.py — ad-hoc REPORT of sheet vs org translation classification.

This is NOT a deployment command and is NOT on the canonical path.
``plan_deploy.py`` calls ``translation_lib.classify()`` (the same function)
when building the deployment plan. Use this CLI only to print the delta
without packaging.

Tabs without a ``Field Label (EN)`` column are untranslated: the delta is empty
and no org session / Metadata API call is made at all.

Authentication comes from the Salesforce CLI (`sf org display --verbose --json`
for `--org`), so any authorized machine works; the last-deploy hashes are kept
per target ORG ID, never per alias.

Usage:
  python scripts/translation_drift.py --rows temp_updates.json --org <ORG> \
      [--lang en_US] [--new-only] [--out .build/translation_drift.json]

Exit codes: 0 ok · 1 sheet defect (unparseable EN cell / invalid language code)
· 2 conflicts with --fail-on-conflict · 3 translations unavailable in the org.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import org_snapshot  # noqa: E402
from translation_lib import (  # noqa: E402
    BUILD_DIR, CHANGED, CONFLICT, DEFAULT_LANG, DEFAULT_SYNC_STATE, INVALID_LANG,
    KIND_OBJECT_FIELD, KIND_OBJECT_HELP, KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST,
    KIND_OBJECT_REL, KIND_NAME_FIELD, MISSING, NEW, ORG_ONLY, PARSE_ERROR,
    SCHEMA_MISSING, UNCHANGED, OrgAuthError, TranslationUnavailable,
    apply_new_only, classify,
    entries_from_object_rows, has_translation_columns, load_sync_state, org_auth,
    parse_object_translation, read_object_translations,
)


def write_delta(out: str, classified: list[dict]) -> None:
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(classified, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def org_index(entries: list[dict], lang: str, auth: dict) -> dict[str, dict]:
    objs = sorted({e["component"] for e in entries
                   if e["kind"] in {KIND_OBJECT_FIELD, KIND_OBJECT_HELP,
                                    KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST,
                                    KIND_OBJECT_REL, KIND_NAME_FIELD}})
    idx: dict[str, dict] = {}
    if objs:
        for rec in read_object_translations(objs, lang, auth):
            fn = (rec.findtext("fullName") or "").strip()
            obj_api = fn.rsplit("-", 1)[0] if fn else ""
            if not obj_api:
                continue
            idx.update(parse_object_translation(rec, obj_api, lang))
        print(f"  org CustomObjectTranslation {lang}: {len(idx)} key(s)")
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description="Sheet vs org translation drift")
    ap.add_argument("--rows", default="",
                    help="temp_updates.json from a live fetch_sheet.py")
    ap.add_argument("--catalog", default="",
                    help="optional pre-built catalog JSON (instead of --rows)")
    ap.add_argument("--org", required=True)
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--conflict", default="park",
                    choices=["park", "sheet-wins", "org-wins"])
    ap.add_argument("--new-only", action="store_true",
                    help="package NEW_TRANSLATION only (future field adds on "
                         "already-translated objects). CHANGED is reported, not packaged.")
    ap.add_argument("--sync-state", default=DEFAULT_SYNC_STATE)
    ap.add_argument("--out", default=str(BUILD_DIR / "translation_drift.json"))
    ap.add_argument("--fail-on-conflict", action="store_true")
    ap.add_argument("--snapshot", default="",
                    help="org_snapshot.json — classify against the ONE bulk org "
                         "read instead of querying again")
    ap.add_argument("--on-unavailable", choices=["error", "skip"], default="error",
                    help="org without Translation Workbench / the language active: "
                         "fail with an actionable message (default) or report an "
                         "empty delta so the field deploy continues")
    args = ap.parse_args()

    sheet: list[dict] = []
    if args.catalog:
        sheet.extend(json.loads(Path(args.catalog).read_text(encoding="utf-8")))
    if args.rows:
        rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
        if has_translation_columns(rows):
            sheet.extend(entries_from_object_rows(rows, lang=args.lang))
        elif not sheet:
            print("translation_drift: no 'Field Label (EN)' column on the target "
                  "tab(s) — untranslated, no org call needed.")
            write_delta(args.out, [])
            return 0
    if not sheet:
        print("❌ translation_drift: pass --rows temp_updates.json (or --catalog)")
        return 1
    sheet = [e for e in sheet if e.get("language", args.lang) == args.lang]
    print(f"translation_drift  org={args.org}  lang={args.lang}  sheet={len(sheet)}")

    org_schema = None
    if args.snapshot:
        snapshot = org_snapshot.load(args.snapshot)
        state = snapshot.get("translationState")
        if state not in ("ok", "off"):
            note = snapshot.get("translationNote") or state
            if args.on_unavailable == "error":
                print(f"❌ translations unavailable: {note}")
                return 3
            print(f"⏭  translations skipped — {note}")
            write_delta(args.out, [])
            return 0
        org_id = (snapshot.get("target") or {}).get("orgId", "")
        org_schema = org_snapshot.org_schema(snapshot)
        org_by_id = {}
        for obj in sorted({e["component"] for e in sheet}):
            rec = org_snapshot.translation_record(snapshot, obj)
            if rec is not None:
                org_by_id.update(parse_object_translation(rec, obj, args.lang))
        print(f"  org CustomObjectTranslation {args.lang}: {len(org_by_id)} key(s) "
              f"(from snapshot)")
    else:
        try:
            auth = org_auth(args.org)
        except OrgAuthError as e:
            print(f"❌ {e}")
            return 1
        try:
            org_by_id = org_index(sheet, args.lang, auth)
        except TranslationUnavailable as e:
            if args.on_unavailable == "error":
                print(f"❌ {e}")
                return 3
            print(f"⏭  translations skipped — {e}")
            write_delta(args.out, [])
            return 0
        org_id = auth.get("orgId", "")

    sync = load_sync_state(args.sync_state, org_id=org_id).get("entries") or {}
    classified = classify(sheet, org_by_id, sync, conflict_policy=args.conflict,
                          org_schema=org_schema)
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
          f"ORG_ONLY={counts[ORG_ONLY]}  INVALID_LANG={counts[INVALID_LANG]}  "
          f"PARSE_ERROR={counts[PARSE_ERROR]}  SCHEMA_MISSING={counts[SCHEMA_MISSING]}")
    print(f"  packaging {packaged} translation(s)  conflict policy={args.conflict}")
    print("=" * 88)
    for c in classified:
        if c["code"] in {UNCHANGED, ORG_ONLY}:
            continue
        flag = "PKG" if c.get("package") else "   "
        print(f"  [{flag}] {c['code']:<20} {c.get('id','')}")
        if c.get("reason"):
            print(f"           {c['reason']}")

    write_delta(args.out, classified)
    print(f"\nsaved → {args.out}")
    if counts[PARSE_ERROR] or counts[INVALID_LANG]:
        print(f"⛔ {counts[PARSE_ERROR]} unparseable EN cell(s) and "
              f"{counts[INVALID_LANG]} invalid language code(s) — fix the sheet; "
              f"these are validation errors, not missing translations.")
        return 1
    if args.fail_on_conflict and counts[CONFLICT]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
