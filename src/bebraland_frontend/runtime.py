from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import minecraft_launcher_lib
import requests

from .api import absolute_url
from .config import launcher_data_dir


Status = Callable[[str], None]
Progress = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]
PROGRESS_SCALE = 10_000
SYSTEM_PROTECTED_LOCAL_PATTERNS = [
    ".bebraland/**",
    "assets/**",
    "libraries/**",
    "versions/**",
    "runtime/**",
    "runtimes/**",
]
USER_PROTECTED_LOCAL_PATTERNS = [
    "saves/**",
    "screenshots/**",
    "resourcepacks/**",
    "shaderpacks/**",
    "logs/**",
    "crash-reports/**",
    "replay_recordings/**",
    "options*.txt",
    "servers.dat",
    "servers.dat_old",
    "usercache.json",
    "launcher_profiles.json",
    "launcher_accounts.json",
]
PROTECTED_LOCAL_PATTERNS = SYSTEM_PROTECTED_LOCAL_PATTERNS + USER_PROTECTED_LOCAL_PATTERNS
REINSTALL_SYSTEM_PATHS = [
    "assets",
    "libraries",
    "versions",
    "runtime",
    "runtimes",
]
SHARED_MINECRAFT_DIR_NAME = ".shared"
AUTHLIB_ARTIFACT_URL = "https://authlib-injector.yushi.moe/artifact/latest.json"
DISABLED_ENV_VALUES = {"0", "false", "no", "off", "disabled"}
_instances_root_override: Path | None = None


class OperationCancelled(RuntimeError):
    pass


def check_cancel(cancelled: CancelCheck | None) -> None:
    if cancelled and cancelled():
        raise OperationCancelled("Download cancelled")


def set_instances_root(path: str | Path | None) -> None:
    global _instances_root_override
    if not path:
        _instances_root_override = None
        return
    _instances_root_override = Path(path).expanduser().resolve()


def format_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = max(0.0, float(value))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds + 0.5))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds_part = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds_part:02d}s"
    hours, minutes_part = divmod(minutes, 60)
    return f"{hours}h {minutes_part:02d}m"


def format_eta(remaining: float, speed: float) -> str | None:
    if remaining <= 0 or speed <= 0:
        return None
    seconds = remaining / speed
    if seconds < 1:
        return "ETA <1s"
    return f"ETA {format_duration(seconds)}"


def scaled_progress(value: int, maximum: int) -> tuple[int, int]:
    if maximum <= 0:
        return max(0, value), maximum
    return int(max(0, min(value, maximum)) / maximum * PROGRESS_SCALE), PROGRESS_SCALE


class RateMeter:
    def __init__(self) -> None:
        self.reset()

    def reset(self, value: int = 0) -> None:
        now = time.monotonic()
        self._start_time = now
        self._start_value = value
        self._last_time = now
        self._last_value = value
        self.speed = 0.0

    def update(self, value: int) -> float:
        now = time.monotonic()
        elapsed = now - self._last_time
        delta = value - self._last_value
        total_elapsed = now - self._start_time
        total_delta = value - self._start_value
        if elapsed > 0 and delta > 0 and total_elapsed >= 0.2:
            average_speed = total_delta / total_elapsed if total_delta > 0 else 0.0
            current_speed = delta / elapsed if elapsed >= 0.05 else average_speed
            self.speed = current_speed if self.speed <= 0 else (self.speed * 0.7) + (current_speed * 0.3)
            if average_speed > 0:
                self.speed = min(self.speed, average_speed * 2.5)
        self._last_time = now
        self._last_value = value
        return self.speed


class ByteProgressTracker:
    def __init__(self, total_bytes: int) -> None:
        self.total_bytes = max(0, total_bytes)
        self.completed_bytes = 0
        self.active_bytes: dict[str, int] = {}
        self.rate = RateMeter()
        self.lock = threading.Lock()

    def emit(
        self,
        progress: Progress | None,
        file_done: int,
        file_total: int,
        label: str,
        file_key: str | None = None,
    ) -> None:
        if not progress:
            return
        with self.lock:
            if file_key:
                self.active_bytes[file_key] = max(0, file_done)
                active_total = sum(self.active_bytes.values())
            else:
                active_total = max(0, file_done)
            done = self.completed_bytes + active_total
            maximum = self.total_bytes or self.completed_bytes + max(0, file_total)
            self.rate.update(done)
            speed = self.rate.speed
        parts = [label]
        if maximum > 0:
            parts.append(f"{format_bytes(done)} / {format_bytes(maximum)}")
        else:
            parts.append(format_bytes(done))
        if speed > 0:
            parts.append(f"{format_bytes(speed)}/s")
            eta = format_eta(maximum - done, speed) if maximum > 0 else None
            if eta:
                parts.append(eta)
        value, max_value = scaled_progress(done, maximum)
        progress(value, max_value, " - ".join(parts))

    def finish_file(self, downloaded_bytes: int, file_key: str | None = None) -> None:
        with self.lock:
            if file_key:
                self.active_bytes.pop(file_key, None)
            self.completed_bytes += max(0, downloaded_bytes)
            self.rate.update(self.completed_bytes + sum(self.active_bytes.values()))


