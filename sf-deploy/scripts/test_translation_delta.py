#!/usr/bin/env python3
"""Self-test: the sheet-driven object-translation pipeline.

Run from anywhere:  python3 scripts/test_translation_delta.py
No org, no sheet, no Salesforce CLI — every org interaction is faked from a
captured readMetadata payload, so this also proves the pipeline is not tied to
one machine's HOME/keychain.

Covers:
  * delta classification + hashing + new-only packaging
  * PRESERVATION: record types, layouts, validation rules, field sets, quick
    actions, web links, plural/gender caseValues and sibling field files all
    survive a single-field patch
  * nameFieldLabel round-trip (standard Name field)
  * tabs with no `Field Label (EN)` column produce nothing
  * unparseable picklist EN cells are validation errors, not MISSING
  * per-org-ID sync state (sandbox A cannot influence sandbox B)
  * multiple languages
  * real source-format output + manifest membership
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from translation_lib import (  # noqa: E402
    CHANGED, EN_FLAG, KIND_NAME_FIELD, KIND_OBJECT_FIELD, KIND_OBJECT_LABEL,
    MISSING, NEW, PARSE_ERROR, SCHEMA_MISSING, UNCHANGED, apply_new_only, classify,
    content_hash, entries_from_object_rows, has_translation_columns,
    load_sync_state, make_entry, parse_object_translation, save_sync_state,
)
from generate_object_translation import (  # noqa: E402
    _merge_org, patch_translation, write_translation_dir,
)

OBJ = "TI_Fnt_ShipoutMovein__c"

# A readMetadata(CustomObjectTranslation) payload as it arrives after
# strip_soap_ns: field translations PLUS everything else the org holds.
ORG_COT = f"""
<records>
  <fullName>{OBJ}-en_US</fullName>
  <caseValues><plural>false</plural><value>Shipout movein</value></caseValues>
  <caseValues><plural>true</plural><value>Shipout moveins</value></caseValues>
  <caseValues><caseType>Accusative</caseType><plural>false</plural><value>Shipout movein (acc)</value></caseValues>
  <fieldSets><label>Key fields</label><name>KeyFields</name></fieldSets>
  <fields><label>Product name</label><name>TI_Fnt_ProductName__c</name></fields>
  <fields>
    <label>Status</label><name>TI_Fnt_Status__c</name>
    <picklistValues><masterLabel>出荷済</masterLabel><translation>Shipped</translation></picklistValues>
    <picklistValues><masterLabel>保留</masterLabel><translation>On hold</translation></picklistValues>
  </fields>
  <gender>Neuter</gender>
  <layouts><layout>Shipout Layout</layout><sections><label>Details</label><section>Details</section></sections></layouts>
  <nameFieldLabel>Shipout number</nameFieldLabel>
  <quickActions><label>New shipout</label><name>New_Shipout</name></quickActions>
  <recordTypes><label>Domestic</label><name>Domestic</name></recordTypes>
  <sharingReasons><label>Owner</label><name>Owner__c</name></sharingReasons>
  <startsWith>Consonant</startsWith>
  <validationRules><errorMessage>Quantity must be positive</errorMessage><name>Qty_Positive</name></validationRules>
  <webLinks><label>Track</label><name>Track</name></webLinks>
  <workflowTasks><description>Call back</description><name>Call_Back</name><subject>Call back</subject></workflowTasks>
