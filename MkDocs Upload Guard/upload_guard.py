#!/usr/bin/env python3
"""Guard an MkDocs upload directory and publish operational metrics.

The service intentionally uses polling instead of platform-specific file-system
APIs. This keeps deployment dependency-free and also lets it wait until an
upload has stopped changing before validating or deleting it.
"""

from __future__ import annotations

import argparse
import codecs
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LOGGER = logging.getLogger("mkdocs-upload-guard")
NAV_BEGIN = "# BEGIN AUTO-GENERATED NAV"
NAV_END = "# END AUTO-GENERATED NAV"
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})
TOP_LEVEL_NAV_PATTERN = re.compile(r"(?m)^nav\s*:")
HEADING_PATTERN = re.compile(r"^\s*#\s+(.+?)\s*$")
METRIC_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigurationError(ValueError):
    """Raised when the JSON configuration is unsafe or incomplete."""


class NavUpdateError(RuntimeError):
    """Raised when mkdocs.yml cannot be updated without losing user data."""


@dataclasses.dataclass(frozen=True)
class ZabbixConfig:
    enabled: bool = False
    server: str = "127.0.0.1"
    port: int = 10051
    host: str = "school-lab"
    timeout_seconds: float = 5.0
    key_prefix: str = "mkdocs.upload"


@dataclasses.dataclass(frozen=True)
class AppConfig:
    watch_dir: Path
    mkdocs_config: Path
    allowed_text_extensions: frozenset[str]
    text_encodings: tuple[str, ...]
    scan_interval_seconds: float
    settle_seconds: float
    heartbeat_seconds: float
    notification_webhook_url: str | None
    notification_timeout_seconds: float
    log_file: Path | None
    log_level: str
    zabbix: ZabbixConfig


@dataclasses.dataclass(frozen=True)
class FileSignature:
    size: int
    modified_ns: int


@dataclasses.dataclass
class PendingFile:
    signature: FileSignature
    stable_since: float


@dataclasses.dataclass
class Counters:
    accepted_total: int = 0
    deleted_total: int = 0
    errors_total: int = 0
    nav_updates_total: int = 0
    last_event_unixtime: int = 0

    def as_metrics(self, current_files: int) -> dict[str, int]:
        return {
            "accepted_total": self.accepted_total,
            "deleted_total": self.deleted_total,
            "errors_total": self.errors_total,
            "nav_updates_total": self.nav_updates_total,
            "files_current": current_files,
            "last_event_unixtime": self.last_event_unixtime,
            "heartbeat": int(time.time()),
        }


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_config(config_path: Path) -> AppConfig:
    config_path = config_path.resolve(strict=True)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a JSON object")

    base_dir = config_path.parent
    watch_dir = _resolve_path(str(raw.get("watch_dir", "")), base_dir)
    mkdocs_config = _resolve_path(str(raw.get("mkdocs_config", "")), base_dir)

    extensions_raw = raw.get(
        "allowed_text_extensions", [".md", ".markdown", ".txt"]
    )
    if not isinstance(extensions_raw, list) or not extensions_raw:
        raise ConfigurationError("allowed_text_extensions must be a non-empty list")
    extensions = frozenset(
        extension.lower()
        if str(extension).startswith(".")
        else f".{str(extension).lower()}"
        for extension in extensions_raw
    )

    encodings_raw = raw.get("text_encodings", ["utf-8-sig"])
    if not isinstance(encodings_raw, list) or not encodings_raw:
        raise ConfigurationError("text_encodings must be a non-empty list")
    encodings: list[str] = []
    for encoding in encodings_raw:
        try:
            normalized = codecs.lookup(str(encoding)).name
        except LookupError as error:
            raise ConfigurationError(f"unknown text encoding: {encoding}") from error
        if normalized not in encodings:
            encodings.append(normalized)

    zabbix_raw = raw.get("zabbix", {})
    if not isinstance(zabbix_raw, dict):
        raise ConfigurationError("zabbix must be a JSON object")
    zabbix = ZabbixConfig(
        enabled=bool(zabbix_raw.get("enabled", False)),
        server=str(zabbix_raw.get("server", "127.0.0.1")),
        port=int(zabbix_raw.get("port", 10051)),
        host=str(zabbix_raw.get("host", "school-lab")),
        timeout_seconds=float(zabbix_raw.get("timeout_seconds", 5.0)),
        key_prefix=str(zabbix_raw.get("key_prefix", "mkdocs.upload")),
    )

    webhook_raw = str(raw.get("notification_webhook_url", "")).strip()
    log_file_raw = str(raw.get("log_file", "")).strip()
    config = AppConfig(
        watch_dir=watch_dir,
        mkdocs_config=mkdocs_config,
        allowed_text_extensions=extensions,
        text_encodings=tuple(encodings),
        scan_interval_seconds=float(raw.get("scan_interval_seconds", 2.0)),
        settle_seconds=float(raw.get("settle_seconds", 2.0)),
        heartbeat_seconds=float(raw.get("heartbeat_seconds", 60.0)),
        notification_webhook_url=webhook_raw or None,
        notification_timeout_seconds=float(
            raw.get("notification_timeout_seconds", 5.0)
        ),
        log_file=_resolve_path(log_file_raw, base_dir) if log_file_raw else None,
        log_level=str(raw.get("log_level", "INFO")).upper(),
        zabbix=zabbix,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if not config.watch_dir.is_dir():
        raise ConfigurationError(f"watch_dir is not a directory: {config.watch_dir}")
    if not config.mkdocs_config.is_file():
        raise ConfigurationError(
            f"mkdocs_config is not a file: {config.mkdocs_config}"
        )
    if _is_relative_to(config.mkdocs_config, config.watch_dir):
        raise ConfigurationError("mkdocs_config must be outside watch_dir")
    if config.log_file and _is_relative_to(config.log_file, config.watch_dir):
        raise ConfigurationError("log_file must be outside watch_dir")
    if config.scan_interval_seconds <= 0:
        raise ConfigurationError("scan_interval_seconds must be greater than zero")
    if config.settle_seconds < 0:
        raise ConfigurationError("settle_seconds must not be negative")
    if config.heartbeat_seconds <= 0:
        raise ConfigurationError("heartbeat_seconds must be greater than zero")
    if config.notification_timeout_seconds <= 0:
        raise ConfigurationError(
            "notification_timeout_seconds must be greater than zero"
        )
    if config.notification_webhook_url and not config.notification_webhook_url.startswith(
        ("http://", "https://")
    ):
        raise ConfigurationError("notification_webhook_url must use http or https")
    if config.zabbix.enabled:
        if not config.zabbix.server or not config.zabbix.host:
            raise ConfigurationError("enabled Zabbix requires server and host")
        if not 1 <= config.zabbix.port <= 65535:
            raise ConfigurationError("Zabbix port must be between 1 and 65535")
        if config.zabbix.timeout_seconds <= 0:
            raise ConfigurationError("Zabbix timeout must be greater than zero")
        if not METRIC_PREFIX_PATTERN.fullmatch(config.zabbix.key_prefix):
            raise ConfigurationError(
                "Zabbix key_prefix may contain only letters, numbers, dot, dash, underscore"
            )


def configure_logging(config: AppConfig) -> None:
    level = getattr(logging, config.log_level, None)
    if not isinstance(level, int):
        raise ConfigurationError(f"unknown log level: {config.log_level}")

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log_file:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)


