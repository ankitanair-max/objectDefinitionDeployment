#!/usr/bin/env python3
"""
get_token.py — Write `.build/orgauth.json` (shape:
{"result": {accessToken, instanceUrl, apiVersion}}) for mdapi_deploy.py and the
direct REST/SOAP helpers.

Preferred path: ask the Salesforce CLI itself (`sf org display --verbose
--json`), which works on any authorized machine — laptop, build agent or
container — and needs no knowledge of where the CLI keeps its auth.

Fallback (only when the CLI is unavailable, e.g. a sandbox that blocks the CLI
from writing to ~/.sfdx): decrypt sf's keychain-encrypted auth file via
sf-core's own Crypto, validate the token over REST and refresh it via the OAuth
refresh-token grant. That path assumes this machine's layout, so it is a last
resort, never the default.

Usage:
  python scripts/get_token.py --alias <ORG> [--out .build/orgauth.json]
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, urllib.parse, urllib.request, urllib.error
from pathlib import Path

HOME = os.path.expanduser("~")
SF_CLIENT = f"{HOME}/.local/share/sf/client/current"
NODE = f"{SF_CLIENT}/bin/node"
CORE = f"{SF_CLIENT}/node_modules/@salesforce/core"

DECRYPT_JS = r"""
const fs = require('fs');
const path = require('path');
const { Crypto } = require(path.join(process.env.CORE, 'lib/crypto/crypto.js'));
(async () => {
  const auth = JSON.parse(fs.readFileSync(process.env.AUTHFILE, 'utf8'));
  const c = await Crypto.create();
  const dec = (v) => { if (!v) return ''; try { return c.decrypt(v); } catch (e) { return ''; } };
  process.stdout.write(JSON.stringify({
    refreshToken: dec(auth.refreshToken), accessToken: dec(auth.accessToken),
    clientId: auth.clientId, clientSecret: auth.clientSecret ? dec(auth.clientSecret) : '',
    instanceUrl: auth.instanceUrl, loginUrl: auth.loginUrl, apiVersion: auth.instanceApiVersion,
  }));
})().catch((e) => { console.error('ERR ' + e.message); process.exit(1); });
"""


def _alias_to_username(alias: str) -> str:
    # aliases live in ~/.sfdx/alias.json ({"orgs": {alias: username}})
    ap = Path(HOME) / ".sfdx" / "alias.json"
    if ap.exists():
        m = json.loads(ap.read_text()).get("orgs", {})
        if alias in m:
            return m[alias]
    return alias  # assume it's already a username


def _rest_ok(inst: str, ver: str, tok: str) -> bool:
    url = f"{inst}/services/data/v{ver}/limits"
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"}), timeout=20)
        return True
    except urllib.error.HTTPError:
        return False


def _from_cli(alias: str) -> dict | None:
    """Session straight from the supported CLI — no filesystem assumptions."""
    try:
        cp = subprocess.run(
            ["sf", "org", "display", "--target-org", alias, "--verbose", "--json"],
            capture_output=True, text=True)
    except FileNotFoundError:
        return None
    try:
        res = (json.loads(cp.stdout or "{}").get("result") or {})
    except json.JSONDecodeError:
        return None
    tok, inst = res.get("accessToken") or "", (res.get("instanceUrl") or "").rstrip("/")
    if cp.returncode != 0 or not tok or not inst:
        return None
    return {"accessToken": tok, "instanceUrl": inst,
            "apiVersion": str(res.get("apiVersion") or "60.0")}


def _write(out: str, result: dict) -> None:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"result": result}), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--out", default=".build/orgauth.json")
    ap.add_argument("--no-cli", action="store_true",
                    help="skip `sf org display` and use the local keychain path")
    args = ap.parse_args()

    if not args.no_cli:
        res = _from_cli(args.alias)
        if res:
            _write(args.out, res)
            print(f"✅ wrote {args.out} via sf CLI "
                  f"(instance {res['instanceUrl']}, api {res['apiVersion']})")
            return 0
        print("ℹ️  `sf org display` unavailable — falling back to the local keychain.")

    user = _alias_to_username(args.alias)
    authfile = Path(HOME) / ".sfdx" / f"{user}.json"
    if not authfile.exists():
        print(f"❌ auth file not found: {authfile} (run: sf org login web -a {args.alias} -r https://test.salesforce.com)")
        return 1

    env = {**os.environ, "CORE": CORE, "AUTHFILE": str(authfile),
           "NODE_PATH": f"{SF_CLIENT}/node_modules"}
    p = subprocess.run([NODE, "-e", DECRYPT_JS], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print("❌ decrypt failed:", p.stderr[:300]); return 1
    d = json.loads(p.stdout)
    inst = (d["instanceUrl"] or "").rstrip("/")
    ver = str(d.get("apiVersion") or "60.0")
    tok = d.get("accessToken") or ""

    if not (tok and _rest_ok(inst, ver, tok)):
        login = (d.get("loginUrl") or "https://test.salesforce.com").rstrip("/")
        data = urllib.parse.urlencode({"grant_type": "refresh_token",
                                       "client_id": d["clientId"],
                                       "refresh_token": d["refreshToken"]}).encode()
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                login + "/services/oauth2/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20))
        except urllib.error.HTTPError as e:
            print("❌ token refresh failed:", e.code, e.read().decode()[:200]); return 1
        tok = r["access_token"]; inst = r.get("instance_url", inst).rstrip("/")

    _write(args.out, {"accessToken": tok, "instanceUrl": inst, "apiVersion": ver})
    print(f"✅ wrote {args.out} (instance {inst}, api {ver})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
