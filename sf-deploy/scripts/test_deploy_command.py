#!/usr/bin/env python3
"""Self-test: what the orchestrator DOES with a plan.

Run from anywhere:  python3 scripts/test_deploy_command.py
No org, no sheet, no Salesforce CLI — every `sf` call is stubbed, so these
tests exercise the orchestrator's behaviour (package iteration, drift
classification, object dependencies, translation verification, destructive
routing) rather than inspecting source strings.

Companion to test_deploy_plan.py, which covers the plan itself.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import attr_drift  # noqa: E402
import plan_deploy  # noqa: E402
import prep_deploy  # noqa: E402
import verify_deploy  # noqa: E402
from build_manifest import plan_parts  # noqa: E402
from translation_lib import EN_FLAG, make_entry  # noqa: E402

OBJ = "TI_Fnt_Receiving__c"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

def args_ns(**kw) -> argparse.Namespace:
    base = dict(org="SANDBOX_A", test_level="NoTestRun", sf_home="",
                xdg_data_home="", google_home="", deletes=False,
                sheet_id="SHEET123", max_components=9000)
    base.update(kw)
    return argparse.Namespace(**base)


@contextlib.contextmanager
def patched(obj, name, value):
    """Minimal monkeypatch, so this file needs no test framework."""
    missing = object()
    old = getattr(obj, name, missing)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if old is missing:
            delattr(obj, name)
        else:
            setattr(obj, name, old)


@contextlib.contextmanager
def fake_runner(handler):
    """Replace subprocess.run inside prep_deploy with `handler(cmd) -> rc`."""
    def run(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, handler(cmd))
    with patched(prep_deploy.subprocess, "run", run):
        yield


@contextlib.contextmanager
def real_deploy_log(tmp: Path):
    """A log that looks like a genuine `deploy start`, as the flow demands."""
    log = tmp / "last_deploy.log"
    log.write_text("# command: sf project deploy start --manifest package.xml\n",
                   encoding="utf-8")
    with patched(prep_deploy, "LAST_DEPLOY_LOG", log):
        yield log


def make_parts(tmp: Path, n: int, per: int = 2) -> list[dict]:
    members = {"CustomField": [f"TI_Fnt_O{i}__c.F{j}__c"
                               for i in range(n) for j in range(per)]}
    parts = []
    for p in plan_parts(members, per):          # one object's group per package
        path = tmp / p["file"]
        path.write_text("<Package/>", encoding="utf-8")
        parts.append({**p, "path": path})
    return parts


def pkg_of(cmd: list[str]) -> str:
    return Path(cmd[cmd.index("--package") + 1]).name


def org_object_xml(*, name_type="Text", display_format="",
                   enable_history="false", sharing="ReadWrite") -> ET.Element:
    df = f"<displayFormat>{display_format}</displayFormat>" if display_format else ""
    return ET.fromstring(
        f"<records><fullName>{OBJ}</fullName>"
        f"<enableHistory>{enable_history}</enableHistory>"
        f"<sharingModel>{sharing}</sharingModel>"
        f"<nameField><type>{name_type}</type>{df}<label>No</label></nameField>"
        f"</records>")


def existing_snapshot(obj_xml: ET.Element, fields=()) -> dict:
    return {"target": {"orgId": "00D1", "alias": "A"}, "lang": "off",
            "objects": {OBJ: {"exists": True}}, "fields": {OBJ: list(fields)},
            "objectMeta": {OBJ: ET.tostring(obj_xml, encoding="unicode")},
            "translations": {}, "translationState": "off", "translationNote": ""}


def meta_row(**extra) -> dict:
    return {"_type": "object_meta", "_SheetName": "t", "Object API Name": OBJ,
            EN_FLAG: False, **extra}


def field_row(api, dtype="Text", **extra) -> dict:
    r = {"_SheetName": "t", "Object API Name": OBJ, "Field API Name": api,
         "Field Label": "数量", "Data Type": dtype, EN_FLAG: False}
    if dtype == "Text":
        r["Length"] = "255"
    r.update(extra)
    return r


# --------------------------------------------------------------------------- #
# 1. every package part is deployed, in order
# --------------------------------------------------------------------------- #

def test_every_package_part_is_deployed_in_order():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = make_parts(tmp, 4)
        assert len(parts) == 4, "fixture must actually split"
        deployed, verified = [], []

        def handler(cmd):
            (verified if "verify_deploy.py" in cmd[1] else deployed).append(cmd)
            return 0

        with real_deploy_log(tmp), fake_runner(handler):
            prep_deploy.deploy_packages(args_ns(), parts, start=True)

        assert [pkg_of(c) for c in deployed] == [p["file"] for p in parts]
        assert len(verified) == len(parts), "each part is verified before the next"
        assert all("--part-members" in c for c in verified)


def test_check_only_also_covers_every_part():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = make_parts(tmp, 3)
        seen = []

        def handler(cmd):
            assert "verify_deploy.py" not in cmd[1], "check-only must not verify"
            seen.append(cmd)
            return 0

        with real_deploy_log(tmp), fake_runner(handler):
            prep_deploy.deploy_packages(args_ns(), parts, start=False)

        assert len(seen) == 3
        assert all("--start" not in cmd for cmd in seen), "check-only never writes"


def test_a_failed_part_stops_the_remaining_parts():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = make_parts(tmp, 3)
        calls = []

        def handler(cmd):
            if "verify_deploy.py" in cmd[1]:
                return 0
            calls.append(pkg_of(cmd))
            return 1 if "part2" in pkg_of(cmd) else 0

        try:
            with real_deploy_log(tmp), fake_runner(handler):
                prep_deploy.deploy_packages(args_ns(), parts, start=True)
            raise AssertionError("a failed part must stop the run")
        except SystemExit as e:
            assert "part2" in str(e) and "NOT deployed" in str(e)
        assert len(calls) == 2, "part 3 must not be attempted after part 2 failed"


def test_a_part_that_did_not_land_stops_the_run():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = make_parts(tmp, 3)
        sent = []

        def handler(cmd):
            if "verify_deploy.py" in cmd[1]:
                return 1                     # deployed, but not confirmed live
            sent.append(pkg_of(cmd))
            return 0

        try:
            with real_deploy_log(tmp), fake_runner(handler):
                prep_deploy.deploy_packages(args_ns(), parts, start=True)
            raise AssertionError("an unverified part must stop the run")
        except SystemExit as e:
            assert "did NOT fully land" in str(e)
        assert sent == ["package.part1.xml"]


def test_a_stale_dry_run_log_is_never_read_as_a_real_deploy():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = make_parts(tmp, 1)
        log = tmp / "last_deploy.log"
        log.write_text("# command: sf project deploy start --dry-run\n",
                       encoding="utf-8")
        try:
            with patched(prep_deploy, "LAST_DEPLOY_LOG", log), \
                    fake_runner(lambda cmd: 0):
                prep_deploy.deploy_packages(args_ns(), parts, start=True)
            raise AssertionError("a dry-run log must not pass as a real deploy")
        except SystemExit as e:
            assert "not a real 'deploy start'" in str(e)


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
    assert sorted(flat) == sorted(m for v in plan["manifestMembers"].values()
                                  for m in v)
    assert len(flat) == len(set(flat))


def test_the_orchestrator_reads_every_part_from_the_plan():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        plan = {"manifestMembers": {"CustomField": ["A__c.F__c", "B__c.F__c"]},
                "manifestParts": [
                    {"file": "package.part1.xml",
                     "members": {"CustomField": ["A__c.F__c"]}, "components": 1},
                    {"file": "package.part2.xml",
                     "members": {"CustomField": ["B__c.F__c"]}, "components": 1}]}
        with patched(prep_deploy, "PACKAGE", tmp / "manifest/package.xml"):
            parts = prep_deploy.package_parts(plan)
        assert [p["file"] for p in parts] == ["package.part1.xml",
                                              "package.part2.xml"]
        assert all(p["path"].parent.name == "manifest" for p in parts)


def test_a_missing_planned_package_is_a_hard_stop():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        parts = [{"file": "package.part1.xml", "components": 1,
                  "members": {"CustomField": ["A__c.F__c"]},
                  "path": tmp / "package.part1.xml"}]     # never written
        try:
            with real_deploy_log(tmp), fake_runner(lambda cmd: 0):
                prep_deploy.deploy_packages(args_ns(), parts, start=False)
            raise AssertionError("a planned package that was not written must stop")
        except SystemExit as e:
            assert "was not written" in str(e)


# --------------------------------------------------------------------------- #
# 2. standard Name drift is a CustomObject, not a CustomField
# --------------------------------------------------------------------------- #

def test_standard_name_drift_is_classified_as_custom_object():
    rows = [meta_row(**{"Name Field Type": "Autonumber",
                        "Name Field Display Format": "RCV-{0000000}"})]
    org = attr_drift.parse_org_object(org_object_xml(name_type="Text"))
    drift, _insync, _w = attr_drift.compute_drift(OBJ, rows, org)

    name = next(d for d in drift if d["field"] == "Name")
    assert name["component"] == "CustomObject", \
        "the standard Name field lives on the CustomObject, not on a CustomField"

    snap = existing_snapshot(org_object_xml())
    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off",
                                  drift={OBJ: drift},
                                  include_drift={f"{OBJ}.Name"})
    assert plan["manifestMembers"].get("CustomObject") == [OBJ]
    assert f"{OBJ}.Name" not in plan["manifestMembers"].get("CustomField", []), \
        "Obj__c.Name is not a valid CustomField member"
    assert plan["objectUpdates"][OBJ], "the plan must say WHY the object ships"


def test_custom_field_drift_stays_a_custom_field():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", dtype="Number",
                                  Precision="18", Scale="0")]
    snap = existing_snapshot(org_object_xml(), fields=["TI_Fnt_Qty__c"])
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

def test_history_tracked_new_field_pulls_in_the_object():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c", **{"Track History": "TRUE"})]
    snap = existing_snapshot(org_object_xml(enable_history="false"))

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")
    assert plan["manifestMembers"]["CustomObject"] == [OBJ]
    assert "enableHistory" in " ".join(plan["objectUpdates"][OBJ])
    # …and still only the new field, never the object's existing ones
    assert plan["manifestMembers"]["CustomField"] == [f"{OBJ}.TI_Fnt_Qty__c"]


def test_master_detail_new_field_pulls_in_the_object():
    rows = [meta_row(),
            field_row("TI_Fnt_Parent__c", dtype="MasterDetail",
                      **{"Type Specific Value": "TI_Fnt_Deal__c"})]
    snap = existing_snapshot(org_object_xml(sharing="ReadWrite"))

    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")
    assert plan["manifestMembers"]["CustomObject"] == [OBJ]
    assert "ControlledByParent" in " ".join(plan["objectUpdates"][OBJ])


def test_no_object_level_change_means_no_object_member():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c")]
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


@contextlib.contextmanager
def org_translations(fields: dict[str, str]):
    with patched(verify_deploy, "org_auth", lambda org: {"accessToken": "t"}), \
            patched(verify_deploy, "read_object_translations",
                    lambda objs, lang, auth: [cot_record(OBJ, "en_US", fields)]):
        yield


def test_translation_verification_confirms_the_live_value():
    with org_translations({"TI_Fnt_Qty__c": "Quantity"}):
        assert verify_deploy.verify_translations(translated_plan(), "SANDBOX_A") == []


def test_translation_missing_in_the_org_fails_verification():
    with org_translations({}):
        failures = verify_deploy.verify_translations(translated_plan(), "SANDBOX_A")
    assert len(failures) == 1 and "not present" in failures[0]


def test_translation_with_a_different_value_fails_verification():
    with org_translations({"TI_Fnt_Qty__c": "Qty"}):
        failures = verify_deploy.verify_translations(translated_plan(), "SANDBOX_A")
    assert len(failures) == 1 and "Qty" in failures[0]


def test_a_translation_only_deploy_is_not_verified_by_fields_alone():
    """No new fields ⇒ the object/field check is vacuous; translations decide."""
    with tempfile.TemporaryDirectory() as td:
        plan_path = Path(td) / "plan.json"
        plan_path.write_text(json.dumps(translated_plan()), encoding="utf-8")
        with patched(verify_deploy, "org_has_object", lambda obj, org: True), \
                patched(verify_deploy, "org_fields", lambda obj, org: set()), \
                patched(verify_deploy, "verify_translations",
                        lambda p, org, objects=None, translation_members=None:
                            ["TI_Fnt_Qty__c: not present"]):
            rc = verify_deploy.main(["--target-org", "A", "--plan", str(plan_path),
                                     "--objects", OBJ])
    assert rc == 1, "an unverified translation must fail the whole verification"


def test_sync_state_is_written_only_for_a_verified_org():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "translation_sync_state.json"
        plan = translated_plan()
        with patched(prep_deploy, "SYNC_STATE", state):
            prep_deploy.persist_sync_state(plan)
            saved = json.loads(state.read_text(encoding="utf-8"))
            assert list(saved["orgs"]) == ["00D1"], \
                "state is keyed by the immutable org Id"

            # no org Id (verification never identified the org) ⇒ nothing recorded
            state.unlink()
            plan["target"]["orgId"] = ""
            prep_deploy.persist_sync_state(plan)
            assert not state.exists()


# --------------------------------------------------------------------------- #
# 5. one snapshot: drift is computed locally, never per object over the wire
# --------------------------------------------------------------------------- #

def test_attribute_drift_is_computed_from_the_snapshot():
    rows = [meta_row(**{"Name Field Type": "Text"}),
            field_row("TI_Fnt_Qty__c", dtype="Number", Precision="18", Scale="0")]
    obj_xml = ET.fromstring(
        f"<records><fullName>{OBJ}</fullName>"
        f"<nameField><type>Text</type><label>No</label></nameField>"
        f"<fields><fullName>TI_Fnt_Qty__c</fullName><type>Text</type>"
        f"<length>255</length></fields></records>")
    snap = existing_snapshot(obj_xml, fields=["TI_Fnt_Qty__c"])

    def no_network(*a, **k):
        raise AssertionError("drift must not call the org: the snapshot has it")

    with patched(attr_drift, "read_org_object", no_network), \
            patched(attr_drift, "load_token", no_network):
        drift = plan_deploy.compute_drift(rows, snap)

    assert drift[OBJ][0]["field"] == "TI_Fnt_Qty__c"
    assert "type: sheet=Number org=Text" in drift[OBJ][0]["reason"]


def test_new_fields_are_not_reported_as_drift():
    rows = [meta_row(**{"Name Field Type": "Text"}), field_row("TI_Fnt_New__c")]
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
            "summary": {"empty": False, "additiveEmpty": True, "deletes": 1}}


@contextlib.contextmanager
def destructive_paths(tmp: Path):
    with patched(prep_deploy, "DESTRUCTIVE", tmp / "destructiveChanges.xml"), \
            patched(prep_deploy, "DESTRUCTIVE_PACKAGE",
                    tmp / "destructive_package.xml"):
        yield


def build_destructive_files(tmp: Path) -> None:
    (tmp / "destructiveChanges.xml").write_text("<Package/>", encoding="utf-8")
    (tmp / "destructive_package.xml").write_text("<Package/>", encoding="utf-8")


def test_isdelete_is_built_but_not_deleted_without_the_flag():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sent = []

        def handler(cmd):
            sent.append(cmd)
            return 0

        with destructive_paths(tmp), fake_runner(handler):
            prep_deploy.destructive_phase(args_ns(deletes=False), delete_plan(),
                                          start=True)
        xml = (tmp / "destructiveChanges.xml").read_text(encoding="utf-8")
        assert f"{OBJ}.TI_Fnt_Old__c" in xml
        assert sent == [], "must not deploy anything without --deletes"


def test_isdelete_runs_the_destructive_deploy_with_the_flag():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sent = []

        def handler(cmd):
            sent.append(cmd)
            return 0

        with destructive_paths(tmp), fake_runner(handler), \
                patched(verify_deploy, "verify_deletes", lambda plan, org: []):
            prep_deploy.destructive_phase(args_ns(deletes=True), delete_plan(),
                                          start=True)
        xml = (tmp / "destructiveChanges.xml").read_text(encoding="utf-8")
        assert f"{OBJ}.TI_Fnt_Old__c" in xml
        assert sent and "--pre-destructive" in sent[0] and "--start" in sent[0]


def test_delete_only_deploy_phase_still_runs_destructive():
    called = []

    def fake_dest(args, plan, *, start):
        called.append(start)

    def no_additive(*a, **k):
        raise AssertionError("delete-only must not send an additive package")

    plan = delete_plan()
    with patched(prep_deploy, "destructive_phase", fake_dest), \
            patched(prep_deploy, "deploy_packages", no_additive), \
            patched(prep_deploy, "refresh_object_reports", lambda *a, **k: None):
        rc = prep_deploy.deploy_phase(args_ns(deletes=True), Path("x"), plan, {})
    assert rc == 0
    assert called == [True]


def test_isdelete_is_never_in_the_additive_package():
    rows = [meta_row(_DeleteRequested=["TI_Fnt_Old__c"]), field_row("TI_Fnt_New__c")]
    snap = existing_snapshot(org_object_xml(), fields=["TI_Fnt_Old__c"])
    plan = plan_deploy.build_plan(rows, snap, sheet_id="S", tabs="t", lang="off")

    assert plan["deleteMembers"] == [f"{OBJ}.TI_Fnt_Old__c"]
    assert f"{OBJ}.TI_Fnt_Old__c" not in plan["manifestMembers"]["CustomField"]


def test_multi_object_name_drift_uses_matching_object_meta():
    a, b = "A__c", "B__c"
    rows = [
        {"_type": "object_meta", "Object API Name": a, "Name Field Type": "Text"},
        {"_type": "object_meta", "Object API Name": b,
         "Name Field Type": "Autonumber", "Name Field Display Format": "B-{0000}"},
    ]
    org_a = attr_drift.parse_org_object(ET.fromstring(
        f"<records><fullName>{a}</fullName>"
        f"<nameField><type>Text</type></nameField></records>"))
    org_b = attr_drift.parse_org_object(ET.fromstring(
        f"<records><fullName>{b}</fullName>"
        f"<nameField><type>AutoNumber</type>"
        f"<displayFormat>B-{{0000}}</displayFormat></nameField></records>"))
    drift_a, _, _ = attr_drift.compute_drift(a, rows, org_a)
    drift_b, _, _ = attr_drift.compute_drift(b, rows, org_b)
    assert not any(d["field"] == "Name" for d in drift_a), drift_a
    assert not any(d["field"] == "Name" for d in drift_b), drift_b


def test_per_part_translation_verification_ignores_later_parts():
    e1 = make_entry(kind="ObjectField", component="A__c", aspect="label",
                    key="F1__c", language="en_US", master="一", translation="One")
    e2 = make_entry(kind="ObjectField", component="B__c", aspect="label",
                    key="F2__c", language="en_US", master="二", translation="Two")
    e1["package"] = e2["package"] = True
    plan = {"lang": "en_US", "translations": [e1, e2],
            "translationPackage": [e1["id"], e2["id"]]}

    def only_a(objs, lang, auth):
        return [cot_record("A__c", "en_US", {"F1__c": "One"})]

    with patched(verify_deploy, "org_auth", lambda org: {"accessToken": "t"}), \
            patched(verify_deploy, "read_object_translations", only_a):
        assert verify_deploy.verify_translations(plan, "X", objects={"A__c"}) == []

    def none(objs, lang, auth):
        return []

    with patched(verify_deploy, "org_auth", lambda org: {"accessToken": "t"}), \
            patched(verify_deploy, "read_object_translations", none):
        failures = verify_deploy.verify_translations(plan, "X")
    assert len(failures) == 2


def test_translation_scope_skips_workbench_without_en():
    rows = [meta_row(), field_row("TI_Fnt_Qty__c")]
    lang, objs = prep_deploy.translation_scope(rows, [OBJ], {OBJ: "t"}, "en_US")
    assert lang == "off"
    assert objs == []

    rows_en = [{"_type": "object_meta", "_SheetName": "受入",
                "Object API Name": OBJ, EN_FLAG: True},
               {"_SheetName": "受入", "Object API Name": OBJ,
                "Field API Name": "TI_Fnt_Qty__c", EN_FLAG: True,
                "Field Label (EN)": "Quantity"}]
    lang, objs = prep_deploy.translation_scope(
        rows_en, [OBJ], {OBJ: "受入"}, "en_US")
    assert lang == "en_US"
    assert objs == [OBJ]


def test_delete_only_fails_if_fields_remain():
    def still_there(plan, org):
        return [f"{OBJ}.TI_Fnt_Old__c"]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def handler(cmd):
            return 0

        with destructive_paths(tmp), fake_runner(handler), \
                patched(verify_deploy, "verify_deletes", still_there):
            try:
                prep_deploy.destructive_phase(
                    args_ns(deletes=True), delete_plan(), start=True)
                raise AssertionError("must not report success while the field remains")
            except SystemExit as e:
                assert "still in the org" in str(e)


def test_one_object_split_across_parts_verifies_only_this_part():
    """Part 1 must not require part 2's fields of the same object to exist yet."""
    part1 = {"CustomField": [f"{OBJ}.TI_Fnt_A__c"]}
    assert verify_deploy.expected_from_part_members(part1) == {OBJ: ["TI_Fnt_A__c"]}
    plan = {"objects": [{"object": OBJ}],
            "newFields": {OBJ: ["TI_Fnt_A__c", "TI_Fnt_B__c", "TI_Fnt_C__c"]},
            "lang": "en_US", "translations": [], "translationPackage": []}
    with tempfile.TemporaryDirectory() as td:
        plan_path = Path(td) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with patched(verify_deploy, "org_has_object", lambda obj, org: True), \
                patched(verify_deploy, "org_fields",
                        lambda obj, org: {"TI_Fnt_A__c"}):
            rc_part = verify_deploy.main(
                ["--target-org", "A", "--plan", str(plan_path),
                 "--part-members", json.dumps(part1)])
            rc_object = verify_deploy.main(
                ["--target-org", "A", "--plan", str(plan_path), "--objects", OBJ])
    assert rc_part == 0
    assert rc_object == 1, "object-scoped verify would demand part 2's fields"


