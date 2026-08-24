from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from pathlib import Path

import box_backup
from box_backup import IncrementalBoxUploader


class _OAuth:
    proxies: dict[str, str] = {}

    def access_token(self, *, force_refresh: bool = False) -> str:
        return "test-token"


class _BoxHandler(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []
    folders: dict[str, list[dict[str, str]]] = {"0": []}
    next_id = 1

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path, b""))
        path = self.path.split("?", 1)[0]
        if path == "/2.0/users/me":
            self._json(200, {"id": "user-1", "name": "Test User", "login": "test@example.com"})
            return
        if path.startswith("/2.0/folders/") and path.endswith("/items"):
            parent_id = path.split("/")[3]
            entries = self.folders.get(parent_id, [])
            self._json(200, {"entries": entries, "total_count": len(entries)})
            return
        self._json(404, {"message": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        self.requests.append(("POST", self.path, body))
        if self.path == "/2.0/folders":
            payload = json.loads(body.decode("utf-8"))
            folder_id = str(self.next_id)
            type(self).next_id += 1
            item = {"id": folder_id, "type": "folder", "name": payload["name"]}
            parent_id = payload["parent"]["id"]
            self.folders.setdefault(parent_id, []).append(item)
            self.folders.setdefault(folder_id, [])
            self._json(201, item)
            return
        if self.path == "/upload/2.0/files/content":
            self._json(201, {"entries": [{"id": "file-1"}]})
            return
        self._json(404, {"message": "not found"})

    def log_message(self, *_args: object) -> None:
        pass


def test_box_connection_and_incremental_stream_upload(tmp_path: Path, monkeypatch) -> None:
    _BoxHandler.requests = []
    _BoxHandler.folders = {"0": []}
    _BoxHandler.next_id = 1
    server = socketserver.TCPServer(("127.0.0.1", 0), _BoxHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(box_backup, "BOX_API_URL", f"{base}/2.0")
    monkeypatch.setattr(box_backup, "BOX_UPLOAD_URL", f"{base}/upload/2.0")
    uploader = IncrementalBoxUploader(
        tmp_path,
        oauth=_OAuth(),
        target_folder="WechatRosterBackup",
        remote_folder="remark-20260824-abc123",
        interval=0.05,
    )
    try:
        assert uploader.test_connection()["name"] == "Test User"
        uploader.start()
        group = tmp_path / "group"
        group.mkdir()
        (group / "page-01.png").write_bytes(b"png-data")
        time.sleep(0.25)
        uploader.stop()
    finally:
        server.shutdown()
        server.server_close()
    uploads = [item for item in _BoxHandler.requests if item[0] == "POST" and item[1] == "/upload/2.0/files/content"]
    assert len(uploads) == 1
    assert b"png-data" in uploads[0][2]
    assert b"page-01.png" in uploads[0][2]
    created_names = [json.loads(body.decode("utf-8"))["name"] for method, path, body in _BoxHandler.requests if method == "POST" and path == "/2.0/folders"]
    assert created_names == ["WechatRosterBackup", "remark-20260824-abc123", "group"]
