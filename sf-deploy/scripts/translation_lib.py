#!/usr/bin/env python3
"""
translation_lib.py — shared primitives for automatic JA→en_US label translation.

In-scope labels (initial): custom object label, standard Name-field label,
custom field labels, and provenance on standard fields (OwnerId, CreatedDate, …).

This module has no CLI and performs no I/O besides pure functions + XML.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sx
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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


def parse_object_translation_el(rec: ET.Element, obj: str, lang: str = SF_LANG) -> dict:
    """SOAP/mdapi CustomObjectTranslation record → overlay-friendly dict.

    Keeps raw field element XML so unrelated picklist/help/rel nodes survive.
    """
    fields: dict[str, dict] = {}
    object_label = ""
    name_field_label = norm(rec.findtext("nameFieldLabel") or "")
    for cv in rec.findall("caseValues"):
        plural = (cv.findtext("plural") or "").lower() == "true"
        val = norm(cv.findtext("value"))
        if val and not plural:
            object_label = val
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
    starts = norm(rec.findtext("startsWith"))
    return {
        "object": obj,
        "language": lang,
        "object_label": object_label,
        "name_field_label": name_field_label,
        "startsWith": starts,
        "fields": fields,
        "raw_xml": ET.tostring(rec, encoding="unicode"),
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
        text=True, capture_output=True)
    if cp.returncode != 0:
        raise SystemExit(f"❌ get_token failed: {cp.stderr[:400] or cp.stdout[:400]}")
    return json.loads(Path(token_file).read_text())["result"]


def read_metadata(mtype: str, full_names: list[str], tok: str, inst: str,
                  ver: str) -> list[ET.Element]:
    import urllib.error, urllib.request
    records: list[ET.Element] = []
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
        root = ET.fromstring(strip_soap_ns(xml))
        for rec in root.findall(".//records"):
            if rec.find("fullName") is not None or rec.find("fields") is not None \
                    or rec.find("caseValues") is not None:
                records.append(rec)
    return records


def org_id_from_display(org: str) -> str:
    import subprocess
    cp = subprocess.run(
        ["sf", "org", "display", "--target-org", org, "--json"],
        text=True, capture_output=True)
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
        "name_field_label": model.get("name_field_label"),
        "startsWith": model.get("startsWith"),
        "fields": fields,
    }

