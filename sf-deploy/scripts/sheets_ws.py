"""Google Workspace MCP sheets client — replaces ADC / google.auth.default.

Duck-types the googleapiclient snippets this repo calls (values.get,
values.batchUpdate, spreadsheets.get, spreadsheets.batchUpdate). Auth is the
same mcp-adaptor Google Workspace login Claude/Cursor already uses, so Sheets
calls do not hit the ADC quota-project 403.
"""
from __future__ import annotations

import ast
import atexit
import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading

_SHEET_LINE = re.compile(r'^\s*- "(.*)" \(ID: (\d+)\)')
_ROW_LINE = re.compile(r"^Row\s+(\d+):\s*(.*)$")
_TITLE_LINE = re.compile(r'^Spreadsheet: "(.*)" \(ID:')
_WS_LOCK = threading.Lock()
_WS_SERVICE = None


def _mcp_adaptor_bin() -> str:
    env = (os.environ.get("MCP_ADAPTOR") or "").strip()
    candidates = [
        env,
        os.path.expanduser("~/.mcp-adaptor/bin/mcp-adaptor"),
        shutil.which("mcp-adaptor") or "",
    ]
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise SystemExit(
        "❌ mcp-adaptor not found. Sheet access uses Google Workspace MCP "
        "(the same login as Claude/Cursor), not gcloud ADC.\n"
        "   Install/auth mcp-adaptor, or set MCP_ADAPTOR to the binary."
    )


def _parse_info_sheets(text: str) -> dict:
    title = ""
    m = _TITLE_LINE.search(text)
    if m:
        title = m.group(1)
    sheets = []
    for line in text.splitlines():
        sm = _SHEET_LINE.match(line)
        if sm:
            sheets.append({
                "properties": {
                    "title": sm.group(1),
                    "sheetId": int(sm.group(2)),
                }
            })
    return {"properties": {"title": title}, "sheets": sheets}


def _parse_read_values(text: str) -> list[list[str]]:
    found: dict[int, list[str]] = {}
    for line in text.splitlines():
        m = _ROW_LINE.match(line.strip())
        if not m:
            continue
        payload = m.group(2).strip()
        try:
            cells = ast.literal_eval(payload) if payload.startswith("[") else [payload]
        except (SyntaxError, ValueError):
            cells = [payload]
        found[int(m.group(1))] = ["" if c is None else str(c) for c in cells]
    if not found:
        return []
    return [found.get(i, []) for i in range(1, max(found) + 1)]


def _parse_csv_values(csv_text: str) -> list[list[str]]:
    return [[str(c) if c is not None else "" for c in row]
            for row in csv.reader(io.StringIO(csv_text))]


def _rgb_to_hex(color: dict) -> str:
    def ch(key):
        return int(round(float(color.get(key, 0) or 0) * 255))
    return f"#{ch('red'):02X}{ch('green'):02X}{ch('blue'):02X}"


