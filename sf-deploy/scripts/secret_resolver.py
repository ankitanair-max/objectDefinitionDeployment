#!/usr/bin/env python3
"""Non-interactive secret resolution for the deployment pipeline."""
from __future__ import annotations

import getpass
import os
import platform
import pwd
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


KEYCHAIN_SERVICE = "sf-deploy/deepl"
MAX_SECRET_BYTES = 8192


class SecretResolutionError(RuntimeError):
    """A configured secret source was invalid or inaccessible."""


@dataclass(frozen=True, repr=False)
class ResolvedSecret:
    value: str
    source: str

    def __repr__(self) -> str:
        return f"ResolvedSecret(value=<redacted>, source={self.source!r})"


def _clean(value: str, source: str) -> ResolvedSecret:
    value = (value or "").strip()
    if not value:
        raise SecretResolutionError(f"{source} returned an empty secret")
    if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
        raise SecretResolutionError(f"{source} returned an oversized secret")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SecretResolutionError(f"{source} returned an invalid multiline secret")
    return ResolvedSecret(value=value, source=source)


def _from_file(raw_path: str) -> ResolvedSecret:
    path = Path(raw_path).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif path.is_symlink():
        raise SecretResolutionError("DEEPL_API_KEY_FILE must not be a symlink")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SecretResolutionError(
            "DEEPL_API_KEY_FILE is unreadable or is a symlink"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretResolutionError(
                "DEEPL_API_KEY_FILE must be a regular file"
            )
        if os.name != "nt" and info.st_mode & 0o077:
            raise SecretResolutionError(
                "DEEPL_API_KEY_FILE permissions are too broad; use mode 0600"
            )
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            data = handle.read(MAX_SECRET_BYTES + 1)
    except OSError as exc:
        raise SecretResolutionError(
            "DEEPL_API_KEY_FILE could not be read"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_SECRET_BYTES:
        raise SecretResolutionError("DEEPL_API_KEY_FILE is oversized")
    try:
        return _clean(data.decode("utf-8"), "file")
    except UnicodeDecodeError as exc:
        raise SecretResolutionError("DEEPL_API_KEY_FILE is not UTF-8") from exc


def _from_keychain(account: str, timeout: float) -> ResolvedSecret | None:
    if platform.system() != "Darwin" or not Path("/usr/bin/security").is_file():
        return None
    try:
        cp = subprocess.run(
            [
                "/usr/bin/security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE, "-a", account, "-w",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return _clean(cp.stdout, "macOS Keychain")


def _current_username() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return getpass.getuser()


def _from_command(raw_command: str, timeout: float) -> ResolvedSecret:
    try:
        argv = shlex.split(raw_command)
    except ValueError as exc:
        raise SecretResolutionError("DEEPL_API_KEY_COMMAND is malformed") from exc
    if not argv:
        raise SecretResolutionError("DEEPL_API_KEY_COMMAND is empty")
    env = os.environ.copy()
    for name in ("DEEPL_API_KEY", "DEEPL_API_KEY_FILE", "DEEPL_API_KEY_COMMAND"):
        env.pop(name, None)
    try:
        cp = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise SecretResolutionError("DEEPL_API_KEY_COMMAND timed out") from exc
    except OSError as exc:
        raise SecretResolutionError("DEEPL_API_KEY_COMMAND could not start") from exc
    if cp.returncode != 0:
        raise SecretResolutionError(
            f"DEEPL_API_KEY_COMMAND failed with exit code {cp.returncode}"
        )
    return _clean(cp.stdout, "command")


def resolve_deepl_api_key(
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float = 5.0,
) -> ResolvedSecret | None:
    """Resolve a DeepL key without prompting or persisting it.

    Precedence: environment, mounted file, macOS Keychain, command.
    """
    env = os.environ if environ is None else environ
    direct = (env.get("DEEPL_API_KEY") or "").strip()
    if direct:
        return _clean(direct, "environment")
    key_file = (env.get("DEEPL_API_KEY_FILE") or "").strip()
    if key_file:
        return _from_file(key_file)
    keychain = _from_keychain(_current_username(), timeout)
    if keychain:
        return keychain
    command = (env.get("DEEPL_API_KEY_COMMAND") or "").strip()
    if command:
        return _from_command(command, timeout)
    return None
