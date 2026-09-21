"""Unit tests for translation primitives (no org, no MCP, no sheet)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from translation_lib import (
    CHANGED,
    FIELD_EN_HEADER,
    KIND_OBJECT_FIELD,
    NEW,
    PROVENANCE_HEADER,
    UNCHANGED,
    build_glossary,
    classify_need,
    content_hash,
    format_provenance,
    glossary_lookup,
    google_formula,
    invalid_english,
    parse_provenance,
    plan_missing_headers,
    starts_with_for,
)


def test_hash_stable_and_normalized():
    assert content_hash("商品名") == content_hash("  商品名  ")
    assert content_hash("商品名") != content_hash("Product name")
    assert content_hash("") == ""


def test_invalid_english_blocks_blank_formula_api_and_jp_copy():
    assert "blank" in invalid_english("")
    assert "formula error" in invalid_english("#VALUE!")
    assert "formula text" in invalid_english("=GOOGLETRANSLATE(A1,\"ja\",\"en\")")
    assert "API name" in invalid_english("TI_Fnt_Status__c", api="TI_Fnt_Status__c")
    assert "Japanese source" in invalid_english("ステータス", ja="ステータス")
    assert invalid_english("Status", ja="ステータス", api="TI_Fnt_Status__c") == ""
    # Latin JA (already English) may equal EN
    assert invalid_english("SKU", ja="SKU") == ""


def test_google_formula_uses_cell_ref():
    f = google_formula("C12")
    assert 'GOOGLETRANSLATE(C12,"ja","en")' in f
    assert f.startswith("=IF(C12=")


def test_existing_unknown_provenance_is_manual_backfill():
    assert classify_need("ステータス", "Status", "", "") == "provenance_backfill"


def test_blank_en_is_translate():
    assert classify_need("ステータス", "", "", "") == "translate"


def test_ja_change_replaces_even_manual():
    old = content_hash("旧ラベル")
    assert classify_need("新ラベル", "Old Label", old, "manual") == "translate"


def test_unchanged_ja_not_retranslated():
    h = content_hash("ステータス")
    assert classify_need("ステータス", "Status", h, "google") == "skip"
    assert classify_need("ステータス", "Status", h, "deepl") == "skip"


def test_blank_ja_blocks():
    assert classify_need("", "Status", "", "manual") == "block_blank_ja"


def test_glossary_api_beats_label_and_ambiguous_goes_to_provider():
    rows = [
        {"Field API Name": "TI_Fnt_Status__c", "Field Label": "ステータス",
         "Field Label (EN)": "Status"},
        {"Field API Name": "TI_Fnt_Other__c", "Field Label": "備考",
         "Field Label (EN)": "Remarks"},
        {"Field API Name": "TI_Fnt_Note__c", "Field Label": "備考",
         "Field Label (EN)": "Notes"},  # ambiguous 備考
    ]
    g = build_glossary(rows)
    en, why = glossary_lookup(g, api="TI_Fnt_Status__c", ja="ステータス")
    assert en == "Status" and why == "glossary.api"
    en, why = glossary_lookup(g, api="TI_Fnt_New__c", ja="備考")
    assert en == "" and why == "glossary.ambiguous"
    en, why = glossary_lookup(g, api="Name", ja="Name")
    assert en == "Name" and why == "glossary.standard"


def test_missing_headers_use_spare_gdc_not_insert():
    header = ["No.", "label", "fullName", "type", "FreeColumnGDC3",
              "FreeColumnGDC4", "FreeColumnGDC5", "FreeColumnGDC6"]
    plan = plan_missing_headers(header)
    assert FIELD_EN_HEADER in plan
    assert PROVENANCE_HEADER in plan
    assert len(plan) == 2
    assert plan[FIELD_EN_HEADER] == header.index("FreeColumnGDC3")
    # Provenance is the column immediately right of Field Label (EN)
    assert plan[PROVENANCE_HEADER] == plan[FIELD_EN_HEADER] + 1


def test_provenance_immediately_right_of_existing_en():
    header = ["No.", "label", "fullName", "type",
              "Field Label (EN)", "FreeColumnGDC4", "FreeColumnGDC5"]
    plan = plan_missing_headers(header)
    assert FIELD_EN_HEADER not in plan
    assert plan[PROVENANCE_HEADER] == header.index("Field Label (EN)") + 1
    assert header[plan[PROVENANCE_HEADER]] == "FreeColumnGDC4"


def test_provenance_does_not_overwrite_fullname():
    header = ["No.", "label", "Field Label (EN)", "fullName", "type", "FreeColumnGDC4"]
    plan = plan_missing_headers(header)
    assert plan[PROVENANCE_HEADER] == header.index("FreeColumnGDC4")
    assert plan[PROVENANCE_HEADER] != header.index("fullName")


def test_provenance_roundtrip_one_cell():
    packed = format_provenance("manual", "abc123", "2026-09-21T10:00:00Z")
    assert packed == "manual | abc123 | 2026-09-21T10:00:00Z"
    assert parse_provenance(packed) == ("manual", "abc123", "2026-09-21T10:00:00Z")
    assert parse_provenance("deepl") == ("deepl", "", "")
    assert parse_provenance("") == ("", "", "")


def test_starts_with():
    assert starts_with_for("Product") == "Consonant"
    assert starts_with_for("Import") == "Vowel"
