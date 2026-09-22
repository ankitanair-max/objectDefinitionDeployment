#!/usr/bin/env python3
"""
mcp_client.py — JSON-RPC client for an already-authenticated MCP server.

Used for optional DeepL translation over stdio or streamable HTTP.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class McpError(RuntimeError):
    """MCP transport or tool error."""


class McpAuthError(McpError):
    """Session missing/expired — operator must run `mcp-adaptor auth`."""


class McpUnavailable(McpError):
    """Server binary missing, not configured, or failed to start."""


def _default_adaptor_bin() -> str:
    env = os.environ.get("MCP_ADAPTOR_BIN", "").strip()
    if env:
        return env
    home = Path.home() / ".mcp-adaptor" / "bin" / "mcp-adaptor"
    if home.is_file() or home.is_symlink():
        return str(home)
    found = shutil.which("mcp-adaptor")
    return found or ""


def is_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    needles = (
        "unauthenticated", "unauthorized", "401", "403",
        "invalid_grant", "not authenticated", "please login",
        "mcp-adaptor auth", "sso", "expired token", "no session",
    )
    return any(n in low for n in needles)


class McpClient:
    """JSON-RPC 2.0 MCP client (stdio Content-Length or streamable HTTP)."""

    def __init__(
        self,
        *,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str = "",
        timeout: float = 60.0,
        retries: int = 3,
        client_name: str = "sf-deploy-translation",
    ):
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.url = (url or os.environ.get("MCP_URL") or "").rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.client_name = client_name
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._initialized = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.url:
            self._initialize_http()
            return
        if not self.command:
            raise McpUnavailable(
                "MCP server command is empty. Set MCP_ADAPTOR_BIN or install "
                "mcp-adaptor, then authenticate with: mcp-adaptor auth"
            )
        cmd = [self.command, *self.args]
        merged = os.environ.copy()
        if self.env:
            merged.update(self.env)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged,
            )
        except FileNotFoundError as e:
            raise McpUnavailable(
                f"MCP server binary not found: {self.command}. {e}"
            ) from e
        self._initialize_stdio()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
        self._initialized = False

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def list_tools(self) -> list[dict]:
        result = self.request("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                result = self.request("tools/call", {
                    "name": name,
                    "arguments": arguments or {},
                })
                return self._unwrap_tool_result(result, name)
            except McpAuthError:
                raise
            except McpError as e:
                last = e
                if attempt + 1 >= self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise last or McpError(f"tool {name} failed")

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params or {},
        }
        if self.url:
            return self._http_rpc(payload)
        return self._stdio_rpc(payload)

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self.url:
            self._http_rpc(payload, notification=True)
            return
        self._write_stdio(payload)

    # ------------------------------------------------------------------ #
    # stdio
    # ------------------------------------------------------------------ #
    def _initialize_stdio(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "1.0"},
        })
        if not isinstance(result, dict):
            raise McpError(f"MCP initialize returned unexpected payload: {result!r}")
        self.notify("notifications/initialized")
        self._initialized = True

    def _write_stdio(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise McpUnavailable("MCP stdio process is not running")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self._proc.stdin.write(header + body)
            self._proc.stdin.flush()
        except BrokenPipeError as e:
            err = self._drain_stderr()
            raise McpUnavailable(f"MCP server closed stdin. {err}") from e

    def _read_stdio(self) -> dict:
        if not self._proc or not self._proc.stdout:
            raise McpUnavailable("MCP stdio process is not running")
        # LSP-style Content-Length framing, with NDJSON fallback.
        header_lines: list[bytes] = []
        while True:
            line = self._proc.stdout.readline()
            if not line:
                err = self._drain_stderr()
                raise McpUnavailable(
                    f"MCP server closed stdout before a response. {err}"
                )
            if line in (b"\r\n", b"\n"):
                break
            header_lines.append(line)
        headers = b"".join(header_lines).decode("ascii", errors="replace")
        length = 0
        for h in headers.splitlines():
            if h.lower().startswith("content-length:"):
                length = int(h.split(":", 1)[1].strip())
        if length:
            body = self._proc.stdout.read(length)
            return json.loads(body.decode("utf-8"))
        # Fallback: the first "header" line was actually a JSON object.
        raw = b"".join(header_lines).strip()
        if raw.startswith(b"{"):
            return json.loads(raw.decode("utf-8"))
        raise McpError(f"unframed MCP response: {raw[:200]!r}")

    def _stdio_rpc(self, payload: dict) -> dict:
        self._write_stdio(payload)
        msg = self._read_stdio()
        return self._rpc_result(msg)

    def _drain_stderr(self) -> str:
        if not self._proc or not self._proc.stderr:
            return ""
        try:
            self._proc.stderr.flush()
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ #
    # HTTP (streamable / JSON-RPC)
    # ------------------------------------------------------------------ #
    def _initialize_http(self) -> None:
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "1.0"},
        })
        self.notify("notifications/initialized")
        self._initialized = True

    def _http_rpc(self, payload: dict, notification: bool = False) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (401, 403) or is_auth_failure(body):
                raise McpAuthError(
                    f"MCP authentication failed (HTTP {e.code}). "
                    f"Run: mcp-adaptor auth\n{body[:400]}"
                ) from e
            raise McpError(f"MCP HTTP {e.code}: {body[:400]}") from e
        except urllib.error.URLError as e:
            raise McpUnavailable(f"MCP HTTP endpoint unreachable: {e}") from e
        if notification or not raw.strip():
            return {}
        # SSE: "event: message\ndata: {...}\n\n"
        if raw.lstrip().startswith("event:") or "data:" in raw[:40]:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
        msg = json.loads(raw)
        return self._rpc_result(msg)

    # ------------------------------------------------------------------ #
    # result handling
    # ------------------------------------------------------------------ #
    def _rpc_result(self, msg: dict) -> dict:
        if not isinstance(msg, dict):
            raise McpError(f"non-object MCP message: {msg!r}")
        if msg.get("error"):
            err = msg["error"]
            text = json.dumps(err, ensure_ascii=False)
            if is_auth_failure(text):
                raise McpAuthError(
                    "MCP authentication failed. Run: mcp-adaptor auth\n" + text[:400]
                )
            raise McpError(f"MCP error: {text[:600]}")
        return msg.get("result") if isinstance(msg.get("result"), dict) else (msg.get("result") or {})

    def _unwrap_tool_result(self, result: Any, name: str) -> Any:
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            text = _content_text(result)
            if is_auth_failure(text):
                raise McpAuthError(
                    f"MCP tool '{name}' is unauthenticated. Run: mcp-adaptor auth\n{text[:400]}"
                )
            raise McpError(f"MCP tool '{name}' error: {text[:800]}")
        text = _content_text(result)
        if is_auth_failure(text):
            raise McpAuthError(
                f"MCP tool '{name}' is unauthenticated. Run: mcp-adaptor auth\n{text[:400]}"
            )
        # Prefer structured content when present.
        if "structuredContent" in result:
            return result["structuredContent"]
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result


def _content_text(result: dict) -> str:
    chunks: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
        elif isinstance(item, str):
            chunks.append(item)
    return "\n".join(chunks)


def deepl_client(**kwargs) -> McpClient | None:
    """DeepL MCP client if configured. Returns None when not configured."""
    url = kwargs.pop("url", "") or os.environ.get("MCP_DEEPL_URL", "")
    command = kwargs.pop("command", "") or os.environ.get("MCP_DEEPL_COMMAND", "")
    raw_args = os.environ.get("MCP_DEEPL_ARGS", "")
    args = kwargs.pop("args", None)
    if args is None:
        args = raw_args.split() if raw_args else []
        server = os.environ.get("MCP_DEEPL_SERVER", "").strip()
        if server and not args:
            adaptor = _default_adaptor_bin()
            if not command:
                command = adaptor
            args = ["--server", server]
    if not url and not command:
        return None
    return McpClient(command=command, args=args, url=url, **kwargs)
