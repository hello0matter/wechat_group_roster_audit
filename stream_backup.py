"""Incremental WebDAV uploader for completed Weixin backup artifacts."""
from __future__ import annotations

import base64
import http.client
from pathlib import Path
from urllib.parse import quote, urlparse

from incremental_backup import IncrementalArtifactUploader


class IncrementalWebDavUploader(IncrementalArtifactUploader):
    provider_name = "webdav"

    def __init__(
        self,
        root: Path,
        *,
        url: str,
        user: str = "",
        secret: str = "",
        remark: str = "微信备份",
        remote_folder: str | None = None,
        interval: float = 1.5,
    ) -> None:
        super().__init__(root, remark=remark, remote_folder=remote_folder, interval=interval)
        self.url = url.rstrip("/")
        self.user = user
        self.secret = secret

    def start(self) -> None:
        if not self.url:
            return
        super().start()

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

    def _parts(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WebDAV 地址必须以 http:// 或 https:// 开头")
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
        path = "/" + "/".join(
            quote(part, safe="")
            for part in (base.strip("/") + "/" + remote.strip("/")).split("/")
            if part
        )
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

    def _prepare_remote(self) -> None:
        self._ensure_folder("")

    def _upload_file(self, path: Path, rel: str, size: int) -> None:
        parts = rel.split("/")
        for index in range(1, len(parts)):
            self._ensure_folder("/".join([self.remote_folder, *parts[:index]]))
        conn, base = self._parts()
        remote = "/".join([self.remote_folder, rel])
        target = "/" + "/".join(
            quote(part, safe="")
            for part in (base.strip("/") + "/" + remote).split("/")
            if part
        )
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
