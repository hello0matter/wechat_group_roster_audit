"""Shared incremental uploader for stable backup artifacts."""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from pathlib import Path


class IncrementalArtifactUploader:
    """Watch a task directory and upload files after two stable scans."""

    provider_name = "cloud"

    def __init__(
        self,
        root: Path,
        *,
        remark: str = "微信备份",
        remote_folder: str | None = None,
        interval: float = 1.5,
    ) -> None:
        self.root = root.resolve()
        self.interval = max(0.3, float(interval))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in remark.strip()) or "wechat-backup"
        self.remote_folder = remote_folder or f"{safe}-{stamp}-{uuid.uuid4().hex[:6]}"
        self.state_path = self.root / ".upload-state.json"
        self.state = self._load_state()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.errors: queue.Queue[str] = queue.Queue()
        self._seen: dict[str, tuple[int, int]] = {}

    def _load_state(self) -> dict[str, object]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def start(self) -> None:
        if self.thread is not None:
            return
        self._prepare_remote()
        self.thread = threading.Thread(
            target=self._worker,
            name=f"{self.provider_name}-uploader",
            daemon=True,
        )
        self.thread.start()

    def stop(self, wait: float = 4.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(wait)
        self._scan_once()
        time.sleep(min(self.interval, 0.5))
        self._scan_once()

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            self._scan_once()
            self.stop_event.wait(self.interval)

    def _scan_once(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.startswith(".") or path.name.endswith(".tmp"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            marker = (stat.st_size, stat.st_mtime_ns)
            if self._seen.get(rel) != marker:
                self._seen[rel] = marker
                continue
            if self.state.get(rel) == f"{stat.st_size}:{stat.st_mtime_ns}":
                continue
            try:
                self._upload_file(path, rel, stat.st_size)
                self.state[rel] = f"{stat.st_size}:{stat.st_mtime_ns}"
                self._save_state()
            except Exception as exc:
                self.errors.put(f"{rel}: {exc}")

    def _prepare_remote(self) -> None:
        raise NotImplementedError

    def _upload_file(self, path: Path, rel: str, size: int) -> None:
        raise NotImplementedError
