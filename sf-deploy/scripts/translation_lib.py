#!/usr/bin/env python3
"""
translation_lib.py — shared primitives for the translation pipeline.

Mirrors the object-definition pipeline:
  * Google Sheet is the source of truth (live reads only):
    https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ
  * Org is the deploy TARGET, compared live via Metadata API (FLS-independent).
  * Delta is key-based + content-hash; unchanged translations are not redeployed.
  * Conflicts (sheet AND org both drifted from last successful deploy) are parked.

This module has no CLI. Import from translation_drift / generate_object_translation.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sx
from pathlib import Path

NS = "http://soap.sforce.com/2006/04/metadata"
SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
BUILD_DIR = REPO_ROOT / ".build"

# Used only when `sf org display` does not report an apiVersion.
FALLBACK_API_VERSION = "60.0"

# Child element order of CustomObjectTranslation / CustomFieldTranslation as
# declared by the Metadata API WSDL (alphabetical within each type). The
# elements are a <sequence>, so a deploy rejects out-of-order children.
COT_CHILD_ORDER = [
    "caseValues", "fieldSets", "fields", "gender", "layouts", "nameFieldLabel",
    "quickActions", "recordTypes", "sharingReasons", "standardFields",
    "startsWith", "validationRules", "webLinks", "workflowTasks",
]
CFT_CHILD_ORDER = [
    "caseValues", "gender", "help", "label", "lookupFilter", "name",
    "picklistValues", "relationshipLabel", "startsWith",
]

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
PARSE_ERROR = "PARSE_ERROR"  # unusable sheet cell → validation error, never silent

# Rows carry this flag when their source tab actually has a Field Label (EN)
# column. Tabs without it are untranslated and are skipped entirely — no
# catalog entries, no org auth, no Metadata API call.
EN_FLAG = "_HasFieldLabelEN"
EN_VALUE_KEYS = ("Field Label (EN)", "Object Label (EN)", "Name Field Label (EN)",
                 "Help Text (EN)", "Relationship Label (EN)", "Picklist Values (EN)")

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


# --------------------------------------------------------------------------- #
# SOQL safety + batching
# --------------------------------------------------------------------------- #

API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(__c|__C|__r|__mdt|__e|__b|__x)?$")


class InvalidApiName(ValueError):
    """A value bound for a SOQL literal is not a Salesforce API name."""


def soql_name(value: str) -> str:
    """Validate an API name for use inside a SOQL string literal.

    API names cannot legally contain quotes or backslashes, so anything that
    does is rejected outright rather than escaped — an unexpected value is a
    bug or an injection attempt, not something to smuggle into the query.
    """
    v = norm(value)
    if not v or len(v) > 80 or not API_NAME_RE.match(v):
        raise InvalidApiName(f"not a Salesforce API name: {value!r}")
    return v


def soql_in_list(values: list[str]) -> str:
    """Render a validated ``IN ('a','b')`` value list."""
    return ",".join(f"'{soql_name(v)}'" for v in values)


def chunks(items: list, size: int) -> list[list]:
    """Split a list into deterministic batches (SOQL IN / readMetadata limits)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]


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


def translated_tabs(rows: list[dict]) -> set[str]:
    """Tab names that actually carry a ``Field Label (EN)`` column.

    fetch_sheet.py stamps every row with ``_HasFieldLabelEN``. Older payloads
    predate the flag, so for those we fall back to "does any EN cell hold a
    value" rather than assuming the column exists.
    """
    tabs: set[str] = set()
    flagged = False
    for r in rows:
        tab = norm(r.get("_SheetName"))
        if EN_FLAG in r:
            flagged = True
            if truthy(r.get(EN_FLAG)):
                tabs.add(tab)
    if flagged:
        return tabs
    for r in rows:
        if any(norm(r.get(k)) for k in EN_VALUE_KEYS):
            tabs.add(norm(r.get("_SheetName")))
    return tabs