class Notifier:
    def __init__(self, webhook_url: str | None, timeout_seconds: float) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def notify(
        self,
        event: str,
        message: str,
        *,
        path: Path | None = None,
        reason: str | None = None,
        severity: str = "info",
    ) -> None:
        level = logging.WARNING if severity == "warning" else logging.INFO
        LOGGER.log(
            level,
            "%s event=%s path=%s reason=%s",
            message,
            event,
            path or "-",
            reason or "-",
        )
        if not self.webhook_url:
            return

        payload = {
            "event": event,
            "message": message,
            "path": str(path) if path else None,
            "reason": reason,
            "severity": severity,
            "timestamp": int(time.time()),
        }
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response.read()
        except Exception as error:  # Notification errors must not stop the guard.
            LOGGER.error("webhook notification failed: %s", error)


class ZabbixSender:
    """Small implementation of the Zabbix sender/trapper protocol."""

    HEADER = b"ZBXD\x01"

    def __init__(self, config: ZabbixConfig) -> None:
        self.config = config

    @staticmethod
    def _receive_exact(sock: socket.socket, length: int) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received < length:
            chunk = sock.recv(length - received)
            if not chunk:
                raise ConnectionError("Zabbix server closed the connection")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def send(self, metrics: Mapping[str, int | float | str]) -> dict[str, Any] | None:
        if not self.config.enabled:
            return None
        now = int(time.time())
        data = [
            {
                "host": self.config.host,
                "key": f"{self.config.key_prefix}.{name}",
                "value": str(value),
                "clock": now,
            }
            for name, value in sorted(metrics.items())
        ]
        body = json.dumps(
            {"request": "sender data", "data": data, "clock": now},
            separators=(",", ":"),
        ).encode("utf-8")
        packet = self.HEADER + struct.pack("<Q", len(body)) + body

        with socket.create_connection(
            (self.config.server, self.config.port),
            timeout=self.config.timeout_seconds,
        ) as sock:
            sock.settimeout(self.config.timeout_seconds)
            sock.sendall(packet)
            header = self._receive_exact(sock, 13)
            if not header.startswith(self.HEADER):
                raise ValueError("invalid response header from Zabbix server")
            response_length = struct.unpack("<Q", header[5:13])[0]
            response_body = self._receive_exact(sock, response_length)

        response = json.loads(response_body.decode("utf-8"))
        if response.get("response") != "success":
            raise RuntimeError(f"Zabbix rejected metrics: {response}")
        return response


