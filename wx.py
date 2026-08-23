"""Fast, single-target navigation and screenshot helper for desktop Weixin."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageChops, ImageStat

import open_group
import quick_capture
import wechat_group_roster_audit as audit


CHAT_NAV = (55, 170)
CONTACTS_NAV = (55, 242)
SEARCH_FIELD = (0.205, 0.07)
LIST_SCROLL_POINT = (0.25, 0.72)
LEFT_PANE = (0.065, 0.035, 0.36, 0.96)
LIST_SCROLL_DELTA = -12000
LIST_TOP_SCROLL_DELTA = 12000
LIST_TOP_SCROLL_STEPS = 3
LIST_SCROLL_SETTLE_SECONDS = 0.25
CONTACTS_SCROLLBAR_X_RATIO = 0.335
CONTACTS_SCROLLBAR_THUMB_Y_RATIO = 0.17
CONTACTS_SCROLLBAR_TOP_Y_RATIO = 0.083
SAVED_GROUPS_TEXT_LEFT_RATIO = 0.24
SAVED_GROUPS_TEXT_SCALE = 3
MAX_PAGES = 1000
UNCHANGED_PAGE_MEAN_DIFFERENCE = 0.5
SCROLLBAR_MIN_NEUTRAL_RUN = 10
SCROLLBAR_BOTTOM_RATIO = 0.94
NAVIGATION_WAIT_SECONDS = 0.45
SEARCH_FOCUS_WAIT_SECONDS = 0.2
SEARCH_RESULT_WAIT_NO_OCR_SECONDS = 0.55
SEARCH_RESULT_WAIT_OCR_SECONDS = 1.0
JOIN_BUTTON_REGION = (0.55, 0.3, 0.84, 0.56)
MIN_JOIN_BUTTON_GREEN_PIXELS = 800
CHAT_OPEN_ATTEMPTS = 2
CHAT_OPEN_WAIT_SECONDS = 0.8
CONTACT_DETAIL_WAIT_SECONDS = 0.55
AUXILIARY_WINDOW_WAIT_SECONDS = 4.0
AUXILIARY_WINDOW_POLL_SECONDS = 0.1
CONTACT_ROW_MIN_TOP = 90
CONTACT_ROW_MAX_TOP_RATIO = 0.92
CONTACT_ROW_MIN_GAP = 18
# Stable point inside the lowest visible contact row. This intentionally does
# not depend on OCR; the same point remains valid while the list scrolls.
CONTACT_FIXED_CLICK_POINT = (0.23, 0.86)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Navigate one Weixin list, optionally search, and save screenshots"
    )
    result.add_argument("-m", choices=("chat", "saved"), default="chat")
    result.add_argument("-f", action="store_true", help="target a friend instead of a group")
    result.add_argument("-r", "--recent", action="store_true", help="capture contacts from recent direct chats")
    result.add_argument("-q", metavar="TEXT", help="single search text")
    result.add_argument("-n", action="store_true", help="skip OCR; keep results open and screenshot")
    result.add_argument(
        "-s",
        type=int,
        metavar="N",
        help="maximum screenshots; omit to scroll until the list stops (max 1000)",
    )
    result.add_argument("-o", type=Path, default=Path("artifacts/wx"))
    result.add_argument("-p", type=int, help="explicit Weixin PID")
    result.add_argument("--config", type=Path, default=audit.DEFAULT_PANEL_CONFIG)
    result.add_argument("--tesseract", type=Path)
    return result


def emit_result(directory: Path, payload: dict[str, object]) -> None:
    """Persist the machine-readable result before writing it to the console."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    (directory / "result.json").write_text(f"{serialized}\n", encoding="utf-8")
    print(serialized)


def consume_skip_request() -> bool:
    """Consume the GUI skip marker once and return whether one was requested."""
    marker = Path(os.environ.get("WECHAT_SKIP_FILE", "skip.request"))
    if not marker.exists():
        return False
    try:
        marker.unlink()
    except OSError:
        return False
    return True


def point_in_window(
    window: dict[str, object],
    ratios: tuple[float, float],
) -> tuple[int, int]:
    return (
        int(window["left"]) + round(int(window["width"]) * ratios[0]),
        int(window["top"]) + round(int(window["height"]) * ratios[1]),
    )


def sidebar_point(
    window: dict[str, object], offset: tuple[int, int]
) -> tuple[int, int]:
    """Return a fixed Weixin sidebar point; sidebar icons do not stretch vertically."""
    return int(window["left"]) + offset[0], int(window["top"]) + offset[1]


