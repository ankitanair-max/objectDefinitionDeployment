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


# DeepL REST-style codes (some MCP servers). Official deepl-mcp-server wants ISO.
SOURCE_LANG = "JA"
TARGET_LANG = "EN-US"
ISO_SOURCE = "ja"
ISO_TARGET = "en-US"

_TEXT_KEYS = ("text", "texts", "input", "content", "q")
_SRC_KEYS = (
    "sourceLangCode", "source_lang_code", "sourceLang", "source_lang",
    "from", "source",
)
_TGT_KEYS = (
    "targetLangCode", "target_lang_code", "targetLang", "target_lang",
    "to", "target",
)


class DeepLUnavailable(McpUnavailable):
    """DeepL MCP is not configured or failed preflight."""


class DeepLTranslateError(McpError):
    """DeepL accepted preflight then failed to translate the batch."""


def _tool_name(t: dict) -> str:
    return (t.get("name") or t.get("toolName") or "").strip()


def pick_translate_tool(tools: list[dict]) -> dict:
    """Return the translate tool descriptor (name + inputSchema)."""
    preferred = (
        "translate_text", "translate-text", "deepl_translate",
        "translate", "translations",
    )
    by_lower = {_tool_name(t).lower(): t for t in tools if _tool_name(t)}
    for p in preferred:
        if p in by_lower:
            return by_lower[p]
    for t in tools:
        if "translat" in _tool_name(t).lower():
            return t
    names = [_tool_name(t) for t in tools if _tool_name(t)]
    raise DeepLUnavailable(
        "DeepL MCP is reachable but exposes no translate tool "
        f"(tools: {names or 'none'})"
    )


def _input_schema(tool: dict | None) -> dict:
    if not tool:
        return {}
    return tool.get("inputSchema") or tool.get("parameters") or {}


def _pick_enum(enum: list, *candidates: str) -> str:
    low = {str(x).lower(): str(x) for x in enum}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    raise DeepLUnavailable(
        f"DeepL MCP language enum does not support {candidates[0]!r}"
    )


def _lang_value(key: str, spec: dict, iso: str, deepl: str) -> str:
    enum = spec.get("enum") if isinstance(spec, dict) else None
    if enum:
        return _pick_enum(list(enum), iso, deepl)
    if "code" in key.lower():
        return iso
    return deepl


def translate_args(text: str, tool: dict | None = None) -> dict:
    """Build tool arguments from tools/list inputSchema only.

    Official deepl-mcp-server (translate-text) wants sourceLangCode /
    targetLangCode with ISO-639 values (ja / en-US). Extra shotgun keys
    are rejected when additionalProperties is false.
    """
    schema = _input_schema(tool)
    props = schema.get("properties") or {}
    if not props:
        return {
            "text": text,
            "sourceLangCode": ISO_SOURCE,
            "targetLangCode": ISO_TARGET,
        }
    args: dict[str, Any] = {}
    text_key = ""
    for k in _TEXT_KEYS:
        if k not in props:
            continue
        spec = props[k] if isinstance(props[k], dict) else {}
        args[k] = [text] if spec.get("type") == "array" or k == "texts" else text
        text_key = k
        break
    if not text_key:
        raise DeepLUnavailable(
            "DeepL MCP translate tool has no supported text property in inputSchema"
        )
    for keys, iso, deepl in (
        (_SRC_KEYS, ISO_SOURCE, SOURCE_LANG),
        (_TGT_KEYS, ISO_TARGET, TARGET_LANG),
    ):
        for k in keys:
            if k in props:
                spec = props[k] if isinstance(props[k], dict) else {}
                args[k] = _lang_value(k, spec, iso, deepl)
                break
    missing = [str(k) for k in schema.get("required") or [] if k not in args]
    if missing:
        raise DeepLUnavailable(
            "DeepL MCP translate schema has unsupported required properties: "
            + ", ".join(missing)
        )
    return args


class DeepLProvider:
    """Batch translator. preflight() must succeed before translate_batch()."""

    name = "deepl"

    def __init__(self, client: McpClient | None = None):
        self._client = client
        self._owns = client is None
        self._tool = ""
        self._tool_meta: dict = {}
        self._healthy = False

    def close(self) -> None:
        if self._owns and self._client is not None:
            self._client.close()
            self._client = None

    def preflight(self) -> None:
        """Raise DeepLUnavailable unless the MCP server is configured + healthy."""
        if self._client is None:
            try:
                self._client = deepl_client()
            except (McpUnavailable, McpAuthError, McpError) as e:
                raise DeepLUnavailable(
                    f"DeepL MCP configuration failed: {e}"
                ) from e
            if self._client is None:
                raise DeepLUnavailable(
                    "No usable DeepL key or MCP transport was resolved. Configure "
                    "DEEPL_API_KEY, DEEPL_API_KEY_FILE, macOS Keychain service "
                    "'sf-deploy/deepl', or DEEPL_API_KEY_COMMAND. Google Translate "
                    "will be used for this batch if selected at preflight."
                )
            try:
                self._client.start()
            except (McpUnavailable, McpAuthError, McpError) as e:
                raise DeepLUnavailable(f"DeepL MCP failed to start: {e}") from e
        try:
            tools = self._client.list_tools()
        except (McpUnavailable, McpAuthError, McpError) as e:
            raise DeepLUnavailable(f"DeepL MCP tools/list failed: {e}") from e
        self._tool_meta = pick_translate_tool(tools)
        self._tool = _tool_name(self._tool_meta)
        self._healthy = True
        framing = getattr(self._client, "framing", "") or ""
        keys = ",".join(translate_args("x", self._tool_meta).keys())
        print(f"      DeepL MCP: tool={self._tool} args=[{keys}]"
              f"{f' framing={framing}' if framing else ''}")

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
        raw = self._client.call_tool(
            self._tool, translate_args(text, self._tool_meta))
        return _extract_text(raw)


def _extract_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        lines = raw.strip().splitlines()
        metadata_prefixes = (
            "Detected source language:",
            "Target language used:",
        )
        while lines and lines[-1].strip().startswith(metadata_prefixes):
            lines.pop()
        return "\n".join(lines).strip()
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
                return _extract_text("\n".join(chunks))
    return str(raw).strip()
