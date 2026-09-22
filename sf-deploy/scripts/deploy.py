#!/usr/bin/env python3
"""
deploy.py — Safe local deploy orchestrator (wraps the Salesforce `sf` CLI).

This is the ONLY script that talks to a live org. It enforces a hard
validation gate before any deploy and defaults to check-only validation.

Safety model:
  * Default action is `validate` (check-only, `sf project deploy validate`) —
    it NEVER writes to the org.
  * `--start` performs a real deploy (`sf project deploy start`). This is a
    GATED action: the Cursor rule requires the user to type `SHOOT` before the
    assistant runs it.
  * A deploy is refused unless the validation report shows zero ERRORs, unless
    `--skip-validation-gate` is explicitly passed.

Usage:
  # check-only (safe, default)
  python scripts/deploy.py --package manifest/package.xml --target-org "ERP DEV 02"

  # real deploy (gated by SHOOT)
  python scripts/deploy.py --start --package manifest/package.xml \
      --target-org "ERP DEV 02" --test-level RunLocalTests

  # delete-only deploy
  python scripts/deploy.py --start --pre-destructive manifest/destructiveChanges.xml \
      --package manifest/package.xml --target-org "ERP DEV 02"
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

VALID_TEST_LEVELS = {"NoTestRun", "RunSpecifiedTests", "RunLocalTests", "RunAllTestsInOrg"}


def sf_available() -> str | None:
    return shutil.which("sf") or shutil.which("sfdx")


def list_connected_orgs() -> list[dict]:
    """Return connected (authenticated) orgs from `sf org list --json`.

    Each item: {alias, username, isDefault, status, instanceUrl}.
    """
    try:
        out = subprocess.run(["sf", "org", "list", "--json"],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
    except Exception as e:
        print(f"⚠️  could not list orgs: {e}")
        return []

    result = data.get("result", {}) or {}
    raw: list[dict] = []
    for bucket in ("nonScratchOrgs", "scratchOrgs", "devHubs", "sandboxes", "other"):
        raw.extend(result.get(bucket, []) or [])

    orgs: dict[str, dict] = {}
    for o in raw:
        username = o.get("username", "")
        if not username:
            continue
        status = str(o.get("connectedStatus") or o.get("status") or "").strip()
        # keep only usable (connected) orgs; drop expired/errored auths
        if status and status.lower() not in {"connected", "active"}:
            continue
        orgs[username] = {
            "alias": o.get("alias") or "",
            "username": username,
            "isDefault": bool(o.get("isDefaultUsername")),
            "status": status or "Connected",
            "instanceUrl": o.get("instanceUrl", ""),
        }
    # stable order: default first, then alias/username
    return sorted(orgs.values(),
                  key=lambda x: (not x["isDefault"], x["alias"] or x["username"]))


def print_org_table(orgs: list[dict]) -> None:
    print("=" * 72)
    print("  CONNECTED SALESFORCE ORGS")
    print("=" * 72)
    if not orgs:
        print("  (none) — connect one:  sf org login web --alias \"<ORG>\"")
        print("=" * 72)
        return
    for i, o in enumerate(orgs, 1):
        star = " *default" if o["isDefault"] else ""
        alias = o["alias"] or "(no alias)"
        print(f"  [{i}] {alias:24} {o['username']:34} {o['status']}{star}")
        if o["instanceUrl"]:
            print(f"      {o['instanceUrl']}")
    print("=" * 72)


def resolve_target_org(explicit: str, orgs: list[dict]) -> str | None:
    """Resolve to a connected org. Requires an explicit selection — NEVER
    silently falls back to the configured default (deploy quality gate)."""
    if not explicit:
        return None
    # accept selection by index, alias, or username
    if explicit.isdigit():
        idx = int(explicit)
        if 1 <= idx <= len(orgs):
            return orgs[idx - 1]["alias"] or orgs[idx - 1]["username"]
        return None
    for o in orgs:
        if explicit in (o["alias"], o["username"]):
            return o["alias"] or o["username"]
    # allow a not-yet-listed alias/username to pass through (user knows best),
    # but warn — it still must authenticate at deploy time.
    return explicit


def validation_has_errors(report_path: Path) -> bool | None:
    """True/False if report exists; None if no report (unknown)."""
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text())
        return int(data.get("counts", {}).get("ERROR", 0)) > 0
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy generated metadata via sf CLI")
    ap.add_argument("--package", default="manifest/package.xml")
    ap.add_argument("--pre-destructive", default="", help="destructiveChanges.xml (pre-deploy delete)")
    ap.add_argument("--post-destructive", default="", help="destructiveChanges.xml (post-deploy delete)")
    ap.add_argument("--target-org", default="",
                    help="REQUIRED for deploy: org alias, username, or list index. "
                         "No silent default — you must pick from the connected orgs.")
    ap.add_argument("--list-orgs", action="store_true",
                    help="print connected orgs and exit (org-selection gate)")
    ap.add_argument("--test-level", default="RunLocalTests", choices=sorted(VALID_TEST_LEVELS))
    ap.add_argument("--tests", default="", help="comma-separated tests for RunSpecifiedTests")
    ap.add_argument("--start", action="store_true", help="REAL deploy (gated). Default is check-only validate.")
    ap.add_argument("--validation-report", default=".build/validation_report.json")
    ap.add_argument("--skip-validation-gate", action="store_true")
    ap.add_argument("--wait", type=int, default=60)
    ap.add_argument("--ignore-conflicts", action="store_true",
                    help="pass --ignore-conflicts to sf (force local source over "
                         "source-tracking conflicts). Use only for intentional redeploys.")
    ap.add_argument("--dry-run", action="store_true", help="print the sf command, do not run")
    args = ap.parse_args()

    cli = sf_available()
    if not cli:
        print("❌ Salesforce CLI not found. Install with: npm i -g @salesforce/cli")
        return 1

    # ---- org-selection gate: ALWAYS show connected orgs -------------------- #
    orgs = list_connected_orgs()
    if args.list_orgs:
        print_org_table(orgs)
        return 0

    pkg = Path(args.package)
    if not pkg.exists():
        print(f"❌ package manifest '{pkg}' not found — run build_manifest.py first.")
        return 1

    org = resolve_target_org(args.target_org, orgs)
    if not org:
        print("⛔ DEPLOY BLOCKED — no target org selected.")
        print("   Every deployment must target an explicitly-chosen connected org.\n")
        print_org_table(orgs)
        print("Re-run with your choice (index, alias, or username), e.g.:")
        print("   python scripts/deploy.py --target-org 1            # by list index")
        print("   python scripts/deploy.py --target-org \"ERP DEV 02\"  # by alias")
        if not orgs:
            print("\nNo orgs connected yet:  sf org login web --alias \"<ORG>\"")
        return 2

    # confirm the resolved org is actually a connected one (warn if unknown)
    known = {o["alias"] for o in orgs} | {o["username"] for o in orgs}
    if orgs and org not in known:
        print(f"⚠️  '{org}' is not in the connected-orgs list — it must authenticate at deploy time.")

    # ---- validation gate --------------------------------------------------- #
    if not args.skip_validation_gate:
        has_err = validation_has_errors(Path(args.validation_report))
        if has_err is True:
            print(f"⛔ Validation report '{args.validation_report}' has ERRORS — deploy blocked.")
            print("   Fix the sheet and re-run:  python scripts/validate_sheet.py --json .build/validation_report.json")
            return 2
        if has_err is None:
            print(f"⚠️  No validation report at '{args.validation_report}'.")
            print("   Run validate_sheet.py first, or pass --skip-validation-gate to override.")
            return 2

    # Check-only uses `deploy start --dry-run` (validates, writes nothing,
    # accepts NoTestRun on sandboxes). `deploy validate` is reserved for
    # production quick-deploys and rejects NoTestRun, so we do not use it here.
    cmd = ["sf", "project", "deploy", "start",
           "--manifest", str(pkg),
           "--target-org", org,
           "--test-level", args.test_level,
           "--wait", str(args.wait)]
    if not args.start:
        cmd.append("--dry-run")
    if args.test_level == "RunSpecifiedTests":
        if not args.tests:
            print("❌ --test-level RunSpecifiedTests requires --tests")
            return 1
        for t in args.tests.split(","):
            cmd += ["--tests", t.strip()]
    if args.pre_destructive:
        cmd += ["--pre-destructive-changes", args.pre_destructive]
    if args.post_destructive:
        cmd += ["--post-destructive-changes", args.post_destructive]
    if args.ignore_conflicts:
        cmd.append("--ignore-conflicts")

    banner = "REAL DEPLOY (writes to org)" if args.start else "CHECK-ONLY VALIDATION (no org writes)"
    print("=" * 72)
    print(f"  {banner}")
    print(f"  org        : {org}")
    print(f"  manifest   : {pkg}")
    print(f"  test level : {args.test_level}")
    print(f"  command    : {' '.join(cmd)}")
    print("=" * 72)

    if args.dry_run:
        print("(dry-run) not executing.")
        return 0

    logpath = Path(".build/last_deploy.log")
    rc = _run_tee(cmd, logpath)

    mode_label = "start" if args.start else "validate (dry-run)"
    if rc != 0:
        _write_failure_context(logpath, cmd, org, mode_label, rc)
        _auto_log_failure()
        print("\n" + "─" * 72)
        print(f"❌ Deploy {mode_label} FAILED (exit {rc}).")
        print(f"   Full log : {logpath}")
        print(f"   Context  : .build/last_deploy_failure.json")
        print("   → A DRAFT lesson was auto-written to .cursor/deployment_knowledge.md.")
        print("     Run the SELF-CORRECTION loop: diagnose the root cause, fix the")
        print("     generator/validation, COMPLETE the draft, and flip its Status")
        print("     (see rule sf-deploy-self-correction).")
    return rc


def _auto_log_failure() -> None:
    """Auto-append a DRAFT self-correction lesson so the KB update is never
    forgotten. Never raises into the caller (logging must not mask the failure)."""
    try:
        script = Path(__file__).with_name("log_failure.py")
        subprocess.run([sys.executable, str(script),
                        "--context", ".build/last_deploy_failure.json"],
                       timeout=30)
    except Exception as e:
        print(f"⚠️  auto-log of failure lesson skipped: {e}")


def _run_tee(cmd: list[str], logpath: Path) -> int:
    """Run a command, streaming output live to console AND a log file."""
    logpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        with logpath.open("w", encoding="utf-8") as lf:
            lf.write(f"# command: {' '.join(cmd)}\n")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                lf.write(line)
            proc.wait()
            return proc.returncode
    except KeyboardInterrupt:
        print("\n⏹️  aborted.")
        return 130


def _write_failure_context(logpath: Path, cmd: list[str], org: str,
                           mode: str, rc: int) -> None:
    """Persist a small structured record + extracted component failures for RCA."""
    import datetime
    import re

    text = logpath.read_text(encoding="utf-8") if logpath.exists() else ""
    # Pull component-failure lines from the sf human/table output (best-effort).
    failures = []
    for line in text.splitlines():
        low = line.lower()
        if any(k in low for k in ("error", "fail", "invalid", "missing", "not found",
                                  "insufficient", "duplicate", "cannot")):
            failures.append(line.strip())
    err_codes = sorted(set(re.findall(r"\b[A-Z][A-Z_]{4,}\b", text)))
    ctx = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "target_org": org,
        "returncode": rc,
        "command": cmd,
        "log_file": str(logpath),
        "extracted_failures": failures[:50],
        "possible_error_codes": err_codes[:30],
    }
    Path(".build/last_deploy_failure.json").write_text(
        json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
