"""Save screenshots of visible Weixin Contacts entries using optional UIA."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from PIL import ImageGrab

import pywechat2_adapter


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Screenshot visible Weixin contacts via pywechat2 UIA")
    result.add_argument("--pywechat-root", type=Path, help="pywechat2 checkout (or PYWECHAT2_ROOT)")
    result.add_argument("-s", "--limit", type=int, default=10, help="maximum entries to click (default: 10)")
    result.add_argument("-o", "--output", type=Path, default=Path("artifacts/uia-contacts"))
    result.add_argument("--groups", action="store_true", help="use Saved Groups instead of Contacts")
    return result


def capture_window(window, output: Path) -> None:
    rect = window.rectangle()
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    image.save(output)


def main() -> int:
    args = parser().parse_args()
    if args.limit < 1:
        print("--limit 必须大于 0")
        return 2
    bundled_root = Path(sys.executable).resolve().parent / "pywechat2"
    root = args.pywechat_root or (
        Path(os.environ["PYWECHAT2_ROOT"])
        if os.environ.get("PYWECHAT2_ROOT")
        else (bundled_root if bundled_root.is_dir() else None)
    )
    try:
        contact_list, window = pywechat2_adapter.open_contacts(root)
        _, tools, _ = pywechat2_adapter.load_package(root)
        tools.Tools.collapse_contacts(window, contact_list)
    except Exception as error:
        if not isinstance(error, pywechat2_adapter.PyWechatUnavailable):
            error = pywechat2_adapter.PyWechatUnavailable(
                f"UIA 不可见或 pywechat2 与当前微信版本不兼容: {type(error).__name__}: {error}"
            )
        print(json.dumps({"ok": False, "reason": "pywechat2_unavailable", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2

    labels = ("Saved Groups", "保存的群聊", "群聊") if args.groups else ("Contacts", "联系人", "聯絡人")
    section = pywechat2_adapter.section_item(contact_list, labels)
    if section is None:
        print(json.dumps({"ok": False, "reason": "section_not_visible", "section": labels}, ensure_ascii=False, indent=2))
        return 3
    section.click_input()
    time.sleep(0.4)

    args.output.mkdir(parents=True, exist_ok=True)
    screenshots: list[str] = []
    seen_ids: set[tuple[int, ...]] = set()
    for index in range(args.limit):
        entries = [
            item
            for item in contact_list.children(control_type="ListItem")
            if item.class_name() != "mmui::ContactsCellGroupView"
            and item.window_text().strip()
        ]
        entry = next((item for item in entries if tuple(item.element_info.runtime_id) not in seen_ids), None)
        if entry is None:
            break
        seen_ids.add(tuple(entry.element_info.runtime_id))
        entry.click_input()
        time.sleep(0.25)
        output = args.output / f"contact-{index + 1:03d}.png"
        capture_window(window, output)
        screenshots.append(str(output.resolve()))
        contact_list.type_keys("{DOWN}")

    result = {
        "ok": True,
        "section": "saved_groups" if args.groups else "contacts",
        "screenshots": screenshots,
        "count": len(screenshots),
        "data_extraction": False,
        "wxid_extraction": False,
    }
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