def has_translation_columns(rows: list[dict]) -> bool:
    """True when at least one source tab is translated (see translated_tabs)."""
    return bool(translated_tabs(rows))


def entries_from_object_rows(rows: list[dict], lang: str = DEFAULT_LANG) -> list[dict]:
    """Convert fetch_sheet.py object/field rows into translation catalog entries.

    Rows whose source tab has no ``Field Label (EN)`` column produce NO
    entries: an untranslated tab must not drag the deploy into the translation
    pipeline (org auth + Metadata API) for nothing.
    """
    en_tabs = translated_tabs(rows)
    out: list[dict] = []
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if not obj:
            continue
        if norm(r.get("_SheetName")) not in en_tabs:
            continue
        source = f"object_tab:{r.get('_SheetName') or obj}"
        if r.get("_type") == "object_meta":
            en = norm(r.get("Object Label (EN)"))
            e = make_entry(kind=KIND_OBJECT_LABEL, component=obj, aspect="label",
                           key=obj, language=lang, master=norm(r.get("Object Label")),
                           translation=en, source=source)
            out.append(e)
            name_en = norm(r.get("Name Field Label (EN)"))
            e = make_entry(kind=KIND_NAME_FIELD, component=obj, aspect="label",
                           key="Name", language=lang,
                           master=norm(r.get("Name Field Label")),
                           translation=name_en, source=source)
            out.append(e)
            continue
        api = norm(r.get("Field API Name"))
        if not api.endswith("__c"):
            continue
        e = make_entry(kind=KIND_OBJECT_FIELD, component=obj, aspect="label",
                       key=api, language=lang, master=norm(r.get("Field Label")),
                       translation=norm(r.get("Field Label (EN)")), source=source)
        out.append(e)
        help_en = norm(r.get("Help Text (EN)"))
        if help_en or norm(r.get("Help Text")):
            out.append(make_entry(kind=KIND_OBJECT_HELP, component=obj, aspect="help",
                                  key=api, language=lang, master=norm(r.get("Help Text")),
                                  translation=help_en, source=source))
        rel_en = norm(r.get("Relationship Label (EN)"))
        if rel_en:
            out.append(make_entry(kind=KIND_OBJECT_REL, component=obj,
                                  aspect="relationshipLabel", key=api, language=lang,
                                  master=norm(r.get("Relationship Label")),
                                  translation=rel_en, source=source))
        dt = norm(r.get("Data Type")).lower()
        if "picklist" in dt:
            masters = [lbl for lbl, _api in parse_picklist_entries(
                r.get("Type Specific Value") or r.get("Picklist Values") or "")]
            pairs, err = parse_picklist_en(r.get("Picklist Values (EN)") or "", masters)
            if err and norm(r.get("Picklist Values (EN)")):
                out.append(make_entry(
                    kind=KIND_OBJECT_PICKLIST, component=obj, aspect="picklist",
                    key=f"{api}::__parse__", language=lang, master="",
                    translation="", source=source,
                    extra={"parse_error": err, "field": api}))
            for master, trans in pairs:
                out.append(make_entry(
                    kind=KIND_OBJECT_PICKLIST, component=obj, aspect="picklist",
                    key=f"{api}::{master}", language=lang, master=master,
                    translation=trans, source=source,
                    extra={"field": api}))
    return out


# --------------------------------------------------------------------------- #
# Delta classification
# --------------------------------------------------------------------------- #

def target_key(entry: dict) -> str:
    """`Obj__c.Field__c` the translation is attached to (object label → Obj__c)."""
    comp = norm(entry.get("component"))
    if entry.get("kind") in {KIND_OBJECT_LABEL, KIND_NAME_FIELD}:
        return comp
    field = norm(entry.get("field")) or norm(entry.get("key")).split("::", 1)[0]
    return f"{comp}.{field}" if field else comp