</records>
"""


def org_rec() -> ET.Element:
    return ET.fromstring(ORG_COT)


def _field(obj, api, en, source="sheet", lang="en_US"):
    return make_entry(kind=KIND_OBJECT_FIELD, component=obj, aspect="label",
                      key=api, language=lang, master="JA", translation=en,
                      source=source)


def _row(tab, obj, api, label, en=None, dtype="Text", **extra):
    r = {"_SheetName": tab, "Object API Name": obj, "Field API Name": api,
         "Field Label": label, "Data Type": dtype}
    if en is not None:
        r["Field Label (EN)"] = en
        r[EN_FLAG] = True
    else:
        r[EN_FLAG] = False
    r.update(extra)
    return r


# --------------------------------------------------------------------------- #
# delta classification
# --------------------------------------------------------------------------- #

def test_japan_adds_one_field():
    """Object already has translations; Japan adds TI_Fnt_New__c with Field Label (EN) filled."""
    existing = _field(OBJ, "TI_Fnt_ProductName__c", "Product name", source="org")
    org = {existing["id"]: existing}

    sheet = [
        _field(OBJ, "TI_Fnt_ProductName__c", "Product name"),  # unchanged
        _field(OBJ, "TI_Fnt_New__c", "New field"),             # Japan add
        _field(OBJ, "TI_Fnt_Blank__c", ""),                    # EN not filled
    ]
    classified = apply_new_only(classify(sheet, org))
    by_key = {c["key"]: c for c in classified if c.get("key")}
    assert by_key["TI_Fnt_ProductName__c"]["code"] == UNCHANGED
    assert by_key["TI_Fnt_ProductName__c"]["package"] is False
    assert by_key["TI_Fnt_New__c"]["code"] == NEW
    assert by_key["TI_Fnt_New__c"]["package"] is True
    assert by_key["TI_Fnt_Blank__c"]["code"] == MISSING
    assert by_key["TI_Fnt_Blank__c"]["package"] is False

    packaged = {c["id"] for c in classified if c.get("package")}
    merged = _merge_org(sheet, org, packaged)
    assert f"ObjectField|{OBJ}|label|TI_Fnt_ProductName__c|en_US" in merged
    assert merged[f"ObjectField|{OBJ}|label|TI_Fnt_New__c|en_US"]["translation"] == "New field"
    print("  ok  japan-adds-one-field")


def test_changed_en_is_packaged_by_default():
    """An English label edit is a translation delta, not a destructive field change."""
    obj = "TI_Fnt_Deal__c"
    org_e = _field(obj, "TI_Fnt_Status__c", "Old Label", source="org")
    org = {org_e["id"]: org_e}
    sheet = [_field(obj, "TI_Fnt_Status__c", "New Label")]
    classified = classify(sheet, org)
    rec = classified[0]
    assert rec["code"] == CHANGED
    assert rec["package"] is True
    print("  ok  changed-en-is-packaged-by-default")


def test_changed_en_not_packaged_new_only():
    obj = "TI_Fnt_Deal__c"
    org_e = _field(obj, "TI_Fnt_Status__c", "Status", source="org")
    org = {org_e["id"]: org_e}
    sheet = [_field(obj, "TI_Fnt_Status__c", "Deal status")]  # edited EN
    classified = apply_new_only(classify(sheet, org))
    rec = classified[0]
    assert rec["code"] == CHANGED
    assert rec["package"] is False
    print("  ok  changed-en-not-packaged")


def test_matching_en_is_a_noop():
    existing = _field(OBJ, "TI_Fnt_ProductName__c", "Product name", source="org")
    classified = classify([_field(OBJ, "TI_Fnt_ProductName__c", "Product name")],
                          {existing["id"]: existing})
    rec = classified[0]
    assert rec["code"] == UNCHANGED and rec["package"] is False
    print("  ok  matching-en-is-a-noop")


def test_wip_and_isdelete_rows_are_not_catalogued():
    rows = [
        _row("出荷", OBJ, "TI_Fnt_Qty__c", "数量", en="Quantity"),
        _row("出荷", OBJ, "TI_Fnt_Draft__c", "下書き", en="Draft", WIP="TRUE"),
        _row("出荷", OBJ, "TI_Fnt_Old__c", "旧", en="Old", IsDelete="TRUE"),
    ]
    keys = {e["key"] for e in entries_from_object_rows(rows)}
    assert "TI_Fnt_Qty__c" in keys
    assert "TI_Fnt_Draft__c" not in keys
    assert "TI_Fnt_Old__c" not in keys
    print("  ok  wip-and-isdelete-rows-are-not-catalogued")


def test_schema_missing_is_not_packaged():
    e = _field(OBJ, "TI_Fnt_Ghost__c", "Ghost")
    classified = classify(
        [e], {}, org_schema={OBJ: {"TI_Fnt_ProductName__c"}},
        planned_fields={OBJ: set()})
    rec = classified[0]
    assert rec["code"] == SCHEMA_MISSING
    assert rec["package"] is False
    print("  ok  schema-missing-is-not-packaged")


def test_hash_utf8():
    assert content_hash("商品名") == content_hash("商品名")
    assert content_hash("商品名") != content_hash("Product name")
    print("  ok  utf8-hash")


# --------------------------------------------------------------------------- #
# preservation of everything the org already holds
# --------------------------------------------------------------------------- #

def test_patch_preserves_all_org_translations():
    """Patching ONE new field must not drop any other translated node or file."""
    new = _field(OBJ, "TI_Fnt_New__c", "New field")
    parent, fields = patch_translation(org_rec(), [new])

    parent_tags = [c.tag for c in parent]
    for tag in ("caseValues", "fieldSets", "gender", "layouts", "nameFieldLabel",
                "quickActions", "recordTypes", "sharingReasons", "startsWith",
                "validationRules", "webLinks", "workflowTasks"):
        assert tag in parent_tags, f"{tag} was dropped from the parent"
    # plural + case caseValues variants survive alongside the singular label
    cases = [c for c in parent if c.tag == "caseValues"]
    assert len(cases) == 3
    assert {c.findtext("value") for c in cases} == {
        "Shipout movein", "Shipout moveins", "Shipout movein (acc)"}
    # sibling field files survive, including their picklist values
    assert set(fields) == {"TI_Fnt_ProductName__c", "TI_Fnt_Status__c", "TI_Fnt_New__c"}
    status = fields["TI_Fnt_Status__c"]
    assert {pv.findtext("masterLabel") for pv in status.findall("picklistValues")} == {"出荷済", "保留"}
    assert fields["TI_Fnt_New__c"].findtext("label") == "New field"
    # nested children are preserved, not flattened
    assert parent_tags.count("layouts") == 1
    layout = next(c for c in parent if c.tag == "layouts")
    assert layout.find("sections") is not None
    print("  ok  patch-preserves-all-org-translations")


def test_name_field_label_round_trip():
    """The standard Name translation lives on the parent <nameFieldLabel>."""
    parsed = parse_object_translation(org_rec(), OBJ, "en_US")
    key = f"NameField|{OBJ}|label|Name|en_US"
    assert key in parsed, "nameFieldLabel was not parsed"
    assert parsed[key]["translation"] == "Shipout number"

    # unchanged by an unrelated patch
    parent, fields = patch_translation(org_rec(), [_field(OBJ, "TI_Fnt_New__c", "New field")])
    assert next(c for c in parent if c.tag == "nameFieldLabel").text == "Shipout number"
    assert "Name" not in fields, "Name must not become a fieldTranslation file"

    # and updatable from the sheet
    name_en = make_entry(kind=KIND_NAME_FIELD, component=OBJ, aspect="label",
                         key="Name", language="en_US", master="出荷番号",
                         translation="Shipout no.", source="sheet")
    parent, fields = patch_translation(org_rec(), [name_en])
    assert next(c for c in parent if c.tag == "nameFieldLabel").text == "Shipout no."
    assert "Name" not in fields
    # round-trips back through the parser (org records arrive ns-stripped)
    from translation_lib import strip_soap_ns
    xml = strip_soap_ns(render_parent(parent))
    again = parse_object_translation(ET.fromstring(xml), OBJ, "en_US")
    assert again[f"NameField|{OBJ}|label|Name|en_US"]["translation"] == "Shipout no."
    print("  ok  name-field-label-round-trip")


def test_object_label_patch_keeps_variants():
    label = make_entry(kind=KIND_OBJECT_LABEL, component=OBJ, aspect="label",
                       key=OBJ, language="en_US", master="出荷移動",
                       translation="Shipout transfer", source="sheet")
    parent, _ = patch_translation(org_rec(), [label])
    cases = [c for c in parent if c.tag == "caseValues"]
    assert len(cases) == 3
    base = [c for c in cases if c.findtext("plural") == "false"
            and not c.findtext("caseType")]
    assert base[0].findtext("value") == "Shipout transfer"
    assert any(c.findtext("value") == "Shipout moveins" for c in cases)
    assert any(c.findtext("caseType") == "Accusative" for c in cases)
    print("  ok  object-label-patch-keeps-variants")


def render_parent(parent) -> str:
    from translation_lib import COT_CHILD_ORDER, render_metadata
    return render_metadata("CustomObjectTranslation", parent, COT_CHILD_ORDER)


# --------------------------------------------------------------------------- #
# EN-column gating
# --------------------------------------------------------------------------- #

def test_tab_without_en_column_produces_nothing():
    rows = [
        {"_type": "object_meta", "_SheetName": "受入", "Object API Name": OBJ,
         "Object Label": "受入", EN_FLAG: False},
        _row("受入", OBJ, "TI_Fnt_ProductName__c", "商品名"),
    ]
    assert has_translation_columns(rows) is False
    assert entries_from_object_rows(rows) == []
    print("  ok  tab-without-en-column-produces-nothing")


def test_mixed_tabs_only_translated_one_contributes():
    rows = [
        _row("受入", OBJ, "TI_Fnt_ProductName__c", "商品名"),
        _row("出荷", "TI_Fnt_Shipout__c", "TI_Fnt_Qty__c", "数量", en="Quantity"),
    ]
    assert has_translation_columns(rows) is True
    entries = entries_from_object_rows(rows)
    assert {e["component"] for e in entries} == {"TI_Fnt_Shipout__c"}
    print("  ok  mixed-tabs-only-translated-one-contributes")


def test_legacy_rows_without_flag_still_work():
    rows = [{"_SheetName": "出荷", "Object API Name": OBJ,
             "Field API Name": "TI_Fnt_Qty__c", "Field Label": "数量",
             "Data Type": "Number", "Field Label (EN)": "Quantity"}]
    assert has_translation_columns(rows) is True
    assert len(entries_from_object_rows(rows)) == 1
    print("  ok  legacy-rows-without-flag-still-work")


# --------------------------------------------------------------------------- #
# picklist parse errors are validation errors
# --------------------------------------------------------------------------- #

def test_picklist_parse_error_is_validation_error():
    rows = [_row("出荷", OBJ, "TI_Fnt_Status__c", "状態", en="Status",
                 dtype="Picklist",
                 **{"Type Specific Value": "出荷済;保留",
                    "Picklist Values (EN)": "Shipped;On hold;Extra"})]
    entries = entries_from_object_rows(rows)
    bad = [e for e in entries if e.get("parse_error")]
    assert bad, "count mismatch should be reported"
    codes = {c["code"] for c in classify(bad, {})}
    assert codes == {PARSE_ERROR}, f"expected PARSE_ERROR, got {codes}"
    print("  ok  picklist-parse-error-is-validation-error")


# --------------------------------------------------------------------------- #
# sync state is per target org
# --------------------------------------------------------------------------- #

def test_sync_state_scoped_by_org_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "translation_sync_state.json"
        e = _field(OBJ, "TI_Fnt_New__c", "New field")
        e.update({"code": NEW, "package": True})
        save_sync_state([e], "SANDBOX_A", path, org_id="00D0000000000A")

        assert load_sync_state(path, org_id="00D0000000000A")["entries"]
        # another sandbox must not inherit sandbox A's hashes
        assert load_sync_state(path, org_id="00D0000000000B")["entries"] == {}
        # and an unknown org is not silently treated as "already deployed"
        assert load_sync_state(path, org_id="")["entries"] == {}

        e2 = _field(OBJ, "TI_Fnt_Other__c", "Other")
        e2.update({"code": NEW, "package": True})
        save_sync_state([e2], "SANDBOX_B", path, org_id="00D0000000000B")
        data = json.loads(path.read_text())
        assert set(data["orgs"]) == {"00D0000000000A", "00D0000000000B"}
        assert len(load_sync_state(path, org_id="00D0000000000A")["entries"]) == 1
    print("  ok  sync-state-scoped-by-org-id")


# --------------------------------------------------------------------------- #
# source-format output + manifest
# --------------------------------------------------------------------------- #

def _generate_dir(tmp: Path, lang: str) -> Path:
    entries = [_field(OBJ, "TI_Fnt_New__c", "New field", lang=lang)]
    parent, fields = patch_translation(org_rec(), entries)
    root = tmp / "force-app/main/default/objectTranslations"
    write_translation_dir(root, OBJ, lang, parent, fields)
    return root / f"{OBJ}-{lang}"


def test_source_format_and_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        folder = _generate_dir(tmp, "en_US")
        names = sorted(p.name for p in folder.iterdir())
        assert names == sorted([
            "TI_Fnt_New__c.fieldTranslation-meta.xml",
            "TI_Fnt_ProductName__c.fieldTranslation-meta.xml",
            "TI_Fnt_Status__c.fieldTranslation-meta.xml",
            f"{OBJ}-en_US.objectTranslation-meta.xml",
        ]), names

        # every file is well-formed, namespaced, correctly rooted
        for p in folder.iterdir():
            root = ET.fromstring(p.read_text(encoding="utf-8"))
            expect = ("CustomObjectTranslation" if p.name.endswith("objectTranslation-meta.xml")
                      else "CustomFieldTranslation")
            assert root.tag == "{http://soap.sforce.com/2006/04/metadata}" + expect

        # children follow the Metadata API sequence
        parent_xml = (folder / f"{OBJ}-en_US.objectTranslation-meta.xml").read_text()
        order = [t.split("}")[-1] for t in
                 [c.tag for c in ET.fromstring(parent_xml)]]
        assert order == sorted(order, key=_cot_rank), order

        # the manifest picks the member up
        out = tmp / "manifest/package.xml"
        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "build_manifest.py"),
             "--source-root", str(tmp / "force-app/main/default"),
             "--project-root", str(tmp), "--out", str(out), "--only", OBJ],
            text=True, capture_output=True)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        pkg = out.read_text(encoding="utf-8")
        assert "<name>CustomObjectTranslation</name>" in pkg
        assert f"<members>{OBJ}-en_US</members>" in pkg
    print("  ok  source-format-and-manifest")


def _cot_rank(tag: str) -> int:
    from translation_lib import COT_CHILD_ORDER
    return COT_CHILD_ORDER.index(tag) if tag in COT_CHILD_ORDER else len(COT_CHILD_ORDER)


def test_other_language_is_independent():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        en = _generate_dir(tmp, "en_US")
        fr = _generate_dir(tmp, "fr")
        assert en.is_dir() and fr.is_dir()
        assert en.name.endswith("-en_US") and fr.name.endswith("-fr")
        entries = entries_from_object_rows(
            [_row("出荷", OBJ, "TI_Fnt_Qty__c", "数量", en="Quantity")], lang="fr")
        assert {e["language"] for e in entries} == {"fr"}
        assert all(e["id"].endswith("|fr") for e in entries)
    print("  ok  other-language-is-independent")


# --------------------------------------------------------------------------- #
# machine independence
# --------------------------------------------------------------------------- #

def test_no_keychain_or_home_assumptions():
    """The translation modules must not read the CLI's auth files directly."""
    banned = (".sfdx", "sf/client/current", "get_token", "orgauth.json")
    for name in ("translation_lib.py", "translation_drift.py",
                 "generate_object_translation.py"):
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        for token in banned:
            assert token not in src, f"{name} still references {token}"
        assert "sf\", \"org\", \"display\"" in src or "org_auth" in src
    print("  ok  no-keychain-or-home-assumptions")