def test_attribute_updates_are_verified_against_live_metadata():
    """Existence-only verification must not pass when the org still has old attrs."""
    plan = {
        "attributeExpectations": {
            OBJ: {
                "object": {"enableHistory": "true",
                           "sharingModel": "ControlledByParent"},
                "nameField": {"type": "AutoNumber",
                              "displayFormat": "RCV-{0000000}"},
                "fields": {"TI_Fnt_Kind__c": {
                    "type": "Picklist", "formula": False, "picklist": ["A", "B"]}},
            }
        }
    }
    auth = {"accessToken": "t", "instanceUrl": "https://x", "apiVersion": "60.0"}
    stale = (
        f"<records><fullName>{OBJ}</fullName>"
        "<enableHistory>false</enableHistory>"
        "<sharingModel>ReadWrite</sharingModel>"
        "<nameField><type>Text</type><label>No</label></nameField>"
        "<fields><fullName>TI_Fnt_Kind__c</fullName><type>Checkbox</type></fields>"
        "</records>")
    landed = (
        f"<records><fullName>{OBJ}</fullName>"
        "<enableHistory>true</enableHistory>"
        "<sharingModel>ControlledByParent</sharingModel>"
        "<nameField><type>AutoNumber</type>"
        "<displayFormat>RCV-{0000000}</displayFormat><label>No</label></nameField>"
        "<fields><fullName>TI_Fnt_Kind__c</fullName><type>Picklist</type>"
        "<value><fullName>A</fullName></value>"
        "<value><fullName>B</fullName></value></fields>"
        "</records>")

    with patched(verify_deploy, "org_auth", lambda org: auth), \
            patched(verify_deploy.org_snapshot, "object_snapshot",
                    lambda objs, a: {OBJ: stale}):
        fails = verify_deploy.verify_attribute_updates(plan, "A")
        assert fails, "stale org metadata must fail attribute verification"
        assert any("Name type" in f for f in fails), fails
        assert any("TI_Fnt_Kind__c" in f for f in fails), fails
        assert any("enableHistory" in f for f in fails), fails

    with patched(verify_deploy, "org_auth", lambda org: auth), \
            patched(verify_deploy.org_snapshot, "object_snapshot",
                    lambda objs, a: {OBJ: landed}):
        assert verify_deploy.verify_attribute_updates(plan, "A") == []

    # a field-only package part must not demand the Name/object patch yet
    field_part = {"CustomField": [f"{OBJ}.TI_Fnt_Kind__c"]}
    mixed = (
        f"<records><fullName>{OBJ}</fullName>"
        "<enableHistory>false</enableHistory>"
        "<sharingModel>ReadWrite</sharingModel>"
        "<nameField><type>Text</type><label>No</label></nameField>"
        "<fields><fullName>TI_Fnt_Kind__c</fullName><type>Picklist</type>"
        "<value><fullName>A</fullName></value>"
        "<value><fullName>B</fullName></value></fields>"
        "</records>")
    with patched(verify_deploy, "org_auth", lambda org: auth), \
            patched(verify_deploy.org_snapshot, "object_snapshot",
                    lambda objs, a: {OBJ: mixed}):
        assert verify_deploy.verify_attribute_updates(
            plan, "A", members=field_part) == []


