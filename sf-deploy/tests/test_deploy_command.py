#!/usr/bin/env python3
"""Behavioural tests for the public `deploy.py` command.

These EXERCISE the command's stages (package iteration, translation
verification, destructive routing) with the org calls stubbed out, rather than
inspecting source strings. Everything here is org-free.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import attr_drift  # noqa: E402
import deploy as deploy_cmd  # noqa: E402
import plan_deploy  # noqa: E402
import verify_deploy  # noqa: E402
from build_manifest import plan_parts  # noqa: E402
from translation_lib import EN_FLAG, make_entry  # noqa: E402

OBJ = "TI_Fnt_Receiving__c"
OBJ2 = "TI_Fnt_Deal__c"


def args_ns(**kw) -> argparse.Namespace:
    base = dict(org="SANDBOX_A", test_level="NoTestRun", sf_home="",
                xdg_data_home="", google_home="", deletes=False,
                sheet_id="SHEET123", max_components=9000)
    base.update(kw)
    return argparse.Namespace(**base)


def make_parts(n: int, per: int = 2) -> list[dict]:
    members = {"CustomField": [f"TI_Fnt_O{i}__c.F{j}__c"
                               for i in range(n) for j in range(per)]}
    parts = plan_parts(members, per)          # one object's group per package
    return [{**p, "path": Path(f"/tmp/{p['file']}")} for p in parts]


@pytest.fixture
def real_deploy_log(tmp_path, monkeypatch):
    """A log that looks like a genuine `deploy start`, as the command demands."""
    log = tmp_path / "last_deploy.log"
    log.write_text("# command: sf project deploy start --manifest package.xml\n",
                   encoding="utf-8")
    monkeypatch.setattr(deploy_cmd, "LAST_DEPLOY_LOG", log)
    return log


# --------------------------------------------------------------------------- #
# 1. every package part is deployed, in order
# --------------------------------------------------------------------------- #

def test_every_package_part_is_deployed_in_order(monkeypatch, real_deploy_log):
    parts = make_parts(4)
    assert len(parts) == 4, "fixture must actually split"

    deployed: list[str] = []
    verified: list[str] = []
    monkeypatch.setattr(deploy_cmd.sf_deployer, "main",
                        lambda argv: deployed.append(argv[argv.index("--package") + 1]) or 0)
    monkeypatch.setattr(deploy_cmd.verify_deploy, "main",
                        lambda argv: verified.append(argv[argv.index("--objects") + 1]) or 0)

    deploy_cmd.deploy_packages(args_ns(), parts, start=True)

    assert [Path(p).name for p in deployed] == [p["file"] for p in parts]
    assert len(verified) == len(parts), "each part is verified before the next"


def test_check_only_also_covers_every_part(monkeypatch, real_deploy_log):
    parts = make_parts(3)
    seen: list[list[str]] = []
    monkeypatch.setattr(deploy_cmd.sf_deployer, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(deploy_cmd.verify_deploy, "main",
                        lambda argv: pytest.fail("check-only must not verify"))

    deploy_cmd.deploy_packages(args_ns(), parts, start=False)

    assert len(seen) == 3
    assert all("--start" not in argv for argv in seen), "check-only never writes"


def test_a_failed_part_stops_the_remaining_parts(monkeypatch, real_deploy_log):
    parts = make_parts(3)
    calls: list[str] = []

    def flaky(argv):
        pkg = argv[argv.index("--package") + 1]
        calls.append(pkg)
        return 1 if "part2" in pkg else 0

    monkeypatch.setattr(deploy_cmd.sf_deployer, "main", flaky)
    monkeypatch.setattr(deploy_cmd.verify_deploy, "main", lambda argv: 0)

    with pytest.raises(deploy_cmd.DeployError) as e:
        deploy_cmd.deploy_packages(args_ns(), parts, start=True)
    assert "part2" in str(e.value) and "NOT deployed" in str(e.value)
    assert len(calls) == 2, "part 3 must not be attempted after part 2 failed"


def test_a_part_that_did_not_land_stops_the_run(monkeypatch, real_deploy_log):
    parts = make_parts(3)
    monkeypatch.setattr(deploy_cmd.sf_deployer, "main", lambda argv: 0)
    monkeypatch.setattr(deploy_cmd.verify_deploy, "main", lambda argv: 1)

    with pytest.raises(deploy_cmd.DeployError) as e:
        deploy_cmd.deploy_packages(args_ns(), parts, start=True)
    assert "did NOT fully land" in str(e.value)


def test_plan_publishes_the_manifest_index():
    objs = [f"TI_Fnt_O{i}__c" for i in range(5)]
    rows = []
    for o in objs:
        rows.append({"_type": "object_meta", "_SheetName": "t",
                     "Object API Name": o, EN_FLAG: False})
        rows.append({"_SheetName": "t", "Object API Name": o,
                     "Field API Name": "TI_Fnt_F__c", "Field Label": "f",
                     "Data Type": "Text", "Length": "255", EN_FLAG: False})
    snap = {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": {o: {"exists": False} for o in objs}, "fields": {},
            "translations": {}, "translationState": "off", "translationNote": ""}

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off",
                                  max_components=2)
    parts = plan["manifestParts"]
    assert len(parts) > 1 and plan["summary"]["packages"] == len(parts)
    assert [p["file"] for p in parts] == [f"package.part{i}.xml"
                                          for i in range(1, len(parts) + 1)]
    # every planned member appears in exactly one part
    flat = [m for p in parts for vals in p["members"].values() for m in vals]
    assert sorted(flat) == sorted(m for v in plan["manifestMembers"].values() for m in v)
    assert len(flat) == len(set(flat))


# --------------------------------------------------------------------------- #
# 2. standard Name drift is a CustomObject, not a CustomField
# --------------------------------------------------------------------------- #

def org_object_xml(*, name_type="Text", display_format="",
                   enable_history="false", sharing="ReadWrite") -> ET.Element:
    df = f"<displayFormat>{display_format}</displayFormat>" if display_format else ""
    return ET.fromstring(
        f"<records><fullName>{OBJ}</fullName>"
        f"<enableHistory>{enable_history}</enableHistory>"
        f"<sharingModel>{sharing}</sharingModel>"
        f"<nameField><type>{name_type}</type>{df}<label>No</label></nameField>"
        f"</records>")


def test_standard_name_drift_is_classified_as_custom_object():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             "Name Field Type": "Autonumber",
             "Name Field Display Format": "RCV-{0000000}", EN_FLAG: False}]
    org = attr_drift.parse_org_object(org_object_xml(name_type="Text"))
    drift, _insync, _w = attr_drift.compute_drift(OBJ, rows, org)

    name = next(d for d in drift if d["field"] == "Name")
    assert name["component"] == "CustomObject", \
        "the standard Name field lives on the CustomObject, not on a CustomField"

    snap = {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": {OBJ: {"exists": True}}, "fields": {OBJ: []},
            "objectMeta": {}, "translations": {}, "translationState": "off",
            "translationNote": ""}
    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off",
                                  drift={OBJ: drift},
                                  include_drift={f"{OBJ}.Name"})
    assert plan["manifestMembers"].get("CustomObject") == [OBJ]
    assert f"{OBJ}.Name" not in plan["manifestMembers"].get("CustomField", []), \
        "Obj__c.Name is not a valid CustomField member"
    assert plan["objectUpdates"][OBJ], "the plan must say WHY the object ships"


def test_custom_field_drift_stays_a_custom_field():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Qty__c", "Field Label": "数量",
             "Data Type": "Number", "Precision": "18", "Scale": "0", EN_FLAG: False}]
    snap = {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": {OBJ: {"exists": True}}, "fields": {OBJ: ["TI_Fnt_Qty__c"]},
            "objectMeta": {}, "translations": {}, "translationState": "off",
            "translationNote": ""}
    drift = [{"field": "TI_Fnt_Qty__c", "sheet": "Number", "org": "Text",
              "reason": "type: sheet=Number org=Text", "component": "CustomField"}]

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off",
                                  drift={OBJ: drift},
                                  include_drift={f"{OBJ}.TI_Fnt_Qty__c"})
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]
    assert "CustomObject" not in plan["manifestMembers"]


# --------------------------------------------------------------------------- #
# 3. object-level dependencies of a new field on an EXISTING object
# --------------------------------------------------------------------------- #

def existing_snapshot(obj_xml: ET.Element, fields=()) -> dict:
    return {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": {OBJ: {"exists": True}}, "fields": {OBJ: list(fields)},
            "objectMeta": {OBJ: ET.tostring(obj_xml, encoding="unicode")},
            "translations": {}, "translationState": "off", "translationNote": ""}


def test_history_tracked_new_field_pulls_in_the_object():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Qty__c", "Field Label": "数量",
             "Data Type": "Text", "Length": "255", "Track History": "TRUE",
             EN_FLAG: False}]
    snap = existing_snapshot(org_object_xml(enable_history="false"))

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")
    assert plan["manifestMembers"]["CustomObject"] == [OBJ]
    assert "enableHistory" in " ".join(plan["objectUpdates"][OBJ])
    # …and still only the new field, never the object's existing ones
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]


def test_master_detail_new_field_pulls_in_the_object():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Parent__c", "Field Label": "親",
             "Data Type": "MasterDetail", "Type Specific Value": "TI_Fnt_Deal__c",
             EN_FLAG: False}]
    snap = existing_snapshot(org_object_xml(sharing="ReadWrite"))

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")
    assert plan["manifestMembers"]["CustomObject"] == [OBJ]
    assert "ControlledByParent" in " ".join(plan["objectUpdates"][OBJ])


def test_no_object_level_change_means_no_object_member():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Qty__c", "Field Label": "数量",
             "Data Type": "Text", "Length": "255", EN_FLAG: False}]
    snap = existing_snapshot(org_object_xml())

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")
    assert "CustomObject" not in plan["manifestMembers"]
    assert plan["objectUpdates"] == {}


# --------------------------------------------------------------------------- #
# 4. translations are verified live before the sync state is written
# --------------------------------------------------------------------------- #

def cot_record(obj: str, lang: str, fields: dict[str, str]) -> ET.Element:
    inner = "".join(f"<fields><label>{v}</label><name>{k}</name></fields>"
                    for k, v in sorted(fields.items()))
    return ET.fromstring(f"<records><fullName>{obj}-{lang}</fullName>{inner}</records>")


def translated_plan() -> dict:
    e = make_entry(kind="ObjectField", component=OBJ, aspect="label",
                   key="TI_Fnt_Qty__c", language="en_US", master="数量",
                   translation="Quantity")
    e["package"] = True
    return {"target": {"orgId": "00D1", "alias": "A"}, "lang": "en_US",
            "objects": [{"object": OBJ}], "translations": [e],
            "translationPackage": [e["id"]], "newFields": {OBJ: []}}


def test_translation_verification_confirms_the_live_value(monkeypatch):
    plan = translated_plan()
    monkeypatch.setattr(verify_deploy, "org_auth", lambda org: {"accessToken": "t"})
    monkeypatch.setattr(verify_deploy, "read_object_translations",
                        lambda objs, lang, auth: [cot_record(OBJ, "en_US",
                                                             {"TI_Fnt_Qty__c": "Quantity"})])
    assert verify_deploy.verify_translations(plan, "SANDBOX_A") == []


def test_translation_missing_in_the_org_fails_verification(monkeypatch):
    plan = translated_plan()
    monkeypatch.setattr(verify_deploy, "org_auth", lambda org: {"accessToken": "t"})
    monkeypatch.setattr(verify_deploy, "read_object_translations",
                        lambda objs, lang, auth: [cot_record(OBJ, "en_US", {})])
    failures = verify_deploy.verify_translations(plan, "SANDBOX_A")
    assert len(failures) == 1 and "not present" in failures[0]


def test_translation_with_a_different_value_fails_verification(monkeypatch):
    plan = translated_plan()
    monkeypatch.setattr(verify_deploy, "org_auth", lambda org: {"accessToken": "t"})
    monkeypatch.setattr(verify_deploy, "read_object_translations",
                        lambda objs, lang, auth: [cot_record(OBJ, "en_US",
                                                             {"TI_Fnt_Qty__c": "Qty"})])
    failures = verify_deploy.verify_translations(plan, "SANDBOX_A")
    assert len(failures) == 1 and "Qty" in failures[0]


def test_a_translation_only_deploy_is_not_verified_by_fields_alone(monkeypatch, capsys):
    """No new fields ⇒ the object/field check is vacuous; translations decide."""
    plan = translated_plan()
    plan_path = Path(deploy_cmd.PLAN)
    monkeypatch.setattr(verify_deploy, "org_has_object", lambda obj, org: True)
    monkeypatch.setattr(verify_deploy, "org_fields", lambda obj, org: set())
    monkeypatch.setattr(verify_deploy, "verify_translations",
                        lambda p, org: ["TI_Fnt_Qty__c: not present"])
    tmp = plan_path.parent / "_test_plan.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(plan), encoding="utf-8")
    try:
        rc = verify_deploy.main(["--target-org", "A", "--plan", str(tmp),
                                 "--objects", OBJ])
    finally:
        tmp.unlink(missing_ok=True)
    assert rc == 1, "an unverified translation must fail the whole verification"
    assert "translations" in capsys.readouterr().out


def test_sync_state_is_written_only_for_a_verified_org(tmp_path, monkeypatch):
    plan = translated_plan()
    state = tmp_path / "translation_sync_state.json"
    monkeypatch.setattr(deploy_cmd, "SYNC_STATE", state)
    deploy_cmd.persist_sync_state(plan)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert list(saved["orgs"]) == ["00D1"], "state is keyed by the immutable org Id"

    # no org Id (verification never identified the org) ⇒ nothing recorded
    state.unlink()
    plan["target"]["orgId"] = ""
    deploy_cmd.persist_sync_state(plan)
    assert not state.exists()


# --------------------------------------------------------------------------- #
# 5. one snapshot: drift is computed locally, never per object over the wire
# --------------------------------------------------------------------------- #

def test_attribute_drift_is_computed_from_the_snapshot(monkeypatch):
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             "Name Field Type": "Text", EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Qty__c", "Field Label": "数量",
             "Data Type": "Number", "Precision": "18", "Scale": "0", EN_FLAG: False}]
    obj_xml = ET.fromstring(
        f"<records><fullName>{OBJ}</fullName>"
        f"<nameField><type>Text</type><label>No</label></nameField>"
        f"<fields><fullName>TI_Fnt_Qty__c</fullName><type>Text</type>"
        f"<length>255</length></fields></records>")
    snap = existing_snapshot(obj_xml, fields=["TI_Fnt_Qty__c"])

    def no_network(*a, **k):
        raise AssertionError("drift must not call the org: the snapshot has it")

    monkeypatch.setattr(attr_drift, "read_org_object", no_network)
    monkeypatch.setattr(attr_drift, "load_token", no_network)

    drift = plan_deploy.compute_drift(rows, snap)
    assert drift[OBJ][0]["field"] == "TI_Fnt_Qty__c"
    assert "type: sheet=Number org=Text" in drift[OBJ][0]["reason"]


def test_new_fields_are_not_reported_as_drift():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             "Name Field Type": "Text", EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_New__c", "Field Label": "新",
             "Data Type": "Text", "Length": "255", EN_FLAG: False}]
    snap = existing_snapshot(org_object_xml())
    assert plan_deploy.compute_drift(rows, snap) == {}


# --------------------------------------------------------------------------- #
# 6. IsDelete really goes through the destructive flow
# --------------------------------------------------------------------------- #

def delete_plan() -> dict:
    return {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": [{"object": OBJ}], "translations": [],
            "translationPackage": [],
            "sheet": {"id": "SHEET123", "tabs": ["受入"]},
            "deleteMembers": [f"{OBJ}.TI_Fnt_Old__c"],
            "manifestMembers": {}, "manifestParts": [],
            "summary": {"empty": True}}


def test_isdelete_is_built_and_only_deleted_on_request(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_cmd, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(deploy_cmd, "DESTRUCTIVE", tmp_path / "destructiveChanges.xml")
    monkeypatch.setattr(deploy_cmd, "DESTRUCTIVE_PACKAGE",
                        tmp_path / "destructive_package.xml")

    built: list[list[str]] = []

    def fake_build(argv):
        built.append(argv)
        (tmp_path / "destructiveChanges.xml").write_text("<Package/>", encoding="utf-8")
        (tmp_path / "destructive_package.xml").write_text("<Package/>", encoding="utf-8")
        return 0

    monkeypatch.setattr(deploy_cmd.build_destructive, "main", fake_build)
    monkeypatch.setattr(deploy_cmd.sf_deployer, "main",
                        lambda argv: pytest.fail("must not delete without --deletes"))

    deploy_cmd.destructive_stage(args_ns(deletes=False), delete_plan(), start=True)
    assert built, "the IsDelete set must be built into destructiveChanges.xml"
    assert "--target-org" in built[0]


def test_isdelete_runs_the_destructive_deploy_with_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_cmd, "MANIFEST_DIR", tmp_path)
    monkeypatch.setattr(deploy_cmd, "DESTRUCTIVE", tmp_path / "destructiveChanges.xml")
    monkeypatch.setattr(deploy_cmd, "DESTRUCTIVE_PACKAGE",
                        tmp_path / "destructive_package.xml")

    def fake_build(argv):
        (tmp_path / "destructiveChanges.xml").write_text("<Package/>", encoding="utf-8")
        (tmp_path / "destructive_package.xml").write_text("<Package/>", encoding="utf-8")
        return 0

    sent: list[list[str]] = []
    monkeypatch.setattr(deploy_cmd.build_destructive, "main", fake_build)
    monkeypatch.setattr(deploy_cmd.sf_deployer, "main", lambda argv: sent.append(argv) or 0)

    deploy_cmd.destructive_stage(args_ns(deletes=True), delete_plan(), start=True)
    assert sent and "--pre-destructive" in sent[0] and "--start" in sent[0]


def test_isdelete_is_never_in_the_additive_package():
    rows = [{"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
             "_DeleteRequested": ["TI_Fnt_Old__c"], EN_FLAG: False},
            {"_SheetName": "t", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_New__c", "Field Label": "新",
             "Data Type": "Text", "Length": "255", EN_FLAG: False}]
    snap = existing_snapshot(org_object_xml())
    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")

    assert plan["deleteMembers"] == [f"{OBJ}.TI_Fnt_Old__c"]
    assert f"{OBJ}.TI_Fnt_Old__c" not in plan["manifestMembers"]["CustomField"]
