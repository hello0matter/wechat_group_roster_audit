"""Incremental WebDAV uploader for completed Weixin backup artifacts."""
from __future__ import annotations

import base64
import http.client
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

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
        proxies: dict[str, str] | None = None,
    ) -> None:
        super().__init__(root, remark=remark, remote_folder=remote_folder, interval=interval)
        self.url = url.rstrip("/")
        self.user = user
        self.secret = secret
        self.proxies = proxies or {}

    def start(self) -> None:
        if not self.url:
            return
        super().start()

    def _remote_url(self, remote: str = "") -> str:
        base = self.url.rstrip("/")
        if not remote:
            return base + "/"
        encoded = "/".join(
            quote(part, safe="") for part in remote.strip("/").split("/") if part
        )
        return f"{base}/{encoded}"

    def _auth(self) -> tuple[str, str] | None:
        return (self.user, self.secret) if self.user else None

    def _headers(self, size: int | None = None) -> dict[str, str]:
        headers = {"User-Agent": "WechatRosterGUI/1.0"}
        if size is not None:
            headers["Content-Length"] = str(size)
        if not self.user and self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        return headers

    def test_connection(self) -> None:
        response = requests.request(
            "PROPFIND",
            self._remote_url(),
            headers={**self._headers(), "Depth": "0"},
            auth=self._auth(),
            timeout=30,
            proxies=self.proxies,
        )
        if response.status_code not in {200, 207}:
            raise RuntimeError(f"HTTP {response.status_code} {response.reason}")

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
        response = requests.request(
            method,
            self._remote_url(remote),
            data=body,
            headers=self._headers(size),
            auth=self._auth(),
            timeout=90,
            proxies=self.proxies,
        )
        if response.status_code not in {200, 201, 204, 207, 405, 409}:
            raise RuntimeError(f"HTTP {response.status_code} {response.reason}")

    def _ensure_folder(self, rel: str) -> None:
        self._request("MKCOL", rel or self.remote_folder)

    def _prepare_remote(self) -> None:
        self._ensure_folder("")

    def _upload_file(self, path: Path, rel: str, size: int) -> None:
        parts = rel.split("/")
        for index in range(1, len(parts)):
            self._ensure_folder("/".join([self.remote_folder, *parts[:index]]))
        remote = "/".join([self.remote_folder, rel])
        with path.open("rb") as stream:
            response = requests.put(
                self._remote_url(remote),
                data=stream,
                headers=self._headers(size),
                auth=self._auth(),
                timeout=300,
                proxies=self.proxies,
            )
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(f"HTTP {response.status_code} {response.reason}")
