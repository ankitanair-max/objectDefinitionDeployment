#!/usr/bin/env python3
"""audit_lib.py — shared live-org helpers for the deployment audit.

Uses a session token minted by get_token.py (.build/orgauth.json). Provides:
  - rest_query / tooling_query  (SOQL over REST / Tooling)
  - read_object_meta            (readMetadata CustomObject -> fields + actionOverrides)
  - read_flexipage              (readMetadata FlexiPage -> {field: section})
  - read_customtab              (readMetadata CustomTab -> exists/label)
All read-only.
"""
from __future__ import annotations
import json, re, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path


def load_auth(token_file: str = ".build/orgauth.json") -> dict:
    return json.load(open(token_file))["result"]


def _strip_ns(xml: str) -> str:
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', '', xml)
    xml = re.sub(r'<(/?)\w+:', r'<\1', xml)
    xml = re.sub(r'\s\w+:(\w+=)', r' \1', xml)
    return xml


def rest_query(soql: str, auth: dict, tooling: bool = False) -> list[dict]:
    inst, ver, tok = auth["instanceUrl"].rstrip("/"), auth["apiVersion"], auth["accessToken"]
    path = "tooling/query" if tooling else "query"
    url = f"{inst}/services/data/v{ver}/{path}?q=" + urllib.parse.quote(soql)
    out: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"query HTTP {e.code}: {e.read().decode()[:300]}\nSOQL: {soql}")
        out += data.get("records", [])
        nxt = data.get("nextRecordsUrl")
        url = (inst + nxt) if nxt else None
    return out


def _soap(body_inner: str, auth: dict) -> str:
    inst, ver, tok = auth["instanceUrl"].rstrip("/"), auth["apiVersion"], auth["accessToken"]
    soap = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:met="http://soap.sforce.com/2006/04/metadata"><soapenv:Header>'
        f'<met:SessionHeader><met:sessionId>{tok}</met:sessionId></met:SessionHeader>'
        f'</soapenv:Header><soapenv:Body>{body_inner}</soapenv:Body></soapenv:Envelope>'
    )
    req = urllib.request.Request(f"{inst}/services/Soap/m/{ver}", data=soap.encode(),
                                 headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'})
    try:
        return urllib.request.urlopen(req, timeout=120).read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()


def read_object_meta(api: str, auth: dict) -> dict | None:
    """-> {'fields': {fullName: {attrs...}}, 'defaultPages': {formFactor: page}, 'raw': xml}"""
    xml = _soap(f'<met:readMetadata><met:type>CustomObject</met:type>'
                f'<met:fullNames>{api}</met:fullNames></met:readMetadata>', auth)
    xml = _strip_ns(xml)
    root = ET.fromstring(xml)
    rec = root.find(".//records")
    if rec is None or rec.find("fullName") is None:
        return None
    fields = {}
    for f in root.iter("fields"):
        d = {c.tag: (c.text or "") for c in f}
        vals = [v.findtext("fullName", "") for v in f.iter("value")]
        if vals:
            d["_picklist"] = [x for x in vals if x]
        fields[d.get("fullName", "")] = d
    default_pages = {}
    for ao in root.iter("actionOverrides"):
        if ao.findtext("actionName") == "View" and ao.findtext("type") == "Flexipage":
            default_pages[ao.findtext("formFactor") or "?"] = ao.findtext("content") or ""
    return {"fields": fields, "defaultPages": default_pages, "raw": xml}


def read_flexipage(name: str, auth: dict) -> dict | None:
    """-> {'fields': {fieldApiName: sectionLabel}, 'exists': True, 'raw': xml}.
    Section is derived from the enclosing fieldSection component's label when present."""
    xml = _soap(f'<met:readMetadata><met:type>FlexiPage</met:type>'
                f'<met:fullNames>{name}</met:fullNames></met:readMetadata>', auth)
    xml = _strip_ns(xml)
    root = ET.fromstring(xml)
    rec = root.find(".//records")
    if rec is None or rec.find("fullName") is None:
        return None
    parent = {c: p for p in root.iter() for c in p}

    def region_label(node):
        """Walk up to find the nearest componentInstance whose componentName is a
        field/section, using its 'label' property, else the flexiPageRegions name."""
        cur = node
        while cur is not None:
            if cur.tag == "componentInstance":
                cname = cur.findtext("componentName", "")
                lbl = ""
                for p in cur.iter("componentInstanceProperties"):
                    if p.findtext("name") == "label":
                        lbl = p.findtext("value") or ""
                        break
                if lbl:
                    return lbl
                if cname:
                    return cname
            if cur.tag == "flexiPageRegions":
                nm = cur.findtext("name", "")
                if nm:
                    return nm
            cur = parent.get(cur)
        return ""

    fields = {}
    for fi in root.iter("fieldInstance"):
        item = fi.findtext("fieldItem", "")  # e.g. Record.TI_Fnt_Foo__c
        fname = item.split(".", 1)[1] if item.startswith("Record.") else item
        if fname:
            fields.setdefault(fname, region_label(fi))
    return {"fields": fields, "exists": True, "raw": xml}


def read_customtab(name: str, auth: dict) -> dict | None:
    xml = _soap(f'<met:readMetadata><met:type>CustomTab</met:type>'
                f'<met:fullNames>{name}</met:fullNames></met:readMetadata>', auth)
    xml = _strip_ns(xml)
    root = ET.fromstring(xml)
    rec = root.find(".//records")
    if rec is None or rec.find("fullName") is None:
        return None
    return {"fullName": rec.findtext("fullName"), "label": rec.findtext("label", ""),
            "motif": rec.findtext("motif", ""), "customObject": rec.findtext("customObject", "")}
