"""Optional bridge to the external pywechat2/pyweixin UIA package.

The bridge is deliberately lazy: the main OCR workflow does not require
pywechat2 to be installed. Callers provide the checkout via ``--pywechat-root``
or ``PYWECHAT2_ROOT``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


class PyWechatUnavailable(RuntimeError):
    """Raised when the optional pywechat2 package cannot be loaded."""


def load_package(root: Path | None = None) -> tuple[ModuleType, ModuleType, ModuleType]:
    """Load ``pyweixin``, ``Navigator`` and ``GlobalConfig`` from a checkout."""
    if root is not None:
        source = root / "src"
        if not (source / "pywechat2").is_dir() and not (source / "pyweixin").is_dir():
            raise PyWechatUnavailable(f"未找到 pywechat2 源码目录: {source}")
        source_text = str(source.resolve())
        if source_text not in sys.path:
            sys.path.insert(0, source_text)
    try:
        package = importlib.import_module("pyweixin")
        tools = importlib.import_module("pyweixin.WeChatTools")
        elements = importlib.import_module("pyweixin.Uielements")
    except Exception as error:  # pragma: no cover - dependency/version specific
        raise PyWechatUnavailable(f"pywechat2 加载失败: {type(error).__name__}: {error}") from error
    return package, tools, elements


def open_contacts(root: Path | None = None):
    """Open the Contacts page without closing Weixin and return its UIA list."""
    package, _, _ = load_package(root)
    package.GlobalConfig.close_weixin = False
    return package.Navigator.open_contacts()


def section_items(contact_list, class_name: str = "mmui::ContactsCellGroupView"):
    """Return currently materialized section headers in the virtual list."""
    return [
        item
        for item in contact_list.children(control_type="ListItem")
        if item.class_name() == class_name
    ]


def section_item(contact_list, labels: tuple[str, ...]):
    """Find a materialized section by its localized prefix."""
    wanted = tuple(label.casefold() for label in labels)
    return next(
        (
            item
            for item in section_items(contact_list)
            if item.window_text().strip().casefold().startswith(wanted)
        ),
        None,
    )
