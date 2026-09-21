"""Adapter tests — no live Google/DeepL session required."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mcp_client import McpAuthError, McpClient, is_auth_failure
from mcp_deepl import DeepLUnavailable, pick_translate_tool
from sheet_client import SheetClient
from translation_lib import cell_at, parse_a1


class _Execute:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeSheetsService:
    """googleapiclient-shaped stub: spreadsheets().values().get/batchUpdate."""

    def __init__(self):
        self.gets = []
        self.batch_bodies = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range, valueRenderOption="FORMATTED_VALUE"):
        self.gets.append(
            {
                "spreadsheetId": spreadsheetId,
                "range": range,
                "valueRenderOption": valueRenderOption,
            }
        )
        return _Execute({"values": [["ok"]]})

    def batchUpdate(self, spreadsheetId, body):
        self.batch_bodies.append({"spreadsheetId": spreadsheetId, "body": body})
        return _Execute({"updated": 1})


def test_a1_helpers():
    assert parse_a1("D12") == (3, 12)
    assert cell_at([["a", "b"], ["c"]], 1, 0) == "c"
    assert cell_at([["a"]], 5, 0) == ""


def test_auth_failure_detection():
    assert is_auth_failure("401 unauthenticated")
    assert is_auth_failure("Please run mcp-adaptor auth")
    assert not is_auth_failure("translated text Status")


def test_unwrap_tool_error_raises_auth():
    c = McpClient(command="/bin/true")
    try:
        c._unwrap_tool_result(
            {"isError": True, "content": [{"type": "text", "text": "401 unauthorized"}]},
            "translate_text",
        )
        assert False, "expected McpAuthError"
    except McpAuthError as e:
        assert "mcp-adaptor auth" in str(e).lower() or "auth" in str(e).lower()


def test_deepl_picks_translate_tool():
    tools = [{"name": "foo"}, {"name": "translate_text"}]
    assert pick_translate_tool(tools) == "translate_text"
    try:
        pick_translate_tool([{"name": "list_files"}])
        assert False
    except DeepLUnavailable:
        pass


def test_sheet_client_write_modes_and_formatted_read():
    fake = FakeSheetsService()
    sc = SheetClient(service=fake)
    sc.write_formulas("sid", [{"range": "T!D2", "values": [["=A1"]]}])
    sc.write_literals("sid", [{"range": "T!D3", "values": [["Status"]]}])
    assert fake.batch_bodies[0]["body"]["valueInputOption"] == "USER_ENTERED"
    assert fake.batch_bodies[1]["body"]["valueInputOption"] == "RAW"
    grid = sc.read_grid("sid", "T")
    assert grid[0][0] == "ok"
    assert fake.gets[0]["valueRenderOption"] == "FORMATTED_VALUE"


def test_no_cloud_translate_in_translation_stack():
    """Must not add Cloud Translation / LLM providers. Sheets ADC is existing write_back."""
    root = Path(__file__).resolve().parents[1] / "scripts"
    files = [
        "mcp_client.py", "mcp_deepl.py", "sheet_client.py",
        "translation_lib.py", "translate_enrich.py",
        "generate_object_translation.py", "translation_plan.py",
    ]
    forbidden_imports = (
        "gcloud",
        "google.cloud",
        "service_account",
        "openai",
        "anthropic",
    )
    for name in files:
        text = (root / name).read_text(encoding="utf-8")
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in text
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
                continue
            low = s.lower()
            if low.startswith("import ") or low.startswith("from "):
                for token in forbidden_imports:
                    assert token not in low, f"{name}: {s}"


def test_no_mcp_sheet_adapter():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    assert not (scripts / "mcp_sheets.py").exists()
    mcp_client = (scripts / "mcp_client.py").read_text(encoding="utf-8")
    assert "google_workspace_client" not in mcp_client
    assert "MCP_SHEETS" not in mcp_client
