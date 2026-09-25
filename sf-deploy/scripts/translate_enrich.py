#!/usr/bin/env python3
"""
translate_enrich.py — JA→en_US labels inside prep_deploy (sheet write + deploy plan).

Dry-run by default; `--apply` writes the confirmed batch after a stale-check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sx
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_deepl import DeepLProvider, DeepLTranslateError, DeepLUnavailable
from secret_resolver import isolated_child_env
from write_back import get_write_service

NS = "http://soap.sforce.com/2006/04/metadata"

KIND_OBJECT_LABEL = "ObjectLabel"
KIND_NAME_FIELD = "NameField"
KIND_OBJECT_FIELD = "ObjectField"

NEW = "NEW_TRANSLATION"
CHANGED = "CHANGED_TRANSLATION"
UNCHANGED = "UNCHANGED_TRANSLATION"
MISSING = "MISSING_TRANSLATION"
INVALID = "INVALID_TRANSLATION"
WIP = "WIP"
ISDELETE = "ISDELETE"
SCHEMA_MISSING = "SCHEMA_MISSING"
CONFLICT = "CONFLICTING_TRANSLATION"
ORG_ONLY = "ORG_ONLY"

ORIGIN_MANUAL = "manual"
ORIGIN_INVENTORY = "inventory"
ORIGIN_DEEPL = "deepl"
ORIGIN_GOOGLE = "google"
VALID_ORIGINS = {ORIGIN_MANUAL, ORIGIN_INVENTORY, ORIGIN_DEEPL, ORIGIN_GOOGLE}

SF_LANG = "en_US"
PROVIDER_SOURCE = "JA"
PROVIDER_TARGET = "EN-US"

LABEL_MAX_LEN = 40  # Custom object + custom field label limit
FORMULA_ERRORS = (
    "#N/A", "#VALUE!", "#REF!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!",
    "#ERROR!", "#ERROR",
)
PLACEHOLDERS = {
    "tbd", "todo", "xxx", "n/a", "na", "t.b.d.",
    "要確認", "未定", "dummy", "placeholder",
}

STANDARD_FIELD_EN = {
    "id": "Id",
    "name": "Name",
    "ownerid": "Owner",
    "createdbyid": "Created By",
    "createddate": "Created Date",
    "lastmodifiedbyid": "Last Modified By",
    "lastmodifieddate": "Last Modified Date",
    "systemmodstamp": "System Modstamp",
    "isdeleted": "Deleted",
    "lastactivitydate": "Last Activity Date",
    "lastvieweddate": "Last Viewed Date",
    "lastreferenceddate": "Last Referenced Date",
    "recordtypeid": "Record Type",
    "currencyisocode": "Currency ISO Code",
}

FIELD_EN_HEADER = "Field Label (EN)"
PROVENANCE_HEADER = "Translation Provenance"
# Legacy 3-column headers — still READ if present, never created.
ORIGIN_HEADER = "Translation Origin"
HASH_HEADER = "Translation Source Hash"
GENERATED_HEADER = "Translation Generated At"
OBJECT_EN_LABEL = "Object Label (EN)"
OBJECT_PROVENANCE_LABEL = "Object Translation Provenance"
OBJECT_ORIGIN_LABEL = "Object Translation Origin"
OBJECT_HASH_LABEL = "Object Translation Source Hash"
OBJECT_GENERATED_LABEL = "Object Translation Generated At"
PROVENANCE_SEP = " | "

# Spare GDC columns (header-driven; AJ–BA = FreeColumnGDC3–20).
SPARE_GDC_HEADERS = [f"FreeColumnGDC{i}" for i in range(3, 21)]

WIP_TRUE = {"x", "true", "1", "yes", "○", "〇"}
DELETE_TRUE = {"true", "1", "yes", "x", "○", "〇"}

_JP_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x4E00, 0x9FFF),  # CJK
    (0xFF66, 0xFF9D),  # halfwidth katakana
)


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def norm(s: Any) -> str:
    t = unicodedata.normalize("NFC", str(s or ""))
    t = t.replace("\u3000", " ").replace("\xa0", " ")
    t = t.translate({0xFF08: ord("("), 0xFF09: ord(")"), 0xFF0F: ord("/")})
    return re.sub(r"\s+", " ", t).strip()


def compare_norm(s: Any) -> str:
    """Comparison-only normalization; never use it to rewrite source text."""
    t = unicodedata.normalize("NFKC", str(s or ""))
    t = t.replace("\u3000", " ").replace("\xa0", " ")
    t = t.translate({0xFF08: ord("("), 0xFF09: ord(")"), 0xFF0F: ord("/")})
    return re.sub(r"\s+", " ", t).strip()


def content_hash(s: str) -> str:
    t = norm(s)
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def has_japanese(s: str) -> bool:
    for ch in s or "":
        o = ord(ch)
        if any(lo <= o <= hi for lo, hi in _JP_RANGES):
            return True
    return False


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truthy(v: Any) -> bool:
    return norm(v).lower() in WIP_TRUE


def is_delete(v: Any) -> bool:
    return norm(v).lower() in DELETE_TRUE


def col_letter(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def a1(col0: int, row1: int) -> str:
    return f"{col_letter(col0)}{row1}"


def parse_a1(cell: str) -> tuple[int, int]:
    m = re.match(r"^\$?([A-Za-z]+)\$?(\d+)$", cell.strip())
    if not m:
        raise ValueError(f"not an A1 cell: {cell!r}")
    letters, row = m.group(1).upper(), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return col - 1, row


def cell_at(grid: list[list[str]], row0: int, col0: int) -> str:
    if row0 < 0 or row0 >= len(grid):
        return ""
    row = grid[row0]
    if col0 < 0 or col0 >= len(row):
        return ""
    return str(row[col0] or "").strip()


def esc(s: str) -> str:
    return sx.escape(norm(s), {"'": "&apos;", '"': "&quot;"})


def google_formula(ja_a1: str) -> str:
    """Sheets formula: skip blanks, JA→en via native GOOGLETRANSLATE."""
    return f'=IF({ja_a1}="","",GOOGLETRANSLATE({ja_a1},"ja","en"))'


# --------------------------------------------------------------------------- #
# English validation
# --------------------------------------------------------------------------- #
def formula_error(value: str) -> str:
    v = norm(value).upper()
    for err in FORMULA_ERRORS:
        if v == err or v.startswith(err):
            return err
    return ""


def looks_like_formula_text(value: str) -> bool:
    return norm(value).startswith("=")


def is_placeholder(value: str) -> bool:
    v = norm(value).lower()
    if v in PLACEHOLDERS:
        return True
    return v.startswith("<") and "未定" in v


def invalid_english(
    en: str,
    *,
    ja: str = "",
    api: str = "",
    allow_same_as_ja_if_latin: bool = True,
) -> str:
    """Return a reason string if `en` must not be deployed, else empty."""
    text = norm(en)
    if not text:
        return "blank English label"
    err = formula_error(text)
    if err:
        return f"Google formula error {err}"
    if looks_like_formula_text(text):
        return "formula text leaked into English (calculated value unread)"
    if is_placeholder(text):
        return f"placeholder English {text!r}"
    if len(text) > LABEL_MAX_LEN:
        return f"English exceeds Salesforce label limit ({len(text)}>{LABEL_MAX_LEN})"
    api_n = norm(api)
    if api_n and text.lower() in {api_n.lower(), api_n.lower().removesuffix("__c")}:
        return "English is the API name"
    ja_n = norm(ja)
    if ja_n and text == ja_n:
        if allow_same_as_ja_if_latin and not has_japanese(ja_n):
            return ""
        return "English is a copy of the Japanese source"
    return ""


def object_en_for_org(header_en: str, name_en: str) -> tuple[str, str]:
    """English packaged as CustomObjectTranslation caseValues.

    Header Field Label (EN) is used when it fits the 40-char object-label
    limit. If it is longer, Name-field English is used instead. Neither cell
    is written back to the sheet.
    """
    header_n, name_n = norm(header_en), norm(name_en)
    if header_n and len(header_n) > LABEL_MAX_LEN:
        return name_n, "name_en"
    return header_n, "header"


_PLURAL_IRREGULAR = {
    "person": "people", "man": "men", "woman": "women",
    "child": "children", "mouse": "mice", "goose": "geese",
    "leaf": "leaves", "life": "lives", "knife": "knives",
}
_VOWELS = set("aeiou")


def _plural_word(word: str) -> str:
    """English plural of one token. Preserves surrounding punctuation."""
    m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", word, flags=re.UNICODE)
    if not m:
        return word + "s"
    pre, core, post = m.group(1), m.group(2), m.group(3)
    if not core:
        return word
    low = core.lower()
    irr = _PLURAL_IRREGULAR.get(low)
    if irr:
        out = irr.upper() if core.isupper() else (irr.capitalize() if core[:1].isupper() else irr)
        return f"{pre}{out}{post}"
    if core.isupper() and core.isalpha() and len(core) <= 6:
        return f"{pre}{core}s{post}"
    # Already plural (details, goods) — do not add "es". "class" (ss) and
    # "bus"/"status" (us) still take the sibilant rule below.
    if low.endswith("s") and not low.endswith(("ss", "us", "is")):
        return word
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return f"{pre}{core}es{post}"
    if len(core) > 1 and low.endswith("y") and low[-2] not in _VOWELS:
        return f"{pre}{core[:-1]}ies{post}"
    return f"{pre}{core}s{post}"


def _pluralize_last_token(phrase: str) -> str:
    tokens = phrase.split(" ")
    tokens[-1] = _plural_word(tokens[-1])
    return " ".join(tokens)


def english_plural_label(singular: str) -> str:
    """Plural English object name for caseValues plural=true. Never written to the sheet.

    Slash compounds pluralize each side's last word. If that exceeds 40 characters,
    only the final word is pluralized so the Salesforce label limit still holds.
    """
    text = norm(singular)
    if not text:
        return ""
    parts = re.split(r"(\s*/\s*)", text)
    built = []
    for p in parts:
        if not p or re.fullmatch(r"\s*/\s*", p):
            built.append(p)
            continue
        built.append(_pluralize_last_token(p))
    plural = "".join(built)
    if plural == text:
        plural = _pluralize_last_token(text)
    if len(plural) > LABEL_MAX_LEN:
        compact = re.sub(r"\s*/\s*", "/", plural)
        if len(compact) <= LABEL_MAX_LEN:
            plural = compact
    if len(plural) > LABEL_MAX_LEN:
        plural = _pluralize_last_token(text)
    if len(plural) > LABEL_MAX_LEN:
        return text
    return plural


# --------------------------------------------------------------------------- #
# Catalog entries
# --------------------------------------------------------------------------- #
def entry_key(kind: str, component: str, key: str, language: str = SF_LANG) -> str:
    return "|".join([kind, component, "label", key, language])


def make_entry(
    *,
    kind: str,
    component: str,
    key: str,
    master: str,
    translation: str,
    language: str = SF_LANG,
    source: str = "",
    origin: str = "",
    source_hash: str = "",
    sheet_row: int = 0,
    extra: dict | None = None,
) -> dict:
    e = {
        "kind": kind,
        "component": norm(component),
        "aspect": "label",
        "key": norm(key),
        "language": language,
        "master": norm(master),
        "translation": norm(translation),
        "hash": content_hash(translation),
        "source_hash": source_hash or content_hash(master),
        "id": entry_key(kind, norm(component), norm(key), language),
        "source": source,
        "origin": origin,
        "sheet_row": sheet_row,
    }
    if extra:
        e.update(extra)
    return e


def in_scope_field(api: str) -> bool:
    a = norm(api)
    if a == "Name":
        return True
    return a.endswith("__c")


# --------------------------------------------------------------------------- #
# Glossary (selected-tab Toray / live-document mappings)
# --------------------------------------------------------------------------- #
def build_glossary(rows: list[dict]) -> dict:
    """Trusted mappings from the LIVE selected tabs only.

    Resolution order later:
      1. unique API-name → EN
      2. standard Salesforce field EN
      3. unique JA → EN (ambiguous JA dropped)
      4. provider
    """
    by_api: dict[str, set[str]] = defaultdict(set)
    by_ja: dict[str, set[str]] = defaultdict(set)

    def _add(api: str, ja: str, en: str) -> None:
        en_n = norm(en)
        if not en_n:
            return
        if invalid_english(en_n, ja=ja, api=api):
            return
        if api:
            by_api[api.lower()].add(en_n)
        if ja:
            by_ja[norm(ja)].add(en_n)

    for r in rows:
        if r.get("_type") == "object_meta":
            api = norm(r.get("Object API Name"))
            _add(api, r.get("Object Label", ""), r.get("Object Label (EN)", ""))
            _add("Name", r.get("Name Field Label", ""), r.get("Name Field Label (EN)", ""))
            continue
        _add(r.get("Field API Name", ""), r.get("Field Label", ""), r.get("Field Label (EN)", ""))
    unique_api = {k: next(iter(v)) for k, v in by_api.items() if len(v) == 1}
    unique_ja = {k: next(iter(v)) for k, v in by_ja.items() if len(v) == 1}
    ambiguous_ja = sorted(k for k, v in by_ja.items() if len(v) > 1)
    return {
        "by_api": unique_api,
        "by_ja": unique_ja,
        "ambiguous_ja": ambiguous_ja,
    }


def glossary_lookup(glossary: dict, *, api: str = "", ja: str = "") -> tuple[str, str]:
    """Return (english, reason). Empty english → caller uses the provider."""
    api_n = norm(api)
    ja_n = norm(ja)
    if api_n:
        hit = glossary.get("by_api", {}).get(api_n.lower())
        if hit:
            return hit, "glossary.api"
        std = STANDARD_FIELD_EN.get(api_n.lower())
        # Name is in translation scope and often has a JP label; only use the
        # Salesforce default when the source is already Latin/English.
        if std and not has_japanese(ja_n):
            return std, "glossary.standard"
    if ja_n:
        if ja_n in (glossary.get("ambiguous_ja") or []):
            return "", "glossary.ambiguous"
        hit = glossary.get("by_ja", {}).get(ja_n)
        if hit:
            return hit, "glossary.ja"
    return "", "provider"


# --------------------------------------------------------------------------- #
# Need classification (sheet-side, before provider)
# --------------------------------------------------------------------------- #
def format_provenance(origin: str = "", source_hash: str = "", generated_at: str = "") -> str:
    """Pack origin + JA-hash + timestamp into one sheet cell."""
    o, h, t = norm(origin), norm(source_hash), norm(generated_at)
    if not (o or h or t):
        return ""
    return PROVENANCE_SEP.join((o, h, t))


def parse_provenance(cell: str) -> tuple[str, str, str]:
    """Unpack a combined provenance cell. Also accepts a bare origin token."""
    text = str(cell or "").strip()
    if not text:
        return "", "", ""
    low = text.lower()
    if "origin=" in low or "hash=" in low:
        kv: dict[str, str] = {}
        for line in re.split(r"[\n;]+", text):
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            kv[norm(k).lower()] = norm(v)
        return kv.get("origin", ""), kv.get("hash", ""), kv.get("at") or kv.get("generated") or kv.get("generated_at") or ""
    parts = [p.strip() for p in re.split(r"\s*\|\s*", text)]
    while len(parts) < 3:
        parts.append("")
    origin, source_hash, generated_at = parts[0], parts[1], parts[2]
    if not source_hash and not generated_at and origin.lower() in VALID_ORIGINS:
        return origin.lower(), "", ""
    return origin, source_hash, generated_at


def unpack_provenance(rec: dict, *, packed_key: str, origin_key: str,
                      hash_key: str, gen_key: str) -> None:
    """Fill origin/hash/generated keys from a packed cell, falling back to legacy columns."""
    o, h, t = parse_provenance(rec.get(packed_key) or "")
    if not o:
        o = norm(rec.get(origin_key))
    if not h:
        h = norm(rec.get(hash_key))
    if not t:
        t = norm(rec.get(gen_key))
    rec[origin_key] = o
    rec[hash_key] = h
    rec[gen_key] = t
    if not norm(rec.get(packed_key)):
        rec[packed_key] = format_provenance(o, h, t)


def classify_need(ja: str, en: str, stored_hash: str, origin: str) -> str:
    """What to do for one in-scope label.

    Returns: skip | provenance_backfill | translate | block_blank_ja
    """
    ja_n, en_n = norm(ja), norm(en)
    if not ja_n:
        return "block_blank_ja"
    current = content_hash(ja_n)
    stored = norm(stored_hash)
    if en_n and not stored:
        # First provenance-enabled run: keep EN, stamp origin=manual.
        return "provenance_backfill"
    if en_n and stored and stored == current:
        return "skip"
    # blank EN, OR Japanese source hash changed (including former manual).
    return "translate"


# --------------------------------------------------------------------------- #
# Header / spare-column planning
# --------------------------------------------------------------------------- #
def header_index(header_row: list, *names: str) -> int | None:
    lowered = [norm(c).lower() for c in header_row]
    want = [norm(n).lower() for n in names]
    for w in want:
        if w in lowered:
            return lowered.index(w)
    return None


def spare_gdc_indices(header_row: list) -> list[int]:
    lowered = [norm(c).lower() for c in header_row]
    out = []
    for name in SPARE_GDC_HEADERS:
        key = name.lower()
        if key in lowered:
            out.append(lowered.index(key))
    return out


def _usable_provenance_slot(header_row: list, idx: int, *, taken: set[int]) -> bool:
    """True if we can rename this column to Translation Provenance.

    Must sit immediately right of Field Label (EN) when that cell is a spare or
    blank. Never overwrite fullName / type / label / status columns.
    """
    if idx < 0 or idx in taken:
        return False
    if idx >= len(header_row):
        return True
    h = norm(header_row[idx])
    if not h:
        return True
    hl = h.lower()
    if hl == PROVENANCE_HEADER.lower():
        return True
    return hl.startswith("freecolumngdc")


def plan_missing_headers(header_row: list) -> dict:
    """Return {header: col_index} to create.

    Translation Provenance is always the column immediately to the right of
    Field Label (EN). EN/provenance occupy spare FreeColumnGDC* (or empty)
    cells — never a mid-list insert that would shift fullName.
    """
    assign: dict[str, int] = {}
    taken: set[int] = set()
    spares = spare_gdc_indices(header_row)

    en_col = header_index(header_row, FIELD_EN_HEADER, "field label (en)")
    if en_col is None:
        if spares:
            en_col = spares[0]
        else:
            en_col = len(header_row)
        assign[FIELD_EN_HEADER] = en_col
        taken.add(en_col)

    if header_index(header_row, PROVENANCE_HEADER) is None:
        candidate = en_col + 1
        if _usable_provenance_slot(header_row, candidate, taken=taken):
            assign[PROVENANCE_HEADER] = candidate
        else:
            right = [s for s in spares if s > en_col and s not in taken]
            if right:
                assign[PROVENANCE_HEADER] = right[0]
            else:
                col = max(len(header_row), en_col + 1)
                while col in taken:
                    col += 1
                assign[PROVENANCE_HEADER] = col
    return assign


# --------------------------------------------------------------------------- #
# Org translation snapshot (preserve unrelated nodes)
# --------------------------------------------------------------------------- #
def strip_soap_ns(xml: str) -> str:
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)
    xml = re.sub(r"<(/?)\w+:", r"<\1", xml)
    xml = re.sub(r"\s\w+:(\w+=)", r" \1", xml)
    return xml


def _text_and_comment(inner: str) -> tuple[str, str]:
    """Split element inner XML into (text, first comment body).

    Translation Workbench stores untranslated labels as comments
    (``<!-- 日本語 -->``) with empty text. ElementTree drops comments, so
    callers must pass the raw SOAP fragment.
    """
    comments = [norm(c) for c in re.findall(r"<!--(.*?)-->", inner or "", flags=re.DOTALL)]
    stripped = re.sub(r"<!--.*?-->", "", inner or "", flags=re.DOTALL)
    stripped = re.sub(r"<[^>]+>", "", stripped)
    return norm(stripped), (comments[0] if comments else "")


def _direct_tag_text_comment(xml: str, tag: str) -> tuple[str, str]:
    m = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", xml or "", flags=re.DOTALL)
    if not m:
        return "", ""
    return _text_and_comment(m.group(1))


def _first_case_value(xml: str, *, plural: bool) -> tuple[str, str]:
    for inner in re.findall(r"<caseValues>(.*?)</caseValues>", xml or "", flags=re.DOTALL):
        is_plural = bool(re.search(r"<plural>\s*true\s*</plural>", inner, flags=re.I))
        if is_plural != plural:
            continue
        vm = re.search(r"<value>(.*?)</value>", inner, flags=re.DOTALL)
        if vm:
            return _text_and_comment(vm.group(1))
    return "", ""


def parse_object_translation_el(rec: ET.Element, obj: str, lang: str = SF_LANG,
                                raw_xml: str = "") -> dict:
    """SOAP/mdapi CustomObjectTranslation record → overlay-friendly dict.

    Keeps raw field element XML so unrelated picklist/help/rel nodes survive.
    Comment-only labels (untranslated) are stored separately and never treated
    as live English.
    """
    fields: dict[str, dict] = {}
    raw = raw_xml or ET.tostring(rec, encoding="unicode")
    object_label, object_label_comment = _first_case_value(raw, plural=False)
    object_label_plural, object_label_plural_comment = _first_case_value(raw, plural=True)
    if not object_label:
        for cv in rec.findall("caseValues"):
            is_pl = (cv.findtext("plural") or "").lower() == "true"
            val = norm(cv.findtext("value"))
            if val and not is_pl:
                object_label = val
            elif val and is_pl and not object_label_plural:
                object_label_plural = val
    name_field_label, name_field_label_comment = _direct_tag_text_comment(raw, "nameFieldLabel")
    if not name_field_label:
        name_field_label = norm(rec.findtext("nameFieldLabel") or "")
    for f in rec.findall("fields"):
        name = norm(f.findtext("name"))
        if not name:
            continue
        fields[name] = {
            "name": name,
            "label": norm(f.findtext("label")),
            "help": norm(f.findtext("help")),
            "relationshipLabel": norm(f.findtext("relationshipLabel")),
            "xml": ET.tostring(f, encoding="unicode"),
        }
    starts, _starts_comment = _direct_tag_text_comment(raw, "startsWith")
    if not starts:
        starts = norm(rec.findtext("startsWith"))
    return {
        "object": obj,
        "language": lang,
        "object_label": object_label,
        "object_label_comment": object_label_comment,
        "object_label_plural": object_label_plural,
        "object_label_plural_comment": object_label_plural_comment,
        "name_field_label": name_field_label,
        "name_field_label_comment": name_field_label_comment,
        "startsWith": starts,
        "fields": fields,
        "raw_xml": raw,
    }


def starts_with_for(english: str) -> str:
    ch = (english or "A")[0]
    if ch.lower() in "aeiou":
        return "Vowel"
    if ch.isalpha():
        return "Consonant"
    return "Special"


# --------------------------------------------------------------------------- #
# Deploy plan
# --------------------------------------------------------------------------- #
def empty_plan(*, org: str = "", org_id: str = "", tabs: list | None = None,
               provider: str = "") -> dict:
    return {
        "org": org,
        "orgId": org_id,
        "tabs": list(tabs or []),
        "provider": provider,
        "language": SF_LANG,
        "members": {},
        "translations": [],
        "schema": {"new_objects": [], "new_fields": [], "existing_objects": []},
        "objectMasters": {},
        "empty": True,
    }


def plan_has_members(plan: dict) -> bool:
    members = plan.get("members") or {}
    return any(members.get(k) for k in members)


def add_member(plan: dict, mtype: str, name: str) -> None:
    plan.setdefault("members", {}).setdefault(mtype, [])
    if name not in plan["members"][mtype]:
        plan["members"][mtype].append(name)
    plan["empty"] = False


# --------------------------------------------------------------------------- #
# Sync state (local, keyed by immutable org Id — supplementary to sheet)
# --------------------------------------------------------------------------- #
def load_sync_state(path: str | Path = ".build/translation_sync_state.json") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sync_state(path: str | Path, org_id: str, entries: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_sync_state(p)
    data[org_id] = {
        "orgId": org_id,
        "updatedAt": now_iso(),
        "entries": {e["id"]: {"hash": e.get("hash"), "translation": e.get("translation")}
                    for e in entries if e.get("id")},
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Metadata API (FLS-independent)
# --------------------------------------------------------------------------- #
def load_token(org: str, token_file: str = ".build/orgauth.json") -> dict:
    import subprocess, sys
    cp = subprocess.run(
        ["python3", "scripts/get_token.py", "--alias", org, "--out", token_file],
        text=True, capture_output=True, env=isolated_child_env())
    if cp.returncode != 0:
        raise SystemExit(f"❌ get_token failed: {cp.stderr[:400] or cp.stdout[:400]}")
    return json.loads(Path(token_file).read_text())["result"]


def read_metadata(mtype: str, full_names: list[str], tok: str, inst: str,
                  ver: str) -> list[tuple[ET.Element, str]]:
    import urllib.error, urllib.request
    records: list[tuple[ET.Element, str]] = []
    names = [n for n in full_names if n]
    for i in range(0, len(names), 10):
        chunk = names[i:i + 10]
        members = "".join(f"<met:fullNames>{sx.escape(n)}</met:fullNames>" for n in chunk)
        soap = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:met="http://soap.sforce.com/2006/04/metadata"><soapenv:Header>'
            f"<met:SessionHeader><met:sessionId>{tok}</met:sessionId></met:SessionHeader>"
            "</soapenv:Header><soapenv:Body><met:readMetadata>"
            f"<met:type>{mtype}</met:type>{members}"
            "</met:readMetadata></soapenv:Body></soapenv:Envelope>"
        )
        req = urllib.request.Request(
            f"{inst}/services/Soap/m/{ver}", data=soap.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})
        try:
            xml = urllib.request.urlopen(req, timeout=180).read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise SystemExit(f"❌ readMetadata {mtype} HTTP {e.code}: {body[:400]}")
        stripped = strip_soap_ns(xml)
        for m in re.finditer(r"<records\b[^>]*>.*?</records>", stripped, flags=re.DOTALL):
            frag = m.group(0)
            try:
                rec = ET.fromstring(frag)
            except ET.ParseError:
                continue
            if rec.find("fullName") is not None or rec.find("fields") is not None \
                    or rec.find("caseValues") is not None \
                    or rec.find("nameFieldLabel") is not None:
                records.append((rec, frag))
    return records


def org_id_from_display(org: str) -> str:
    import subprocess
    cp = subprocess.run(
        ["sf", "org", "display", "--target-org", org, "--json"],
        text=True, capture_output=True, env=isolated_child_env())
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    res = data.get("result") or {}
    return str(res.get("id") or res.get("orgId") or "")


def jsonable_translation(model: dict) -> dict:
    """Drop raw XML elements so the snapshot can be serialized."""
    fields = {}
    for name, f in (model.get("fields") or {}).items():
        fields[name] = {
            "name": f.get("name"),
            "label": f.get("label"),
            "help": f.get("help"),
            "relationshipLabel": f.get("relationshipLabel"),
            "xml": f.get("xml") or "",
        }
    return {
        "object": model.get("object"),
        "language": model.get("language"),
        "object_label": model.get("object_label"),
        "object_label_comment": model.get("object_label_comment"),
        "object_label_plural": model.get("object_label_plural"),
        "object_label_plural_comment": model.get("object_label_plural_comment"),
        "name_field_label": model.get("name_field_label"),
        "startsWith": model.get("startsWith"),
        "fields": fields,
    }



# --- deploy plan (sheet × org) ---

def entries_from_rows(rows: list[dict], lang: str = SF_LANG) -> list[dict]:
    out = []
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if not obj:
            continue
        src = f"object_tab:{r.get('_SheetName') or obj}"
        if r.get("_type") == "object_meta":
            obj_en, _via = object_en_for_org(
                r.get("Object Label (EN)"), r.get("Name Field Label (EN)"))
            if obj_en:
                # Header-level object name has no provenance stamp.
                out.append(make_entry(
                    kind=KIND_OBJECT_LABEL, component=obj, key=obj, language=lang,
                    master=norm(r.get("Object Label")),
                    translation=obj_en,
                    source=src, origin="", source_hash="",
                    extra={"translation_plural": english_plural_label(obj_en)},
                ))
            out.append(make_entry(
                kind=KIND_NAME_FIELD, component=obj, key="Name", language=lang,
                master=norm(r.get("Name Field Label")),
                translation=norm(r.get("Name Field Label (EN)")),
                source=src, origin=norm(r.get("Name Translation Origin")),
                source_hash=norm(r.get("Name Translation Source Hash")),
            ))
            continue
        api = norm(r.get("Field API Name"))
        if not api.endswith("__c"):
            continue
        out.append(make_entry(
            kind=KIND_OBJECT_FIELD, component=obj, key=api, language=lang,
            master=norm(r.get("Field Label")),
            translation=norm(r.get("Field Label (EN)")),
            source=src, origin=norm(r.get("Translation Origin")),
            source_hash=norm(r.get("Translation Source Hash")),
            sheet_row=int(r.get("_SheetRow") or 0),
        ))
    return out


def classify_against_org(sheet_entries: list[dict], org_models: dict[str, dict],
                         org_fields: dict[str, set[str]],
                         org_objects: set[str],
                         sync_entries: dict | None = None) -> list[dict]:
    out = []
    for e in sheet_entries:
        rec = dict(e)
        obj = e["component"]
        en = e.get("translation") or ""
        bad = invalid_english(en, ja=e.get("master", ""), api=e.get("key", "")) if en else "blank"
        org_model = org_models.get(f"{obj}-{e['language']}") or org_models.get(obj) or {}
        org_label = ""
        if e["kind"] == KIND_OBJECT_LABEL:
            org_label = org_model.get("object_label") or ""
            in_schema = True  # object itself may be new
        elif e["kind"] == KIND_NAME_FIELD:
            org_label = org_model.get("name_field_label") or ""
            fld = (org_model.get("fields") or {}).get("Name") or {}
            org_label = org_label or fld.get("label") or ""
            in_schema = True
        else:
            fld = (org_model.get("fields") or {}).get(e["key"]) or {}
            org_label = fld.get("label") or ""
            present_fields = org_fields.get(obj) or set()
            in_schema = (e["key"] in present_fields) or (obj not in org_objects)
            # new object → field is in THIS plan (not schema-missing)
            if obj not in org_objects:
                in_schema = True
            elif e["key"] not in present_fields:
                # field also being created in this run counts as in-schema
                rec["_field_is_new"] = True
                in_schema = True

        if not en or bad == "blank":
            rec["code"] = MISSING
            rec["package"] = False
            rec["reason"] = "blank or invalid English"
            out.append(rec)
            continue
        if bad and bad != "blank":
            rec["code"] = MISSING
            rec["package"] = False
            rec["reason"] = bad
            out.append(rec)
            continue
        if not in_schema:
            rec["code"] = SCHEMA_MISSING
            rec["package"] = False
            rec["reason"] = "target field absent from org and not in schema plan"
            out.append(rec)
            continue
        sheet_hash = content_hash(en)
        org_matches = bool(org_label) and sheet_hash == content_hash(org_label)
        prev = (sync_entries or {}).get(e["id"]) or {}
        prev_hash = norm(prev.get("hash"))
        last_matches = (not prev_hash) or prev_hash == sheet_hash
        if not org_label:
            rec["code"] = NEW
            rec["package"] = True
            rec["reason"] = "no en_US translation in org"
            out.append(rec)
            continue
        rec["org_translation"] = org_label
        if e["kind"] == KIND_OBJECT_LABEL:
            expected_plural = e.get("translation_plural") or english_plural_label(en)
            rec["translation_plural"] = expected_plural
            org_plural = org_model.get("object_label_plural") or ""
            rec["org_translation_plural"] = org_plural
            plural_matches = bool(expected_plural) and content_hash(org_plural) == content_hash(expected_plural)
        else:
            plural_matches = True
        if org_matches and last_matches and plural_matches:
            rec["code"] = UNCHANGED
            rec["package"] = False
            rec["reason"] = "sheet matches org"
            out.append(rec)
            continue
        rec["code"] = CHANGED
        rec["package"] = True
        if e["kind"] == KIND_OBJECT_LABEL and org_matches and not plural_matches:
            rec["reason"] = "object plural English differs from org"
        else:
            rec["reason"] = (
                "sheet English differs from org" if not org_matches
                else "sheet English differs from last deployed translation"
            )
        out.append(rec)
    return out


def snapshot_translations(objs: list[str], org: str, lang: str = SF_LANG) -> dict:
    """Live CustomObjectTranslation snapshot, keyed by '<Obj>-en_US'."""
    if not objs:
        return {}
    tokinfo = load_token(org)
    tok, inst, ver = tokinfo["accessToken"], tokinfo["instanceUrl"].rstrip("/"), tokinfo["apiVersion"]
    members = [f"{o}-{lang}" for o in objs]
    recs = read_metadata("CustomObjectTranslation", members, tok, inst, ver)
    out = {}
    # Map by fullName when present.
    for rec, raw in recs:
        full = norm(rec.findtext("fullName") or "")
        obj = full.rsplit("-", 1)[0] if full else ""
        if not obj:
            continue
        model = parse_object_translation_el(rec, obj, lang, raw_xml=raw)
        out[f"{obj}-{lang}"] = jsonable_translation(model)
    return out


def build_plan(
    *,
    rows: list[dict],
    org: str,
    org_id: str,
    tabs: list[str],
    provider: str,
    present_objects: set[str],
    present_fields: dict[str, set[str]],
    org_translations: dict,
    lang: str = SF_LANG,
) -> dict:
    plan = empty_plan(org=org, org_id=org_id, tabs=tabs, provider=provider)
    plan["language"] = lang

    objs = []
    for r in rows:
        if r.get("_type") == "object_meta":
            api = norm(r.get("Object API Name"))
            if api and api not in objs:
                objs.append(api)

    new_objects, existing = [], []
    for o in objs:
        if o in present_objects:
            existing.append(o)
        else:
            new_objects.append(o)
            add_member(plan, "CustomObject", o)
    plan["schema"]["new_objects"] = new_objects
    plan["schema"]["existing_objects"] = existing
    masters = {}
    for r in rows:
        if r.get("_type") != "object_meta":
            continue
        api = norm(r.get("Object API Name"))
        ja = norm(r.get("Object Label"))
        if api and ja:
            masters[api] = ja
    plan["objectMasters"] = masters

    sheet_fields: dict[str, list[str]] = {}
    for r in rows:
        if r.get("_type") == "object_meta":
            continue
        obj = norm(r.get("Object API Name"))
        api = norm(r.get("Field API Name"))
        if obj and api.endswith("__c"):
            sheet_fields.setdefault(obj, []).append(api)

    new_fields = []
    for obj, fields in sheet_fields.items():
        have = present_fields.get(obj) or set()
        for f in fields:
            if obj in new_objects or f not in have:
                new_fields.append(f"{obj}.{f}")
                add_member(plan, "CustomField", f"{obj}.{f}")
                if obj in new_objects:
                    add_member(plan, "CustomObject", obj)
    plan["schema"]["new_fields"] = new_fields

    entries = entries_from_rows(rows, lang=lang)
    sync = (load_sync_state().get(org_id) or {}).get("entries") or {}
    classified = classify_against_org(
        entries, org_translations, present_fields, present_objects, sync_entries=sync)
    plan["translations"] = classified

    packaged_objs = set()
    for rec in classified:
        if rec.get("package"):
            packaged_objs.add(rec["component"])
            # Keep dependent schema + translation in the SAME package.
            if rec["component"] in new_objects:
                add_member(plan, "CustomObject", rec["component"])
            if rec["kind"] == KIND_OBJECT_FIELD and rec.get("_field_is_new"):
                add_member(plan, "CustomField", f"{rec['component']}.{rec['key']}")
    for obj in packaged_objs:
        add_member(plan, "CustomObjectTranslation", f"{obj}-{lang}")
        # New field + its translation must travel together; if this object also
        # has schema members they are already on the plan.
        if obj in new_objects:
            add_member(plan, "CustomObject", obj)

    plan["empty"] = not any(plan.get("members", {}).values())
    return plan


def print_delta(plan: dict) -> None:
    """Surface schema AND translation deltas. Name-existence is not enough."""
    schema = plan.get("schema") or {}
    new_fields = schema.get("new_fields") or []
    existing = schema.get("existing_objects") or []
    new_objs = schema.get("new_objects") or []
    trans = plan.get("translations") or []
    changed = [t for t in trans if t.get("code") == CHANGED]
    new_t = [t for t in trans if t.get("code") == NEW]
    unchanged = [t for t in trans if t.get("code") == UNCHANGED]
    print(f"      SCHEMA       new_objects={len(new_objs)}  new_fields={len(new_fields)}  "
          f"existing_objects={len(existing)}")
    print(f"      TRANSLATIONS new={len(new_t)}  changed={len(changed)}  "
          f"unchanged={len(unchanged)}")
    for t in new_t:
        print(f"        NEW     {t.get('key')}: {t.get('translation')!r}")
    for t in changed:
        print(f"        CHANGED {t.get('key')}: org {t.get('org_translation')!r} "
              f"→ sheet {t.get('translation')!r}")
    if not new_t and not changed and not new_fields and not new_objs:
        print("        (no schema or translation delta)")


def write_plan(plan: dict, path: str | Path = ".build/deploy_plan.json") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# --- sheet enrichment ---

NEEDS_CONFIRMATION = 3  # distinct from validation failure (1)


@dataclass
class CellWrite:
    tab: str
    range: str  # 'Tab'!A1
    old: str
    new: str
    mode: str  # RAW | USER_ENTERED
    kind: str
    field: str
    object_api: str
    ja: str
    ja_a1: str = ""
    note: str = ""


@dataclass
class Enrichment:
    provider: str = ""
    tabs: list[str] = field(default_factory=list)
    spreadsheet_id: str = ""
    headers_to_create: dict[str, dict[str, int]] = field(default_factory=dict)
    writes: list[CellWrite] = field(default_factory=list)
    rows_patch: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    skipped: int = 0
    translated: int = 0
    backfilled: int = 0
    objects_affected: int = 0
    fields_affected: int = 0
    stale: bool = False
    applied: bool = False


class TranslationAbort(SystemExit):
    """Hard stop: no sheet write, no deploy."""


def _read_tab(svc, sid: str, tab: str) -> list[list[str]]:
    """Live tab read — same call as write_back.py / write_attr_fixes.py."""
    vals = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'",
        valueRenderOption="FORMATTED_VALUE",
    ).execute().get("values", []) or []
    return [[str(c) if c is not None else "" for c in row] for row in vals]


def _batch_update(svc, sid: str, data: list[dict], option: str) -> None:
    """Live tab write — same call as write_back.py / writeback_cells.py."""
    if not data:
        return
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": option, "data": data},
    ).execute()


def _cells_from_grid(grid: list[list[str]], a1_cells: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cell in a1_cells:
        col, row = parse_a1(cell)
        out[cell] = cell_at(grid, row - 1, col)
    return out


def _wait_recalc(svc, sid: str, tab: str, a1_cells: list[str],
                 timeout: float = 45.0, interval: float = 1.5) -> dict[str, str]:
    deadline = time.time() + timeout
    last: dict[str, str] = {}
    while True:
        last = _cells_from_grid(_read_tab(svc, sid, tab), a1_cells)
        if all(not (v.startswith("=") or v == "Loading...") for v in last.values()):
            return last
        if time.time() >= deadline:
            stuck = {k: v for k, v in last.items() if v.startswith("=") or v == "Loading..."}
            raise RuntimeError(
                "Spreadsheet recalculation timed out; calculated English "
                f"could not be read for: {stuck}"
            )
        time.sleep(interval)


def _cell(grid, r0, c) -> str:
    if c is None:
        return ""
    return cell_at(grid, r0, c)


_META_LABELS = {
    "表示ラベル", "オブジェクト名", "説明", "レポートを許可", "活動を許可",
    "項目履歴管理", "検索を許可", "タブ作成 (create tab)",
}


def _find_meta_value(grid: list[list[str]], header_idx: int, *labels: str) -> tuple[str, int, int]:
    """Return (value, row0, col0) for a labeled cell in the object-meta block."""
    want = {norm(x).rstrip(":").lower() for x in labels}
    known_labels = {norm(x).rstrip(":").lower() for x in _META_LABELS}
    for r in range(header_idx):
        row = grid[r] if r < len(grid) else []
        for c, val in enumerate(row):
            if norm(val).rstrip(":").lower() in want:
                # A value belongs only to this logical header region. If the
                # next metadata label appears first, the intended value is
                # blank; never borrow that label's value or the object API.
                for k in range(c + 1, max(len(row) + 4, c + 8)):
                    v = cell_at(grid, r, k)
                    if norm(v).rstrip(":").lower() in known_labels:
                        break
                    if v and not v.endswith(":"):
                        return v, r, k
                return "", r, c + 1
    return "", -1, -1


def collect_tab(
    tab: str,
    grid: list[list[str]],
    *,
    parse_tab_fn,
) -> tuple[list[dict], dict]:
    """Use fetch_sheet.parse_tab plus raw grid coordinates for write-back."""
    from fetch_sheet import find_header_row, find_helper_cols, is_field_list_end, build_col_map

    rows = parse_tab_fn(tab, grid)
    hidx = find_header_row(grid)
    if hidx is None:
        return rows, {"tab": tab, "error": "no header row"}
    header = grid[hidx]
    col_map = build_col_map(header, grid[hidx - 1] if hidx else None)
    helper = find_helper_cols(header)
    inv = {v: k for k, v in col_map.items()}
    ja_col = inv.get("Field Label")
    if ja_col is None:
        ja_col = header_index(header, "label", "field label")
    en_col = header_index(header, FIELD_EN_HEADER, "field label (en)")
    prov_col = header_index(header, PROVENANCE_HEADER)
    origin_col = header_index(header, ORIGIN_HEADER)
    hash_col = header_index(header, HASH_HEADER)
    gen_col = header_index(header, GENERATED_HEADER)
    api_col = header_index(header, "fullname")
    type_col = header_index(header, "type")
    missing = plan_missing_headers(header)

    # If EN/provenance will be created, use the planned indices for writes.
    if en_col is None:
        en_col = missing.get(FIELD_EN_HEADER)
    if prov_col is None:
        prov_col = missing.get(PROVENANCE_HEADER)

    # Object-meta sits above the JP field-header row (hidx-1) and the EN
    # field-header row (hidx). Searching through hidx matches 翻訳出典 on the
    # field header and parks object provenance in AL9.
    meta_limit = max(hidx - 1, 0)
    obj_en, obj_en_r, obj_en_c = _find_meta_value(
        grid, meta_limit, OBJECT_EN_LABEL, "表示ラベル (EN)", "表示ラベル(EN)")
    obj_pv, obj_pv_r, obj_pv_c = _find_meta_value(
        grid, meta_limit, OBJECT_PROVENANCE_LABEL)
    obj_or, obj_or_r, obj_or_c = _find_meta_value(grid, meta_limit, OBJECT_ORIGIN_LABEL)
    obj_hs, obj_hs_r, obj_hs_c = _find_meta_value(grid, meta_limit, OBJECT_HASH_LABEL)
    obj_gn, obj_gn_r, obj_gn_c = _find_meta_value(grid, meta_limit, OBJECT_GENERATED_LABEL)
    obj_ja, obj_ja_r, obj_ja_c = _find_meta_value(grid, meta_limit, "表示ラベル")
    # Object header is the 表示ラベル row (typically row 1, value in col D).
    # Object EN/provenance are that same row in the Field Label (EN) /
    # Translation Provenance columns (field-header row is below this).
    if obj_en_r < 0 and obj_ja_r >= 0 and en_col is not None:
        obj_en = _cell(grid, obj_ja_r, en_col)
        obj_en_r, obj_en_c = obj_ja_r, en_col
    if obj_pv_r < 0 and obj_ja_r >= 0 and prov_col is not None:
        obj_pv = _cell(grid, obj_ja_r, prov_col)
        obj_pv_r, obj_pv_c = obj_ja_r, prov_col
    pv_o, pv_h, pv_t = parse_provenance(obj_pv)
    obj_or = pv_o or obj_or
    obj_hs = pv_h or obj_hs
    obj_gn = pv_t or obj_gn
    if obj_pv_r < 0 and obj_or_r >= 0:
        obj_pv, obj_pv_r, obj_pv_c = obj_or, obj_or_r, obj_or_c

    loc = {
        "tab": tab,
        "header_row": hidx + 1,
        "header_cells": header,
        "ja_col": ja_col,
        "en_col": en_col,
        "prov_col": prov_col,
        "origin_col": origin_col,
        "hash_col": hash_col,
        "gen_col": gen_col,
        "api_col": api_col,
        "type_col": type_col,
        "helper": helper,
        "missing_headers": missing,
        "object_en": {"value": obj_en, "row0": obj_en_r, "col0": obj_en_c},
        "object_prov": {"value": format_provenance(obj_or, obj_hs, obj_gn) if (obj_or or obj_hs or obj_gn) else obj_pv,
                        "row0": obj_pv_r, "col0": obj_pv_c},
        "object_origin": {"value": obj_or, "row0": obj_or_r, "col0": obj_or_c},
        "object_hash": {"value": obj_hs, "row0": obj_hs_r, "col0": obj_hs_c},
        "object_generated": {"value": obj_gn, "row0": obj_gn_r, "col0": obj_gn_c},
        "object_ja": {"value": obj_ja, "row0": obj_ja_r, "col0": obj_ja_c},
        "field_rows": [],
        "name_row": None,
    }

    wip_c = helper.get("wip")
    del_c = helper.get("isdelete")
    for i in range(hidx + 1, len(grid)):
        cells = grid[i]
        vals = [norm(c) for c in cells]
        if is_field_list_end(vals):
            break
        api = _cell(grid, i, api_col)
        ja = _cell(grid, i, ja_col)
        ftype = _cell(grid, i, type_col)
        if not (api or (ja and ftype)):
            continue
        packed = _cell(grid, i, prov_col)
        po, ph, pt = parse_provenance(packed)
        origin = po or _cell(grid, i, origin_col)
        source_hash = ph or _cell(grid, i, hash_col)
        generated_at = pt or _cell(grid, i, gen_col)
        rec = {
            "sheet_row": i + 1,
            "api": api,
            "ja": ja,
            "en": _cell(grid, i, en_col),
            "origin": origin,
            "source_hash": source_hash,
            "generated_at": generated_at,
            "type": ftype,
            "wip": truthy(_cell(grid, i, wip_c)),
            "isdelete": is_delete(_cell(grid, i, del_c)),
            "ja_a1": a1(ja_col, i + 1) if ja_col is not None else "",
            "en_a1": a1(en_col, i + 1) if en_col is not None else "",
            "prov_a1": a1(prov_col, i + 1) if prov_col is not None else "",
            "origin_a1": a1(origin_col, i + 1) if origin_col is not None else "",
            "hash_a1": a1(hash_col, i + 1) if hash_col is not None else "",
            "gen_a1": a1(gen_col, i + 1) if gen_col is not None else "",
        }
        if api == "Name":
            loc["name_row"] = rec
        else:
            loc["field_rows"].append(rec)
    return rows, loc


def _q(tab: str, cell: str) -> str:
    safe = tab.replace("'", "''")
    return f"'{safe}'!{cell}"


def _write(tab, cell, old, new, mode, kind, field, obj, ja, ja_a1="", note="") -> CellWrite:
    return CellWrite(
        tab=tab, range=_q(tab, cell), old=old or "", new=new,
        mode=mode, kind=kind, field=field, object_api=obj, ja=ja,
        ja_a1=ja_a1, note=note,
    )


def select_provider(
    *,
    force: str = "",
) -> tuple[str, DeepLProvider | None]:
    """Pick ONE provider for the whole batch. Never mix."""
    force = (force or "").strip().lower()
    if force == "google":
        return ORIGIN_GOOGLE, None
    provider = DeepLProvider()
    try:
        provider.preflight()
        return ORIGIN_DEEPL, provider
    except DeepLUnavailable as exc:
        if force == "deepl":
            raise TranslationAbort(
                "⛔ DeepL was required (--force-provider deepl) but preflight "
                f"failed: {exc}"
            ) from exc
        provider.close()
        return ORIGIN_GOOGLE, None


def build_jobs(locs: list[dict], object_rows: list[dict]) -> list[dict]:
    """In-scope translation jobs (object, Name, custom fields)."""
    jobs = []
    obj_by_tab = {norm(r.get("_SheetName")): r for r in object_rows if r.get("_type") == "object_meta"}
    for loc in locs:
        tab = loc["tab"]
        meta = obj_by_tab.get(norm(tab))
        if not meta:
            raise TranslationAbort(
                f"⛔ no object definition on tab {tab!r} — check --tabs names "
                f"(comma-separated, e.g. Deal,Shipping)."
            )
        obj = norm(meta.get("Object API Name"))
        # Object JA = 表示ラベル on the object-header row. Object EN = that
        # same header row in the Field Label (EN) column.
        ja = loc["object_ja"]["value"] or norm(meta.get("Object Label"))
        en = loc["object_en"]["value"] or norm(meta.get("Object Label (EN)"))
        if ja:
            jobs.append({
                "kind": KIND_OBJECT_LABEL,
                "tab": tab,
                "object_api": obj,
                "field_api": obj,
                "ja": ja,
                "en": en,
                "origin": loc["object_origin"]["value"] or norm(meta.get("Object Translation Origin")),
                "source_hash": loc["object_hash"]["value"] or norm(meta.get("Object Translation Source Hash")),
                "generated_at": loc["object_generated"]["value"] or norm(meta.get("Object Translation Generated At")),
                "ja_a1": a1(loc["object_ja"]["col0"], loc["object_ja"]["row0"] + 1)
                if loc["object_ja"]["row0"] >= 0 and loc["object_ja"]["col0"] >= 0 else "",
                "en_a1": a1(loc["object_en"]["col0"], loc["object_en"]["row0"] + 1)
                if loc["object_en"]["row0"] >= 0 and loc["object_en"]["col0"] >= 0 else "",
                "prov_a1": a1(loc["object_prov"]["col0"], loc["object_prov"]["row0"] + 1)
                if loc["object_prov"]["row0"] >= 0 and loc["object_prov"]["col0"] >= 0 else "",
                "origin_a1": "",
                "hash_a1": "",
                "gen_a1": "",
                "wip": False,
                "isdelete": False,
            })
        name = loc.get("name_row")
        if name and not name["wip"]:
            jobs.append({
                "kind": KIND_NAME_FIELD,
                "tab": tab,
                "object_api": obj,
                "field_api": "Name",
                **{k: name[k] for k in (
                    "ja", "en", "origin", "source_hash", "generated_at",
                    "ja_a1", "en_a1", "prov_a1", "origin_a1", "hash_a1", "gen_a1",
                    "wip", "isdelete",
                )},
            })
        for fr in loc["field_rows"]:
            api = fr["api"]
            if not api:
                continue
            if fr["wip"] or fr["isdelete"]:
                jobs.append({
                    "kind": KIND_OBJECT_FIELD,
                    "tab": tab, "object_api": obj, "field_api": api,
                    **fr, "action": "skip_flag",
                })
                continue
            jobs.append({
                "kind": KIND_OBJECT_FIELD,
                "tab": tab, "object_api": obj, "field_api": api, **fr,
            })
    return jobs


def enrich_jobs(
    jobs: list[dict],
    glossary: dict,
    provider_name: str,
    deepl: DeepLProvider | None,
) -> tuple[list[dict], list[dict]]:
    """Fill English on jobs. DeepL translates the whole remainder first."""
    blocked: list[dict] = []
    need_provider: list[dict] = []
    ts = now_iso()
    origin = ORIGIN_DEEPL if provider_name == ORIGIN_DEEPL else ORIGIN_GOOGLE

    for j in jobs:
        j["old_en"] = j.get("en") or ""
        j["old_origin"] = j.get("origin") or ""
        j["old_hash"] = j.get("source_hash") or ""
        j["old_generated"] = j.get("generated_at") or ""
        if j.get("action") == "skip_flag":
            j["action"] = "skip"
            continue
        # Object-header EN/provenance are never written. Over-limit header EN
        # is resolved to Name EN only in the org package (object_en_for_org).
        if j.get("kind") == KIND_OBJECT_LABEL:
            j["action"] = "skip"
            continue
        action = classify_need(j.get("ja", ""), j.get("en", ""),
                               j.get("source_hash", ""), j.get("origin", ""))
        j["action"] = action
        if action == "block_blank_ja":
            blocked.append({**j, "reason": "blank Japanese source label"})
            continue
        if action == "skip":
            continue
        if action == "provenance_backfill":
            j["origin"] = j.get("origin") or ORIGIN_MANUAL
            j["source_hash"] = content_hash(j["ja"])
            j["generated_at"] = j.get("generated_at") or ts
            j["en_new"] = j["en"]
            continue
        # translate
        en, why = glossary_lookup(glossary, api=j.get("field_api", ""), ja=j.get("ja", ""))
        if en:
            j["en_new"] = en
            j["origin"] = origin if action == "translate" and why.startswith("provider") else (
                ORIGIN_MANUAL if why.startswith("glossary") else origin
            )
            # glossary hit is trusted terminology — origin stays inventory/manual
            if why.startswith("glossary"):
                j["origin"] = ORIGIN_MANUAL
            j["source_hash"] = content_hash(j["ja"])
            j["generated_at"] = ts
            j["via"] = why
            continue
        j["via"] = "provider"
        need_provider.append(j)

    if need_provider and provider_name == ORIGIN_DEEPL:
        if deepl is None:
            raise DeepLTranslateError("DeepL selected but provider is missing")
        texts = [j["ja"] for j in need_provider]
        try:
            translated = deepl.translate_batch(texts)
        except DeepLTranslateError:
            # discard incomplete in-memory results — do not write, do not fallback
            raise
        for j, en in zip(need_provider, translated):
            j["en_new"] = en
            j["origin"] = ORIGIN_DEEPL
            j["source_hash"] = content_hash(j["ja"])
            j["generated_at"] = ts
            j["via"] = "deepl"
    elif need_provider and provider_name == ORIGIN_GOOGLE:
        for j in need_provider:
            if not j.get("ja_a1"):
                blocked.append({**j, "reason": "no Japanese cell address for GOOGLETRANSLATE"})
                j["action"] = "block"
                continue
            j["en_new"] = google_formula(j["ja_a1"])
            j["origin"] = ORIGIN_GOOGLE
            j["source_hash"] = content_hash(j["ja"])
            j["generated_at"] = ts
            j["via"] = "google"
            j["formula"] = True
    return jobs, blocked


def jobs_to_writes(jobs: list[dict], locs_by_tab: dict[str, dict]) -> list[CellWrite]:
    writes: list[CellWrite] = []
    for j in jobs:
        action = j.get("action")
        if action in {"skip", "block", "skip_flag"}:
            continue
        tab = j["tab"]
        obj = j.get("object_api") or ""
        field = j.get("field_api") or ""
        ja = j.get("ja") or ""
        mode = "USER_ENTERED" if j.get("formula") else "RAW"
        en_new = j.get("en_new", "")
        if action == "provenance_backfill":
            en_new = j.get("en") or ""
            mode = "RAW"

        def add(cell, old, new, which):
            if not cell or new is None:
                return
            if (old or "") == (new or ""):
                return
            writes.append(_write(
                tab, cell, old, new, mode if which == "en" else "RAW",
                j["kind"], field, obj, ja, j.get("ja_a1") or "", which,
            ))

        if j["kind"] == KIND_OBJECT_LABEL:
            continue

        # Name + custom fields share the field-row columns.
        if action != "provenance_backfill" or (j.get("old_en") != en_new):
            add(j.get("en_a1"), j.get("old_en"), en_new, "en")
        old_p = format_provenance(j.get("old_origin"), j.get("old_hash"), j.get("old_generated"))
        new_p = format_provenance(j.get("origin"), j.get("source_hash"), j.get("generated_at"))
        add(j.get("prov_a1") or j.get("origin_a1"), old_p, new_p, "provenance")
    # de-dupe identical ranges keeping last
    seen = {}
    for w in writes:
        seen[w.range] = w
    return list(seen.values())


def header_writes(locs: list[dict]) -> list[CellWrite]:
    out: list[CellWrite] = []
    for loc in locs:
        missing = loc.get("missing_headers") or {}
        hrow = loc["header_row"]
        hdr_cells = loc.get("header_cells") or []
        tab = loc["tab"]
        for hdr, col in missing.items():
            cell = a1(col, hrow)
            old = hdr_cells[col] if col < len(hdr_cells) else ""
            out.append(_write(tab, cell, old, hdr, "RAW", "header", hdr, "", "", note="header"))
            # JP header row (one above) when present
            if hrow > 1:
                jp = {
                    FIELD_EN_HEADER: "項目ラベル名 (EN)",
                    PROVENANCE_HEADER: "翻訳出典",
                }.get(hdr, hdr)
                out.append(_write(
                    tab, a1(col, hrow - 1), "", jp, "RAW",
                    "header", hdr, "", "", note="jp-header",
                ))
    return out


def preview(enr: Enrichment) -> str:
    lines = [
        "=" * 72,
        "  TRANSLATION WRITE PREVIEW  (one confirmation for this exact batch)",
        "=" * 72,
        f"  spreadsheet : {enr.spreadsheet_id}",
        f"  tabs        : {', '.join(enr.tabs)}",
        f"  provider    : {enr.provider}",
        f"  language    : JA → {PROVIDER_DISPLAY(enr.provider)}",
        f"  objects     : {enr.objects_affected}   fields: {enr.fields_affected}",
        f"  headers     : {enr.headers_to_create or '(none — already present)'}",
        "-" * 72,
    ]
    for w in enr.writes:
        lines.append(f"  {w.range:28}  {w.old!r:22} → {w.new!r}   [{w.mode} {w.note or w.kind}]")
    if not enr.writes:
        lines.append("  (no sheet writes — idempotent)")
    lines.append("-" * 72)
    lines.append(f"  cells: {len(enr.writes)}   translated: {enr.translated}   "
                 f"provenance backfill: {enr.backfilled}   skipped: {enr.skipped}")
    lines.append("  Re-run with --apply-translations after confirming this exact batch.")
    lines.append("=" * 72)
    return "\n".join(lines)


def PROVIDER_DISPLAY(p: str) -> str:
    return "EN-US (DeepL literals)" if p == ORIGIN_DEEPL else "en (GOOGLETRANSLATE formula → read calculated)"


def snapshot_cells(svc, sid: str, writes: list[CellWrite]) -> dict[str, str]:
    by_tab: dict[str, list[str]] = {}
    for w in writes:
        # range is 'Tab'!A1
        if "!" not in w.range:
            continue
        tab, cell = w.range.split("!", 1)
        tab = tab.strip("'")
        by_tab.setdefault(tab, []).append(cell)
    out: dict[str, str] = {}
    for tab, cells in by_tab.items():
        got = _cells_from_grid(_read_tab(svc, sid, tab), cells)
        for c, v in got.items():
            out[_q(tab, c)] = v
    return out


def stale_against(writes: list[CellWrite], live: dict[str, str]) -> list[str]:
    problems = []
    for w in writes:
        cur = live.get(w.range, "")
        if norm(cur) != norm(w.old):
            problems.append(f"{w.range}: expected {w.old!r}, live {cur!r}")
    return problems


def apply_writes(svc, sid: str, writes: list[CellWrite]) -> None:
    formulas = [w for w in writes if w.mode == "USER_ENTERED"]
    literals = [w for w in writes if w.mode != "USER_ENTERED"]
    if literals:
        _batch_update(svc, sid, [{"range": w.range, "values": [[w.new]]} for w in literals], "RAW")
    if formulas:
        _batch_update(svc, sid, [{"range": w.range, "values": [[w.new]]} for w in formulas], "USER_ENTERED")


def read_calculated(svc, sid: str, jobs: list[dict]) -> None:
    formula_jobs = [j for j in jobs if j.get("formula") and j.get("en_a1") and j.get("action") == "translate"]
    by_tab: dict[str, list[dict]] = {}
    for j in formula_jobs:
        by_tab.setdefault(j["tab"], []).append(j)
    for tab, group in by_tab.items():
        cells = [j["en_a1"] for j in group]
        values = _wait_recalc(svc, sid, tab, cells)
        for j in group:
            calc = values.get(j["en_a1"], "")
            reason = invalid_english(calc, ja=j.get("ja", ""), api=j.get("field_api", ""))
            if reason:
                raise TranslationAbort(
                    f"⛔ calculated Google Translate result is invalid for "
                    f"{j.get('object_api')}.{j.get('field_api')}: {reason} "
                    f"(value={calc!r})"
                )
            j["en_new"] = calc
            j["formula"] = False  # subsequent planning uses the calculated value


def fill_org_object_descriptions(
    object_rows: list[dict],
    rows: list[dict],
    provider_name: str,
    deepl: DeepLProvider | None,
) -> None:
    """Set Object Description (EN) on in-memory rows for CustomObject.description.

    The sheet 説明 cell is never written. DeepL only (same provider as labels);
    Google batches skip this because GOOGLETRANSLATE would require a sheet cell.
    """
    ja_list, targets = [], []
    for r in object_rows:
        ja = norm(r.get("Object Description"))
        # Sheet-layout notes (RT/VR/lookup columns removed) are not the
        # object's business description — do not send them to the org.
        layout_markers = ("レコードタイプ", "入力規則", "ルックアップ検索条件")
        if not ja or sum(marker in ja for marker in layout_markers) >= 2:
            continue
        ja_list.append(ja)
        targets.append(r)
    if not targets or provider_name != ORIGIN_DEEPL or deepl is None:
        return
    ens = deepl.translate_batch(ja_list)
    by_obj = {}
    for r, en in zip(targets, ens):
        en_n = norm(en)
        if not en_n:
            continue
        r["Object Description (EN)"] = en_n
        by_obj[norm(r.get("Object API Name"))] = en_n
    for r in rows:
        if r.get("_type") == "object_meta":
            en_n = by_obj.get(norm(r.get("Object API Name")))
            if en_n:
                r["Object Description (EN)"] = en_n


def merge_into_rows(rows: list[dict], jobs: list[dict]) -> list[dict]:
    by_obj_field = {(j["object_api"], j["field_api"], j["kind"]): j for j in jobs}
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if r.get("_type") == "object_meta":
            n = by_obj_field.get((obj, "Name", KIND_NAME_FIELD))
            if n and n.get("en_new"):
                r["Name Field Label (EN)"] = n["en_new"]
            continue
        api = norm(r.get("Field API Name"))
        j = by_obj_field.get((obj, api, KIND_OBJECT_FIELD))
        if j and j.get("en_new"):
            r["Field Label (EN)"] = j["en_new"]
            r["Translation Provenance"] = format_provenance(
                j.get("origin", ""), j.get("source_hash", ""), j.get("generated_at", ""))
            r["Translation Origin"] = j.get("origin", "")
            r["Translation Source Hash"] = j.get("source_hash", "")
            r["Translation Generated At"] = j.get("generated_at", "")
    return rows


def run_enrichment(
    *,
    spreadsheet_id: str,
    tabs: list[str],
    rows: list[dict],
    apply: bool = False,
    force_provider: str = "",
) -> Enrichment:
    from fetch_sheet import parse_tab

    enr = Enrichment(spreadsheet_id=spreadsheet_id, tabs=list(tabs))
    locs = []
    parsed_all = []
    api_hints = {
        norm(r.get("_SheetName")): norm(r.get("Object API Name"))
        for r in rows
        if r.get("_type") == "object_meta"
    }
    svc = get_write_service()
    for tab in tabs:
        grid = _read_tab(svc, spreadsheet_id, tab)
        parsed, loc = collect_tab(
            tab,
            grid,
            parse_tab_fn=lambda title, values: parse_tab(
                title,
                values,
                object_api_hint=api_hints.get(norm(title), ""),
            ),
        )
        parsed_all.extend(parsed)
        locs.append(loc)
        if loc.get("missing_headers"):
            enr.headers_to_create[tab] = loc["missing_headers"]

    # Prefer the live grid parse; fall back to caller rows for object APIs.
    object_rows = [r for r in (parsed_all or rows) if r.get("_type") == "object_meta"]
    if not object_rows:
        object_rows = [r for r in rows if r.get("_type") == "object_meta"]

    glossary = build_glossary(parsed_all or rows)
    jobs = build_jobs(locs, object_rows)

    # Provider preflight only if at least one label will need a provider.
    probe = []
    for j in jobs:
        if j.get("action") == "skip_flag" or j.get("kind") == KIND_OBJECT_LABEL:
            continue
        act = classify_need(j.get("ja", ""), j.get("en", ""),
                            j.get("source_hash", ""), j.get("origin", ""))
        if act == "translate":
            en, why = glossary_lookup(glossary, api=j.get("field_api", ""), ja=j.get("ja", ""))
            if not en:
                probe.append(j)
    deepl = None
    # An explicit provider choice is itself a preflight request. In
    # particular, --force-provider deepl must fail without a usable key/server
    # even when every label is already translated.
    if probe or force_provider:
        enr.provider, deepl = select_provider(force=force_provider)
    else:
        enr.provider = ORIGIN_MANUAL

    try:
        jobs, blocked = enrich_jobs(
            jobs, glossary, enr.provider if enr.provider in (ORIGIN_DEEPL, ORIGIN_GOOGLE)
            else ORIGIN_GOOGLE, deepl,
        )
        fill_org_object_descriptions(object_rows, rows, enr.provider, deepl)
    except DeepLTranslateError as e:
        if deepl:
            deepl.close()
        raise TranslationAbort(
            f"⛔ DeepL failed after a successful preflight. Incomplete results "
            f"discarded; Google Translate was NOT used. Fix DeepL or re-run "
            f"so preflight can select Google.\n{e}"
        ) from e
    finally:
        if deepl:
            deepl.close()

    enr.blocked = blocked

    locs_by_tab = {loc["tab"]: loc for loc in locs}
    writes = header_writes(locs) + jobs_to_writes(jobs, locs_by_tab)
    enr.writes = writes
    enr.translated = sum(1 for j in jobs if j.get("action") == "translate")
    enr.backfilled = sum(1 for j in jobs if j.get("action") == "provenance_backfill")
    enr.skipped = sum(1 for j in jobs if j.get("action") == "skip")
    objs = {j["object_api"] for j in jobs if j.get("action") in {"translate", "provenance_backfill"}}
    fields = {j["field_api"] for j in jobs if j.get("kind") != KIND_OBJECT_LABEL
              and j.get("action") in {"translate", "provenance_backfill"}}
    enr.objects_affected = len(objs)
    enr.fields_affected = len(fields)

    if not apply:
        enr.rows_patch = jobs
        return enr

    if writes:
        if svc is None:
            svc = get_write_service()
        live = snapshot_cells(svc, spreadsheet_id, writes)
        problems = stale_against(writes, live)
        if problems:
            enr.stale = True
            raise TranslationAbort(
                "⛔ stale/concurrent sheet edit detected — refusing to write.\n"
                + "\n".join(f"  {p}" for p in problems[:20])
                + "\nRe-run to build a fresh preview."
            )
        apply_writes(svc, spreadsheet_id, writes)
        enr.applied = True
        if any(j.get("formula") for j in jobs):
            read_calculated(svc, spreadsheet_id, jobs)
    enr.rows_patch = jobs
    return enr


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview/apply JA→EN translation enrichment")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tabs", required=True,
                    help="comma-separated object tab names (e.g. Deal,Shipping)")
    ap.add_argument("--rows", default="temp_updates.json")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-provider", default="", choices=["", "deepl", "google"])
    ap.add_argument("--out", default=".build/translation_preview.json")
    args = ap.parse_args()
    from fetch_sheet import parse_tab_list
    tabs = parse_tab_list(args.tabs)
    rows = json.loads(Path(args.rows).read_text(encoding="utf-8")) if Path(args.rows).exists() else []
    try:
        enr = run_enrichment(
            spreadsheet_id=args.spreadsheet_id,
            tabs=tabs,
            rows=rows,
            apply=args.apply,
            force_provider=args.force_provider,
        )
    except TranslationAbort as e:
        print(e)
        return 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(enr)
    payload["writes"] = [asdict(w) for w in enr.writes]
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(preview(enr))
    if not args.apply and enr.writes:
        print("DRY RUN — nothing written.")
        return NEEDS_CONFIRMATION
    return 0


if __name__ == "__main__":
    sys.exit(main())