def main() -> int:
    print("test_deploy_command")
    for fn in (
        test_every_package_part_is_deployed_in_order,
        test_check_only_also_covers_every_part,
        test_a_failed_part_stops_the_remaining_parts,
        test_a_part_that_did_not_land_stops_the_run,
        test_a_stale_dry_run_log_is_never_read_as_a_real_deploy,
        test_plan_publishes_the_manifest_index,
        test_the_orchestrator_reads_every_part_from_the_plan,
        test_a_missing_planned_package_is_a_hard_stop,
        test_standard_name_drift_is_classified_as_custom_object,
        test_custom_field_drift_stays_a_custom_field,
        test_history_tracked_new_field_pulls_in_the_object,
        test_master_detail_new_field_pulls_in_the_object,
        test_no_object_level_change_means_no_object_member,
        test_translation_verification_confirms_the_live_value,
        test_translation_missing_in_the_org_fails_verification,
        test_translation_with_a_different_value_fails_verification,
        test_a_translation_only_deploy_is_not_verified_by_fields_alone,
        test_sync_state_is_written_only_for_a_verified_org,
        test_attribute_drift_is_computed_from_the_snapshot,
        test_new_fields_are_not_reported_as_drift,
        test_isdelete_is_built_but_not_deleted_without_the_flag,
        test_isdelete_runs_the_destructive_deploy_with_the_flag,
        test_delete_only_deploy_phase_still_runs_destructive,
        test_isdelete_is_never_in_the_additive_package,
        test_multi_object_name_drift_uses_matching_object_meta,
        test_per_part_translation_verification_ignores_later_parts,
        test_translation_scope_skips_workbench_without_en,
        test_delete_only_fails_if_fields_remain,
        test_one_object_split_across_parts_verifies_only_this_part,
        test_attribute_updates_are_verified_against_live_metadata,
    ):
        fn()
        print(f"  ok  {fn.__name__[5:].replace('_', '-')}")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
