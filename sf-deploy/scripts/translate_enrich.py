#!/usr/bin/env python3
"""
translate_enrich.py — automatic JA→EN label enrichment on selected live tabs.

Called from prep_deploy.py (not a parallel command). Dry-run by default;
`--apply` writes the confirmed batch after a stale-check.

Steps:
  collect in-scope labels → provider preflight (DeepL else Google) →
  glossary resolve → translate remainder → preview → (apply) stale-check write →
  read calculated values.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from mcp_deepl import DeepLProvider, DeepLTranslateError, DeepLUnavailable
from sheet_client import SheetClient
from translation_lib import (
    FIELD_EN_HEADER,
    KIND_NAME_FIELD,
    KIND_OBJECT_FIELD,
    KIND_OBJECT_LABEL,
    OBJECT_EN_LABEL,
    OBJECT_GENERATED_LABEL,
    OBJECT_HASH_LABEL,
    OBJECT_ORIGIN_LABEL,
    OBJECT_PROVENANCE_LABEL,
    ORIGIN_DEEPL,
    ORIGIN_GOOGLE,
    ORIGIN_HEADER,
    ORIGIN_MANUAL,
    GENERATED_HEADER,
    HASH_HEADER,
    PROVENANCE_HEADER,
    SF_LANG,
    a1 as sheets_a1,
    cell_at,
    classify_need,
    content_hash,
    format_provenance,
    google_formula,
    glossary_lookup,
    build_glossary,
    header_index,
    invalid_english,
    is_delete,
    now_iso,
    norm,
    parse_provenance,
    plan_missing_headers,
    truthy,
)


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


def _cell(grid, r0, c) -> str:
    if c is None:
        return ""
    return cell_at(grid, r0, c)


def _find_meta_value(grid: list[list[str]], header_idx: int, *labels: str) -> tuple[str, int, int]:
    """Return (value, row0, col0) for a labeled cell in the object-meta block."""
    want = {norm(x).lower() for x in labels}
    for r in range(header_idx):
        row = grid[r] if r < len(grid) else []
        for c, val in enumerate(row):
            if norm(val).lower() in want:
                # value is the next non-blank to the right
                for k in range(c + 1, max(len(row) + 4, c + 8)):
                    v = cell_at(grid, r, k)
                    # skip if the next cell is itself a known label
                    if v and norm(v).lower() not in want and not v.endswith(":"):
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

    obj_en, obj_en_r, obj_en_c = _find_meta_value(
        grid, hidx, OBJECT_EN_LABEL, "表示ラベル (EN)", "表示ラベル(EN)")
    obj_pv, obj_pv_r, obj_pv_c = _find_meta_value(
        grid, hidx, OBJECT_PROVENANCE_LABEL, "翻訳出典")
    obj_or, obj_or_r, obj_or_c = _find_meta_value(grid, hidx, OBJECT_ORIGIN_LABEL)
    obj_hs, obj_hs_r, obj_hs_c = _find_meta_value(grid, hidx, OBJECT_HASH_LABEL)
    obj_gn, obj_gn_r, obj_gn_c = _find_meta_value(grid, hidx, OBJECT_GENERATED_LABEL)
    obj_ja, obj_ja_r, obj_ja_c = _find_meta_value(grid, hidx, "表示ラベル")
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
            "ja_a1": sheets_a1(ja_col, i + 1) if ja_col is not None else "",
            "en_a1": sheets_a1(en_col, i + 1) if en_col is not None else "",
            "prov_a1": sheets_a1(prov_col, i + 1) if prov_col is not None else "",
            "origin_a1": sheets_a1(origin_col, i + 1) if origin_col is not None else "",
            "hash_a1": sheets_a1(hash_col, i + 1) if hash_col is not None else "",
            "gen_a1": sheets_a1(gen_col, i + 1) if gen_col is not None else "",
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
    deepl_factory: Callable[[], DeepLProvider] | None = None,
) -> tuple[str, DeepLProvider | None]:
    """Pick ONE provider for the whole batch. Never mix."""
    force = (force or "").strip().lower()
    if force == "google":
        return ORIGIN_GOOGLE, None
    factory = deepl_factory or DeepLProvider
    provider = factory()
    try:
        provider.preflight()
        return ORIGIN_DEEPL, provider
    except DeepLUnavailable:
        if force == "deepl":
            raise TranslationAbort(
                "⛔ DeepL was required (--force-provider deepl) but preflight failed."
            )
        provider.close()
        return ORIGIN_GOOGLE, None


def _ensure_object_meta_cell(loc: dict, key: str, label: str, header_row: int) -> dict:
    """If a meta provenance/EN cell is missing, park it on the header-1 row."""
    slot = loc[key]
    if slot["row0"] >= 0 and slot["col0"] >= 0:
        return slot
    # Place on the row above the field header, appending to the right.
    row0 = max(0, header_row - 2)
    col0 = 10  # column K — meta block is typically A–J
    slot = {"value": "", "row0": row0, "col0": col0, "create_label": label}
    loc[key] = slot
    return slot


def build_jobs(locs: list[dict], object_rows: list[dict]) -> list[dict]:
    """In-scope translation jobs (object, Name, custom fields)."""
    jobs = []
    obj_by_tab = {norm(r.get("_SheetName")): r for r in object_rows if r.get("_type") == "object_meta"}
    for loc in locs:
        tab = loc["tab"]
        meta = obj_by_tab.get(tab) or next(
            (r for r in object_rows if r.get("_type") == "object_meta"), {}
        )
        obj = norm(meta.get("Object API Name"))
        # object label
        ja = loc["object_ja"]["value"] or norm(meta.get("Object Label"))
        en = loc["object_en"]["value"] or norm(meta.get("Object Label (EN)"))
        jobs.append({
            "kind": KIND_OBJECT_LABEL,
            "tab": tab,
            "object_api": obj,
            "field_api": obj,
            "ja": ja,
            "en": en,
            "origin": loc["object_origin"]["value"],
            "source_hash": loc["object_hash"]["value"],
            "generated_at": loc["object_generated"]["value"],
            "ja_a1": sheets_a1(loc["object_ja"]["col0"], loc["object_ja"]["row0"] + 1)
            if loc["object_ja"]["row0"] >= 0 and loc["object_ja"]["col0"] >= 0 else "",
            "en_a1": sheets_a1(loc["object_en"]["col0"], loc["object_en"]["row0"] + 1)
            if loc["object_en"]["row0"] >= 0 and loc["object_en"]["col0"] >= 0 else "",
            "prov_a1": sheets_a1(loc["object_prov"]["col0"], loc["object_prov"]["row0"] + 1)
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
    *,
    fail_after_preflight: bool = False,
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

    if fail_after_preflight and provider_name == ORIGIN_DEEPL:
        raise DeepLTranslateError("forced DeepL mid-batch failure (test hook)")

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
        loc = locs_by_tab[tab]
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
            writes.append(_write(
                tab, cell, old, new, mode if which == "en" else "RAW",
                j["kind"], field, obj, ja, j.get("ja_a1") or "", which,
            ))

        if j["kind"] == KIND_OBJECT_LABEL:
            # Ensure object EN cell exists (may need a created header cell).
            if not j.get("en_a1"):
                slot = _ensure_object_meta_cell(loc, "object_en", OBJECT_EN_LABEL, loc["header_row"])
                j["en_a1"] = sheets_a1(slot["col0"], slot["row0"] + 1)
                writes.append(_write(
                    tab, sheets_a1(max(slot["col0"] - 1, 0), slot["row0"] + 1),
                    "", OBJECT_EN_LABEL, "RAW", j["kind"], field, obj, ja, note="header",
                ))
            add(j.get("en_a1"), j.get("old_en"), en_new, "en")
            old_p = format_provenance(j.get("old_origin"), j.get("old_hash"), j.get("old_generated"))
            new_p = format_provenance(j.get("origin"), j.get("source_hash"), j.get("generated_at"))
            add(j.get("prov_a1") or _meta_a1(loc, "object_prov", OBJECT_PROVENANCE_LABEL),
                old_p, new_p, "provenance")
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


def _meta_a1(loc, key, label) -> str:
    slot = _ensure_object_meta_cell(loc, key, label, loc["header_row"])
    return sheets_a1(slot["col0"], slot["row0"] + 1)


def header_writes(locs: list[dict]) -> list[CellWrite]:
    out: list[CellWrite] = []
    for loc in locs:
        missing = loc.get("missing_headers") or {}
        hrow = loc["header_row"]
        hdr_cells = loc.get("header_cells") or []
        tab = loc["tab"]
        for hdr, col in missing.items():
            cell = sheets_a1(col, hrow)
            old = hdr_cells[col] if col < len(hdr_cells) else ""
            out.append(_write(tab, cell, old, hdr, "RAW", "header", hdr, "", "", note="header"))
            # JP header row (one above) when present
            if hrow > 1:
                jp = {
                    FIELD_EN_HEADER: "項目ラベル名 (EN)",
                    PROVENANCE_HEADER: "翻訳出典",
                }.get(hdr, hdr)
                out.append(_write(
                    tab, sheets_a1(col, hrow - 1), "", jp, "RAW",
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


def snapshot_cells(sheet: SheetClient, sid: str, writes: list[CellWrite]) -> dict[str, str]:
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
        got = sheet.read_cells(sid, tab, cells)
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


def apply_writes(sheet: SheetClient, sid: str, writes: list[CellWrite]) -> None:
    formulas = [w for w in writes if w.mode == "USER_ENTERED"]
    literals = [w for w in writes if w.mode != "USER_ENTERED"]
    if literals:
        sheet.write_literals(sid, [{"range": w.range, "values": [[w.new]]} for w in literals])
    if formulas:
        sheet.write_formulas(sid, [{"range": w.range, "values": [[w.new]]} for w in formulas])


def read_calculated(sheet: SheetClient, sid: str, jobs: list[dict]) -> None:
    formula_jobs = [j for j in jobs if j.get("formula") and j.get("en_a1") and j.get("action") == "translate"]
    by_tab: dict[str, list[dict]] = {}
    for j in formula_jobs:
        by_tab.setdefault(j["tab"], []).append(j)
    for tab, group in by_tab.items():
        cells = [j["en_a1"] for j in group]
        values = sheet.wait_recalc(sid, tab, cells)
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


def merge_into_rows(rows: list[dict], jobs: list[dict]) -> list[dict]:
    by_obj_field = {(j["object_api"], j["field_api"], j["kind"]): j for j in jobs}
    for r in rows:
        obj = norm(r.get("Object API Name"))
        if r.get("_type") == "object_meta":
            j = by_obj_field.get((obj, obj, KIND_OBJECT_LABEL))
            if j and j.get("en_new"):
                r["Object Label (EN)"] = j["en_new"]
                r["Object Translation Provenance"] = format_provenance(
                    j.get("origin", ""), j.get("source_hash", ""), j.get("generated_at", ""))
                r["Object Translation Origin"] = j.get("origin", "")
                r["Object Translation Source Hash"] = j.get("source_hash", "")
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
    sheet: SheetClient,
    apply: bool = False,
    force_provider: str = "",
    fail_after_preflight: bool = False,
    deepl_factory: Callable[[], DeepLProvider] | None = None,
    grids: dict[str, list[list[str]]] | None = None,
) -> Enrichment:
    from fetch_sheet import parse_tab

    enr = Enrichment(spreadsheet_id=spreadsheet_id, tabs=list(tabs))
    locs = []
    parsed_all = []
    for tab in tabs:
        grid = (grids or {}).get(tab) if grids else None
        if grid is None:
            grid = sheet.read_grid(spreadsheet_id, tab)
        parsed, loc = collect_tab(tab, grid, parse_tab_fn=parse_tab)
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
        if j.get("action") == "skip_flag":
            continue
        act = classify_need(j.get("ja", ""), j.get("en", ""),
                            j.get("source_hash", ""), j.get("origin", ""))
        if act == "translate":
            en, why = glossary_lookup(glossary, api=j.get("field_api", ""), ja=j.get("ja", ""))
            if not en:
                probe.append(j)
    deepl = None
    if probe:
        enr.provider, deepl = select_provider(
            force=force_provider, deepl_factory=deepl_factory)
    else:
        enr.provider = force_provider or ORIGIN_MANUAL

    try:
        jobs, blocked = enrich_jobs(
            jobs, glossary, enr.provider if enr.provider in (ORIGIN_DEEPL, ORIGIN_GOOGLE)
            else ORIGIN_GOOGLE, deepl,
            fail_after_preflight=fail_after_preflight,
        )
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
    if blocked and any(b.get("reason", "").startswith("blank Japanese") for b in blocked):
        # blank JA on an in-scope deployable field is a hard blocker
        pass

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
        live = snapshot_cells(sheet, spreadsheet_id, writes)
        problems = stale_against(writes, live)
        if problems:
            enr.stale = True
            raise TranslationAbort(
                "⛔ stale/concurrent sheet edit detected — refusing to write.\n"
                + "\n".join(f"  {p}" for p in problems[:20])
                + "\nRe-run to build a fresh preview."
            )
        apply_writes(sheet, spreadsheet_id, writes)
        enr.applied = True
        if any(j.get("formula") for j in jobs):
            read_calculated(sheet, spreadsheet_id, jobs)
    enr.rows_patch = jobs
    return enr


def main() -> int:
    ap = argparse.ArgumentParser(description="Preview/apply JA→EN translation enrichment")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tabs", required=True)
    ap.add_argument("--rows", default="temp_updates.json")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-provider", default="", choices=["", "deepl", "google"])
    ap.add_argument("--out", default=".build/translation_preview.json")
    args = ap.parse_args()
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    rows = json.loads(Path(args.rows).read_text(encoding="utf-8")) if Path(args.rows).exists() else []
    with SheetClient() as sheet:
        try:
            enr = run_enrichment(
                spreadsheet_id=args.spreadsheet_id,
                tabs=tabs,
                rows=rows,
                sheet=sheet,
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
