#!/usr/bin/env python3
"""
generate_translations.py — CustomLabels (master JA) + Translations (en_US)
from the I18N_LWC and I18N_Flows catalog tabs.

CRITICAL: CustomLabels and Translations are org-wide single files. This script
RETRIEVES the live org file, MERGES our delta, and writes the combined XML so
a partial catalog cannot wipe unrelated labels/flow strings.

Usage:
  python scripts/generate_translations.py --catalog .build/i18n_catalog.json \
      --delta .build/i18n_drift.json --org ERPDEV01 --lang en_US
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "scripts")
from i18n_lib import (  # noqa: E402
    DEFAULT_LANG, KIND_CUSTOM_LABEL, KIND_FLOW_CHOICE, KIND_FLOW_DEF,
    KIND_FLOW_ERROR, KIND_FLOW_SCREEN, KIND_FLOW_SCREEN_FIELD, KIND_FLOW_STAGE,
    KIND_FLOW_TEXT, MASTER_LANG, esc, load_token, parse_custom_labels_file,
    parse_translations, read_metadata, write_xml,
)

LABELS_PATH = Path("force-app/main/default/labels/CustomLabels.labels-meta.xml")
TRANS_DIR = Path("force-app/main/default/translations")


def _retrieve_labels(tok, inst, ver) -> dict[str, dict]:
    recs = read_metadata("CustomLabels", ["CustomLabels"], tok, inst, ver)
    out = {}
    for rec in recs:
        out.update(parse_custom_labels_file(rec))
    return out


def _retrieve_translations(tok, inst, ver, lang: str) -> dict[str, dict]:
    recs = read_metadata("Translations", [lang], tok, inst, ver)
    out = {}
    for rec in recs:
        out.update(parse_translations(rec, lang))
    return out


def render_custom_labels(labels: dict[str, dict]) -> str:
    lines = ['<CustomLabels xmlns="http://soap.sforce.com/2006/04/metadata">']
    for name in sorted(labels):
        lab = labels[name]
        prot = lab.get("protected") or "false"
        if str(prot).lower() in {"true", "1", "yes"}:
            prot = "true"
        else:
            prot = "false"
        lines += [
            "    <labels>",
            f"        <fullName>{esc(name)}</fullName>",
            f"        <categories>{esc(lab.get('categories') or 'LWC')}</categories>",
            f"        <language>{esc(lab.get('language') or MASTER_LANG)}</language>",
            f"        <protected>{prot}</protected>",
            f"        <shortDescription>{esc(lab.get('shortDescription') or name)}</shortDescription>",
            f"        <value>{esc(lab.get('value') or '')}</value>",
            "    </labels>",
        ]
    lines.append("</CustomLabels>")
    lines.append("")
    return "\n".join(lines)


def _flow_bucket(entries: list[dict]) -> dict[str, dict]:
    """component → structured flow translation ready for XML."""
    flows: dict[str, dict] = defaultdict(lambda: {
        "label": "", "screens": defaultdict(lambda: {"label": "", "fields": defaultdict(dict)}),
        "choices": {}, "textTemplates": {}, "errors": {}, "stages": {},
    })
    for e in entries:
        if not e.get("translation"):
            continue
        f = flows[e["component"]]
        k, aspect, key, t = e["kind"], e.get("aspect") or "label", e["key"], e["translation"]
        if k == KIND_FLOW_DEF:
            f["label"] = t
        elif k == KIND_FLOW_SCREEN:
            f["screens"][key]["label"] = t
        elif k == KIND_FLOW_SCREEN_FIELD:
            screen, _, fname = key.partition(".")
            f["screens"][screen]["fields"][fname][aspect] = t
        elif k == KIND_FLOW_CHOICE:
            f["choices"][key] = t
        elif k == KIND_FLOW_TEXT:
            f["textTemplates"][key] = t
        elif k == KIND_FLOW_ERROR:
            f["errors"][key] = t
        elif k == KIND_FLOW_STAGE:
            f["stages"][key] = t
    return flows


def render_translations(lang: str, label_entries: list[dict],
                        flow_entries: list[dict],
                        org_xml_labels: list[tuple[str, str]],
                        org_flow_xml: str | None) -> str:
    """Merge delta label/flow translations on top of org members.

    org_xml_labels: [(name, label), ...] already in org, not in our delta —
    kept so we do not wipe them. Flow merge is done at the structured-dict
    level by the caller (org entries converted to catalog shape).
    """
    lines = [f'<Translations xmlns="http://soap.sforce.com/2006/04/metadata">']
    # custom labels: delta first (sheet wins), then org-only
    seen = set()
    for e in label_entries:
        if not e.get("translation"):
            continue
        seen.add(e["component"])
        lines += [
            "    <customLabels>",
            f"        <label>{esc(e['translation'])}</label>",
            f"        <name>{esc(e['component'])}</name>",
            "    </customLabels>",
        ]
    for name, label in org_xml_labels:
        if name in seen or not label:
            continue
        lines += [
            "    <customLabels>",
            f"        <label>{esc(label)}</label>",
            f"        <name>{esc(name)}</name>",
            "    </customLabels>",
        ]
    flows = _flow_bucket(flow_entries)
    for flow_api in sorted(flows):
        f = flows[flow_api]
        lines += ["    <flowDefinitions>", f"        <fullName>{esc(flow_api)}</fullName>"]
        if f["label"]:
            lines.append(f"        <label>{esc(f['label'])}</label>")
        for ename, emsg in sorted(f["errors"].items()):
            lines += [
                "        <customErrorMessages>",
                f"            <errorMessage>{esc(emsg)}</errorMessage>",
                f"            <name>{esc(ename)}</name>",
                "        </customErrorMessages>",
            ]
        for sname, sc in sorted(f["screens"].items()):
            lines += ["        <screens>", f"            <name>{esc(sname)}</name>"]
            if sc.get("label"):
                lines.append(f"            <label>{esc(sc['label'])}</label>")
            for fname, aspects in sorted(sc["fields"].items()):
                lines += ["            <fields>", f"                <name>{esc(fname)}</name>"]
                if aspects.get("fieldText"):
                    lines.append(f"                <fieldText>{esc(aspects['fieldText'])}</fieldText>")
                if aspects.get("help"):
                    lines.append(f"                <helpText>{esc(aspects['help'])}</helpText>")
                lines.append("            </fields>")
            lines.append("        </screens>")
        for cname, ctxt in sorted(f["choices"].items()):
            lines += [
                "        <choices>",
                f"            <choiceText>{esc(ctxt)}</choiceText>",
                f"            <name>{esc(cname)}</name>",
                "        </choices>",
            ]
        for tname, ttxt in sorted(f["textTemplates"].items()):
            lines += [
                "        <textTemplates>",
                f"            <name>{esc(tname)}</name>",
                f"            <text>{esc(ttxt)}</text>",
                "        </textTemplates>",
            ]
        for sname, slabel in sorted(f["stages"].items()):
            lines += [
                "        <stages>",
                f"            <label>{esc(slabel)}</label>",
                f"            <name>{esc(sname)}</name>",
                "        </stages>",
            ]
        lines.append("    </flowDefinitions>")
    lines.append("</Translations>")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate CustomLabels + Translations XML")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--delta", default="")
    ap.add_argument("--org", default="")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--labels-out", default=str(LABELS_PATH))
    ap.add_argument("--trans-out", default="")
    args = ap.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    catalog = [e for e in catalog if e.get("language", args.lang) == args.lang]
    package_ids = None
    if args.delta:
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        package_ids = {d["id"] for d in delta if d.get("package")}

    def take(kind_pred):
        rows = [e for e in catalog if kind_pred(e["kind"])]
        if package_ids is None:
            return [e for e in rows if e.get("translation")]
        return [e for e in rows if e["id"] in package_ids and e.get("translation")]

    label_delta = take(lambda k: k == KIND_CUSTOM_LABEL)
    flow_delta = take(lambda k: k.startswith("Flow"))

    org_labels: dict[str, dict] = {}
    org_trans: dict[str, dict] = {}
    if args.org:
        a = load_token(args.org)
        tok, inst, ver = a["accessToken"], a["instanceUrl"].rstrip("/"), a["apiVersion"]
        org_labels = _retrieve_labels(tok, inst, ver)
        org_trans = _retrieve_translations(tok, inst, ver, args.lang)
        print(f"  retrieved org CustomLabels={len(org_labels)}  Translations[{args.lang}]={len(org_trans)}")

    # Master CustomLabels: org ∪ new labels from the LWC tab (JA master value).
    merged_labels = dict(org_labels)
    created = 0
    for e in [x for x in catalog if x["kind"] == KIND_CUSTOM_LABEL]:
        name = e["component"]
        if name not in merged_labels:
            master = e.get("master") or e.get("translation") or name
            merged_labels[name] = {
                "fullName": name,
                "value": master,
                "language": MASTER_LANG,
                "categories": e.get("categories") or "LWC",
                "shortDescription": e.get("short_description") or name,
                "protected": e.get("protected") or "false",
            }
            created += 1
    write_xml(Path(args.labels_out), render_custom_labels(merged_labels))
    print(f"  CustomLabels → {args.labels_out}  ({len(merged_labels)} labels, {created} new)")

    # Translations file: merge org customLabels + our delta. Flow org members
    # that are not in the catalog stay as extra catalog-shaped entries.
    org_label_pairs = []
    org_flow_as_entries = []
    for eid, oe in org_trans.items():
        if oe["kind"] == KIND_CUSTOM_LABEL:
            org_label_pairs.append((oe["component"], oe.get("translation") or ""))
        elif oe["kind"].startswith("Flow"):
            org_flow_as_entries.append(oe)
    # sheet delta wins: put org flow entries first, then overwrite via _flow_bucket order
    flow_merged = org_flow_as_entries + flow_delta

    trans_path = Path(args.trans_out) if args.trans_out else TRANS_DIR / f"{args.lang}.translation-meta.xml"
    write_xml(trans_path, render_translations(
        args.lang, label_delta, flow_merged, org_label_pairs, None))
    print(f"  Translations → {trans_path}  "
          f"(labels delta={len(label_delta)}, flow delta={len(flow_delta)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
