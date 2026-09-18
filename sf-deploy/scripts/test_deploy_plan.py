#!/usr/bin/env python3
"""Self-test: the delta-driven deployment plan, staged generation and manifest.

Run from anywhere:  python3 scripts/test_deploy_plan.py
No org, no sheet, no Salesforce CLI — the target-org snapshot is a fixture, so
these tests prove the pipeline's decisions, not one machine's org state.

The headline case (the PR's acceptance criterion) is
`test_existing_object_one_new_field`: an existing object with ONE newly added
field must produce a package containing only that field and its required new
translation, and `test_second_run_is_an_empty_delta` must then find nothing to
do on a re-run against the same sandbox.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import plan_deploy  # noqa: E402
from build_manifest import split_members  # noqa: E402
from translation_lib import (  # noqa: E402
    CHANGED, EN_FLAG, MISSING, NEW, SCHEMA_MISSING, content_hash,
    save_sync_state,
)

OBJ = "TI_Fnt_Receiving__c"
ORG_A = "00D0000000000A"
ORG_B = "00D0000000000B"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def meta_row(obj=OBJ, tab="受入", en="Receiving", wip=(), delete=()):
    return {"_type": "object_meta", "_SheetName": tab, "Object API Name": obj,
            "Object Label": "受入", "Object Label (EN)": en, EN_FLAG: bool(en),
            "_WipSkipped": list(wip), "_DeleteRequested": list(delete),
            "Name Field Type": "Text", "Name Field Label": "受入番号"}


def field_row(api, label, en=None, obj=OBJ, tab="受入", dtype="Text", **extra):
    r = {"_SheetName": tab, "Object API Name": obj, "Field API Name": api,
         "Field Label": label, "Data Type": dtype, "Length": "255",
         EN_FLAG: en is not None}
    if en is not None:
        r["Field Label (EN)"] = en
    r.update(extra)
    return r


def org_cot(obj: str, lang: str, fields: dict[str, str], name_label="Receiving No") -> str:
    inner = "".join(f"<fields><label>{v}</label><name>{k}</name></fields>"
                    for k, v in sorted(fields.items()))
    return (f"<records><fullName>{obj}-{lang}</fullName>"
            f"<caseValues><plural>false</plural><value>Receiving</value></caseValues>"
            f"<nameFieldLabel>{name_label}</nameFieldLabel>"
            f"<recordTypes><label>Domestic</label><name>Domestic</name></recordTypes>"
            f"{inner}</records>")


def snapshot(*, org_id=ORG_A, alias="SANDBOX_A", exists=True,
             fields=(), translations=None, lang="en_US", state="ok",
             obj=OBJ, note="") -> dict:
    return {
        "target": {"orgId": org_id, "alias": alias, "username": "u@example.com",
                   "instanceUrl": "https://x.my.salesforce.com",
                   "apiVersion": "60.0"},
        "lang": lang,
        "objects": {obj: {"exists": exists}},
        "fields": {obj: sorted(fields)} if exists else {},
        "translations": dict(translations or {}),
        "translationState": state,
        "translationNote": note,
    }


def plan_for(rows, snap, **kw):
    kw.setdefault("sheet_id", "SHEET123")
    kw.setdefault("tabs", "受入")
    return plan_deploy.build_plan(rows, snap, **kw)


# --------------------------------------------------------------------------- #
# object / field delta
# --------------------------------------------------------------------------- #

def test_new_object_packages_object_fields_and_translations():
    rows = [meta_row(),
            field_row("TI_Fnt_ProductName__c", "商品名", "Product name"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(exists=False))

    assert plan["newObjects"] == [OBJ]
    assert plan["newFields"][OBJ] == ["TI_Fnt_ProductName__c", "TI_Fnt_Qty__c"]
    assert plan["manifestMembers"]["CustomObject"] == [OBJ]
    assert plan["manifestMembers"]["CustomField"] == [
        f"{OBJ}.TI_Fnt_ProductName__c", f"{OBJ}.TI_Fnt_Qty__c"]
    assert plan["manifestMembers"]["CustomObjectTranslation"] == [f"{OBJ}-en_US"]
    assert plan["summary"]["skippedFields"] == 0
    print("  ok  new-object-packages-object-fields-and-translations")


def test_existing_object_one_new_field():
    """THE acceptance criterion: only the new field + its new translation."""
    rows = [meta_row(),
            field_row("TI_Fnt_ProductName__c", "商品名", "Product name"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]   # ← the new one
    snap = snapshot(fields=["TI_Fnt_ProductName__c"],
                    translations={OBJ: org_cot(
                        OBJ, "en_US", {"TI_Fnt_ProductName__c": "Product name"})})
    plan = plan_for(rows, snap)

    assert plan["newObjects"] == []                      # object already there
    assert plan["newFields"] == {OBJ: ["TI_Fnt_Qty__c"]}  # ONLY the new field
    assert plan["skippedFields"] == {OBJ: ["TI_Fnt_ProductName__c"]}
    assert plan["manifestMembers"] == {
        "CustomField": [f"{OBJ}.TI_Fnt_Qty__c"],
        "CustomObjectTranslation": [f"{OBJ}-en_US"],
    }, plan["manifestMembers"]
    packaged = [t for t in plan["translations"] if t["package"]]
    assert len(packaged) == 1 and packaged[0]["code"] == NEW
    assert "TI_Fnt_Qty__c" in packaged[0]["key"]
    print("  ok  existing-object-one-new-field")


def test_second_run_is_an_empty_delta():
    """Re-running against the SAME sandbox after the deploy: nothing to do."""
    rows = [meta_row(),
            field_row("TI_Fnt_ProductName__c", "商品名", "Product name"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    # the org now holds both fields AND both translations
    after = snapshot(fields=["TI_Fnt_ProductName__c", "TI_Fnt_Qty__c"],
                     translations={OBJ: org_cot(OBJ, "en_US", {
                         "TI_Fnt_ProductName__c": "Product name",
                         "TI_Fnt_Qty__c": "Quantity"})})
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "sync.json"
        plan = plan_for(rows, after, sync_state=state)

    assert plan["newFields"] == {}
    assert plan["manifestMembers"] == {}
    assert plan["translationPackage"] == []
    assert plan["summary"]["empty"] is True
    assert plan["summary"]["components"] == 0
    print("  ok  second-run-is-an-empty-delta")


def test_existing_field_missing_en_is_reported_not_packaged():
    rows = [meta_row(), field_row("TI_Fnt_ProductName__c", "商品名", en="")]
    snap = snapshot(fields=["TI_Fnt_ProductName__c"],
                    translations={OBJ: org_cot(OBJ, "en_US", {})})
    plan = plan_for(rows, snap)
    codes = {t["code"] for t in plan["translations"]}
    assert MISSING in codes
    assert plan["manifestMembers"] == {}, "a blank EN must not package anything"
    print("  ok  existing-field-missing-en-is-reported-not-packaged")


def test_changed_en_is_reported_not_packaged():
    rows = [meta_row(), field_row("TI_Fnt_ProductName__c", "商品名", "Item name")]
    snap = snapshot(fields=["TI_Fnt_ProductName__c"],
                    translations={OBJ: org_cot(
                        OBJ, "en_US", {"TI_Fnt_ProductName__c": "Product name"})})
    plan = plan_for(rows, snap)
    changed = [t for t in plan["translations"] if t["code"] == CHANGED]
    assert changed and not any(t["package"] for t in changed)
    assert plan["manifestMembers"] == {}
    # ...unless the operator explicitly asks for changed translations
    plan2 = plan_for(rows, snap, new_only=False)
    assert plan2["manifestMembers"]["CustomObjectTranslation"] == [f"{OBJ}-en_US"]
    print("  ok  changed-en-is-reported-not-packaged")


def test_wip_and_isdelete_are_separated():
    rows = [meta_row(wip=["TI_Fnt_Draft__c"], delete=["TI_Fnt_Obsolete__c"]),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(fields=[]))
    obj = plan["objects"][0]

    assert obj["wipSkipped"] == ["TI_Fnt_Draft__c"]
    assert plan["deleteMembers"] == [f"{OBJ}.TI_Fnt_Obsolete__c"]
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]
    for member in plan["manifestMembers"]["CustomField"]:
        assert "Draft" not in member and "Obsolete" not in member
    print("  ok  wip-and-isdelete-are-separated")


def test_standard_fields_are_never_custom_field_members():
    rows = [meta_row(),
            field_row("Name", "受入番号", "Receiving No"),
            field_row("OwnerId", "所有者", "Owner"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(fields=[]))
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]
    assert plan["objects"][0]["standardSkipped"] == ["Name", "OwnerId"]
    print("  ok  standard-fields-are-never-custom-field-members")


def test_attribute_drift_needs_an_explicit_decision():
    rows = [meta_row(), field_row("TI_Fnt_Kind__c", "種別", "Kind", dtype="Picklist")]
    snap = snapshot(fields=["TI_Fnt_Kind__c"],
                    translations={OBJ: org_cot(OBJ, "en_US",
                                               {"TI_Fnt_Kind__c": "Kind"})})
    drift = {OBJ: [{"field": "TI_Fnt_Kind__c",
                    "reason": "type Checkbox (org) vs Picklist (sheet)"}]}

    reported = plan_for(rows, snap, drift=drift)
    assert reported["summary"]["driftedFields"] == 1
    assert reported["manifestMembers"] == {}, "drift is never packaged automatically"

    approved = plan_for(rows, snap, drift=drift,
                        include_drift={f"{OBJ}.TI_Fnt_Kind__c"})
    assert approved["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Kind__c"]
    print("  ok  attribute-drift-needs-an-explicit-decision")


def test_translation_for_unknown_field_is_schema_missing():
    """A translation whose target field exists nowhere is a different defect
    from a blank EN cell — and must never be packaged."""
    rows = [meta_row(), field_row("TI_Fnt_Ghost__c", "幽霊", "Ghost")]
    snap = snapshot(fields=["TI_Fnt_ProductName__c"],
                    translations={OBJ: org_cot(OBJ, "en_US", {})})
    # the field is in neither the org nor the plan: simulate a WIP-parked row
    # by stripping it from the sheet's deployable set
    rows[1]["WIP"] = "TRUE"
    plan = plan_for(rows, snap)
    ghost = [t for t in plan["translations"] if "Ghost" in t["translation"]]
    assert ghost and ghost[0]["code"] == SCHEMA_MISSING, ghost
    assert not any(t["package"] for t in plan["translations"])
    assert plan["summary"]["schemaMissingTranslations"] == 1
    print("  ok  translation-for-unknown-field-is-schema-missing")


def test_untranslated_tab_plans_fields_only():
    rows = [meta_row(en=""), field_row("TI_Fnt_Qty__c", "数量")]  # no EN column
    plan = plan_for(rows, snapshot(exists=False, state="off", lang="off"),
                    lang="en_US")
    assert plan["lang"] == "off"
    assert plan["translations"] == []
    assert "CustomObjectTranslation" not in plan["manifestMembers"]
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]
    print("  ok  untranslated-tab-plans-fields-only")


def test_workbench_unavailable_still_plans_fields():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(exists=False, state="unavailable",
                                   note="Translation Workbench is disabled"))
    assert plan["translationState"] == "unavailable"
    assert plan["translations"] == []
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]
    print("  ok  workbench-unavailable-still-plans-fields")


def test_multiple_sandboxes_are_independent():
    """Sandbox A's recorded hashes must not mark sandbox B "already deployed"."""
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    cot = {OBJ: org_cot(OBJ, "en_US", {"TI_Fnt_Qty__c": "Quantity"})}
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "sync.json"
        a = plan_for(rows, snapshot(org_id=ORG_A, alias="A",
                                    fields=["TI_Fnt_Qty__c"], translations=cot),
                     sync_state=state)
        save_sync_state([t for t in a["translations"]], "A", state, org_id=ORG_A)

        # B is a different sandbox: the field is absent there, so it is new again
        b = plan_for(rows, snapshot(org_id=ORG_B, alias="B", fields=[]),
                     sync_state=state)
        assert b["target"]["orgId"] == ORG_B
        assert b["newFields"] == {OBJ: ["TI_Fnt_Qty__c"]}
        assert b["manifestMembers"]["CustomObjectTranslation"] == [f"{OBJ}-en_US"]
    print("  ok  multiple-sandboxes-are-independent")


