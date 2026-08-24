"""Box OAuth 2.0 and incremental Box API uploads."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import mimetypes
import secrets
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from incremental_backup import IncrementalArtifactUploader
from secret_store import load_secret, save_secret

BOX_AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
BOX_TOKEN_URL = "https://api.box.com/oauth2/token"
BOX_API_URL = "https://api.box.com/2.0"
BOX_UPLOAD_URL = "https://upload.box.com/api/2.0"
DIRECT_UPLOAD_LIMIT = 50 * 1024 * 1024


class BoxApiError(RuntimeError):
    pass


class _MultipartStream:
    def __init__(self, path: Path, fields: list[tuple[str, str]], *, file_field: str = "file") -> None:
        self.path = path
        self.boundary = f"wechat-roster-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields:
            chunks.append(
                f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                "Content-Type: application/json\r\n\r\n".encode("utf-8")
                + value.encode("utf-8")
                + b"\r\n"
            )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(
            f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{path.name.replace(chr(34), '_')}\"\r\nContent-Type: {content_type}\r\n\r\n".encode("utf-8")
        )
        self.prefix = b"".join(chunks)
        self.suffix = f"\r\n--{self.boundary}--\r\n".encode("ascii")
        self.length = len(self.prefix) + path.stat().st_size + len(self.suffix)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterable[bytes]:
        yield self.prefix
        with self.path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                yield chunk
        yield self.suffix


class BoxOAuthClient:
    """Authorization-code OAuth client using a localhost callback."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        credential_path: Path,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.credential_path = credential_path
        self.proxies = proxies or {}
        self.session = requests.Session()
        self.session.proxies.update(self.proxies)

    def _validate_config(self) -> None:
        if not self.client_id:
            raise ValueError("请填写 Box Client ID")
        if not self.client_secret:
            raise ValueError("请填写 Box Client Secret")
        parsed = urlparse(self.redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise ValueError("Box 回调地址必须是带端口的本机 HTTP 地址，例如 http://127.0.0.1:53682/callback")

    def _stored(self) -> dict[str, str]:
        return load_secret(self.credential_path)

    def _save_tokens(self, payload: dict[str, object]) -> None:
        values = self._stored()
        values.update(
            {
                "box_access_token": str(payload.get("access_token", "")),
                "box_refresh_token": str(payload.get("refresh_token", values.get("box_refresh_token", ""))),
                "box_expires_at": str(time.time() + max(60, int(payload.get("expires_in", 3600))) - 60),
            }
        )
        save_secret(self.credential_path, values)

    def clear_tokens(self) -> None:
        values = self._stored()
        for key in ("box_access_token", "box_refresh_token", "box_expires_at"):
            values.pop(key, None)
        save_secret(self.credential_path, values)

    def authorization_url(self, state: str) -> str:
        self._validate_config()
        return f"{BOX_AUTHORIZE_URL}?{urlencode({'response_type': 'code', 'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'state': state})}"

    def login(
        self,
        *,
        timeout: float = 240.0,
        on_authorize: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        self._validate_config()
        parsed = urlparse(self.redirect_uri)
        callback_path = parsed.path or "/"
        expected_state = secrets.token_urlsafe(24)
        result: dict[str, str] = {}
        completed = threading.Event()

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request = urlparse(self.path)
                if request.path != callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                values = parse_qs(request.query)
                result["code"] = values.get("code", [""])[0]
                result["state"] = values.get("state", [""])[0]
                result["error"] = values.get("error_description", values.get("error", [""]))[0]
                body = "Box 授权已返回，可以关闭此页面并回到微信备份工具。".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                completed.set()

            def log_message(self, *_args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer((parsed.hostname or "127.0.0.1", parsed.port), CallbackHandler)
        server.timeout = 0.5
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = self.authorization_url(expected_state)
            if on_authorize is not None:
                on_authorize(url)
            webbrowser.open(url)
            if not completed.wait(timeout):
                raise TimeoutError("等待 Box 浏览器授权超时")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2.0)
        if result.get("error"):
            raise BoxApiError(f"Box 授权失败：{result['error']}")
        if not result.get("code") or result.get("state") != expected_state:
            raise BoxApiError("Box 授权返回无效或 state 校验失败")
        response = self.session.post(
            BOX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
            },
            timeout=30,
        )
        payload = _json_response(response, "Box 登录")
        self._save_tokens(payload)
        return {"access_token": str(payload.get("access_token", ""))}

    def access_token(self, *, force_refresh: bool = False) -> str:
        self._validate_config()
        values = self._stored()
        access_token = values.get("box_access_token", "")
        try:
            expires_at = float(values.get("box_expires_at", "0"))
        except ValueError:
            expires_at = 0.0
        if access_token and not force_refresh and expires_at > time.time():
            return access_token
        refresh_token = values.get("box_refresh_token", "")
        if not refresh_token:
            raise BoxApiError("尚未登录 Box，请先点击“登录 Box”")
        response = self.session.post(
            BOX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        payload = _json_response(response, "刷新 Box 登录")
        self._save_tokens(payload)
        return str(payload.get("access_token", ""))


class IncrementalBoxUploader(IncrementalArtifactUploader):
    provider_name = "box"

    def __init__(
        self,
        root: Path,
        *,
        oauth: BoxOAuthClient,
        target_folder: str = "WechatRosterBackup",
        remark: str = "微信备份",
        remote_folder: str | None = None,
        interval: float = 1.5,
    ) -> None:
        super().__init__(root, remark=remark, remote_folder=remote_folder, interval=interval)
        self.oauth = oauth
        self.target_folder = target_folder.strip().strip("/") or "WechatRosterBackup"
        self.session = requests.Session()
        self.session.proxies.update(oauth.proxies)
        self._folder_cache: dict[tuple[str, str], str] = {}
        self.run_folder_id = ""

    def _request(self, method: str, url: str, *, retry: bool = True, **kwargs) -> requests.Response:
        token = self.oauth.access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        response = self.session.request(method, url, headers=headers, timeout=90, **kwargs)
        if response.status_code == 401 and retry:
            token = self.oauth.access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            response = self.session.request(method, url, headers=headers, timeout=90, **kwargs)
        if response.status_code >= 400:
            _json_response(response, "Box API")
        return response

    def test_connection(self) -> dict[str, object]:
        response = self._request("GET", f"{BOX_API_URL}/users/me", params={"fields": "id,name,login"})
        value = response.json()
        return value if isinstance(value, dict) else {}

    def _list_folder(self, parent_id: str) -> list[dict[str, object]]:
        offset = 0
        items: list[dict[str, object]] = []
        while True:
            response = self._request(
                "GET",
                f"{BOX_API_URL}/folders/{parent_id}/items",
                params={"fields": "id,type,name", "limit": 1000, "offset": offset},
            )
            payload = response.json()
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            items.extend(item for item in entries if isinstance(item, dict))
            total = int(payload.get("total_count", len(items))) if isinstance(payload, dict) else len(items)
            if not entries or len(items) >= total:
                return items
            offset += len(entries)

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        for item in self._list_folder(parent_id):
            if item.get("type") == "folder" and item.get("name") == name:
                folder_id = str(item["id"])
                self._folder_cache[key] = folder_id
                return folder_id
        response = self._request(
            "POST",
            f"{BOX_API_URL}/folders",
            json={"name": name, "parent": {"id": parent_id}},
        )
        folder_id = str(response.json()["id"])
        self._folder_cache[key] = folder_id
        return folder_id

    def _ensure_path(self, parent_id: str, parts: list[str]) -> str:
        current = parent_id
        for part in parts:
            if part:
                current = self._ensure_folder(current, part)
        return current

    def _prepare_remote(self) -> None:
        parent = self._ensure_path("0", [part for part in self.target_folder.split("/") if part])
        self.run_folder_id = self._ensure_folder(parent, self.remote_folder)

    def _find_file(self, parent_id: str, name: str) -> str:
        for item in self._list_folder(parent_id):
            if item.get("type") == "file" and item.get("name") == name:
                return str(item["id"])
        return ""

    def _upload_file(self, path: Path, rel: str, size: int) -> None:
        if not self.run_folder_id:
            self._prepare_remote()
        parts = rel.split("/")
        parent_id = self._ensure_path(self.run_folder_id, parts[:-1])
        existing_id = self._find_file(parent_id, parts[-1])
        if size <= DIRECT_UPLOAD_LIMIT:
            self._direct_upload(path, parent_id, existing_id)
        else:
            self._session_upload(path, parent_id, existing_id)

    def _direct_upload(self, path: Path, parent_id: str, existing_id: str) -> None:
        if existing_id:
            url = f"{BOX_UPLOAD_URL}/files/{existing_id}/content"
            fields: list[tuple[str, str]] = []
        else:
            url = f"{BOX_UPLOAD_URL}/files/content"
            attributes = json.dumps({"name": path.name, "parent": {"id": parent_id}}, ensure_ascii=False)
            fields = [("attributes", attributes)]
        body = _MultipartStream(path, fields)
        self._request(
            "POST",
            url,
            data=body,
            headers={"Content-Type": body.content_type, "Content-Length": str(len(body))},
        )

    def _session_upload(self, path: Path, parent_id: str, existing_id: str) -> None:
        if existing_id:
            url = f"{BOX_UPLOAD_URL}/files/{existing_id}/upload_sessions"
            payload = {"file_size": path.stat().st_size}
        else:
            url = f"{BOX_UPLOAD_URL}/files/upload_sessions"
            payload = {"folder_id": parent_id, "file_size": path.stat().st_size, "file_name": path.name}
        session_info = self._request("POST", url, json=payload).json()
        upload_url = str(session_info["session_endpoints"]["upload_part"])
        commit_url = str(session_info["session_endpoints"]["commit"])
        part_size = int(session_info["part_size"])
        total = path.stat().st_size
        parts: list[dict[str, object]] = []
        overall = hashlib.sha1()
        offset = 0
        with path.open("rb") as stream:
            while chunk := stream.read(part_size):
                digest = base64.b64encode(hashlib.sha1(chunk).digest()).decode("ascii")
                overall.update(chunk)
                end = offset + len(chunk) - 1
                response = self._request(
                    "PUT",
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                        "Digest": f"sha={digest}",
                        "Content-Length": str(len(chunk)),
                    },
                )
                part = response.json().get("part", {})
                parts.append(part)
                offset = end + 1
        digest = base64.b64encode(overall.digest()).decode("ascii")
        response = self._request(
            "POST",
            commit_url,
            json={"parts": parts},
            headers={"Digest": f"sha={digest}"},
        )
        if response.status_code == 202:
            retry_after = max(1, int(response.headers.get("Retry-After", "1")))
            time.sleep(min(retry_after, 10))


def _json_response(response: requests.Response, action: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error_description") or payload.get("error") or "")
        suffix = f"：{detail}" if detail else ""
        raise BoxApiError(f"{action}失败，HTTP {response.status_code}{suffix}")
    if not isinstance(payload, dict):
        raise BoxApiError(f"{action}返回格式异常")
    return payload
