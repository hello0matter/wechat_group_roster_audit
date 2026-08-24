"""Incremental WebDAV uploader for completed Weixin backup artifacts."""
from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse


class IncrementalWebDavUploader:
    def __init__(self, root: Path, *, url: str, user: str = "", secret: str = "", remark: str = "\u5fae\u4fe1\u5907\u4efd", remote_folder: str | None = None, interval: float = 1.5) -> None:
        self.root = root.resolve()
        self.url = url.rstrip("/")
        self.user = user
        self.secret = secret
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
        if not self.url or self.thread is not None:
            return
        self._ensure_folder("")
        self.thread = threading.Thread(target=self._worker, name="webdav-uploader", daemon=True)
        self.thread.start()

    def stop(self, wait: float = 4.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(wait)
        # Two final scans make files created immediately before interruption
        # eligible without requiring another application launch.
        self._scan_once()
        time.sleep(min(self.interval, 0.5))
        self._scan_once()

    def test_connection(self) -> None:
        conn, base = self._parts()
        path = base or "/"
        try:
            conn.request("PROPFIND", path, headers={**self._headers(), "Depth": "0"})
            response = conn.getresponse()
            response.read()
            if response.status not in {200, 207}:
                raise RuntimeError(f"HTTP {response.status} {response.reason}")
        finally:
            conn.close()

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
            uploaded = self.state.get(rel)
            if uploaded == f"{stat.st_size}:{stat.st_mtime_ns}":
                continue
            try:
                self._upload(path, rel, stat.st_size)
                self.state[rel] = f"{stat.st_size}:{stat.st_mtime_ns}"
                self._save_state()
            except Exception as exc:
                self.errors.put(f"{rel}: {exc}")

    def _parts(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WebDAV \u5730\u5740\u5fc5\u987b\u4ee5 http:// \u6216 https:// \u5f00\u5934")
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        return conn_cls(parsed.netloc, timeout=30), parsed.path.rstrip("/")

    def _headers(self, size: int | None = None) -> dict[str, str]:
        headers = {"User-Agent": "WechatRosterGUI/1.0"}
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        elif self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if size is not None:
            headers["Content-Length"] = str(size)
        return headers

    def _request(self, method: str, remote: str, *, body=None, size: int | None = None) -> None:
        conn, base = self._parts()
        path = "/" + "/".join(quote(part, safe="") for part in (base.strip("/") + "/" + remote.strip("/")).split("/") if part)
        try:
            conn.request(method, path, body=body, headers=self._headers(size))
            response = conn.getresponse()
            response.read()
            if response.status not in {200, 201, 204, 207, 405, 409}:
                raise RuntimeError(f"HTTP {response.status} {response.reason}")
        finally:
            conn.close()

    def _ensure_folder(self, rel: str) -> None:
        self._request("MKCOL", rel or self.remote_folder)

    def _upload(self, path: Path, rel: str, size: int) -> None:
        parts = rel.split("/")
        for index in range(1, len(parts)):
            self._ensure_folder("/".join([self.remote_folder, *parts[:index]]))
        conn, base = self._parts()
        remote = "/".join([self.remote_folder, rel])
        target = "/" + "/".join(quote(part, safe="") for part in (base.strip("/") + "/" + remote).split("/") if part)
        try:
            conn.putrequest("PUT", target)
            for key, value in self._headers(size).items():
                conn.putheader(key, value)
            conn.endheaders()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    conn.send(chunk)
            response = conn.getresponse()
            response.read()
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"HTTP {response.status} {response.reason}")
        finally:
            conn.close()
