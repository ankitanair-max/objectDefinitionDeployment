#!/usr/bin/env python3
"""
prep_deploy.py — DEPRECATED shim. The deployment command is `deploy.py`.

    old:  python scripts/prep_deploy.py --org <ORG> --tabs "<TABS>" --phase deploy
    new:  python scripts/deploy.py      --org <ORG> --tabs "<TABS>" --start

This forwards to deploy.py so existing runbooks keep working; `--phase build`
maps to the default check-only mode and `--phase deploy` maps to `--start`.
It will be removed once the runbooks are updated.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402


def translate(argv: list[str]) -> list[str]:
    out: list[str] = []
    it = iter(range(len(argv)))
    skip = -1
    for i, a in enumerate(argv):
        if i == skip:
            continue
        if a == "--phase":
            phase = argv[i + 1] if i + 1 < len(argv) else "build"
            skip = i + 1
            if phase == "deploy":
                out.append("--start")
            continue
        if a.startswith("--phase="):
            if a.split("=", 1)[1] == "deploy":
                out.append("--start")
            continue
        out.append(a)
    del it
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print("⚠️  prep_deploy.py is deprecated — use:\n"
          "      python scripts/deploy.py --org <ORG> --tabs \"<TABS>\" [--start]\n")
    return deploy.main(translate(argv))


if __name__ == "__main__":
    sys.exit(main())