class CountingRaw:
    def __init__(self, raw: Any, on_bytes: Callable[[int], None]) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_on_bytes", on_bytes)

    def read(self, *args: Any, **kwargs: Any) -> Any:
        data = self._raw.read(*args, **kwargs)
        if data:
            self._on_bytes(len(data))
        return data

    def readinto(self, buffer: Any) -> Any:
        count = self._raw.readinto(buffer)
        if count:
            self._on_bytes(int(count))
        return count

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        for chunk in self._raw.stream(*args, **kwargs):
            if chunk:
                self._on_bytes(len(chunk))
            yield chunk

    def __iter__(self) -> Iterator[Any]:
        return iter(self._raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_raw", "_on_bytes"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._raw, name, value)


@contextmanager
def track_streamed_request_bytes(on_bytes: Callable[[int], None]) -> Iterator[None]:
    original_get = requests.get
    original_session_get = requests.sessions.Session.get

    def wrap_response(response: requests.Response) -> requests.Response:
        raw = getattr(response, "raw", None)
        if raw is not None and not isinstance(raw, CountingRaw):
            response.raw = CountingRaw(raw, on_bytes)
        return response

    def tracked_get(*args: Any, **kwargs: Any) -> requests.Response:
        response = original_get(*args, **kwargs)
        return wrap_response(response) if kwargs.get("stream") else response

    def tracked_session_get(session: requests.sessions.Session, *args: Any, **kwargs: Any) -> requests.Response:
        response = original_session_get(session, *args, **kwargs)
        return wrap_response(response) if kwargs.get("stream") else response

    requests.get = tracked_get
    requests.sessions.Session.get = tracked_session_get
    try:
        yield
    finally:
        requests.get = original_get
        requests.sessions.Session.get = original_session_get


class MinecraftInstallProgress:
    def __init__(self, status: Status, progress: Progress | None) -> None:
        self.status = status
        self.progress = progress
        self.lock = threading.Lock()
        self.phase = "Install Minecraft"
        self.current = 0
        self.maximum = 0
        self.downloaded_bytes = 0
        self.unit_rate = RateMeter()
        self.byte_rate = RateMeter()
        self.last_emit = 0.0

    def set_status(self, text: str) -> None:
        is_phase = self._is_phase_status(text)
        with self.lock:
            if is_phase:
                self.phase = text
        if is_phase or not self.progress:
            self.status(text)

    def set_max(self, value: int) -> None:
        with self.lock:
            self.maximum = max(0, int(value))
            self.current = 0
            self.unit_rate.reset(0)
            label = self._label_locked()
            current = self.current
            maximum = self.maximum
        self._emit(current, maximum, label)

    def set_progress(self, value: int) -> None:
        with self.lock:
            self.current = max(0, int(value))
            self.unit_rate.update(self.current)
            label = self._label_locked()
            current = self.current
            maximum = self.maximum
        self._emit(current, maximum, label)

    def add_downloaded_bytes(self, count: int) -> None:
        if count <= 0 or not self.progress:
            return
        now = time.monotonic()
        with self.lock:
            self.downloaded_bytes += count
            self.byte_rate.update(self.downloaded_bytes)
            if now - self.last_emit < 0.3:
                return
            self.last_emit = now
            label = self._label_locked()
            current = self.current
            maximum = self.maximum
        self._emit(current, maximum, label)

    def _emit(self, value: int, maximum: int, label: str) -> None:
        if self.progress:
            self.progress(value, maximum, label)

    def _label_locked(self) -> str:
        parts = [self.phase]
        if self.maximum > 0:
            parts.append(f"{self.current}/{self.maximum} files")
        if self.byte_rate.speed > 0:
            parts.append(f"{format_bytes(self.byte_rate.speed)}/s")
        elif self.unit_rate.speed > 0:
            parts.append(f"{self.unit_rate.speed:.1f} files/s")
        eta = format_eta(self.maximum - self.current, self.unit_rate.speed) if self.maximum > 0 else None
        if eta:
            parts.append(eta)
        return " - ".join(parts)

    @staticmethod
    def _is_phase_status(text: str) -> bool:
        return (
            text in {"Download Libraries", "Download Assets", "Install java runtime", "Installation complete"}
            or text.startswith("Running ")
            or text.startswith("Extract ")
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_system_protected(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in PROTECTED_LOCAL_PATTERNS)


def matches_pattern(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/").strip("/")
    if normalized_pattern in {"*", "**", "**/*"}:
        return True
    if not any(char in normalized_pattern for char in "*?[]"):
        return normalized == normalized_pattern or normalized.startswith(f"{normalized_pattern}/")
    return fnmatch.fnmatch(normalized, normalized_pattern)


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(matches_pattern(path, pattern) for pattern in patterns)


def sync_mode_for(path: str, rules: dict[str, Any]) -> str:
    blacklist = rules.get("blacklist") or []
    whitelist = rules.get("whitelist") or []
    if matches_any(path, blacklist):
        return "enforce"
    if matches_any(path, whitelist):
        return "seed"
    return "enforce"


def selected_manifest_files(
    manifest: dict[str, Any],
    selected_optional_mod_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    files = list(manifest.get("files") or [])
    if selected_optional_mod_ids is None:
        return files, set()

    selected = {str(mod_id) for mod_id in selected_optional_mod_ids}
    included: list[dict[str, Any]] = []
    disabled_removable: set[str] = set()
    for item in files:
        rel = str(item.get("path") or "").replace("\\", "/")
        optional_mod = str(item.get("optional_mod") or "")
        if optional_mod and optional_mod not in selected:
            if rel and not item.get("optional_keep_on_disable"):
                disabled_removable.add(rel)
            continue
        included.append(item)
    return included, disabled_removable


def instances_root() -> Path:
    path = _instances_root_override or (launcher_data_dir() / "instances")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def instance_path(slug: str) -> Path:
    root = instances_root()
    path = root / slug
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Invalid instance slug: {slug}")
    return path


def instance_dir(slug: str) -> Path:
    path = instance_path(slug)
    path.mkdir(parents=True, exist_ok=True)
    return path


def shared_minecraft_dir() -> Path:
    override = os.environ.get("BEBRALAND_SHARED_MINECRAFT_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        path = (instances_root() / SHARED_MINECRAFT_DIR_NAME / "minecraft").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_instance_path(game_dir: Path, relative_path: str) -> Path:
    root = game_dir.resolve()
    path = game_dir / relative_path
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Invalid local pack path: {relative_path}")
    return path


def state_dir(game_dir: Path) -> Path:
    path = game_dir / ".bebraland"
    path.mkdir(parents=True, exist_ok=True)
    return path


def installed_version_id(profile: dict[str, Any]) -> str:
    loader_id = profile["mod_loader"].lower()
    minecraft_version = profile["minecraft_version"]
    loader_version = profile.get("loader_version") or None
    if loader_id in {"vanilla", "minecraft", "none"}:
        return minecraft_version
    if not loader_version:
        raise ValueError(f"Loader version required for {loader_id}")
    mod_loader = minecraft_launcher_lib.mod_loader.get_mod_loader(loader_id)
    return mod_loader.get_installed_version(minecraft_version, loader_version)


def version_is_installed(minecraft_dir: Path, version_id: str) -> bool:
    return (minecraft_dir / "versions" / version_id / f"{version_id}.json").is_file()


def system_java_enabled() -> bool:
    value = os.environ.get("BEBRALAND_USE_SYSTEM_JAVA", "1").strip().lower()
    return value not in DISABLED_ENV_VALUES


def java_home_from_path(value: str | os.PathLike) -> Path:
    path = Path(value).expanduser()
    name = path.name.lower()
    if name in {"java", "java.exe", "javaw", "javaw.exe"}:
        return path.parent.parent
    if name == "bin":
        return path.parent
    return path


def java_major_from_version(version: Any) -> int | None:
    text = str(version or "").replace("_", ".")
    parts: list[int] = []
    for part in text.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    if not parts:
        return None
    if parts[0] == 1 and len(parts) > 1:
        return parts[1]
    return parts[0]


def java_parent_search_dirs() -> list[Path]:
    result: list[Path] = []
    for env_name in ("BEBRALAND_JAVA_SEARCH_DIRS", "BEBRALAND_JAVA_SEARCH_DIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            result.extend(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())

    for env_name in ("JAVA_HOME", "BEBRALAND_JAVA_HOME", "BEBRALAND_JAVA_PATH"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            result.append(java_home_from_path(raw).parent)

    if os.name == "nt":
        vendor_dirs = [
            "Java",
            "Eclipse Adoptium",
            "Microsoft",
            "BellSoft",
            "Zulu",
            "Amazon Corretto",
            "RedHat",
            "Semeru",
        ]
        for base_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(base_name, "").strip()
            if base:
                result.extend(Path(base) / vendor for vendor in vendor_dirs)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in result:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def candidate_java_homes() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("BEBRALAND_JAVA_PATH", "BEBRALAND_JAVA_HOME", "JAVA_HOME"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(java_home_from_path(raw))

    path_java = shutil.which("java")
    if path_java:
        candidates.append(java_home_from_path(path_java))

    try:
        found = minecraft_launcher_lib.java_utils.find_system_java_versions(
            additional_directories=java_parent_search_dirs(),
        )
    except Exception:
        found = []
    candidates.extend(Path(item) for item in found)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def required_java_major(profile: dict[str, Any], minecraft_dir: Path) -> int | None:
    minecraft_version = str(profile["minecraft_version"])
    information = minecraft_launcher_lib.runtime.get_version_runtime_information(minecraft_version, str(minecraft_dir))
    if not information:
        return None
    return int(information["javaMajorVersion"])


def select_system_java(profile: dict[str, Any], minecraft_dir: Path, status: Status) -> dict[str, Any] | None:
    if not system_java_enabled():
        return None

    try:
        required_major = required_java_major(profile, minecraft_dir)
    except Exception as exc:
        status(f"System Java check skipped: {exc}")
        return None
    if not required_major:
        return None

    for home in candidate_java_homes():
        try:
            information = minecraft_launcher_lib.java_utils.get_java_information(str(home))
        except Exception:
            continue
        major = java_major_from_version(information.get("version"))
        if major != required_major:
            continue
        if not bool(information.get("is_64bit")):
            continue
        java_path = Path(str(information.get("java_path") or ""))
        if not java_path.is_file():
            continue
        status(f"Use system Java {required_major}: {java_path}")
        return dict(information)

    return None


@contextmanager
def skip_mojang_java_runtime_when_system_java(java_info: dict[str, Any] | None, status: Status) -> Iterator[None]:
    if not java_info:
        yield
        return

    original_install_jvm_runtime = minecraft_launcher_lib.install.install_jvm_runtime
    java_version = java_major_from_version(java_info.get("version"))

    def skip_jvm_runtime(
        jvm_version: str,
        minecraft_directory: str | os.PathLike,
        callback: dict[str, Callable[..., Any]] | None = None,
        max_workers: int | None = None,
    ) -> None:
        del minecraft_directory, max_workers
        label = f"Use system Java {java_version}, skip Mojang runtime {jvm_version}"
        if callback and callback.get("setStatus"):
            callback["setStatus"](label)
        else:
            status(label)

    minecraft_launcher_lib.install.install_jvm_runtime = skip_jvm_runtime
    try:
        yield
    finally:
        minecraft_launcher_lib.install.install_jvm_runtime = original_install_jvm_runtime


def copy_missing_tree(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    copied = 0
    if source.is_symlink() or source.is_file():
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        return copied

    for item in sorted(source.rglob("*")):
        rel = item.relative_to(source)
        dest = target / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        copied += 1
    return copied


def seed_shared_minecraft_cache(minecraft_dir: Path, current_game_dir: Path, status: Status) -> int:
    root = instances_root()
    shared_root = (root / SHARED_MINECRAFT_DIR_NAME).resolve()
    candidates = [current_game_dir]
    if root.exists():
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir() or candidate.name == SHARED_MINECRAFT_DIR_NAME:
                continue
            if candidate.resolve() == current_game_dir.resolve():
                continue
            candidates.append(candidate)

    copied = 0
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == shared_root or shared_root in resolved.parents:
            continue
        for name in REINSTALL_SYSTEM_PATHS:
            copied += copy_missing_tree(candidate / name, minecraft_dir / name)
    if copied:
        status(f"Migrated old per-pack Minecraft cache to shared cache: {copied} files")
    return copied


def read_old_manifest(game_dir: Path) -> dict[str, Any] | None:
    path = state_dir(game_dir) / "manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(game_dir: Path, manifest: dict[str, Any]) -> None:
    path = state_dir(game_dir) / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def remove_local_path(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return 1
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    return 0


def remove_empty_parents(path: Path, stop: Path) -> None:
    stop_resolved = stop.resolve()
    current = path.parent
    while current.resolve() != stop_resolved and stop_resolved in current.resolve().parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def delete_instance(slug: str, status: Status) -> None:
    game_dir = instance_path(slug)
    if not game_dir.exists():
        status(f"Local pack already deleted: {slug}")
        return
    remove_local_path(game_dir)
    status(f"Deleted local pack: {slug}")


def cleanup_legacy_instance_runtime(game_dir: Path, status: Status) -> int:
    removed = 0
    for name in REINSTALL_SYSTEM_PATHS:
        removed += remove_local_path(game_dir / name)
    if removed:
        status(f"Removed old per-pack Minecraft cache: {removed}")
    return removed


def prepare_reinstall(
    manifest: dict[str, Any],
    status: Status,
    selected_optional_mod_ids: set[str] | None = None,
) -> Path:
    profile = manifest["profile"]
    game_dir = instance_dir(profile["slug"])
    rules = manifest.get("rules") or {}
    manifest_files, disabled_optional = selected_manifest_files(manifest, selected_optional_mod_ids)
    removed = 0

    status(f"Clean local pack cache for {profile['slug']}")
    removed += cleanup_legacy_instance_runtime(game_dir, status)

    for item in manifest_files:
        rel = str(item["path"]).replace("\\", "/")
        mode = item.get("mode") or sync_mode_for(rel, rules)
        if mode == "seed" or is_system_protected(rel):
            continue
        target = local_instance_path(game_dir, rel)
        removed += remove_local_path(target)
        remove_empty_parents(target, game_dir)
    for rel in disabled_optional:
        if is_system_protected(rel):
            continue
        target = local_instance_path(game_dir, rel)
        removed += remove_local_path(target)
        remove_empty_parents(target, game_dir)

    manifest_path = game_dir / ".bebraland" / "manifest.json"
    removed += remove_local_path(manifest_path)
    status(f"Reinstall cleanup done: removed={removed}")
    return game_dir


def download_file(
    url: str,
    dest: Path,
    expected_sha256: str,
    status: Status,
    progress: Progress | None = None,
    progress_tracker: ByteProgressTracker | None = None,
    progress_label: str | None = None,
    progress_key: str | None = None,
    cancelled: CancelCheck | None = None,
) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    label = progress_label or f"Download {dest.name}"
    if not progress_tracker or not progress:
        status(label)
    check_cancel(cancelled)
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        if progress_tracker:
            progress_tracker.emit(progress, done, total, label, progress_key)
        try:
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    check_cancel(cancelled)
                    if chunk:
                        handle.write(chunk)
                        done += len(chunk)
                        if progress_tracker:
                            progress_tracker.emit(progress, done, total, label, progress_key)
                        elif progress and total:
                            detail = f"{label} - {format_bytes(done)} / {format_bytes(total)}"
                            progress(done, total, detail)
        except OperationCancelled:
            tmp.unlink(missing_ok=True)
            raise
    check_cancel(cancelled)
    actual = sha256_file(tmp)
    if actual != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Hash mismatch for {dest}: {actual} != {expected_sha256}")
    tmp.replace(dest)
    if progress_tracker:
        progress_tracker.finish_file(done, progress_key)


def cleanup_extra_files(
    game_dir: Path,
    wanted: set[str],
    rules: dict[str, Any],
    status: Status,
    force_remove: set[str] | None = None,
) -> int:
    removed = 0
    force_remove = force_remove or set()
    for item in sorted(game_dir.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(game_dir).as_posix()
        if rel in wanted or is_system_protected(rel):
            continue
        if rel in force_remove:
            status(f"Remove disabled optional {rel}")
            item.unlink()
            removed += 1
            continue
        if sync_mode_for(rel, rules) == "seed":
            continue
        status(f"Remove extra {rel}")
        item.unlink()
        removed += 1
    return removed


def sync_manifest(
    manifest: dict[str, Any],
    server_url: str,
    status: Status,
    progress: Progress | None = None,
    selected_optional_mod_ids: set[str] | None = None,
    cancelled: CancelCheck | None = None,
) -> Path:
    check_cancel(cancelled)
    profile = manifest["profile"]
    game_dir = instance_dir(profile["slug"])
    cleanup_legacy_instance_runtime(game_dir, status)
    manifest_files, disabled_optional = selected_manifest_files(manifest, selected_optional_mod_ids)
    wanted = {str(item["path"]).replace("\\", "/"): item for item in manifest_files}
    rules = manifest.get("rules") or {}
    stats = {"checked": len(wanted), "downloaded": 0, "updated": 0, "seeded": 0, "kept": 0, "removed": 0}
    downloads: list[tuple[str, dict[str, Any], Path, str]] = []

    for rel, item in wanted.items():
        target = local_instance_path(game_dir, rel)
        mode = item.get("mode") or sync_mode_for(rel, rules)
        if target.exists():
            if mode == "seed":
                stats["kept"] += 1
                continue
            if sha256_file(target) == item["sha256"]:
                continue
            downloads.append((rel, item, target, "updated"))
            continue
        if mode == "seed":
            downloads.append((rel, item, target, "seeded"))
        else:
            downloads.append((rel, item, target, "downloaded"))

    total_download_bytes = sum(int(item.get("size") or 0) for _, item, _, _ in downloads)
    download_progress = ByteProgressTracker(total_download_bytes)
    if downloads:
        download_progress.emit(progress, 0, total_download_bytes, "Download pack files")

    def fetch_pack_file(download: tuple[str, dict[str, Any], Path, str]) -> tuple[str, str]:
        check_cancel(cancelled)
        rel, item, target, stat_key = download
        url = absolute_url(server_url, item["url"])
        download_file(
            url,
            target,
            item["sha256"],
            status,
            progress,
            progress_tracker=download_progress,
            progress_label=f"Download {rel}",
            progress_key=rel,
            cancelled=cancelled,
        )
        return rel, stat_key

    if len(downloads) == 1:
        _, stat_key = fetch_pack_file(downloads[0])
        stats[stat_key] += 1
    elif downloads:
        worker_count = min(8, len(downloads))
        status(f"Download pack files with {worker_count} workers")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(fetch_pack_file, download) for download in downloads]
            for future in as_completed(futures):
                _, stat_key = future.result()
                stats[stat_key] += 1

    check_cancel(cancelled)
    stats["removed"] = cleanup_extra_files(game_dir, set(wanted), rules, status, disabled_optional)

    saved_manifest = dict(manifest)
    if selected_optional_mod_ids is not None:
        saved_manifest["selected_optional_mods"] = sorted(str(mod_id) for mod_id in selected_optional_mod_ids)
    write_manifest(game_dir, saved_manifest)
    status(
        "Sync done: "
        f"checked={stats['checked']} downloaded={stats['downloaded']} updated={stats['updated']} "
        f"seeded={stats['seeded']} kept_user={stats['kept']} removed={stats['removed']}"
    )
    return game_dir


def install_mod_loader(
    manifest: dict[str, Any],
    game_dir: Path,
    status: Status,
    progress: Progress | None = None,
) -> str:
    profile = manifest["profile"]
    loader_id = profile["mod_loader"].lower()
    minecraft_version = profile["minecraft_version"]
    loader_version = profile["loader_version"]
    minecraft_dir = shared_minecraft_dir()
    seed_shared_minecraft_cache(minecraft_dir, game_dir, status)
    java_info = select_system_java(profile, minecraft_dir, status)
    java_path = str(java_info["java_path"]) if java_info else None
    install_progress = MinecraftInstallProgress(status, progress)

    def set_status(text: str) -> None:
        install_progress.set_status(text)

    def set_max(value: int) -> None:
        install_progress.set_max(value)

    def set_progress(value: int) -> None:
        install_progress.set_progress(value)

    callback = {"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}
    installed_version = installed_version_id(profile)
    if version_is_installed(minecraft_dir, installed_version):
        status(f"Use shared Minecraft cache: {installed_version}")
        return installed_version

    with skip_mojang_java_runtime_when_system_java(java_info, status):
        if loader_id in {"vanilla", "minecraft", "none"}:
            status(f"Install Minecraft {minecraft_version} to shared cache")
            with track_streamed_request_bytes(install_progress.add_downloaded_bytes):
                minecraft_launcher_lib.install.install_minecraft_version(
                    minecraft_version,
                    str(minecraft_dir),
                    callback=callback,
                )
            return minecraft_version

        status(f"Install {loader_id} {loader_version} for Minecraft {minecraft_version} to shared cache")
        mod_loader = minecraft_launcher_lib.mod_loader.get_mod_loader(loader_id)
        with track_streamed_request_bytes(install_progress.add_downloaded_bytes):
            return mod_loader.install(
                minecraft_version,
                str(minecraft_dir),
                loader_version=loader_version,
                callback=callback,
                java=java_path,
            )


def ram_jvm_arguments(ram_mb: int | None) -> list[str]:
    if not ram_mb:
        return []
    value = max(512, int(ram_mb))
    return [f"-Xmx{value}M", f"-Xms{min(512, value)}M"]


def authlib_api_url(server_url: str) -> str:
    return f"{server_url.rstrip('/')}/api/yggdrasil/"


def authlib_cache_dir() -> Path:
    override = os.environ.get("BEBRALAND_AUTHLIB_CACHE_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        path = shared_minecraft_dir().parent / "authlib-injector"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_authlib_cache_dir() -> Path:
    return launcher_data_dir() / "authlib-injector"


def fetch_authlib_metadata(api_url: str, status: Status) -> str:
    cache_path = authlib_metadata_cache_path(api_url)
    try:
        status("Fetch authlib metadata")
        with requests.get(api_url, timeout=20) as response:
            response.raise_for_status()
            metadata = response.json()
        compact = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encoded = base64.b64encode(compact).decode("ascii")
        cache_path.write_text(encoded, encoding="utf-8")
        return encoded
    except Exception as exc:
        if cache_path.is_file():
            status(f"Use cached authlib metadata after backend check failed: {exc}")
            return cache_path.read_text(encoding="utf-8").strip()
        raise


def authlib_metadata_cache_path(api_url: str) -> Path:
    digest = hashlib.sha256(api_url.encode("utf-8")).hexdigest()
    return authlib_cache_dir() / f"metadata-{digest}.b64"


def offline_installed_version(manifest: dict[str, Any], status: Status) -> str:
    profile = manifest["profile"]
    minecraft_dir = shared_minecraft_dir()
    installed_version = installed_version_id(profile)
    if not version_is_installed(minecraft_dir, installed_version):
        raise RuntimeError("Offline launch unavailable: Minecraft/modloader is not installed locally yet")
    status(f"Use cached Minecraft/modloader: {installed_version}")
    return installed_version


def ensure_authlib_injector(status: Status, progress: Progress | None = None) -> Path:
    override = os.environ.get("AUTHLIB_INJECTOR_JAR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"AUTHLIB_INJECTOR_JAR not found: {path}")
        return path

    cache_dir = authlib_cache_dir()
    artifact_url = os.environ.get("AUTHLIB_INJECTOR_ARTIFACT_URL", AUTHLIB_ARTIFACT_URL)
    try:
        status("Check authlib-injector")
        with requests.get(artifact_url, timeout=20) as response:
            response.raise_for_status()
        artifact = response.json()
        version = str(artifact["version"])
        checksum = str(artifact["checksums"]["sha256"])
        jar_path = cache_dir / f"authlib-injector-{version}.jar"
        if jar_path.is_file() and sha256_file(jar_path) == checksum:
            status(f"Use authlib-injector {version}")
            return jar_path
        legacy_jar = legacy_authlib_cache_dir() / jar_path.name
        if legacy_jar.resolve() != jar_path.resolve() and legacy_jar.is_file() and sha256_file(legacy_jar) == checksum:
            shutil.copy2(legacy_jar, jar_path)
            status(f"Migrated authlib-injector {version} to shared cache")
            return jar_path
        download_file(str(artifact["download_url"]), jar_path, checksum, status, progress)
        (cache_dir / "latest.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        status(f"Downloaded authlib-injector {version}")
        return jar_path
    except Exception as exc:
        cache_dirs = [cache_dir, legacy_authlib_cache_dir()]
        cached_paths = []
        seen: set[Path] = set()
        for item in cache_dirs:
            if not item.exists():
                continue
            for jar in item.glob("authlib-injector-*.jar"):
                resolved = jar.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                cached_paths.append(jar)
        cached = sorted(cached_paths, key=lambda path: path.stat().st_mtime, reverse=True)
        if cached:
            status(f"Use cached authlib-injector after update check failed: {exc}")
            return cached[0]
        raise


def authlib_jvm_arguments(
    server_url: str,
    status: Status,
    progress: Progress | None = None,
) -> list[str]:
    api_url = authlib_api_url(server_url)
    jar_path = ensure_authlib_injector(status, progress)
    prefetched = fetch_authlib_metadata(api_url, status)
    return [
        f"-javaagent:{jar_path}={api_url}",
        f"-Dauthlibinjector.yggdrasil.prefetched={prefetched}",
        "-Dauthlibinjector.noLogFile",
    ]


def minecraft_profile_values(
    username: str | None,
    access_token: str | None,
    minecraft_profile: dict[str, Any] | None,
) -> tuple[str, str, str]:
    if not access_token:
        raise ValueError("Login required before launching Minecraft")
    profile = minecraft_profile or {}
    name = str(profile.get("name") or username or "").strip()
    profile_id = str(profile.get("id") or profile.get("uuid") or "").replace("-", "").strip()
    if not name:
        raise ValueError("Minecraft profile has no username")
    if len(profile_id) != 32:
        raise ValueError("Minecraft profile has no UUID")
    return name, profile_id, access_token


def ensure_mojang_java_runtime(
    installed_version: str,
    minecraft_dir: Path,
    status: Status,
    progress: Progress | None = None,
) -> None:
    try:
        information = minecraft_launcher_lib.runtime.get_version_runtime_information(installed_version, str(minecraft_dir))
    except Exception as exc:
        status(f"Mojang Java runtime check skipped: {exc}")
        return
    if not information:
        return

    runtime_name = str(information["name"])
    if minecraft_launcher_lib.runtime.get_executable_path(runtime_name, str(minecraft_dir)):
        return

    install_progress = MinecraftInstallProgress(status, progress)

    def set_status(text: str) -> None:
        install_progress.set_status(text)

    def set_max(value: int) -> None:
        install_progress.set_max(value)

    def set_progress(value: int) -> None:
        install_progress.set_progress(value)

    callback = {"setStatus": set_status, "setMax": set_max, "setProgress": set_progress}
    status(f"Install Mojang Java runtime {runtime_name}")
    with track_streamed_request_bytes(install_progress.add_downloaded_bytes):
        minecraft_launcher_lib.runtime.install_jvm_runtime(runtime_name, str(minecraft_dir), callback=callback)


def validate_launch_executable(command: list[str]) -> None:
    if not command:
        raise RuntimeError("Minecraft launch command is empty")
    executable = str(command[0])
    path = Path(executable)
    has_directory = path.is_absolute() or path.parent != Path(".")
    if has_directory:
        if not path.is_file():
            raise FileNotFoundError(f"Minecraft executable not found: {path}")
        return
    if not shutil.which(executable):
        raise FileNotFoundError(f"Minecraft executable not found on PATH: {executable}")


def launch_minecraft(
    manifest: dict[str, Any],
    game_dir: Path,
    username: str | None,
    status: Status,
    progress: Progress | None = None,
    installed_version: str | None = None,
    ram_mb: int | None = None,
    server_url: str | None = None,
    access_token: str | None = None,
    minecraft_profile: dict[str, Any] | None = None,
    window_settings: dict[str, Any] | None = None,
    debug_console: bool = False,
) -> subprocess.Popen:
    if installed_version is None:
        installed_version = install_mod_loader(manifest, game_dir, status, progress)
    minecraft_dir = shared_minecraft_dir()
    java_info = select_system_java(manifest["profile"], minecraft_dir, status)
    if not java_info:
        ensure_mojang_java_runtime(installed_version, minecraft_dir, status, progress)
    mc_username, mc_uuid, mc_token = minecraft_profile_values(username, access_token, minecraft_profile)
    options = minecraft_launcher_lib.utils.generate_test_options()
    jvm_arguments = list(options.get("jvmArguments") or [])
    jvm_arguments = [
        argument
        for argument in jvm_arguments
        if not argument.startswith("-Xmx") and not argument.startswith("-Xms")
    ]
    if not server_url:
        raise ValueError("Authlib server URL required before launching Minecraft")
    jvm_arguments.extend(authlib_jvm_arguments(server_url, status, progress))
    jvm_arguments.extend(ram_jvm_arguments(ram_mb))
    window_settings = window_settings or {}
    fullscreen = bool(window_settings.get("fullscreen"))
    width = int(window_settings.get("width") or 0)
    height = int(window_settings.get("height") or 0)
    options.update(
        {
            "username": mc_username,
            "uuid": mc_uuid,
            "token": mc_token,
            "gameDirectory": str(game_dir),
            "jvmArguments": jvm_arguments,
            "launcherName": "BebraLand Launcher",
            "launcherVersion": "0.1.0",
        }
    )
    if java_info:
        options["executablePath"] = str(java_info["java_path"])
    if fullscreen:
        options["fullscreen"] = True
    elif width > 0 and height > 0:
        options["customResolution"] = True
        options["resolutionWidth"] = str(width)
        options["resolutionHeight"] = str(height)
    command = minecraft_launcher_lib.command.get_minecraft_command(
        installed_version,
        str(minecraft_dir),
        options,
    )
    for index, argument in enumerate(command[:-1]):
        if argument == "--userType":
            command[index + 1] = "mojang"
    if ram_mb:
        status(f"Start Minecraft with {int(ram_mb)} MB RAM")
    else:
        status("Start Minecraft")
    creationflags = 0
    if os.name == "nt":
        java_path = Path(str(command[0]))
        if debug_console:
            creationflags = subprocess.CREATE_NEW_CONSOLE
            if java_path.name.lower() == "javaw.exe":
                java_exe = java_path.with_name("java.exe")
                if java_exe.is_file():
                    command[0] = str(java_exe)
        else:
            if java_path.name.lower() == "java.exe":
                javaw_exe = java_path.with_name("javaw.exe")
                if javaw_exe.is_file():
                    command[0] = str(javaw_exe)
    validate_launch_executable(command)
    return subprocess.Popen(command, cwd=str(game_dir), creationflags=creationflags)
