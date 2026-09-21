"""MCP adapter tests — no live Google/DeepL session required."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mcp_client import McpAuthError, McpClient, is_auth_failure
from mcp_deepl import DeepLUnavailable, pick_translate_tool
from mcp_sheets import parse_grid, SheetClient


def test_parse_grid_json_and_nested():
    assert parse_grid({"values": [["a", "b"], ["c"]]}) == [["a", "b"], ["c"]]
    assert parse_grid('{"values":[["x"]]}') == [["x"]]
    assert parse_grid([["1", "2"]]) == [["1", "2"]]


def test_auth_failure_detection():
    assert is_auth_failure("401 unauthenticated")
    assert is_auth_failure("Please run mcp-adaptor auth")
    assert not is_auth_failure("translated text Status")


def test_unwrap_tool_error_raises_auth():
    c = McpClient(command="/bin/true")
    try:
        c._unwrap_tool_result(
            {"isError": True, "content": [{"type": "text", "text": "401 unauthorized"}]},
            "read_sheet_values",
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


def test_sheet_client_retries_and_write_modes():
    class Fake:
        def __init__(self):
            self.calls = []
        def call_tool(self, name, args):
            self.calls.append((name, args))
            if name == "read_sheet_values":
                return {"values": [["ok"]]}
            return {"updated": 1}

    fake = Fake()
    sc = SheetClient(client=fake, retries=1)
    sc.write_formulas("sid", [{"range": "T!D2", "values": [["=A1"]]}])
    sc.write_literals("sid", [{"range": "T!D3", "values": [["Status"]]}])
    assert fake.calls[0][1]["value_input_option"] == "USER_ENTERED"
    assert fake.calls[1][1]["value_input_option"] == "RAW"
    grid = sc.read_grid("sid", "T")
    assert grid[0][0] == "ok"


def test_no_gcp_imported_by_translation_stack():
    """Translation path must not import gcloud / service-account / Cloud Translate."""
    root = Path(__file__).resolve().parents[1] / "scripts"
    files = [
        "mcp_client.py", "mcp_sheets.py", "mcp_deepl.py",
        "translation_lib.py", "translate_enrich.py",
        "generate_object_translation.py", "translation_plan.py",
    ]
    for name in files:
        text = (root / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
                continue
            low = s.lower()
            if low.startswith("import ") or low.startswith("from "):
                assert "gcloud" not in low
                assert "google.cloud" not in low
                assert "service_account" not in low
                assert "google.auth" not in low
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in s
            assert "openai" not in low
            assert "anthropic" not in low