def classify(sheet_entries: list[dict], org_by_id: dict[str, dict],
             sync_by_id: dict[str, dict] | None = None,
             conflict_policy: str = "park",
             org_schema: dict[str, set[str]] | None = None,
             planned_fields: dict[str, set[str]] | None = None) -> list[dict]:
    """Classify each sheet entry against live org (+ optional last-deploy hashes).

    conflict_policy: park | sheet-wins | org-wins

    org_schema      {object: {existing field API names}} from the org snapshot.
    planned_fields  {object: {field API names this deploy creates}}.

    A translation whose TARGET field exists neither in the org nor in this
    deploy is SCHEMA_MISSING — a different problem from a blank EN cell
    (MISSING_TRANSLATION), and it must not be packaged: the COT member would
    fail on an unknown field.
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
        if e.get("parse_error"):
            # An unparseable EN cell is a sheet defect, not a missing
            # translation — surface it as a validation error so it cannot be
            # mistaken for "Japan has not filled this in yet".
            rec["code"] = PARSE_ERROR
            rec["package"] = False
            rec["reason"] = e["parse_error"]
            out.append(rec)
            continue
        if e.get("lang_error"):
            rec["code"] = INVALID_LANG
            rec["package"] = False
            rec["reason"] = e["lang_error"]
            out.append(rec)
            continue
        if org_schema is not None and org is None:
            tgt = target_key(e)
            if "." in tgt:
                obj, field = tgt.split(".", 1)
                if not (field in (org_schema.get(obj) or set())
                        or field in ((planned_fields or {}).get(obj) or set())):
                    rec["code"] = SCHEMA_MISSING
                    rec["package"] = False
                    rec["reason"] = (f"target field {tgt} is neither in the org nor "
                                     f"created by this deploy")
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


DEFAULT_SYNC_STATE = str(BUILD_DIR / "translation_sync_state.json")


def _read_sync_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_sync_state(path: str | Path = DEFAULT_SYNC_STATE, org_id: str = "") -> dict:
    """Last-deployed hashes for ONE target org.

    State is keyed by the org's 18-char Id, never by alias: two sandboxes may
    share an alias across machines, and a hash recorded against sandbox A must
    never make sandbox B look "unchanged".
    """
    data = _read_sync_file(Path(path))
    key = norm(org_id)
    if not key:
        return {"entries": {}}
    return {"entries": ((data.get("orgs") or {}).get(key) or {}).get("entries") or {}}


def save_sync_state(entries: list[dict], org: str,
                    path: str | Path = DEFAULT_SYNC_STATE,
                    org_id: str = "") -> None:
    import datetime
    key = norm(org_id)
    if not key:
        # Without an org Id we cannot attribute the hashes to a sandbox, so we
        # record nothing rather than poison a shared file.
        return
    p = Path(path)
    data = _read_sync_file(p)
    orgs = data.get("orgs") or {}
    bucket = orgs.get(key) or {}
    store = bucket.get("entries") or {}
    for e in entries:
        if e.get("package") and e.get("code") in {NEW, CHANGED, CONFLICT}:
            store[e["id"]] = {"hash": e.get("hash", ""), "translation": e.get("translation", "")}
    orgs[key] = {
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "alias": org,
        "entries": store,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"orgs": orgs}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #

def qname(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def xml_header() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n'


def esc(s: str) -> str:
    return sx.escape(norm(s), {"'": "&apos;", '"': "&quot;"})


def localize(el: ET.Element) -> ET.Element:
    """Strip namespaces from an already-PARSED tree (namespace-aware).

    The XML parser resolves prefixes, so tags arrive as ``{uri}local``; we drop
    the URI in place. This replaces the old regex namespace scrubbing, which
    corrupted any element text that happened to contain ``<prefix:`` or an
    ``xmlns=`` string.
    """
    for node in el.iter():
        if isinstance(node.tag, str) and node.tag.startswith("{"):
            node.tag = node.tag.split("}", 1)[1]
        for name in [a for a in node.attrib if a.startswith("{")]:
            node.attrib[name.split("}", 1)[1]] = node.attrib.pop(name)
    return el


def parse_soap(xml: str) -> ET.Element:
    """Parse a SOAP response namespace-aware; raise on a SOAP fault."""
    try:
        root = localize(ET.fromstring(xml))
    except ET.ParseError as e:
        raise MetadataApiError(f"malformed SOAP response: {e}") from e
    fault = root.find(".//faultstring")
    if fault is not None and norm(fault.text):
        raise MetadataApiError(norm(fault.text)[:600])
    return root


def strip_soap_ns(xml: str) -> str:
    """Deprecated: kept for callers that need a namespace-free XML *string*.

    Prefer parse_soap()/localize(), which let the XML parser handle namespaces.
    """
    return ET.tostring(localize(ET.fromstring(xml)), encoding="unicode")


def write_xml(path: Path, root_xml: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml_header() + root_xml, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Org session + Metadata API
# --------------------------------------------------------------------------- #

class MetadataApiError(RuntimeError):
    """A readMetadata/SOAP call was rejected by the org."""


class OrgAuthError(RuntimeError):
    """`sf org display` could not produce a usable session for the alias."""


class TranslationUnavailable(RuntimeError):
    """Translation Workbench (or the requested language) is not usable in the org."""


def org_auth(org: str) -> dict:
    """Session for `org` via the supported Salesforce CLI, on any machine.

    Uses `sf org display --verbose --json`, so authentication comes from
    whatever the CLI is already configured with (web login, JWT, auth URL, CI
    secret). Nothing here reads or decrypts the CLI's keychain files, and
    nothing assumes a particular HOME layout.
    """
    cmd = ["sf", "org", "display", "--target-org", org, "--verbose", "--json"]
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True)
    except FileNotFoundError:
        raise OrgAuthError("the `sf` CLI is not on PATH — install Salesforce CLI, "
                           f"then `sf org login web --alias {org}`")
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        raise OrgAuthError(f"could not parse `sf org display` output:\n"
                           f"{(cp.stdout or cp.stderr or '')[:400]}")
    res = data.get("result") or {}
    token = norm(res.get("accessToken"))
    inst = norm(res.get("instanceUrl")).rstrip("/")
    if cp.returncode != 0 or not token or not inst:
        msg = norm(data.get("message")) or norm(cp.stderr) or "no accessToken returned"
        raise OrgAuthError(f"`sf org display --target-org {org}` failed: {msg[:400]}\n"
                           f"   authorize the org first: sf org login web --alias {org}")
    return {
        "accessToken": token,
        "instanceUrl": inst,
        "apiVersion": norm(res.get("apiVersion")) or FALLBACK_API_VERSION,
        "orgId": norm(res.get("id")),
        "username": norm(res.get("username")),
    }


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
            raise MetadataApiError(
                f"readMetadata {mtype} HTTP {e.code}: {e.read().decode()[:600]}")
        except urllib.error.URLError as e:
            raise MetadataApiError(
                f"readMetadata {mtype}: cannot reach {inst} ({e.reason})")
        try:
            root = parse_soap(xml)
        except MetadataApiError as e:
            raise MetadataApiError(f"readMetadata {mtype}: {e}") from e
        found = root.findall(".//records")
        if not found:
            # A readMetadata that answers with neither records nor a fault is a
            # partial/unexpected response — never silently read as "absent".
            raise MetadataApiError(
                f"readMetadata {mtype}: response carried no <records> for "
                f"{', '.join(chunk)}")
        for rec in found:
            if rec.find("fullName") is not None or rec.find("fields") is not None:
                records.append(rec)
    return records


_UNAVAILABLE_HINTS = (
    "not available for this organization",
    "not enabled for this organization",
    "invalid_type",
    "translation workbench",
    "invalid language",
    "language is not",
)


def index_entries(entries: list[dict]) -> dict[str, dict]:
    """Group translation entries ONCE: {object: {"object": [...], "fields": {...}}}.

    Generation is then a single pass over indexed dictionaries instead of
    re-scanning every entry per field (which is quadratic on a 200-field
    object). Field keys are sorted so repeated runs emit identical files.
    """
    idx: dict[str, dict] = {}
    for e in entries:
        obj = norm(e.get("component"))
        if not obj:
            continue
        bucket = idx.setdefault(obj, {"object": [], "fields": {}})
        if e["kind"] in {KIND_OBJECT_LABEL, KIND_NAME_FIELD}:
            bucket["object"].append(e)
            continue
        field = norm(e.get("field")) or norm(e.get("key")).split("::", 1)[0]
        if not field:
            continue
        bucket["fields"].setdefault(field, []).append(e)
    for bucket in idx.values():
        bucket["fields"] = {k: bucket["fields"][k] for k in sorted(bucket["fields"])}
    return dict(sorted(idx.items()))


def read_object_translations(objs: list[str], lang: str, auth: dict) -> list[ET.Element]:
    """readMetadata(CustomObjectTranslation) with a Translation-Workbench preflight.

    Raises TranslationUnavailable (actionable) when the org cannot serve
    translations for `lang` at all, so the caller can either fail loudly or
    skip translations explicitly — never emit an opaque SOAP fault.
    """
    members = [f"{o}-{lang}" for o in objs if o]
    if not members:
        return []
    try:
        return read_metadata("CustomObjectTranslation", members,
                             auth["accessToken"], auth["instanceUrl"],
                             auth["apiVersion"])
    except MetadataApiError as e:
        low = str(e).lower()
        if any(h in low for h in _UNAVAILABLE_HINTS):
            raise TranslationUnavailable(
                f"the org cannot serve CustomObjectTranslation for '{lang}'.\n"
                f"   org said: {str(e)[:300]}\n"
                f"   enable Setup → Translation Workbench → Translation Language "
                f"Settings and activate '{lang}', or run with --on-unavailable skip.")
        raise


def is_base_case_value(cv: ET.Element) -> bool:
    """The plain singular object label, not a gender/case/possessive variant."""
    if (cv.findtext("plural") or "").lower() == "true":
        return False
    return not any(norm(cv.findtext(t)) for t in ("caseType", "possessive", "article"))


def parse_object_translation(rec: ET.Element, obj: str, lang: str) -> dict[str, dict]:
    """CustomObjectTranslation SOAP record → {entry_id: org-entry}."""
    out: dict[str, dict] = {}
    # object label lives in caseValues (singular = plural false)
    for cv in rec.findall("caseValues"):
        val = norm(cv.findtext("value"))
        if val and is_base_case_value(cv):
            e = make_entry(kind=KIND_OBJECT_LABEL, component=obj, aspect="label",
                           key=obj, language=lang, master="", translation=val,
                           source="org")
            out[e["id"]] = e
    # the standard Name field is translated by the PARENT <nameFieldLabel>,
    # not by a Name.fieldTranslation child.
    name_label = norm(rec.findtext("nameFieldLabel"))
    if name_label:
        e = make_entry(kind=KIND_NAME_FIELD, component=obj, aspect="label",
                       key="Name", language=lang, master="",
                       translation=name_label, source="org")
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


# --------------------------------------------------------------------------- #
# Source-format preservation
#
# A CustomObjectTranslation carries far more than field labels: recordTypes,
# layouts, validationRules, fieldSets, quickActions, webLinks, sharingReasons,
# workflowTasks, startsWith/gender and the plural/case caseValues variants.
# The deploy member is the WHOLE object-language pair, so anything we fail to
# write back is anything we erase. We therefore never rebuild the file from our
# own reduced model: we keep the org's element tree and patch only the nodes we
# actually intend to change.
# --------------------------------------------------------------------------- #

# Metadata-API-only wrappers that must not be written to source format.
_DROP_PARENT_TAGS = {"fullName"}


def _skip(el: ET.Element) -> bool:
    return el.get("nil") == "true"


def element_xml(el: ET.Element, tag: str = "", level: int = 1) -> list[str]:
    """Serialize a namespace-stripped element as indented source-format lines."""
    tag = tag or el.tag
    pad = "    " * level
    kids = [k for k in list(el) if not _skip(k)]
    if not kids:
        text = norm(el.text)
        return [f"{pad}<{tag}>{esc(text)}</{tag}>"] if text else [f"{pad}<{tag}/>"]
    lines = [f"{pad}<{tag}>"]
    for k in kids:
        lines += element_xml(k, level=level + 1)
    lines.append(f"{pad}</{tag}>")
    return lines


def render_metadata(root_tag: str, children: list[ET.Element],
                    order: list[str]) -> str:
    """Render a metadata file, ordering children per the Metadata API sequence."""
    rank = {t: i for i, t in enumerate(order)}
    ordered = sorted(
        [c for c in children if not _skip(c) and c.tag not in _DROP_PARENT_TAGS],
        key=lambda c: rank.get(c.tag, len(order)))
    lines = [f'<{root_tag} xmlns="{NS}">']
    for c in ordered:
        lines += element_xml(c)
    lines += [f"</{root_tag}>", ""]
    return "\n".join(lines)


def split_object_translation(rec: ET.Element | None) -> tuple[list[ET.Element],
                                                              dict[str, ET.Element]]:
    """Split an org COT record into (parent children, {fieldName: <fields> element}).

    Source format stores the parent nodes in
    ``<Obj>-<lang>.objectTranslation-meta.xml`` and each field in its own
    ``<Field>.fieldTranslation-meta.xml``, so the two halves are split here and
    both are written back untouched unless explicitly patched.
    """
    parent: list[ET.Element] = []
    fields: dict[str, ET.Element] = {}
    if rec is None:
        return parent, fields
    for child in list(rec):
        if _skip(child) or child.tag in _DROP_PARENT_TAGS:
            continue
        if child.tag == "fields":
            name = norm(child.findtext("name"))
            if name:
                fields[name] = child
            continue
        parent.append(child)
    return parent, fields


def set_text(parent: ET.Element, tag: str, value: str) -> None:
    """Create-or-update a single-valued child element."""
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.attrib.pop("nil", None)
    el.text = norm(value)


def field_element(name: str) -> ET.Element:
    el = ET.Element("fields")
    set_text(el, "name", name)
    return el


def set_picklist_translation(field_el: ET.Element, master: str, translation: str) -> None:
    """Patch one picklistValues pair, leaving every other value untouched."""
    for pv in field_el.findall("picklistValues"):
        if norm(pv.findtext("masterLabel")) == norm(master):
            set_text(pv, "translation", translation)
            return
    pv = ET.SubElement(field_el, "picklistValues")
    set_text(pv, "masterLabel", master)
    set_text(pv, "translation", translation)


def set_object_label(parent: list[ET.Element], label: str) -> list[ET.Element]:
    """Patch the base singular caseValues; plural/case variants are preserved."""
    for cv in parent:
        if cv.tag == "caseValues" and is_base_case_value(cv):
            set_text(cv, "value", label)
            return parent
    cv = ET.Element("caseValues")
    set_text(cv, "plural", "false")
    set_text(cv, "value", label)
    return parent + [cv]


def set_parent_text(parent: list[ET.Element], tag: str, value: str) -> list[ET.Element]:
    for el in parent:
        if el.tag == tag:
            el.attrib.pop("nil", None)
            el.text = norm(value)
            return parent
    el = ET.Element(tag)
    el.text = norm(value)
    return parent + [el]
