#!/usr/bin/env python3
"""
run.py — One-command pipeline for the sheet → package flow (NO deploy, NO org).

Runs, in order:
  1. fetch_sheet.py    Google Sheet  -> temp_updates.json
  2. validate_sheet.py temp_updates.json -> validation log + .build/validation_report.json
     (HARD GATE: stops here if any ERROR)
  3. generate_xml.py   temp_updates.json -> force-app/main/default/objects/**
  4. build_manifest.py force-app -> manifest/package.xml

Deployment is intentionally NOT part of this script. Deploy uses the canonical
org-aware entry point:

  python scripts/prep_deploy.py --org "<ORG>" --tabs "<TABS>" --phase deploy

Usage:
  python scripts/run.py --spreadsheet-id <ID> [--tabs "成約"] [--only Obj__c] [--strict]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def step(title: str, cmd: list[str]) -> int:
    print("\n" + "═" * 72)
    print(f"▶  {title}")
    print("═" * 72)
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Run sheet->package pipeline (no deploy)")
    ap.add_argument("--spreadsheet-id", required=True)
    ap.add_argument("--tabs", default="", help="REQUIRED scope: object tab(s) to process (comma-separated)")
    ap.add_argument("--all-tabs", action="store_true",
                    help="explicitly process EVERY object tab (bypasses the scope gate)")
    ap.add_argument("--only", default="", help="restrict package.xml to these object API names")
    ap.add_argument("--strict", action="store_true", help="treat validation WARN as blocking")
    args = ap.parse_args()

    # ---- scope-selection gate --------------------------------------------- #
    if not args.tabs.strip() and not args.all_tabs:
        print("⛔ SCOPE REQUIRED — specify which object tab(s) to process.")
        print("   Pick tabs first:  python scripts/fetch_sheet.py --spreadsheet-id "
              f"{args.spreadsheet_id} --list-tabs")
        print("   Then run:         python scripts/run.py --spreadsheet-id <ID> --tabs \"<tab1,tab2>\"")
        print("   Or, to process the ENTIRE sheet on purpose:  ... --all-tabs")
        return 2

    s = str(ROOT / "scripts")

    # 1. fetch
    rc = step("1/4  Fetch sheet → temp_updates.json",
              [PY, f"{s}/fetch_sheet.py", "--spreadsheet-id", args.spreadsheet_id,
               *(["--tabs", args.tabs] if args.tabs else []),
               "--out", "temp_updates.json"])
    if rc != 0:
        print("✗ fetch failed."); return rc

    # 2. validate (hard gate)
    val_cmd = [PY, f"{s}/validate_sheet.py", "--in", "temp_updates.json",
               "--json", ".build/validation_report.json"]
    if args.strict:
        val_cmd.append("--strict")
    rc = step("2/4  Validate (deployment gate)", val_cmd)
    if rc != 0:
        print("\n⛔ Validation FAILED — pipeline stopped. Fix the sheet and re-run.")
        print("   No metadata was generated; nothing to deploy.")
        return rc

    # 3. generate metadata
    rc = step("3/4  Generate metadata XML", [PY, f"{s}/generate_xml.py"])
    if rc != 0:
        print("✗ generation failed."); return rc

    # 4. build manifest
    man_cmd = [PY, f"{s}/build_manifest.py", "--out", "manifest/package.xml"]
    if args.only:
        man_cmd += ["--only", args.only]
    rc = step("4/4  Build package.xml", man_cmd)
    if rc != 0:
        print("✗ manifest build failed."); return rc

    print("\n" + "✔" * 36)
    print("Pipeline complete. Review:")
    print("   • force-app/main/default/objects/**   (generated metadata)")
    print("   • manifest/package.xml                (deploy manifest)")
    print("\nNext (org-aware deploy):")
    print("   python scripts/prep_deploy.py --org \"<org>\" --tabs \"<tabs>\"")
    print("   python scripts/prep_deploy.py --org \"<org>\" --tabs \"<tabs>\" --phase deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
