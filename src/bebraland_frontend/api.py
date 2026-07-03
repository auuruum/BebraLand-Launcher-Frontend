from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi

from .config import build_update_id, platform_id


ProfilesCallback = Callable[[list[dict[str, Any]]], None]
LogCallback = Callable[[str], None]


class WebSocketApiError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail.get("message") if isinstance(detail, dict) else str(detail))


class WebSocketClosed(RuntimeError):
    pass


class WebSocketConnection:
    def __init__(self, url: str, timeout: float = 15) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._write_lock = threading.Lock()

    def connect(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"Unsupported websocket scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError(f"Invalid websocket URL: {self.url}")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_socket = socket.create_connection((parsed.hostname, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            context = ssl.create_default_context(cafile=certifi.where())
            sock: socket.socket | ssl.SSLSocket = context.wrap_socket(raw_socket, server_hostname=parsed.hostname)
        else:
            sock = raw_socket
        sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("WebSocket handshake closed")
            response += chunk

        header_text = response.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise WebSocketClosed(f"WebSocket handshake failed: {lines[0] if lines else header_text}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected_accept:
            raise WebSocketClosed("WebSocket handshake accept mismatch")

        sock.settimeout(None)
        self._socket = sock

    def close(self) -> None:
        sock = self._socket
        if not sock:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        except WebSocketClosed:
            pass
        self._socket = None
        try:
            sock.close()
        except OSError:
            pass

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x1:
                data = json.loads(payload.decode("utf-8"))
                if isinstance(data, dict):
                    return data
                raise ValueError("WebSocket message must be JSON object")
            if opcode == 0x8:
                raise WebSocketClosed("WebSocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._socket
        if not sock:
            raise WebSocketClosed("WebSocket is not connected")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend(struct.pack("!BH", 0x80 | 126, length))
        else:
            header.extend(struct.pack("!BQ", 0x80 | 127, length))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._write_lock:
            sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if fin:
            return opcode, payload

        chunks = [payload]
        while True:
            next_first, next_second = self._recv_exact(2)
            next_fin = bool(next_first & 0x80)
            next_opcode = next_first & 0x0F
            next_masked = bool(next_second & 0x80)
            next_length = next_second & 0x7F
            if next_length == 126:
                next_length = struct.unpack("!H", self._recv_exact(2))[0]
            elif next_length == 127:
                next_length = struct.unpack("!Q", self._recv_exact(8))[0]
            next_mask = self._recv_exact(4) if next_masked else b""
            next_payload = self._recv_exact(next_length)
            if next_masked:
                next_payload = bytes(byte ^ next_mask[index % 4] for index, byte in enumerate(next_payload))
            if next_opcode != 0x0:
                raise WebSocketClosed("Unexpected fragmented websocket frame")
            chunks.append(next_payload)
            if next_fin:
                return opcode, b"".join(chunks)

    def _recv_exact(self, length: int) -> bytes:
        sock = self._socket
        if not sock:
            raise WebSocketClosed("WebSocket is not connected")
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise WebSocketClosed("WebSocket closed")
            data += chunk
        return data


class ApiClient:
    def __init__(self, server_url: str, token: str | None = None) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.on_profiles_changed: ProfilesCallback | None = None
        self.on_log: LogCallback | None = None
        self._ws: WebSocketConnection | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._responses: dict[str, dict[str, Any]] = {}
        self._reader_thread: threading.Thread | None = None
        self._response_ready = threading.Condition(self._lock)

    def ws_url(self) -> str:
        parsed = urlsplit(self.server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/api/v1/ws", "", ""))

    def set_event_handlers(
        self,
        profiles_changed: ProfilesCallback | None = None,
        log: LogCallback | None = None,
    ) -> None:
        self.on_profiles_changed = profiles_changed
        self.on_log = log

    def start_event_stream(self) -> None:
        threading.Thread(target=self._connect_for_events, daemon=True).start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            ws = self._ws
            self._ws = None
            self._response_ready.notify_all()
        if ws:
            ws.close()

    def get_profiles(self, current_hash: str = "") -> list[dict[str, Any]] | None:
        payload = self._request("profiles.check", {"hash": current_hash}, timeout=20)
        if payload.get("unchanged"):
            return None
        return payload["profiles"]

    def latest_manifest(self, slug: str) -> dict[str, Any]:
        return self._request("profile.latest", {"slug": slug}, timeout=120)

    def azuriom_login(self, email: str, password: str, code: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"email": email, "password": password}
        if code:
            body["code"] = code
        payload = self._request("auth.azuriom.login", body, timeout=45)
        if payload.get("access_token"):
            self.token = payload["access_token"]
        return payload

    def azuriom_verify(self, access_token: str) -> dict[str, Any]:
        return self._request("auth.azuriom.verify", {"access_token": access_token}, timeout=30)

    def azuriom_logout(self, access_token: str) -> dict[str, Any]:
        return self._request("auth.azuriom.logout", {"access_token": access_token}, timeout=30)

    def skin_profile(self, username: str) -> dict[str, Any]:
        return self._request("skin.profile", {"username": username}, timeout=30)

    def upload_skin(self, image: bytes, filename: str = "skin.png") -> dict[str, Any]:
        if not self.token:
            raise ValueError("Login required before uploading skin")
        return self._request(
            "skin.upload",
            {
                "access_token": self.token,
                "filename": filename,
                "image_base64": base64.b64encode(image).decode("ascii"),
            },
            timeout=60,
        )

    def upload_cape(self, image: bytes, filename: str = "cape.png") -> dict[str, Any]:
        if not self.token:
            raise ValueError("Login required before uploading cape")
        return self._request(
            "cape.upload",
            {
                "access_token": self.token,
                "filename": filename,
                "image_base64": base64.b64encode(image).decode("ascii"),
            },
            timeout=60,
        )

    def check_update(
        self,
        current_version: str,
        platform: str | None = None,
        current_update_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "launcher.update",
            {
                "current_version": current_version,
                "current_update_id": current_update_id if current_update_id is not None else build_update_id(),
                "platform": platform or platform_id(),
            },
            timeout=20,
        )

    def _connect_for_events(self) -> None:
        try:
            self._ensure_connected()
        except Exception as exc:
            self._log(f"WebSocket offline: {exc}")

    def _ensure_connected(self) -> None:
        with self._lock:
            if self._closed:
                raise WebSocketClosed("WebSocket client closed")
            if self._ws and self._reader_thread and self._reader_thread.is_alive():
                return
        with self._connect_lock:
            with self._lock:
                if self._closed:
                    raise WebSocketClosed("WebSocket client closed")
                if self._ws and self._reader_thread and self._reader_thread.is_alive():
                    return
            ws = WebSocketConnection(self.ws_url())
            ws.connect()
            reader = threading.Thread(target=self._reader_loop, args=(ws,), daemon=True)
            with self._lock:
                if self._closed:
                    ws.close()
                    raise WebSocketClosed("WebSocket client closed")
                self._ws = ws
                self._reader_thread = reader
            reader.start()
            self._log("WebSocket connected")

    def _reader_loop(self, ws: WebSocketConnection) -> None:
        try:
            while True:
                self._handle_message(ws.recv_json())
        except Exception as exc:
            self._drop_connection(ws, str(exc))

    def _handle_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message.get("type") == "response" and message_id:
            with self._response_ready:
                self._responses[str(message_id)] = message
                self._response_ready.notify_all()
            return

        if message.get("type") in {"hello", "profiles.changed"}:
            profiles = message.get("profiles")
            if isinstance(profiles, list) and self.on_profiles_changed:
                self.on_profiles_changed(profiles)
            if message.get("type") == "profiles.changed" and message.get("reason") not in {"hello", None}:
                self._log("Profiles updated live")

    def _request(self, message_type: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        message = {"id": request_id, "type": message_type, "payload": payload}
        if self.token:
            message["token"] = self.token

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._ensure_connected()
                with self._lock:
                    ws = self._ws
                if not ws:
                    raise WebSocketClosed("WebSocket is not connected")
                ws.send_json(message)
                return self._wait_response(request_id, timeout)
            except (OSError, WebSocketClosed) as exc:
                last_error = exc
                with self._lock:
                    ws = self._ws
                if ws:
                    self._drop_connection(ws, str(exc))
                if attempt == 0:
                    continue
        raise WebSocketClosed(str(last_error) if last_error else "WebSocket request failed")

    def _wait_response(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._response_ready:
            while request_id not in self._responses:
                if self._ws is None:
                    raise WebSocketClosed("WebSocket disconnected")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("WebSocket request timed out")
                self._response_ready.wait(remaining)
            response = self._responses.pop(request_id)
        if response.get("ok"):
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        error = response.get("error") or {}
        status_code = int(error.get("status_code") or 500) if isinstance(error, dict) else 500
        detail = error.get("detail") if isinstance(error, dict) else error
        raise WebSocketApiError(status_code, detail)

    def _drop_connection(self, ws: WebSocketConnection, reason: str) -> None:
        with self._response_ready:
            if self._ws is ws:
                self._ws = None
                self._responses.clear()
                self._response_ready.notify_all()
        ws.close()
        if not self._closed:
            self._log(f"WebSocket disconnected: {reason}")

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)


def absolute_url(server_url: str, value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urljoin(f"{server_url.rstrip('/')}/", value.lstrip("/"))