def file_signature(path: Path) -> FileSignature:
    stat = path.lstat()
    return FileSignature(size=stat.st_size, modified_ns=stat.st_mtime_ns)


def iter_files(root: Path) -> Iterable[Path]:
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if not directory.startswith(".")
            and not (Path(current_root) / directory).is_symlink()
        )
        for filename in sorted(filenames):
            yield Path(current_root) / filename


def validate_text_file(
    path: Path,
    allowed_extensions: frozenset[str],
    encodings: Sequence[str],
) -> tuple[bool, str]:
    if path.is_symlink():
        return False, "symbolic links are not allowed"
    if path.suffix.lower() not in allowed_extensions:
        return False, f"extension {path.suffix or '<none>'} is not allowed"

    for encoding in encodings:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        valid = True
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    if b"\x00" in chunk:
                        return False, "NUL byte detected"
                    decoder.decode(chunk, final=False)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            valid = False
        except OSError as error:
            return False, f"read failed: {error}"
        if valid:
            return True, encoding
    return False, "content is not valid text in an allowed encoding"


def markdown_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                match = HEADING_PATTERN.match(line)
                if match:
                    return match.group(1).strip()
    except (OSError, UnicodeDecodeError):
        pass
    if path.name.lower() == "index.md":
        return "Home" if path.parent.name == "docs" else "Overview"
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.stem


def directory_title(path: Path) -> str:
    return path.name.replace("_", " ").replace("-", " ").strip() or path.name


NavEntry = tuple[str, str | list["NavEntry"]]


def build_nav_entries(directory: Path, root: Path) -> list[NavEntry]:
    entries: list[NavEntry] = []
    markdown_files = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in MARKDOWN_EXTENSIONS
            and not path.name.startswith(".")
        ),
        key=lambda path: (path.name.lower() != "index.md", path.name.casefold()),
    )
    for path in markdown_files:
        relative_path = path.relative_to(root).as_posix()
        entries.append((markdown_title(path), relative_path))

    child_directories = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
        ),
        key=lambda path: path.name.casefold(),
    )
    for child in child_directories:
        child_entries = build_nav_entries(child, root)
        if child_entries:
            entries.append((directory_title(child), child_entries))
    return entries


def render_nav(entries: Sequence[NavEntry]) -> str:
    lines = [NAV_BEGIN]
    if not entries:
        lines.append("nav: []")
    else:
        lines.append("nav:")

        def append_entries(items: Sequence[NavEntry], level: int) -> None:
            indent = "  " * level
            for title, value in items:
                quoted_title = json.dumps(title, ensure_ascii=False)
                if isinstance(value, list):
                    lines.append(f"{indent}- {quoted_title}:")
                    append_entries(value, level + 1)
                else:
                    quoted_path = json.dumps(value, ensure_ascii=False)
                    lines.append(f"{indent}- {quoted_title}: {quoted_path}")

        append_entries(entries, 1)
    lines.append(NAV_END)
    return "\n".join(lines)


def update_mkdocs_nav(mkdocs_config: Path, docs_dir: Path) -> bool:
    original = mkdocs_config.read_text(encoding="utf-8")
    original_mode = mkdocs_config.stat().st_mode
    has_begin = NAV_BEGIN in original
    has_end = NAV_END in original
    if has_begin != has_end:
        raise NavUpdateError("only one generated-nav marker exists in mkdocs.yml")
    if not has_begin and TOP_LEVEL_NAV_PATTERN.search(original):
        raise NavUpdateError(
            "mkdocs.yml already contains a manual nav; remove it or surround it "
            f"with {NAV_BEGIN!r} and {NAV_END!r}"
        )

    generated = render_nav(build_nav_entries(docs_dir, docs_dir))
    if has_begin:
        start = original.index(NAV_BEGIN)
        end = original.index(NAV_END, start) + len(NAV_END)
        updated = original[:start] + generated + original[end:]
    else:
        updated = original.rstrip() + "\n\n" + generated + "\n"

    if updated == original:
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{mkdocs_config.name}.",
        suffix=".tmp",
        dir=mkdocs_config.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, mkdocs_config)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return True


