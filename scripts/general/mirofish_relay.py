#!/usr/bin/env python3
"""Multi-account Mirofish Anthropic-compatible relay with a built-in WebUI.

Credential backends: macOS Keychain (host) or an encrypted secrets file
(Docker/container). SQLite stores metadata only. The relay never forwards a
caller's upstream Authorization header.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import datetime as datetime_module
import getpass
import http.client
import json
import os
import pathlib
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional


AUTH_BASE = os.environ.get("MIROFISH_AUTH_BASE", "https://admin.test.mirofish.ai")
RELAY_BASE = os.environ.get("MIROFISH_RELAY_BASE", "https://mirasim-relay.mirofish.ai")
ANTHROPIC_VERSION = "2023-06-01"
KEYCHAIN_SERVICE = "open-reverselab.mirofish-relay"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_DATA_DIR = pathlib.Path.home() / ".config" / "mirofish-relay"
MAX_BODY_BYTES = 8 * 1024 * 1024
CRED_BACKEND = os.environ.get("MIROFISH_CRED_BACKEND", "").lower()
PROXY_POOL_ALIAS = "proxy_pool"
PROXY_REFRESH_SECONDS = max(30.0, float(os.environ.get("MIROFISH_PROXY_REFRESH_SECONDS", "600")))
PROXY_FETCH_MAX_BYTES = 8 * 1024 * 1024
PROXY_FETCH_TIMEOUT = max(3.0, float(os.environ.get("MIROFISH_PROXY_FETCH_TIMEOUT", "10")))
MIHOMO_CONTROLLER_TIMEOUT = max(1.0, min(PROXY_FETCH_TIMEOUT,
    float(os.environ.get("MIROFISH_MIHOMO_CONTROLLER_TIMEOUT", "5"))))
PROXY_SUBSCRIPTION_USER_AGENT = os.environ.get(
    "MIROFISH_PROXY_SUBSCRIPTION_USER_AGENT", "mihomo/1.19.0").strip() or "mihomo/1.19.0"
PROXY_FAILURE_THRESHOLD = max(1, int(os.environ.get("MIROFISH_PROXY_FAILURE_THRESHOLD", "2")))
MIHOMO_CONTROLLER = os.environ.get("MIROFISH_MIHOMO_CONTROLLER", "").rstrip("/")
MIHOMO_PROXY_URL = os.environ.get("MIROFISH_MIHOMO_PROXY", "").strip()
MIHOMO_SELECTOR = os.environ.get("MIROFISH_MIHOMO_SELECTOR", "MirofishPool").strip() or "MirofishPool"
MIHOMO_PROVIDER = os.environ.get("MIROFISH_MIHOMO_PROVIDER", "mirofish").strip() or "mirofish"
MIHOMO_SYSTEM_PROXIES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}


class RelayError(RuntimeError):
    def __init__(self, message: str, status: int = 500, data: Any = None):
        super().__init__(message)
        self.status = status
        self.data = data


def utc_now() -> str:
    return datetime_module.datetime.now(datetime_module.timezone.utc).isoformat()


def dump_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": raw[:1000].decode("utf-8", "replace")}


def alias_value(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise RelayError("alias must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}", 400)
    return value


def email_value(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise RelayError("invalid email address", 400)
    return value


def token_value(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RelayError("upstream response did not contain " + key, 502)
    return value


def proxy_subscription_value(value: str) -> str:
    """Validate a subscription URL without ever returning it in diagnostics."""
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
    except ValueError as exc:
        raise RelayError("proxy subscription must be an http(s) URL", 400) from exc
    return value


def proxy_subscription_file_value(value: str) -> str:
    """Validate a container-local provider file path without exposing its content."""
    value = value.strip()
    path = pathlib.PurePosixPath(value)
    if not value or not path.is_absolute() or ".." in path.parts:
        raise RelayError("proxy subscription file must be an absolute container path", 400)
    return value


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("#"):
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].replace("\\\"", "\"") if value[0] == '"' else value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _inline_yaml_mapping(value: str) -> dict[str, Any]:
    """Parse the small inline mapping form commonly emitted by subscriptions."""
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return {}
    result: dict[str, Any] = {}
    current: list[str] = []
    quote = ""
    depth = 0
    parts: list[str] = []
    for char in value[1:-1]:
        if char in "'\"":
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif not quote and char in "[{":
            depth += 1
        elif not quote and char in "]}":
            depth -= 1
        if char == "," and not quote and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    for part in parts:
        if ":" not in part:
            continue
        key, raw = part.split(":", 1)
        result[key.strip().strip("'\"")] = _yaml_scalar(raw)
    return result


def _proxy_from_mapping(mapping: dict[str, Any], fallback_name: str = "") -> Optional[dict[str, Any]]:
    kind = str(mapping.get("type", "")).strip().lower()
    scheme = {"http": "http", "https": "https", "socks": "socks5", "socks5": "socks5"}.get(kind)
    if not scheme or (scheme == "socks5" and bool(mapping.get("tls"))):
        return None
    host = str(mapping.get("server", mapping.get("host", ""))).strip()
    try:
        port = int(mapping.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not host or not 1 <= port <= 65535:
        return None
    name = str(mapping.get("name", fallback_name)).strip()[:200] or f"{host}:{port}"
    username = mapping.get("username", mapping.get("user"))
    password = mapping.get("password")
    return {"name": name, "scheme": scheme, "host": host, "port": port,
            "username": str(username) if username is not None else "",
            "password": str(password) if password is not None else ""}


def _proxy_from_uri(value: str) -> Optional[dict[str, Any]]:
    value = value.strip().strip("'\"")
    if "://" not in value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        scheme = {"http": "http", "https": "https", "socks": "socks5",
                  "socks5": "socks5", "socks5h": "socks5"}.get(parsed.scheme.lower())
        if not scheme or not parsed.hostname or not parsed.port:
            return None
        name = urllib.parse.unquote(parsed.fragment).strip()[:200] if parsed.fragment else ""
        return {"name": name or f"{parsed.hostname}:{parsed.port}", "scheme": scheme,
                "host": parsed.hostname, "port": parsed.port,
                "username": urllib.parse.unquote(parsed.username or ""),
                "password": urllib.parse.unquote(parsed.password or "")}
    except (ValueError, UnicodeError):
        return None


def _mihomo_yaml_proxies(text: str) -> list[dict[str, Any]]:
    """Extract proxy entries without adding PyYAML to the stdlib-only relay."""
    lines = text.splitlines()
    in_proxies = False
    proxy_indent: Optional[int] = None
    current: dict[str, Any] = {}
    result: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if current:
            result.append(current)
            current = {}

    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not in_proxies:
            if re.match(r"^proxies\s*:\s*(?:#.*)?$", stripped):
                in_proxies = True
                proxy_indent = indent
            continue
        if proxy_indent is not None and indent <= proxy_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("- ") or stripped == "-":
            flush()
            inline = stripped[1:].strip()
            if inline.startswith("{"):
                current.update(_inline_yaml_mapping(inline))
            elif ":" in inline:
                key, value = inline.split(":", 1)
                current[key.strip()] = _yaml_scalar(value)
            continue
        if not current or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = _yaml_scalar(value)
    flush()
    return result


def parse_proxy_subscription(raw: bytes) -> tuple[list[dict[str, Any]], int]:
    """Parse URI-list, base64 URI-list, JSON, or common Mihomo YAML output.

    Only transports that urllib can use directly (HTTP(S) and SOCKS5) are
    returned.  Encrypted Mihomo transports are counted as skipped instead of
    being treated as direct connections.
    """
    text = raw.decode("utf-8", "replace").lstrip("\ufeff").strip()
    candidates = [text]
    compact = "".join(text.split())
    if len(compact) >= 16:
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
            decoded_text = decoded.decode("utf-8", "replace").strip()
            if decoded_text and decoded_text != text:
                candidates.insert(0, decoded_text)
        except (ValueError, binascii.Error):
            pass

    supported: list[dict[str, Any]] = []
    skipped = 0
    seen: set[str] = set()
    for candidate in candidates:
        mappings: list[dict[str, Any]] = []
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
                mappings = [item for item in parsed["proxies"] if isinstance(item, dict)]
            elif isinstance(parsed, list):
                mappings = [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
        if not mappings and "proxies:" in candidate:
            mappings = _mihomo_yaml_proxies(candidate)
        entries: list[Optional[dict[str, Any]]] = []
        if mappings:
            entries.extend(_proxy_from_mapping(item) for item in mappings)
        else:
            entries.extend(_proxy_from_uri(line) for line in candidate.splitlines())
        candidate_supported = False
        for item in entries:
            if item is None:
                if any(token in candidate.lower() for token in ("://", "type:", "proxies:")):
                    skipped += 1
                continue
            candidate_supported = True
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if identity not in seen:
                seen.add(identity)
                supported.append(item)
        if candidate_supported:
            break
    if not supported and text:
        raise RelayError("proxy subscription contains no supported HTTP(S)/SOCKS5 nodes", 502,
                         {"skipped": skipped})
    return supported, skipped


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = sock.recv(length - len(output))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        output.extend(chunk)
    return bytes(output)


def _socks5_connect(proxy: dict[str, Any], target_host: str, target_port: int,
                    timeout: float) -> socket.socket:
    sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
    try:
        username = proxy.get("username", "").encode("utf-8")
        password = proxy.get("password", "").encode("utf-8")
        methods = b"\x00\x02" if username or password else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 255:
            raise OSError("SOCKS5 proxy authentication negotiation failed")
        if method == 2:
            if len(username) > 255 or len(password) > 255:
                raise OSError("SOCKS5 credentials are too long")
            sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            if _recv_exact(sock, 2) != b"\x01\x00":
                raise OSError("SOCKS5 proxy authentication failed")
        elif method != 0:
            raise OSError("SOCKS5 proxy requires unsupported authentication")
        try:
            target_ip = socket.inet_pton(socket.AF_INET, target_host)
            address = b"\x01" + target_ip
        except OSError:
            try:
                target_ip = socket.inet_pton(socket.AF_INET6, target_host)
                address = b"\x04" + target_ip
            except OSError:
                encoded = target_host.encode("idna")
                if len(encoded) > 255:
                    raise OSError("target hostname is too long")
                address = b"\x03" + bytes([len(encoded)]) + encoded
        sock.sendall(b"\x05\x01\x00" + address + target_port.to_bytes(2, "big"))
        version, status, _reserved, address_type = _recv_exact(sock, 4)
        if version != 5 or status != 0:
            raise OSError("SOCKS5 proxy could not connect to upstream")
        if address_type == 1:
            _recv_exact(sock, 4)
        elif address_type == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        elif address_type == 4:
            _recv_exact(sock, 16)
        else:
            raise OSError("SOCKS5 proxy returned an invalid address")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, proxy: dict[str, Any], timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
                 **kwargs: Any) -> None:
        super().__init__(host, timeout=timeout, **kwargs)
        self._proxy = proxy

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 30.0
        self.sock = _socks5_connect(self._proxy, self.host, self.port, timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, proxy: dict[str, Any], context: Any,
                 timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(host, timeout=timeout, context=context, **kwargs)
        self._proxy = proxy

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 30.0
        sock = _socks5_connect(self._proxy, self.host, self.port, timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy: dict[str, Any]) -> None:
        super().__init__()
        self.proxy = proxy

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(lambda host, **kwargs: _SocksHTTPConnection(host, proxy=self.proxy, **kwargs), req)


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: dict[str, Any]) -> None:
        super().__init__()
        self.proxy = proxy

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            lambda host, **kwargs: _SocksHTTPSConnection(host, proxy=self.proxy,
                                                         context=self._context, **kwargs), req)


def _proxy_url(proxy: dict[str, Any]) -> str:
    auth = ""
    if proxy.get("username") or proxy.get("password"):
        auth = urllib.parse.quote(str(proxy.get("username", "")), safe="") + ":" + \
            urllib.parse.quote(str(proxy.get("password", "")), safe="") + "@"
    host = proxy["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return f"{proxy['scheme']}://{auth}{host}:{proxy['port']}"


def open_url(request: urllib.request.Request, timeout: float,
             proxy: Optional[dict[str, Any]] = None) -> Any:
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    if proxy["scheme"] == "socks5":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                              _SocksHTTPHandler(proxy), _SocksHTTPSHandler(proxy))
    else:
        proxy_url = _proxy_url(proxy)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url,
                                                                           "https": proxy_url}))
    return opener.open(request, timeout=timeout)


class Keychain:
    def __init__(self) -> None:
        self.service = KEYCHAIN_SERVICE

    def account_name(self, alias: str, kind: str) -> str:
        return alias_value(alias) + ":" + kind

    def put(self, alias: str, kind: str, value: str) -> None:
        if sys.platform != "darwin":
            raise RelayError("persistent credentials require macOS Keychain", 500)
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", self.account_name(alias, kind),
             "-s", self.service, "-w", value],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RelayError("could not save credential to macOS Keychain", 500)

    def get(self, alias: str, kind: str) -> str:
        if sys.platform != "darwin":
            raise RelayError("persistent credentials require macOS Keychain", 500)
        result = subprocess.run(
            ["security", "find-generic-password", "-a", self.account_name(alias, kind),
             "-s", self.service, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RelayError("credential is missing from macOS Keychain", 500)
        return result.stdout.strip()

    def delete(self, alias: str, kind: str) -> None:
        if sys.platform != "darwin":
            return
        subprocess.run(
            ["security", "delete-generic-password", "-a", self.account_name(alias, kind),
             "-s", self.service],
            capture_output=True, text=True,
        )


class FileVault:
    """Encrypted-at-rest credential file for Docker/Linux.

    Format: v1.<base64url(salt||nonce||ciphertext||tag)>
    Encryption: HMAC-SHA256 derived keystream (SHA-256 CTR-like blocks) plus an
    HMAC-SHA256 integrity tag over the salt, nonce and ciphertext. The master
    key is read from MIROFISH_MASTER_KEY or MIROFISH_MASTER_KEY_FILE and never
    stored alongside the secrets file.
    """

    def __init__(self, secrets_path: pathlib.Path) -> None:
        self.secrets_path = secrets_path
        self.master_key = self._load_master_key()
        self.lock = __import__("threading").Lock()

    def _load_master_key(self) -> bytes:
        key_file = os.environ.get("MIROFISH_MASTER_KEY_FILE")
        if key_file:
            key_text = pathlib.Path(key_file).read_text(encoding="utf-8").strip()
        else:
            key_text = os.environ.get("MIROFISH_MASTER_KEY", "").strip()
        if not key_text:
            raise RelayError(
                "file credential backend requires MIROFISH_MASTER_KEY "
                "(or MIROFISH_MASTER_KEY_FILE)", 500)
        if len(key_text) < 16:
            raise RelayError("MIROFISH_MASTER_KEY must be at least 16 characters", 500)
        return key_text.encode("utf-8")

    def _derive(self, salt: bytes) -> tuple[bytes, bytes]:
        enc = hashlib.pbkdf2_hmac("sha256", self.master_key, salt + b"enc", 60000, dklen=32)
        mac = hashlib.pbkdf2_hmac("sha256", self.master_key, salt + b"mac", 60000, dklen=32)
        return enc, mac

    def _stream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest())
            counter += 1
        return bytes(output[:length])

    def _encrypt(self, plaintext: str) -> str:
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        enc_key, mac_key = self._derive(salt)
        data = plaintext.encode("utf-8")
        stream = self._stream(enc_key, nonce, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(mac_key, salt + nonce + cipher, hashlib.sha256).digest()
        return "v1." + base64.urlsafe_b64encode(salt + nonce + cipher + tag).decode("ascii")

    def _decrypt(self, blob: str) -> str:
        try:
            version, encoded = blob.split(".", 1)
            if version != "v1":
                raise ValueError("unsupported version")
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
            salt, nonce, rest = raw[:16], raw[16:32], raw[32:]
            cipher, tag = rest[:-32], rest[-32:]
            enc_key, mac_key = self._derive(salt)
            expected = hmac.new(mac_key, salt + nonce + cipher, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError("integrity check failed")
            stream = self._stream(enc_key, nonce, len(cipher))
            return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")
        except (ValueError, IndexError) as exc:
            raise RelayError("could not decrypt secrets file (wrong master key?)", 500) from exc

    def _read_all(self) -> dict[str, dict[str, str]]:
        if not self.secrets_path.exists():
            return {}
        text = self.secrets_path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        decoded = self._decrypt(text)
        value = json.loads(decoded)
        return value if isinstance(value, dict) else {}

    def _write_all(self, data: dict[str, dict[str, str]]) -> None:
        blob = self._encrypt(json.dumps(data, ensure_ascii=False))
        fd = os.open(str(self.secrets_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(blob + "\n")
        finally:
            if fd != -1:
                os.close(fd)

    def put(self, alias: str, kind: str, value: str) -> None:
        with self.lock:
            data = self._read_all()
            entry = data.setdefault(alias_value(alias), {})
            entry[kind] = value
            self._write_all(data)

    def get(self, alias: str, kind: str) -> str:
        with self.lock:
            data = self._read_all()
        value = data.get(alias_value(alias), {}).get(kind, "")
        if not value:
            raise RelayError("credential is missing from secrets file", 500)
        return value

    def delete(self, alias: str, kind: str) -> None:
        with self.lock:
            data = self._read_all()
            entry = data.get(alias_value(alias))
            if not entry:
                return
            entry.pop(kind, None)
            if not entry:
                data.pop(alias_value(alias), None)
            self._write_all(data)


def make_credential_store(data_dir: pathlib.Path) -> Any:
    backend = CRED_BACKEND
    if not backend:
        backend = "keychain" if sys.platform == "darwin" and not os.environ.get(
            "MIROFISH_IN_DOCKER") else "file"
    if backend == "keychain":
        return Keychain()
    if backend == "file":
        return FileVault(data_dir / "secrets.enc")
    raise RelayError("unknown MIROFISH_CRED_BACKEND: " + backend, 500)


class Store:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, stat.S_IRWXU)
        self.db_path = self.data_dir / "accounts.sqlite3"
        self.proxy_key_path = self.data_dir / "proxy.key"
        self.keychain = make_credential_store(self.data_dir)
        self.db_lock = threading.RLock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
              alias TEXT PRIMARY KEY, email TEXT NOT NULL, user_id TEXT,
              plan TEXT, tenant TEXT, proxy_id TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        columns = {str(row[1]) for row in self.db.execute("PRAGMA table_info(accounts)")}
        if "proxy_id" not in columns:
            self.db.execute("ALTER TABLE accounts ADD COLUMN proxy_id TEXT")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proxies (
              proxy_id TEXT PRIMARY KEY, name TEXT NOT NULL, scheme TEXT NOT NULL,
              host TEXT NOT NULL, port INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              last_checked TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        self.db.commit()
        os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)

    def proxy_key(self) -> str:
        if self.proxy_key_path.exists():
            return self.proxy_key_path.read_text(encoding="utf-8").strip()
        value = secrets.token_urlsafe(32)
        fd = os.open(str(self.proxy_key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(value + "\n")
        finally:
            if fd != -1:
                os.close(fd)
        return value

    def aliases(self) -> list[str]:
        with self.db_lock:
            return [str(row[0]) for row in self.db.execute("SELECT alias FROM accounts ORDER BY alias")]

    def row(self, alias: str) -> sqlite3.Row:
        alias = alias_value(alias)
        with self.db_lock:
            row = self.db.execute("SELECT * FROM accounts WHERE alias=?", (alias,)).fetchone()
        if row is None:
            raise RelayError("unknown account: " + alias, 404)
        return row

    def credentials(self, alias: str) -> tuple[str, str]:
        self.row(alias)
        return self.keychain.get(alias, "access"), self.keychain.get(alias, "refresh")

    def save(self, alias: str, email: str, access: str, refresh: str,
             metadata: dict[str, Any], proxy_id: Optional[str] = None) -> None:
        alias = alias_value(alias)
        self.keychain.put(alias, "access", access)
        self.keychain.put(alias, "refresh", refresh)
        stamp = utc_now()
        with self.db_lock:
            self.db.execute("""
            INSERT INTO accounts(alias,email,user_id,plan,tenant,proxy_id,metadata_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(alias) DO UPDATE SET email=excluded.email,user_id=excluded.user_id,
              plan=excluded.plan,tenant=excluded.tenant,proxy_id=COALESCE(excluded.proxy_id, accounts.proxy_id),
              metadata_json=excluded.metadata_json,
              updated_at=excluded.updated_at
            """, (alias, email, metadata.get("user_id"), metadata.get("plan"),
                  metadata.get("tenant"), proxy_id, json.dumps(metadata, ensure_ascii=False), stamp, stamp))
            self.db.commit()

    def update_metadata(self, alias: str, metadata: dict[str, Any]) -> None:
        with self.db_lock:
            self.db.execute("UPDATE accounts SET user_id=?,plan=?,tenant=?,metadata_json=?,updated_at=? WHERE alias=?",
                            (metadata.get("user_id"), metadata.get("plan"), metadata.get("tenant"),
                             json.dumps(metadata, ensure_ascii=False), utc_now(), alias_value(alias)))
            self.db.commit()

    def remove(self, alias: str) -> None:
        alias = alias_value(alias)
        self.row(alias)
        self.keychain.delete(alias, "access")
        self.keychain.delete(alias, "refresh")
        with self.db_lock:
            self.db.execute("DELETE FROM accounts WHERE alias=?", (alias,))
            self.db.commit()

    def _optional_secret(self, alias: str, kind: str) -> str:
        try:
            return self.keychain.get(alias, kind)
        except RelayError as exc:
            if "missing" in str(exc).lower():
                return ""
            raise

    def proxy_subscription_url(self) -> str:
        file_path = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL_FILE", "").strip()
        if file_path:
            try:
                return proxy_subscription_value(pathlib.Path(file_path).read_text(encoding="utf-8"))
            except OSError as exc:
                raise RelayError("could not read proxy subscription URL file", 500) from exc
        env_value = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL", "").strip()
        if env_value:
            return proxy_subscription_value(env_value)
        return self._optional_secret(PROXY_POOL_ALIAS, "subscription_url").strip()

    def set_proxy_subscription_url(self, value: str) -> None:
        if value.strip():
            self.keychain.put(PROXY_POOL_ALIAS, "subscription_url", proxy_subscription_value(value))
        else:
            self.keychain.delete(PROXY_POOL_ALIAS, "subscription_url")

    def proxy_configs(self) -> dict[str, dict[str, Any]]:
        raw = self._optional_secret(PROXY_POOL_ALIAS, "configs")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayError("could not decode encrypted proxy pool", 500) from exc
        return value if isinstance(value, dict) else {}

    def save_proxy_configs(self, configs: dict[str, dict[str, Any]]) -> None:
        self.keychain.put(PROXY_POOL_ALIAS, "configs", json.dumps(configs, ensure_ascii=False))

    def set_account_proxy(self, alias: str, proxy_id: Optional[str]) -> None:
        with self.db_lock:
            self.db.execute("UPDATE accounts SET proxy_id=?,updated_at=? WHERE alias=?",
                            (proxy_id, utc_now(), alias_value(alias)))
            self.db.commit()

    def deactivate_proxies(self) -> None:
        with self.db_lock:
            self.db.execute("UPDATE proxies SET active=0,updated_at=?", (utc_now(),))
            self.db.commit()

    def upsert_proxy(self, proxy_id: str, config: dict[str, Any], active: bool = True) -> None:
        stamp = utc_now()
        with self.db_lock:
            self.db.execute("""
                INSERT INTO proxies(proxy_id,name,scheme,host,port,active,failure_count,last_error,
                                    last_checked,created_at,updated_at)
                VALUES(?,?,?,?,?,?,0,NULL,NULL,?,?)
                ON CONFLICT(proxy_id) DO UPDATE SET name=excluded.name,scheme=excluded.scheme,
                  host=excluded.host,port=excluded.port,active=excluded.active,
                  failure_count=CASE WHEN excluded.active=1 THEN 0 ELSE proxies.failure_count END,
                  last_error=CASE WHEN excluded.active=1 THEN NULL ELSE proxies.last_error END,
                  updated_at=excluded.updated_at
            """, (proxy_id, config["name"], config["scheme"], config["host"], config["port"],
                  1 if active else 0, stamp, stamp))
            self.db.commit()

    def proxy_rows(self, active_only: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM proxies WHERE active=1 ORDER BY proxy_id" if active_only \
            else "SELECT * FROM proxies ORDER BY proxy_id"
        with self.db_lock:
            return list(self.db.execute(query).fetchall())

    def mark_proxy_success(self, proxy_id: str) -> None:
        with self.db_lock:
            self.db.execute("UPDATE proxies SET failure_count=0,last_error=NULL,last_checked=?,updated_at=? WHERE proxy_id=?",
                            (utc_now(), utc_now(), proxy_id))
            self.db.commit()

    def mark_proxy_failure(self, proxy_id: str, message: str) -> None:
        with self.db_lock:
            self.db.execute("""
                UPDATE proxies SET failure_count=failure_count+1,last_error=?,last_checked=?,
                  active=CASE WHEN failure_count+1>=? THEN 0 ELSE active END,updated_at=?
                WHERE proxy_id=?
            """, (message[:500], utc_now(), PROXY_FAILURE_THRESHOLD, utc_now(), proxy_id))
            self.db.commit()

    def proxy_assignment_counts(self) -> dict[str, int]:
        with self.db_lock:
            rows = self.db.execute("SELECT proxy_id,COUNT(*) AS count FROM accounts WHERE proxy_id IS NOT NULL GROUP BY proxy_id")
            return {str(row[0]): int(row[1]) for row in rows}


def proxy_identity(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def fetch_proxy_subscription(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": "application/yaml, text/yaml, text/plain, application/json, */*",
        "User-Agent": PROXY_SUBSCRIPTION_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("content-length")
            try:
                if length and int(length) > PROXY_FETCH_MAX_BYTES:
                    raise RelayError("proxy subscription is too large", 413)
            except ValueError:
                pass
            body = response.read(PROXY_FETCH_MAX_BYTES + 1)
            if len(body) > PROXY_FETCH_MAX_BYTES:
                raise RelayError("proxy subscription is too large", 413)
            return body
    except urllib.error.HTTPError as exc:
        raise RelayError("proxy subscription request failed", 502, {"status": exc.code}) from exc
    except urllib.error.URLError as exc:
        raise RelayError("proxy subscription network error", 502,
                         {"reason": str(exc.reason)[:200]}) from exc


def mihomo_json(method: str, path: str, payload: Optional[dict[str, Any]] = None,
                timeout: float = 10.0) -> Any:
    if not MIHOMO_CONTROLLER:
        raise RelayError("Mihomo controller is not configured", 503)
    body = dump_json(payload) if payload is not None else None
    request = urllib.request.Request(MIHOMO_CONTROLLER + path, data=body, method=method,
                                     headers={"Accept": "application/json",
                                              "Content-Type": "application/json",
                                              "User-Agent": "mirofish-relay/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse_json(response.read())
    except urllib.error.HTTPError as exc:
        raise RelayError("Mihomo controller request failed", 502,
                         {"status": exc.code, "response": parse_json(exc.read())}) from exc
    except urllib.error.URLError as exc:
        raise RelayError("Mihomo controller is unavailable", 503,
                         {"reason": str(exc.reason)[:200]}) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RelayError("Mihomo controller request timed out", 503) from exc
    except OSError as exc:
        raise RelayError("Mihomo controller connection failed", 503,
                         {"reason": str(exc)[:200]}) from exc


def mihomo_path_component(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class ProxyPool:
    """Encrypted subscription-backed, sticky account-to-proxy assignments."""

    def __init__(self, store: Store, timeout: float = 30.0) -> None:
        self.store = store
        self.timeout = timeout
        self.lock = threading.RLock()
        self.mihomo_proxy = _proxy_from_uri(MIHOMO_PROXY_URL) if MIHOMO_PROXY_URL else None
        if bool(MIHOMO_CONTROLLER) != bool(MIHOMO_PROXY_URL):
            raise RelayError("configure both MIROFISH_MIHOMO_CONTROLLER and MIROFISH_MIHOMO_PROXY", 500)
        if MIHOMO_PROXY_URL and not self.mihomo_proxy:
            raise RelayError("MIROFISH_MIHOMO_PROXY must be an http(s) or socks5 URL", 500)
        self.subscription_url = store.proxy_subscription_url()
        self.configs = store.proxy_configs()
        self.last_refresh = 0.0
        self.last_attempt = 0.0
        self.last_error = ""
        self.skipped_nodes = 0

    @property
    def uses_mihomo(self) -> bool:
        return bool(MIHOMO_CONTROLLER and self.mihomo_proxy)

    def _sync_url(self) -> str:
        url = self.store.proxy_subscription_url()
        if url != self.subscription_url:
            self.subscription_url = url
            self.last_refresh = 0.0
            self.last_attempt = 0.0
            self.last_error = ""
            self.configs = self.store.proxy_configs()
        return url

    def _refresh_mihomo(self, force: bool) -> dict[str, Any]:
        now = time.time()
        if not force and now - self.last_refresh < PROXY_REFRESH_SECONDS:
            return self.public_summary()
        self.last_attempt = now
        try:
            if force:
                try:
                    mihomo_json("PUT", "/providers/proxies/" + mihomo_path_component(MIHOMO_PROVIDER),
                                timeout=min(self.timeout, MIHOMO_CONTROLLER_TIMEOUT))
                except RelayError:
                    # Older Mihomo releases may not expose provider refresh; the
                    # configured provider still refreshes on its own interval.
                    pass
            data = mihomo_json("GET", "/proxies/" + mihomo_path_component(MIHOMO_SELECTOR),
                               timeout=min(self.timeout, MIHOMO_CONTROLLER_TIMEOUT))
            node_names = data.get("all") if isinstance(data, dict) else None
            if not isinstance(node_names, list):
                raise RelayError("Mihomo selector did not return proxy nodes", 502)
            configs: dict[str, dict[str, Any]] = {}
            for raw_name in node_names:
                name = str(raw_name).strip()
                if not name or name in MIHOMO_SYSTEM_PROXIES or name == MIHOMO_SELECTOR:
                    continue
                config = {**self.mihomo_proxy, "name": name, "mihomo_node": name}
                proxy_id = proxy_identity(config)
                configs[proxy_id] = {**config, "id": proxy_id}
            if not configs:
                raise RelayError("Mihomo has not loaded any subscription nodes", 502)
            merged_configs = dict(self.configs)
            merged_configs.update(configs)
            self.store.save_proxy_configs(merged_configs)
            self.store.deactivate_proxies()
            for proxy_id, config in configs.items():
                self.store.upsert_proxy(proxy_id, config, active=True)
            self.configs = merged_configs
            self.skipped_nodes = 0
            self.last_refresh = now
            self.last_error = ""
            return self.public_summary()
        except RelayError as exc:
            self.last_error = str(exc)
            if force or not self.store.proxy_rows(active_only=True):
                raise
            return self.public_summary()

    def refresh(self, force: bool = False) -> dict[str, Any]:
        with self.lock:
            if self.uses_mihomo:
                return self._refresh_mihomo(force)
            url = self._sync_url()
            if not url:
                self.last_refresh = time.time()
                self.last_error = ""
                return self.public_summary()
            now = time.time()
            if not force and now - self.last_refresh < PROXY_REFRESH_SECONDS:
                return self.public_summary()
            self.last_attempt = now
            try:
                raw = fetch_proxy_subscription(url, min(self.timeout, PROXY_FETCH_TIMEOUT))
                configs_list, skipped = parse_proxy_subscription(raw)
                configs = {proxy_identity(item): {**item, "id": proxy_identity(item)} for item in configs_list}
                if not configs:
                    raise RelayError("proxy subscription contains no supported nodes", 502,
                                     {"skipped": skipped})
                merged_configs = dict(self.configs)
                merged_configs.update(configs)
                self.store.save_proxy_configs(merged_configs)
                self.store.deactivate_proxies()
                for proxy_id, config in configs.items():
                    self.store.upsert_proxy(proxy_id, config, active=True)
                self.configs = merged_configs
                self.skipped_nodes = skipped
                self.last_refresh = now
                self.last_error = ""
                return self.public_summary()
            except RelayError as exc:
                self.last_error = str(exc)
                if force or not self.store.proxy_rows(active_only=True):
                    raise
                return self.public_summary()

    def refresh_if_needed(self) -> None:
        with self.lock:
            if self.uses_mihomo:
                now = time.time()
                if now - self.last_refresh >= PROXY_REFRESH_SECONDS:
                    if not (self.last_error and now - self.last_attempt < PROXY_REFRESH_SECONDS
                            and self.store.proxy_rows(active_only=True)):
                        self.refresh(force=False)
                return
            url = self._sync_url()
            if not url:
                return
            now = time.time()
            if now - self.last_refresh < PROXY_REFRESH_SECONDS:
                return
            if self.last_error and now - self.last_attempt < PROXY_REFRESH_SECONDS:
                if self.store.proxy_rows(active_only=True):
                    return
            self.refresh(force=False)

    def set_subscription(self, value: str) -> dict[str, Any]:
        if self.uses_mihomo:
            raise RelayError("Mihomo mode reads the subscription from .env; change it and recreate the containers", 400)
        value = value.strip()
        if value:
            value = proxy_subscription_value(value)
        self.store.set_proxy_subscription_url(value)
        with self.lock:
            self.subscription_url = value
            self.last_refresh = 0.0
            self.last_attempt = 0.0
            self.last_error = ""
            if not value:
                self.store.deactivate_proxies()
                for alias in self.store.aliases():
                    self.store.set_account_proxy(alias, None)
        return self.refresh(force=True)

    def _config_for_row(self, row: sqlite3.Row) -> Optional[dict[str, Any]]:
        config = self.configs.get(str(row["proxy_id"]))
        return dict(config) if isinstance(config, dict) else None

    def _select(self, alias: str, exclude: Optional[str] = None) -> dict[str, Any]:
        rows = [row for row in self.store.proxy_rows(active_only=True)
                if str(row["proxy_id"]) != (exclude or "") and int(row["failure_count"]) == 0
                and self._config_for_row(row)]
        if not rows:
            raise RelayError("proxy pool has no available node for this account", 503)
        counts = self.store.proxy_assignment_counts()
        rows.sort(key=lambda row: (counts.get(str(row["proxy_id"]), 0),
                                   hashlib.sha256((alias + str(row["proxy_id"])).encode()).hexdigest()))
        config = self._config_for_row(rows[0])
        if not config:
            raise RelayError("proxy pool node configuration is missing", 500)
        return config

    def pending_proxy(self, alias: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        self.refresh_if_needed()
        if not self.subscription_url and not self.uses_mihomo:
            return None
        with self.lock:
            return self._select(alias)

    def by_id(self, proxy_id: Any) -> Optional[dict[str, Any]]:
        if not proxy_id:
            return None
        with self.lock:
            config = self.configs.get(str(proxy_id))
            return dict(config) if isinstance(config, dict) else None

    def for_account(self, alias: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        self.refresh_if_needed()
        if not self.subscription_url and not self.uses_mihomo:
            return None
        with self.lock:
            row = self.store.row(alias)
            current_id = str(row["proxy_id"] or "")
            if current_id:
                proxy_row = next((item for item in self.store.proxy_rows(active_only=True)
                                  if str(item["proxy_id"]) == current_id and int(item["failure_count"]) == 0), None)
                if proxy_row is not None:
                    config = self._config_for_row(proxy_row)
                    if config:
                        return config
            config = self._select(alias)
            self.store.set_account_proxy(alias, str(config["id"]))
            return config

    def rotate(self, alias: str, failed: dict[str, Any], reason: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        with self.lock:
            self.store.mark_proxy_failure(str(failed["id"]), reason)
            if not self.subscription_url and not self.uses_mihomo:
                return None
            self.store.set_account_proxy(alias, None)
            config = self._select(alias, exclude=str(failed["id"]))
            self.store.set_account_proxy(alias, str(config["id"]))
            return config

    def success(self, proxy: Optional[dict[str, Any]]) -> None:
        if proxy:
            self.store.mark_proxy_success(str(proxy["id"]))

    def activate(self, proxy: Optional[dict[str, Any]]) -> None:
        if not proxy or not self.uses_mihomo:
            return
        node_name = str(proxy.get("mihomo_node", "")).strip()
        if not node_name:
            raise RelayError("Mihomo proxy node name is missing", 500)
        mihomo_json("PUT", "/proxies/" + mihomo_path_component(MIHOMO_SELECTOR),
                    {"name": node_name}, timeout=min(self.timeout, PROXY_FETCH_TIMEOUT))

    def account_public(self, alias: str) -> Optional[dict[str, Any]]:
        row = self.store.row(alias)
        proxy_id = str(row["proxy_id"] or "")
        if not proxy_id:
            return None
        proxy_row = next((item for item in self.store.proxy_rows() if str(item["proxy_id"]) == proxy_id), None)
        config = self.configs.get(proxy_id, {})
        if not proxy_row and not config:
            return {"id": proxy_id, "active": False}
        return {"id": proxy_id, "name": str(config.get("name", proxy_row["name"] if proxy_row else proxy_id)),
                "scheme": str(config.get("scheme", proxy_row["scheme"] if proxy_row else "")),
                "host": str(config.get("host", proxy_row["host"] if proxy_row else "")),
                "port": int(config.get("port", proxy_row["port"] if proxy_row else 0)),
                "active": bool(proxy_row["active"]) and int(proxy_row["failure_count"]) == 0 if proxy_row else False,
                "failure_count": int(proxy_row["failure_count"]) if proxy_row else 0,
                "last_error": proxy_row["last_error"] if proxy_row else None}

    def public_summary(self) -> dict[str, Any]:
        rows = self.store.proxy_rows()
        counts = self.store.proxy_assignment_counts()
        return {"configured": bool(self.subscription_url) or self.uses_mihomo,
                "backend": "mihomo" if self.uses_mihomo else "direct",
                "active": sum(bool(row["active"]) and int(row["failure_count"]) == 0 for row in rows),
                "total": len(rows), "assigned": sum(counts.values()),
                "last_refresh": datetime_module.datetime.fromtimestamp(self.last_refresh,
                    datetime_module.timezone.utc).isoformat() if self.last_refresh else None,
                "last_error": self.last_error or None, "skipped_nodes": self.skipped_nodes,
                "nodes": [{"id": str(row["proxy_id"]), "name": str(row["name"]),
                            "scheme": str(row["scheme"]), "host": str(row["host"]),
                            "port": int(row["port"]),
                            "active": bool(row["active"]) and int(row["failure_count"]) == 0,
                            "assigned": counts.get(str(row["proxy_id"]), 0),
                            "failure_count": int(row["failure_count"]),
                            "last_error": row["last_error"]} for row in rows]}


def upstream_json(method: str, base: str, path: str, payload: Optional[dict[str, Any]] = None,
                  access: Optional[str] = None, timeout: float = 30.0,
                  proxy: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, str], Any]:
    body = None if payload is None else dump_json(payload)
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "mirofish-local-relay/1.0"}
    if access:
        headers["Authorization"] = "Bearer " + access
    request = urllib.request.Request(base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with open_url(request, timeout, proxy) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, parse_json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, parse_json(exc.read())
    except urllib.error.URLError as exc:
        raise RelayError("upstream network error", 502,
                         {"proxy_network": bool(proxy), "reason": str(exc.reason)[:200]})


def refresh(store: Store, alias: str, refresh_token: str, timeout: float,
            proxy: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    status, _, data = upstream_json("POST", AUTH_BASE, "/auth/refresh",
                                    {"refresh_token": refresh_token}, timeout=timeout, proxy=proxy)
    if status < 200 or status >= 300 or not isinstance(data, dict):
        raise RelayError("account refresh failed", 401, data)
    access = token_value(data, "access_token")
    renewal = token_value(data, "refresh_token")
    store.keychain.put(alias, "access", access)
    store.keychain.put(alias, "refresh", renewal)
    return access, renewal


def quota_headers(headers: dict[str, str]) -> dict[str, Any]:
    return {"7d_utilization": headers.get("anthropic-ratelimit-unified-7d-utilization"),
            "7d_reset_epoch": headers.get("anthropic-ratelimit-unified-7d-reset")}


def public_status(row: sqlite3.Row, metadata: Optional[dict[str, Any]] = None,
                  proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    metadata = metadata or json.loads(row["metadata_json"])
    return {"alias": row["alias"], "email": row["email"], "user_id": row["user_id"],
            "plan": row["plan"], "tenant": row["tenant"], "referral": metadata.get("referral", {}),
            "quota": metadata.get("quota", {}), "last_usage": metadata.get("last_usage", {}),
            "checked_at": metadata.get("checked_at"), "token_balance": None,
            "proxy": proxy,
            "token_balance_note": "没有发现精确余额接口；quota 是 relay 返回的 7-day utilization，last_usage 是最近一次响应的 token 用量。"}


def fetch_status(store: Store, alias: str, timeout: float, probe: bool = False,
                 proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    row = store.row(alias)
    access, renewal = store.credentials(alias)
    status, _, me = upstream_json("GET", AUTH_BASE, "/auth/me", access=access, timeout=timeout, proxy=proxy)
    if status == 401:
        access, renewal = refresh(store, alias, renewal, timeout, proxy=proxy)
        status, _, me = upstream_json("GET", AUTH_BASE, "/auth/me", access=access, timeout=timeout, proxy=proxy)
    if status < 200 or status >= 300:
        raise RelayError("account identity check failed", status, me)
    ref_status, _, referral = upstream_json("GET", AUTH_BASE, "/auth/referral", access=access, timeout=timeout, proxy=proxy)
    ten_status, _, tenant = upstream_json("GET", RELAY_BASE, "/me/tenant", access=access, timeout=timeout, proxy=proxy)
    if ref_status < 200 or ref_status >= 300 or ten_status < 200 or ten_status >= 300:
        raise RelayError("account status check failed", 502)
    metadata = {"user_id": me.get("id"), "email": me.get("email", row["email"]),
                "plan": referral.get("current_plan"), "tenant": tenant.get("tenant"),
                "referral": referral, "tenant_response": tenant,
                "quota": json.loads(row["metadata_json"]).get("quota", {}),
                "last_usage": json.loads(row["metadata_json"]).get("last_usage", {}),
                "checked_at": utc_now()}
    if probe:
        result, headers = model_request(store, alias, {"model": DEFAULT_MODEL, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}, timeout, proxy=proxy)
        metadata["last_usage"] = result.get("usage", {}) if isinstance(result, dict) else {}
        metadata["quota"] = quota_headers(headers)
    store.update_metadata(alias, metadata)
    return public_status(store.row(alias), metadata)


def model_request(store: Store, alias: str, payload: dict[str, Any], timeout: float,
                  proxy: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, str]]:
    access, renewal = store.credentials(alias)
    headers = {"Authorization": "Bearer " + access, "anthropic-version": ANTHROPIC_VERSION,
               "Content-Type": "application/json", "Accept-Encoding": "identity",
               "x-mirasim-probe": "usage"}
    request = urllib.request.Request(RELAY_BASE.rstrip("/") + "/v1/messages", data=dump_json(payload),
                                     headers=headers, method="POST")
    try:
        with open_url(request, timeout, proxy) as response:
            return parse_json(response.read()), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = parse_json(exc.read())
        if exc.code != 401:
            raise RelayError("model request rejected", exc.code, body)
        access, _ = refresh(store, alias, renewal, timeout, proxy=proxy)
        headers["Authorization"] = "Bearer " + access
        retry = urllib.request.Request(RELAY_BASE.rstrip("/") + "/v1/messages", data=dump_json(payload),
                                        headers=headers, method="POST")
        try:
            with open_url(retry, timeout, proxy) as response:
                return parse_json(response.read()), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as retry_error:
            raise RelayError("model request failed after token refresh", retry_error.code, parse_json(retry_error.read()))
        except urllib.error.URLError as retry_error:
            raise RelayError("relay network error", 502,
                             {"proxy_network": bool(proxy), "reason": str(retry_error.reason)[:200]})
    except urllib.error.URLError as exc:
        raise RelayError("relay network error", 502,
                         {"proxy_network": bool(proxy), "reason": str(exc.reason)[:200]})


MODEL_CATALOG_TTL = 300  # seconds for the local /v1/models cache

SCAN_CANDIDATES = [
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5",
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-haiku-20241022",
]


def probe_model_list(store: Store, alias: str, timeout: float,
                     proxy: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Query the upstream LiteLLM /v1/models with the account token (zero cost)."""
    access, renewal = store.credentials(alias)
    status, headers, data = upstream_json("GET", RELAY_BASE, "/v1/models", access=access, timeout=timeout, proxy=proxy)
    if status == 401:
        access, _ = refresh(store, alias, renewal, timeout, proxy=proxy)
        status, headers, data = upstream_json("GET", RELAY_BASE, "/v1/models", access=access, timeout=timeout, proxy=proxy)
    body = data if isinstance(data, dict) else {"raw": data}
    return status, body, headers


def public_model_list(status: int, data: dict[str, Any]) -> dict[str, Any]:
    """Normalize the upstream /v1/models response into a summary that is
    compatible with BOTH sub2api-style parsers (data[].id/type/display_name/
    created_at), OpenAI clients (data[].object), and our own CLI/WebUI
    (models/count)."""
    if status < 200 or status >= 300:
        return {"ok": False, "status": status, "data": [], "error": data}
    ids: list[str] = []
    for entry in data.get("data") if isinstance(data.get("data"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.append(entry["id"])
    ids = sorted(ids)
    data_rows = [
        {"id": mid, "object": "model", "type": "model",
         "display_name": mid, "created_at": "2024-01-01T00:00:00Z",
         "created": 0, "owned_by": "mirofish"}
        for mid in ids
    ]
    return {"object": "list", "data": data_rows, "ok": True, "status": status,
            "models": ids, "count": len(ids),
            "note": "来自上游 /v1/models；若为空说明该接口未输出模型或账号被隐藏。"}


def fetch_account_models(store: Store, alias: str, timeout: float,
                         proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fetch the upstream model catalog for one account (used by local /v1/models)."""
    status, body, _headers = probe_model_list(store, alias, timeout, proxy=proxy)
    return public_model_list(status, body)


def openai_content_text(content: Any) -> str:
    """Flatten OpenAI message content (string or list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" or "text" in item:
                    parts.append(alias_value(str(item.get("text", ""))))
                elif item_type == "image_url":
                    parts.append("[image]")
                else:
                    parts.append("")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def anthropize_openai_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI /v1/chat/completions body into Anthropic Messages shape."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for raw in payload.get("messages") if isinstance(payload.get("messages"), list) else []:
        if not isinstance(raw, dict):
            continue
        role = alias_value(str(raw.get("role", "user")))
        if role == "system":
            system_parts.append(openai_content_text(raw.get("content")))
            continue
        messages.append({
            "role": role if role in ("user", "assistant") else "user",
            "content": openai_content_text(raw.get("content")),
        })
    out: dict[str, Any] = {
        "model": alias_value(str(payload.get("model"))),
        "max_tokens": int(payload.get("max_tokens") or 4096),
        "messages": messages,
    }
    if system_parts:
        out["system"] = system_parts[0] if len(system_parts) == 1 else "\n\n".join(system_parts)
    return out


def openai_from_anthropic_response(resp: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate an Anthropic Messages response into an OpenAI chat.completion."""
    finish = {"end_turn": "stop", "stop_sequence": "stop",
              "max_tokens": "length", "tool_use": "tool_calls"}.get(resp.get("stop_reason"), "stop")
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    content_blocks = resp.get("content") if isinstance(resp.get("content"), list) else []
    text = "".join(block.get("text", "")
                   for block in content_blocks
                   if isinstance(block, dict) and block.get("type") == "text")
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion},
    }


def openai_stream_chunks(openai_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a single chat.completion into SSE chunk deltas (content + finish)."""
    text = openai_response["choices"][0]["message"]["content"]
    finish = openai_response["choices"][0]["finish_reason"]
    head = dict(openai_response)
    head["object"] = "chat.completion.chunk"
    head.pop("choices", None)
    chunks: list[dict[str, Any]] = []
    if text:
        chunks.append({**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                            "finish_reason": None}]})
        chunks.append({**head, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
    chunks.append({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return chunks


def scan_model_probe(store: Store, alias: str, timeout: float,
                     max_models: int = 0, proxy: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Try a short curated candidate list via max_tokens=1 probes. Each accepted
    probe costs ~1 output token on the account itself; list stays small."""
    candidates = SCAN_CANDIDATES[:max_models] if max_models else SCAN_CANDIDATES
    results: list[dict[str, Any]] = []
    for model in candidates:
        payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
        try:
            model_request(store, alias, payload, timeout, proxy=proxy)
            results.append({"model": model, "accepted": True})
        except RelayError as exc:
            results.append({"model": model, "accepted": False, "status": exc.status})
    return results


def start_login(alias: str, email: str, timeout: float,
                proxy: Optional[dict[str, Any]] = None) -> None:
    """Send the email verification code for a new account login."""
    alias = alias_value(alias)
    email = email_value(email)
    status, _, sent = upstream_json("POST", AUTH_BASE, "/auth/code", {"email": email}, timeout=timeout, proxy=proxy)
    if status < 200 or status >= 300 or not isinstance(sent, dict) or sent.get("sent") is not True:
        raise RelayError("verification code was not accepted", status, sent)


def finish_login(store: Store, alias: str, email: str, code: str, timeout: float,
                 proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Verify the code, fetch account state, and persist the credentials."""
    alias = alias_value(alias)
    email = email_value(email)
    if not re.fullmatch(r"\d{6}", code):
        raise RelayError("verification code must be 6 digits", 400)
    status, _, auth = upstream_json("POST", AUTH_BASE, "/auth/verify",
                                    {"email": email, "code": code}, timeout=timeout, proxy=proxy)
    if status < 200 or status >= 300 or not isinstance(auth, dict):
        raise RelayError("login failed", status, auth)
    access = token_value(auth, "access_token")
    renewal = token_value(auth, "refresh_token")
    s1, _, me = upstream_json("GET", AUTH_BASE, "/auth/me", access=access, timeout=timeout, proxy=proxy)
    s2, _, referral = upstream_json("GET", AUTH_BASE, "/auth/referral", access=access, timeout=timeout, proxy=proxy)
    s3, _, tenant = upstream_json("GET", RELAY_BASE, "/me/tenant", access=access, timeout=timeout, proxy=proxy)
    if any(s < 200 or s >= 300 for s in (s1, s2, s3)):
        raise RelayError("could not verify account state", 502)
    metadata = {"user_id": me.get("id"), "email": me.get("email", email),
                "plan": referral.get("current_plan"), "tenant": tenant.get("tenant"),
                "referral": referral, "tenant_response": tenant, "quota": {},
                "last_usage": {}, "checked_at": utc_now()}
    store.save(alias, email, access, renewal, metadata,
               proxy_id=str(proxy["id"]) if proxy and proxy.get("id") else None)
    return public_status(store.row(alias), metadata)


def login_account(store: Store, alias: str, email: str, timeout: float,
                  proxy_pool: Optional[ProxyPool] = None) -> None:
    pending_proxy = proxy_pool.pending_proxy(alias) if proxy_pool else None
    start_login(alias, email, timeout, proxy=pending_proxy)
    print("验证码已发送。")
    code = getpass.getpass("输入 6 位验证码（不会回显）：").strip()
    result = finish_login(store, alias, email, code, timeout, proxy=pending_proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))


WEBUI_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mirofish Relay 管理</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--ok:#4ade80;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,'PingFang SC','Helvetica Neue',sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px 24px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #334155}
header h1{font-size:18px;margin:0}
.badge{font-size:12px;color:var(--muted);border:1px solid #334155;border-radius:999px;padding:2px 10px}
main{max-width:1100px;margin:24px auto;padding:0 16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid #334155;border-radius:12px;padding:16px}
.card h2{font-size:15px;margin:0 0 12px}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px}
input,textarea,select{width:100%;background:#0b1220;border:1px solid #334155;border-radius:8px;color:var(--fg);padding:8px 10px;font-size:14px}
textarea{font-family:ui-monospace,Menlo,monospace;min-height:120px}
button{background:var(--accent);border:none;color:#082f49;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer;margin-top:10px}
button.ghost{background:transparent;border:1px solid #475569;color:var(--fg)}
button.danger{background:var(--bad);color:#450a0a}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.row>div{flex:1;min-width:160px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #334155;vertical-align:top}
th{color:var(--muted);font-weight:600}
.plan{color:var(--ok);font-weight:600}.muted{color:var(--muted);font-size:12px}
#msg{font-size:13px;margin-top:8px;min-height:18px}#msg.ok{color:var(--ok)}#msg.err{color:var(--bad)}
pre{background:#0b1220;border:1px solid #334155;border-radius:8px;padding:10px;font-size:12px;overflow:auto;white-space:pre-wrap}
.hidden{display:none}
</style>
</head>
<body>
<header><h1>Mirofish Relay 管理</h1><span class="badge" id="backend"></span><span class="badge" id="endpoint"></span></header>
<main>
<div class="card" id="keyCard">
<h2>本地代理密钥</h2>
<label>Proxy Key（启动时生成，保存在数据目录 proxy.key）</label>
<div class="row"><div><input id="key" type="password" placeholder="X-Mirofish-Proxy-Key"></div>
<div style="flex:0"><button onclick="saveKey()">保存</button></div></div>
<div class="muted">密钥仅保存在浏览器 localStorage，用于调用本机管理 API。</div>
</div>
<div class="card hidden" id="proxyCard">
<h2>代理池</h2>
<label>订阅链接（只写入服务端加密凭证存储，不会回显）</label>
<div class="row"><div><input id="proxySub" type="url" placeholder="https://.../sub?..."></div>
<div style="flex:0"><button onclick="saveProxySubscription()">保存并刷新</button>
<button class="ghost" onclick="refreshProxies()">刷新池</button></div></div>
<div class="muted">每个账号会固定绑定一个节点；节点网络失败时才会自动换到下一个节点。当前内置支持 HTTP(S) / SOCKS5，其他 Mihomo 加密协议会跳过。</div>
<pre id="proxyInfo">尚未读取代理池状态</pre>
</div>
<div class="card hidden" id="addCard">
<h2>添加账号（邮箱验证码登录）</h2>
<div class="row">
<div><label>别名 alias</label><input id="alias" placeholder="work"></div>
<div><label>邮箱 email</label><input id="email" placeholder="you@example.com"></div>
<div style="flex:0"><button onclick="sendCode()">发送验证码</button></div>
</div>
<div class="row hidden" id="verifyRow">
<div><label>6 位验证码</label><input id="code" maxlength="6" placeholder="123456"></div>
<div style="flex:0"><button onclick="verifyCode()">完成登录</button></div>
</div>
<div id="msg"></div>
</div>
<div class="card hidden" id="listCard">
<h2>账号列表 <button class="ghost" style="margin:0 0 0 8px;padding:2px 10px;font-size:12px" onclick="loadAccounts()">刷新</button></h2>
<table><thead><tr><th>别名</th><th>邮箱</th><th>Plan</th><th>租户</th><th>代理</th><th>最近用量</th><th>7天配额</th><th>操作</th></tr></thead><tbody id="rows"></tbody></table>
</div>
<div class="card hidden" id="testCard">
<h2>测试调用</h2>
<label>账号别名（留空用默认账号）</label><input id="testAccount" placeholder="work">
<label>提示词</label><textarea id="prompt">你好，请用一句话介绍你自己</textarea>
<div class="row"><div><label>max_tokens</label><input id="maxTokens" type="number" value="64"></div>
<div style="flex:0"><button onclick="callModel()">发送</button></div></div>
<pre id="result" class="hidden"></pre>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
function key(){return localStorage.getItem('mf_proxy_key')||''}
function saveKey(){localStorage.setItem('mf_proxy_key',$('key').value.trim());init()}
function headers(){return {'Content-Type':'application/json','X-Mirofish-Proxy-Key':key()}}
function msg(t,ok){$('msg').textContent=t;$('msg').className=ok?'ok':'err'}
async function api(path,opts){const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error((d.error&&d.error.message)||('HTTP '+r.status));return d}
async function init(){
 $('endpoint').textContent=location.origin;
 if(!key())return;
 try{const h=await api('/health',{headers:headers()});
  ['proxyCard','addCard','listCard','testCard'].forEach(id=>$(id).classList.remove('hidden'));
  $('backend').textContent='accounts: '+h.accounts;await loadProxy();await loadAccounts();}
 catch(e){$('backend').textContent='密钥无效';}
}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadProxy(){try{const d=await api('/proxies',{headers:headers()});
 $('proxyInfo').textContent=JSON.stringify({configured:d.configured,active:d.active,total:d.total,assigned:d.assigned,
  last_refresh:d.last_refresh,last_error:d.last_error,skipped_nodes:d.skipped_nodes,
  nodes:(d.nodes||[]).map(p=>({name:p.name,endpoint:p.scheme+'://'+p.host+':'+p.port,active:p.active,assigned:p.assigned,failure_count:p.failure_count}))},null,2);}
 catch(e){$('proxyInfo').textContent='读取代理池失败：'+e.message}}
async function saveProxySubscription(){try{const d=await api('/api/proxies/subscription',{method:'POST',headers:headers(),body:JSON.stringify({url:$('proxySub').value.trim()})});
 $('proxySub').value='';$('proxyInfo').textContent=JSON.stringify(d,null,2);await loadAccounts();}
 catch(e){$('proxyInfo').textContent='保存代理池失败：'+e.message}}
async function refreshProxies(){try{const d=await api('/api/proxies/refresh',{method:'POST',headers:headers()});$('proxyInfo').textContent=JSON.stringify(d,null,2);await loadAccounts();}
 catch(e){$('proxyInfo').textContent='刷新代理池失败：'+e.message}}
async function sendCode(){msg('');
 try{await api('/api/login/start',{method:'POST',headers:headers(),body:JSON.stringify({alias:$('alias').value,email:$('email').value})});
  $('verifyRow').classList.remove('hidden');msg('验证码已发送，请查收邮箱。',true);}
 catch(e){msg(e.message,false)}}
async function verifyCode(){
 try{const d=await api('/api/login/finish',{method:'POST',headers:headers(),body:JSON.stringify({alias:$('alias').value,email:$('email').value,code:$('code').value})});
  msg('登录成功：'+d.alias+' / plan='+(d.plan||'?'),true);$('code').value='';await loadAccounts();}
 catch(e){msg(e.message,false)}}
async function loadAccounts(){
 const d=await api('/accounts',{headers:headers()});const tb=$('rows');tb.innerHTML='';
 for(const a of d.accounts){const u=a.last_usage||{},q=a.quota||{};
  const p=a.proxy||{};const proxyText=p.name?(esc(p.name)+'<br><span class="muted">'+esc(p.host)+':'+esc(p.port)+(p.active?'':'（不可用）')+'</span>'):'-';
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+a.alias+'</td><td>'+a.email+'</td><td class="plan">'+(a.plan||'-')+'</td><td>'+(a.tenant||'-')+
   '</td><td>'+proxyText+'</td><td class="muted">in '+(u.input_tokens??'-')+' / out '+(u.output_tokens??'-')+'</td><td class="muted">'+
   (q['7d_utilization']?Number(q['7d_utilization']).toFixed(4):'-')+'</td>'+
   '<td><button class="ghost" style="padding:2px 10px;font-size:12px" onclick="refreshStatus(\\''+a.alias+'\\')">刷新</button> '+
   '<button class="danger" style="padding:2px 10px;font-size:12px" onclick="delAccount(\\''+a.alias+'\\')">删除</button></td>';
  tb.appendChild(tr);}}
async function refreshStatus(a){try{await api('/accounts/'+a+'/status',{headers:headers()});await loadAccounts();}catch(e){alert(e.message)}}
async function delAccount(a){if(!confirm('删除账号 '+a+' 的本地凭证？（不注销远端账号）'))return;
 try{await api('/api/accounts/'+a,{method:'DELETE',headers:headers()});await loadAccounts();}catch(e){alert(e.message)}}
async function callModel(){const r=$('result');r.classList.remove('hidden');r.textContent='请求中...';
 try{const d=await api('/v1/messages',{method:'POST',headers:Object.assign(headers(),{'X-Mirofish-Account':$('testAccount').value.trim()}),
  body:JSON.stringify({model:'claude-haiku-4-5-20251001',max_tokens:Number($('maxTokens').value)||64,
   messages:[{role:'user',content:$('prompt').value}]})});
  const text=(d.content||[]).map(b=>b.text||'').join('');r.textContent=text||JSON.stringify(d,null,2);await loadAccounts();}
 catch(e){r.textContent='错误：'+e.message}}
init();
</script>
</body>
</html>"""


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "MirofishLocalRelay/1.0"

    @property
    def app(self) -> "RelayServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[relay] " + (fmt % args) + "\n")

    def authorized(self) -> bool:
        # Accept the custom header, X-Api-Key, and Authorization: Bearer <key> so
        # standard OpenAI/Anthropic clients (and tools like sub2pai that only send
        # Authorization) can authenticate without a custom header.
        supplied = (
            self.headers.get("X-Mirofish-Proxy-Key", "")
            or self.headers.get("X-Api-Key", "")
            or (self.headers.get("Authorization", "") or "").removeprefix("Bearer ").removeprefix("bearer ").strip()
        )
        return bool(supplied) and secrets.compare_digest(supplied, self.app.proxy_key)

    def send_json(self, status: int, value: Any, headers: Optional[dict[str, str]] = None) -> None:
        body = dump_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_internal_error(self) -> None:
        """Return a valid response even if an unexpected handler error occurs."""
        try:
            self.send_json(500, {"error": {"type": "internal_error",
                                            "message": "relay request failed; check container logs"}})
        except (BrokenPipeError, ConnectionResetError):
            # The caller may already have timed out while the failing operation
            # was in progress. There is no useful response left to send.
            pass

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY_BYTES:
            raise RelayError("request body too large", 413)
        value = parse_json(self.rfile.read(length))
        if not isinstance(value, dict):
            raise RelayError("request body must be a JSON object", 400)
        return value

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in ("/", "/index.html"):
                body = WEBUI_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self.authorized():
                self.send_json(401, {"error": {"type": "unauthorized", "message": "invalid local proxy key"}})
                return
            if parsed.path == "/health":
                self.send_json(200, {"ok": True, "accounts": len(self.app.store.aliases())})
            elif parsed.path == "/accounts":
                self.send_json(200, {"accounts": [public_status(self.app.store.row(a),
                    proxy=self.app.proxy_pool.account_public(a)) for a in self.app.store.aliases()]})
            elif parsed.path == "/proxies":
                self.send_json(200, self.app.proxy_pool.public_summary())
            elif parsed.path == "/v1/models":
                account = self.headers.get("X-Mirofish-Account", "").strip() or self.app.default_account
                if not account:
                    aliases = self.app.store.aliases()
                    account = aliases[0] if aliases else ""
                if not account:
                    raise RelayError("no account; add one or pass X-Mirofish-Account", 400)
                cached = self.app.model_cache.get(account)
                if cached and time.time() - cached[0] < MODEL_CATALOG_TTL:
                    payload = cached[1]
                else:
                    payload = self.app.call_with_proxy(
                        account, lambda proxy: fetch_account_models(self.app.store, account,
                                                                    self.app.timeout, proxy=proxy))
                    self.app.model_cache[account] = (time.time(), payload)
                self.send_json(200, payload)
            else:
                match = re.fullmatch(r"/accounts/([^/]+)/status", parsed.path)
                if not match:
                    self.send_json(404, {"error": {"type": "not_found", "message": "unknown endpoint"}})
                    return
                query = urllib.parse.parse_qs(parsed.query)
                probe = query.get("probe", ["0"])[0] == "1"
                account = match.group(1)
                status = self.app.call_with_proxy(
                    account, lambda proxy: fetch_status(self.app.store, account, self.app.timeout,
                                                        probe, proxy=proxy))
                status["proxy"] = self.app.proxy_pool.account_public(account)
                self.send_json(200, status)
        except RelayError as exc:
            self.send_json(exc.status, {"error": {"type": "relay_error", "message": str(exc), "data": exc.data}})
        except Exception as exc:
            self.log_error("unexpected GET error: %s", type(exc).__name__)
            self._send_internal_error()

    def do_POST(self) -> None:
        try:
            if not self.authorized():
                self.send_json(401, {"error": {"type": "unauthorized", "message": "invalid local proxy key"}})
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/login/start":
                payload = self.read_body()
                alias = alias_value(str(payload.get("alias", "")))
                email = email_value(str(payload.get("email", "")))
                pending_proxy, _ = self.app.call_with_pending_proxy(
                    alias, lambda proxy: start_login(alias, email, self.app.timeout, proxy=proxy))
                self.app.pending_logins[alias] = {"email": email, "created": time.time(),
                                                 "proxy_id": pending_proxy.get("id") if pending_proxy else None}
                self.send_json(200, {"sent": True, "alias": alias})
                return
            if path == "/api/login/finish":
                payload = self.read_body()
                alias = alias_value(str(payload.get("alias", "")))
                pending = self.app.pending_logins.get(alias)
                if not pending:
                    raise RelayError("no pending login for this alias; send a code first", 400)
                if time.time() - pending["created"] > 600:
                    self.app.pending_logins.pop(alias, None)
                    raise RelayError("login session expired; send a new code", 400)
                pending_proxy = self.app.proxy_pool.by_id(pending.get("proxy_id"))
                result = self.app.call_with_fixed_proxy(
                    pending_proxy, lambda proxy: finish_login(self.app.store, alias, pending["email"],
                                                              str(payload.get("code", "")), self.app.timeout,
                                                              proxy=proxy))
                self.app.pending_logins.pop(alias, None)
                self.send_json(200, result)
                return
            if path == "/api/proxies/subscription":
                payload = self.read_body()
                summary = self.app.proxy_pool.set_subscription(str(payload.get("url", "")))
                self.send_json(200, summary)
                return
            if path == "/api/proxies/refresh":
                self.send_json(200, self.app.proxy_pool.refresh(force=True))
                return
            if path == "/v1/messages":
                account = self.app.pick_account(self.headers.get("X-Mirofish-Account", ""))
                payload = self.read_body()
                result, outgoing = self.app.call_with_proxy(
                    account, lambda proxy: self._relay_messages(account, payload, proxy))
                self.send_json(200, result, outgoing)
                return
            if path == "/v1/chat/completions":
                account = self.app.pick_account(self.headers.get("X-Mirofish-Account", ""))
                payload = self.read_body()
                anthropic_payload = anthropize_openai_payload(payload)
                result, outgoing = self.app.call_with_proxy(
                    account, lambda proxy: self._relay_messages(account, anthropic_payload, proxy))
                openai_response = openai_from_anthropic_response(
                    result, anthropic_payload.get("model") or payload.get("model") or DEFAULT_MODEL)
                if bool(payload.get("stream")):
                    self.send_sse(openai_stream_chunks(openai_response), outgoing)
                else:
                    self.send_json(200, openai_response, outgoing)
                return
            self.send_json(404, {"error": {"type": "not_found", "message": "unknown endpoint"}})
        except RelayError as exc:
            if isinstance(exc.data, dict) and exc.status >= 400:
                self.send_json(exc.status, exc.data)
            else:
                self.send_json(exc.status, {"error": {"type": "relay_error", "message": str(exc)}})
        except Exception as exc:
            self.log_error("unexpected POST error: %s", type(exc).__name__)
            self._send_internal_error()

    def _relay_messages(self, account: str, payload: dict[str, Any],
                        proxy: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Forward an Anthropic Messages payload to the chosen account's relay and
        record quota/usage metadata. Returns (upstream result, outgoing headers)."""
        result, response_headers = model_request(self.app.store, account, payload, self.app.timeout, proxy=proxy)
        row = self.app.store.row(account)
        metadata = json.loads(row["metadata_json"])
        metadata["last_usage"] = result.get("usage", {}) if isinstance(result, dict) else {}
        metadata["quota"] = quota_headers(response_headers)
        metadata["last_model"] = payload.get("model")
        self.app.store.update_metadata(account, metadata)
        outgoing = {"X-Mirofish-Account": alias_value(account)}
        if metadata["quota"].get("7d_utilization"):
            outgoing["X-Mirofish-Quota-7d-Utilization"] = str(metadata["quota"]["7d_utilization"])
        if metadata["quota"].get("7d_reset_epoch"):
            outgoing["X-Mirofish-Quota-7d-Reset"] = str(metadata["quota"]["7d_reset_epoch"])
        return result, outgoing

    def send_sse(self, chunks: list[dict[str, Any]], headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + dump_json(chunk) + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def do_DELETE(self) -> None:
        try:
            if not self.authorized():
                self.send_json(401, {"error": {"type": "unauthorized", "message": "invalid local proxy key"}})
                return
            path = urllib.parse.urlsplit(self.path).path
            match = re.fullmatch(r"/api/accounts/([^/]+)", path)
            if not match:
                self.send_json(404, {"error": {"type": "not_found", "message": "unknown endpoint"}})
                return
            self.app.store.remove(match.group(1))
            self.send_json(200, {"deleted": match.group(1)})
        except RelayError as exc:
            self.send_json(exc.status, {"error": {"type": "relay_error", "message": str(exc)}})
        except Exception as exc:
            self.log_error("unexpected DELETE error: %s", type(exc).__name__)
            self._send_internal_error()


class RelayServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: Store, proxy_key: str,
                 default_account: Optional[str], timeout: float) -> None:
        super().__init__(address, RelayHandler)
        self.store = store
        self.proxy_key = proxy_key
        self.default_account = default_account
        self.timeout = timeout
        self.proxy_pool = ProxyPool(store, timeout=timeout)
        self.pending_logins: dict[str, dict[str, Any]] = {}
        self.model_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._rr_index = 0
        self._rr_lock = __import__("threading").Lock()
        # Mihomo's selector is process-global. Keep the switch and the related
        # upstream call together so another account cannot change the exit node
        # in the middle of a request.
        self._mihomo_request_lock = threading.RLock()

    def call_with_proxy(self, account: str,
                        operation: Callable[[Optional[dict[str, Any]]], Any]) -> Any:
        """Run one account operation and rotate its sticky node on network failure."""
        lock = self._mihomo_request_lock if self.proxy_pool.uses_mihomo else None
        if lock:
            lock.acquire()
        try:
            proxy = self.proxy_pool.for_account(account)
            attempts = 1 if proxy is None else min(4, max(2, len(self.proxy_pool.public_summary()["nodes"])))
            for attempt in range(attempts):
                try:
                    self.proxy_pool.activate(proxy)
                    result = operation(proxy)
                    self.proxy_pool.success(proxy)
                    return result
                except RelayError as exc:
                    network_failure = (exc.status == 502 and isinstance(exc.data, dict)
                                       and exc.data.get("proxy_network") is True)
                    if not proxy or not network_failure or attempt + 1 >= attempts:
                        raise
                    proxy = self.proxy_pool.rotate(account, proxy, "proxy network failure")
            raise RelayError("proxy request failed", 502)
        finally:
            if lock:
                lock.release()

    def call_with_fixed_proxy(self, proxy: Optional[dict[str, Any]],
                              operation: Callable[[Optional[dict[str, Any]]], Any]) -> Any:
        lock = self._mihomo_request_lock if self.proxy_pool.uses_mihomo else None
        if lock:
            lock.acquire()
        try:
            self.proxy_pool.activate(proxy)
            return operation(proxy)
        finally:
            if lock:
                lock.release()

    def call_with_pending_proxy(self, alias: str,
                                operation: Callable[[Optional[dict[str, Any]]], Any]) -> tuple[Optional[dict[str, Any]], Any]:
        lock = self._mihomo_request_lock if self.proxy_pool.uses_mihomo else None
        if lock:
            lock.acquire()
        try:
            proxy = self.proxy_pool.pending_proxy(alias)
            self.proxy_pool.activate(proxy)
            return proxy, operation(proxy)
        finally:
            if lock:
                lock.release()

    def pick_account(self, requested: str) -> str:
        """Pick account: explicit header > default_account > round-robin across all."""
        if requested:
            return requested
        if self.default_account:
            return self.default_account
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        with self._rr_lock:
            account = aliases[self._rr_index % len(aliases)]
            self._rr_index += 1
        return account


def write_mihomo_config(output_path: pathlib.Path) -> None:
    """Write the small, private Mihomo config used by the Docker sidecar."""
    subscription = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL", "").strip()
    subscription_file = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_FILE", "").strip()
    if subscription and subscription_file:
        raise RelayError("configure either MIROFISH_PROXY_SUBSCRIPTION_URL or MIROFISH_PROXY_SUBSCRIPTION_FILE, not both", 500)
    if not subscription and not subscription_file:
        raise RelayError("MIROFISH_PROXY_SUBSCRIPTION_URL or MIROFISH_PROXY_SUBSCRIPTION_FILE is required for Mihomo", 500)
    if subscription:
        subscription = proxy_subscription_value(subscription)
    else:
        subscription_file = proxy_subscription_file_value(subscription_file)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    (output_path.parent / "providers").mkdir(parents=True, exist_ok=True)
    provider_file: Optional[pathlib.Path] = None
    if not subscription:
        source_path = pathlib.Path(subscription_file)
        try:
            if not source_path.is_file():
                raise RelayError("static proxy subscription file does not exist", 500)
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise RelayError("cannot read static proxy subscription file", 500) from exc
        if len(source_bytes) > PROXY_FETCH_MAX_BYTES:
            raise RelayError("static proxy subscription file is too large", 413)
        # Mihomo restricts file providers to its home/safe path. Copy the
        # read-only host bind mount into the named /config volume first.
        provider_file = output_path.parent / "subscription.yaml"
        provider_temp = provider_file.with_name(provider_file.name + ".tmp")
        fd = os.open(str(provider_temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(source_bytes)
            os.replace(provider_temp, provider_file)
            os.chmod(provider_file, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if fd != -1:
                os.close(fd)
    scalar = lambda value: json.dumps(value, ensure_ascii=False)
    content_lines = [
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: warning",
        "external-controller: 0.0.0.0:9090",
        "proxy-providers:",
        "  " + scalar(MIHOMO_PROVIDER) + ":",
    ]
    if subscription:
        content_lines += [
            "    type: http",
            "    url: " + scalar(subscription),
            "    path: ./providers/mirofish.yaml",
            "    interval: " + str(int(PROXY_REFRESH_SECONDS)),
            "    header:",
            "      User-Agent:",
            "        - " + scalar(PROXY_SUBSCRIPTION_USER_AGENT),
        ]
    else:
        content_lines += [
            "    type: file",
            "    path: " + scalar(str(provider_file)),
        ]
    content = "\n".join(content_lines + [
        "proxy-groups:",
        "  - name: " + scalar(MIHOMO_SELECTOR),
        "    type: select",
        "    use:",
        "      - " + scalar(MIHOMO_PROVIDER),
        "rules:",
        "  - MATCH," + MIHOMO_SELECTOR,
        "",
    ])
    temp_path = output_path.with_name(output_path.name + ".tmp")
    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
        os.replace(temp_path, output_path)
        os.chmod(output_path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if fd != -1:
            os.close(fd)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本机多账号 Mirofish Anthropic-compatible 中转")
    parser.add_argument("--data-dir", type=pathlib.Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="登录并持久化一个账号")
    add.add_argument("alias")
    add.add_argument("--email")
    commands.add_parser("list", help="列出本地账号状态")
    status = commands.add_parser("status", help="刷新账号套餐和配额状态")
    status.add_argument("alias")
    status.add_argument("--probe", action="store_true", help="发送一次 1-token 探测，会产生模型调用")
    models = commands.add_parser("models", help="探测 relay 支持的模型列表")
    models.add_argument("alias")
    models.add_argument("--scan", action="store_true",
                        help="额外用 1-token 探测候选模型名；会产生少量模型调用费用")
    models.add_argument("--max-scan", type=int, default=0,
                        help="--scan 时最多探测的候选模型数（默认全部）")
    remove = commands.add_parser("remove", help="删除本地账号及 Keychain 凭证")
    remove.add_argument("alias")
    mihomo = commands.add_parser("mihomo-config", help="生成 Docker Mihomo sidecar 配置")
    mihomo.add_argument("--output", type=pathlib.Path, required=True)
    serve = commands.add_parser("serve", help="启动仅监听 localhost 的中转")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--default-account",
                       default=os.environ.get("MIROFISH_DEFAULT_ACCOUNT"))
    serve.add_argument("--proxy-key")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        if args.command == "mihomo-config":
            write_mihomo_config(args.output)
            print("已生成 Mihomo 配置：" + str(args.output))
            return 0
        store = Store(args.data_dir)
        proxy_pool = ProxyPool(store, timeout=args.timeout)
        if args.command == "add":
            login_account(store, args.alias, args.email or input("邮箱："), args.timeout, proxy_pool)
        elif args.command == "list":
            print(json.dumps({"accounts": [public_status(store.row(a),
                proxy=proxy_pool.account_public(a)) for a in store.aliases()]}, ensure_ascii=False, indent=2))
        elif args.command == "status":
            proxy = proxy_pool.for_account(args.alias)
            result = fetch_status(store, args.alias, args.timeout, args.probe, proxy=proxy)
            result["proxy"] = proxy_pool.account_public(args.alias)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "models":
            proxy = proxy_pool.for_account(args.alias)
            status, _body, headers = probe_model_list(store, args.alias, args.timeout, proxy=proxy)
            result = public_model_list(status, _body)
            if args.scan:
                result["probe_scan"] = scan_model_probe(store, args.alias, args.timeout, args.max_scan, proxy=proxy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "remove":
            if input("确认删除本地账号和 Keychain 凭证？输入 DELETE：") == "DELETE":
                store.remove(args.alias)
                print("已删除本地账号：" + args.alias)
        elif args.command == "serve":
            if args.default_account:
                store.row(args.default_account)
            proxy_key = args.proxy_key or store.proxy_key()
            server = RelayServer((args.host, args.port), store, proxy_key, args.default_account, args.timeout)
            print("中转地址：http://{}:{}".format(args.host, args.port))
            print("本地代理密钥（仅显示一次）：" + proxy_key)
            print("账号选择头：X-Mirofish-Account")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
            finally:
                server.server_close()
        return 0
    except RelayError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
