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
  - CustomObjectTranslation  objectTranslations/<Obj>-<lang>/  -> member <Obj>-<lang>
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
    "CustomObjectTranslation",
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


def discover(source_root: Path, only: set[str] | None) -> dict[str, list[str]]:
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

    return {k: sorted(v) for k, v in members.items()}


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


def object_of(mtype: str, member: str) -> str:
    """The object a member belongs to, so splits never separate an object from
    its own fields and translations."""
    if mtype == "CustomField" or mtype == "RecordType":
        return member.split(".", 1)[0]
    if mtype == "CustomObjectTranslation":
        return member.rsplit("-", 1)[0]
    if mtype == "Layout":
        return member.split("-", 1)[0]
    return member


def split_object_group(group: dict[str, list[str]], max_components: int
                       ) -> list[dict[str, list[str]]]:
    """Packages for ONE object, each at most ``max_components``.

    ``CustomObject`` is always the first member of the first package so a
    new object's definition precedes any field part that depends on it.
    Oversized groups are split rather than emitted as one illegal package.
    """
    if max_components <= 0:
        return [group]
    sequence: list[tuple[str, str]] = []
    seen: set[str] = set()
    for mtype in TYPE_ORDER:
        for m in group.get(mtype) or []:
            sequence.append((mtype, m))
        seen.add(mtype)
    for mtype, vals in group.items():
        if mtype in seen:
            continue
        for m in vals:
            sequence.append((mtype, m))
    if not sequence:
        return []

    packages: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    count = 0
    for mtype, member in sequence:
        if count >= max_components:
            packages.append(current)
            current, count = {}, 0
        current.setdefault(mtype, []).append(member)
        count += 1
    if current:
        packages.append(current)
    return packages


def split_members(members: dict[str, list[str]], max_components: int
                  ) -> list[dict[str, list[str]]]:
    """Split a member set into deterministic packages under the component cap.

    Members of the same object stay together when they fit. If one object's
    group exceeds the cap it is split (CustomObject first, then fields, then
    translations) rather than producing an oversized package.
    """
    total = sum(len(v) for v in members.values())
    if max_components <= 0 or total <= max_components:
        return [members] if total else []

    by_object: dict[str, dict[str, list[str]]] = {}
    for mtype in TYPE_ORDER:
        for m in members.get(mtype, []):
            by_object.setdefault(object_of(mtype, m), {}).setdefault(mtype, []).append(m)

    packages: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    count = 0

    def flush() -> None:
        nonlocal current, count
        if current:
            packages.append(current)
        current, count = {}, 0

    for obj in sorted(by_object):
        for chunk in split_object_group(by_object[obj], max_components):
            size = sum(len(v) for v in chunk.values())
            if size > max_components:
                raise ValueError(
                    f"object {obj} produced a {size}-component chunk above "
                    f"max_components={max_components}; split_object_group must "
                    "keep every package at or under the cap")
            if count and count + size > max_components:
                flush()
            for mtype, vals in chunk.items():
                current.setdefault(mtype, []).extend(vals)
            count += size
    flush()
    return [{k: sorted(v) for k, v in sorted(
        p.items(),
        key=lambda kv: TYPE_ORDER.index(kv[0]) if kv[0] in TYPE_ORDER else 99)}
            for p in packages]


def plan_parts(members: dict[str, list[str]], max_components: int,
               stem: str = "package", suffix: str = ".xml") -> list[dict]:
    """The manifest INDEX: every package file this member set becomes.

    The planner stores this in the plan so the deploy command knows there are
    N packages and deploys every one of them in order. Deploying only
    `package.xml` when the plan split into parts silently drops components.
    """
    packages = split_members(members, max_components)
    if not packages:
        return []
    if len(packages) == 1:
        return [{"file": f"{stem}{suffix}", "members": packages[0],
                 "components": sum(len(v) for v in packages[0].values())}]
    return [{"file": f"{stem}.part{i}{suffix}", "members": part,
             "components": sum(len(v) for v in part.values())}
            for i, part in enumerate(packages, 1)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build package.xml / destructiveChanges.xml")
    ap.add_argument("--plan", default="",
                    help="deploy_plan.json — take the members from the PLAN "
                         "instead of scanning the source tree (authoritative)")
    ap.add_argument("--max-components", type=int, default=9000,
                    help="component cap per package (Metadata API allows 10000); "
                         "larger plans split deterministically into package.partN.xml")
    ap.add_argument("--source-root", default="force-app/main/default")
    ap.add_argument("--out", default="manifest/package.xml")
    ap.add_argument("--api-version", default="")
    ap.add_argument("--only", default="", help="comma-separated object API names to restrict to")
    ap.add_argument("--destroy", default="", help="deletions.json path")
    ap.add_argument("--destroy-out", default="manifest/destructiveChanges.xml")
    ap.add_argument("--project-root", default=".")
    args = ap.parse_args(argv)

    proj_root = Path(args.project_root)
    api_version = args.api_version or default_api_version(proj_root)
    source_root = Path(args.source_root)
    only = {o.strip() for o in args.only.split(",") if o.strip()} or None

    parts: list[dict] | None = None
    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        members = {k: sorted(v) for k, v in (plan.get("manifestMembers") or {}).items()}
        # The plan already decided how the members split; honour that index
        # verbatim so the files on disk match what the deploy will iterate.
        parts = plan.get("manifestParts") or None
        source = f"plan {args.plan}"
    else:
        if not source_root.is_dir():
            print(f"❌ source root '{source_root}' not found — run the generators first.")
            return 1
        members = discover(source_root, only)
        source = f"source scan {source_root}"

    total = sum(len(v) for v in members.values())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if parts is None:
        parts = plan_parts(members, args.max_components, out.stem, out.suffix)
    written = []
    for part in parts:
        p = out.with_name(Path(part["file"]).name)
        p.write_text(render_package(part["members"], api_version), encoding="utf-8")
        written.append(p)
    if not parts:                       # empty delta: still emit a valid manifest
        out.write_text(render_package({}, api_version), encoding="utf-8")
        written = [out]
    # Stale parts from a previous, larger run would otherwise be deployed again.
    keep = {p.name for p in written}
    for old in out.parent.glob(f"{out.stem}.part*{out.suffix}"):
        if old.name not in keep:
            old.unlink()
    packages = [p["members"] for p in parts]

    print(f"📦 package.xml  ->  {', '.join(str(p) for p in written)}   "
          f"(api {api_version}, from {source})")
    for mtype in TYPE_ORDER:
        if members.get(mtype):
            print(f"     {mtype:26} {len(members[mtype])}")
    if len(packages) > 1:
        print(f"   ⚠️  {total} components exceed --max-components "
              f"{args.max_components}: split into {len(packages)} package(s); "
              f"deploy them in order.")
    if total == 0:
        print("   ⚠️  no members — nothing to deploy (empty delta)."
              if args.plan else "   ⚠️  no metadata discovered under source root.")

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
