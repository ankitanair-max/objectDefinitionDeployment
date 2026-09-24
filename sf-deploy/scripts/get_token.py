#!/usr/bin/env python3
"""
get_token.py — Mint a currently-valid ERPDEV01 (or any alias) session token
WITHOUT letting the sf CLI write to ~/.sfdx (which the sandbox blocks).

It decrypts sf's keychain-encrypted auth file via sf-core's own Crypto (run
with sf's bundled node), validates the access token over REST, refreshes it via
the OAuth refresh-token grant if needed, and writes `.build/orgauth.json`
(shape: {"result": {accessToken, instanceUrl, apiVersion}}) for mdapi_deploy.py
and the direct REST/SOAP helpers.

Usage:
  python scripts/get_token.py --alias ERPDEV01 [--out .build/orgauth.json]
"""
from __future__ import annotations
import argparse, json, os, pwd, subprocess, sys, urllib.parse, urllib.request, urllib.error
from pathlib import Path


def _real_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _homes() -> list[Path]:
    out: list[Path] = []
    for raw in (os.environ.get("SF_HOME"), os.environ.get("HOME"), str(_real_home())):
        if not raw:
            continue
        p = Path(os.path.expanduser(raw))
        if p not in out:
            out.append(p)
    return out


def _sfdx_dir() -> Path:
    for h in _homes():
        d = h / ".sfdx"
        if d.is_dir():
            return d
    return Path.home() / ".sfdx"


def _sf_client() -> Path:
    for h in _homes():
        p = h / ".local" / "share" / "sf" / "client" / "current"
        if (p / "bin" / "node").is_file():
            return p
    return Path.home() / ".local" / "share" / "sf" / "client" / "current"


SF_CLIENT = str(_sf_client())
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
    # aliases live in ~/.sfdx/alias.json. HOME may be a .sfhome shim; also
    # try the real user home so translation snapshot/verify still mint a token.
    for h in _homes():
        ap = h / ".sfdx" / "alias.json"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", required=True)
    ap.add_argument("--out", default=".build/orgauth.json")
    args = ap.parse_args()

    user = _alias_to_username(args.alias)
    authfile = _sfdx_dir() / f"{user}.json"
    if not authfile.exists():
        for h in _homes():
            cand = h / ".sfdx" / f"{user}.json"
            if cand.exists():
                authfile = cand
                break
    if not authfile.exists():
        print(f"❌ auth file not found: {authfile} (run: sf org login web -a {args.alias} -r https://test.salesforce.com)")
        return 1

    auth_home = str(authfile.parent.parent)
    env = {**os.environ, "CORE": CORE, "AUTHFILE": str(authfile),
           "NODE_PATH": f"{SF_CLIENT}/node_modules", "HOME": auth_home}
    env.pop("SF_HOME", None)
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

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"result": {"accessToken": tok, "instanceUrl": inst, "apiVersion": ver}}, open(args.out, "w"))
    print(f"✅ wrote {args.out} (instance {inst}, api {ver})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