def test_plan_carries_immutable_org_id_and_scope():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(exists=False), tabs="受入,出荷")
    assert plan["target"]["orgId"] == ORG_A
    assert plan["target"]["alias"] == "SANDBOX_A"
    assert plan["sheet"] == {"id": "SHEET123", "tabs": ["受入", "出荷"]}
    print("  ok  plan-carries-immutable-org-id-and-scope")


def test_multiple_languages_plan_separate_members():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    for lang in ("en_US", "fr", "zh_CN"):
        plan = plan_for(rows, snapshot(exists=False, lang=lang), lang=lang)
        assert plan["manifestMembers"]["CustomObjectTranslation"] == [f"{OBJ}-{lang}"]
        assert all(t["language"] == lang for t in plan["translations"])
    print("  ok  multiple-languages-plan-separate-members")


# --------------------------------------------------------------------------- #
# staged generation + manifest
# --------------------------------------------------------------------------- #

def _write(tmp: Path, rows: list[dict], plan: dict) -> tuple[Path, Path, Path]:
    rows_path = tmp / "custom_rows.json"       # deliberately NOT temp_updates.json
    plan_path = tmp / "deploy_plan.json"
    rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return rows_path, plan_path, tmp / "staging/force-app/main/default"


def test_staged_generation_honours_custom_input_and_plan():
    rows = [meta_row(),
            field_row("TI_Fnt_ProductName__c", "商品名", "Product name"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(fields=["TI_Fnt_ProductName__c"]))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows_path, plan_path, staged = _write(tmp, rows, plan)
        tracked = tmp / "force-app"

        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_xml.py"),
             "--in", str(rows_path), "--source-root", str(staged),
             "--plan", str(plan_path)], text=True, capture_output=True, cwd=str(tmp))
        assert cp.returncode == 0, cp.stdout + cp.stderr

        fields_dir = staged / "objects" / OBJ / "fields"
        generated = sorted(p.name for p in fields_dir.iterdir())
        assert generated == ["TI_Fnt_Qty__c.field-meta.xml"], generated
        assert not tracked.exists(), "generation must not touch the tracked tree"
    print("  ok  staged-generation-honours-custom-input-and-plan")


