#!/usr/bin/env python3
"""
build_manifest.py — Generate a Salesforce deploy manifest (package.xml) and,
optionally, a destructiveChanges.xml, from the local `force-app` source tree.

The legacy CI pipeline deployed via `--source-dir` file lists and never built
a manifest. This script produces a proper, dependency-ordered `package.xml`
so deployments are explicit, reviewable, and reproducible.

Metadata types discovered under force-app/main/default/:
  - CustomObject      objects/<Api>/<Api>.object-meta.xml         -> member <Api>
  - CustomField       objects/<Api>/fields/<Field>.field-meta.xml -> member <Api>.<Field>
  - Layout            layouts/<file>.layout-meta.xml              -> member <file>
  - FlexiPage         flexipages/<file>.flexipage-meta.xml        -> member <file>
  - PermissionSet     permissionsets/<file>.permissionset-meta.xml-> member <file>
  - RecordType        objects/<Api>/recordTypes/<RT>.recordType-meta.xml -> <Api>.<RT>

Usage:
  python scripts/build_manifest.py \
      [--source-root force-app/main/default] \
      [--out manifest/package.xml] \
      [--api-version 60.0] \                       # default: from sfdx-project.json
      [--only Sales_Deal__c,Other__c] \            # restrict to these objects
      [--destroy deletions.json --destroy-out manifest/destructiveChanges.xml]

`deletions.json` shape (for destructive changes):
  { "CustomField": ["Obj__c.Field__c"], "CustomObject": ["Old__c"] }
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote  # noqa: F401  (kept for parity with URL-ish inputs)
import xml.sax.saxutils as sx

# Ordered so parents deploy before children (objects before fields/record types,
# permissionsets/layouts/flexipages after the objects they reference).
TYPE_ORDER = [
    "CustomObject",
    "CustomField",
    "CustomLabels",
    "CustomObjectTranslation",
    "GlobalValueSetTranslation",
    "Translations",
    "RecordType",
    "Layout",
    "FlexiPage",
    "PermissionSet",
]


def default_api_version(root: Path) -> str:
    proj = root / "sfdx-project.json"
    try:
        data = json.loads(proj.read_text())
        v = str(data.get("sourceApiVersion", "")).strip()
        if v:
            return v
    except Exception:
        pass
    return "60.0"


def discover(source_root: Path, only: set[str] | None,
             only_types: set[str] | None = None) -> dict[str, list[str]]:
    members: dict[str, set[str]] = defaultdict(set)
    objects_dir = source_root / "objects"

    if objects_dir.is_dir():
        for obj_dir in sorted(p for p in objects_dir.iterdir() if p.is_dir()):
            obj = obj_dir.name
            if only and obj not in only:
                continue
            if (obj_dir / f"{obj}.object-meta.xml").exists():
                members["CustomObject"].add(obj)
            fields_dir = obj_dir / "fields"
            if fields_dir.is_dir():
                for f in sorted(fields_dir.glob("*.field-meta.xml")):
                    members["CustomField"].add(f"{obj}.{f.name[:-len('.field-meta.xml')]}")
            rt_dir = obj_dir / "recordTypes"
            if rt_dir.is_dir():
                for f in sorted(rt_dir.glob("*.recordType-meta.xml")):
                    members["RecordType"].add(f"{obj}.{f.name[:-len('.recordType-meta.xml')]}")

    # CustomObjectTranslation: folder objectTranslations/<Obj>-<lang>/
    ot_dir = source_root / "objectTranslations"
    if ot_dir.is_dir():
        for folder in sorted(p for p in ot_dir.iterdir() if p.is_dir()):
            member = folder.name  # e.g. TI_Fnt_Deal__c-en_US
            obj = member.rsplit("-", 1)[0]
            if only and obj not in only:
                continue
            meta = folder / f"{member}.objectTranslation-meta.xml"
            if meta.exists():
                members["CustomObjectTranslation"].add(member)

    gvs_dir = source_root / "globalValueSetTranslations"
    if gvs_dir.is_dir() and not only:
        for f in sorted(gvs_dir.glob("*.globalValueSetTranslation-meta.xml")):
            members["GlobalValueSetTranslation"].add(
                f.name[: -len(".globalValueSetTranslation-meta.xml")])

    labels = source_root / "labels" / "CustomLabels.labels-meta.xml"
    if labels.exists() and not only:
        members["CustomLabels"].add("CustomLabels")

    tr_dir = source_root / "translations"
    if tr_dir.is_dir() and not only:
        for f in sorted(tr_dir.glob("*.translation-meta.xml")):
            members["Translations"].add(f.name[: -len(".translation-meta.xml")])

    simple = {
        "Layout": ("layouts", ".layout-meta.xml"),
        "FlexiPage": ("flexipages", ".flexipage-meta.xml"),
        "PermissionSet": ("permissionsets", ".permissionset-meta.xml"),
    }
    for mtype, (folder, suffix) in simple.items():
        d = source_root / folder
        if not d.is_dir():
            continue
        # When --only restricts the deploy to specific object(s), non-object
        # metadata must NOT ride along:
        #   * Layout members are "Object-Label" → filter by the object prefix.
        #   * FlexiPage / PermissionSet have no object prefix in their file name
        #     and are deployed by their OWN gated manifests (post-deploy FlexiPage
        #     gate, FLS/permission-set deploy). Including them here would silently
        #     redeploy unrelated pages/permission sets on every object push, so
        #     skip them entirely under --only. (Deploy them explicitly when needed.)
        if only and mtype in ("FlexiPage", "PermissionSet"):
            continue
        for f in sorted(d.glob(f"*{suffix}")):
            name = f.name[: -len(suffix)]
            # Layout members are Object-Label; filter by object when --only given
            if only and mtype == "Layout":
                obj = name.split("-", 1)[0]
                if obj not in only:
                    continue
            members[mtype].add(name)

    result = {k: sorted(v) for k, v in members.items()}
    if only_types:
        result = {k: v for k, v in result.items() if k in only_types}
    return result


def render_package(members: dict[str, list[str]], api_version: str) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Package xmlns="http://soap.sforce.com/2006/04/metadata">']
    for mtype in TYPE_ORDER:
        vals = members.get(mtype)
        if not vals:
            continue
        lines.append("    <types>")
        for m in vals:
            lines.append(f"        <members>{sx.escape(m)}</members>")
        lines.append(f"        <name>{mtype}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    return "\n".join(lines) + "\n"


def render_destructive(deletions: dict[str, list[str]], api_version: str) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Package xmlns="http://soap.sforce.com/2006/04/metadata">']
    for mtype in TYPE_ORDER:
        vals = sorted(set(deletions.get(mtype, [])))
        if not vals:
            continue
        lines.append("    <types>")
        for m in vals:
            lines.append(f"        <members>{sx.escape(m)}</members>")
        lines.append(f"        <name>{mtype}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build package.xml / destructiveChanges.xml")
    ap.add_argument("--source-root", default="force-app/main/default")
    ap.add_argument("--out", default="manifest/package.xml")
    ap.add_argument("--api-version", default="")
    ap.add_argument("--only", default="", help="comma-separated object API names to restrict to")
    ap.add_argument("--types", default="",
                    help="comma-separated metadata types to include (e.g. CustomLabels,Translations)")
    ap.add_argument("--destroy", default="", help="deletions.json path")
    ap.add_argument("--destroy-out", default="manifest/destructiveChanges.xml")
    ap.add_argument("--project-root", default=".")
    args = ap.parse_args()

    proj_root = Path(args.project_root)
    api_version = args.api_version or default_api_version(proj_root)
    source_root = Path(args.source_root)
    only = {o.strip() for o in args.only.split(",") if o.strip()} or None
    only_types = {t.strip() for t in args.types.split(",") if t.strip()} or None

    if not source_root.is_dir():
        print(f"❌ source root '{source_root}' not found — run the generators first.")
        return 1

    members = discover(source_root, only, only_types)
    total = sum(len(v) for v in members.values())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_package(members, api_version), encoding="utf-8")

    print(f"📦 package.xml  ->  {out}   (api {api_version})")
    for mtype in TYPE_ORDER:
        if members.get(mtype):
            print(f"     {mtype:14} {len(members[mtype])}")
    if total == 0:
        print("   ⚠️  no metadata discovered under source root.")

    if args.destroy:
        try:
            deletions = json.loads(Path(args.destroy).read_text())
        except Exception as e:
            print(f"❌ could not read deletions file '{args.destroy}': {e}")
            return 1
        dout = Path(args.destroy_out)
        dout.parent.mkdir(parents=True, exist_ok=True)
        dout.write_text(render_destructive(deletions, api_version), encoding="utf-8")
        d_total = sum(len(v) for v in deletions.values())
        print(f"🗑️  destructiveChanges.xml  ->  {dout}   ({d_total} member(s))")
        print("     NOTE: pair with an empty package.xml for a delete-only deploy,")
        print("           or place alongside package.xml for an additive+destructive deploy.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
