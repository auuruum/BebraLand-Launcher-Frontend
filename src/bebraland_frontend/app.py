from __future__ import annotations

import ctypes
import ctypes.wintypes
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QLockFile, QPoint, Property, QObject, QRect, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCursor, QDesktopServices, QIcon
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QVBoxLayout, QWidget

from . import __version__
from .api import ApiClient, WebSocketApiError, absolute_url
from .config import DEFAULT_SERVER_URL, build_update_id, launcher_data_dir, platform_id, update_manifest_url
from .runtime import (
    delete_instance,
    install_mod_loader,
    instance_dir,
    instance_path,
    launch_minecraft,
    offline_installed_version,
    OperationCancelled,
    prepare_reinstall,
    read_old_manifest,
    set_instances_root,
    sync_manifest,
)
from .settings import load_settings, save_settings
from .theme import DEFAULT_BACKGROUND_PATH, GML_ASSETS_DIR, GML_IMAGES_DIR, NEWS_API_URL, register_fonts
from .updater import (
    can_self_replace,
    cleanup_update_cache,
    display_version,
    download_release,
    get_update_release,
    replace_current_exe,
    run_update_helper_from_cli,
)


DEFAULT_RECOMMENDED_RAM_MB = 2048
MIN_RAM_MB = 512
MAX_RAM_MB = 16384
RESERVED_SYSTEM_RAM_MB = 1024
DEFAULT_WINDOW_WIDTH = 900
DEFAULT_WINDOW_HEIGHT = 600
MAX_SKIN_RENDER_BYTES = 5 * 1024 * 1024
WINDOW_RESIZE_MARGIN = 8
GWL_STYLE = -16
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
ERROR_ALREADY_EXISTS = 183
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
QML_MAIN = GML_ASSETS_DIR.parent / "qml" / "Main.qml"
APP_ICON_PATH = GML_IMAGES_DIR / "logo.ico"


def launcher_log_path() -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return launcher_data_dir() / "logs" / f"launcher-{stamp}.log"


def append_log(path: Path, text: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {text}", flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {text}\n")


def strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def profiles_hash(profiles: list[dict[str, Any]]) -> str:
    payload = json.dumps(profiles, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def is_two_factor_required_error(exc: Exception) -> bool:
    if not isinstance(exc, WebSocketApiError):
        return False
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    reason = str(detail.get("reason") or "").lower()
    message = str(detail.get("message") or "").lower()
    details = str(detail.get("details") or "").lower()
    text = " ".join((reason, message, details))
    return (
        reason in {"2fa", "two_factor", "totp"}
        or "2fa" in text
        or "two-factor" in text
        or "two factor" in text
    ) and ("missing" in text or "required" in text or reason in {"2fa", "two_factor", "totp"})


def auth_error_message(exc: Exception) -> str:
    if isinstance(exc, WebSocketApiError) and isinstance(exc.detail, dict):
        message = str(exc.detail.get("message") or exc.detail.get("reason") or exc)
        if "missing" in message.lower() and is_two_factor_required_error(exc):
            return "2FA code required"
        return message
    return str(exc)


def detect_system_ram_mb() -> int | None:
    if sys.platform.startswith("win"):
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys // (1024 * 1024))
        return None

    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            return None
        if isinstance(pages, int) and isinstance(page_size, int):
            return int(pages * page_size // (1024 * 1024))
    return None


def launcher_ram_limit_mb() -> int:
    total = detect_system_ram_mb()
    if not total:
        return MAX_RAM_MB
    return max(MIN_RAM_MB, min(MAX_RAM_MB, total - RESERVED_SYSTEM_RAM_MB))


def clamp_ram_mb(value: Any, maximum: int) -> int:
    try:
        ram_mb = int(value)
    except (TypeError, ValueError):
        ram_mb = DEFAULT_RECOMMENDED_RAM_MB
    return max(MIN_RAM_MB, min(ram_mb, maximum))


def snap_ram_mb(value: Any, maximum: int, step: int = 256) -> int:
    try:
        ram_mb = float(value)
    except (TypeError, ValueError):
        ram_mb = DEFAULT_RECOMMENDED_RAM_MB
    step = max(1, int(step or 256))
    return clamp_ram_mb(round(ram_mb / step) * step, maximum)


def file_url(path: Path) -> str:
    return QUrl.fromLocalFile(str(path)).toString()


def cache_busted_url(url: str, nonce: int) -> str:
    if not nonce or not url.startswith(("http://", "https://")):
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_bl={nonce}"


def profile_asset_cache_path(url: str) -> Path:
    parsed = urlsplit(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = ".img"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return launcher_data_dir() / "cache" / "profile-assets" / f"{digest}{suffix}"


def cached_profile_asset_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return url
    path = profile_asset_cache_path(url)
    return file_url(path) if path.is_file() else url


def download_profile_asset(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    target = profile_asset_cache_path(url)
    if target.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        url,
        headers={"Accept": "image/*", "User-Agent": f"BebraLand Launcher/{__version__}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
    tmp.replace(target)
    return True


def format_post_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    if "+" in text:
        head, tail = text.split("+", 1)
        return f"{head} +{tail}"
    return text


class Bridge(QObject):
    log = Signal(str)
    error = Signal(str)
    profiles = Signal(list)
    profiles_silent = Signal(list)
    auth = Signal(dict)
    two_factor = Signal(str)
    install_update = Signal(dict)
    replace_update = Signal(object)
    progress = Signal(int, int, str)
    progress_done = Signal()
    operation_finished = Signal()
    minecraft_started = Signal(object)
    minecraft_finished = Signal(object)
    news = Signal(list)
    skin_profile = Signal(dict)
    logged_out = Signal(str)
    saved_login_unverified = Signal(str)
    profiles_unavailable = Signal()
    update_notice = Signal(dict)


class LauncherWindow(QWidget):
    stateChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BebraLand Launcher")
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1000, 600)
        self.setMinimumSize(1000, 600)
        self._normal_geometry = QRect(self.geometry())
        self._windows_resize_frame_enabled = False
        register_fonts(self)

        self.settings = load_settings()
        self.client = ApiClient(self.settings.get("server_url", DEFAULT_SERVER_URL), self.settings.get("access_token"))
        self.auth_user: dict[str, Any] | None = self.settings.get("user")
        self.minecraft_profile: dict[str, Any] | None = self.settings.get("minecraft_profile")
        cached_profiles = self.settings.get("cached_profiles")
        self.profiles: list[dict[str, Any]] = cached_profiles if isinstance(cached_profiles, list) else []
        self.selected_profile_slug = str(self.settings.get("selected_profile") or "")
        self.max_ram_mb = launcher_ram_limit_mb()
        self.news: list[dict[str, Any]] = []
        self.skin_profile_payload: dict[str, Any] = {}
        self.skin_cache_nonce = 0
        self.status_text = f"BebraLand Launcher {__version__}"
        self.progress_text = ""
        self.progress_title = ""
        self.progress_details = ""
        self.progress_amount = ""
        self.progress_speed = ""
        self.progress_eta = ""
        self.progress_value = 0
        self.progress_maximum = 100
        self.progress_visible = False
        self.login_status = ""
        self.update_notice: dict[str, Any] = {"visible": False}
        self.two_factor_visible = False
        self._last_login_email = ""
        self._last_login_password = ""
        self._profiles_loaded = bool(self.profiles)
        self._offline_mode = bool(self.profiles)
        self._profiles_revision = 0
        self._auth_verify_pending = bool(self.client.token)
        self._pack_operation_running = False
        self._cancel_event: threading.Event | None = None
        self._minecraft_process: Any | None = None
        self._log_path = launcher_log_path()
        self._log_cleanup_cache: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {}
        append_log(self._log_path, f"BebraLand Launcher {__version__} started")

        self.apply_install_root()
        self.bridge = Bridge()
        self.bridge.log.connect(self.log_line)
        self.bridge.error.connect(self.show_error)
        self.bridge.profiles.connect(self.set_profiles)
        self.bridge.profiles_silent.connect(self.set_profiles_silent)
        self.bridge.auth.connect(self.set_auth)
        self.bridge.two_factor.connect(self.show_two_factor)
        self.bridge.install_update.connect(self.install_update)
        self.bridge.replace_update.connect(replace_current_exe)
        self.bridge.progress.connect(self.set_progress)
        self.bridge.progress_done.connect(self.clear_progress)
        self.bridge.operation_finished.connect(self.finish_pack_operation)
        self.bridge.minecraft_started.connect(self.set_minecraft_process)
        self.bridge.minecraft_finished.connect(self.clear_minecraft_process)
        self.bridge.news.connect(self.set_news)
        self.bridge.skin_profile.connect(self.set_skin_profile)
        self.bridge.logged_out.connect(self.handle_logged_out)
        self.bridge.saved_login_unverified.connect(self.handle_saved_login_unverified)
        self.bridge.profiles_unavailable.connect(self.mark_profiles_unavailable)
        self.bridge.update_notice.connect(self.set_update_notice)

        self.refresh_state()
        self.build_ui()
        self.configure_client_events()
        self.refresh_profiles()
        self.fetch_news()
        if self.client.token:
            self.verify_saved_login()
        self.check_update()

    @Property("QVariant", notify=stateChanged)
    def state(self) -> dict[str, Any]:
        return self._state

    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.quick = QQuickWidget(self)
        self.quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.quick.rootContext().setContextProperty("controller", self)
        self.quick.setSource(QUrl.fromLocalFile(str(QML_MAIN)))
        layout.addWidget(self.quick)
        self.enable_windows_resize_frame()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.enable_windows_resize_frame()

    def enable_windows_resize_frame(self) -> None:
        if not sys.platform.startswith("win") or self._windows_resize_frame_enabled:
            return
        hwnd = int(self.winId())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
        )
        self._windows_resize_frame_enabled = True

    def nativeEvent(self, event_type: bytes, message: int) -> tuple[bool, int]:
        if not sys.platform.startswith("win"):
            return False, 0
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == WM_NCCALCSIZE and msg.wParam:
            return True, 0
        if msg.message != WM_NCHITTEST:
            return False, 0
        if self.isMaximized():
            return False, 0
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        pos = self.mapFromGlobal(QPoint(x, y))
        if pos.x() < 0 or pos.y() < 0 or pos.x() > self.width() or pos.y() > self.height():
            return False, 0
        left = pos.x() < WINDOW_RESIZE_MARGIN
        right = pos.x() >= self.width() - WINDOW_RESIZE_MARGIN
        top = pos.y() < WINDOW_RESIZE_MARGIN
        bottom = pos.y() >= self.height() - WINDOW_RESIZE_MARGIN
        if top and left:
            return True, HTTOPLEFT
        if top and right:
            return True, HTTOPRIGHT
        if bottom and left:
            return True, HTBOTTOMLEFT
        if bottom and right:
            return True, HTBOTTOMRIGHT
        if left:
            return True, HTLEFT
        if right:
            return True, HTRIGHT
        if top:
            return True, HTTOP
        if bottom:
            return True, HTBOTTOM
        return False, 0

    def moveEvent(self, event: Any) -> None:
        super().moveEvent(event)
        self.remember_normal_geometry()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.remember_normal_geometry()

    def remember_normal_geometry(self) -> None:
        if self.isMaximized() or self.isMinimized():
            return
        geometry = self.geometry()
        if geometry.width() > 0 and geometry.height() > 0:
            self._normal_geometry = QRect(geometry)

    def restore_for_title_drag(self, root_x: float, root_y: float) -> None:
        normal_geometry = QRect(self._normal_geometry)
        if normal_geometry.width() <= 0 or normal_geometry.height() <= 0:
            normal_geometry = self.normalGeometry()
        if normal_geometry.width() <= 0 or normal_geometry.height() <= 0:
            normal_geometry = QRect(0, 0, 1000, 600)

        cursor_pos = QCursor.pos()
        screen = QApplication.screenAt(cursor_pos) or self.screen() or QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry() if screen else None
        ratio = max(0.0, min(1.0, root_x / max(1, self.width()))) if root_x >= 0 else 0.5
        title_offset_y = max(0, int(root_y)) if root_y >= 0 else WINDOW_RESIZE_MARGIN
        x = cursor_pos.x() - round(normal_geometry.width() * ratio)
        y = cursor_pos.y() - title_offset_y

        if screen_geometry:
            max_x = screen_geometry.right() - normal_geometry.width() + 1
            max_y = screen_geometry.bottom() - normal_geometry.height() + 1
            x = max(screen_geometry.left(), min(x, max_x))
            y = max(screen_geometry.top(), min(y, max_y))

        self.showNormal()
        QApplication.processEvents()
        self.setGeometry(x, y, normal_geometry.width(), normal_geometry.height())
        QApplication.processEvents()

    def default_install_dir(self) -> Path:
        return launcher_data_dir() / "instances"

    def install_dir(self) -> Path:
        return Path(str(self.settings.get("install_dir") or self.default_install_dir())).expanduser().resolve()

    def apply_install_root(self) -> None:
        set_instances_root(self.install_dir())

    def selected_instance_dir(self) -> Path | None:
        slug = self.selected_profile_slug or self.selected_slug()
        if not slug:
            return None
        try:
            return instance_path(slug)
        except ValueError:
            return None

    def log_cleanup_targets(self) -> list[Path]:
        game_dir = self.selected_instance_dir()
        if not game_dir or not game_dir.exists():
            return []

        targets: list[Path] = []
        for folder_name in ("logs", "crash-reports"):
            folder = game_dir / folder_name
            if folder.exists():
                targets.append(folder)

        for pattern in ("debug.log", "fabricloader.log", "win-event*.txt", "win_event*.txt", "crash-*.txt", "hs_err_pid*.log"):
            targets.extend(path for path in game_dir.glob(pattern) if path.exists())

        return list(dict.fromkeys(targets))

    def log_cleanup_size_bytes(self) -> int:
        total = 0
        for target in self.log_cleanup_targets():
            if target.is_file():
                try:
                    total += target.stat().st_size
                except OSError:
                    pass
                continue
            for file_path in target.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    total += file_path.stat().st_size
                except OSError:
                    pass
        return total

    def log_cleanup_state(self) -> dict[str, Any]:
        game_dir = self.selected_instance_dir()
        cache_key = str(game_dir) if game_dir else ""
        if cache_key and cache_key in self._log_cleanup_cache:
            return self._log_cleanup_cache[cache_key]

        size = self.log_cleanup_size_bytes()
        state = {
            "sizeBytes": size,
            "sizeText": format_bytes(size),
            "hasLogs": size > 0,
            "path": str(game_dir) if game_dir else "",
        }
        if cache_key:
            self._log_cleanup_cache[cache_key] = state
        return state

    def refresh_state(self) -> None:
        profile = self.selected_profile()
        authenticated = bool(self.client.token and self.auth_user)
        bootstrapping = self._auth_verify_pending or (authenticated and not self._profiles_loaded)
        minecraft_running = self.minecraft_running()
        has_selected_profile = profile is not None
        launch_allowed = bool(profile and profile.get("launch_allowed", not bool(profile.get("opening_mode"))))
        self._state = {
            "authenticated": authenticated,
            "offlineMode": self._offline_mode,
            "bootstrapping": bootstrapping,
            "version": __version__,
            "status": self.status_text,
            "progressText": self.progress_text,
            "progressTitle": self.progress_title,
            "progressDetails": self.progress_details,
            "progressAmount": self.progress_amount,
            "progressSpeed": self.progress_speed,
            "progressEta": self.progress_eta,
            "progressValue": self.progress_value,
            "progressMaximum": self.progress_maximum,
            "progressVisible": self.progress_visible,
            "operationRunning": self._pack_operation_running,
            "minecraftRunning": minecraft_running,
            "hasSelectedProfile": has_selected_profile,
            "playDisabled": not has_selected_profile or not launch_allowed or self._pack_operation_running or minecraft_running,
            "canCancelDownload": self._cancel_event is not None and not self._cancel_event.is_set(),
            "defaultBackgroundUrl": file_url(DEFAULT_BACKGROUND_PATH),
            "assetsUrl": file_url(GML_ASSETS_DIR),
            "profiles": [self.public_profile(profile_item) for profile_item in self.profiles],
            "selectedSlug": self.selected_profile_slug,
            "selectedProfile": self.public_profile(profile) if profile else {},
            "ram": self.ram_state(profile),
            "window": self.window_state(),
            "debugConsole": self.debug_console_enabled(),
            "installDir": str(self.install_dir()),
            "logCleanup": self.log_cleanup_state(),
            "optionalMods": self.optional_mod_state(profile),
            "news": self.news,
            "loginStatus": self.login_status,
            "updateNotice": self.update_notice or {"visible": False},
            "twoFactorVisible": self.two_factor_visible,
            "accountName": self.current_username() or "Not logged in",
            "skinBodyUrl": self.skin_body_url(),
        }
        self.stateChanged.emit()

    def minecraft_running(self) -> bool:
        process = self._minecraft_process
        return bool(process is not None and process.poll() is None)

    def set_minecraft_process(self, process: Any) -> None:
        self._minecraft_process = process
        self.showMinimized()
        self.refresh_state()
        threading.Thread(target=self.wait_for_minecraft_exit, args=(process,), daemon=True).start()

    def wait_for_minecraft_exit(self, process: Any) -> None:
        try:
            process.wait()
        except Exception as exc:
            self.bridge.log.emit(f"Minecraft process watch stopped: {exc}")
        self.bridge.minecraft_finished.emit(process)

    def clear_minecraft_process(self, process: Any) -> None:
        if self._minecraft_process is not process:
            return
        self._minecraft_process = None
        self.refresh_state()

    def public_profile(self, profile: dict[str, Any] | None) -> dict[str, Any]:
        if not profile:
            return {}
        return {
            **profile,
            "icon_url": self.absolute_profile_url(profile, "icon_url"),
            "background_url": self.absolute_profile_url(profile, "background_url"),
            "offline": self._offline_mode,
        }

    def absolute_profile_url(self, profile: dict[str, Any], field: str) -> str:
        value = str(profile.get(field) or "").strip()
        return cached_profile_asset_url(absolute_url(self.client.server_url, value)) if value else ""

    def selected_profile(self) -> dict[str, Any] | None:
        for profile in self.profiles:
            if profile.get("slug") == self.selected_profile_slug:
                return profile
        return self.profiles[0] if self.profiles else None

    def selected_slug(self) -> str | None:
        profile = self.selected_profile()
        return str(profile.get("slug")) if profile else None

    def recommended_ram_mb(self, profile: dict[str, Any] | None = None) -> int:
        profile = profile or self.selected_profile()
        if not profile:
            return DEFAULT_RECOMMENDED_RAM_MB
        return clamp_ram_mb(profile.get("recommended_ram_mb", DEFAULT_RECOMMENDED_RAM_MB), self.max_ram_mb)

    def profile_ram_value(self, profile: dict[str, Any] | None) -> int:
        if not profile:
            return DEFAULT_RECOMMENDED_RAM_MB
        overrides = self.settings.setdefault("profile_ram_mb", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self.settings["profile_ram_mb"] = overrides
        return clamp_ram_mb(overrides.get(profile["slug"], profile.get("recommended_ram_mb")), self.max_ram_mb)

    def ram_state(self, profile: dict[str, Any] | None) -> dict[str, Any]:
        value = self.profile_ram_value(profile)
        recommended = self.recommended_ram_mb(profile)
        hint = f"Recommended: {recommended} MB"
        if value < recommended:
            hint = f"Below recommended: {recommended} MB"
        return {"min": MIN_RAM_MB, "max": self.max_ram_mb, "value": value, "recommended": recommended, "hint": hint}

    def window_state(self) -> dict[str, Any]:
        value = self.settings.get("minecraft_window")
        if not isinstance(value, dict):
            value = {}
        return {
            "fullscreen": bool(value.get("fullscreen")),
            "width": int(value.get("width") or DEFAULT_WINDOW_WIDTH),
            "height": int(value.get("height") or DEFAULT_WINDOW_HEIGHT),
        }

    def debug_console_enabled(self) -> bool:
        return bool(self.settings.get("debug_console", False))

    def optional_mods_for_profile(self, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        profile = profile or self.selected_profile()
        if not profile:
            return []
        mods = profile.get("optional_mods") or []
        return mods if isinstance(mods, list) else []

    @staticmethod
    def optional_mod_id(mod: dict[str, Any]) -> str:
        return str(mod.get("id") or "").strip()

    @staticmethod
    def optional_mod_name(mod: dict[str, Any]) -> str:
        return str(mod.get("name") or mod.get("id") or "").strip()

    def optional_mod_settings(self) -> dict[str, Any]:
        value = self.settings.get("profile_optional_mods")
        if not isinstance(value, dict):
            value = {}
            self.settings["profile_optional_mods"] = value
        return value

    def optional_mod_state(self, profile: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not profile:
            return []
        selected = self.selected_optional_mod_ids(profile)
        result = []
        for mod in self.optional_mods_for_profile(profile):
            mod_id = self.optional_mod_id(mod)
            if not mod_id:
                continue
            requires = [str(item) for item in mod.get("requires") or []]
            conflicts = [str(item) for item in mod.get("conflicts") or []]
            description = str(mod.get("description") or "").strip()
            result.append(
                {
                    "id": mod_id,
                    "name": self.optional_mod_name(mod),
                    "description": description,
                    "defaultEnabled": bool(mod.get("default_enabled")),
                    "requires": requires,
                    "requiresText": ", ".join(requires),
                    "conflicts": conflicts,
                    "conflictsText": ", ".join(conflicts),
                    "enabled": mod_id in selected,
                }
            )
        return result

    def resolve_optional_mod_ids(self, mods: list[dict[str, Any]], selected: set[str]) -> set[str]:
        mod_map = {self.optional_mod_id(mod): mod for mod in mods if self.optional_mod_id(mod)}
        resolved = {mod_id for mod_id in selected if mod_id in mod_map}
        changed = True
        while changed:
            changed = False
            for mod_id in list(resolved):
                for required_id in mod_map[mod_id].get("requires") or []:
                    required_id = str(required_id)
                    if required_id in mod_map and required_id not in resolved:
                        resolved.add(required_id)
                        changed = True
        for mod_id in list(resolved):
            for conflict_id in mod_map[mod_id].get("conflicts") or []:
                conflict_id = str(conflict_id)
                if conflict_id in resolved:
                    resolved.discard(conflict_id)
        return resolved

    def remove_optional_mod_with_dependents(
        self,
        mods: list[dict[str, Any]],
        selected: set[str],
        disabled_id: str,
    ) -> set[str]:
        mod_map = {self.optional_mod_id(mod): mod for mod in mods if self.optional_mod_id(mod)}
        disabled = {disabled_id.strip()}
        if not disabled_id.strip():
            return {mod_id for mod_id in selected if mod_id in mod_map}

        changed = True
        while changed:
            changed = False
            for mod_id in list(selected):
                if mod_id in disabled or mod_id not in mod_map:
                    continue
                requires = {str(required_id).strip() for required_id in mod_map[mod_id].get("requires") or []}
                if requires & disabled:
                    disabled.add(mod_id)
                    changed = True

        return {mod_id for mod_id in selected if mod_id in mod_map and mod_id not in disabled}

    def selected_optional_mod_ids(self, profile: dict[str, Any] | None = None) -> set[str]:
        profile = profile or self.selected_profile()
        if not profile:
            return set()
        overrides = self.optional_mod_settings().get(str(profile.get("slug") or ""))
        if not isinstance(overrides, dict):
            overrides = {}
        selected = set()
        for mod in self.optional_mods_for_profile(profile):
            mod_id = self.optional_mod_id(mod)
            if mod_id and bool(overrides.get(mod_id, mod.get("default_enabled"))):
                selected.add(mod_id)
        return self.resolve_optional_mod_ids(self.optional_mods_for_profile(profile), selected)

    def current_username(self) -> str:
        if self.minecraft_profile:
            name = str(self.minecraft_profile.get("name") or "").strip()
            if name:
                return name
        if self.auth_user:
            return str(self.auth_user.get("display_name") or self.auth_user.get("username") or "").strip()
        return ""

    def skin_body_url(self) -> str:
        avatars = self.skin_profile_payload.get("avatars")
        if isinstance(avatars, dict):
            return str(avatars.get("body_url") or "")
        return ""

    def cache_skin_profile_assets(self, payload: dict[str, Any], nonce: int = 0) -> dict[str, Any]:
        avatars = payload.get("avatars")
        if not isinstance(avatars, dict):
            return payload

        body_url = str(avatars.get("body_url") or "").strip()
        if body_url.startswith("file:"):
            return payload

        cached_payload = dict(payload)
        cached_avatars = dict(avatars)
        cached_avatars["remote_body_url"] = body_url
        cached_avatars["body_url"] = ""
        cached_payload["avatars"] = cached_avatars

        cache_dir = launcher_data_dir() / "cache" / "skins"
        cache_dir.mkdir(parents=True, exist_ok=True)

        if not body_url or not body_url.startswith(("http://", "https://")):
            return cached_payload

        try:
            request = urllib.request.Request(
                cache_busted_url(body_url, nonce),
                headers={"Accept": "image/*", "User-Agent": f"BebraLand Launcher/{__version__}"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read(MAX_SKIN_RENDER_BYTES + 1)
        except Exception as exc:
            self.bridge.log.emit(f"Skin render unavailable: {exc}")
            return cached_payload

        if len(data) > MAX_SKIN_RENDER_BYTES:
            self.bridge.log.emit("Skin render unavailable: image too large")
            return cached_payload

        digest = hashlib.sha256(f"{body_url}|{nonce}".encode("utf-8")).hexdigest()
        path = cache_dir / f"{digest}.png"
        path.write_bytes(data)
        cached_avatars["body_url"] = file_url(path)
        return cached_payload

    def run_bg(self, fn: Callable[[], Any], popup: bool = True) -> None:
        def worker() -> None:
            try:
                fn()
            except Exception as exc:
                if popup:
                    self.bridge.error.emit(str(exc))
                else:
                    self.bridge.log.emit(f"Error: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def run_pack_operation(self, fn: Callable[[threading.Event], Any]) -> None:
        if self._pack_operation_running:
            self.log_line("Launcher already working")
            return
        cancel_event = threading.Event()
        self._pack_operation_running = True
        self._cancel_event = cancel_event
        self.refresh_state()

        def task() -> None:
            try:
                fn(cancel_event)
            except OperationCancelled as exc:
                self.bridge.log.emit(str(exc))
                self.bridge.progress_done.emit()
            except Exception as exc:
                self.bridge.error.emit(str(exc))
                self.bridge.progress_done.emit()
            finally:
                self.bridge.operation_finished.emit()

        threading.Thread(target=task, daemon=True).start()

    def finish_pack_operation(self) -> None:
        self._pack_operation_running = False
        self._cancel_event = None
        self.refresh_state()

    def configure_client_events(self) -> None:
        self.client.set_event_handlers(profiles_changed=self.bridge.profiles.emit, log=self.bridge.log.emit)
        self.client.start_event_stream()

    def reset_client(self) -> None:
        server_url = str(self.settings.get("server_url") or DEFAULT_SERVER_URL).strip()
        if server_url.rstrip("/") != self.client.server_url:
            token = self.client.token
            self.client.close()
            self.client = ApiClient(server_url, token)
            self.configure_client_events()
        self.settings["server_url"] = self.client.server_url
        save_settings(self.settings)

    def log_line(self, text: str) -> None:
        append_log(self._log_path, text)
        self.status_text = text
        self.refresh_state()

    def show_error(self, text: str) -> None:
        self.log_line(f"Error: {text}")
        QMessageBox.critical(self, "BebraLand", text)

    def show_two_factor(self, message: str) -> None:
        self.two_factor_visible = True
        self.login_status = message or "2FA code required"
        self.refresh_state()

    def set_progress(self, value: int, maximum: int, label: str) -> None:
        self.progress_visible = True
        self.progress_value = max(0, value)
        self.progress_maximum = max(1, maximum)
        self.progress_text = label
        title, separator, details = label.partition(" - ")
        self.progress_title = title or label
        self.progress_details = details if separator else ""
        detail_parts = self.progress_details.split(" - ") if self.progress_details else []
        self.progress_amount = detail_parts[0] if len(detail_parts) > 0 else ""
        self.progress_speed = detail_parts[1] if len(detail_parts) > 1 else ""
        self.progress_eta = detail_parts[2] if len(detail_parts) > 2 else ""
        self.status_text = self.progress_title or self.status_text
        self.refresh_state()

    def clear_progress(self) -> None:
        self.progress_visible = False
        self.progress_value = 0
        self.progress_maximum = 100
        self.progress_text = ""
        self.progress_title = ""
        self.progress_details = ""
        self.progress_amount = ""
        self.progress_speed = ""
        self.progress_eta = ""
        self.refresh_state()

    def set_profiles(self, profiles: list[dict[str, Any]]) -> None:
        self.apply_profiles(profiles, update_status=True)

    def set_profiles_silent(self, profiles: list[dict[str, Any]]) -> None:
        self.apply_profiles(profiles, update_status=False)

    def apply_profiles(self, profiles: list[dict[str, Any]], update_status: bool) -> None:
        digest = profiles_hash(profiles)
        current_digest = str(self.settings.get("cached_profiles_hash") or "")
        if digest == current_digest and profiles == self.profiles:
            self._profiles_loaded = True
            self._offline_mode = False
            if update_status:
                self.status_text = f"Profiles: {len(profiles)}"
            self.refresh_state()
            return

        self._profiles_loaded = True
        self._offline_mode = False
        self._profiles_revision += 1
        revision = self._profiles_revision
        self.profiles = profiles
        self.settings["cached_profiles"] = profiles
        self.settings["cached_profiles_hash"] = digest
        save_settings(self.settings)
        self.warm_profile_asset_cache(profiles, revision)
        if not self.selected_profile_slug and profiles:
            self.selected_profile_slug = str(profiles[0].get("slug") or "")
        if self.selected_profile_slug and not any(p.get("slug") == self.selected_profile_slug for p in profiles):
            self.selected_profile_slug = str(profiles[0].get("slug") or "") if profiles else ""
        if update_status:
            self.status_text = f"Profiles: {len(profiles)}"
        self.refresh_state()

    def warm_profile_asset_cache(self, profiles: list[dict[str, Any]], revision: int) -> None:
        urls: list[str] = []
        for profile in profiles:
            for field in ("icon_url", "background_url"):
                value = str(profile.get(field) or "").strip()
                if not value:
                    continue
                url = absolute_url(self.client.server_url, value)
                if url.startswith(("http://", "https://")) and not profile_asset_cache_path(url).is_file():
                    urls.append(url)
        if not urls:
            return

        unique_urls = list(dict.fromkeys(urls))

        def task() -> None:
            changed = False
            for url in unique_urls:
                try:
                    changed = download_profile_asset(url) or changed
                except Exception as exc:
                    self.bridge.log.emit(f"Profile asset cache skipped: {exc}")
            if changed and revision == self._profiles_revision:
                self.bridge.profiles_silent.emit(profiles)

        self.run_bg(task)

    def mark_profiles_unavailable(self) -> None:
        self._profiles_loaded = True
        cached_profiles = self.settings.get("cached_profiles")
        if isinstance(cached_profiles, list) and cached_profiles:
            self._offline_mode = True
            self.profiles = cached_profiles
            if not self.selected_profile_slug:
                self.selected_profile_slug = str(cached_profiles[0].get("slug") or "")
            self.status_text = "Backend offline - cached packs available"
        else:
            self._offline_mode = False
            self.status_text = "Backend offline - no cached packs"
        self.refresh_state()

    def set_update_notice(self, payload: dict[str, Any]) -> None:
        self.update_notice = payload if isinstance(payload, dict) else {}
        self.refresh_state()

    def set_news(self, posts: list[dict[str, Any]]) -> None:
        self.news = posts
        self.refresh_state()

    def set_auth(self, payload: dict[str, Any]) -> None:
        self._auth_verify_pending = False
        self.auth_user = payload["user"]
        self.minecraft_profile = payload.get("minecraft_profile") or self.minecraft_profile
        if payload.get("access_token"):
            self.client.token = payload["access_token"]
            self.settings["access_token"] = payload["access_token"]
        self.settings["user"] = self.auth_user
        if self.minecraft_profile:
            self.settings["minecraft_profile"] = self.minecraft_profile
        save_settings(self.settings)
        self.two_factor_visible = False
        self.login_status = ""
        self.status_text = f"Logged in: {self.current_username()}"
        self.refresh_skin_profile()
        self.refresh_profiles(silent=True)
        self.refresh_state()

    def set_skin_profile(self, payload: dict[str, Any]) -> None:
        self.skin_profile_payload = payload
        self.refresh_state()

    def handle_logged_out(self, reason: str = "") -> None:
        self._auth_verify_pending = False
        self.auth_user = None
        self.minecraft_profile = None
        self.client.token = None
        self.settings.pop("access_token", None)
        self.settings.pop("user", None)
        self.settings.pop("minecraft_profile", None)
        save_settings(self.settings)
        self.login_status = reason
        self.refresh_profiles(silent=True)
        self.refresh_state()

    def handle_saved_login_unverified(self, reason: str = "") -> None:
        self._auth_verify_pending = False
        self.status_text = reason or "Backend offline - saved login kept"
        self.mark_profiles_unavailable()

    def refresh_profiles(self, silent: bool = False) -> None:
        self.reset_client()

        def task() -> None:
            try:
                if not silent:
                    self.bridge.log.emit("Load profiles")
                current_hash = profiles_hash(self.profiles) if self.profiles else ""
                profiles = self.client.get_profiles(current_hash)
                if profiles is None:
                    profiles = self.profiles
                if silent:
                    self.bridge.profiles_silent.emit(profiles)
                else:
                    self.bridge.profiles.emit(profiles)
            except Exception:
                if not silent:
                    self.bridge.profiles_unavailable.emit()
                raise

        self.run_bg(task, popup=False)

    def cached_manifest_for_slug(self, slug: str) -> dict[str, Any]:
        game_dir = instance_dir(slug)
        manifest = read_old_manifest(game_dir)
        if not manifest:
            raise RuntimeError("Backend offline and no cached manifest for this pack. Launch once online first.")
        profile = manifest.get("profile")
        if not isinstance(profile, dict) or str(profile.get("slug") or "") != slug:
            raise RuntimeError("Cached manifest does not match selected pack")
        return manifest

    def fetch_news(self) -> None:
        def task() -> None:
            request = urllib.request.Request(NEWS_API_URL, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            posts = []
            if isinstance(payload, list):
                for item in payload[:5]:
                    if not isinstance(item, dict):
                        continue
                    posts.append(
                        {
                            "title": str(item.get("title") or "News"),
                            "description": strip_html(item.get("description") or item.get("content") or ""),
                            "date": format_post_date(item.get("published_at")),
                            "url": str(item.get("url") or ""),
                            "image": str(item.get("image") or ""),
                        }
                    )
            self.bridge.news.emit(posts)

        self.run_bg(task, popup=False)

    def verify_saved_login(self) -> None:
        token = self.client.token
        if not token:
            return

        def task() -> None:
            try:
                payload = self.client.azuriom_verify(token)
            except WebSocketApiError as exc:
                if exc.status_code in {401, 403}:
                    self.bridge.logged_out.emit("Saved login expired")
                    return
                self.bridge.saved_login_unverified.emit(f"Backend auth unavailable - saved login kept: {exc}")
                return
            except Exception as exc:
                self.bridge.saved_login_unverified.emit(f"Backend offline - saved login kept: {exc}")
                return
            payload["access_token"] = token
            self.bridge.auth.emit(payload)

        threading.Thread(target=task, daemon=True).start()

    def check_update(self) -> None:
        manifest_url = update_manifest_url()
        if not manifest_url:
            return

        def task() -> None:
            release = get_update_release(__version__, manifest_url, self.bridge.log.emit, platform_id(), build_update_id())
            if release:
                version = display_version(release)
                self.bridge.log.emit(f"Update available: {version}")
                self.bridge.update_notice.emit(
                    {
                        "visible": True,
                        "title": "Launcher update",
                        "version": version,
                        "status": "Preparing update...",
                        "details": "Keep the launcher open. It will restart automatically.",
                        "phase": "preparing",
                    }
                )
                self.bridge.install_update.emit(release)

        self.run_bg(task, popup=False)

    @Slot()
    def windowMinimize(self) -> None:
        self.showMinimized()

    @Slot()
    def windowMaximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.remember_normal_geometry()
            self.showMaximized()

    @Slot()
    def windowClose(self) -> None:
        self.close()

    @Slot(float, float)
    def startWindowMove(self, root_x: float = -1, root_y: float = -1) -> None:
        handle = self.windowHandle()
        if self.isMaximized():
            self.restore_for_title_drag(root_x, root_y)
            handle = self.windowHandle()
        if handle:
            handle.startSystemMove()

    @Slot(str)
    def openUrl(self, value: str) -> None:
        if value:
            QDesktopServices.openUrl(QUrl(value))

    @Slot(str)
    def selectProfile(self, slug: str) -> None:
        self.selected_profile_slug = slug
        self.settings["selected_profile"] = slug
        save_settings(self.settings)
        self.refresh_state()

    @Slot(float, result=int)
    def roundRam(self, value: float) -> int:
        shift_pressed = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        step = 1024 if shift_pressed else 256
        return snap_ram_mb(value, self.max_ram_mb, step)

    @Slot(int)
    def setRam(self, value: int) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        overrides = self.settings.setdefault("profile_ram_mb", {})
        if not isinstance(overrides, dict):
            overrides = {}
            self.settings["profile_ram_mb"] = overrides
        slug = str(profile["slug"])
        ram_mb = snap_ram_mb(value, self.max_ram_mb)
        if overrides.get(slug) == ram_mb:
            return
        overrides[slug] = ram_mb
        save_settings(self.settings)
        self.refresh_state()

    @Slot(bool, int, int)
    def setWindowSettings(self, fullscreen: bool, width: int, height: int) -> None:
        self.settings["minecraft_window"] = {
            "fullscreen": bool(fullscreen),
            "width": max(320, int(width or DEFAULT_WINDOW_WIDTH)),
            "height": max(240, int(height or DEFAULT_WINDOW_HEIGHT)),
        }
        save_settings(self.settings)
        self.refresh_state()

    @Slot(bool)
    def setDebugConsole(self, enabled: bool) -> None:
        self.settings["debug_console"] = bool(enabled)
        save_settings(self.settings)
        self.refresh_state()

    @Slot()
    def chooseInstallFolder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose install folder", str(self.install_dir()))
        if not path:
            return
        self.settings["install_dir"] = path
        save_settings(self.settings)
        self.apply_install_root()
        self.refresh_state()

    @Slot()
    def openInstallFolder(self) -> None:
        self.install_dir().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.install_dir())))

    @Slot()
    def refreshLogSize(self) -> None:
        game_dir = self.selected_instance_dir()
        if game_dir:
            self._log_cleanup_cache.pop(str(game_dir), None)
        self.refresh_state()

    @Slot()
    def clearSelectedLogs(self) -> None:
        game_dir = self.selected_instance_dir()
        if not game_dir:
            QMessageBox.information(self, "BebraLand logs", "Select a pack first.")
            return

        targets = self.log_cleanup_targets()
        size = self.log_cleanup_size_bytes()
        if not targets or size <= 0:
            self.status_text = "No logs to delete"
            self.refresh_state()
            return

        answer = QMessageBox.question(
            self,
            "BebraLand logs",
            f"Delete {format_bytes(size)} of logs for this pack?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        root = game_dir.resolve()
        for target in targets:
            try:
                resolved = target.resolve()
                if resolved != root and root not in resolved.parents:
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                self.show_error(f"Failed to delete logs: {exc}")
                return

        self.status_text = f"Deleted logs: {format_bytes(size)}"
        self._log_cleanup_cache.pop(str(root), None)
        self.refresh_state()

    @Slot(str, str, str)
    def login(self, email: str, password: str, code: str = "") -> None:
        email = email.strip()
        code = code.strip()
        if not email or not password:
            self.login_status = "Login and password required"
            self.refresh_state()
            return
        if self.two_factor_visible and not code:
            self.login_status = "Enter 2FA code"
            self.refresh_state()
            return
        self._last_login_email = email
        self._last_login_password = password
        self.reset_client()

        def task() -> None:
            try:
                payload = self.client.azuriom_login(email, password, code or None)
            except Exception as exc:
                if not code and is_two_factor_required_error(exc):
                    message = auth_error_message(exc) or "2FA code required"
                    self.bridge.log.emit(message)
                    self.bridge.two_factor.emit(message)
                    return
                raise
            if payload.get("status") == "pending":
                message = payload.get("message") or "2FA code required"
                self.bridge.log.emit(message)
                self.bridge.two_factor.emit(message)
                return
            self.bridge.auth.emit(payload)

        self.login_status = "Logging in..."
        self.refresh_state()
        self.run_bg(task)

    @Slot(str)
    def confirm2fa(self, code: str) -> None:
        self.login(self._last_login_email, self._last_login_password, code)

    @Slot()
    def logout(self) -> None:
        token = self.client.token

        def task() -> None:
            if token:
                try:
                    self.client.azuriom_logout(token)
                except Exception:
                    pass
            self.bridge.logged_out.emit("Logged out")

        self.run_bg(task, popup=False)

    @Slot()
    def refreshSkin(self) -> None:
        self.refresh_skin_profile(force=True)

    def refresh_skin_profile(self, force: bool = False) -> None:
        username = self.current_username()
        if not username:
            return
        if force:
            self.skin_cache_nonce = time.time_ns()
        nonce = self.skin_cache_nonce

        def task() -> None:
            payload = self.client.skin_profile(username)
            self.bridge.skin_profile.emit(self.cache_skin_profile_assets(payload, nonce))

        self.run_bg(task, popup=False)

    @Slot(str)
    def uploadTexture(self, texture_type: str) -> None:
        if not self.client.token or not self.auth_user:
            QMessageBox.warning(self, "BebraLand", "Login to BebraLand first")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose PNG", str(Path.home()), "PNG images (*.png)")
        if not path:
            return
        file_path = Path(path)

        def task() -> None:
            image = file_path.read_bytes()
            if texture_type == "cape":
                self.client.upload_cape(image, file_path.name)
            else:
                self.client.upload_skin(image, file_path.name)
            self.bridge.log.emit(f"Uploaded {texture_type}: {file_path.name}")
            self.refresh_skin_profile(force=True)

        self.run_bg(task)

    @Slot(str, bool)
    def toggleOptionalMod(self, mod_id: str, checked: bool) -> None:
        mod_id = str(mod_id).strip()
        if not mod_id:
            return
        profile = self.selected_profile()
        if not profile:
            return
        mods = self.optional_mods_for_profile(profile)
        selected = self.selected_optional_mod_ids(profile)
        if checked:
            selected.add(mod_id)
            selected = self.resolve_optional_mod_ids(mods, selected)
        else:
            selected = self.remove_optional_mod_with_dependents(mods, selected, mod_id)
        slug = str(profile.get("slug") or "")
        self.optional_mod_settings()[slug] = {
            self.optional_mod_id(mod): self.optional_mod_id(mod) in selected
            for mod in mods
            if self.optional_mod_id(mod)
        }
        save_settings(self.settings)
        self.refresh_state()

    @Slot()
    def launchSelected(self) -> None:
        if self._pack_operation_running:
            self.log_line("Launcher already working")
            return
        if self.minecraft_running():
            self.log_line("Minecraft already running")
            return
        slug = self.selected_slug()
        if not slug:
            QMessageBox.warning(self, "BebraLand", "Choose pack first")
            return
        if not self.client.token or not self.auth_user:
            QMessageBox.warning(self, "BebraLand", "Login to BebraLand first")
            return
        profile = self.selected_profile()
        if profile and not bool(profile.get("launch_allowed", not bool(profile.get("opening_mode")))):
            QMessageBox.information(
                self,
                "BebraLand Opening Mode",
                "This pack is in Opening Mode. Files can be downloaded, but launch is available only to launcher admins.",
            )
            return
        ram_mb = self.profile_ram_value(profile)
        recommended = self.recommended_ram_mb(profile)
        if ram_mb < recommended:
            answer = QMessageBox.question(
                self,
                "BebraLand RAM",
                f"Selected RAM ({ram_mb} MB) is below recommended ({recommended} MB). Launch anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        selected_optional_mod_ids = self.selected_optional_mod_ids(profile)

        def task(cancel_event: threading.Event) -> None:
            offline_launch = False
            try:
                self.bridge.log.emit(f"Fetch manifest {slug}")
                manifest = self.client.latest_manifest(slug)
                self._offline_mode = False
            except Exception as exc:
                self.bridge.log.emit(f"Backend offline, try cached pack: {exc}")
                manifest = self.cached_manifest_for_slug(slug)
                offline_launch = True
                self._offline_mode = True
            if cancel_event.is_set():
                raise OperationCancelled("Download cancelled")
            game_dir = instance_dir(manifest["profile"]["slug"])
            if offline_launch:
                installed_version = offline_installed_version(manifest, self.bridge.log.emit)
                self.bridge.log.emit("Offline launch uses cached pack files; no sync")
            else:
                installed_version = install_mod_loader(manifest, game_dir, self.bridge.log.emit, self.bridge.progress.emit)
                if cancel_event.is_set():
                    raise OperationCancelled("Download cancelled")
                game_dir = sync_manifest(
                    manifest,
                    self.client.server_url,
                    self.bridge.log.emit,
                    self.bridge.progress.emit,
                    selected_optional_mod_ids=selected_optional_mod_ids,
                    cancelled=cancel_event.is_set,
                )
                if cancel_event.is_set():
                    raise OperationCancelled("Download cancelled")
            process = launch_minecraft(
                manifest,
                game_dir,
                self.current_username() or "BebraPlayer",
                self.bridge.log.emit,
                self.bridge.progress.emit,
                installed_version=installed_version,
                ram_mb=ram_mb,
                server_url=self.client.server_url,
                access_token=self.client.token,
                minecraft_profile=self.minecraft_profile,
                window_settings=self.window_state(),
                debug_console=self.debug_console_enabled(),
            )
            self.bridge.minecraft_started.emit(process)
            self.bridge.progress_done.emit()

        self.run_pack_operation(task)

    @Slot()
    def reinstallSelected(self) -> None:
        if self._pack_operation_running:
            self.log_line("Launcher already working")
            return
        slug = self.selected_slug()
        if not slug:
            return
        profile = self.selected_profile() or {}
        name = profile.get("name") or slug
        answer = QMessageBox.question(
            self,
            "BebraLand reinstall",
            f"Reinstall local pack '{name}'?\n\nUser data stays. Shared Minecraft/authlib cache stays. Managed pack files download again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        selected_optional_mod_ids = self.selected_optional_mod_ids(profile)

        def task(cancel_event: threading.Event) -> None:
            manifest = self.client.latest_manifest(slug)
            if cancel_event.is_set():
                raise OperationCancelled("Download cancelled")
            game_dir = prepare_reinstall(manifest, self.bridge.log.emit, selected_optional_mod_ids=selected_optional_mod_ids)
            install_mod_loader(manifest, game_dir, self.bridge.log.emit, self.bridge.progress.emit)
            if cancel_event.is_set():
                raise OperationCancelled("Download cancelled")
            sync_manifest(
                manifest,
                self.client.server_url,
                self.bridge.log.emit,
                self.bridge.progress.emit,
                selected_optional_mod_ids=selected_optional_mod_ids,
                cancelled=cancel_event.is_set,
            )
            self.bridge.log.emit(f"Reinstalled {slug}")
            self.bridge.progress_done.emit()

        self.run_pack_operation(task)

    @Slot()
    def cancelDownload(self) -> None:
        if not self._cancel_event or self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.log_line("Cancelling download...")
        self.refresh_state()

    @Slot()
    def deleteSelected(self) -> None:
        slug = self.selected_slug()
        if not slug:
            return
        profile = self.selected_profile() or {}
        name = profile.get("name") or slug
        path = instance_path(slug)
        answer = QMessageBox.question(
            self,
            "BebraLand delete",
            f"Delete local pack '{name}' from this computer?\n\n{path}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def task() -> None:
            delete_instance(slug, self.bridge.log.emit)

        self.run_bg(task)

    def install_update(self, release: dict[str, Any]) -> None:
        def task() -> None:
            version = display_version(release)
            self.bridge.log.emit(f"Install launcher update {version}")
            self.bridge.update_notice.emit(
                {
                    "visible": True,
                    "title": "Launcher update",
                    "version": version,
                    "status": "Downloading update...",
                    "details": "The launcher will restart after the download finishes.",
                    "phase": "downloading",
                }
            )
            downloaded = download_release(release, self.bridge.log.emit)
            if can_self_replace():
                self.bridge.update_notice.emit(
                    {
                        "visible": True,
                        "title": "Launcher update",
                        "version": version,
                        "status": "Restarting to apply update...",
                        "details": "The window will close for a moment and open again.",
                        "phase": "restarting",
                    }
                )
                self.bridge.log.emit("Restart launcher to apply update")
                time.sleep(2)
                self.bridge.replace_update.emit(downloaded)
            else:
                self.bridge.update_notice.emit(
                    {
                        "visible": True,
                        "title": "Launcher update downloaded",
                        "version": version,
                        "status": "Update downloaded",
                        "details": "Dev mode cannot replace the running launcher automatically.",
                        "phase": "ready",
                    }
                )
                self.bridge.log.emit("Run downloaded launcher manually in dev mode")

        self.run_bg(task)

    def closeEvent(self, event: Any) -> None:
        self.client.close()
        super().closeEvent(event)


def main() -> None:
    if run_update_helper_from_cli():
        return
    cleanup_update_cache()
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    instance_mutex = None
    instance_lock: QLockFile | None = None
    if sys.platform.startswith("win"):
        instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "BebraLandLauncher.SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            QMessageBox.information(None, "BebraLand Launcher", "BebraLand Launcher is already running.")
            if instance_mutex:
                ctypes.windll.kernel32.CloseHandle(instance_mutex)
            return
    else:
        lock_path = launcher_data_dir() / "BebraLandLauncher.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(lock_path))
        if not instance_lock.tryLock(100):
            QMessageBox.information(None, "BebraLand Launcher", "BebraLand Launcher is already running.")
            return
    window = LauncherWindow()
    window.show()
    try:
        sys.exit(app.exec())
    finally:
        if instance_lock:
            instance_lock.unlock()
        if instance_mutex:
            ctypes.windll.kernel32.CloseHandle(instance_mutex)