def test_manifest_contains_only_planned_members():
    rows = [meta_row(),
            field_row("TI_Fnt_ProductName__c", "商品名", "Product name"),
            field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(fields=["TI_Fnt_ProductName__c"],
                                   translations={OBJ: org_cot(OBJ, "en_US", {})}))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows_path, plan_path, staged = _write(tmp, rows, plan)
        # a STALE artifact from an earlier run, which a directory scan would
        # happily package:
        stale = staged / "objects" / OBJ / "fields"
        stale.mkdir(parents=True)
        (stale / "TI_Fnt_Stale__c.field-meta.xml").write_text("<x/>", encoding="utf-8")

        out = tmp / "manifest/package.xml"
        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_manifest.py"),
             "--plan", str(plan_path), "--project-root", str(tmp),
             "--out", str(out)], text=True, capture_output=True)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        pkg = out.read_text(encoding="utf-8")
        assert f"<members>{OBJ}.TI_Fnt_Qty__c</members>" in pkg
        assert "Stale" not in pkg, "the manifest must come from the plan, not a scan"
        assert "ProductName" not in pkg, "an existing field must not be redeployed"
        members = [e.text for e in ET.fromstring(pkg).iter()
                   if e.tag.endswith("members")]
        assert sorted(members) == sorted(
            [f"{OBJ}.TI_Fnt_Qty__c", f"{OBJ}-en_US"]), members
    print("  ok  manifest-contains-only-planned-members")


