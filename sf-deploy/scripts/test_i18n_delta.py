#!/usr/bin/env python3
"""Self-test: translation delta (NEW field add vs already-translated object).

Run from sf-deploy/:  python3 scripts/test_i18n_delta.py
No org, no sheet — exercises classify + apply_new_only + merge.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "scripts")
from i18n_lib import (  # noqa: E402
    CHANGED, KIND_OBJECT_FIELD, MISSING, NEW, UNCHANGED, apply_new_only,
    classify, content_hash, make_entry,
)
from generate_object_translation import _merge_org  # noqa: E402


def _field(obj, api, en, source="sheet"):
    return make_entry(kind=KIND_OBJECT_FIELD, component=obj, aspect="label",
                      key=api, language="en_US", master="JA", translation=en,
                      source=source)


def test_japan_adds_one_field():
    """Object already has translations; Japan adds TI_Fnt_New__c with col D filled."""
    obj = "TI_Fnt_ShipoutMovein__c"
    existing = _field(obj, "TI_Fnt_ProductName__c", "Product name")
    existing["source"] = "org"
    org = {existing["id"]: existing}

    sheet = [
        _field(obj, "TI_Fnt_ProductName__c", "Product name"),  # unchanged
        _field(obj, "TI_Fnt_New__c", "New field"),             # Japan add
        _field(obj, "TI_Fnt_Blank__c", ""),                    # EN not filled
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
    assert "ObjectField|TI_Fnt_ShipoutMovein__c|label|TI_Fnt_ProductName__c|en_US" in merged
    assert merged["ObjectField|TI_Fnt_ShipoutMovein__c|label|TI_Fnt_New__c|en_US"]["translation"] == "New field"
    print("  ok  japan-adds-one-field")


def test_changed_en_not_packaged_new_only():
    obj = "TI_Fnt_Deal__c"
    org_e = _field(obj, "TI_Fnt_Status__c", "Status")
    org = {org_e["id"]: org_e}
    sheet = [_field(obj, "TI_Fnt_Status__c", "Deal status")]  # edited EN
    classified = apply_new_only(classify(sheet, org))
    rec = classified[0]
    assert rec["code"] == CHANGED
    assert rec["package"] is False
    print("  ok  changed-en-not-packaged")


def test_hash_utf8():
    assert content_hash("商品名") == content_hash("商品名")
    assert content_hash("商品名") != content_hash("Product name")
    print("  ok  utf8-hash")


def main() -> int:
    print("test_i18n_delta")
    test_japan_adds_one_field()
    test_changed_en_not_packaged_new_only()
    test_hash_utf8()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