def crop_left_pane(image: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    width, height = image.size
    box = (
        round(width * LEFT_PANE[0]),
        round(height * LEFT_PANE[1]),
        round(width * LEFT_PANE[2]),
        round(height * LEFT_PANE[3]),
    )
    return image.crop(box), (box[0], box[1])


def screen_point_from_capture(
    window: dict[str, object], image: Image.Image, x: int, y: int
) -> tuple[int, int]:
    """Map a pixel in a captured Weixin window back to a screen point."""
    return (
        int(window["left"]) + round(x * int(window["width"]) / image.width),
        int(window["top"]) + round(y * int(window["height"]) / image.height),
    )


def left_pane_lines(
    lines: list[open_group.OcrLine], image: Image.Image
) -> list[open_group.OcrLine]:
    """Keep section headings found in the actual Contacts pane, not the preview."""
    return [line for line in lines if line.left < round(image.width * LEFT_PANE[2])]


def section_kind(text: str) -> str | None:
    normalized = open_group.normalize_text(text)
    if normalized[:1] in {">", "v", "y"}:
        normalized = normalized[1:]
    if normalized in {"mostused", "最常使用"}:
        return "most_used"
    if "savedgroup" in normalized or "保存的群聊" in normalized:
        return "saved_groups"
    if ("group" in normalized and "chat" in normalized) or "群聊" in normalized:
        return "groups"
    if normalized in {"contacts", "联系人"}:
        return "contacts"
    if "officialaccounts" in normalized or "公众号" in normalized:
        return "official_accounts"
    if "wecomcontacts" in normalized or "企业微信联系人" in normalized:
        return "wecom_contacts"
    if "myenterprise" in normalized or "我的企业" in normalized:
        return "my_enterprise"
    if "chathistory" in normalized or "聊天记录" in normalized:
        return "chat_history"
    if "internetsearchresults" in normalized or "互联网搜索结果" in normalized:
        return "internet"
    if "serviceaccounts" in normalized or "服务号" in normalized:
        return "service_accounts"
    return None


def ocr_match_key(text: str) -> str:
    """Normalize high-frequency OCR substitutions used in Weixin names."""
    return open_group.normalize_text(text).translate(str.maketrans({"未": "末"}))


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
    expected = ocr_match_key(query)
    if "most_used" in allowed and headings:
        first_heading_top = min(line.top for line, _kind in headings)
        top_matches = [
            line
            for line in lines
            if line.top < first_heading_top and expected in ocr_match_key(line.text)
        ]
        if top_matches:
            return min(top_matches, key=lambda line: (line.top, line.left))
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
            and expected in ocr_match_key(line.text)
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


def saved_group_heading(lines: list[open_group.OcrLine]) -> open_group.OcrLine | None:
    return next((line for line in lines if section_kind(line.text) == "saved_groups"), None)


def saved_groups_is_collapsed(
    lines: list[open_group.OcrLine], heading: open_group.OcrLine
) -> bool:
    """Recognize the closed disclosure row without reading any group names."""
    label = heading.text.lstrip()
    if label.startswith(">"):
        return True
    _, end_top, _ = saved_group_section_bounds(lines, False)
    return end_top is not None and end_top - heading.bottom < 70


def expand_saved_groups(
    window: dict[str, object],
    tesseract: Path,
    directory: Path,
) -> bool:
    """Expand the Saved Groups section header without selecting a group entry."""
    for attempt in range(2):
        frame = directory / ".saved-groups-expand-frame.png"
        full_image, _ = capture_live_window(window, frame)
        frame.unlink(missing_ok=True)
        ocr_path = directory / ".saved-groups-expand-ocr.png"
        full_image.save(ocr_path)
        lines = open_group.run_ocr(tesseract, ocr_path, psm=11, language="chi_sim+eng")
        ocr_path.unlink(missing_ok=True)
        lines = left_pane_lines(lines, full_image)
        heading = saved_group_heading(lines)
        if heading is not None:
            break
        if attempt == 0:
            scroll_list_to_top(window)
    else:
        return False

    if not saved_groups_is_collapsed(lines, heading):
        return True

    # Click the discovered header row, not a hard-coded disclosure-arrow coordinate.
    point = screen_point_from_capture(
        window,
        full_image,
        (heading.left + heading.right) // 2,
        (heading.top + heading.bottom) // 2,
    )
    open_group.click_screen_point(point)
    time.sleep(NAVIGATION_WAIT_SECONDS)

    verification_frame = directory / ".saved-groups-expand-verify-frame.png"
    verification_image, _ = capture_live_window(window, verification_frame)
    verification_frame.unlink(missing_ok=True)
    verification_path = directory / ".saved-groups-expand-verify-ocr.png"
    verification_image.save(verification_path)
    verification_lines = open_group.run_ocr(
        tesseract, verification_path, psm=11, language="chi_sim+eng"
    )
    verification_path.unlink(missing_ok=True)
    verification_lines = left_pane_lines(verification_lines, verification_image)
    verification_heading = saved_group_heading(verification_lines)
    return (
        verification_heading is not None
        and not saved_groups_is_collapsed(verification_lines, verification_heading)
        and page_changed(full_image, verification_image)
    )


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
    # WeChat's Qt surface may reject PrintWindow and return a blank frame.
    # Auto mode falls back to a desktop capture when the window is visible.
    image, metadata = audit.capture_window_image(window, "auto")
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
    # A blank Qt surface is recoverable by the same one-cycle taskbar action
    # users perform manually. Retry once before any OCR or mouse operation.
    if max(ImageStat.Stat(image.convert("RGB")).stddev) < 1.0:
        if audit.recover_blank_surface(window):
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
    expected = ocr_match_key(query)
    return any(
        expected in ocr_match_key(line.text)
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


def scroll_list(window: dict[str, object], delta: int = LIST_SCROLL_DELTA) -> None:
    open_group.scroll_screen_point(point_in_window(window, LIST_SCROLL_POINT), delta)
    time.sleep(LIST_SCROLL_SETTLE_SECONDS)


def scroll_list_to_top(window: dict[str, object]) -> None:
    """Return the Contacts list to its first visible entry before a saved-group scan."""
    for _ in range(LIST_TOP_SCROLL_STEPS):
        open_group.scroll_screen_point(point_in_window(window, LIST_SCROLL_POINT), LIST_TOP_SCROLL_DELTA)
        time.sleep(0.1)
    time.sleep(LIST_SCROLL_SETTLE_SECONDS)


def scroll_contacts_to_top(window: dict[str, object]) -> None:
    """Drag the visible Contacts-list scrollbar thumb to its top position."""
    start = point_in_window(
        window, (CONTACTS_SCROLLBAR_X_RATIO, CONTACTS_SCROLLBAR_THUMB_Y_RATIO)
    )
    end = point_in_window(
        window, (CONTACTS_SCROLLBAR_X_RATIO, CONTACTS_SCROLLBAR_TOP_Y_RATIO)
    )
    open_group.set_cursor_pos(start)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    open_group.set_cursor_pos(end)
    time.sleep(0.12)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(LIST_SCROLL_SETTLE_SECONDS)


def page_changed(previous: Image.Image, current: Image.Image) -> bool:
    """Treat a nearly identical post-scroll list image as the end of the list."""
    if previous.size != current.size:
        return True
    difference = ImageChops.difference(previous.convert("L"), current.convert("L"))
    return ImageStat.Stat(difference).mean[0] > UNCHANGED_PAGE_MEAN_DIFFERENCE


def scrollbar_reached_bottom(image: Image.Image) -> bool:
    """Detect a list scrollbar thumb resting against the bottom of the left pane."""
    width, height = image.size
    start_x = round(width * 0.88)
    end_x = round(width * 0.96)
    required_bottom = round(height * SCROLLBAR_BOTTOM_RATIO)
    for x in range(start_x, end_x):
        run_start = None
        for y in range(height):
            red, green, blue = image.getpixel((x, y))[:3]
            neutral_thumb = max(red, green, blue) - min(red, green, blue) <= 4 and 80 <= red <= 210
            if neutral_thumb:
                run_start = y if run_start is None else run_start
            elif run_start is not None:
                if y - run_start >= SCROLLBAR_MIN_NEUTRAL_RUN and y - 1 >= required_bottom:
                    return True
                run_start = None
        if (
            run_start is not None
            and height - run_start >= SCROLLBAR_MIN_NEUTRAL_RUN
            and height - 1 >= required_bottom
        ):
            return True
    return False


def save_visible_pages(
    window: dict[str, object],
    directory: Path,
    maximum_pages: int | None,
    *,
    live: bool,
) -> tuple[list[str], str]:
    outputs = []
    previous_page = None
    page_limit = maximum_pages or MAX_PAGES
    for index in range(page_limit):
        output = directory / f"page-{index + 1:02d}.png"
        frame = directory / f".page-{index + 1:02d}-frame.png"
        capture = capture_live_window if live or index else capture_full_window
        full_image, _ = capture(window, frame)
        list_image, _ = crop_left_pane(full_image)
        if previous_page is not None and not page_changed(previous_page, list_image):
            frame.unlink(missing_ok=True)
            return outputs, "page_unchanged"
        list_image.save(output)
        frame.unlink(missing_ok=True)
        outputs.append(str(output.resolve()))
        if scrollbar_reached_bottom(list_image):
            return outputs, "scrollbar_bottom"
        previous_page = list_image.copy()
        if index + 1 < page_limit:
            scroll_list(window)
    return outputs, "maximum_pages"


def _contact_rows(lines: list[open_group.OcrLine], image: Image.Image) -> list[open_group.OcrLine]:
    """Return contact rows in visual order using their square avatar regions."""
    contact_heading = next(
        (line for line in left_pane_lines(lines, image) if section_kind(line.text) == "contacts"),
        None,
    )
    min_top = max(CONTACT_ROW_MIN_TOP, contact_heading.bottom if contact_heading else 0)
    max_top = round(image.height * CONTACT_ROW_MAX_TOP_RATIO)
    left = round(image.width * 0.14)
    right = round(image.width * 0.19)
    variation = [
        sum(ImageStat.Stat(image.crop((left, y, right, y + 1)).convert("RGB")).stddev)
        for y in range(image.height)
    ]
    rows: list[open_group.OcrLine] = []
    run_start = None
    for y, value in enumerate([*variation, 0.0]):
        if value > 18 and run_start is None:
            run_start = y
        elif value <= 18 and run_start is not None:
            height = y - run_start
            if 30 <= height <= 60 and run_start >= min_top and run_start <= max_top:
                rows.append(
                    open_group.OcrLine(
                        f"avatar-{run_start}", left, run_start, right, y - 1
                    )
                )
            run_start = None
    return rows


def contact_identifier(lines: list[open_group.OcrLine], image: Image.Image) -> str | None:
    """Read the visible profile identifier used to reject unchanged details."""
    for line in lines:
        if line.left < round(image.width * 0.25):
            continue
        match = re.search(r"(?:Weixin\s*ID|微信号)\s*[:：]\s*(\S+)", line.text, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip(":：;,，。")
    return None


def recent_conversation_rows(image: Image.Image) -> list[open_group.OcrLine]:
    """Locate visible conversation rows by their avatars in visual order."""
    left, right = round(image.width * 0.105), round(image.width * 0.16)
    variation = [
        sum(ImageStat.Stat(image.crop((left, y, right, y + 1)).convert("RGB")).stddev)
        for y in range(image.height)
    ]
    rows = []
    run_start = None
    for y, value in enumerate([*variation, 0.0]):
        if value > 18 and run_start is None:
            run_start = y
        elif value <= 18 and run_start is not None:
            height = y - run_start
            if 30 <= height <= 65 and run_start >= 120:
                rows.append(open_group.OcrLine(f"chat-{run_start}", left, run_start, right, y - 1))
            run_start = None
    return rows


def direct_chat_avatar(
    lines: list[open_group.OcrLine], image: Image.Image
) -> tuple[int, int] | None:
    """Return the sole peer avatar in a direct-chat settings page."""
    normalized = [open_group.normalize_text(line.text) for line in lines]
    if not any("searchchathistory" in text or "搜索聊天记录" in text for text in normalized):
        return None
    add = next((line for line in lines if open_group.normalize_text(line.text) in {"add", "添加"}), None)
    blocks = chat_settings_member_tiles(image)
    if add is not None:
        blocks = [
            point
            for point in blocks
            if not add.left - 20 <= point[0] <= add.right + 20
        ]
        return blocks[0] if len(blocks) == 1 else None
    # Tesseract often misses the outlined English "Add" label. A direct chat
    # still has exactly two visual tiles: peer avatar followed by Add. Group
    # settings expose three or more member/add tiles and must be skipped.
    return blocks[0] if len(blocks) == 2 else None


def chat_settings_member_tiles(image: Image.Image) -> list[tuple[int, int]]:
    """Locate the first row of member and Add tiles in chat settings."""
    y1, y2 = 135, min(image.height, 198)
    variation = [
        sum(ImageStat.Stat(image.crop((x, y1, x + 1, y2)).convert("RGB")).stddev)
        for x in range(image.width)
    ]
    blocks = []
    run_start = None
    for x, value in enumerate([*variation, 0.0]):
        in_region = round(image.width * 0.66) <= x <= round(image.width * 0.96)
        if in_region and value > 18 and run_start is None:
            run_start = x
        elif (not in_region or value <= 18) and run_start is not None:
            width = x - run_start
            if 20 <= width <= 75:
                center = (run_start + x - 1) // 2
                blocks.append((center, (y1 + y2) // 2))
            run_start = None
    return blocks


def wechat_auxiliary_windows() -> set[int]:
    """Return visible Chromium windows used by public accounts and mini programs."""
    handles: set[int] = set()

    def collect(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = audit.psutil.Process(pid).name().casefold()
        except (OSError, audit.psutil.Error, win32gui.error):
            return True
        if name == "wechatappex.exe":
            handles.add(hwnd)
        return True

    win32gui.EnumWindows(collect, None)
    return handles


def close_wechat_auxiliary_windows(handles: set[int]) -> None:
    """Close only the public-account/mini-program windows selected by the caller."""
    if not handles:
        return
    pids: set[int] = set()
    for hwnd in handles:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pids.add(pid)
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except win32gui.error:
            continue
    time.sleep(0.35)
    remaining = wechat_auxiliary_windows() & handles
    for hwnd in remaining:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in pids:
                audit.psutil.Process(pid).terminate()
        except (OSError, audit.psutil.Error, win32gui.error):
            continue


def wait_for_new_wechat_auxiliary_windows(existing: set[int]) -> set[int]:
    """Wait for an auxiliary view opened by the current click."""
    deadline = time.monotonic() + AUXILIARY_WINDOW_WAIT_SECONDS
    while time.monotonic() < deadline:
        current = wechat_auxiliary_windows()
        opened = current - existing
        foreground = win32gui.GetForegroundWindow()
        if foreground in current:
            opened.add(foreground)
        if opened:
            return opened
        time.sleep(AUXILIARY_WINDOW_POLL_SECONDS)
    return set()


def save_recent_contact_pages(
    window: dict[str, object], directory: Path, maximum: int | None, tesseract: Path
) -> tuple[list[str], str]:
    """Open recent direct chats and save unique visible contact profiles."""
    outputs: list[str] = []
    seen_identifiers: set[str] = set()
    limit = maximum or MAX_PAGES
    preexisting_auxiliary_windows = wechat_auxiliary_windows()

    def return_to_chat_list(current: dict[str, object], *, profile_open: bool) -> None:
        close_wechat_auxiliary_windows(
            wechat_auxiliary_windows() - preexisting_auxiliary_windows
        )
        activation = audit.activate_window(current)
        if activation["activated"]:
            current = activation["window"]
        if profile_open:
            open_group.click_screen_point(
                (int(current["left"]) + int(current["width"]) - 25, int(current["top"]) + 600)
            )
            time.sleep(0.2)
        open_group.click_screen_point(sidebar_point(current, CHAT_NAV))
        time.sleep(NAVIGATION_WAIT_SECONDS)

    scroll_list_to_top(window)
    for _page in range(MAX_PAGES):
        frame = directory / ".recent-list.png"
        window = audit.select_weixin_window(int(window["pid"])) or window
        list_image, _ = capture_live_window(window, frame)
        rows = recent_conversation_rows(list_image)
        if not rows:
            frame.replace(directory / "recent-not-detected.png")
            return outputs, "recent_chats_not_detected"
        for row in rows:
            window = audit.select_weixin_window(int(window["pid"])) or window
            open_group.click_screen_point(
                screen_point_from_capture(
                    window, list_image, (row.left + row.right) // 2, (row.top + row.bottom) // 2
                )
            )
            time.sleep(CHAT_OPEN_WAIT_SECONDS)
            window = audit.select_weixin_window(int(window["pid"])) or window
            open_group.click_screen_point(
                (int(window["left"]) + int(window["width"]) - 62, int(window["top"]) + 84)
            )
            time.sleep(CONTACT_DETAIL_WAIT_SECONDS)
            settings_path = directory / ".recent-settings.png"
            settings_image, _ = capture_live_window(window, settings_path)
            settings_lines = open_group.run_ocr(tesseract, settings_path, psm=11, language="chi_sim+eng")
            avatar = direct_chat_avatar(settings_lines, settings_image)
            settings_path.unlink(missing_ok=True)
            if avatar is None:
                return_to_chat_list(window, profile_open=False)
                continue
            auxiliary_windows_before = wechat_auxiliary_windows()
            open_group.click_screen_point(screen_point_from_capture(window, settings_image, *avatar))
            time.sleep(CONTACT_DETAIL_WAIT_SECONDS)
            window = audit.select_weixin_window(int(window["pid"])) or window
            candidate = directory / ".recent-candidate.png"
            candidate_image, _ = capture_live_window(window, candidate)
            candidate_lines = open_group.run_ocr(tesseract, candidate, psm=11, language="chi_sim+eng")
            identifier = contact_identifier(candidate_lines, candidate_image)
            if identifier is not None:
                if identifier not in seen_identifiers:
                    seen_identifiers.add(identifier)
                    output = directory / f"recent-contact-{len(outputs) + 1:03d}.png"
                    candidate.replace(output)
                    outputs.append(str(output.resolve()))
                else:
                    candidate.unlink(missing_ok=True)
                return_to_chat_list(window, profile_open=True)
            else:
                candidate.unlink(missing_ok=True)
                auxiliary_windows = wait_for_new_wechat_auxiliary_windows(
                    auxiliary_windows_before
                )
                close_wechat_auxiliary_windows(auxiliary_windows)
                return_to_chat_list(window, profile_open=False)
            if len(outputs) >= limit:
                frame.unlink(missing_ok=True)
                return outputs, "maximum_contacts"
        before, _ = crop_left_pane(list_image)
        frame.unlink(missing_ok=True)
        scroll_list(window)
        after_path = directory / ".recent-after.png"
        after_full, _ = capture_live_window(window, after_path)
        after_path.unlink(missing_ok=True)
        after, _ = crop_left_pane(after_full)
        if not page_changed(before, after):
            return outputs, "scrollbar_bottom"
    return outputs, "maximum_pages"


def expand_contacts(
    window: dict[str, object], directory: Path, tesseract: Path
) -> bool:
    """Open the Contacts disclosure section before scanning individual rows."""
    frame = directory / ".contacts-expand.png"
    full_image, _ = capture_live_window(window, frame)
    lines = open_group.run_ocr(tesseract, frame, psm=11, language="chi_sim+eng")
    heading = next((line for line in lines if section_kind(line.text) == "contacts"), None)
    if heading is None:
        frame.unlink(missing_ok=True)
        return False
    # The section may already be expanded from a previous run. In that case
    # clicking the heading would collapse it and hide the rows we need.
    if _contact_rows(lines, full_image):
        frame.unlink(missing_ok=True)
        return True
    point = screen_point_from_capture(
        window,
        full_image,
        (heading.left + heading.right) // 2,
        (heading.top + heading.bottom) // 2,
    )
    open_group.click_screen_point(point)
    time.sleep(NAVIGATION_WAIT_SECONDS)
    frame.unlink(missing_ok=True)
    verify = directory / ".contacts-expand-verify.png"
    verify_image, _ = capture_live_window(window, verify)
    verify_lines = open_group.run_ocr(tesseract, verify, psm=11, language="chi_sim+eng")
    rows = _contact_rows(verify_lines, verify_image)
    verify.unlink(missing_ok=True)
    return heading is not None


def save_contact_detail_pages(
    window: dict[str, object],
    directory: Path,
    maximum: int | None,
    tesseract: Path,
) -> tuple[list[str], str]:
    """Open contacts using a fresh screenshot for every click.

    Returning through the Contacts navigation can reset or partially restore
    the list. Never reuse stale row coordinates: identify each row from its
    current avatar crop, skip rows already attempted, and only scroll after
    every currently visible row has been handled.
    """
    outputs: list[str] = []
    limit = maximum or MAX_PAGES
    seen_identifiers: set[str] = set()
    probe_index = 0

    def probe(name: str, image: Image.Image, **fields: object) -> None:
        nonlocal probe_index
        if not os.environ.get("WECHAT_DEBUG_PROBES"):
            return
        probe_index += 1
        image.save(directory / f"contact-probe-{probe_index:04d}-{name}.png")
        with (directory / "contact-probes.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"step": probe_index, "name": name, **fields}, ensure_ascii=False) + "\n")

    for _page in range(MAX_PAGES):
        if consume_skip_request():
            return outputs, "skipped_by_user"
        if len(outputs) >= limit:
            return outputs, "maximum_contacts"
        frame = directory / ".contacts-list.png"
        full_image, _ = capture_live_window(window, frame)
        frame.unlink(missing_ok=True)
        click_x = round(full_image.width * CONTACT_FIXED_CLICK_POINT[0])
        click_y = round(full_image.height * CONTACT_FIXED_CLICK_POINT[1])
        probe("fixed-bottom-click", full_image, click_x=click_x, click_y=click_y)
        open_group.click_screen_point(
            screen_point_from_capture(window, full_image, click_x, click_y)
        )
        time.sleep(CONTACT_DETAIL_WAIT_SECONDS)
        candidate = directory / ".contact-candidate.png"
        candidate_image, _ = capture_live_window(window, candidate)
        candidate_lines = open_group.run_ocr(tesseract, candidate, psm=11, language="chi_sim+eng")
        identifier = contact_identifier(candidate_lines, candidate_image)
        probe("profile-after-click", candidate_image, identifier=identifier)
        if identifier is not None and identifier not in seen_identifiers:
            seen_identifiers.add(identifier)
            output = directory / f"contact-{len(outputs) + 1:03d}.png"
            candidate.replace(output)
            outputs.append(str(output.resolve()))
        else:
            candidate.unlink(missing_ok=True)
        if len(outputs) >= limit:
            return outputs, "maximum_contacts"
        # The left list remains visible behind the profile. Clicking the same
        # fixed bottom point returns to the list without navigating to its top.
        open_group.click_screen_point(
            screen_point_from_capture(window, candidate_image, click_x, click_y)
        )
        time.sleep(0.18)
        before, _ = crop_left_pane(full_image)
        scroll_list(window, -120)
        after_path = directory / ".contacts-after.png"
        after_full, _ = capture_live_window(window, after_path)
        after_path.unlink(missing_ok=True)
        after, _ = crop_left_pane(after_full)
        changed = page_changed(before, after)
        probe("scroll-after-profile", after_full, changed=changed)
        if not changed:
            return outputs, "scrollbar_bottom"
    return outputs, "maximum_pages"


def save_saved_group_pages(
    window: dict[str, object],
    directory: Path,
    maximum_pages: int,
    tesseract: Path,
) -> tuple[list[str], str]:
    """Save only the visible Saved Groups section, never the general contacts below it."""
    outputs = []
    section_started = False
    section_language = "eng"
    for index in range(maximum_pages):
        frame = directory / f".saved-groups-{index + 1:02d}-frame.png"
        full_image, _ = capture_live_window(window, frame)
        list_image, _ = crop_left_pane(full_image)
        frame.unlink(missing_ok=True)
        ocr_path = directory / f".saved-groups-{index + 1:02d}-ocr.png"
        list_image.save(ocr_path)
        lines = open_group.run_ocr(tesseract, ocr_path, psm=11, language=section_language)
        ocr_path.unlink(missing_ok=True)
        start_top, end_top, continues = saved_group_section_bounds(lines, section_started)
        if not section_started and start_top is None:
            # Determine the UI language once, then reuse it for subsequent pages.
            section_language = "chi_sim+eng"
            ocr_path.write_bytes(b"")
            list_image.save(ocr_path)
            lines = open_group.run_ocr(tesseract, ocr_path, psm=11, language=section_language)
            ocr_path.unlink(missing_ok=True)
            start_top, end_top, continues = saved_group_section_bounds(lines, section_started)
        if start_top is None:
            return outputs, "saved_groups_not_visible"
        section_started = True
        bottom = list_image.height if end_top is None else end_top
        if bottom > start_top:
            output = directory / f"page-{len(outputs) + 1:02d}.png"
            list_image.crop((0, max(0, start_top), list_image.width, bottom)).save(output)
            outputs.append(str(output.resolve()))
        if not continues:
            return outputs, "left_saved_groups"
        if scrollbar_reached_bottom(list_image):
            return outputs, "scrollbar_bottom"
        scroll_list(window)
    return outputs, "maximum_pages"


def select_pid(explicit_pid: int | None, config: Path) -> tuple[int | None, str]:
    if explicit_pid is not None:
        return explicit_pid, "command_line"
    pid, status = quick_capture.configured_pid(config)
    windows = audit.visible_weixin_windows()
    if pid is not None and any(int(window["pid"]) == pid for window in windows):
        return pid, str(config.resolve())
    if len(windows) == 1:
        return int(windows[0]["pid"]), f"auto_single_window:{status}"
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
    if args.recent and (args.f or args.m != "chat" or args.q is not None):
        print("-r/--recent 是独立的最近单聊联系人模式，不能与 -f、-m saved 或 -q 同时使用。")
        return 2
    if args.s is not None and not 1 <= args.s <= MAX_PAGES:
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

    # `-m chat -f` means contacts: friend detail capture starts from the
    # Contacts sidebar, while plain chat mode starts from conversations.
    nav_point = sidebar_point(
        window,
        CHAT_NAV
        if args.recent
        else (CONTACTS_NAV if args.f else (CHAT_NAV if args.m == "chat" else CONTACTS_NAV)),
    )
    open_group.click_screen_point(nav_point)
    time.sleep(NAVIGATION_WAIT_SECONDS)

    saved_group_search = args.m == "saved" and not args.f and args.q is not None
    if args.q is not None and not saved_group_search:
        open_group.focus_global_search(window)
        time.sleep(SEARCH_FOCUS_WAIT_SECONDS)
        open_group.select_all()
        open_group.send_unicode_text(args.q)
        time.sleep(
            SEARCH_RESULT_WAIT_NO_OCR_SECONDS
            if args.n
            else SEARCH_RESULT_WAIT_OCR_SECONDS
        )

    args.o.mkdir(parents=True, exist_ok=True)
    if args.recent:
        tesseract = open_group.resolve_tesseract(args.tesseract)
        if tesseract is None:
            print("未找到 Tesseract，最近聊天联系人模式需要 OCR。")
            return 2
        for stale in args.o.glob("recent-contact-*.png"):
            stale.unlink(missing_ok=True)
        for temporary in (
            ".recent-list.png",
            ".recent-settings.png",
            ".recent-candidate.png",
            ".recent-after.png",
            "recent-not-detected.png",
        ):
            (args.o / temporary).unlink(missing_ok=True)
        outputs, stop_reason = save_recent_contact_pages(window, args.o, args.s, tesseract)
        emit_result(
            args.o,
            {
                "ok": bool(outputs),
                "ocr": True,
                "mode": "recent",
                "target": "friend",
                "query": None,
                "pages": outputs,
                "stop_reason": stop_reason,
                "capture_scope": "recent_contact_details",
                "target_pid": pid,
                "pid_source": pid_source,
            },
        )
        return 0 if outputs else 3
    if args.f and args.q is None:
        tesseract = open_group.resolve_tesseract(args.tesseract)
        if tesseract is None:
            print("未找到 Tesseract，联系人详情定位需要 OCR。")
            return 2
        if not expand_contacts(window, args.o, tesseract):
            print("未找到或无法展开 Contacts/联系人分区。")
            return 3
        scroll_list_to_top(window)
        for stale in args.o.glob("contact-*.png"):
            stale.unlink(missing_ok=True)
        outputs, stop_reason = save_contact_detail_pages(window, args.o, args.s, tesseract)
        emit_result(
            args.o,
            {
                "ok": bool(outputs),
                "ocr": True,
                "mode": args.m,
                "target": "friend",
                "query": args.q,
                "pages": outputs,
                "stop_reason": stop_reason,
                "capture_scope": "contact_details",
                "target_pid": pid,
                "pid_source": pid_source,
            },
        )
        return 0 if outputs else 3
    if args.n or args.q is None:
        if args.m == "saved" and not args.f:
            tesseract = open_group.resolve_tesseract(args.tesseract)
            if tesseract is None:
                print("未找到 Tesseract，无法确认 Saved Groups 分区边界。")
                return 2
            if not expand_saved_groups(window, tesseract, args.o):
                print("未找到 Saved Groups/保存的群聊分区。")
                return 2
            outputs, stop_reason = save_saved_group_pages(
                window, args.o, args.s or MAX_PAGES, tesseract
            )
        else:
            outputs, stop_reason = save_visible_pages(
                window,
                args.o,
                args.s,
                live=args.q is not None or args.s is None or args.s > 1,
            )
        emit_result(
            args.o,
            {
                "ok": True,
                "ocr": False,
                "mode": args.m,
                "target": target_label,
                "query": args.q,
                "pages": outputs,
                "stop_reason": stop_reason,
                "capture_scope": "saved_groups"
                if args.m == "saved" and not args.f
                else "list",
                "target_pid": pid,
                "pid_source": pid_source,
            },
        )
        return 0

    tesseract = open_group.resolve_tesseract(args.tesseract)
    if tesseract is None:
        print("未找到 Tesseract。使用 -n 可跳过 OCR 直接截图。")
        return 2
    if saved_group_search:
        if not expand_saved_groups(window, tesseract, args.o):
            print("未找到 Saved Groups/保存的群聊分区。")
            return 2
        try:
            match, crop_offset, ocr_seconds, screenshots = find_saved_group(
                window, args.q, args.o, args.s or MAX_PAGES, tesseract
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
