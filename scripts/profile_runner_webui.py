#!/usr/bin/env python3
"""Local WebUI and scheduler for isolated GuJumpgate Chrome workers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_ASSET_DIR = PROJECT_ROOT / "runner"
MANIFEST_PATH = PROJECT_ROOT / "manifest.json"
DATA_DIR = PROJECT_ROOT / "data" / "profile-runner"
WEBUI_CONFIG_PATH = DATA_DIR / "webui-config.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT.parent / "sub2api"
DEFAULT_HELPER_BASE_URL = "http://127.0.0.1:17373"
SMSBOWER_API_URL = "https://smsbower.app/stubs/handler_api.php"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17374
DEFAULT_TIMEOUT_MINUTES = 25
MAX_CONCURRENCY = 10
TERMINAL_JOB_STATUSES = frozenset({"success", "failed", "stopped"})
SMSBOWER_COUNTRIES = (
    (33, "Colombia"),
    (151, "Chile"),
    (73, "Brazil"),
    (16, "United Kingdom"),
    (31, "South Africa"),
    (4, "Philippines"),
    (6, "Indonesia"),
    (187, "USA Physical"),
    (46, "Sweden"),
    (117, "Portugal"),
)
SMSBOWER_COUNTRY_IDS = frozenset(country_id for country_id, _ in SMSBOWER_COUNTRIES)
PERSISTED_SETTING_KEYS = frozenset({
    "chromePath", "cleanupProfiles", "concurrency", "helperBaseUrl", "outputDir",
    "phoneActivationRetryRounds", "phoneAutoReleaseOnStop", "phoneCodePollIntervalSeconds",
    "phoneCodePollMaxRounds", "phoneCodeTimeoutWindows", "phoneCodeWaitSeconds",
    "phoneReplacementLimit", "profileRoot", "proxyDefaultProtocol", "proxyPoolText", "smsBowerAcquirePriority",
    "smsBowerApiKey", "smsBowerCountryIds", "smsBowerMaxPrice", "smsBowerMinPrice",
    "smsBowerPreferredPrice", "timeoutMinutes", "verificationResendCount",
    "whatsappRestartEnabled", "whatsappRestartMaxAttempts",
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = fallback
    return max(minimum, min(maximum, normalized))


def normalize_sms_settings(payload: dict[str, Any]) -> dict[str, Any]:
    raw_country_ids = payload.get("smsBowerCountryIds")
    if raw_country_ids is None:
        country_ids = [country_id for country_id, _ in SMSBOWER_COUNTRIES]
    elif not isinstance(raw_country_ids, list):
        raise ValueError("SMSBower 国家优先级格式无效。")
    else:
        country_ids = []
        for value in raw_country_ids:
            try:
                country_id = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"SMSBower 国家 ID 无效：{value}") from None
            if country_id not in SMSBOWER_COUNTRY_IDS:
                raise ValueError(f"SMSBower 不支持国家 ID：{country_id}")
            if country_id not in country_ids:
                country_ids.append(country_id)
    if not country_ids:
        raise ValueError("SMSBower 国家优先级至少选择一个国家。")

    acquire_priority = str(payload.get("smsBowerAcquirePriority") or "country").strip().lower()
    if acquire_priority not in {"country", "price", "price_high"}:
        raise ValueError("SMSBower 拿号优先级无效。")

    def normalize_price(key: str, label: str, fallback: str = "") -> str:
        raw_value = payload[key] if key in payload else fallback
        raw = str(raw_value or "").strip()
        if not raw:
            return ""
        try:
            parsed = float(raw)
        except ValueError:
            raise ValueError(f"{label}必须是有效数字。") from None
        if not (parsed > 0 and parsed < float("inf")):
            raise ValueError(f"{label}必须大于 0。")
        return raw

    min_price = normalize_price("smsBowerMinPrice", "SMSBower 最低购买价（USD）", "0.03")
    max_price = normalize_price("smsBowerMaxPrice", "SMSBower 价格上限（USD）", "0.15")
    preferred_price = normalize_price("smsBowerPreferredPrice", "SMSBower 指定档位")
    if min_price and max_price and float(min_price) > float(max_price):
        raise ValueError("SMSBower 最低购买价不能高于价格上限。")

    def bounded_int(key: str, label: str, minimum: int, maximum: int, fallback: int) -> int:
        raw = payload.get(key, fallback)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}必须是整数。") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}必须在 {minimum}-{maximum} 之间。")
        return value

    return {
        "countryIds": country_ids,
        "acquirePriority": acquire_priority,
        "minPrice": min_price,
        "maxPrice": max_price,
        "preferredPrice": preferred_price,
        "verificationResendCount": bounded_int("verificationResendCount", "验证码重发次数", 0, 20, 0),
        "replacementLimit": bounded_int("phoneReplacementLimit", "换号上限", 1, 100, 3),
        "whatsappRestartEnabled": bool(payload.get("whatsappRestartEnabled", True)),
        "whatsappRestartMaxAttempts": bounded_int("whatsappRestartMaxAttempts", "WhatsApp 重试次数", 1, 20, 5),
        "codeWaitSeconds": bounded_int("phoneCodeWaitSeconds", "验证码限时", 15, 300, 45),
        "timeoutWindows": bounded_int("phoneCodeTimeoutWindows", "验证码超时轮数", 1, 10, 2),
        "pollIntervalSeconds": bounded_int("phoneCodePollIntervalSeconds", "验证码轮询间隔", 1, 30, 3),
        "pollMaxRounds": bounded_int("phoneCodePollMaxRounds", "验证码轮询次数", 1, 120, 4),
        "activationRetryRounds": bounded_int("phoneActivationRetryRounds", "取号重试轮数", 1, 10, 3),
        "autoReleaseOnStop": bool(payload.get("phoneAutoReleaseOnStop", True)),
    }


def is_invalid_mail_refresh_reason(reason: Any) -> bool:
    normalized = str(reason or "").lower()
    return "invalid_grant" in normalized or "refresh_token' or 'assertion' is not valid" in normalized


def fetch_smsbower_balance(api_key: Any, timeout: float = 12) -> str:
    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        raise ValueError("请先填写 SMSBower API Key。")
    query = urllib.parse.urlencode({"api_key": normalized_key, "action": "getBalance"})
    request = urllib.request.Request(
        f"{SMSBOWER_API_URL}?{query}",
        headers={"Accept": "text/plain", "User-Agent": "GuJumpgate-Profile-Runner/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024).decode("utf-8", errors="replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"SMSBower 余额查询请求失败：{exc}") from exc
    if not raw.startswith("ACCESS_BALANCE:"):
        safe_error = raw[:160] or "空响应"
        raise ValueError(f"SMSBower 余额查询失败：{safe_error}")
    balance = raw.split(":", 1)[1].strip()
    try:
        numeric = float(balance)
    except ValueError:
        raise ValueError("SMSBower 返回了无效余额。") from None
    if not (numeric >= 0 and numeric < float("inf")):
        raise ValueError("SMSBower 返回了无效余额。")
    return balance


def is_loopback_host(value: str) -> bool:
    return str(value or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def normalize_loopback_base_url(value: Any, fallback: str = DEFAULT_HELPER_BASE_URL) -> str:
    raw = str(value or fallback).strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme != "http" or not is_loopback_host(parsed.hostname or "") or not parsed.port:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("本地助手地址必须是带端口的 loopback HTTP 地址，例如 http://127.0.0.1:17373。")
    host = "127.0.0.1" if parsed.hostname in {"localhost", "127.0.0.1"} else "[::1]"
    return f"http://{host}:{parsed.port}"


def ensure_absolute_directory(value: Any, label: str, create: bool = True) -> Path:
    raw = str(value or "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        raise ValueError(f"{label}必须填写绝对路径。")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{label}不是目录：{path}")
    return path.resolve()


def verify_directory_writable(path: Path, label: str) -> None:
    probe = path / f".gujumpgate-write-test-{secrets.token_hex(6)}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"{label}不可写：{exc}") from exc


def compute_extension_id(manifest_path: Path = MANIFEST_PATH) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_key = str(manifest.get("key") or "").strip()
    if not manifest_key:
        raise RuntimeError("manifest.json 缺少固定 key，无法保证多 Profile 使用相同扩展 ID。")
    try:
        digest = hashlib.sha256(base64.b64decode(manifest_key)).digest()[:16]
    except Exception as exc:
        raise RuntimeError("manifest.json 的扩展 key 无效。") from exc
    return "".join(chr(97 + (byte >> 4)) + chr(97 + (byte & 15)) for byte in digest)


def detect_chrome_binary() -> Optional[Path]:
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])
    elif os.name == "nt":
        for root in [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]:
            if root:
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    else:
        for command in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
            found = shutil.which(command)
            if found:
                candidates.append(Path(found))
    return next((path.resolve() for path in candidates if path.is_file()), None)


@dataclass(frozen=True)
class ImportedAccount:
    email: str
    client_id: str
    refresh_token: str = field(repr=False)
    password: str = field(default="", repr=False)

    @property
    def account_id(self) -> str:
        digest = hashlib.sha256(self.email.lower().encode("utf-8")).hexdigest()[:20]
        return f"profile-runner-{digest}"

    def worker_payload(self) -> dict[str, Any]:
        return {
            "id": self.account_id,
            "email": self.email,
            "password": self.password,
            "clientId": self.client_id,
            "refreshToken": self.refresh_token,
        }


@dataclass
class AccountPoolEntry:
    account: ImportedAccount = field(repr=False)
    status: str = "pending"
    reason: str = ""
    output_file: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "email": self.account.email,
            "status": self.status,
            "reason": self.reason,
            "outputFile": self.output_file,
        }


def parse_account_text(raw_text: Any) -> tuple[list[ImportedAccount], list[str]]:
    accounts: list[ImportedAccount] = []
    errors: list[str] = []
    seen_emails: set[str] = set()
    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) == 3:
            email, client_id, refresh_token = parts
            password = ""
        elif len(parts) == 4:
            email, password, client_id, refresh_token = parts
        else:
            errors.append(f"第 {line_number} 行字段数错误，应为 3 段或兼容的 4 段格式。")
            continue
        email_key = email.lower()
        if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
            errors.append(f"第 {line_number} 行邮箱格式无效。")
            continue
        if not client_id:
            errors.append(f"第 {line_number} 行缺少 clientId。")
            continue
        if not refresh_token:
            errors.append(f"第 {line_number} 行缺少 mailRefreshToken。")
            continue
        if email_key in seen_emails:
            errors.append(f"第 {line_number} 行邮箱重复：{email}")
            continue
        seen_emails.add(email_key)
        accounts.append(ImportedAccount(
            email=email,
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
        ))
    if not accounts and not errors:
        errors.append("请先导入至少一个微软邮箱账号。")
    return accounts, errors


@dataclass(frozen=True)
class ProxyEntry:
    protocol: str
    host: str
    port: int
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    @property
    def proxy_id(self) -> str:
        signature = f"{self.protocol}|{self.host.lower()}|{self.port}|{self.username}|{self.password}"
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]

    @property
    def label(self) -> str:
        auth_hint = " / auth" if self.username else ""
        return f"{self.protocol}://{self.host}:{self.port}{auth_hint}"

    def worker_payload(self) -> dict[str, Any]:
        return {
            "id": self.proxy_id,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "label": self.label,
        }


class Socks5HttpBridge:
    """Local unauthenticated HTTP proxy that dials an upstream SOCKS5 proxy."""

    def __init__(self, upstream: ProxyEntry, on_error: Optional[Callable[[str], None]] = None):
        self.upstream = upstream
        self.on_error = on_error
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self.port = 0
        self.last_error = ""

    @property
    def proxy_entry(self) -> ProxyEntry:
        if not self.port:
            raise RuntimeError("SOCKS5 bridge has not started.")
        return ProxyEntry("http", "127.0.0.1", self.port)

    def start(self) -> None:
        if self._server_socket:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(64)
            server.settimeout(0.5)
        except OSError:
            server.close()
            raise
        self._server_socket = server
        self.port = int(server.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name=f"socks5-http-bridge-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        server = self._server_socket
        self._server_socket = None
        if server:
            try:
                server.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while not self._stopped.is_set():
            server = self._server_socket
            if not server:
                return
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(20)
            header = self._read_http_header(client)
            if not header:
                return
            lines = header.decode("iso-8859-1", errors="replace").split("\r\n")
            method, target, version = self._parse_request_line(lines[0])
            headers = self._parse_headers(lines[1:])
            host, port, path = self._resolve_http_target(method, target, headers)
            upstream = self._connect_via_socks5(host, port)
            if method.upper() == "CONNECT":
                client.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
            else:
                filtered = [
                    line for line in lines[1:]
                    if line and not line.lower().startswith(("proxy-connection:", "connection:"))
                ]
                rewritten = "\r\n".join([f"{method} {path} {version}", *filtered, "", ""]).encode("iso-8859-1")
                upstream.sendall(rewritten)
            self._relay(client, upstream)
        except Exception as exc:
            self._report_error(str(exc) or exc.__class__.__name__)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            for sock in (client, upstream):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _read_http_header(sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while total < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            data = b"".join(chunks)
            if b"\r\n\r\n" in data:
                return data.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        return b""

    def _report_error(self, message: str) -> None:
        safe_message = str(message or "unknown bridge error").replace(self.upstream.password, "***")
        safe_message = safe_message.replace(self.upstream.username, "***")
        self.last_error = safe_message[:500]
        if self.on_error:
            self.on_error(self.last_error)

    @staticmethod
    def _parse_request_line(line: str) -> tuple[str, str, str]:
        parts = line.split()
        if len(parts) != 3:
            raise ValueError("invalid proxy request line")
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _parse_headers(lines: list[str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in lines:
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers

    @staticmethod
    def _resolve_http_target(method: str, target: str, headers: dict[str, str]) -> tuple[str, int, str]:
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            return host or target, int(port_text or 443), target
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme and parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            return parsed.hostname, port, path
        host_header = headers.get("host", "")
        host, _, port_text = host_header.rpartition(":")
        host = host or host_header
        return host, int(port_text or 80), target or "/"

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise OSError("upstream SOCKS5 connection closed")
            data += chunk
        return data

    def _connect_via_socks5(self, target_host: str, target_port: int) -> socket.socket:
        upstream = socket.create_connection((self.upstream.host, self.upstream.port), timeout=20)
        upstream.settimeout(20)
        methods = b"\x00" + (b"\x02" if self.upstream.username else b"")
        upstream.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = self._recv_exact(upstream, 2)
        if version != 5:
            raise OSError("invalid SOCKS5 greeting")
        if method == 2:
            username = self.upstream.username.encode("utf-8")
            password = self.upstream.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise OSError("SOCKS5 credentials are too long")
            upstream.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth_version, auth_status = self._recv_exact(upstream, 2)
            if auth_version != 1 or auth_status != 0:
                raise OSError("SOCKS5 authentication failed")
        elif method != 0:
            raise OSError("SOCKS5 server rejected supported auth methods")

        host_bytes = target_host.encode("idna")
        if len(host_bytes) > 255:
            raise OSError("target host is too long")
        request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", int(target_port))
        upstream.sendall(request)
        head = self._recv_exact(upstream, 4)
        if head[0] != 5 or head[1] != 0:
            raise OSError(f"SOCKS5 connect failed: code={head[1] if len(head) > 1 else 'unknown'}")
        address_type = head[3]
        if address_type == 1:
            self._recv_exact(upstream, 4)
        elif address_type == 3:
            length = self._recv_exact(upstream, 1)[0]
            self._recv_exact(upstream, length)
        elif address_type == 4:
            self._recv_exact(upstream, 16)
        self._recv_exact(upstream, 2)
        upstream.settimeout(None)
        return upstream

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        for sock in sockets:
            sock.setblocking(False)
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional or not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                target = right if source is left else left
                try:
                    target.sendall(data)
                except OSError:
                    return


class HttpAuthBridge:
    """Local unauthenticated HTTP proxy that injects auth for an upstream HTTP proxy."""

    def __init__(self, upstream: ProxyEntry, on_error: Optional[Callable[[str], None]] = None):
        self.upstream = upstream
        self.on_error = on_error
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self.port = 0
        self.last_error = ""

    @property
    def proxy_entry(self) -> ProxyEntry:
        if not self.port:
            raise RuntimeError("HTTP auth bridge has not started.")
        return ProxyEntry("http", "127.0.0.1", self.port)

    def start(self) -> None:
        if self._server_socket:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(64)
            server.settimeout(0.5)
        except OSError:
            server.close()
            raise
        self._server_socket = server
        self.port = int(server.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name=f"http-auth-bridge-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        server = self._server_socket
        self._server_socket = None
        if server:
            try:
                server.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while not self._stopped.is_set():
            server = self._server_socket
            if not server:
                return
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(20)
            header = Socks5HttpBridge._read_http_header(client)
            if not header:
                return
            lines = header.decode("iso-8859-1", errors="replace").split("\r\n")
            method, target, version = Socks5HttpBridge._parse_request_line(lines[0])
            headers = Socks5HttpBridge._parse_headers(lines[1:])
            upstream = socket.create_connection((self.upstream.host, self.upstream.port), timeout=20)
            upstream.settimeout(20)
            auth_value = base64.b64encode(
                f"{self.upstream.username}:{self.upstream.password}".encode("utf-8")
            ).decode("ascii")
            filtered = [
                line for line in lines[1:]
                if line and not line.lower().startswith(("proxy-authorization:", "proxy-connection:"))
            ]
            if method.upper() == "CONNECT":
                request_lines = [
                    f"{method} {target} {version}",
                    *filtered,
                    f"Proxy-Authorization: Basic {auth_value}",
                    "Proxy-Connection: keep-alive",
                    "",
                    "",
                ]
                upstream.sendall("\r\n".join(request_lines).encode("iso-8859-1"))
                response = Socks5HttpBridge._read_http_header(upstream)
                status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
                if " 200 " not in f" {status_line} ":
                    raise OSError(f"upstream HTTP CONNECT failed: {status_line or 'empty response'}")
                client.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
            else:
                if "://" not in target:
                    host_header = headers.get("host", "")
                    target = f"http://{host_header}{target if target.startswith('/') else '/' + target}"
                request_lines = [
                    f"{method} {target} {version}",
                    *filtered,
                    f"Proxy-Authorization: Basic {auth_value}",
                    "",
                    "",
                ]
                upstream.sendall("\r\n".join(request_lines).encode("iso-8859-1"))
            Socks5HttpBridge._relay(client, upstream)
        except Exception as exc:
            self._report_error(str(exc) or exc.__class__.__name__)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            for sock in (client, upstream):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _report_error(self, message: str) -> None:
        safe_message = str(message or "unknown bridge error").replace(self.upstream.password, "***")
        safe_message = safe_message.replace(self.upstream.username, "***")
        self.last_error = safe_message[:500]
        if self.on_error:
            self.on_error(self.last_error)


def normalize_proxy_protocol(value: Any, fallback: str = "http") -> str:
    protocol = str(value or fallback).strip().lower()
    if protocol == "socks5h":
        protocol = "socks5"
    if protocol not in {"http", "https", "socks4", "socks5"}:
        raise ValueError(f"不支持的代理协议：{protocol}")
    return protocol


def parse_proxy_line(raw_line: str, default_protocol: str = "http") -> ProxyEntry:
    raw = str(raw_line or "").strip()
    if not raw:
        raise ValueError("代理行为空。")
    protocol = normalize_proxy_protocol(default_protocol)
    host = ""
    port_text = ""
    username = ""
    password = ""

    if "://" in raw or "@" in raw:
        url_value = raw if "://" in raw else f"{protocol}://{raw}"
        try:
            parsed = urllib.parse.urlsplit(url_value)
            protocol = str(parsed.scheme or protocol).lower()
            host = str(parsed.hostname or "").strip()
            port_text = str(parsed.port or "")
            username = urllib.parse.unquote(parsed.username or "")
            password = urllib.parse.unquote(parsed.password or "")
        except (ValueError, TypeError) as exc:
            raise ValueError("代理 URL 无法解析。") from exc
    else:
        parts = raw.split(":")
        if len(parts) == 2:
            host, port_text = parts
        elif len(parts) == 4:
            host, port_text, username, password = parts
        else:
            raise ValueError("代理格式应为 protocol://user:pass@host:port、host:port 或 host:port:user:pass。")

    protocol = normalize_proxy_protocol(protocol)
    host = host.strip().strip("[]")
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("代理端口无效。") from exc
    if not host or not 1 <= port <= 65535:
        raise ValueError("代理 host 或 port 无效。")
    return ProxyEntry(
        protocol=protocol,
        host=host,
        port=port,
        username=str(username or "").strip(),
        password=str(password or ""),
    )


def parse_proxy_text(raw_text: Any, default_protocol: str = "http") -> tuple[list[ProxyEntry], list[str]]:
    proxies: list[ProxyEntry] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = parse_proxy_line(line, default_protocol)
        except ValueError as exc:
            errors.append(f"代理第 {line_number} 行：{exc}")
            continue
        if proxy.proxy_id in seen_ids:
            errors.append(f"代理第 {line_number} 行与前面的代理配置重复。")
            continue
        seen_ids.add(proxy.proxy_id)
        proxies.append(proxy)
    return proxies, errors


def process_health_check(base_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        request = urllib.request.Request(f"{base_url.rstrip('/')}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and payload.get("ok"):
            return True, ""
        return False, "健康检查响应无效。"
    except Exception as exc:
        return False, str(exc)


class HelperManager:
    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen[Any]] = None
        self.log_handle: Optional[Any] = None
        self.base_url = DEFAULT_HELPER_BASE_URL
        self.last_error = ""
        self._lock = threading.RLock()

    def ensure(self, base_url: str) -> tuple[bool, str]:
        normalized = normalize_loopback_base_url(base_url)
        self.base_url = normalized
        healthy, reason = process_health_check(normalized)
        if healthy:
            self.last_error = ""
            return True, ""
        with self._lock:
            if self.process and self.process.poll() is None:
                self.last_error = reason
                return False, reason
            parsed = urllib.parse.urlparse(normalized)
            if not is_loopback_host(parsed.hostname or ""):
                self.last_error = "只会自动启动 loopback 地址上的 Hotmail helper。"
                return False, self.last_error
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            helper_script = PROJECT_ROOT / "scripts" / "hotmail_helper.py"
            helper_log = DATA_DIR / "hotmail-helper.log"
            self.log_handle = helper_log.open("ab", buffering=0)
            try:
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        str(helper_script),
                        "--host",
                        parsed.hostname or "127.0.0.1",
                        "--port",
                        str(parsed.port),
                    ],
                    cwd=str(PROJECT_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=self.log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                self.last_error = f"启动 Hotmail helper 失败：{exc}"
                if self.log_handle:
                    self.log_handle.close()
                    self.log_handle = None
                return False, self.last_error

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            healthy, reason = process_health_check(normalized, timeout=1.0)
            if healthy:
                self.last_error = ""
                return True, ""
            if self.process and self.process.poll() is not None:
                break
            time.sleep(0.2)
        self.last_error = f"Hotmail helper 未能启动：{reason}"
        if self.process and self.process.poll() is not None:
            self.process = None
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
        return False, self.last_error

    def status(self) -> dict[str, Any]:
        healthy, reason = process_health_check(self.base_url, timeout=1.0)
        return {
            "baseUrl": self.base_url,
            "healthy": healthy,
            "owned": bool(self.process and self.process.poll() is None),
            "error": "" if healthy else (self.last_error or reason),
        }

    def stop_owned(self) -> None:
        with self._lock:
            process = self.process
            self.process = None
        if process and process.poll() is None:
            terminate_process_group(process, force=False)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                terminate_process_group(process, force=True)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


def terminate_process_group(process: subprocess.Popen[Any], force: bool = False) -> None:
    if process.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass


class LocalCdpWebSocket:
    """Minimal loopback WebSocket client for Chrome's browser-level CDP endpoint."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host: str, port: int, path: str, timeout: float = 15.0) -> None:
        if host != "127.0.0.1":
            raise ValueError("CDP WebSocket 仅允许连接 127.0.0.1。")
        self.host = host
        self.port = int(port)
        self.path = path if str(path).startswith("/") else f"/{path}"
        self.timeout = timeout
        self.socket: Optional[socket.socket] = None
        self._buffer = b""
        self._next_id = 1

    def __enter__(self) -> "LocalCdpWebSocket":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Chrome CDP WebSocket 握手提前断开。")
            response += chunk
            if len(response) > 64 * 1024:
                raise RuntimeError("Chrome CDP WebSocket 握手响应异常过大。")
        headers, self._buffer = response.split(b"\r\n\r\n", 1)
        status_line = headers.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"Chrome CDP WebSocket 握手失败：{status_line.decode('latin1', 'replace')}")
        expected_accept = base64.b64encode(
            hashlib.sha1(f"{key}{self._GUID}".encode("ascii")).digest()
        ).decode("ascii")
        header_text = headers.decode("latin1", "replace").lower()
        if f"sec-websocket-accept: {expected_accept.lower()}" not in header_text:
            raise RuntimeError("Chrome CDP WebSocket 握手签名无效。")
        self.socket = sock
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.socket:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self.socket.close()
            self.socket = None

    def _recv_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            if not self.socket:
                raise RuntimeError("Chrome CDP WebSocket 未连接。")
            chunk = self.socket.recv(max(4096, length - len(self._buffer)))
            if not chunk:
                raise RuntimeError("Chrome CDP WebSocket 已断开。")
            self._buffer += chunk
        value, self._buffer = self._buffer[:length], self._buffer[length:]
        return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.socket:
            raise RuntimeError("Chrome CDP WebSocket 未连接。")
        payload = bytes(payload)
        first = 0x80 | (opcode & 0x0F)
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length <= 0xFFFF:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        masked = bool(second & 0x80)
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        call_id = self._next_id
        self._next_id += 1
        message = json.dumps({"id": call_id, "method": method, "params": params or {}}).encode("utf-8")
        self._send_frame(0x1, message)
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise RuntimeError("Chrome CDP WebSocket 在命令完成前关闭。")
            if opcode != 0x1:
                continue
            response = json.loads(payload.decode("utf-8"))
            if response.get("id") != call_id:
                continue
            if response.get("error"):
                error = response["error"]
                raise RuntimeError(f"CDP {method} 失败：{error.get('message') or error}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}


def wait_for_chrome_cdp_endpoint(
    profile_dir: Path,
    process: subprocess.Popen[Any],
    timeout_seconds: float = 20.0,
) -> tuple[int, str]:
    active_port_path = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chrome 在 CDP 就绪前退出（退出码 {process.returncode}）。")
        try:
            lines = active_port_path.read_text(encoding="utf-8").splitlines()
            port = int(lines[0])
            websocket_path = str(lines[1]).strip()
            if 1 <= port <= 65535 and websocket_path.startswith("/devtools/browser/"):
                return port, websocket_path
        except (OSError, ValueError, IndexError):
            pass
        time.sleep(0.1)
    raise RuntimeError("等待 Chrome 本机调试端口超时。")


def load_extension_and_open_worker_via_cdp(
    profile_dir: Path,
    process: subprocess.Popen[Any],
    extension_dir: Path,
    expected_extension_id: str,
    worker_url: str,
) -> None:
    port, websocket_path = wait_for_chrome_cdp_endpoint(profile_dir, process)
    with LocalCdpWebSocket("127.0.0.1", port, websocket_path, timeout=20.0) as cdp:
        loaded = cdp.call("Extensions.loadUnpacked", {"path": str(extension_dir.resolve())})
        loaded_id = str(loaded.get("id") or "").strip()
        if loaded_id != expected_extension_id:
            raise RuntimeError(
                f"加载后的扩展 ID 不一致：期望 {expected_extension_id}，实际 {loaded_id or '空'}。"
            )
        created = cdp.call("Target.createTarget", {"url": worker_url, "newWindow": False})
        if not str(created.get("targetId") or "").strip():
            raise RuntimeError("Chrome 未返回 Worker 标签页 targetId。")


@dataclass
class RunnerJob:
    job_id: str
    index: int
    mode: str
    token: str = field(repr=False)
    account: Optional[ImportedAccount] = field(default=None, repr=False)
    proxy: Optional[ProxyEntry] = field(default=None, repr=False)
    email_label: str = ""
    proxy_label: str = ""
    diagnostic_marker: str = ""
    status: str = "queued"
    worker_id: int = 0
    phase: str = "queued"
    current_node: str = ""
    reason: str = ""
    output_file: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    last_heartbeat_at: float = 0.0
    recent_logs: list[str] = field(default_factory=list)
    profile_dir: Optional[Path] = None
    chrome_log_path: Optional[Path] = None
    process: Optional[subprocess.Popen[Any]] = field(default=None, repr=False)
    chrome_log_handle: Optional[Any] = field(default=None, repr=False)
    proxy_bridge: Optional[Any] = field(default=None, repr=False)
    terminate_requested_at: float = 0.0
    diagnostic_ready: bool = False
    diagnostic_payload: dict[str, Any] = field(default_factory=dict)
    proxy_exit_ip: str = ""
    proxy_exit_region: str = ""
    oauth_released: bool = False

    @property
    def email(self) -> str:
        if self.account:
            return self.account.email
        if self.email_label:
            return self.email_label
        return f"隔离自检 #{self.index + 1}"

    def public_dict(self) -> dict[str, Any]:
        now = time.time()
        elapsed_from = self.started_at or self.created_at
        elapsed_until = self.finished_at or now
        return {
            "id": self.job_id,
            "index": self.index + 1,
            "email": self.email,
            "status": self.status,
            "workerId": self.worker_id,
            "phase": self.phase,
            "currentNode": self.current_node,
            "reason": self.reason,
            "outputFile": self.output_file,
            "proxy": self.proxy.label if self.proxy else (self.proxy_label or "直连"),
            "exitIp": self.proxy_exit_ip,
            "proxyGuardPassed": self.oauth_released,
            "recentLogs": self.recent_logs[-5:],
            "profileDir": str(self.profile_dir or ""),
            "chromeLog": str(self.chrome_log_path or ""),
            "elapsedSeconds": max(0, int(elapsed_until - elapsed_from)),
            "diagnostic": self.diagnostic_payload if self.mode == "diagnostic" else None,
        }


@dataclass
class RunnerRun:
    run_id: str
    mode: str
    concurrency: int
    timeout_seconds: int
    chrome_path: Path
    profile_root: Path
    output_dir: Path
    helper_base_url: str
    sms_bower_api_key: str = field(repr=False)
    sms_settings: dict[str, Any] = field(default_factory=dict, repr=False)
    proxies: list[ProxyEntry] = field(default_factory=list, repr=False)
    rejected_proxy_ids: set[str] = field(default_factory=set, repr=False)
    cleanup_profiles: bool = True
    jobs: list[RunnerJob] = field(default_factory=list)
    status: str = "running"
    stop_requested: bool = False
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        counts = {key: 0 for key in ["queued", "starting", "running", "success", "failed", "stopped"]}
        for job in self.jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        elapsed_until = self.finished_at or time.time()
        return {
            "id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "concurrency": self.concurrency,
            "total": len(self.jobs),
            "counts": counts,
            "outputDir": str(self.output_dir),
            "profileRoot": str(self.profile_root),
            "createdAt": datetime.fromtimestamp(self.created_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsedSeconds": max(0, int(elapsed_until - self.created_at)),
            "jobs": [job.public_dict() for job in self.jobs],
        }


def is_descendant(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def safe_cleanup_profile(path: Path, profile_root: Path, run_id: str) -> bool:
    resolved_path = path.resolve()
    resolved_root = profile_root.resolve()
    expected_prefix = f"gujumpgate-{run_id}-job-"
    if not is_descendant(resolved_path, resolved_root) or not resolved_path.name.startswith(expected_prefix):
        raise RuntimeError(f"拒绝清理未通过路径保护的 Profile：{resolved_path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)
    return True


def build_worker_url(extension_id: str, callback_base_url: str, token: str) -> str:
    query = urllib.parse.urlencode({"callback": callback_base_url, "token": token})
    return f"chrome-extension://{extension_id}/runner/worker.html?{query}"


def build_worker_launch_url(callback_base_url: str, token: str) -> str:
    return f"{callback_base_url.rstrip('/')}/launch/{urllib.parse.quote(token, safe='')}"


def build_chrome_command(
    chrome_path: Path,
    profile_dir: Path,
    worker_id: int,
) -> list[str]:
    command = [
        str(chrome_path),
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--enable-unsafe-extension-debugging",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-default-apps",
        "--window-size=1120,860",
        f"--window-position={20 + ((worker_id - 1) % 5) * 36},{30 + ((worker_id - 1) // 5) * 42}",
    ]
    if sys.platform == "darwin":
        command.append("--use-mock-keychain")
    command.extend(["--new-window", "about:blank"])
    return command


def validate_output_artifact(file_path: str, output_dir: Path, expected_email: str) -> tuple[bool, str]:
    raw_path = str(file_path or "").strip()
    if not raw_path:
        return False, "扩展报告成功，但没有返回 JSON 文件路径。"
    candidate = Path(raw_path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False, f"扩展报告的 JSON 文件不存在：{candidate}"
    if not resolved.is_file() or not is_descendant(resolved, output_dir):
        return False, "JSON 文件不在本轮配置的输出目录内。"
    try:
        if resolved.stat().st_size > 5 * 1024 * 1024:
            return False, "JSON 文件异常过大。"
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"JSON 文件读取失败：{exc}"
    required = ["email", "access_token", "refresh_token", "last_refresh"]
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if str(payload.get("type") or "").strip().lower() != "codex":
        return False, "JSON 的 type 不是 codex。"
    if missing:
        return False, f"JSON 缺少必需字段：{', '.join(missing)}。"
    if str(payload.get("email") or "").strip().lower() != expected_email.strip().lower():
        return False, "JSON 邮箱与当前任务邮箱不一致。"
    return True, str(resolved)


class ProfileRunnerManager:
    def __init__(
        self,
        host: str,
        port: int,
        helper_manager: HelperManager,
        config_path: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.callback_base_url = f"http://127.0.0.1:{port}"
        self.helper_manager = helper_manager
        self.extension_id = compute_extension_id()
        self.chrome_path = detect_chrome_binary()
        self.current_run: Optional[RunnerRun] = None
        self.config_path = config_path.resolve() if config_path else None
        self._saved_settings: dict[str, Any] = {}
        self._account_pool: dict[str, AccountPoolEntry] = {}
        self._jobs_by_token: dict[str, RunnerJob] = {}
        self._lock = threading.RLock()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._load_persisted_config()

    def _load_persisted_config(self) -> None:
        if not self.config_path or not self.config_path.is_file():
            return
        try:
            if self.config_path.stat().st_size > 20 * 1024 * 1024:
                return
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        settings = payload.get("settings") if isinstance(payload, dict) else None
        if isinstance(settings, dict):
            self._saved_settings = {
                key: value for key, value in settings.items() if key in PERSISTED_SETTING_KEYS
            }
        pool_entries = payload.get("accountPool") if isinstance(payload, dict) else None
        if not isinstance(pool_entries, list):
            return
        for raw_entry in pool_entries:
            if not isinstance(raw_entry, dict):
                continue
            email = str(raw_entry.get("email") or "").strip()
            client_id = str(raw_entry.get("clientId") or "").strip()
            refresh_token = str(raw_entry.get("refreshToken") or "").strip()
            if not email or "@" not in email or not client_id or not refresh_token:
                continue
            status = str(raw_entry.get("status") or "pending").strip().lower()
            reason = str(raw_entry.get("reason") or "")[:2000]
            if is_invalid_mail_refresh_reason(reason):
                status = "invalid"
            elif status in {"running", "starting"}:
                status = "failed"
            elif status not in {"pending", "success", "failed", "stopped", "invalid"}:
                status = "failed"
            account = ImportedAccount(
                email=email,
                password=str(raw_entry.get("password") or ""),
                client_id=client_id,
                refresh_token=refresh_token,
            )
            self._account_pool[email.lower()] = AccountPoolEntry(
                account=account,
                status=status,
                reason=reason,
                output_file=str(raw_entry.get("outputFile") or "")[:2048],
            )

    def _persist_config_unlocked(self) -> None:
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "savedAt": utc_now_iso(),
            "settings": self._saved_settings,
            "accountPool": [
                {
                    "email": entry.account.email,
                    "password": entry.account.password,
                    "clientId": entry.account.client_id,
                    "refreshToken": entry.account.refresh_token,
                    "status": entry.status,
                    "reason": entry.reason,
                    "outputFile": entry.output_file,
                }
                for entry in self._account_pool.values()
            ],
        }
        temporary_path = self.config_path.with_name(
            f".{self.config_path.name}.{secrets.token_hex(6)}.tmp"
        )
        try:
            temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary_path, self.config_path)
            try:
                self.config_path.chmod(0o600)
            except OSError:
                pass
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def saved_settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._saved_settings)

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        proxy_text = str(payload.get("proxyPoolText") or "")
        if len(proxy_text.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("代理池配置不能超过 5 MB。")
        proxy_default_protocol = normalize_proxy_protocol(payload.get("proxyDefaultProtocol") or "http")
        _, proxy_errors = parse_proxy_text(proxy_text, proxy_default_protocol)
        if proxy_errors:
            raise ValueError("\n".join(proxy_errors[:20]))
        api_key = str(payload.get("smsBowerApiKey") or "").strip()
        if len(api_key) > 4096:
            raise ValueError("SMSBower API Key 长度异常。")
        sms_settings = normalize_sms_settings(payload)
        settings = {
            key: payload.get(key)
            for key in PERSISTED_SETTING_KEYS
            if key in payload
        }
        settings.update({
            "proxyDefaultProtocol": proxy_default_protocol,
            "smsBowerCountryIds": sms_settings["countryIds"],
            "smsBowerAcquirePriority": sms_settings["acquirePriority"],
            "smsBowerMinPrice": sms_settings["minPrice"],
            "smsBowerMaxPrice": sms_settings["maxPrice"],
            "smsBowerPreferredPrice": sms_settings["preferredPrice"],
            "verificationResendCount": sms_settings["verificationResendCount"],
            "phoneReplacementLimit": sms_settings["replacementLimit"],
            "whatsappRestartEnabled": sms_settings["whatsappRestartEnabled"],
            "whatsappRestartMaxAttempts": sms_settings["whatsappRestartMaxAttempts"],
            "phoneCodeWaitSeconds": sms_settings["codeWaitSeconds"],
            "phoneCodeTimeoutWindows": sms_settings["timeoutWindows"],
            "phoneCodePollIntervalSeconds": sms_settings["pollIntervalSeconds"],
            "phoneCodePollMaxRounds": sms_settings["pollMaxRounds"],
            "phoneActivationRetryRounds": sms_settings["activationRetryRounds"],
            "phoneAutoReleaseOnStop": sms_settings["autoReleaseOnStop"],
        })
        with self._lock:
            self._saved_settings = settings
            self._persist_config_unlocked()
            return dict(self._saved_settings)

    def query_smsbower_balance(self, payload: dict[str, Any]) -> dict[str, Any]:
        supplied_key = str(payload.get("smsBowerApiKey") or "").strip()
        with self._lock:
            api_key = supplied_key or str(self._saved_settings.get("smsBowerApiKey") or "").strip()
        balance = fetch_smsbower_balance(api_key)
        return {"balance": balance, "currency": "USD"}

    def _require_available(self) -> None:
        if self.current_run and self.current_run.status in {"running", "stopping"}:
            raise ValueError("当前已有任务在运行，请先等待完成或停止。")

    def _account_pool_public_unlocked(self) -> dict[str, Any]:
        entries = list(self._account_pool.values())
        available_statuses = {"pending", "failed", "stopped"}
        available = sum(1 for entry in entries if entry.status in available_statuses)
        used = sum(1 for entry in entries if entry.status == "success")
        invalid = sum(1 for entry in entries if entry.status == "invalid")
        running = sum(1 for entry in entries if entry.status in {"starting", "running"})
        return {
            "total": len(entries),
            "available": available,
            "used": used,
            "invalid": invalid,
            "running": running,
            "entries": [entry.public_dict() for entry in entries],
        }

    def import_accounts(self, raw_text: Any) -> dict[str, Any]:
        accounts, errors = parse_account_text(raw_text)
        if errors:
            raise ValueError("\n".join(errors[:20]))
        imported = 0
        updated = 0
        skipped_used = 0
        with self._lock:
            for account in accounts:
                key = account.email.strip().lower()
                existing = self._account_pool.get(key)
                if existing is None:
                    self._account_pool[key] = AccountPoolEntry(account=account)
                    imported += 1
                elif existing.status != "success":
                    existing.account = account
                    existing.status = "pending"
                    existing.reason = ""
                    existing.output_file = ""
                    updated += 1
                else:
                    skipped_used += 1
            self._persist_config_unlocked()
            return {
                "imported": imported,
                "updated": updated,
                "skippedUsed": skipped_used,
                "pool": self._account_pool_public_unlocked(),
            }

    def retry_failed_accounts(self) -> dict[str, Any]:
        retried = 0
        skipped_invalid_token = 0
        with self._lock:
            for entry in self._account_pool.values():
                if entry.status == "invalid":
                    skipped_invalid_token += 1
                    continue
                if entry.status not in {"failed", "stopped"}:
                    continue
                if is_invalid_mail_refresh_reason(entry.reason):
                    entry.status = "invalid"
                    skipped_invalid_token += 1
                    continue
                entry.status = "pending"
                entry.reason = ""
                entry.output_file = ""
                retried += 1
            self._persist_config_unlocked()
            return {
                "retried": retried,
                "skippedInvalidToken": skipped_invalid_token,
                "pool": self._account_pool_public_unlocked(),
            }

    def start_accounts_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_accounts = str(payload.get("accountsText") or "").strip()
        if raw_accounts:
            self.import_accounts(raw_accounts)
        with self._lock:
            accounts = [
                entry.account
                for entry in self._account_pool.values()
                if entry.status in {"pending", "failed", "stopped"}
            ]
        if not accounts:
            raise ValueError("微软邮箱账户池没有可用账号，请先导入账号。")
        secrets.SystemRandom().shuffle(accounts)
        proxy_default_protocol = normalize_proxy_protocol(payload.get("proxyDefaultProtocol") or "http")
        proxies, proxy_errors = parse_proxy_text(payload.get("proxyPoolText"), proxy_default_protocol)
        if proxy_errors:
            raise ValueError("\n".join(proxy_errors[:20]))
        api_key = str(payload.get("smsBowerApiKey") or "").strip()
        if not api_key:
            raise ValueError("请填写 SMSBower API Key。")
        sms_settings = normalize_sms_settings(payload)
        self.save_settings(payload)
        return self._start_run(
            payload,
            accounts=accounts,
            mode="accounts",
            sms_bower_api_key=api_key,
            sms_settings=sms_settings,
            proxies=proxies,
        )

    def start_diagnostic(self, payload: dict[str, Any]) -> dict[str, Any]:
        concurrency = clamp_int(payload.get("concurrency"), 1, MAX_CONCURRENCY, MAX_CONCURRENCY)
        return self._start_run(
            payload,
            accounts=[None] * concurrency,
            mode="diagnostic",
            sms_bower_api_key="",
            sms_settings=normalize_sms_settings(payload),
            proxies=[],
        )

    def _start_run(
        self,
        payload: dict[str, Any],
        accounts: list[Optional[ImportedAccount]],
        mode: str,
        sms_bower_api_key: str,
        sms_settings: dict[str, Any],
        proxies: list[ProxyEntry],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            chrome_value = str(payload.get("chromePath") or self.chrome_path or "").strip()
            chrome_path = Path(chrome_value).expanduser()
            if not chrome_value or not chrome_path.is_absolute() or not chrome_path.is_file():
                raise ValueError("未找到 Google Chrome，请在高级设置中填写 Chrome 可执行文件绝对路径。")
            profile_default = Path("/tmp/gujumpgate-profiles") if os.name == "posix" else Path(tempfile.gettempdir()) / "gujumpgate-profiles"
            profile_root = ensure_absolute_directory(payload.get("profileRoot") or profile_default, "Profile 根目录")
            verify_directory_writable(profile_root, "Profile 根目录")
            output_dir = ensure_absolute_directory(payload.get("outputDir") or DEFAULT_OUTPUT_DIR, "JSON 输出目录")
            verify_directory_writable(output_dir, "JSON 输出目录")
            helper_base_url = normalize_loopback_base_url(payload.get("helperBaseUrl") or DEFAULT_HELPER_BASE_URL)
            helper_ok, helper_error = self.helper_manager.ensure(helper_base_url)
            if not helper_ok and mode == "accounts":
                raise ValueError(f"Hotmail 本地助手不可用：{helper_error}")

            requested_concurrency = clamp_int(payload.get("concurrency"), 1, MAX_CONCURRENCY, MAX_CONCURRENCY)
            concurrency = min(requested_concurrency, len(accounts))
            timeout_minutes = clamp_int(payload.get("timeoutMinutes"), 5, 120, DEFAULT_TIMEOUT_MINUTES)
            cleanup_profiles = bool(payload.get("cleanupProfiles", True))
            run_id = f"{int(time.time())}-{secrets.token_hex(4)}"
            jobs: list[RunnerJob] = []
            for index, account in enumerate(accounts):
                token = secrets.token_urlsafe(32)
                job = RunnerJob(
                    job_id=f"job-{index + 1:04d}-{secrets.token_hex(3)}",
                    index=index,
                    mode=mode,
                    token=token,
                    account=account,
                    email_label=account.email if account else "",
                    diagnostic_marker=secrets.token_hex(16) if mode == "diagnostic" else "",
                )
                jobs.append(job)
                self._jobs_by_token[token] = job
            run = RunnerRun(
                run_id=run_id,
                mode=mode,
                concurrency=concurrency,
                timeout_seconds=timeout_minutes * 60,
                chrome_path=chrome_path.resolve(),
                profile_root=profile_root,
                output_dir=output_dir,
                helper_base_url=helper_base_url,
                sms_bower_api_key=sms_bower_api_key,
                sms_settings=sms_settings,
                proxies=proxies,
                cleanup_profiles=cleanup_profiles,
                jobs=jobs,
            )
            self.current_run = run
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                args=(run_id,),
                name=f"profile-runner-{run_id}",
                daemon=True,
            )
            self._scheduler_thread.start()
            return run.public_dict()

    def _scheduler_loop(self, run_id: str) -> None:
        while not self._shutdown:
            with self._lock:
                run = self.current_run
                if not run or run.run_id != run_id:
                    return
                self._maintain_processes(run)
                if run.stop_requested:
                    self._apply_stop(run)
                self._launch_queued_jobs(run)
                self._sync_account_pool(run)
                if all(job.status in TERMINAL_JOB_STATUSES and job.process is None for job in run.jobs):
                    run.sms_bower_api_key = ""
                    run.sms_settings.clear()
                    run.proxies.clear()
                    for job in run.jobs:
                        self._expire_job_secrets(job)
                    run.finished_at = time.time()
                    run.status = "stopped" if run.stop_requested else "complete"
                    return
            time.sleep(0.25)

    def _sync_account_pool(self, run: RunnerRun) -> None:
        if run.mode != "accounts":
            return
        changed = False
        for job in run.jobs:
            entry = self._account_pool.get(job.email.strip().lower())
            if entry is None or job.status == "queued":
                continue
            pool_status = "invalid" if is_invalid_mail_refresh_reason(job.reason) else job.status
            next_values = (pool_status, job.reason, job.output_file)
            if next_values != (entry.status, entry.reason, entry.output_file):
                entry.status, entry.reason, entry.output_file = next_values
                changed = True
        if changed:
            try:
                self._persist_config_unlocked()
            except OSError:
                pass

    def _active_worker_ids(self, run: RunnerRun) -> set[int]:
        return {job.worker_id for job in run.jobs if job.process is not None and job.worker_id > 0}

    def _success_count(self, run: RunnerRun) -> int:
        return sum(1 for job in run.jobs if job.status == "success")

    def _target_success_reached(self, run: RunnerRun) -> bool:
        return (
            run.mode == "accounts"
            and run.target_success_count > 0
            and self._success_count(run) >= run.target_success_count
        )

    def _stop_remaining_after_target(self, run: RunnerRun) -> None:
        now = time.time()
        for job in run.jobs:
            if job.status == "queued":
                job.status = "stopped"
                job.phase = "target_reached"
                job.reason = f"已达到目标导出数 {run.target_success_count}，本账号未启动。"
                job.finished_at = now
            elif job.process is not None and job.status not in TERMINAL_JOB_STATUSES:
                job.status = "stopped"
                job.phase = "target_reached"
                job.reason = f"已达到目标导出数 {run.target_success_count}，已停止剩余 Worker。"
                job.finished_at = now
                terminate_process_group(job.process, force=False)
                job.terminate_requested_at = now

    def _launch_queued_jobs(self, run: RunnerRun) -> None:
        if run.stop_requested:
            return
        if self._target_success_reached(run):
            return
        active_workers = self._active_worker_ids(run)
        free_workers = [worker_id for worker_id in range(1, run.concurrency + 1) if worker_id not in active_workers]
        queued = [job for job in run.jobs if job.status == "queued"]
        for worker_id, job in zip(free_workers, queued):
            if run.proxies:
                proxy = self._select_available_proxy(run)
                if proxy is None:
                    usable_count = len(run.proxies) - len(run.rejected_proxy_ids)
                    active_proxy_count = len({
                        item.proxy.proxy_id
                        for item in run.jobs
                        if item.process is not None and item.proxy is not None
                    })
                    if usable_count <= active_proxy_count:
                        break
                    job.status = "failed"
                    job.phase = "proxy_allocation"
                    job.reason = "没有可安全分配的独占代理。"
                    job.finished_at = time.time()
                    continue
                job.proxy = proxy
                job.proxy_label = proxy.label
            self._launch_job(run, job, worker_id)

        if queued and run.proxies:
            usable = [proxy for proxy in run.proxies if proxy.proxy_id not in run.rejected_proxy_ids]
            active_ids = {
                item.proxy.proxy_id
                for item in run.jobs
                if item.process is not None and item.proxy is not None
            }
            if not usable and not active_ids:
                run.stop_requested = True
                for job in run.jobs:
                    if job.status == "queued":
                        job.status = "stopped"
                        job.phase = "not_started"
                        job.reason = "代理池已无可用代理，本账号未启动并保留为可用。"
                        job.finished_at = time.time()

    def _select_available_proxy(self, run: RunnerRun) -> Optional[ProxyEntry]:
        active_proxy_ids = {
            job.proxy.proxy_id
            for job in run.jobs
            if job.process is not None and job.proxy is not None
        }
        candidates = [
            proxy for proxy in run.proxies
            if proxy.proxy_id not in active_proxy_ids
            and proxy.proxy_id not in run.rejected_proxy_ids
        ]
        if not candidates:
            return None
        launch_count = sum(1 for job in run.jobs if job.started_at)
        return candidates[launch_count % len(candidates)]

    def _launch_job(self, run: RunnerRun, job: RunnerJob, worker_id: int) -> None:
        profile_dir = run.profile_root / f"gujumpgate-{run.run_id}-job-{job.index + 1:04d}-worker-{worker_id:02d}"
        if profile_dir.exists():
            job.status = "failed"
            job.reason = "新的任务 Profile 目录意外已存在，已拒绝复用。"
            job.finished_at = time.time()
            return
        profile_dir.mkdir(parents=True, exist_ok=False)
        run_log_dir = DATA_DIR / run.run_id
        run_log_dir.mkdir(parents=True, exist_ok=True)
        chrome_log_path = run_log_dir / f"worker-{worker_id:02d}-job-{job.index + 1:04d}.log"
        command = build_chrome_command(
            run.chrome_path,
            profile_dir,
            worker_id,
        )
        job.worker_id = worker_id
        job.profile_dir = profile_dir
        job.chrome_log_path = chrome_log_path
        job.status = "starting"
        job.phase = "launching_chrome"
        job.started_at = time.time()
        job.last_heartbeat_at = job.started_at
        try:
            job.chrome_log_handle = chrome_log_path.open("ab", buffering=0)
            job.process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=job.chrome_log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if job.proxy and job.proxy.protocol in {"http", "socks5"} and job.proxy.username:
                def record_bridge_error(message: str, target_job: RunnerJob = job) -> None:
                    with self._lock:
                        target_job.recent_logs.append(f"代理桥接失败：{message}")
                        target_job.recent_logs = target_job.recent_logs[-8:]

                bridge_factory = HttpAuthBridge if job.proxy.protocol == "http" else Socks5HttpBridge
                bridge = bridge_factory(job.proxy, on_error=record_bridge_error)
                try:
                    bridge.start()
                    job.proxy_bridge = bridge
                    job.recent_logs.append(f"已为带鉴权 {job.proxy.protocol.upper()} 代理启动本地 HTTP 桥接。")
                except OSError as exc:
                    bridge.stop()
                    job.proxy_bridge = None
                    job.recent_logs.append(f"代理桥接启动失败，已退回浏览器代理鉴权：{exc}")
            worker_url = build_worker_url(self.extension_id, self.callback_base_url, job.token)
            threading.Thread(
                target=self._bootstrap_job_browser,
                args=(run.run_id, job.job_id, job.process, profile_dir, worker_url),
                name=f"profile-bootstrap-{run.run_id}-{job.index + 1}",
                daemon=True,
            ).start()
        except OSError as exc:
            job.status = "failed"
            job.reason = f"启动 Chrome 失败：{exc}"
            job.finished_at = time.time()
            job.process = None
            self._stop_job_proxy_bridge(job)
            self._close_job_log(job)
            self._cleanup_job_profile(run, job)
            self._expire_job_secrets(job)

    def _bootstrap_job_browser(
        self,
        run_id: str,
        job_id: str,
        process: subprocess.Popen[Any],
        profile_dir: Path,
        worker_url: str,
    ) -> None:
        try:
            with self._lock:
                run = self.current_run
                job = next((item for item in (run.jobs if run else []) if item.job_id == job_id), None)
                if not run or run.run_id != run_id or not job or job.process is not process:
                    return
                job.phase = "loading_extension"
            load_extension_and_open_worker_via_cdp(
                profile_dir,
                process,
                PROJECT_ROOT,
                self.extension_id,
                worker_url,
            )
            with self._lock:
                run = self.current_run
                job = next((item for item in (run.jobs if run else []) if item.job_id == job_id), None)
                if (
                    run
                    and run.run_id == run_id
                    and job
                    and job.process is process
                    and job.status not in TERMINAL_JOB_STATUSES
                ):
                    job.phase = "worker_page_opened"
        except Exception as exc:
            with self._lock:
                run = self.current_run
                job = next((item for item in (run.jobs if run else []) if item.job_id == job_id), None)
                if (
                    run
                    and run.run_id == run_id
                    and job
                    and job.process is process
                    and job.status not in TERMINAL_JOB_STATUSES
                ):
                    job.status = "failed"
                    job.phase = "extension_bootstrap"
                    job.reason = f"加载 GuJumpgate 扩展失败：{exc}"
                    job.finished_at = time.time()

    def _maintain_processes(self, run: RunnerRun) -> None:
        now = time.time()
        for job in run.jobs:
            process = job.process
            if process is None:
                continue
            exit_code = process.poll()
            if exit_code is not None:
                if job.status not in TERMINAL_JOB_STATUSES:
                    job.status = "failed"
                    job.reason = f"Chrome 在任务完成前退出（退出码 {exit_code}）。"
                    job.finished_at = now
                job.process = None
                self._close_job_log(job)
                self._cleanup_job_profile(run, job)
                self._expire_job_secrets(job)
                continue
            if job.status in TERMINAL_JOB_STATUSES:
                if not job.terminate_requested_at:
                    terminate_process_group(process, force=False)
                    job.terminate_requested_at = now
                elif now - job.terminate_requested_at > 4:
                    terminate_process_group(process, force=True)
                continue
            if job.started_at and now - job.started_at > run.timeout_seconds:
                job.status = "failed"
                job.phase = "timeout"
                job.reason = f"单账号运行超过 {run.timeout_seconds // 60} 分钟，已停止当前 Profile。"
                job.finished_at = now
                terminate_process_group(process, force=False)
                job.terminate_requested_at = now

    def _close_job_log(self, job: RunnerJob) -> None:
        if job.chrome_log_handle:
            try:
                job.chrome_log_handle.close()
            except OSError:
                pass
            job.chrome_log_handle = None

    def _stop_job_proxy_bridge(self, job: RunnerJob) -> None:
        if job.proxy_bridge:
            job.proxy_bridge.stop()
            job.proxy_bridge = None

    def _expire_job_secrets(self, job: RunnerJob) -> None:
        self._jobs_by_token.pop(job.token, None)
        self._stop_job_proxy_bridge(job)
        job.account = None
        job.proxy = None

    def _cleanup_job_profile(self, run: RunnerRun, job: RunnerJob) -> None:
        if not run.cleanup_profiles or not job.profile_dir:
            return
        try:
            safe_cleanup_profile(job.profile_dir, run.profile_root, run.run_id)
        except Exception as exc:
            job.recent_logs.append(f"Profile 清理失败：{exc}")

    def _apply_stop(self, run: RunnerRun) -> None:
        now = time.time()
        run.status = "stopping"
        for job in run.jobs:
            if job.status == "queued":
                job.status = "stopped"
                job.phase = "stopped"
                job.reason = "用户停止了本轮任务。"
                job.finished_at = now
            elif job.process is not None and job.status not in TERMINAL_JOB_STATUSES:
                job.status = "stopped"
                job.phase = "stopped"
                job.reason = "用户停止了本轮任务。"
                job.finished_at = now
                terminate_process_group(job.process, force=False)
                job.terminate_requested_at = now

    def stop_current_run(self) -> dict[str, Any]:
        with self._lock:
            if not self.current_run or self.current_run.status not in {"running", "stopping"}:
                raise ValueError("当前没有正在运行的任务。")
            self.current_run.stop_requested = True
            self.current_run.status = "stopping"
            return self.current_run.public_dict()

    def close_all_workers(self) -> dict[str, Any]:
        with self._lock:
            run = self.current_run
            if not run:
                return {"closedCount": 0, "run": None}
            active_jobs = [job for job in run.jobs if job.process is not None]
            run.stop_requested = True
            if active_jobs or run.status in {"running", "stopping"}:
                run.status = "stopping"
                self._apply_stop(run)
            return {
                "closedCount": len(active_jobs),
                "run": run.public_dict(),
            }

    def get_worker_config(self, token: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs_by_token.get(token)
            run = self.current_run
            if not job or not run or job not in run.jobs or job.status in TERMINAL_JOB_STATUSES:
                raise KeyError("任务令牌无效或已过期。")
            return {
                "jobToken": job.token,
                "jobIndex": job.index + 1,
                "workerId": job.worker_id,
                "mode": job.mode,
                "account": job.account.worker_payload() if job.account else None,
                "proxy": (
                    job.proxy_bridge.proxy_entry.worker_payload()
                    if job.proxy_bridge
                    else (job.proxy.worker_payload() if job.proxy else None)
                ),
                "diagnosticMarker": job.diagnostic_marker,
                "expectedExtensionId": self.extension_id,
                "helperBaseUrl": run.helper_base_url,
                "outputDir": str(run.output_dir),
                "smsBowerApiKey": run.sms_bower_api_key,
                "smsSettings": dict(run.sms_settings),
            }

    def diagnostic_release_ready(self, token: str) -> bool:
        with self._lock:
            job = self._jobs_by_token.get(token)
            run = self.current_run
            if not job or not run or run.mode != "diagnostic" or job not in run.jobs:
                raise KeyError("隔离自检令牌无效。")
            return all(candidate.diagnostic_ready for candidate in run.jobs)

    def handle_worker_event(self, token: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = self._jobs_by_token.get(token)
            run = self.current_run
            if not job or not run or job not in run.jobs:
                raise KeyError("任务令牌无效或已过期。")
            if job.status in TERMINAL_JOB_STATUSES:
                return {"accepted": True, "terminal": True}
            normalized_kind = str(kind or "").strip().lower()
            now = time.time()
            job.last_heartbeat_at = now
            event_result: dict[str, Any] = {}
            if normalized_kind in {"booted", "started"}:
                job.status = "running"
                job.phase = str(payload.get("phase") or normalized_kind)[:80]
            elif normalized_kind == "proxy-ready":
                if not job.proxy:
                    raise ValueError("当前任务没有分配代理，不能进入代理出口放行。")
                exit_ip = str(payload.get("exitIp") or "").strip()
                if not exit_ip:
                    run.rejected_proxy_ids.add(job.proxy.proxy_id)
                    raise ValueError("代理探测没有返回出口 IP。")
                conflicting_job = next((
                    other for other in run.jobs
                    if other is not job
                    and other.process is not None
                    and other.oauth_released
                    and other.proxy_exit_ip == exit_ip
                ), None)
                job.proxy_exit_ip = exit_ip[:128]
                job.proxy_exit_region = str(payload.get("exitRegion") or "")[:160]
                job.phase = "proxy_guard"
                if conflicting_job:
                    run.rejected_proxy_ids.add(job.proxy.proxy_id)
                    job.status = "failed"
                    job.reason = (
                        f"代理实际出口 {exit_ip} 与 Worker {conflicting_job.worker_id} 重复，"
                        "已在打开 OpenAI 前拦截。"
                    )
                    job.finished_at = now
                    event_result = {"proxyRelease": False, "reason": job.reason}
                else:
                    job.status = "running"
                    job.oauth_released = True
                    event_result = {"proxyRelease": True, "exitIp": exit_ip}
            elif normalized_kind == "heartbeat":
                job.status = "running"
                job.phase = str(payload.get("phase") or "running")[:80]
                job.current_node = str(payload.get("currentNodeId") or "")[:160]
                job.output_file = str(payload.get("existingPlusJsonFilePath") or job.output_file)[:2048]
                job.reason = str(payload.get("reason") or "")[:1000]
                logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
                job.recent_logs = [str(item)[:800] for item in logs[-8:]]
            elif normalized_kind == "diagnostic-ready":
                if run.mode != "diagnostic" or str(payload.get("marker") or "") != job.diagnostic_marker:
                    raise ValueError("隔离自检 ready 标记不匹配。")
                job.status = "running"
                job.phase = "diagnostic_barrier"
                job.diagnostic_ready = True
            elif normalized_kind == "success":
                if run.mode == "diagnostic":
                    passed = self._validate_diagnostic_result(job, payload)
                    if not passed:
                        job.status = "failed"
                        job.reason = "Profile 隔离标记校验失败。"
                    else:
                        job.status = "success"
                        job.reason = ""
                    job.diagnostic_payload = {
                        "passed": passed,
                        "extensionId": str(payload.get("extensionId") or ""),
                        "tabCount": clamp_int(payload.get("tabCount"), 0, 1000, 0),
                    }
                else:
                    output_file = str(payload.get("outputFile") or "").strip()
                    valid, result = validate_output_artifact(output_file, run.output_dir, job.email)
                    if valid:
                        job.status = "success"
                        job.output_file = result
                        job.reason = ""
                    else:
                        job.status = "failed"
                        job.reason = result
                job.phase = str(payload.get("phase") or "complete")[:80]
                job.finished_at = now
            elif normalized_kind == "failed":
                job.status = "failed"
                job.phase = str(payload.get("phase") or "failed")[:80]
                job.reason = str(payload.get("reason") or "Worker 报告任务失败。")[:2000]
                job.finished_at = now
                if job.proxy and job.phase in {"proxy_apply", "proxy_probe", "proxy_guard"}:
                    run.rejected_proxy_ids.add(job.proxy.proxy_id)
            else:
                raise ValueError(f"不支持的 Worker 事件：{normalized_kind}")
            return {
                "accepted": True,
                "terminal": job.status in TERMINAL_JOB_STATUSES,
                **event_result,
            }

    def _validate_diagnostic_result(self, job: RunnerJob, payload: dict[str, Any]) -> bool:
        marker = job.diagnostic_marker
        return bool(payload.get("passed")) and all([
            str(payload.get("marker") or "") == marker,
            str(payload.get("storageMarker") or "") == marker,
            str(payload.get("cookieMarker") or "") == marker,
            str(payload.get("extensionId") or "") == self.extension_id,
        ])

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "run": self.current_run.public_dict() if self.current_run else None,
                "accountPool": self._account_pool_public_unlocked(),
                "helper": self.helper_manager.status(),
            }

    def shutdown(self) -> None:
        self._shutdown = True
        with self._lock:
            if self.current_run and self.current_run.status in {"running", "stopping"}:
                self.current_run.stop_requested = True
                self._apply_stop(self.current_run)
            run = self.current_run
            jobs = list(run.jobs if run else [])
        for job in jobs:
            if job.process is not None:
                terminate_process_group(job.process, force=False)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if all(job.process is None or job.process.poll() is not None for job in jobs):
                break
            time.sleep(0.1)
        for job in jobs:
            process = job.process
            if process is not None and process.poll() is None:
                terminate_process_group(process, force=True)
            if process is not None:
                try:
                    process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            job.process = None
            self._close_job_log(job)
            if run:
                self._cleanup_job_profile(run, job)
            self._expire_job_secrets(job)
        if run:
            run.sms_bower_api_key = ""
            run.sms_settings.clear()
            run.proxies.clear()


class ProfileRunnerHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        manager: ProfileRunnerManager,
        csrf_token: str,
        diagnostic_only: bool = False,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.manager = manager
        self.csrf_token = csrf_token
        self.diagnostic_only = bool(diagnostic_only)


class ProfileRunnerHandler(BaseHTTPRequestHandler):
    server_version = "GuJumpgateProfileRunner/1.0"

    @property
    def app(self) -> ProfileRunnerHttpServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/worker/") or path.startswith("/launch/"):
            return
        super().log_message(fmt, *args)

    def _client_is_loopback(self) -> bool:
        return is_loopback_host(self.client_address[0])

    def _send_json(self, status: int, payload: dict[str, Any], cors: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self.send_header("Access-Control-Allow-Origin", f"chrome-extension://{self.app.manager.extension_id}")
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _send_worker_launcher(self, token: str) -> None:
        try:
            self.app.manager.get_worker_config(token)
        except KeyError:
            self.send_error(404)
            return
        worker_url = build_worker_url(
            self.app.manager.extension_id,
            self.app.manager.callback_base_url,
            token,
        )
        escaped_url = html.escape(worker_url, quote=True)
        body = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"refresh\" content=\"2;url={escaped_url}\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><title>启动 GuJumpgate Worker</title>"
            "</head><body><p>正在加载独立 Profile 的 GuJumpgate 扩展...</p>"
            f"<p><a href=\"{escaped_url}\">继续</a></p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'none'; navigate-to chrome-extension:")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, maximum_bytes: int = 10 * 1024 * 1024) -> dict[str, Any]:
        content_type = str(self.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            raise ValueError("请求 Content-Type 必须是 application/json。")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        if length <= 0 or length > maximum_bytes:
            raise ValueError("请求内容为空或过大。")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求 JSON 无效。") from exc
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象。")
        return value

    def _require_csrf(self) -> None:
        if not secrets.compare_digest(
            str(self.headers.get("X-Profile-Runner-Token") or ""),
            self.app.csrf_token,
        ):
            raise PermissionError("WebUI 请求令牌无效，请刷新页面。")

    def do_OPTIONS(self) -> None:
        if not self._client_is_loopback():
            self.send_error(403)
            return
        origin = str(self.headers.get("Origin") or "")
        expected = f"chrome-extension://{self.app.manager.extension_id}"
        if origin != expected:
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", expected)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._client_is_loopback():
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_asset(RUNNER_ASSET_DIR / "webui.html", "text/html; charset=utf-8")
            return
        if path == "/webui.css":
            self._send_asset(RUNNER_ASSET_DIR / "webui.css", "text/css; charset=utf-8")
            return
        if path == "/webui.js":
            self._send_asset(RUNNER_ASSET_DIR / "webui.js", "application/javascript; charset=utf-8")
            return
        if path in {"/health", "/api/health"}:
            self._send_json(200, {"ok": True, "service": "gujumpgate-profile-runner", "time": utc_now_iso()})
            return
        if path == "/api/config":
            chrome_path = self.app.manager.chrome_path
            self._send_json(200, {
                "ok": True,
                "csrfToken": self.app.csrf_token,
                "extensionId": self.app.manager.extension_id,
                "chromePath": str(chrome_path or ""),
                "helperBaseUrl": DEFAULT_HELPER_BASE_URL,
                "outputDir": str(DEFAULT_OUTPUT_DIR.resolve()),
                "profileRoot": "/tmp/gujumpgate-profiles" if os.name == "posix" else str(Path(tempfile.gettempdir()) / "gujumpgate-profiles"),
                "maxConcurrency": MAX_CONCURRENCY,
                "defaultConcurrency": 1,
                "defaultTimeoutMinutes": DEFAULT_TIMEOUT_MINUTES,
                "diagnosticOnly": self.app.diagnostic_only,
                "helper": self.app.manager.helper_manager.status(),
            })
            return
        if path == "/api/status":
            self._send_json(200, self.app.manager.public_status())
            return
        launch_prefix = "/launch/"
        if path.startswith(launch_prefix):
            token = urllib.parse.unquote(path[len(launch_prefix):])
            self._send_worker_launcher(token)
            return
        worker_prefix = "/api/worker/config/"
        if path.startswith(worker_prefix):
            token = urllib.parse.unquote(path[len(worker_prefix):])
            try:
                config = self.app.manager.get_worker_config(token)
                self._send_json(200, {"ok": True, "config": config}, cors=True)
            except KeyError as exc:
                self._send_json(404, {"ok": False, "error": str(exc)}, cors=True)
            return
        release_prefix = "/api/worker/diagnostic-release/"
        if path.startswith(release_prefix):
            token = urllib.parse.unquote(path[len(release_prefix):])
            try:
                ready = self.app.manager.diagnostic_release_ready(token)
                self._send_json(200, {"ok": True, "release": ready}, cors=True)
            except KeyError as exc:
                self._send_json(404, {"ok": False, "error": str(exc)}, cors=True)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._client_is_loopback():
            self.send_error(403)
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/settings/load":
                self._require_csrf()
                self._send_json(200, {"ok": True, "settings": self.app.manager.saved_settings()})
                return
            if path == "/api/settings/save":
                self._require_csrf()
                settings = self.app.manager.save_settings(payload)
                self._send_json(200, {"ok": True, "settings": settings})
                return
            if path == "/api/smsbower/balance":
                self._require_csrf()
                result = self.app.manager.query_smsbower_balance(payload)
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/accounts/import":
                self._require_csrf()
                if self.app.diagnostic_only:
                    raise PermissionError("当前服务以 diagnostic-only 模式运行，账号导入接口已禁用。")
                result = self.app.manager.import_accounts(payload.get("accountsText"))
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/accounts/retry-failed":
                self._require_csrf()
                if self.app.diagnostic_only:
                    raise PermissionError("当前服务以 diagnostic-only 模式运行，账户池接口已禁用。")
                result = self.app.manager.retry_failed_accounts()
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/run/start":
                self._require_csrf()
                if self.app.diagnostic_only:
                    raise PermissionError("当前服务以 diagnostic-only 模式运行，账号任务接口已禁用。")
                run = self.app.manager.start_accounts_run(payload)
                self._send_json(200, {"ok": True, "run": run})
                return
            if path == "/api/diagnostics/start":
                self._require_csrf()
                run = self.app.manager.start_diagnostic(payload)
                self._send_json(200, {"ok": True, "run": run})
                return
            if path == "/api/run/stop":
                self._require_csrf()
                run = self.app.manager.stop_current_run()
                self._send_json(200, {"ok": True, "run": run})
                return
            if path == "/api/workers/close-all":
                self._require_csrf()
                result = self.app.manager.close_all_workers()
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/worker/event":
                result = self.app.manager.handle_worker_event(
                    str(payload.get("token") or ""),
                    str(payload.get("kind") or ""),
                    payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                )
                self._send_json(200, {"ok": True, **result}, cors=True)
                return
            self.send_error(404)
        except PermissionError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)}, cors=path.startswith("/api/worker/"))
        except KeyError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)}, cors=path.startswith("/api/worker/"))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)}, cors=path.startswith("/api/worker/"))
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"本机调度器内部错误：{exc}"}, cors=path.startswith("/api/worker/"))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the GuJumpgate isolated Chrome Profile runner WebUI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Do not open the WebUI in the default browser.")
    parser.add_argument(
        "--diagnostic-only",
        action="store_true",
        help="Disable account runs and allow only isolated Chrome Profile diagnostics.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.host != DEFAULT_HOST:
        raise SystemExit("安全限制：WebUI 只能监听 127.0.0.1。")
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1-65535 之间。")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    helper_manager = HelperManager()
    if not args.diagnostic_only:
        helper_manager.ensure(DEFAULT_HELPER_BASE_URL)
    try:
        manager = ProfileRunnerManager(args.host, args.port, helper_manager, WEBUI_CONFIG_PATH)
        csrf_token = secrets.token_urlsafe(32)
        server = ProfileRunnerHttpServer(
            (args.host, args.port),
            ProfileRunnerHandler,
            manager,
            csrf_token,
            diagnostic_only=args.diagnostic_only,
        )
    except Exception as exc:
        helper_manager.stop_owned()
        print(f"启动 WebUI 失败：{exc}", file=sys.stderr, flush=True)
        return 1
    webui_url = f"http://{args.host}:{args.port}"

    print("=" * 64, flush=True)
    print("GuJumpgate 多 Chrome Profile 并发调度器", flush=True)
    print(f"WebUI: {webui_url}", flush=True)
    print(f"扩展 ID: {manager.extension_id}", flush=True)
    print(f"Chrome: {manager.chrome_path or '未检测到'}", flush=True)
    print(f"Hotmail helper: {helper_manager.base_url}", flush=True)
    print(f"运行模式: {'仅隔离自检（账号接口已禁用）' if args.diagnostic_only else '完整调度'}", flush=True)
    print("关闭此窗口会停止当前调度器及其启动的 Chrome workers。", flush=True)
    print("=" * 64, flush=True)

    def handle_termination_signal(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_termination_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handle_termination_signal)

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(webui_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        server.server_close()
        helper_manager.stop_owned()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