def test_empty_plan_yields_empty_manifest():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", "数量", "Quantity")]
    plan = plan_for(rows, snapshot(fields=["TI_Fnt_Qty__c"], translations={
        OBJ: org_cot(OBJ, "en_US", {"TI_Fnt_Qty__c": "Quantity"})}))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _, plan_path, _ = _write(tmp, rows, plan)
        out = tmp / "manifest/package.xml"
        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_manifest.py"),
             "--plan", str(plan_path), "--project-root", str(tmp),
             "--out", str(out)], text=True, capture_output=True)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "<members>" not in out.read_text(encoding="utf-8")
        assert "empty delta" in cp.stdout
    print("  ok  empty-plan-yields-empty-manifest")


def test_package_split_is_deterministic_and_grouped():
    members = {
        "CustomObject": [f"O{i}__c" for i in range(6)],
        "CustomField": [f"O{i}__c.F{j}__c" for i in range(6) for j in range(4)],
        "CustomObjectTranslation": [f"O{i}__c-en_US" for i in range(6)],
    }
    parts = split_members(members, max_components=12)
    assert len(parts) > 1
    assert sum(sum(len(v) for v in p.values()) for p in parts) == 36
    # every object stays with its own fields and translation
    for p in parts:
        objs = {m.split(".")[0] for m in p.get("CustomField", [])}
        for o in objs:
            assert o in p.get("CustomObject", []), (o, p)
            assert f"{o}-en_US" in p.get("CustomObjectTranslation", []), (o, p)
    assert split_members(members, max_components=12) == parts, "must be stable"
    assert split_members(members, max_components=0) == [members]
    print("  ok  package-split-is-deterministic-and-grouped")


