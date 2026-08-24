"""Store cloud credentials with Windows DPAPI instead of plaintext config."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import win32crypt


def save_secret(path: Path, values: dict[str, str]) -> None:
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    encrypted = win32crypt.CryptProtectData(payload, "WechatRosterCloud", None, None, None, 0)
    path.write_text(base64.b64encode(encrypted).decode("ascii"), encoding="ascii")


def load_secret(path: Path) -> dict[str, str]:
    try:
        encrypted = base64.b64decode(path.read_text(encoding="ascii"))
        payload = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]
        value = json.loads(payload.decode("utf-8"))
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
    except Exception:
        return {}


def update_secret(path: Path, values: dict[str, str]) -> None:
    current = load_secret(path)
    current.update({key: value for key, value in values.items() if value})
    save_secret(path, current)


def delete_secret_keys(path: Path, *keys: str) -> None:
    current = load_secret(path)
    for key in keys:
        current.pop(key, None)
    if current:
        save_secret(path, current)
    else:
        path.unlink(missing_ok=True)


def delete_secret(path: Path) -> None:
    path.unlink(missing_ok=True)
