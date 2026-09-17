#!/usr/bin/env python3
"""
translation_lib.py — shared primitives for the translation pipeline.

Mirrors the object-definition pipeline:
  * Google Sheet is the source of truth (live reads only):
    https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
  * Org is the deploy TARGET, compared live via Metadata API (FLS-independent).
  * Delta is key-based + content-hash; unchanged translations are not redeployed.
  * Conflicts (sheet AND org both drifted from last successful deploy) are parked.

This module has no CLI. Import from fetch_translations / validate_translations / translation_drift /
generate_object_translation.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sx
from pathlib import Path

NS = "http://soap.sforce.com/2006/04/metadata"

# Salesforce Translation Workbench language codes (not always ISO 639-1:
# Salesforce uses `ja` not `ja_JP`, `en_US` not `en`).
SF_LANG_CODES = {
    "en_US", "en_GB", "en_AU", "en_MY", "en_IN", "en_PH",
    "ja", "ja_JP",  # ja_JP is accepted as an alias → normalized to ja
    "zh_CN", "zh_TW", "ko", "th", "vi", "id", "ms",
    "de", "fr", "it", "es", "es_MX", "pt_BR", "pt_PT", "nl_NL",
    "da", "sv", "fi", "no", "nb",
    "pl", "cs", "sk", "hu", "ro", "bg", "hr", "sl", "lt", "lv", "et",
    "ru", "uk", "tr", "el", "iw", "ar", "hi", "bn",
}
LANG_ALIASES = {"ja_jp": "ja", "jp": "ja", "en": "en_US", "en-us": "en_US",
                "zh-cn": "zh_CN", "zh-tw": "zh_TW", "pt-br": "pt_BR",
                "ko_kr": "ko", "kr": "ko"}

DEFAULT_LANG = "en_US"
MASTER_LANG = "ja"  # org/sheet master language for this project

# Canonical kinds emitted by the parsers.
KIND_OBJECT_LABEL = "ObjectLabel"
KIND_NAME_FIELD = "NameField"
KIND_OBJECT_FIELD = "ObjectField"
KIND_OBJECT_HELP = "ObjectHelp"
KIND_OBJECT_REL = "ObjectRelationshipLabel"
KIND_OBJECT_PICKLIST = "ObjectPicklist"

# Codes used by translation_drift / generators.
NEW = "NEW_TRANSLATION"
CHANGED = "CHANGED"
UNCHANGED = "UNCHANGED"
MISSING = "MISSING_TRANSLATION"
ORG_ONLY = "ORG_ONLY"
CONFLICT = "CONFLICT"
SCHEMA_MISSING = "SCHEMA_MISSING"  # target field/value absent in org
INVALID_LANG = "INVALID_LANG"

WIP_TRUE = {"x", "true", "1", "yes", "○", "〇"}
DELETE_TRUE = {"true", "1", "yes", "x", "○", "〇"}

_JP_PUNCT = {0xFF1B: ord(";"), 0xFF1A: ord(":")}


# --------------------------------------------------------------------------- #
# Text / hash / language
# --------------------------------------------------------------------------- #

def norm(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


def content_hash(s: str) -> str:
    """SHA-256 of NFC-normalized UTF-8 text. Empty → empty string (not a hash)."""
    t = norm(s)
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def entry_key(kind: str, component: str, aspect: str, key: str, language: str) -> str:
    return "|".join([kind, component, aspect or "label", key, language])


def normalize_lang(raw: str) -> tuple[str, str]:
    """Return (canonical_code, error). error is empty when valid."""
    s = norm(raw) or DEFAULT_LANG
    alias = LANG_ALIASES.get(s.lower().replace("-", "_"), "")
    if alias:
        s = alias
    # keep Salesforce mixed-case (en_US, zh_CN, pt_BR)
    if s in SF_LANG_CODES:
        if s == "ja_JP":
            s = "ja"
        return s, ""
    # try case-insensitive match
    for c in SF_LANG_CODES:
        if c.lower() == s.lower():
            return ("ja" if c == "ja_JP" else c), ""
    return s, f"invalid Salesforce language code '{raw}'"


def truthy(v) -> bool:
    return norm(v).lower() in WIP_TRUE


def is_delete(v) -> bool:
    return norm(v).lower() in DELETE_TRUE


# --------------------------------------------------------------------------- #
# Picklist pairing (same delimiter rules as generate_xml.py)
# --------------------------------------------------------------------------- #

def parse_picklist_entries(cell: str) -> list[tuple[str, str]]:
    """Parse a Type Specific Value / Picklist Values cell → [(label, api), ...]."""
    raw = str(cell or "").translate(_JP_PUNCT)
    values = [e.strip() for e in re.split(r"[;\n]", raw) if e.strip()]
    out = []
    esc = "\x00"
    for entry in values:
        e = entry.replace("\\:", esc)
        if ":" in e:
            label, api = [p.strip() for p in e.split(":", 1)]
        else:
            label = api = e
        out.append((label.replace(esc, ":"), api.replace(esc, ":")))
    return out


def parse_picklist_en(cell: str, master_labels: list[str]) -> tuple[list[tuple[str, str]], str]:
    """Pair EN picklist translations with master (JA) labels.

    Accepts:
      * positional: same count/order as master (`A;B;C`)
      * explicit:   `master=translation` or `master→translation` pairs

    Returns ([(masterLabel, translation), ...], error).
    """
    raw = str(cell or "").translate(_JP_PUNCT).strip()
    if not raw:
        return [], ""
    parts = [e.strip() for e in re.split(r"[;\n]", raw) if e.strip()]
    paired = any(("=" in p or "→" in p or "->" in p) for p in parts)
    if paired:
        out = []
        for p in parts:
            if "->" in p:
                m, t = p.split("->", 1)
            elif "→" in p:
                m, t = p.split("→", 1)
            elif "=" in p:
                m, t = p.split("=", 1)
            else:
                return [], f"mixed pair/positional picklist EN entry {p!r}"
            out.append((norm(m), norm(t)))
        unknown = [m for m, _ in out if m not in master_labels]
        if unknown:
            return out, f"EN picklist masterLabel(s) not in JA value set: {unknown}"
        return out, ""
    if len(parts) != len(master_labels):
        return [], (f"positional Picklist Values (EN) count {len(parts)} "
                    f"!= master count {len(master_labels)}")
    return list(zip(master_labels, [norm(p) for p in parts])), ""


# --------------------------------------------------------------------------- #
# Catalog entry shape
# --------------------------------------------------------------------------- #

def make_entry(*, kind: str, component: str, aspect: str, key: str,
               language: str, master: str, translation: str,
               source: str = "", sheet_row: int = 0,
               extra: dict | None = None) -> dict:
    lang, lang_err = normalize_lang(language)
    e = {
        "kind": kind,
        "component": norm(component),
        "aspect": norm(aspect) or "label",
        "key": norm(key),
        "language": lang,
        "master": norm(master),
        "translation": norm(translation),
        "hash": content_hash(translation),
        "id": entry_key(kind, norm(component), norm(aspect) or "label",
                        norm(key), lang),
        "source": source,
        "sheet_row": sheet_row,
        "lang_error": lang_err,
    }
    if extra:
        e.update(extra)
    return e


# --------------------------------------------------------------------------- #
# Delta classification
# --------------------------------------------------------------------------- #

def classify(sheet_entries: list[dict], org_by_id: dict[str, dict],
             sync_by_id: dict[str, dict] | None = None,
             conflict_policy: str = "park") -> list[dict]:
    """Classify each sheet entry against live org (+ optional last-deploy hashes).

    conflict_policy: park | sheet-wins | org-wins
    """
    sync_by_id = sync_by_id or {}
    seen = set()
    out = []
    for e in sheet_entries:
        eid = e["id"]
        seen.add(eid)
        org = org_by_id.get(eid)
        last = sync_by_id.get(eid) or {}
        rec = dict(e)
        if e.get("lang_error"):
            rec["code"] = INVALID_LANG
            rec["package"] = False
            rec["reason"] = e["lang_error"]
            out.append(rec)
            continue
        if not e["translation"]:
            rec["code"] = MISSING
            rec["package"] = False
            rec["reason"] = "blank translation cell"
            out.append(rec)
            continue
        if org is None:
            rec["code"] = NEW
            rec["package"] = True
            rec["reason"] = "not in org"
            out.append(rec)
            continue
        org_hash = org.get("hash") or content_hash(org.get("translation", ""))
        sheet_hash = e["hash"]
        last_hash = last.get("hash", "")
        if sheet_hash == org_hash:
            rec["code"] = UNCHANGED
            rec["package"] = False
            rec["reason"] = "sheet matches org"
            out.append(rec)
            continue
        # both sides moved since last successful deploy → conflict
        if (last_hash and org_hash != last_hash and sheet_hash != last_hash
                and org_hash != sheet_hash):
            rec["code"] = CONFLICT
            rec["org_translation"] = org.get("translation", "")
            rec["reason"] = "sheet AND org changed since last deploy"
            if conflict_policy == "sheet-wins":
                rec["package"] = True
            elif conflict_policy == "org-wins":
                rec["package"] = False
            else:  # park
                rec["package"] = False
            out.append(rec)
            continue
        rec["code"] = CHANGED
        rec["package"] = True
        rec["org_translation"] = org.get("translation", "")
        rec["reason"] = "sheet differs from org"
        out.append(rec)

    for eid, org in org_by_id.items():
        if eid in seen:
            continue
        rec = dict(org)
        rec["id"] = eid
        rec["code"] = ORG_ONLY
        rec["package"] = False
        rec["reason"] = "present in org, absent from sheet — left untouched"
        out.append(rec)
    return out


def apply_new_only(classified: list[dict]) -> list[dict]:
    """Keep packaging limited to NEW_TRANSLATION (Japan added a field).

    CHANGED_EN is still reported but not packaged — same rule as field
    attr_drift: an existing definition is not silently redeployed.
    """
    for rec in classified:
        if rec.get("code") == CHANGED and rec.get("package"):
            rec["package"] = False
            rec["reason"] = (rec.get("reason") or "") + " [new-only: not packaged]"
    return classified


def load_sync_state(path: str | Path = ".build/translation_sync_state.json") -> dict:
    p = Path(path)
    if not p.exists():
        return {"entries": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": {}}


def save_sync_state(entries: list[dict], org: str,
                    path: str | Path = ".build/translation_sync_state.json") -> None:
    import datetime
    p = Path(path)
    prev = load_sync_state(p)
    store = prev.get("entries") or {}
    for e in entries:
        if e.get("package") and e.get("code") in {NEW, CHANGED, CONFLICT}:
            store[e["id"]] = {"hash": e.get("hash", ""), "translation": e.get("translation", "")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "org": org,
        "entries": store,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #

def qname(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def xml_header() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n'


def esc(s: str) -> str:
    return sx.escape(norm(s), {"'": "&apos;", '"': "&quot;"})


def strip_soap_ns(xml: str) -> str:
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)
    xml = re.sub(r"<(/?)\w+:", r"<\1", xml)
    xml = re.sub(r"\s\w+:(\w+=)", r" \1", xml)
    return xml


def write_xml(path: Path, root_xml: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml_header() + root_xml, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Org session + Metadata API
# --------------------------------------------------------------------------- #

def load_token(org: str, token_file: str = ".build/orgauth.json") -> dict:
    cp = subprocess.run(
        ["python3", "scripts/get_token.py", "--alias", org, "--out", token_file],
        text=True, capture_output=True)
    if cp.returncode != 0:
        sys.exit(f"❌ get_token failed: {cp.stderr[:400] or cp.stdout[:400]}")
    return json.loads(Path(token_file).read_text())["result"]


def read_metadata(mtype: str, full_names: list[str], tok: str, inst: str,
                  ver: str) -> list[ET.Element]:
    """SOAP readMetadata → list of <records> elements (namespace-stripped)."""
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
            sys.exit(f"❌ readMetadata {mtype} HTTP {e.code}: {e.read().decode()[:400]}")
        root = ET.fromstring(strip_soap_ns(xml))
        for rec in root.findall(".//records"):
            if rec.find("fullName") is not None or rec.find("fields") is not None:
                records.append(rec)
    return records


def parse_object_translation(rec: ET.Element, obj: str, lang: str) -> dict[str, dict]:
    """CustomObjectTranslation SOAP record → {entry_id: org-entry}."""
    out: dict[str, dict] = {}
    # object label lives in caseValues (singular = plural false)
    for cv in rec.findall("caseValues"):
        plural = (cv.findtext("plural") or "").lower() == "true"
        val = norm(cv.findtext("value"))
        if val and not plural:
            e = make_entry(kind=KIND_OBJECT_LABEL, component=obj, aspect="label",
                           key=obj, language=lang, master="", translation=val,
                           source="org")
            out[e["id"]] = e
    for f in rec.findall("fields"):
        name = norm(f.findtext("name"))
        if not name:
            continue
        label = norm(f.findtext("label"))
        help_ = norm(f.findtext("help"))
        rel = norm(f.findtext("relationshipLabel"))
        if label:
            e = make_entry(kind=KIND_OBJECT_FIELD, component=obj, aspect="label",
                           key=name, language=lang, master="", translation=label,
                           source="org")
            out[e["id"]] = e
        if help_:
            e = make_entry(kind=KIND_OBJECT_HELP, component=obj, aspect="help",
                           key=name, language=lang, master="", translation=help_,
                           source="org")
            out[e["id"]] = e
        if rel:
            e = make_entry(kind=KIND_OBJECT_REL, component=obj, aspect="relationshipLabel",
                           key=name, language=lang, master="", translation=rel,
                           source="org")
            out[e["id"]] = e
        for pv in f.findall("picklistValues"):
            master = norm(pv.findtext("masterLabel"))
            trans = norm(pv.findtext("translation"))
            if not master:
                continue
            e = make_entry(kind=KIND_OBJECT_PICKLIST, component=obj,
                           aspect="picklist", key=f"{name}::{master}",
                           language=lang, master=master, translation=trans,
                           source="org")
            out[e["id"]] = e
    return out
