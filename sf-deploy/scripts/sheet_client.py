#!/usr/bin/env python3
"""
sheet_client.py — Google Sheets I/O using the pipeline's existing auth.

Same credentials as fetch_sheet.py / write_back.py / write_attr_fixes.py:
Application Default Credentials (already used today). No MCP sheet adapter.

Reads return calculated/formatted values. Writes accept RAW (literals) or
USER_ENTERED (formulas, so GOOGLETRANSLATE can evaluate).
"""
from __future__ import annotations

import time
from typing import Callable

from translation_lib import cell_at, parse_a1
from write_back import get_write_service


DEFAULT_RANGE = "A1:CZ500"


def _tab_range(tab: str, range_a1: str) -> str:
    if "!" in range_a1:
        return range_a1
    safe = tab.replace("'", "''")
    return f"'{safe}'!{range_a1}"


class SheetClient:
    """Read/write a spreadsheet through the existing Sheets API client."""

    def __init__(self, service=None, retries: int = 3):
        self._svc = service
        self._owns = service is None
        self.retries = retries

    def __enter__(self) -> "SheetClient":
        if self._svc is None:
            self._svc = get_write_service()
        return self

    def __exit__(self, *exc) -> None:
        self._svc = None if self._owns else self._svc

    @property
    def svc(self):
        if self._svc is None:
            raise RuntimeError("SheetClient is not started")
        return self._svc

    def read_grid(
        self,
        spreadsheet_id: str,
        tab: str,
        range_a1: str = DEFAULT_RANGE,
    ) -> list[list[str]]:
        rng = _tab_range(tab, range_a1)
        vals = self.svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueRenderOption="FORMATTED_VALUE",
        ).execute().get("values", []) or []
        return [[str(c) if c is not None else "" for c in row] for row in vals]

    def write_cells(
        self,
        spreadsheet_id: str,
        updates: list[dict],
        *,
        value_input_option: str = "RAW",
    ) -> None:
        if not updates:
            return
        data = [{"range": u["range"], "values": u["values"]} for u in updates]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": value_input_option, "data": data},
        ).execute()

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
        if not a1_cells:
            return {}
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
        pending = is_pending or (lambda v: v.startswith("=") or v == "Loading...")
        deadline = time.time() + timeout
        last: dict[str, str] = {}
        while True:
            last = self.read_cells(spreadsheet_id, tab, a1_cells)
            if all(not pending(v) for v in last.values()):
                return last
            if time.time() >= deadline:
                stuck = {k: v for k, v in last.items() if pending(v)}
                raise RuntimeError(
                    "Spreadsheet recalculation timed out; calculated English "
                    f"could not be read for: {stuck}"
                )
            time.sleep(interval)