# --------------------------------------------------------------------------- #
# scale
# --------------------------------------------------------------------------- #

def test_performance_100_objects_200_fields():
    rows: list[dict] = []
    org_fields: dict[str, list[str]] = {}
    objects: dict[str, dict] = {}
    cot: dict[str, str] = {}
    for o in range(100):
        obj = f"TI_Fnt_Perf{o:03d}__c"
        objects[obj] = {"exists": True}
        rows.append(meta_row(obj=obj, tab=f"tab{o}"))
        have = []
        translated = {}
        for f in range(200):
            api = f"TI_Fnt_F{f:03d}__c"
            rows.append(field_row(api, f"項目{f}", f"Field {f}", obj=obj,
                                  tab=f"tab{o}"))
            if f < 199:                      # one genuinely new field per object
                have.append(api)
                translated[api] = f"Field {f}"
        org_fields[obj] = have
        cot[obj] = org_cot(obj, "en_US", translated, name_label="No")
    snap = snapshot()
    snap["objects"] = objects
    snap["fields"] = org_fields
    snap["translations"] = cot

    t0 = time.time()
    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="x")
    elapsed = time.time() - t0

    assert len(rows) == 100 * 201
    assert plan["summary"]["newFields"] == 100, plan["summary"]
    assert plan["summary"]["skippedFields"] == 100 * 199
    assert len(plan["manifestMembers"]["CustomField"]) == 100
    assert len(plan["manifestMembers"]["CustomObjectTranslation"]) == 100
    assert elapsed < 30, f"plan of 20k rows took {elapsed:.1f}s"
    print(f"  ok  performance-100-objects-200-fields ({elapsed:.1f}s for "
          f"{len(rows)} rows)")


def test_generation_is_linear_in_entries():
    """Indexed grouping: patching a 200-field object stays well under a second."""
    from generate_object_translation import patch_translation
    from translation_lib import KIND_OBJECT_FIELD, make_entry

    entries = [make_entry(kind=KIND_OBJECT_FIELD, component=OBJ, aspect="label",
                          key=f"TI_Fnt_F{i:03d}__c", language="en_US",
                          master="JA", translation=f"Field {i}", source="sheet",
                          extra={"field": f"TI_Fnt_F{i:03d}__c"})
               for i in range(200)]
    rec = ET.fromstring(org_cot(OBJ, "en_US",
                                {f"TI_Fnt_F{i:03d}__c": f"Old {i}" for i in range(200)}))
    t0 = time.time()
    parent, fields = patch_translation(rec, entries)
    elapsed = time.time() - t0
    assert len(fields) == 200
    assert list(fields) == sorted(fields), "field order must be deterministic"
    assert elapsed < 2, f"patch of 200 fields took {elapsed:.2f}s"
    print(f"  ok  generation-is-linear-in-entries ({elapsed:.2f}s)")


# --------------------------------------------------------------------------- #
# one canonical entry point
# --------------------------------------------------------------------------- #

def test_single_canonical_entry_point():
    prep = (SCRIPTS / "prep_deploy.py").read_text(encoding="utf-8")
    for step in ("org_snapshot.py", "plan_deploy.py", "generate_xml.py",
                 "generate_object_translation.py", "build_manifest.py",
                 "deploy.py", "verify_deploy.py"):
        assert step in prep, f"the canonical entry point must run {step}"
    assert "--plan" in prep and "--snapshot" in prep
    assert "CANONICAL DEPLOY ENTRY POINT" in prep

    run = (SCRIPTS / "run.py").read_text(encoding="utf-8")
    assert "prep_deploy.py" in run, "run.py must delegate, not fork the pipeline"
    assert "generate_xml.py" not in run and "build_manifest.py" not in run
    print("  ok  single-canonical-entry-point")


