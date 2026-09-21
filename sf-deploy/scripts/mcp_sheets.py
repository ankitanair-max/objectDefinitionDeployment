#!/usr/bin/env python3
"""
mcp_sheets.py — Google Sheets I/O through the Salesforce Google Workspace MCP.

Provider: google-workspace-rw
Transport: mcp-adaptor
Auth: the user's Salesforce Google SSO (mcp-adaptor auth)

Reads return calculated/formatted values. Writes accept RAW (literals) or
USER_ENTERED (formulas, so GOOGLETRANSLATE can evaluate).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from mcp_client import (
    McpAuthError,
    McpClient,
    McpError,
    McpUnavailable,
    google_workspace_client,
)


DEFAULT_RANGE = "A1:CZ500"
WRITE_TOOL = "modify_sheet_values"
READ_TOOL = "read_sheet_values"


def col_letter(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def a1(col0: int, row1: int) -> str:
    return f"{col_letter(col0)}{row1}"


class SheetClient:
    """Bounded-retry sheet reader/writer over an authenticated MCP session."""

    def __init__(self, client: McpClient | None = None, retries: int = 3):
        self._client = client
        self._owns = client is None
        self.retries = retries

    def __enter__(self) -> "SheetClient":
        if self._client is None:
            self._client = google_workspace_client(retries=self.retries)
            self._client.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._owns and self._client is not None:
            self._client.close()

    @property
    def client(self) -> McpClient:
        if self._client is None:
            raise McpUnavailable("SheetClient is not started")
        return self._client

    def read_grid(
        self,
        spreadsheet_id: str,
        tab: str,
        range_a1: str = DEFAULT_RANGE,
    ) -> list[list[str]]:
        """Read calculated/formatted values for one tab (never formulas)."""
        rng = _tab_range(tab, range_a1)
        raw = self._call(READ_TOOL, {
            "spreadsheet_id": spreadsheet_id,
            "range_name": rng,
        })
        return parse_grid(raw)

    def write_cells(
        self,
        spreadsheet_id: str,
        updates: list[dict],
        *,
        value_input_option: str = "RAW",
    ) -> None:
        """Write a batch of {range, values} cells.

        value_input_option:
          RAW          — store literals (DeepL English, provenance)
          USER_ENTERED — evaluate formulas (GOOGLETRANSLATE)
        """
        if not updates:
            return
        # One MCP call per range: the Workspace tool takes a single range.
        # Grouping is the caller's job; we still retry each write.
        for item in updates:
            self._call(WRITE_TOOL, {
                "spreadsheet_id": spreadsheet_id,
                "range_name": item["range"],
                "values": item["values"],
                "value_input_option": value_input_option,
            })

    def write_formulas(self, spreadsheet_id: str, updates: list[dict]) -> None:
        self.write_cells(spreadsheet_id, updates, value_input_option="USER_ENTERED")

    def write_literals(self, spreadsheet_id: str, updates: list[dict]) -> None:
        self.write_cells(spreadsheet_id, updates, value_input_option="RAW")

    def read_cells(
        self,
        spreadsheet_id: str,
        tab: str,
        a1_cells: list[str],
    ) -> dict[str, str]:
        """Read calculated values for specific A1 cells on one tab."""
        if not a1_cells:
            return {}
        # One range covering all requested cells is cheaper than N round-trips.
        grid = self.read_grid(spreadsheet_id, tab)
        out: dict[str, str] = {}
        for cell in a1_cells:
            col, row = parse_a1(cell)
            out[cell] = cell_at(grid, row - 1, col)
        return out

    def wait_recalc(
        self,
        spreadsheet_id: str,
        tab: str,
        a1_cells: list[str],
        *,
        timeout: float = 45.0,
        interval: float = 1.5,
        is_pending: Callable[[str], bool] | None = None,
    ) -> dict[str, str]:
        """Poll calculated values until formulas settle or timeout."""
        pending = is_pending or (lambda v: v.startswith("=") or v == "Loading...")
        deadline = time.time() + timeout
        last: dict[str, str] = {}
        while True:
            last = self.read_cells(spreadsheet_id, tab, a1_cells)
            if all(not pending(v) for v in last.values()):
                return last
            if time.time() >= deadline:
                stuck = {k: v for k, v in last.items() if pending(v)}
                raise McpError(
                    "Spreadsheet recalculation timed out; calculated English "
                    f"could not be read for: {stuck}"
                )
            time.sleep(interval)

    def _call(self, tool: str, args: dict) -> Any:
        last: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                return self.client.call_tool(tool, args)
            except McpAuthError:
                raise
            except (McpError, McpUnavailable) as e:
                last = e
                if attempt + 1 >= self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise last or McpError(f"{tool} failed")


def _tab_range(tab: str, range_a1: str) -> str:
    if "!" in range_a1:
        return range_a1
    # Single-quote tab names so commas/spaces are legal A1 sheet names.
    safe = tab.replace("'", "''")
    return f"'{safe}'!{range_a1}"


def parse_a1(cell: str) -> tuple[int, int]:
    m = re.match(r"^\$?([A-Za-z]+)\$?(\d+)$", cell.strip())
    if not m:
        raise ValueError(f"not an A1 cell: {cell!r}")
    letters, row = m.group(1).upper(), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return col - 1, row


def cell_at(grid: list[list[str]], row0: int, col0: int) -> str:
    if row0 < 0 or row0 >= len(grid):
        return ""
    row = grid[row0]
    if col0 < 0 or col0 >= len(row):
        return ""
    return str(row[col0] or "").strip()


def parse_grid(raw: Any) -> list[list[str]]:
    """Best-effort parse of MCP read_sheet_values output into a 2D string grid."""
    if raw is None:
        return []
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            return [[_cell(c) for c in row] for row in raw]
        if raw and isinstance(raw[0], dict) and "values" in raw[0]:
            return parse_grid(raw[0]["values"])
        return [[_cell(c) for c in raw]]
    if isinstance(raw, dict):
        for key in ("values", "data", "rows", "formatted"):
            if key in raw:
                return parse_grid(raw[key])
        # Sheets API-ish: sheets[0].data[0].rowData
        sheets = raw.get("sheets")
        if isinstance(sheets, list) and sheets:
            return parse_grid(sheets[0])
        if "rowData" in raw:
            rows = []
            for rd in raw.get("rowData") or []:
                vals = []
                for c in rd.get("values") or []:
                    vals.append(_cell(c.get("formattedValue") or c.get("effectiveValue") or ""))
                rows.append(vals)
            return rows
        if "range" in raw and "values" not in raw:
            return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s[0] in "{[":
            try:
                return parse_grid(json.loads(s))
            except json.JSONDecodeError:
                pass
        return _parse_text_table(s)
    return []


def _parse_text_table(text: str) -> list[list[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("|---")]
    rows: list[list[str]] = []
    for ln in lines:
        if ln.strip().startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows.append(cells)
        elif "\t" in ln:
            rows.append([c.strip() for c in ln.split("\t")])
        else:
            rows.append([ln.rstrip()])
    return rows


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("formattedValue", "effectiveValue", "userEnteredValue", "value"):
            if k in v:
                return _cell(v[k])
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip() if not isinstance(v, str) else v
