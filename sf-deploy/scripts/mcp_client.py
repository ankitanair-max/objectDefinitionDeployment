#!/usr/bin/env python3
"""
mcp_client.py — JSON-RPC client for an already-authenticated MCP server.

Used for optional DeepL translation over stdio or streamable HTTP.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from secret_resolver import SecretResolutionError, resolve_deepl_api_key

MAX_MCP_MESSAGE_BYTES = 8 * 1024 * 1024


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
    """JSON-RPC 2.0 MCP client (stdio NDJSON or Content-Length, or HTTP)."""

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
        sensitive_values: list[str] | None = None,
    ):
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.url = (url or os.environ.get("MCP_URL") or "").rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.client_name = client_name
        self._sensitive_values = [v for v in (sensitive_values or []) if v]
        self.framing = ""
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._initialized = False
        self._stderr_buf: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_buf = bytearray()
        self._session_id = ""
        self.secret_source = ""

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
        wanted = (os.environ.get("MCP_STDIO_FRAMING") or "auto").strip().lower()
        if wanted not in ("auto", "ndjson", "lsp"):
            wanted = "auto"
        if wanted == "auto":
            last: Exception | None = None
            for mode in ("ndjson", "lsp"):
                try:
                    self._spawn()
                    self.framing = mode
                    self._initialize_stdio()
                    return
                except (McpUnavailable, McpError) as e:
                    last = e
                    self.close()
            raise McpUnavailable(
                f"MCP stdio initialize failed (NDJSON and Content-Length). {last}"
            ) from last
        self._spawn()
        self.framing = wanted
        self._initialize_stdio()

    def _spawn(self) -> None:
        self.close()
        cmd = [self.command, *self.args]
        merged = os.environ.copy()
        # Resolver controls these inputs; the child receives only the resolved
        # key, never local file paths or secret-manager commands.
        merged.pop("DEEPL_API_KEY_FILE", None)
        merged.pop("DEEPL_API_KEY_COMMAND", None)
        if self.env:
            merged.update(self.env)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged,
                bufsize=0,
            )
        except OSError as e:
            raise McpUnavailable(
                "MCP server could not start: "
                f"{self._redact(self.command)}. {self._redact(str(e))}"
            ) from e
        stderr = self._proc.stderr
        stderr_buf: list[str] = []
        self._stderr_buf = stderr_buf
        self._stdout_buf = bytearray()
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader, args=(stderr, stderr_buf), daemon=True)
        self._stderr_thread.start()

    @staticmethod
    def _stderr_reader(err, target: list[str]) -> None:
        if not err:
            return
        try:
            while True:
                chunk = os.read(err.fileno(), 4096)
                if not chunk:
                    break
                target.append(chunk.decode("utf-8", errors="replace"))
                if len(target) > 20:
                    del target[:-20]
        except Exception:
            pass

    def close(self) -> None:
        proc = self._proc
        thread = self._stderr_thread
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=1)
        self._proc = None
        self._stderr_thread = None
        self._initialized = False
        self._id = 0
        self._stdout_buf = bytearray()

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
            raise McpError("MCP initialize returned a non-object payload")
        self.notify("notifications/initialized")
        self._initialized = True

    def _write_stdio(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise McpUnavailable("MCP stdio process is not running")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.framing == "lsp":
            frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        else:
            frame = body + b"\n"
        try:
            self._proc.stdin.write(frame)
            self._proc.stdin.flush()
        except BrokenPipeError as e:
            err = self._drain_stderr()
            raise McpUnavailable(f"MCP server closed stdin. {err}") from e

    def _wait_readable(self, timeout: float) -> bool:
        if not self._proc or not self._proc.stdout:
            return False
        fd = self._proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        return bool(ready)

    def _read_chunk(self, deadline: float) -> None:
        if not self._proc or not self._proc.stdout:
            raise McpUnavailable("MCP stdio process is not running")
        remaining = max(0.0, deadline - time.monotonic())
        if not self._wait_readable(remaining):
            raise McpUnavailable(
                f"MCP stdio timed out ({self.framing or 'unknown'}). "
                f"{self._drain_stderr()}"
            )
        chunk = os.read(self._proc.stdout.fileno(), 65536)
        if not chunk:
            raise McpUnavailable(
                f"MCP server closed stdout before a response. "
                f"{self._drain_stderr()}"
            )
        if len(self._stdout_buf) + len(chunk) > MAX_MCP_MESSAGE_BYTES:
            raise McpError(
                f"MCP stdio response exceeded {MAX_MCP_MESSAGE_BYTES} bytes"
            )
        self._stdout_buf.extend(chunk)

    def _readline(self, deadline: float) -> bytes:
        while True:
            pos = self._stdout_buf.find(b"\n")
            if pos >= 0:
                line = bytes(self._stdout_buf[:pos + 1])
                del self._stdout_buf[:pos + 1]
                return line
            self._read_chunk(deadline)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        while len(self._stdout_buf) < size:
            self._read_chunk(deadline)
        out = bytes(self._stdout_buf[:size])
        del self._stdout_buf[:size]
        return out

    def _read_stdio(self, timeout: float | None = None) -> dict:
        wait = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait
        first = self._readline(deadline)
        stripped = first.strip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            self.framing = self.framing or "ndjson"
            return self._decode_json(stripped, "NDJSON")
        header_lines = [first]
        while True:
            line = self._readline(deadline)
            if line in (b"\r\n", b"\n"):
                break
            header_lines.append(line)
        headers = b"".join(header_lines).decode("ascii", errors="replace")
        length = 0
        for h in headers.splitlines():
            if h.lower().startswith("content-length:"):
                try:
                    length = int(h.split(":", 1)[1].strip())
                except ValueError as exc:
                    raise McpError(
                        "MCP Content-Length header was invalid"
                    ) from exc
        if length > MAX_MCP_MESSAGE_BYTES:
            raise McpError(
                f"MCP Content-Length exceeded {MAX_MCP_MESSAGE_BYTES} bytes"
            )
        if length:
            self.framing = "lsp"
            body = self._read_exact(length, deadline)
            return self._decode_json(body, "Content-Length")
        raw = b"".join(header_lines).strip()
        if raw.startswith(b"{"):
            return self._decode_json(raw, "unframed")
        raise McpError("MCP server returned an invalid framing header")

    def _decode_json(self, raw: bytes, framing: str) -> dict:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpError(f"MCP {framing} response was not valid JSON") from exc
        if not isinstance(message, dict):
            raise McpError(f"MCP {framing} response was not a JSON object")
        return message

    def _stdio_rpc(self, payload: dict) -> dict:
        self._write_stdio(payload)
        timeout = min(self.timeout, 30.0) if not self._initialized else self.timeout
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            msg = self._read_stdio(timeout=remaining)
            if msg.get("id") == payload.get("id"):
                return self._rpc_result(msg)
            if msg.get("method"):
                continue
            raise McpError(
                "unexpected MCP response id: "
                + self._redact(repr(msg.get("id")))
            )

    def _drain_stderr(self) -> str:
        return self._redact("".join(self._stderr_buf[-20:]).strip())

    def _redact(self, text: str) -> str:
        out = text or ""
        for value in self._sensitive_values:
            out = out.replace(value, "<redacted>")
        return out

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
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._session_id = (
                    resp.headers.get("Mcp-Session-Id") or self._session_id
                )
                raw_bytes = resp.read(MAX_MCP_MESSAGE_BYTES + 1)
                if len(raw_bytes) > MAX_MCP_MESSAGE_BYTES:
                    raise McpError(
                        f"MCP HTTP response exceeded {MAX_MCP_MESSAGE_BYTES} bytes"
                    )
                try:
                    raw = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise McpError(
                        "MCP HTTP response was not UTF-8"
                    ) from exc
        except urllib.error.HTTPError as e:
            body_bytes = e.read(MAX_MCP_MESSAGE_BYTES + 1)
            if len(body_bytes) > MAX_MCP_MESSAGE_BYTES:
                body = "<oversized HTTP error body discarded>"
            else:
                body = body_bytes.decode("utf-8", errors="replace")
            if e.code in (401, 403) or is_auth_failure(body):
                raise McpAuthError(
                    f"MCP authentication failed (HTTP {e.code}). "
                    f"Run: mcp-adaptor auth\n{self._redact(body[:400])}"
                ) from e
            raise McpError(
                f"MCP HTTP {e.code}: {self._redact(body[:400])}"
            ) from e
        except urllib.error.URLError as e:
            raise McpUnavailable(
                f"MCP HTTP endpoint unreachable: {self._redact(str(e))}"
            ) from e
        if notification or not raw.strip():
            return {}
        # SSE: "event: message\ndata: {...}\n\n"
        if raw.lstrip().startswith("event:") or "data:" in raw[:40]:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError("MCP HTTP response was not valid JSON") from exc
        return self._rpc_result(msg)

    # ------------------------------------------------------------------ #
    # result handling
    # ------------------------------------------------------------------ #
    def _rpc_result(self, msg: dict) -> dict:
        if not isinstance(msg, dict):
            raise McpError("MCP message was not a JSON object")
        if msg.get("error"):
            err = msg["error"]
            text = json.dumps(err, ensure_ascii=False)
            if is_auth_failure(text):
                raise McpAuthError(
                    "MCP authentication failed. Run: mcp-adaptor auth\n"
                    + self._redact(text[:400])
                )
            raise McpError(f"MCP error: {self._redact(text[:600])}")
        result = (
            msg.get("result")
            if isinstance(msg.get("result"), dict)
            else (msg.get("result") or {})
        )
        if self._contains_sensitive(result):
            raise McpError(
                "MCP server response contained secret material and was discarded"
            )
        return result

    def _contains_sensitive(self, value: Any) -> bool:
        if not self._sensitive_values:
            return False
        if isinstance(value, str):
            return any(secret in value for secret in self._sensitive_values)
        if isinstance(value, dict):
            return any(
                self._contains_sensitive(k) or self._contains_sensitive(v)
                for k, v in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(self._contains_sensitive(item) for item in value)
        return False

    def _unwrap_tool_result(self, result: Any, name: str) -> Any:
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            text = _content_text(result)
            if is_auth_failure(text):
                raise McpAuthError(
                    f"MCP tool '{name}' is unauthenticated. Run: mcp-adaptor auth\n"
                    f"{self._redact(text[:400])}"
                )
            raise McpError(
                f"MCP tool '{name}' error: {self._redact(text[:800])}"
            )
        text = _content_text(result)
        if is_auth_failure(text):
            raise McpAuthError(
                f"MCP tool '{name}' is unauthenticated. Run: mcp-adaptor auth\n"
                f"{self._redact(text[:400])}"
            )
        # Prefer structured content when present.
        if "structuredContent" in result:
            return result["structuredContent"]
        if text:
            # Only containers are decoded: a translation of "null", "true" or
            # "123" is text, not a JSON scalar.
            if text.lstrip()[:1] in ("{", "["):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
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
    """DeepL MCP client from MCP_DEEPL_* env vars. Does not read Cursor mcp.json."""
    url = kwargs.pop("url", "") or os.environ.get("MCP_DEEPL_URL", "")
    command = kwargs.pop("command", "") or os.environ.get("MCP_DEEPL_COMMAND", "")
    raw_args = os.environ.get("MCP_DEEPL_ARGS", "")
    args = kwargs.pop("args", None)
    client_env = dict(kwargs.pop("env", None) or {})
    try:
        secret = resolve_deepl_api_key()
    except SecretResolutionError as exc:
        raise McpUnavailable(f"DeepL API key resolution failed: {exc}") from exc
    if secret:
        client_env["DEEPL_API_KEY"] = secret.value
    if args is None:
        args = shlex.split(raw_args) if raw_args else []
        server = os.environ.get("MCP_DEEPL_SERVER", "").strip()
        if server and not args:
            adaptor = _default_adaptor_bin()
            if not command:
                command = adaptor
            args = ["--server", server]
    if secret and not url and not command:
        installed = shutil.which("deepl-mcp-server")
        npx = shutil.which("npx")
        if installed:
            command, args = installed, []
        elif npx:
            command, args = npx, ["-y", "deepl-mcp-server@1.3.9"]
    if secret and any(
        secret.value in str(part) for part in [command, url, *(args or [])]
    ):
        raise McpUnavailable(
            "Refusing to place the DeepL API key in an MCP command, argument, "
            "or URL; provide it only through a supported secret source."
        )
    if not url and not command:
        return None
    client = McpClient(
        command=command,
        args=args,
        env=client_env or None,
        url=url,
        sensitive_values=[secret.value] if secret else [],
        **kwargs,
    )
    client.secret_source = secret.source if secret else ""
    return client