def _extract_json(raw: str) -> dict | None:
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return None


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _WorkspaceSheets:
    """stdio JSON-RPC client for `mcp-adaptor --server google_workspace`."""

    def __init__(self):
        self._proc = None
        self._n = 0
        self._gids: dict[tuple[str, int], str] = {}
        self._start()

    def _start(self):
        bin_path = _mcp_adaptor_bin()
        self._proc = subprocess.Popen(
            [bin_path, "--server", "google_workspace"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        atexit.register(self.close)
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sf-deploy", "version": "1"},
        })
        self._notify("notifications/initialized")

    def close(self):
        p = self._proc
        self._proc = None
        if p and p.poll() is None:
            p.kill()

    def _notify(self, method: str, params: dict | None = None):
        self._proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params or {},
        }) + "\n")
        self._proc.stdin.flush()

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._n += 1
        req_id = self._n
        self._proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params or {},
        }) + "\n")
        self._proc.stdin.flush()
        while True:
            line = self._proc.stdout.readline()
            if not line:
                err = ""
                if self._proc.stderr:
                    err = self._proc.stderr.read()[:800]
                raise SystemExit(f"❌ Google Workspace MCP closed unexpectedly.\n{err}")
            msg = json.loads(line)
            if msg.get("id") != req_id:
                continue
            if msg.get("error"):
                raise SystemExit(f"❌ Google Workspace MCP error: {msg['error']}")
            return msg.get("result") or {}

    def _call(self, name: str, arguments: dict) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise SystemExit(f"❌ {name} failed: {result}")
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        return "\n".join(texts)

    def spreadsheets(self):
        return _Spreadsheets(self)

    def values_get(self, sid: str, rng: str, _render: str = "FORMATTED_VALUE") -> dict:
        rng = str(rng or "").strip()
        if "!" not in rng:
            tab = rng.strip("'")
            values: list[list[str]] = []
            start = 1
            while True:
                raw = self._call("export_google_sheet_csv_page", {
                    "spreadsheet_id": sid, "tab": tab,
                    "start_row": start, "max_rows": 5000,
                })
                page = _extract_json(raw) or {"csv": raw, "done": True}
                values.extend(_parse_csv_values(page.get("csv") or ""))
                if page.get("done", True):
                    break
                start = int(page.get("next_start_row") or (start + 5000))
            return {"values": values}
        text = self._call("read_sheet_values", {
            "spreadsheet_id": sid, "range_name": rng,
        })
        return {"values": _parse_read_values(text)}

    def values_batch_update(self, sid: str, body: dict) -> dict:
        option = body.get("valueInputOption") or "RAW"
        updated = 0
        for item in body.get("data") or []:
            rng = item.get("range") or ""
            vals = item.get("values") or []
            self._call("modify_sheet_values", {
                "spreadsheet_id": sid,
                "range_name": rng,
                "values": vals,
                "value_input_option": option,
            })
            updated += sum(len(r) for r in vals)
        return {"totalUpdatedCells": updated}

    def spreadsheet_get(self, sid: str) -> dict:
        info = _parse_info_sheets(self._call("get_spreadsheet_info", {
            "spreadsheet_id": sid,
        }))
        for sh in info.get("sheets") or []:
            props = sh.get("properties") or {}
            gid = props.get("sheetId")
            title = props.get("title")
            if gid is not None and title:
                self._gids[(sid, int(gid))] = title
        return info

    def spreadsheet_batch_update(self, sid: str, body: dict) -> dict:
        for req in body.get("requests") or []:
            cell = (req.get("repeatCell") or {})
            rng = cell.get("range") or {}
            fill = ((cell.get("cell") or {}).get("userEnteredFormat") or {}).get(
                "backgroundColor")
            if not fill:
                raise SystemExit(f"❌ unsupported Sheets batchUpdate request: {req}")
            gid = int(rng.get("sheetId"))
            tab = self._gids.get((sid, gid))
            if not tab:
                self.spreadsheet_get(sid)
                tab = self._gids.get((sid, gid))
            if not tab:
                raise SystemExit(f"❌ unknown sheetId {gid} for {sid}")
            hex_color = _rgb_to_hex(fill)
            start_row = int(rng.get("startRowIndex", 0))
            end_row = int(rng.get("endRowIndex", start_row + 1))
            start_col = int(rng.get("startColumnIndex", 0))
            col = chr(ord("A") + start_col)
            for r0 in range(start_row, end_row):
                self._call("format_sheet_range", {
                    "spreadsheet_id": sid,
                    "range_name": f"'{tab}'!{col}{r0 + 1}",
                    "background_color": hex_color,
                })
        return {}


class _Spreadsheets:
    def __init__(self, ws: _WorkspaceSheets):
        self._ws = ws

    def values(self):
        return _Values(self._ws)

    def get(self, spreadsheetId: str, **_kwargs):
        return _Exec(lambda: self._ws.spreadsheet_get(spreadsheetId))

    def batchUpdate(self, spreadsheetId: str, body: dict, **_kwargs):
        return _Exec(lambda: self._ws.spreadsheet_batch_update(spreadsheetId, body))


class _Values:
    def __init__(self, ws: _WorkspaceSheets):
        self._ws = ws

    def get(self, spreadsheetId: str, range: str,
            valueRenderOption: str = "FORMATTED_VALUE", **_kwargs):
        return _Exec(lambda: self._ws.values_get(spreadsheetId, range, valueRenderOption))

    def batchUpdate(self, spreadsheetId: str, body: dict, **_kwargs):
        return _Exec(lambda: self._ws.values_batch_update(spreadsheetId, body))


def get_sheets_service():
    """Sheets client via Google Workspace MCP (not ADC)."""
    global _WS_SERVICE
    with _WS_LOCK:
        if _WS_SERVICE is None:
            _WS_SERVICE = _WorkspaceSheets()
        return _WS_SERVICE
