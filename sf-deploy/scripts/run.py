#!/usr/bin/env python3
"""
run.py — thin alias for the canonical pipeline's BUILD phase.

There is exactly ONE deployment entry point: `prep_deploy.py`. It is the only
script that performs the full chain (live sheet fetch → validation → org
snapshot → object/field delta → translation delta → staged generation →
manifest → check-only → real deploy → live verification), so this script simply
delegates to it rather than running a second, translation-blind pipeline of its
own (which is what it used to do: fetch → validate → generate_xml →
build_manifest, with no delta and no translations).

  python scripts/run.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>"

is equivalent to the canonical build phase:

  python scripts/prep_deploy.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>" \
      --phase build

To deploy, use the canonical command directly:

  python scripts/prep_deploy.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>" \
      --phase deploy
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Alias for `prep_deploy.py --phase build` (no org writes)")
    ap.add_argument("--org", required=True,
                    help="target org: the delta needs a live org snapshot, so a "
                         "build without an org is no longer meaningful")
    ap.add_argument("--tabs", required=True, help="object tab(s), comma-separated")
    ap.add_argument("--spreadsheet-id", default="")
    ap.add_argument("--lang", default="")
    ap.add_argument("--out", default="")
    args, extra = ap.parse_known_args()

    cmd = [sys.executable, str(SCRIPTS / "prep_deploy.py"),
           "--org", args.org, "--tabs", args.tabs, "--phase", "build"]
    if args.spreadsheet_id:
        cmd += ["--sheet-id", args.spreadsheet_id]
    if args.lang:
        cmd += ["--lang", args.lang]
    if args.out:
        cmd += ["--out", args.out]
    cmd += extra

    print("ℹ️  run.py delegates to the canonical entry point:")
    print("      " + " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
