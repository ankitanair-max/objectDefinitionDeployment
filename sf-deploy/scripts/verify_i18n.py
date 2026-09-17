#!/usr/bin/env python3
"""
verify_i18n.py — post-deploy live check that packaged translations landed.

Reads the delta JSON (only rows with package=true) and re-reads the org via
Metadata API. Exit 0 = every packaged translation matches the sheet text.

Usage:
  python scripts/verify_i18n.py --delta .build/i18n_drift.json --org ERPDEV01 --lang en_US
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from i18n_lib import (  # noqa: E402
    KIND_CUSTOM_LABEL, content_hash, load_token, parse_object_translation,
    parse_translations, read_metadata,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify packaged translations in the org")
    ap.add_argument("--delta", default=".build/i18n_drift.json")
    ap.add_argument("--org", required=True)
    ap.add_argument("--lang", default="en_US")
    args = ap.parse_args()

    delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
    expected = [e for e in delta if e.get("package")]
    if not expected:
        print("verify_i18n: nothing was packaged — nothing to verify.")
        return 0

    a = load_token(args.org)
    tok, inst, ver = a["accessToken"], a["instanceUrl"].rstrip("/"), a["apiVersion"]

    org: dict[str, dict] = {}
    objs = sorted({e["component"] for e in expected if e["kind"].startswith("Object")
                   or e["kind"] == "NameField"})
    if objs:
        recs = read_metadata("CustomObjectTranslation",
                             [f"{o}-{args.lang}" for o in objs], tok, inst, ver)
        for rec in recs:
            fn = (rec.findtext("fullName") or "").strip()
            obj = fn.rsplit("-", 1)[0] if fn else ""
            if obj:
                org.update(parse_object_translation(rec, obj, args.lang))
    need_tr = any(e["kind"] == KIND_CUSTOM_LABEL or e["kind"].startswith("Flow")
                  for e in expected)
    if need_tr:
        recs = read_metadata("Translations", [args.lang], tok, inst, ver)
        for rec in recs:
            org.update(parse_translations(rec, args.lang))

    missing = []
    mismatch = []
    for e in expected:
        got = org.get(e["id"])
        if got is None or not got.get("translation"):
            missing.append(e["id"])
            continue
        if content_hash(got["translation"]) != e.get("hash"):
            mismatch.append((e["id"], e.get("translation"), got.get("translation")))

    print("=" * 72)
    print(f"  verify_i18n  packaged={len(expected)}  missing={len(missing)}  mismatch={len(mismatch)}")
    print("=" * 72)
    for i in missing:
        print(f"  MISSING   {i}")
    for i, exp, got in mismatch:
        print(f"  MISMATCH  {i}")
        print(f"            sheet={exp!r}")
        print(f"            org  ={got!r}")
    if missing or mismatch:
        print("⛔ translations did NOT fully land.")
        return 1
    print("  ✅ every packaged translation confirmed in the org.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
