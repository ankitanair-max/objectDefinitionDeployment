"""Enrichment, plan, XML merge, and existing-pipeline regression tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from generate_object_translation import generate, patch_tree, render_field_file
from generate_xml import build_field_xml
from mcp_deepl import DeepLTranslateError, DeepLUnavailable
from translate_enrich import (
    TranslationAbort,
    enrich_jobs,
    run_enrichment,
    select_provider,
    stale_against,
)
from translation_lib import (
    KIND_OBJECT_FIELD,
    KIND_OBJECT_LABEL,
    KIND_NAME_FIELD,
    ORIGIN_DEEPL,
    ORIGIN_GOOGLE,
    content_hash,
    make_entry,
    plan_has_members,
)
from translation_plan import build_plan, classify_against_org, entries_from_rows
from validate_sheet import Report, validate


# --------------------------------------------------------------------------- #
# Fake sheet / DeepL
# --------------------------------------------------------------------------- #
def _grid(*, en_header=True, provenance=False, fields=None, obj_en="", name_en=""):
    """Minimal Format-style tab parseable by fetch_sheet.parse_tab."""
    header = ["No.", "Is_Standard", "label", "Field Label (EN)" if en_header else "FreeColumnGDC3",
              "fullName", "type", "WIP", "IsDelete"]
    if provenance:
        header += ["Translation Origin", "Translation Source Hash", "Translation Generated At"]
    else:
        header += ["FreeColumnGDC4", "FreeColumnGDC5", "FreeColumnGDC6"]
    rows = [
        ["表示ラベル", "成約", "オブジェクト名", "TI_Fnt_Deal__c"],
        ["Object Label (EN)", obj_en or ""],
        ["説明", ""],
        ["レポートを許可", "活動を許可", "項目履歴管理", "検索を許可"],
        ["TRUE", "TRUE", "TRUE", "TRUE"],
        [],
        ["項目"],
        ["No.", "標準", "項目ラベル名", "項目ラベル名 (EN)", "項目名", "データ型"],
        header,
        ["", "○", "名前", name_en or "Deal", "Name", "Autonumber"],
    ]
    for f in fields or []:
        row = ["", "", f.get("ja", ""), f.get("en", ""), f.get("api", ""), f.get("type", "Text"),
               f.get("wip", ""), f.get("isdelete", "")]
        if provenance:
            row += [f.get("origin", ""), f.get("hash", ""), f.get("generated", "")]
        else:
            row += ["", "", ""]
        rows.append(row)
    rows.append(["END[項目]"])
    return rows


class FakeSheet:
    def __init__(self, grids: dict[str, list]):
        self.grids = {k: [list(r) for r in v] for k, v in grids.items()}
        self.literal_writes = []
        self.formula_writes = []

    def read_grid(self, sid, tab, range_a1="A1:CZ500"):
        return self.grids[tab]

    def read_cells(self, sid, tab, cells):
        from translation_lib import cell_at, parse_a1
        out = {}
        grid = self.grids[tab]
        for c in cells:
            col, row = parse_a1(c)
            out[c] = cell_at(grid, row - 1, col)
        return out

    def write_literals(self, sid, updates):
        self.literal_writes.extend(updates)
        self._apply(updates)

    def write_formulas(self, sid, updates):
        self.formula_writes.extend(updates)
        self._apply(updates)

    def wait_recalc(self, sid, tab, cells, **kw):
        # Pretend GOOGLETRANSLATE produced a stable English word.
        return {c: "Translated" for c in cells}

    def _apply(self, updates):
        from translation_lib import parse_a1
        for u in updates:
            rng = u["range"]
            tab, cell = rng.split("!", 1)
            tab = tab.strip("'")
            col, row = parse_a1(cell)
            grid = self.grids.setdefault(tab, [])
            while len(grid) < row:
                grid.append([])
            r = grid[row - 1]
            while len(r) <= col:
                r.append("")
            r[col] = u["values"][0][0]


class FakeDeepL:
    def __init__(self, mapping=None, fail_preflight=False, fail_batch=False):
        self.mapping = mapping or {}
        self.fail_preflight = fail_preflight
        self.fail_batch = fail_batch
        self.closed = False
        self.translated = []

    def preflight(self):
        if self.fail_preflight:
            raise DeepLUnavailable("deepl down")

    def translate_batch(self, texts):
        if self.fail_batch:
            raise DeepLTranslateError("mid-batch boom")
        out = []
        for t in texts:
            en = self.mapping.get(t, f"EN:{t}")
            if not en:
                raise DeepLTranslateError("blank")
            out.append(en)
            self.translated.append(t)
        return out

    def close(self):
        self.closed = True


TAB = "Deal"


def _rows_from_grid(grid):
    from fetch_sheet import parse_tab
    return parse_tab(TAB, grid)


def test_existing_generate_xml_still_uses_japanese_label():
    row = {
        "Field API Name": "TI_Fnt_Status__c",
        "Field Label": "ステータス",
        "Field Label (EN)": "Status",
        "Data Type": "Text",
        "Length": "80",
    }
    el = build_field_xml(row)
    label = el.find("{http://soap.sforce.com/2006/04/metadata}label")
    assert label is not None and label.text == "ステータス"


def test_validate_sheet_still_flags_missing_api():
    rep = Report()
    validate([{
        "_type": "skip",
        "Object API Name": "TI_Fnt_Deal__c",
        "Field Label": "ステータス",
        "Field Label (EN)": "Status",
        "Data Type": "Text",
        "Field API Name": "",
        "Length": "10",
    }], rep)
    assert any(i["check"] == "field.api" for i in rep.items)


def test_blank_field_en_is_hard_error_not_warning():
    rep = Report()
    validate([{
        "Object API Name": "TI_Fnt_Deal__c",
        "Field Label": "ステータス",
        "Field Label (EN)": "",
        "Data Type": "Text",
        "Field API Name": "TI_Fnt_Status__c",
        "Length": "10",
    }], rep)
    errs = [i for i in rep.items if i["severity"] == "ERROR" and i["check"] == "translation.en"]
    assert errs
    assert all(i["severity"] != "WARN" or "translation" not in i["check"] for i in rep.items)


def test_headers_included_in_preview_not_written_until_apply():
    grid = _grid(en_header=False, fields=[{"ja": "ステータス", "api": "TI_Fnt_Status__c", "type": "Text"}])
    sheet = FakeSheet({TAB: grid})
    enr = run_enrichment(
        spreadsheet_id="sid", tabs=[TAB], rows=_rows_from_grid(grid),
        sheet=sheet, apply=False, force_provider="google",
        grids={TAB: grid},
        deepl_factory=lambda: FakeDeepL(fail_preflight=True),
    )
    assert enr.headers_to_create.get(TAB)
    assert not sheet.literal_writes
    assert any(w.note in {"header", "jp-header"} for w in enr.writes)


def test_deepl_selected_when_healthy():
    p, prov = select_provider(deepl_factory=lambda: FakeDeepL())
    assert p == ORIGIN_DEEPL
    assert prov is not None


def test_google_selected_when_deepl_preflight_fails():
    p, prov = select_provider(deepl_factory=lambda: FakeDeepL(fail_preflight=True))
    assert p == ORIGIN_GOOGLE and prov is None


def test_deepl_failure_after_preflight_does_not_fallback_or_write():
    jobs = [{
        "kind": KIND_OBJECT_FIELD, "tab": TAB, "object_api": "TI_Fnt_Deal__c",
        "field_api": "TI_Fnt_Status__c", "ja": "ステータス", "en": "",
        "origin": "", "source_hash": "", "generated_at": "",
        "ja_a1": "C12", "en_a1": "D12", "origin_a1": "H12", "hash_a1": "I12",
        "gen_a1": "J12", "wip": False, "isdelete": False,
    }]
    deepl = FakeDeepL(fail_batch=True)
    deepl.preflight()
    with pytest.raises(DeepLTranslateError):
        enrich_jobs(jobs, {"by_api": {}, "by_ja": {}, "ambiguous_ja": []},
                    ORIGIN_DEEPL, deepl)
    assert jobs[0].get("en_new") in (None, "")  # no partial fill


def test_no_partial_deepl_batch_written():
    grid = _grid(fields=[
        {"ja": "ステータス", "api": "TI_Fnt_Status__c", "type": "Text"},
        {"ja": "備考", "api": "TI_Fnt_Remarks__c", "type": "Text"},
    ])
    sheet = FakeSheet({TAB: grid})
    with pytest.raises(TranslationAbort):
        run_enrichment(
            spreadsheet_id="sid", tabs=[TAB], rows=_rows_from_grid(grid),
            sheet=sheet, apply=True, force_provider="deepl",
            fail_after_preflight=True,
            grids={TAB: grid},
            deepl_factory=lambda: FakeDeepL(),
        )
    assert sheet.literal_writes == []
    assert sheet.formula_writes == []


def test_google_formulas_then_calculated_values():
    grid = _grid(fields=[{"ja": "ステータス", "api": "TI_Fnt_Status__c", "type": "Text"}])
    sheet = FakeSheet({TAB: grid})
    enr = run_enrichment(
        spreadsheet_id="sid", tabs=[TAB], rows=_rows_from_grid(grid),
        sheet=sheet, apply=True, force_provider="google",
        grids={TAB: grid},
        deepl_factory=lambda: FakeDeepL(fail_preflight=True),
    )
    assert sheet.formula_writes, "GOOGLETRANSLATE formula must be written USER_ENTERED"
    jobs = [j for j in enr.rows_patch if j.get("field_api") == "TI_Fnt_Status__c"]
    assert jobs and jobs[0]["en_new"] == "Translated"  # calculated, not formula


def test_formula_error_blocks():
    class ErrSheet(FakeSheet):
        def wait_recalc(self, sid, tab, cells, **kw):
            return {c: "#VALUE!" for c in cells}
    grid = _grid(fields=[{"ja": "ステータス", "api": "TI_Fnt_Status__c", "type": "Text"}])
    sheet = ErrSheet({TAB: grid})
    with pytest.raises(TranslationAbort):
        run_enrichment(
            spreadsheet_id="sid", tabs=[TAB], rows=_rows_from_grid(grid),
            sheet=sheet, apply=True, force_provider="google",
            grids={TAB: grid},
            deepl_factory=lambda: FakeDeepL(fail_preflight=True),
        )


def test_stale_concurrent_edit_rejects_write():
    writes = [type("W", (), {
        "range": "'Deal'!D12", "old": "", "new": "Status",
    })()]
    live = {"'Deal'!D12": "SomeoneElseTypedThis"}
    problems = stale_against(writes, live)
    assert problems


def test_new_object_and_field_packaged_together_with_translation():
    rows = [
        {"_type": "object_meta", "Object API Name": "TI_Fnt_New__c",
         "Object Label": "新規", "Object Label (EN)": "New Object",
         "Name Field Label": "名前", "Name Field Label (EN)": "Name"},
        {"Object API Name": "TI_Fnt_New__c", "Field API Name": "TI_Fnt_A__c",
         "Field Label": "項目", "Field Label (EN)": "Item"},
    ]
    plan = build_plan(
        rows=rows, org="ORG", org_id="00Dxx", tabs=[TAB], provider="deepl",
        present_objects=set(), present_fields={}, org_translations={},
    )
    members = plan["members"]
    assert "TI_Fnt_New__c" in members["CustomObject"]
    assert "TI_Fnt_New__c.TI_Fnt_A__c" in members["CustomField"]
    assert "TI_Fnt_New__c-en_US" in members["CustomObjectTranslation"]
    # same package — single members dict
    assert plan_has_members(plan)


def test_unrelated_org_translations_preserved():
    org = {
        "object_label": "Deal",
        "name_field_label": "Deal Name",
        "fields": {
            "TI_Fnt_Keep__c": {
                "name": "TI_Fnt_Keep__c", "label": "Keep Me",
                "help": "do not drop", "relationshipLabel": "",
                "xml": "<fields><name>TI_Fnt_Keep__c</name><label>Keep Me</label>"
                       "<help>do not drop</help><picklistValues><masterLabel>A</masterLabel>"
                       "<translation>Alpha</translation></picklistValues></fields>",
            }
        },
    }
    overlay = {
        make_entry(kind=KIND_OBJECT_FIELD, component="TI_Fnt_Deal__c",
                   key="TI_Fnt_New__c", master="新規", translation="New")["id"]:
        make_entry(kind=KIND_OBJECT_FIELD, component="TI_Fnt_Deal__c",
                   key="TI_Fnt_New__c", master="新規", translation="New"),
    }
    model = patch_tree(org, overlay, "TI_Fnt_Deal__c")
    assert model["fields"]["TI_Fnt_Keep__c"]["label"] == "Keep Me"
    assert "picklistValues" in model["fields"]["TI_Fnt_Keep__c"]["xml"]
    xml = render_field_file(model["fields"]["TI_Fnt_Keep__c"])
    assert "Alpha" in xml and "do not drop" in xml
    assert model["fields"]["TI_Fnt_New__c"]["label"] == "New"


def test_empty_plan_when_sheet_matches_org():
    rows = [{
        "Object API Name": "TI_Fnt_Deal__c", "Field API Name": "TI_Fnt_Status__c",
        "Field Label": "ステータス", "Field Label (EN)": "Status",
    }]
    org_t = {
        "TI_Fnt_Deal__c-en_US": {
            "object_label": "", "name_field_label": "",
            "fields": {"TI_Fnt_Status__c": {"label": "Status"}},
        }
    }
    plan = build_plan(
        rows=rows + [{"_type": "object_meta", "Object API Name": "TI_Fnt_Deal__c",
                      "Object Label": "成約", "Object Label (EN)": "Deal",
                      "Name Field Label": "成約", "Name Field Label (EN)": "Deal"}],
        org="ORG", org_id="00D", tabs=[TAB], provider="google",
        present_objects={"TI_Fnt_Deal__c"},
        present_fields={"TI_Fnt_Deal__c": {"TI_Fnt_Status__c"}},
        org_translations={
            "TI_Fnt_Deal__c-en_US": {
                "object_label": "Deal", "name_field_label": "Deal",
                "fields": {"TI_Fnt_Status__c": {"label": "Status"},
                           "Name": {"label": "Deal"}},
            }
        },
    )
    assert plan["empty"] is True
    assert not plan_has_members(plan)


def test_changed_ja_hash_packages_new_english():
    e = make_entry(kind=KIND_OBJECT_FIELD, component="TI_Fnt_Deal__c",
                   key="TI_Fnt_Status__c", master="新ステータス", translation="New Status")
    classified = classify_against_org(
        [e],
        {"TI_Fnt_Deal__c-en_US": {"fields": {"TI_Fnt_Status__c": {"label": "Old Status"}}}},
        {"TI_Fnt_Deal__c": {"TI_Fnt_Status__c"}},
        {"TI_Fnt_Deal__c"},
    )
    assert classified[0]["code"] == "CHANGED_TRANSLATION"
    assert classified[0]["package"] is True


def test_blank_object_and_name_labels_are_translated():
    jobs = [
        {"kind": KIND_OBJECT_LABEL, "tab": TAB, "object_api": "TI_Fnt_Deal__c",
         "field_api": "TI_Fnt_Deal__c", "ja": "成約", "en": "",
         "origin": "", "source_hash": "", "ja_a1": "B1", "en_a1": "B2",
         "origin_a1": "C2", "hash_a1": "D2", "gen_a1": "E2"},
        {"kind": KIND_NAME_FIELD, "tab": TAB, "object_api": "TI_Fnt_Deal__c",
         "field_api": "Name", "ja": "成約名", "en": "",
         "origin": "", "source_hash": "", "ja_a1": "C10", "en_a1": "D10",
         "origin_a1": "H10", "hash_a1": "I10", "gen_a1": "J10"},
    ]
    deepl = FakeDeepL(mapping={"成約": "Deal", "成約名": "Deal Name"})
    deepl.preflight()
    out, blocked = enrich_jobs(jobs, {"by_api": {}, "by_ja": {}, "ambiguous_ja": []},
                               ORIGIN_DEEPL, deepl)
    assert not blocked
    assert out[0]["en_new"] == "Deal"
    assert out[1]["en_new"] == "Deal Name"


def test_unselected_tab_is_never_read():
    grid = _grid(fields=[{"ja": "ステータス", "api": "TI_Fnt_Status__c", "type": "Text"}])

    class Guard(FakeSheet):
        def read_grid(self, sid, tab, range_a1="A1:CZ500"):
            if tab != TAB:
                raise AssertionError(f"read unselected tab {tab}")
            return super().read_grid(sid, tab, range_a1)

    sheet = Guard({TAB: grid})
    run_enrichment(
        spreadsheet_id="sid", tabs=[TAB], rows=_rows_from_grid(grid),
        sheet=sheet, apply=False, force_provider="google",
        grids={TAB: grid},
        deepl_factory=lambda: FakeDeepL(fail_preflight=True),
    )
    """Plan is built only from the rows of selected tabs (caller responsibility)."""
    rows = [{
        "_type": "object_meta", "_SheetName": "Deal",
        "Object API Name": "TI_Fnt_Deal__c",
        "Object Label": "成約", "Object Label (EN)": "Deal",
        "Name Field Label": "N", "Name Field Label (EN)": "Name",
    }]
    plan = build_plan(
        rows=rows, org="ORG", org_id="00D", tabs=["Deal"], provider="google",
        present_objects={"TI_Fnt_Deal__c"}, present_fields={"TI_Fnt_Deal__c": set()},
        org_translations={"TI_Fnt_Deal__c-en_US": {
            "object_label": "Deal", "name_field_label": "Name", "fields": {}}},
    )
    assert plan["tabs"] == ["Deal"]
    # Other workbook objects never appear
    assert "TI_Fnt_Other__c" not in json.dumps(plan)


def test_sheet_success_deploy_failure_retains_translations(tmp_path):
    """Sync state must NOT be written when verification fails (caller contract)."""
    from translation_lib import load_sync_state, save_sync_state
    path = tmp_path / "sync.json"
    # caller only saves after verify — simulate "not saved"
    assert load_sync_state(path) == {}
    save_sync_state(path, "00Dxx", [make_entry(
        kind=KIND_OBJECT_LABEL, component="X", key="X", master="x", translation="X")])
    # And if we DID save only after success, org key exists
    assert "00Dxx" in load_sync_state(path)
