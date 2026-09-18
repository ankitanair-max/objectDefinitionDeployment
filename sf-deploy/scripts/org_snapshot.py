#!/usr/bin/env python3
"""
org_snapshot.py — take ONE bulk snapshot of the target org.

Everything downstream (delta, translation classification, generation, manifest,
verification) reads this file instead of querying the org again, so a batch of
N objects costs a bounded, predictable number of API calls:

  1 × `sf org display`                      (identity + session)
  ceil(N/200) × EntityDefinition SOQL       (object existence, batched IN)
  ceil(N/50)  × Tooling CustomField SOQL    (existing field names, batched IN)
  ceil(N/10)  × readMetadata CustomObject   (attribute drift, computed locally)
  ceil(N/10)  × readMetadata COT            (translation snapshot, if translated)

Every duplicate read is gone: translations were previously retrieved by the
drift step and AGAIN per object by the generator, and attribute drift spawned
attr_drift.py — one authentication plus one Metadata call — for every existing
object. Both now read this snapshot.

Reads are independent, so the SOQL batches run with bounded concurrency; the
results are re-sorted afterwards, so the snapshot (and every plan built from
it) is deterministic regardless of completion order.

Usage:
  python scripts/org_snapshot.py --org <ORG> --objects A__c,B__c \
      [--lang en_US | --lang off] [--out .build/org_snapshot.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from translation_lib import (  # noqa: E402
    BUILD_DIR, DEFAULT_LANG, InvalidApiName, MetadataApiError, OrgAuthError,
    TranslationUnavailable, chunks, norm, org_auth, read_metadata,
    read_object_translations, soql_in_list, soql_name,
)

DEFAULT_OUT = BUILD_DIR / "org_snapshot.json"
ENTITY_BATCH = 200   # objects per EntityDefinition query
FIELD_BATCH = 50     # objects per Tooling CustomField query
MAX_READERS = 4      # bounded concurrency for independent reads


class OrgQueryError(RuntimeError):
    """A SOQL/Tooling query against the target org failed."""


def soql(query: str, org: str, tooling: bool = False) -> list[dict]:
    cmd = ["sf", "data", "query", "--query", query, "--target-org", org, "--json"]
    if tooling:
        cmd.append("--use-tooling-api")
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True)
    except FileNotFoundError:
        raise OrgQueryError("the `sf` CLI is not on PATH")
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        raise OrgQueryError(f"unparseable query response: "
                            f"{(cp.stdout or cp.stderr or '')[:300]}")
    if cp.returncode != 0 or data.get("status") not in (0, None):
        raise OrgQueryError(norm(data.get("message")) or norm(cp.stderr)[:300]
                            or "query failed")
    return data.get("result", {}).get("records", []) or []


def _map(fn, batches: list) -> list:
    """Run independent read batches with bounded concurrency, order preserved."""
    if len(batches) <= 1:
        return [fn(b) for b in batches]
    with ThreadPoolExecutor(max_workers=MAX_READERS) as pool:
        return list(pool.map(fn, batches))


def existing_objects(objs: list[str], org: str) -> set[str]:
    """Live EntityDefinition existence check, batched."""
    def q(batch: list[str]) -> list[dict]:
        return soql("SELECT QualifiedApiName FROM EntityDefinition "
                    f"WHERE QualifiedApiName IN ({soql_in_list(batch)})", org)
    found: set[str] = set()
    for rows in _map(q, chunks(sorted(objs), ENTITY_BATCH)):
        found.update(norm(r.get("QualifiedApiName")) for r in rows)
    return {o for o in found if o}


def existing_fields(objs: list[str], org: str) -> dict[str, list[str]]:
    """Tooling `CustomField` field names per object — FLS-independent.

    Never `sobject describe` / `FieldDefinition`: both are FLS-gated and would
    report a deployed-but-unpermissioned field as missing, so the delta would
    try to create it again.
    """
    def q(batch: list[str]) -> list[dict]:
        return soql("SELECT DeveloperName, EntityDefinition.QualifiedApiName "
                    "FROM CustomField WHERE EntityDefinition.QualifiedApiName "
                    f"IN ({soql_in_list(batch)})", org, tooling=True)

    out: dict[str, set[str]] = {o: set() for o in objs}
    for rows in _map(q, chunks(sorted(objs), FIELD_BATCH)):
        for r in rows:
            ent = (r.get("EntityDefinition") or {}).get("QualifiedApiName")
            dev = norm(r.get("DeveloperName"))
            if ent and dev:
                # Tooling DeveloperName omits the __c suffix — re-add it.
                out.setdefault(norm(ent), set()).add(f"{dev}__c")
    return {o: sorted(v) for o, v in sorted(out.items())}


def object_snapshot(objs: list[str], auth: dict) -> dict[str, str]:
    """One bulk readMetadata(CustomObject) for every existing target object.

    This is what makes attribute drift a LOCAL computation: the planner parses
    these records and compares them to the sheet, instead of the orchestrator
    spawning attr_drift.py (one authentication + one Metadata call) per object.
    """
    snap: dict[str, str] = {}
    records = read_metadata("CustomObject", sorted(objs), auth["accessToken"],
                            auth["instanceUrl"].rstrip("/"), auth["apiVersion"])
    for rec in records:
        full = norm(rec.findtext("fullName"))
        if full:
            snap[full] = ET.tostring(rec, encoding="unicode")
    return snap


def translation_snapshot(objs: list[str], lang: str, auth: dict) -> dict[str, str]:
    """One bulk readMetadata of every object's CustomObjectTranslation.

    The raw record XML is kept so generation can patch the org's own element
    tree later without a second round-trip (and without losing any node).
    """
    snap: dict[str, str] = {}
    for rec in read_object_translations(sorted(objs), lang, auth):
        full = norm(rec.findtext("fullName"))
        obj = full.rsplit("-", 1)[0] if full else ""
        if obj:
            snap[obj] = ET.tostring(rec, encoding="unicode")
    return snap


def take(objs: list[str], org: str, lang: str = DEFAULT_LANG,
         on_unavailable: str = "error",
         translation_objects: list[str] | None = None) -> dict:
    """Build the whole snapshot. Raises typed errors for the caller to report.

    CustomObjectTranslation is read only for ``translation_objects`` (tabs that
    actually have ``Field Label (EN)``). ``lang='off'`` skips the Workbench
    entirely so a sandbox without it can still take a field snapshot.
    """
    objs = sorted({soql_name(o) for o in objs if norm(o)})
    auth = org_auth(org)
    present = existing_objects(objs, org) if objs else set()
    fields = existing_fields(sorted(present), org) if present else {}
    # CustomObject metadata for the existing objects: attribute drift (custom
    # fields AND the standard Name field) is computed from this locally.
    object_meta = object_snapshot(sorted(present), auth) if present else {}

    translations: dict[str, str] = {}
    translation_state = "off" if lang in ("", "off") else "ok"
    translation_note = ""
    cot_targets: list[str] = []
    if translation_state == "ok":
        if translation_objects is None:
            cot_targets = sorted(present)
        else:
            cot_targets = sorted({soql_name(o) for o in translation_objects
                                  if soql_name(o) in present})
    if cot_targets:
        try:
            translations = translation_snapshot(cot_targets, lang, auth)
        except TranslationUnavailable as e:
            if on_unavailable == "error":
                raise
            translation_state, translation_note = "unavailable", str(e)
        except MetadataApiError as e:
            if on_unavailable == "error":
                raise
            translation_state, translation_note = "error", str(e)

    return {
        "target": {
            # the org Id is the immutable identity; the alias is only what the
            # operator typed and may point at a different sandbox elsewhere.
            "orgId": auth.get("orgId", ""),
            "alias": org,
            "username": auth.get("username", ""),
            "instanceUrl": auth.get("instanceUrl", ""),
            "apiVersion": auth.get("apiVersion", ""),
        },
        "lang": lang,
        "objects": {o: {"exists": o in present} for o in objs},
        "fields": fields,
        "objectMeta": object_meta,
        "translations": translations,
        "translationState": translation_state,
        "translationNote": translation_note,
    }


def load(path: str | Path = DEFAULT_OUT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def org_schema(snapshot: dict) -> dict[str, set[str]]:
    return {o: set(f) for o, f in (snapshot.get("fields") or {}).items()}


def translation_record(snapshot: dict, obj: str) -> ET.Element | None:
    xml = (snapshot.get("translations") or {}).get(obj)
    return ET.fromstring(xml) if xml else None


def object_record(snapshot: dict, obj: str) -> ET.Element | None:
    """The object's CustomObject metadata from the bulk snapshot (no org call)."""
    xml = (snapshot.get("objectMeta") or {}).get(obj)
    return ET.fromstring(xml) if xml else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One bulk snapshot of the target org")
    ap.add_argument("--org", required=True)
    ap.add_argument("--objects", required=True, help="comma-separated object API names")
    ap.add_argument("--lang", default=DEFAULT_LANG, help="'off' to skip translations")
    ap.add_argument("--translation-objects", default=None,
                    help="comma-separated objects whose CustomObjectTranslation "
                         "to read. Omit to read every existing target object. "
                         "Pass empty (or --lang off) to skip COT entirely.")
    ap.add_argument("--on-unavailable", choices=["error", "skip"], default="error")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    objs = [o.strip() for o in args.objects.split(",") if o.strip()]
    if args.translation_objects is None:
        trans_objs = None
    else:
        trans_objs = [o.strip() for o in args.translation_objects.split(",") if o.strip()]
    try:
        snap = take(objs, args.org, args.lang, args.on_unavailable, trans_objs)
    except (OrgAuthError, OrgQueryError, InvalidApiName) as e:
        print(f"❌ {e}")
        return 1
    except TranslationUnavailable as e:
        print(f"❌ {e}")
        return 3
    except MetadataApiError as e:
        print(f"❌ Metadata API: {e}")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    new = [o for o, v in snap["objects"].items() if not v["exists"]]
    have = [o for o, v in snap["objects"].items() if v["exists"]]
    print(f"org snapshot  org={snap['target']['alias']} "
          f"({snap['target']['orgId'] or 'id unknown'})  lang={snap['lang']}")
    print(f"  objects: {len(have)} existing, {len(new)} new")
    for o in have:
        print(f"    EXISTS {o}: {len(snap['fields'].get(o, []))} custom field(s)"
              f"{', translation present' if o in snap['translations'] else ''}")
    for o in new:
        print(f"    NEW    {o}")
    if snap["translationState"] != "ok":
        print(f"  translations: {snap['translationState']} {snap['translationNote'][:200]}")
    print(f"saved → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