class UploadGuard:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.notifier = Notifier(
            config.notification_webhook_url,
            config.notification_timeout_seconds,
        )
        self.zabbix = ZabbixSender(config.zabbix)
        self.counters = Counters()
        self.known: dict[Path, FileSignature] = {}
        self.pending: dict[Path, PendingFile] = {}
        self.stop_event = threading.Event()
        self.last_heartbeat = 0.0

    def _collect(self) -> dict[Path, FileSignature]:
        collected: dict[Path, FileSignature] = {}
        for path in iter_files(self.config.watch_dir):
            try:
                collected[path] = file_signature(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                self.counters.errors_total += 1
                LOGGER.error("cannot inspect %s: %s", path, error)
        return collected

    def _send_metrics(self, current_files: int) -> None:
        try:
            self.zabbix.send(self.counters.as_metrics(current_files))
        except Exception as error:
            LOGGER.error("Zabbix metric send failed: %s", error)

    def _update_nav(self) -> None:
        try:
            changed = update_mkdocs_nav(
                self.config.mkdocs_config, self.config.watch_dir
            )
            if changed:
                self.counters.nav_updates_total += 1
                self.notifier.notify(
                    "nav_updated",
                    "mkdocs.yml navigation was updated",
                    path=self.config.mkdocs_config,
                )
        except Exception as error:
            self.counters.errors_total += 1
            self.notifier.notify(
                "nav_update_failed",
                "mkdocs.yml navigation update failed",
                path=self.config.mkdocs_config,
                reason=str(error),
                severity="warning",
            )

    def _process(self, path: Path, signature: FileSignature) -> None:
        valid, reason = validate_text_file(
            path,
            self.config.allowed_text_extensions,
            self.config.text_encodings,
        )
        relative_path = path.relative_to(self.config.watch_dir)
        self.counters.last_event_unixtime = int(time.time())
        if valid:
            self.known[path] = signature
            self.counters.accepted_total += 1
            self.notifier.notify(
                "file_accepted",
                "text file accepted",
                path=relative_path,
                reason=f"encoding={reason}",
            )
        else:
            try:
                path.unlink()
                self.known.pop(path, None)
                self.counters.deleted_total += 1
                self.notifier.notify(
                    "file_deleted",
                    "non-text or disallowed file deleted",
                    path=relative_path,
                    reason=reason,
                    severity="warning",
                )
            except FileNotFoundError:
                self.known.pop(path, None)
            except OSError as error:
                self.counters.errors_total += 1
                self.notifier.notify(
                    "file_delete_failed",
                    "disallowed file could not be deleted",
                    path=relative_path,
                    reason=str(error),
                    severity="warning",
                )
        self._update_nav()

    def scan(self, *, settle_immediately: bool = False) -> None:
        now = time.monotonic()
        current = self._collect()
        event_happened = False

        removed_paths = set(self.known) - set(current)
        for path in removed_paths:
            self.known.pop(path, None)
            self.pending.pop(path, None)
        if removed_paths:
            self.counters.last_event_unixtime = int(time.time())
            self._update_nav()
            event_happened = True

        for path, signature in current.items():
            if self.known.get(path) == signature:
                self.pending.pop(path, None)
                continue
            pending = self.pending.get(path)
            if pending is None or pending.signature != signature:
                self.pending[path] = PendingFile(signature, now)
                if not settle_immediately:
                    continue
            pending = self.pending[path]
            if settle_immediately or now - pending.stable_since >= self.config.settle_seconds:
                self.pending.pop(path, None)
                try:
                    self._process(path, signature)
                    event_happened = True
                except FileNotFoundError:
                    continue
                except Exception as error:
                    self.counters.errors_total += 1
                    LOGGER.exception("unexpected processing error for %s: %s", path, error)

        if event_happened or now - self.last_heartbeat >= self.config.heartbeat_seconds:
            self._send_metrics(len(self._collect()))
            self.last_heartbeat = now

    def run(self) -> None:
        LOGGER.info(
            "starting watch_dir=%s mkdocs_config=%s",
            self.config.watch_dir,
            self.config.mkdocs_config,
        )
        while not self.stop_event.is_set():
            self.scan()
            self.stop_event.wait(self.config.scan_interval_seconds)
        self._send_metrics(len(self._collect()))
        LOGGER.info("stopped")

    def stop(self, *_args: object) -> None:
        self.stop_event.set()


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate MkDocs uploads, generate nav, and send Zabbix metrics."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to the JSON configuration file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="scan current files immediately and exit",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        config = load_config(args.config)
        configure_logging(config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if args.check_config:
        print("configuration is valid")
        return 0

    guard = UploadGuard(config)
    if args.once:
        guard.scan(settle_immediately=True)
        guard._send_metrics(len(guard._collect()))
        return 0 if guard.counters.errors_total == 0 else 1

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, guard.stop)
    guard.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