def test_clean_home_generation_without_org():
    """With no --org (and a bare HOME) the generator still produces valid source."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows = tmp / "rows.json"
        rows.write_text(json.dumps([
            _row("出荷", OBJ, "TI_Fnt_Qty__c", "数量", en="Quantity")]),
            encoding="utf-8")
        out = tmp / "objectTranslations"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp / "empty-home"),
               "LANG": "en_US.UTF-8"}
        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_object_translation.py"),
             "--rows", str(rows), "--out-root", str(out)],
            text=True, capture_output=True, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        f = out / f"{OBJ}-en_US/TI_Fnt_Qty__c.fieldTranslation-meta.xml"
        assert f.exists(), cp.stdout
        assert "<label>Quantity</label>" in f.read_text(encoding="utf-8")
    print("  ok  clean-home-generation-without-org")


def test_untranslated_rows_skip_the_org_entirely():
    """No EN column ⇒ the generator exits 0 without ever needing auth."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows = tmp / "rows.json"
        rows.write_text(json.dumps([
            _row("受入", OBJ, "TI_Fnt_ProductName__c", "商品名")]), encoding="utf-8")
        out = tmp / "objectTranslations"
        env = {"PATH": "/nonexistent", "HOME": str(tmp / "empty-home")}
        cp = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_object_translation.py"),
             "--rows", str(rows), "--org", "SOME_SANDBOX", "--out-root", str(out)],
            text=True, capture_output=True, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert not out.exists(), "nothing should be generated for an untranslated tab"
    print("  ok  untranslated-rows-skip-the-org-entirely")


