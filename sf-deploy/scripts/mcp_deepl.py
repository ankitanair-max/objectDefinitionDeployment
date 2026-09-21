#!/usr/bin/env python3
"""
mcp_deepl.py — DeepL translation via an authenticated DeepL MCP server.

JA → EN-US. No LLM, no browser, no unofficial scraper.
When the server is missing/unhealthy this module reports that fact so the
pipeline can select Google Translate for the *entire* batch (preflight only).
A failure AFTER a successful preflight is fatal — no Google fallback.
"""
from __future__ import annotations

from typing import Any

from mcp_client import (
    McpAuthError,
    McpClient,
    McpError,
    McpUnavailable,
    deepl_client,
)


SOURCE_LANG = "JA"
TARGET_LANG = "EN-US"


class DeepLUnavailable(McpUnavailable):
    """DeepL MCP is not configured or failed preflight."""


class DeepLTranslateError(McpError):
    """DeepL accepted preflight then failed to translate the batch."""


def _tool_names(tools: list[dict]) -> list[str]:
    out = []
    for t in tools:
        name = (t.get("name") or t.get("toolName") or "").strip()
        if name:
            out.append(name)
    return out


def pick_translate_tool(tools: list[dict]) -> str:
    names = _tool_names(tools)
    preferred = (
        "translate_text", "translate-text", "deepl_translate",
        "translate", "translations",
    )
    lower = {n.lower(): n for n in names}
    for p in preferred:
        if p in lower:
            return lower[p]
    for n in names:
        if "translat" in n.lower():
            return n
    raise DeepLUnavailable(
        "DeepL MCP is reachable but exposes no translate tool "
        f"(tools: {names or 'none'})"
    )


class DeepLProvider:
    """Batch translator. preflight() must succeed before translate_batch()."""

    name = "deepl"

    def __init__(self, client: McpClient | None = None):
        self._client = client
        self._owns = client is None
        self._tool = ""
        self._healthy = False

    def close(self) -> None:
        if self._owns and self._client is not None:
            self._client.close()
            self._client = None

    def preflight(self) -> None:
        """Raise DeepLUnavailable unless the MCP server is configured + healthy."""
        if self._client is None:
            self._client = deepl_client()
            if self._client is None:
                raise DeepLUnavailable(
                    "DeepL MCP is not configured (set MCP_DEEPL_COMMAND / "
                    "MCP_DEEPL_SERVER / MCP_DEEPL_URL). Google Translate will "
                    "be used for this batch if selected at preflight."
                )
            try:
                self._client.start()
            except (McpUnavailable, McpAuthError, McpError) as e:
                raise DeepLUnavailable(f"DeepL MCP failed to start: {e}") from e
        try:
            tools = self._client.list_tools()
        except (McpUnavailable, McpAuthError, McpError) as e:
            raise DeepLUnavailable(f"DeepL MCP tools/list failed: {e}") from e
        self._tool = pick_translate_tool(tools)
        self._healthy = True

    def translate_batch(self, texts: list[str]) -> list[str]:
        """Translate every item JA→EN-US. Any blank/malformed result is fatal."""
        if not self._healthy or not self._client or not self._tool:
            raise DeepLTranslateError(
                "DeepL translate_batch called before a successful preflight"
            )
        if not texts:
            return []
        out: list[str] = []
        try:
            for src in texts:
                en = self._translate_one(src)
                if not (en or "").strip():
                    raise DeepLTranslateError(
                        f"DeepL returned a blank translation for: {src!r}"
                    )
                out.append(en.strip())
        except DeepLTranslateError:
            raise
        except (McpError, McpAuthError, McpUnavailable) as e:
            raise DeepLTranslateError(
                f"DeepL failed mid-batch after successful preflight: {e}"
            ) from e
        if len(out) != len(texts):
            raise DeepLTranslateError(
                f"DeepL returned {len(out)} results for {len(texts)} sources"
            )
        return out

    def _translate_one(self, text: str) -> str:
        assert self._client is not None
        raw = self._client.call_tool(self._tool, _translate_args(text))
        return _extract_text(raw)


def _translate_args(text: str) -> dict:
    # Cover the common DeepL MCP argument names without coupling to one server.
    return {
        "text": text,
        "texts": [text],
        "source_lang": SOURCE_LANG,
        "target_lang": TARGET_LANG,
        "sourceLang": SOURCE_LANG,
        "targetLang": TARGET_LANG,
        "from": SOURCE_LANG,
        "to": TARGET_LANG,
    }


def _extract_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        if not raw:
            return ""
        return _extract_text(raw[0])
    if isinstance(raw, dict):
        for key in (
            "text", "translation", "translated_text", "translatedText",
            "result", "output",
        ):
            if key in raw and raw[key] not in (None, ""):
                return _extract_text(raw[key])
        translations = raw.get("translations")
        if isinstance(translations, list) and translations:
            return _extract_text(translations[0])
        content = raw.get("content")
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(item.get("text") or "")
            if chunks:
                return "\n".join(chunks).strip()
    return str(raw).strip()
