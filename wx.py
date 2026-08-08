"""Fast, single-target navigation and screenshot helper for desktop Weixin."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import win32api
import win32con
from PIL import Image

import open_group
import quick_capture
import wechat_group_roster_audit as audit


CHAT_NAV = (0.035, 0.14)
CONTACTS_NAV = (0.035, 0.205)
SEARCH_FIELD = (0.205, 0.07)
LIST_SCROLL_POINT = (0.25, 0.72)
LEFT_PANE = (0.065, 0.035, 0.36, 0.96)
LIST_SCROLL_DELTA = -4800
LIST_TOP_SCROLL_DELTA = 12000
SAVED_GROUPS_TEXT_LEFT_RATIO = 0.24
SAVED_GROUPS_TEXT_SCALE = 3
MAX_PAGES = 20
NAVIGATION_WAIT_SECONDS = 0.45
SEARCH_FOCUS_WAIT_SECONDS = 0.2
SEARCH_RESULT_WAIT_NO_OCR_SECONDS = 0.55
SEARCH_RESULT_WAIT_OCR_SECONDS = 1.0
JOIN_BUTTON_REGION = (0.55, 0.3, 0.84, 0.56)
MIN_JOIN_BUTTON_GREEN_PIXELS = 800
CHAT_OPEN_ATTEMPTS = 2
CHAT_OPEN_WAIT_SECONDS = 0.8


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Navigate one Weixin list, optionally search, and save screenshots"
    )
    result.add_argument("-m", choices=("chat", "saved"), default="chat")
    result.add_argument("-f", action="store_true", help="target a friend instead of a group")
    result.add_argument("-q", metavar="TEXT", help="single search text")
    result.add_argument("-n", action="store_true", help="skip OCR; keep results open and screenshot")
    result.add_argument("-s", type=int, default=1, metavar="N", help="save N visible pages (max 20)")
    result.add_argument("-o", type=Path, default=Path("artifacts/wx"))
    result.add_argument("-p", type=int, help="explicit Weixin PID")
    result.add_argument("--config", type=Path, default=audit.DEFAULT_PANEL_CONFIG)
    result.add_argument("--tesseract", type=Path)
    return result


def point_in_window(
    window: dict[str, object],
    ratios: tuple[float, float],
) -> tuple[int, int]:
    return (
        int(window["left"]) + round(int(window["width"]) * ratios[0]),
        int(window["top"]) + round(int(window["height"]) * ratios[1]),
    )


def crop_left_pane(image: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    width, height = image.size
    box = (
        round(width * LEFT_PANE[0]),
        round(height * LEFT_PANE[1]),
        round(width * LEFT_PANE[2]),
        round(height * LEFT_PANE[3]),
    )
    return image.crop(box), (box[0], box[1])


def section_kind(text: str) -> str | None:
    normalized = open_group.normalize_text(text)
    if normalized == "mostused":
        return "most_used"
    if "group" in normalized and "chat" in normalized:
        return "groups"
    if "savedgroup" in normalized:
        return "saved_groups"
    if normalized == "contacts":
        return "contacts"
    if "officialaccounts" in normalized:
        return "official_accounts"
    if "wecomcontacts" in normalized:
        return "wecom_contacts"
    if "myenterprise" in normalized:
        return "my_enterprise"
    if "chathistory" in normalized:
        return "chat_history"
    if "internetsearchresults" in normalized:
        return "internet"
    if "serviceaccounts" in normalized:
        return "service_accounts"
    return None


def allowed_sections(mode: str, friend: bool) -> set[str]:
    if mode == "saved":
        return {"contacts"} if friend else {"saved_groups"}
    return {"most_used", "contacts"} if friend else {"most_used", "groups"}


def find_exact_result(
    lines: list[open_group.OcrLine],
    query: str,
    allowed: set[str],
) -> open_group.OcrLine | None:
    headings = [(line, section_kind(line.text)) for line in lines]
    headings = [(line, kind) for line, kind in headings if kind is not None]
    expected = open_group.normalize_text(query)
    for heading, kind in headings:
        if kind not in allowed:
            continue
        end_top = min(
            (line.top for line, _ in headings if line.top > heading.bottom),
            default=float("inf"),
        )
        matches = [
            line
            for line in lines
            if heading.bottom < line.top < end_top
            and expected in open_group.normalize_text(line.text)
        ]
        if matches:
            return min(matches, key=lambda line: (line.top, line.left))
    return None


SAVED_GROUPS_END_SECTIONS = {
    "contacts",
    "official_accounts",
    "service_accounts",
    "wecom_contacts",
    "my_enterprise",
}


def saved_group_section_bounds(
    lines: list[open_group.OcrLine],
    section_started: bool,
) -> tuple[int | None, int | None, bool]:
    """Return the visible Saved Groups text bounds and whether the section continues."""
    headings = [(line, section_kind(line.text)) for line in lines]
    saved_heading = next(
        (line for line, kind in headings if kind == "saved_groups"),
        None,
    )
    if saved_heading is not None:
        section_started = True
        start_top = saved_heading.bottom
    elif section_started:
        start_top = -1
    else:
        return None, None, False

    end_top = min(
        (
            line.top
            for line, kind in headings
            if kind in SAVED_GROUPS_END_SECTIONS and line.top > start_top
        ),
        default=float("inf"),
    )
    return start_top, (None if end_top == float("inf") else int(end_top)), end_top == float("inf")


def find_saved_group_result(
    lines: list[open_group.OcrLine],
    query: str,
    section_started: bool,
) -> tuple[open_group.OcrLine | None, bool]:
    """Find one group while preserving the Saved Groups section across pages."""
    start_top, end_top, continue_scanning = saved_group_section_bounds(lines, section_started)
    if start_top is None:
        return None, False
    expected = open_group.normalize_text(query)
    match = next(
        (
            line
            for line in lines
            if line.top > start_top
            and (end_top is None or line.top < end_top)
            and expected in open_group.normalize_text(line.text)
        ),
        None,
    )
    return match, continue_scanning


def find_saved_group_in_expanded_text(
    tesseract: Path,
    image_path: Path,
    query: str,
    start_top: int,
    end_top: int | None,
) -> open_group.OcrLine | None:
    """OCR only the visible Saved Groups labels at a readable scale."""
    with Image.open(image_path) as image:
        left = round(image.width * SAVED_GROUPS_TEXT_LEFT_RATIO)
        bottom = image.height if end_top is None else end_top
        if bottom <= start_top:
            return None
        crop = image.crop((left, start_top, image.width, bottom))
    enlarged = crop.resize(
        (crop.width * SAVED_GROUPS_TEXT_SCALE, crop.height * SAVED_GROUPS_TEXT_SCALE),
        Image.Resampling.LANCZOS,
    )
    expanded_path = image_path.with_name(f"{image_path.stem}-text.png")
    enlarged.save(expanded_path)
    expected = open_group.normalize_text(query)
    for line in open_group.run_ocr(tesseract, expanded_path, psm=6):
        if expected in open_group.normalize_text(line.text):
            return open_group.OcrLine(
                line.text,
                left + line.left // SAVED_GROUPS_TEXT_SCALE,
                start_top + line.top // SAVED_GROUPS_TEXT_SCALE,
                left + line.right // SAVED_GROUPS_TEXT_SCALE,
                start_top + line.bottom // SAVED_GROUPS_TEXT_SCALE,
            )
    return None


def capture_full_window(
    window: dict[str, object],
    output: Path,
) -> tuple[Image.Image, dict[str, object]]:
    image, metadata = audit.capture_window_image(window, "window")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return image, metadata


def capture_live_window(
    window: dict[str, object],
    output: Path,
) -> tuple[Image.Image, dict[str, object]]:
    """Capture the currently displayed frame after interactive UI changes."""
    open_group.desktop_window_capture(window, output)
    with Image.open(output) as captured:
        image = captured.copy()
    return image, {"source": "screen"}


def verify_opened_title(tesseract: Path, image_path: Path, query: str) -> bool:
    """Check whether one explicitly selected chat is visible in the chat header."""
    with Image.open(image_path) as opened:
        width, height = opened.size
        header = opened.crop((round(width * 0.3), 0, width, round(height * 0.13)))
    header_path = image_path.with_name(f"{image_path.stem}-header.png")
    header.save(header_path)
    expected = open_group.normalize_text(query)
    return any(
        expected in open_group.normalize_text(line.text)
        for line in open_group.run_ocr(tesseract, header_path)
    )


def saved_group_preview_state(image: Image.Image) -> str:
    """Classify a saved-group preview without clicking its state-changing controls."""
    width, height = image.size
    left = round(width * JOIN_BUTTON_REGION[0])
    top = round(height * JOIN_BUTTON_REGION[1])
    right = round(width * JOIN_BUTTON_REGION[2])
    bottom = round(height * JOIN_BUTTON_REGION[3])
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    green_pixels = sum(
        red < 90 and green > 130 and blue < 150 and green - red > 70 and green - blue > 30
        for red, green, blue in crop.getdata()
    )
    return "join_group" if green_pixels >= MIN_JOIN_BUTTON_GREEN_PIXELS else "unknown"


def click_result_and_verify_chat(
    window: dict[str, object],
    result_point: tuple[int, int],
    tesseract: Path,
    output_dir: Path,
    query: str,
) -> tuple[dict[str, object], bool, int, Path]:
    """Click one verified result, retrying only that same point if no chat header appears."""
    opened_path = output_dir / "opened.png"
    metadata: dict[str, object] = {}
    for attempt in range(1, CHAT_OPEN_ATTEMPTS + 1):
        open_group.click_screen_point(result_point)
        time.sleep(CHAT_OPEN_WAIT_SECONDS)
        _, metadata = capture_live_window(window, opened_path)
        if verify_opened_title(tesseract, opened_path, query):
            return metadata, True, attempt, opened_path
    return metadata, False, CHAT_OPEN_ATTEMPTS, opened_path


def scroll_list(window: dict[str, object]) -> None:
    win32api.SetCursorPos(point_in_window(window, LIST_SCROLL_POINT))
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, LIST_SCROLL_DELTA, 0)
    time.sleep(0.65)


def scroll_list_to_top(window: dict[str, object]) -> None:
    """Return the Contacts list to its first visible entry before a saved-group scan."""
    win32api.SetCursorPos(point_in_window(window, LIST_SCROLL_POINT))
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, LIST_TOP_SCROLL_DELTA, 0)
    time.sleep(0.65)


def save_visible_pages(
    window: dict[str, object],
    directory: Path,
    count: int,
    *,
    live: bool,
) -> list[str]:
    outputs = []
    for index in range(count):
        output = directory / f"page-{index + 1:02d}.png"
        capture = capture_live_window if live else capture_full_window
        full_image, _ = capture(window, output)
        list_image, _ = crop_left_pane(full_image)
        list_image.save(output)
        outputs.append(str(output.resolve()))
        if index + 1 < count:
            scroll_list(window)
    return outputs


def select_pid(explicit_pid: int | None, config: Path) -> tuple[int | None, str]:
    if explicit_pid is not None:
        return explicit_pid, "command_line"
    pid, status = quick_capture.configured_pid(config)
    return pid, str(config.resolve()) if pid is not None else status


def find_saved_group(
    window: dict[str, object],
    query: str,
    directory: Path,
    page_limit: int,
    tesseract: Path,
) -> tuple[open_group.OcrLine | None, tuple[int, int], float, list[str]]:
    ocr_seconds = 0.0
    screenshots = []
    section_started = False
    for index in range(page_limit):
        full_path = directory / f"saved-search-{index + 1:02d}.png"
        full_image, _ = capture_live_window(window, full_path)
        list_image, crop_offset = crop_left_pane(full_image)
        list_image.save(full_path)
        screenshots.append(str(full_path.resolve()))
        started = time.perf_counter()
        lines = open_group.run_ocr(tesseract, full_path)
        ocr_seconds += time.perf_counter() - started
        start_top, end_top, continue_scanning = saved_group_section_bounds(lines, section_started)
        section_started = continue_scanning
        match = None
        if start_top is not None:
            expected = open_group.normalize_text(query)
            match = next(
                (
                    line
                    for line in lines
                    if line.top > start_top
                    and (end_top is None or line.top < end_top)
                    and expected in open_group.normalize_text(line.text)
                ),
                None,
            )
            if match is None:
                started = time.perf_counter()
                match = find_saved_group_in_expanded_text(
                    tesseract, full_path, query, start_top, end_top
                )
                ocr_seconds += time.perf_counter() - started
        if match is not None:
            return match, crop_offset, ocr_seconds, screenshots
        if continue_scanning and index + 1 < page_limit:
            scroll_list(window)
        elif not continue_scanning:
            break
    return None, (0, 0), ocr_seconds, screenshots


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.s <= MAX_PAGES:
        print(f"-s 必须在 1 到 {MAX_PAGES} 之间。")
        return 2
    if args.m == "saved" and not args.f and args.q is not None and args.n:
        print("保存的群聊没有独立搜索框；-m saved -q 需要 OCR，请移除 -n。")
        return 2
    if args.m == "saved" and args.f and args.q is None:
        # Saved + friend means the regular Contacts list, not Saved Groups.
        target_label = "contacts"
    else:
        target_label = "friend" if args.f else "group"

    pid, pid_source = select_pid(args.p, args.config)
    if pid is None:
        print(f"无法确定微信 PID（{pid_source}）。请传 -p 或重新校准。")
        return 2
    window = audit.select_weixin_window(pid)
    if window is None:
        print(f"未找到 PID {pid} 的微信主窗口。")
        return 2
    activation = audit.activate_window(window)
    if not activation["activated"]:
        print(json.dumps({"activation": activation}, ensure_ascii=False, indent=2))
        return 2
    window = activation["window"]
    hwnd = int(window["hwnd"])

    nav_point = point_in_window(window, CHAT_NAV if args.m == "chat" else CONTACTS_NAV)
    open_group.click_screen_point(nav_point)
    time.sleep(NAVIGATION_WAIT_SECONDS)

    saved_group_search = args.m == "saved" and not args.f and args.q is not None
    search_point = None
    if args.q is not None and not saved_group_search:
        search_point = point_in_window(window, SEARCH_FIELD)
        open_group.click_screen_point(search_point)
        time.sleep(SEARCH_FOCUS_WAIT_SECONDS)
        active, focus, caret = open_group.gui_thread_handles()
        if (active, focus, caret) != (hwnd, hwnd, hwnd):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "search_did_not_receive_caret",
                        "handles": {"active": active, "focus": focus, "caret": caret},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        open_group.select_all()
        open_group.send_unicode_text(args.q)
        time.sleep(
            SEARCH_RESULT_WAIT_NO_OCR_SECONDS
            if args.n
            else SEARCH_RESULT_WAIT_OCR_SECONDS
        )

    args.o.mkdir(parents=True, exist_ok=True)
    if args.n or args.q is None:
        outputs = save_visible_pages(
            window,
            args.o,
            args.s,
            live=args.q is not None or args.s > 1,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "ocr": False,
                    "mode": args.m,
                    "target": target_label,
                    "query": args.q,
                    "pages": outputs,
                    "target_pid": pid,
                    "pid_source": pid_source,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    tesseract = open_group.resolve_tesseract(args.tesseract)
    if tesseract is None:
        print("未找到 Tesseract。使用 -n 可跳过 OCR 直接截图。")
        return 2
    if saved_group_search:
        scroll_list_to_top(window)
        try:
            match, crop_offset, ocr_seconds, screenshots = find_saved_group(
                window, args.q, args.o, args.s, tesseract
            )
        except RuntimeError as error:
            print(f"无法识别保存的群聊列表：{error}")
            return 2
        if match is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "exact_group_not_found_in_saved_pages",
                        "mode": args.m,
                        "target": target_label,
                        "query": args.q,
                        "pages_checked": len(screenshots),
                        "ocr_seconds": round(ocr_seconds, 3),
                        "screenshots": screenshots,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        result_point = (
            int(window["left"]) + crop_offset[0] + (match.left + match.right) // 2,
            int(window["top"]) + crop_offset[1] + (match.top + match.bottom) // 2,
        )
        open_group.click_screen_point(result_point)
        time.sleep(1.0)
        try:
            opened_path = args.o / "opened.png"
            opened_image, opened_metadata = capture_live_window(window, opened_path)
            title_verified = verify_opened_title(tesseract, opened_path, args.q)
            preview_state = saved_group_preview_state(opened_image)
        except RuntimeError as error:
            print(f"已点击匹配项，但无法截取打开后的画面：{error}")
            return 2
        opened = {
            "ok": True,
            "reason": None,
            "ocr": True,
            "ocr_seconds": round(ocr_seconds, 3),
            "mode": args.m,
            "target": target_label,
            "query": args.q,
            "pages_checked": len(screenshots),
            "match": match._asdict(),
            "click": list(result_point),
            "selected": True,
            "view": "saved_group_preview",
            "chat_opened": title_verified,
            "preview_state": preview_state,
            "join_required": preview_state == "join_group",
            "title_verified": title_verified,
            "opened_screenshot": str(opened_path.resolve()),
            "image_source": opened_metadata,
            "target_pid": pid,
            "pid_source": pid_source,
        }
        print(
            json.dumps(
                opened,
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        full_image, metadata = capture_live_window(window, args.o / "search.png")
    except RuntimeError as error:
        print(f"无法截取当前搜索画面：{error}")
        return 2
    left_image, crop_offset = crop_left_pane(full_image)
    left_path = args.o / "search-left.png"
    left_image.save(left_path)
    started = time.perf_counter()
    lines = open_group.run_ocr(tesseract, left_path)
    ocr_seconds = time.perf_counter() - started
    match = find_exact_result(lines, args.q, allowed_sections(args.m, args.f))
    if match is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "exact_result_not_found_in_allowed_section",
                    "mode": args.m,
                    "target": target_label,
                    "query": args.q,
                    "ocr_seconds": round(ocr_seconds, 3),
                    "screenshot": str((args.o / "search.png").resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3

    result_point = (
        int(window["left"]) + crop_offset[0] + (match.left + match.right) // 2,
        int(window["top"]) + crop_offset[1] + (match.top + match.bottom) // 2,
    )
    try:
        opened_metadata, title_verified, click_attempts, opened_path = click_result_and_verify_chat(
            window, result_point, tesseract, args.o, args.q
        )
    except RuntimeError as error:
        print(f"已点击匹配项，但无法截取打开后的画面：{error}")
        return 2
    opened = {
        "ok": title_verified,
        "reason": None if title_verified else "clicked_result_but_chat_title_not_verified",
        "ocr": True,
        "ocr_seconds": round(ocr_seconds, 3),
        "mode": args.m,
        "target": target_label,
        "query": args.q,
        "match": match._asdict(),
        "click": list(result_point),
        "click_attempts": click_attempts,
        "clicked": True,
        "title_verified": title_verified,
        "opened_screenshot": str(opened_path.resolve()),
        "image_source": opened_metadata,
        "target_pid": pid,
        "pid_source": pid_source,
    }
    print(
        json.dumps(
            opened,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if title_verified else 3


if __name__ == "__main__":
    raise SystemExit(main())
