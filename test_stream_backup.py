from __future__ import annotations

import http.server
import socketserver
import threading
import time
from pathlib import Path

from stream_backup import IncrementalWebDavUploader


class _Handler(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def do_MKCOL(self) -> None:
        self.requests.append(("MKCOL", self.path, b""))
        self.send_response(201)
        self.end_headers()

    def do_PUT(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.requests.append(("PUT", self.path, body))
        self.send_response(201)
        self.end_headers()

    def do_PROPFIND(self) -> None:
        self.requests.append(("PROPFIND", self.path, b""))
        self.send_response(207)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


def test_incremental_upload_uses_unique_task_folder(tmp_path: Path) -> None:
    _Handler.requests = []
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    uploader = IncrementalWebDavUploader(
        tmp_path,
        url=f"http://127.0.0.1:{server.server_address[1]}/dav",
        remote_folder="remark-20260824-abc123",
        interval=0.05,
    )
    try:
        uploader.test_connection()
        uploader.start()
        group = tmp_path / "group"
        group.mkdir()
        (group / "page-01.png").write_bytes(b"png-data")
        time.sleep(0.25)
        uploader.stop()
    finally:
        server.shutdown()
        server.server_close()
    uploads = [item for item in _Handler.requests if item[0] == "PUT"]
    assert uploads[-1] == (
        "PUT",
        "/dav/remark-20260824-abc123/group/page-01.png",
        b"png-data",
    )


def test_stop_performs_final_stability_scans(tmp_path: Path) -> None:
    _Handler.requests = []
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    uploader = IncrementalWebDavUploader(
        tmp_path,
        url=f"http://127.0.0.1:{server.server_address[1]}/dav",
        remote_folder="interrupted-run",
        interval=0.05,
    )
    try:
        uploader.start()
        (tmp_path / "last.json").write_text("{}", encoding="utf-8")
        uploader.stop()
    finally:
        server.shutdown()
        server.server_close()
    assert any(item[0] == "PUT" and item[1].endswith("/last.json") for item in _Handler.requests)