def test_existing_field_en_generates_artifact():
    """Existing object/field + EN in the sheet ⇒ a fieldTranslation file is written."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        entries = [_field(OBJ, "TI_Fnt_ProductName__c", "Product name")]
        parent, fields = patch_translation(None, entries)
        root = tmp / "objectTranslations"
        write_translation_dir(root, OBJ, "en_US", parent, fields)
        f = root / f"{OBJ}-en_US" / "TI_Fnt_ProductName__c.fieldTranslation-meta.xml"
        assert f.exists()
        assert "<label>Product name</label>" in f.read_text(encoding="utf-8")
    print("  ok  existing-field-en-generates-artifact")


def test_duplicate_and_invalid_fail_validation():
    """Sheet defects are ERRORs on the existing validate_sheet Report."""
    import validate_sheet

    dup_rows = [
        {"_type": "object_meta", "_SheetName": "出荷", "Object API Name": OBJ,
         "Object Label": "出荷", EN_FLAG: True, "Object Label (EN)": "Shipout"},
        _row("出荷", OBJ, "TI_Fnt_Status__c", "状態", en="Status"),
        _row("出荷", OBJ, "TI_Fnt_Status__c", "状態", en="Status"),
    ]
    rep = validate_sheet.Report()
    validate_sheet.validate_translations(dup_rows, rep)
    dups = [i for i in rep.items if i["check"] == "translation.duplicate"]
    assert dups, rep.items
    assert "Duplicate English translation entries" in dups[0]["message"]
    assert "Object: " + OBJ in dups[0]["message"]
    assert "Field: TI_Fnt_Status__c" in dups[0]["message"]

    bad_rows = [
        {"_type": "object_meta", "_SheetName": "出荷", "Object API Name": OBJ,
         EN_FLAG: True, "Object Label (EN)": "Shipout"},
        _row("出荷", OBJ, "", "幽霊", en="Ghost"),
        _row("出荷", "", "TI_Fnt_Qty__c", "数量", en="Quantity"),
    ]
    # give the blank-object row the EN flag so it is treated as translated
    bad_rows[-1][EN_FLAG] = True
    bad_rows[-1]["Object API Name"] = ""
    rep2 = validate_sheet.Report()
    validate_sheet.validate_translations(bad_rows, rep2)
    checks = {i["check"] for i in rep2.items}
    assert "translation.field" in checks, rep2.items
    assert "translation.object" in checks, rep2.items
    assert any("invalid/missing field identifier" in i["message"] for i in rep2.items)
    assert any("invalid/missing object identifier" in i["message"] for i in rep2.items)
    print("  ok  duplicate-and-invalid-fail-validation")


def test_no_separate_translation_command():
    """Translations ride the canonical command; there is no translation-deploy CLI."""
    names = {p.name for p in SCRIPTS.glob("*.py")}
    banned = {"deploy_translations.py", "translation_deploy.py",
              "deploy-translations.py", "translation-deploy.py"}
    assert not (names & banned), names & banned
    prep = (SCRIPTS / "prep_deploy.py").read_text(encoding="utf-8")
    assert "generate_object_translation.py" in prep
    assert "CANONICAL DEPLOY ENTRY POINT" in prep
    for token in ("deploy-translations", "translation-deploy", "deploy_translations"):
        assert token not in prep
    print("  ok  no-separate-translation-command")


def test_translation_unavailable_is_actionable():
    from translation_lib import MetadataApiError, TranslationUnavailable, read_object_translations
    import translation_lib as tl

    real = tl.read_metadata
    tl.read_metadata = lambda *a, **k: (_ for _ in ()).throw(
        MetadataApiError("INVALID_TYPE: This type of metadata is not available "
                         "for this organization"))
    try:
        try:
            read_object_translations([OBJ], "en_US",
                                     {"accessToken": "x", "instanceUrl": "y",
                                      "apiVersion": "60.0"})
            raise AssertionError("expected TranslationUnavailable")
        except TranslationUnavailable as e:
            msg = str(e)
            assert "Translation Language" in msg and "en_US" in msg
            assert "--on-unavailable skip" in msg
    finally:
        tl.read_metadata = real
    print("  ok  translation-unavailable-is-actionable")


def main() -> int:
    print("test_translation_delta")
    for fn in (
        test_japan_adds_one_field,
        test_changed_en_is_packaged_by_default,
        test_changed_en_not_packaged_new_only,
        test_matching_en_is_a_noop,
        test_wip_and_isdelete_rows_are_not_catalogued,
        test_schema_missing_is_not_packaged,
        test_hash_utf8,
        test_patch_preserves_all_org_translations,
        test_name_field_label_round_trip,
        test_object_label_patch_keeps_variants,
        test_tab_without_en_column_produces_nothing,
        test_mixed_tabs_only_translated_one_contributes,
        test_legacy_rows_without_flag_still_work,
        test_picklist_parse_error_is_validation_error,
        test_sync_state_scoped_by_org_id,
        test_source_format_and_manifest,
        test_other_language_is_independent,
        test_no_keychain_or_home_assumptions,
        test_clean_home_generation_without_org,
        test_untranslated_rows_skip_the_org_entirely,
        test_existing_field_en_generates_artifact,
        test_duplicate_and_invalid_fail_validation,
        test_no_separate_translation_command,
        test_translation_unavailable_is_actionable,
    ):
        fn()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
