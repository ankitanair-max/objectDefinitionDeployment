#!/usr/bin/env python3
"""
generate_object_translation.py — CustomObjectTranslation XML from Field Label (EN).

Source of truth: the object-definition tab on the live Data Dictionary sheet
https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
English is the header ``Field Label (EN)``. Japanese in ``Field Label`` stays
CustomField.label. Locate EN by header name (same as fullName / WIP / IsDelete).

Tabs WITHOUT a ``Field Label (EN)`` column are untranslated: they produce no
entries at all, so no org session and no Metadata API call is needed for them.

A CustomObjectTranslation deploys as one whole object-language member, so
anything missing from the file we write is erased in the org. This script
therefore never rebuilds the file from our own model. It reads the org's
existing translation, keeps its element tree, and patches ONLY the nodes the
delta says are new:

  * the parent keeps its recordTypes, layouts, validationRules, fieldSets,
    quickActions, webLinks, sharingReasons, workflowTasks, gender/startsWith
    and its plural/case caseValues variants,
  * every already-translated field keeps its own file verbatim,
  * the standard Name field is translated through the parent <nameFieldLabel>.

Future delta (Japan adds a field to an already-translated object):
  1. They add the row (JA in Field Label, EN in Field Label (EN), API in fullName).
  2. translation_drift --new-only marks that field NEW_TRANSLATION.
  3. This script patches that one field into the retrieved org translation.

Usage:
  python scripts/generate_object_translation.py --rows temp_updates.json \
      --snapshot .build/org_snapshot.json --lang en_US --delta .build/deploy_plan.json
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
    CFT_CHILD_ORDER, COT_CHILD_ORDER, DEFAULT_LANG, KIND_NAME_FIELD,
    KIND_OBJECT_FIELD, KIND_OBJECT_HELP, KIND_OBJECT_LABEL, KIND_OBJECT_PICKLIST,
    KIND_OBJECT_REL, REPO_ROOT, OrgAuthError, TranslationUnavailable,
    entries_from_object_rows, field_element, has_translation_columns,
    index_entries, norm, org_auth, read_object_translations, render_metadata,
    set_object_label, set_parent_text, set_picklist_translation, set_text,
    split_object_translation, write_xml,
)

OUT_ROOT = REPO_ROOT / "force-app/main/default/objectTranslations"

TRANSLATION_KINDS = {KIND_OBJECT_LABEL, KIND_NAME_FIELD, KIND_OBJECT_FIELD,
                     KIND_OBJECT_HELP, KIND_OBJECT_PICKLIST, KIND_OBJECT_REL}


def _group(entries: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e["kind"] in TRANSLATION_KINDS:
            g[e["component"]].append(e)
    return dict(sorted(g.items()))


def _merge_org(sheet_entries: list[dict], org_by_id: dict[str, dict],
               overlay_ids: set[str] | None) -> dict[str, dict]:
    """Org translations stay; overlay only the NEW (packaged) sheet entries.

    Kept as the flat view used for reporting and tests; the file writer works
    on the org's element tree so unmodelled nodes survive untouched.
    """
    merged = dict(org_by_id)
    for e in sheet_entries:
        if not e.get("translation"):
            continue
        if overlay_ids is None or e["id"] in overlay_ids:
            merged[e["id"]] = e
    return merged


def _field_of(entry: dict) -> str:
    return norm(entry.get("field")) or norm(entry["key"]).split("::", 1)[0]


def patch_translation(org_rec, entries: list[dict]):
    """Apply `entries` onto the org's translation tree.

    Returns (parent_children, {fieldName: <fields> element}); every node the
    entries do not mention is the org's own, unmodified.

    Entries are indexed once (object-level vs per-field) and each field is then
    visited exactly once, so a 200-field object costs O(entries), not
    O(entries × fields).
    """
    parent, fields = split_object_translation(org_rec)
    indexed = index_entries([e for e in entries if e["kind"] in TRANSLATION_KINDS])

    def apply(entry, field_el):
        value = norm(entry.get("translation"))
        if not value:
            return
        kind = entry["kind"]
        if kind == KIND_OBJECT_FIELD:
            set_text(field_el, "label", value)
        elif kind == KIND_OBJECT_HELP:
            set_text(field_el, "help", value)
        elif kind == KIND_OBJECT_REL:
            set_text(field_el, "relationshipLabel", value)
        elif kind == KIND_OBJECT_PICKLIST:
            master = norm(entry.get("master")) or norm(entry["key"]).split("::", 1)[-1]
            set_picklist_translation(field_el, master, value)

    for bucket in indexed.values():
        for e in bucket["object"]:
            value = norm(e.get("translation"))
            if not value:
                continue
            if e["kind"] == KIND_OBJECT_LABEL:
                parent = set_object_label(parent, value)
            elif e["kind"] == KIND_NAME_FIELD:
                parent = set_parent_text(parent, "nameFieldLabel", value)
        for name, ents in bucket["fields"].items():
            if not any(norm(e.get("translation")) for e in ents):
                continue
            if name not in fields:
                fields[name] = field_element(name)
            for e in ents:
                apply(e, fields[name])
    return parent, dict(sorted(fields.items()))


def write_translation_dir(root: Path, obj: str, lang: str,
                          parent, fields: dict) -> int:
    folder = root / f"{obj}-{lang}"
    write_xml(folder / f"{obj}-{lang}.objectTranslation-meta.xml",
              render_metadata("CustomObjectTranslation", parent, COT_CHILD_ORDER))
    for name, el in sorted(fields.items()):
        write_xml(folder / f"{name}.fieldTranslation-meta.xml",
                  render_metadata("CustomFieldTranslation", list(el), CFT_CHILD_ORDER))
    return len(fields)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate CustomObjectTranslation XML")
    ap.add_argument("--rows", default="", help="temp_updates.json from fetch_sheet.py")
    ap.add_argument("--catalog", default="", help="translation_catalog.json")
    ap.add_argument("--delta", default="", help="deploy_plan.json — only the entries the plan packages")
    ap.add_argument("--org", default="",
                    help="only needed without --snapshot (a live per-object read)")
    ap.add_argument("--snapshot", default="",
                    help="org_snapshot.json — reuse the ONE bulk org read "
                         "instead of retrieving each object again")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--on-unavailable", choices=["error", "skip"], default="error",
                    help="org without Translation Workbench / the language active")
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    args = ap.parse_args(argv)

    entries: list[dict] = []
    if args.catalog:
        entries.extend(json.loads(Path(args.catalog).read_text(encoding="utf-8")))
    if args.rows:
        rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
        if has_translation_columns(rows):
            entries.extend(entries_from_object_rows(rows, lang=args.lang))
        elif not entries:
            print("generate_object_translation: no 'Field Label (EN)' column on the "
                  "target tab(s) — untranslated, nothing to generate.")
            return 0
    entries = [e for e in entries if e.get("language", args.lang) == args.lang]
    entries = [e for e in entries if not e.get("parse_error")]
    if not entries:
        print("generate_object_translation: no catalog entries — nothing to write.")
        return 0

    package_ids: set[str] | None = None
    if args.delta:
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        # accept either a plain delta list or a deploy_plan.json
        if isinstance(delta, dict):
            delta = delta.get("translations") or []
        package_ids = {d["id"] for d in delta if d.get("package")}

    grouped = _group(entries)
    targets = {obj: ents for obj, ents in grouped.items()
               if any(e.get("translation") for e in ents)
               and (package_ids is None
                    or any(e["id"] in package_ids for e in ents))}
    if not targets:
        print("generate_object_translation: delta packages nothing — no file written.")
        return 0

    snapshot = None
    auth = None
    if args.snapshot:
        snapshot = org_snapshot.load(args.snapshot)
        if snapshot.get("translationState") not in ("ok", "off"):
            msg = snapshot.get("translationNote") or snapshot["translationState"]
            if args.on_unavailable == "error":
                print(f"❌ translations unavailable in {snapshot['target'].get('alias')}: {msg}")
                return 1
            print(f"⏭  translations skipped — {msg}")
            return 0
    elif args.org:
        try:
            auth = org_auth(args.org)
        except OrgAuthError as e:
            print(f"❌ {e}")
            return 1

    root = Path(args.out_root)
    written = 0
    for obj, ents in sorted(targets.items()):
        org_rec = None
        if snapshot is not None:
            # reuse the bulk snapshot — no extra Metadata API call per object
            org_rec = org_snapshot.translation_record(snapshot, obj)
        elif auth:
            try:
                recs = read_object_translations([obj], args.lang, auth)
            except TranslationUnavailable as e:
                if args.on_unavailable == "skip":
                    print(f"  ⏭  {obj}-{args.lang}: skipped — {e}")
                    continue
                print(f"❌ {e}")
                return 1
            org_rec = recs[0] if recs else None
        elif package_ids is None:
            print(f"  ⚠️  {obj}-{args.lang}: no --org/--snapshot, writing sheet-only "
                  f"translation (existing org translations are NOT merged).")

        patch_ents = [e for e in ents
                      if package_ids is None or e["id"] in package_ids]
        parent, fields = patch_translation(org_rec, patch_ents)
        n = write_translation_dir(root, obj, args.lang, parent, fields)
        patched = sorted({_field_of(e) for e in patch_ents
                          if e["kind"] not in {KIND_OBJECT_LABEL, KIND_NAME_FIELD}
                          and e.get("translation")})
        print(f"  ✓ {obj}-{args.lang}: {n} field translation file(s), "
              f"{len(patched)} patched, rest preserved from org")
        written += 1

    print(f"generate_object_translation: wrote {written} object translation(s) under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