def test_libraries_raise_instead_of_exiting():
    banned = ("sys.exit(", "SystemExit(")
    for name in ("translation_lib.py", "org_snapshot.py"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        body, _, cli = src.partition("def main(")
        for token in banned:
            assert token not in body, f"{name} exits from a library function"
    print("  ok  libraries-raise-instead-of-exiting")


def test_soql_inputs_are_validated():
    from translation_lib import InvalidApiName, chunks, soql_in_list, soql_name
    assert soql_name("TI_Fnt_Qty__c") == "TI_Fnt_Qty__c"
    for bad in ("Obj' OR Name!='", 'Obj"', "Obj;DROP", "1Obj__c", "", "a" * 90):
        try:
            soql_name(bad)
            raise AssertionError(f"accepted {bad!r}")
        except InvalidApiName:
            pass
    assert soql_in_list(["A__c", "B__c"]) == "'A__c','B__c'"
    assert chunks(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
    assert chunks([], 10) == []
    print("  ok  soql-inputs-are-validated")


def test_partial_metadata_response_is_an_error():
    """A readMetadata answering with neither records nor a fault is a fault."""
    import translation_lib as tl
    from translation_lib import MetadataApiError

    real = tl.urllib.request.urlopen

    class Resp:
        def read(self):
            return b'<?xml version="1.0"?><Envelope><Body><readMetadataResponse/>'\
                   b'</Body></Envelope>'

    tl.urllib.request.urlopen = lambda *a, **k: Resp()
    try:
        tl.read_metadata("CustomObjectTranslation", [f"{OBJ}-en_US"],
                         "token", "https://y", "60.0")
        raise AssertionError("expected MetadataApiError")
    except MetadataApiError as e:
        assert "no <records>" in str(e), e
    finally:
        tl.urllib.request.urlopen = real
    print("  ok  partial-metadata-response-is-an-error")


def test_namespace_aware_parsing_keeps_text_intact():
    from translation_lib import parse_soap
    xml = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
           'xmlns="http://soap.sforce.com/2006/04/metadata"><soapenv:Body>'
           '<records><fullName>X-en_US</fullName>'
           '<fields><label>a &lt; b xmlns="trap" &lt;met:trap&gt;</label>'
           '<name>F__c</name></fields></records>'
           '</soapenv:Body></soapenv:Envelope>')
    root = parse_soap(xml)
    label = root.find(".//records/fields/label")
    assert label is not None
    assert label.text == 'a < b xmlns="trap" <met:trap>', label.text
    print("  ok  namespace-aware-parsing-keeps-text-intact")


def main() -> int:
    print("test_deploy_plan")
    for fn in (
        test_new_object_packages_object_fields_and_translations,
        test_existing_object_one_new_field,
        test_second_run_is_an_empty_delta,
        test_existing_field_missing_en_is_reported_not_packaged,
        test_changed_en_is_reported_not_packaged,
        test_wip_and_isdelete_are_separated,
        test_standard_fields_are_never_custom_field_members,
        test_attribute_drift_needs_an_explicit_decision,
        test_translation_for_unknown_field_is_schema_missing,
        test_untranslated_tab_plans_fields_only,
        test_workbench_unavailable_still_plans_fields,
        test_multiple_sandboxes_are_independent,
        test_plan_carries_immutable_org_id_and_scope,
        test_multiple_languages_plan_separate_members,
        test_staged_generation_honours_custom_input_and_plan,
        test_manifest_contains_only_planned_members,
        test_empty_plan_yields_empty_manifest,
        test_package_split_is_deterministic_and_grouped,
        test_performance_100_objects_200_fields,
        test_generation_is_linear_in_entries,
        test_single_canonical_entry_point,
        test_libraries_raise_instead_of_exiting,
        test_soql_inputs_are_validated,
        test_partial_metadata_response_is_an_error,
        test_namespace_aware_parsing_keeps_text_intact,
    ):
        fn()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
