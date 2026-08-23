"""Visible-UI group member backup for an already opened Weixin group."""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import time
from pathlib import Path

import win32api
import win32con
from PIL import Image, ImageChops, ImageStat

import open_group
import wechat_group_roster_audit as audit
import wx


DEFAULT_TERMS = ("1",) + tuple(string.ascii_lowercase)
GROUP_SEARCH_POINT = (0.80, 0.15)
GROUP_RESULT_SCROLL_POINT = (0.79, 0.72)
PROFILE_DISMISS_POINT = (0.50, 0.72)
RESULT_PANEL = (0.60, 0.18, 0.98, 0.96)
SETTINGS_WAIT_SECONDS = 0.65
SEARCH_WAIT_SECONDS = 0.65
PROFILE_WAIT_SECONDS = 0.55
PROFILE_DISMISS_WAIT_SECONDS = 0.65
SCROLL_WAIT_SECONDS = 0.15
MEMBER_RESET_WAIT_SECONDS = 0.35
MEMBER_RESET_MAX_STEPS = 12
MEMBER_RESET_STABLE_FRAMES = 2
MEMBER_RESET_MIN_WHEELS = 3
MAX_PAGES = 1000
GROUP_RESULTS_CROP = (0.09, 0.10, 0.55, 0.93)
GROUP_RESULTS_SCALE = 2
VISIBLE_ID_PATTERN = re.compile(
    r"(?:Weixin\s*ID|微信号)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,})",
    re.IGNORECASE,
)


def debug_step(directory: Path, name: str, image: Image.Image | None = None, **fields: object) -> None:
    """Persist an opt-in group-member step probe for coordinate debugging."""
    if not os.environ.get("WECHAT_DEBUG_GROUP_STEPS"):
        return
    directory.mkdir(parents=True, exist_ok=True)
    if image is not None:
        image.save(directory / f".debug-{name}.png")
    with (directory / "debug-steps.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"step": name, **fields}, ensure_ascii=False) + "\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Capture visible members of the opened group")
    result.add_argument("-g", "--group", help="group name or unique visible fragment")
    result.add_argument("-M", "--member-mode", choices=("auto", "list", "detail"), default="auto")
    result.add_argument("-k", "--terms", default="1,a-z", help="member search terms")
    result.add_argument("-s", type=int, default=MAX_PAGES, metavar="N", help="maximum pages per term")
    result.add_argument("-o", type=Path, default=Path("artifacts/group-members"))
    result.add_argument("-p", type=int, help="explicit Weixin PID")
    result.add_argument("--tesseract", type=Path)
    result.add_argument("--term-timeout", type=float, help="maximum seconds for one member search term")
    return result


def wait_seconds(name: str, default: float) -> float:
    return max(0.0, float(os.environ.get(name, str(default))))


def parse_terms(value: str) -> tuple[str, ...]:
    """Expand the short a-z token and retain unique terms in input order."""
    result: list[str] = []
    for raw in value.replace("，", ",").split(","):
        term = raw.strip()
        expanded = string.ascii_lowercase if term.casefold() == "a-z" else (term,)
        for item in expanded:
            if item and item not in result:
                result.append(item)
    return tuple(result)


def result_member_rows(image: Image.Image) -> list[open_group.OcrLine]:
    """Locate member result rows from avatar texture, independent of nickname OCR."""
    left, right = round(image.width * 0.72), round(image.width * 0.78)
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
            if 30 <= height <= 68 and run_start >= round(image.height * 0.18):
                rows.append(
                    open_group.OcrLine(
                        f"member-{run_start}", left, run_start, right, y - 1
                    )
                )
            run_start = None
    return rows


def visible_identifiers(
    lines: list[open_group.OcrLine], image: Image.Image
) -> list[str]:
    """Read IDs already exposed directly in the group member result list."""
    identifiers: list[str] = []
    for line in lines:
        if line.top < round(image.height * RESULT_PANEL[1]):
            continue
        match = VISIBLE_ID_PATTERN.search(line.text)
        identifier = match.group(1) if match else wx.contact_identifier([line], image)
        if identifier is not None and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def visible_identifiers_from_panel(
    tesseract: Path, image: Image.Image, probe: Path
) -> list[str]:
    """OCR the member result panel enlarged; grey IDs are missed at full size."""
    panel = crop_result_panel(image)
    enlarged = panel.resize((panel.width * 2, panel.height * 2), Image.Resampling.LANCZOS)
    enlarged_path = probe.with_name(".member-panel-ocr.png")
    enlarged.save(enlarged_path)
    try:
        lines = open_group.run_ocr(tesseract, enlarged_path, psm=6, language="chi_sim+eng")
        identifiers: list[str] = []
        for line in lines:
            match = VISIBLE_ID_PATTERN.search(line.text)
            if match and match.group(1) not in identifiers:
                identifiers.append(match.group(1))
        return identifiers
    finally:
        enlarged_path.unlink(missing_ok=True)


def list_ids_enabled() -> bool:
    """Whether visible member IDs should be captured without opening cards."""
    return os.environ.get("WECHAT_LIST_IF_ID", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def crop_result_panel(image: Image.Image) -> Image.Image:
    return image.crop(
        (
            round(image.width * RESULT_PANEL[0]),
            round(image.height * RESULT_PANEL[1]),
            round(image.width * RESULT_PANEL[2]),
            round(image.height * RESULT_PANEL[3]),
        )
    )


def result_page_changed(previous: Image.Image, current: Image.Image) -> bool:
    if previous.size != current.size:
        return True
    difference = ImageStat.Stat(ImageChops.difference(previous, current).convert("L")).mean[0]
    return difference >= wx.UNCHANGED_PAGE_MEAN_DIFFERENCE


def scroll_member_results(
    window: dict[str, object], *, upward: bool = False, delta: int = 12000
) -> None:
    open_group.set_cursor_pos(wx.point_in_window(window, GROUP_RESULT_SCROLL_POINT))
    win32api.mouse_event(
        win32con.MOUSEEVENTF_WHEEL,
        0,
        0,
        abs(delta) if upward else -abs(delta),
        0,
    )
    time.sleep(wait_seconds("WECHAT_SCROLL_DELAY", SCROLL_WAIT_SECONDS))


class MemberTermTimeout(RuntimeError):
    def __init__(self, term: str, stage: str) -> None:
        super().__init__(f"member_term_timeout:{term}:{stage}")
        self.term = term
        self.stage = stage


def ensure_term_time(deadline: float, term: str, stage: str) -> None:
    if wx.stop_requested():
        raise RuntimeError("stop_requested")
    if time.monotonic() >= deadline:
        raise MemberTermTimeout(term, stage)


def reset_member_results_to_top(
    window: dict[str, object], directory: Path, term: str, deadline: float
) -> bool:
    """Overscroll upward and require stable frames before the next search."""
    label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", term).strip("-") or "term"
    probe_dir = directory / "reset"
    probe_dir.mkdir(parents=True, exist_ok=True)
    previous: Image.Image | None = None
    stable = 0
    keep_probes = bool(os.environ.get("WECHAT_DEBUG_GROUP_STEPS"))
    max_steps = max(1, int(wait_seconds("WECHAT_MEMBER_RESET_MAX_STEPS", MEMBER_RESET_MAX_STEPS)))
    stable_frames = max(1, int(wait_seconds("WECHAT_MEMBER_RESET_STABLE_FRAMES", MEMBER_RESET_STABLE_FRAMES)))
    min_wheels = max(1, int(wait_seconds("WECHAT_MEMBER_RESET_MIN_WHEELS", MEMBER_RESET_MIN_WHEELS)))
    for step in range(1, max_steps + 1):
        ensure_term_time(deadline, term, f"reset-before-{step}")
        before_path = probe_dir / f"{label}-before-{step:02d}.png"
        before, _ = wx.capture_live_window(window, before_path)
        if not keep_probes:
            before_path.unlink(missing_ok=True)
        if previous is not None and not result_page_changed(crop_result_panel(previous), crop_result_panel(before)):
            stable += 1
        else:
            stable = 0
        debug_step(directory, "term-reset-before", before, term=term, step=step, stable=stable)
        previous = before
        if stable >= stable_frames and step >= min_wheels:
            debug_step(directory, "term-reset-top", term=term, ok=True, step=step, stable=stable)
            return True
        ensure_term_time(deadline, term, f"reset-wheel-{step}")
        scroll_member_results(window, upward=True, delta=12000)
        time.sleep(wait_seconds("WECHAT_MEMBER_RESET_DELAY", MEMBER_RESET_WAIT_SECONDS))
        after_path = probe_dir / f"{label}-after-{step:02d}.png"
        after, _ = wx.capture_live_window(window, after_path)
        if not keep_probes:
            after_path.unlink(missing_ok=True)
        changed = result_page_changed(crop_result_panel(before), crop_result_panel(after))
        debug_step(directory, "term-reset-after", after, term=term, step=step, changed=changed)
        stable = stable + 1 if not changed else 0
        previous = after
        if stable >= stable_frames and step >= min_wheels:
            debug_step(directory, "term-reset-top", term=term, ok=True, step=step, stable=stable)
            return True
    debug_step(directory, "term-reset-top", term=term, ok=False, stable=stable, min_wheels=min_wheels)
    return False


def replace_search_text(window: dict[str, object], value: str) -> None:
    # The Qt/Weixin search control often has no native focus handle and its
    # background is not reliably green. The click itself is the reliable
    # focus operation; capture the probe for audit, then type regardless of
    # the heuristic focus check.
    point = wx.point_in_window(window, GROUP_SEARCH_POINT)
    open_group.click_screen_point(point)
    time.sleep(0.15)
    debug_step(
        Path("artifacts/group-member-debug"),
        "search-focused",
        point=point,
        value=value,
        search_visible=member_search_visible(window),
        native_focus=weixin_has_keyboard_focus(int(window["hwnd"])),
    )
    open_group.select_all()
    open_group.send_unicode_text(value)
    time.sleep(SEARCH_WAIT_SECONDS)


def member_search_visible(window: dict[str, object]) -> bool:
    """Confirm the right-panel member search box is visible before typing."""
    probe = Path("artifacts") / ".member-search-focus.png"
    try:
        image, _ = wx.capture_live_window(window, probe)
        left = round(image.width * 0.70)
        right = round(image.width * 0.97)
        top = round(image.height * 0.14)
        bottom = round(image.height * 0.27)
        green = 0
        for y in range(top, bottom, 2):
            for x in range(left, right, 2):
                red, blue_green, blue = image.getpixel((x, y))[:3]
                if green := (red < 40 and 130 <= blue_green <= 230 and 60 <= blue <= 180):
                    return True
        return False
    finally:
        probe.unlink(missing_ok=True)


def close_member_profile(
    window: dict[str, object],
    directory: Path,
    ordinal: int,
) -> bool:
    """Dismiss one member card and prove the member panel is usable again."""
    before = directory / f".debug-before-dismiss-{ordinal:04d}.png"
    image, _ = wx.capture_live_window(window, before)
    debug_step(
        directory,
        "before-dismiss",
        image,
        ordinal=ordinal,
        point=wx.point_in_window(window, GROUP_SEARCH_POINT),
    )
    # Focusing the member search box dismisses the profile card without using
    # Escape, which can close the group settings surface in some Weixin builds.
    open_group.click_screen_point(wx.point_in_window(window, GROUP_SEARCH_POINT))
    time.sleep(wait_seconds("WECHAT_PROFILE_DISMISS_DELAY", PROFILE_DISMISS_WAIT_SECONDS))
    after = directory / f".debug-after-dismiss-{ordinal:04d}.png"
    after_image, _ = wx.capture_live_window(window, after)
    rows = result_member_rows(after_image)
    visible = member_search_visible(window)
    debug_step(
        directory,
        "after-dismiss",
        after_image,
        ordinal=ordinal,
        rows=len(rows),
        search_visible=visible,
    )
    if visible and rows:
        return True

    # The settings pane may have collapsed while the card was open. Re-open it
    # once, then refocus search and verify before allowing the next click.
    point = (
        int(window["left"]) + int(window["width"]) - 62,
        int(window["top"]) + 84,
    )
    open_group.click_screen_point(point)
    time.sleep(wait_seconds("WECHAT_SETTINGS_DELAY", SETTINGS_WAIT_SECONDS))
    open_group.click_screen_point(wx.point_in_window(window, GROUP_SEARCH_POINT))
    time.sleep(wait_seconds("WECHAT_PROFILE_DISMISS_DELAY", PROFILE_DISMISS_WAIT_SECONDS))
    recovered = directory / f".debug-after-recover-{ordinal:04d}.png"
    recovered_image, _ = wx.capture_live_window(window, recovered)
    recovered_rows = result_member_rows(recovered_image)
    recovered_visible = member_search_visible(window)
    debug_step(
        directory,
        "after-recover",
        recovered_image,
        ordinal=ordinal,
        rows=len(recovered_rows),
        search_visible=recovered_visible,
    )
    return recovered_visible and bool(recovered_rows)


def weixin_has_keyboard_focus(hwnd: int) -> bool:
    """Qt may expose keyboard focus while omitting its native caret handle."""
    active, focus, _caret = open_group.gui_thread_handles()
    return active == hwnd and focus == hwnd


def open_named_group(
    window: dict[str, object], query: str, directory: Path, tesseract: Path
) -> dict[str, object]:
    """Open one group from global search without accepting non-group results."""
    open_group.click_screen_point(wx.sidebar_point(window, wx.CHAT_NAV))
    time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    open_group.focus_global_search(window)
    time.sleep(wx.SEARCH_FOCUS_WAIT_SECONDS)
    hwnd = int(window["hwnd"])
    if not weixin_has_keyboard_focus(hwnd):
        raise RuntimeError("global_search_not_focusable")
    open_group.select_all()
    open_group.send_unicode_text(query)
    time.sleep(wx.SEARCH_RESULT_WAIT_OCR_SECONDS)
    results_path = directory / ".group-search-results.png"
    image, _ = wx.capture_live_window(window, results_path)
    crop_box = (
        round(image.width * GROUP_RESULTS_CROP[0]),
        round(image.height * GROUP_RESULTS_CROP[1]),
        round(image.width * GROUP_RESULTS_CROP[2]),
        round(image.height * GROUP_RESULTS_CROP[3]),
    )
    enlarged = image.crop(crop_box).resize(
        (
            (crop_box[2] - crop_box[0]) * GROUP_RESULTS_SCALE,
            (crop_box[3] - crop_box[1]) * GROUP_RESULTS_SCALE,
        )
    )
    crop_path = directory / ".group-search-results-enlarged.png"
    enlarged.save(crop_path)
    lines = open_group.run_ocr(tesseract, crop_path, psm=6, language="chi_sim+eng")
    match = wx.find_exact_result(
        lines, query, {"most_used", "groups", "chat_history"}
    )
    top_limit = round(enlarged.height * 0.22)
    used_top_fallback = False
    if match is None:
        expected = wx.ocr_match_key(query)
        match = next(
            (
                line
                for line in sorted(lines, key=lambda item: (item.top, item.left))
                if line.top < top_limit and expected in wx.ocr_match_key(line.text)
            ),
            None,
        )
        used_top_fallback = match is not None
    crop_path.unlink(missing_ok=True)
    if match is None:
        raise RuntimeError("group_not_found_in_group_results")
    headings = sorted(
        (
            (line, wx.section_kind(line.text))
            for line in lines
            if wx.section_kind(line.text) is not None
        ),
        key=lambda item: item[0].top,
    )
    containing_section = next(
        (
            kind
            for line, kind in reversed(headings)
            if line.bottom < match.top
        ),
        None,
    )
    if used_top_fallback or containing_section == "most_used":
        selection_index = 0
    else:
        expected = wx.ocr_match_key(query)
        candidate_rows: list[int] = []
        for line in sorted(lines, key=lambda item: (item.top, item.left)):
            if line.top > match.top + 8 or expected not in wx.ocr_match_key(line.text):
                continue
            center = (line.top + line.bottom) // 2
            if not candidate_rows or center - candidate_rows[-1] > 12:
                candidate_rows.append(center)
        if not candidate_rows:
            raise RuntimeError("group_result_keyboard_index_not_found")
        selection_index = len(candidate_rows)
    open_group.open_search_result_with_keyboard(window, selection_index)
    time.sleep(wx.CHAT_OPEN_WAIT_SECONDS)
    opened_path = directory / "opened.png"
    wx.capture_live_window(window, opened_path)
    verified = wx.verify_opened_title(tesseract, opened_path, query)
    if not verified:
        opened_lines = open_group.run_ocr(
            tesseract, opened_path, psm=11, language="chi_sim+eng"
        )
        if login_required(opened_lines):
            raise RuntimeError("weixin_login_required")
        raise RuntimeError("opened_group_title_not_verified")
    results_path.unlink(missing_ok=True)
    return audit.select_weixin_window(int(window["pid"])) or window


def settings_visible(
    lines: list[open_group.OcrLine], image_width: int | None = None
) -> bool:
    # Only accept settings markers in the right-side panel. Chat messages can
    # contain the same words and must not make a normal conversation look like
    # a group settings view.
    candidates = (
        line
        for line in lines
        if image_width is None or line.left >= round(image_width * 0.65)
    )
    normalized = [open_group.normalize_text(line.text) for line in candidates]
    return any(
        marker in text
        for text in normalized
        for marker in (
            "groupname",
            "myaliasingroup",
            "\u7fa4\u804a\u540d\u79f0",
            "\u6211\u5728\u672c\u7fa4\u7684\u6635\u79f0",
        )
    )


def login_required(lines: list[open_group.OcrLine]) -> bool:
    normalized = [open_group.normalize_text(line.text) for line in lines]
    return any(
        ("accountsecurity" in text and "loginagain" in text)
        or "为了账号安全请重新登录" in text
        or "transferfilesonly" in text
        or "仅传输文件" in text
        for text in normalized
    )


def prepare_group_member_search(
    window: dict[str, object], directory: Path, tesseract: Path
) -> tuple[dict[str, object], Image.Image]:
    """Open group settings when necessary and focus its member search field."""
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ".group-settings.png"
    image, _ = wx.capture_live_window(window, probe)
    debug_step(directory, "settings-before-search", image, search_point=wx.point_in_window(window, GROUP_SEARCH_POINT))
    lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    for attempt in range(3):
        if settings_visible(lines, image.width):
            break
        point = (
            int(window["left"]) + int(window["width"]) - 62,
            int(window["top"]) + 84,
        )
        open_group.click_screen_point(point)
        time.sleep(SETTINGS_WAIT_SECONDS + attempt * 0.2)
        window = audit.select_weixin_window(int(window["pid"])) or window
        image, _ = wx.capture_live_window(window, probe)
        lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    if not settings_visible(lines, image.width):
        raise RuntimeError("group_settings_not_detected")

    search_point = wx.point_in_window(window, GROUP_SEARCH_POINT)
    open_group.click_screen_point(search_point)
    time.sleep(0.2)
    hwnd = int(window["hwnd"])
    search_visible = member_search_visible(window)
    debug_step(
        directory,
        "search-focus-check",
        point=search_point,
        search_visible=search_visible,
        native_focus=weixin_has_keyboard_focus(hwnd),
    )
    if not search_visible and not weixin_has_keyboard_focus(hwnd):
        # Native focus/UIA handles are unreliable for this Qt control. Retry
        # the known search point once, then let replace_search_text perform
        # the actual select-all and typing operation.
        open_group.click_screen_point(search_point)
        time.sleep(0.25)
        debug_step(
            directory,
            "search-focus-retry",
            point=search_point,
            search_visible=member_search_visible(window),
            native_focus=weixin_has_keyboard_focus(hwnd),
        )
    probe.unlink(missing_ok=True)
    return window, image


def save_list_pages(
    window: dict[str, object],
    directory: Path,
    term: str,
    maximum: int,
    deadline: float,
) -> list[str]:
    outputs: list[str] = []
    label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", term).strip("-") or "term"
    for index in range(maximum):
        ensure_term_time(deadline, term, f"list-before-{index + 1}")
        if wx.consume_skip_request():
            break
        path = directory / f"members-{label}-{index + 1:03d}.png"
        image, _ = wx.capture_live_window(window, path)
        ensure_term_time(deadline, term, f"list-after-capture-{index + 1}")
        rows = result_member_rows(image)
        if not rows:
            path.unlink(missing_ok=True)
            break
        outputs.append(str(path.resolve()))
        if wx.stop_requested():
            break
        before = crop_result_panel(image)
        ensure_term_time(deadline, term, f"list-before-scroll-{index + 1}")
        scroll_member_results(window)
        if wx.stop_requested():
            break
        after_path = directory / ".members-after.png"
        after_image, _ = wx.capture_live_window(window, after_path)
        after_path.unlink(missing_ok=True)
        if not result_page_changed(before, crop_result_panel(after_image)):
            # The current image is the last valid page. Keep it once, but do
            # not issue another wheel event or create a duplicate page.
            break
    return outputs


def save_detail_pages(
    window: dict[str, object],
    directory: Path,
    term: str,
    maximum: int,
    tesseract: Path,
    seen_identifiers: set[str],
    deadline: float,
) -> list[str]:
    outputs: list[str] = []
    for step in range(maximum):
        ensure_term_time(deadline, term, f"detail-before-{step + 1}")
        if wx.consume_skip_request():
            break
        # Only use the bottom-most currently visible row. A single missed OCR
        # row can otherwise make an indexed loop jump from A to C; bottom-anchor
        # traversal may revisit a member, but it cannot skip past one.
        page_path = directory / ".member-results-current.png"
        current_image, _ = wx.capture_live_window(window, page_path)
        current_rows = result_member_rows(current_image)
        if not current_rows:
            page_path.unlink(missing_ok=True)
            break
        row = current_rows[-1]
        click_x = min(current_image.width - 48, row.right + 55)
        click_y = row.top + max(8, min(18, (row.bottom - row.top) // 3))
        debug_step(
            directory,
            f"before-detail-click-{step + 1:04d}",
            current_image,
            ordinal=step + 1,
            point=wx.screen_point_from_capture(window, current_image, click_x, click_y),
            row=(row.left, row.top, row.right, row.bottom),
            strategy="bottom_anchor",
        )
        open_group.click_screen_point(
            wx.screen_point_from_capture(window, current_image, click_x, click_y)
        )
        ensure_term_time(deadline, term, f"detail-after-click-{step + 1}")
        time.sleep(wait_seconds("WECHAT_PROFILE_DELAY", PROFILE_WAIT_SECONDS))
        candidate = directory / ".member-profile.png"
        candidate_image, _ = wx.capture_live_window(window, candidate)
        lines = open_group.run_ocr(tesseract, candidate, psm=11, language="chi_sim+eng")
        identifier = wx.contact_identifier(lines, candidate_image)
        if identifier is not None and identifier not in seen_identifiers:
            seen_identifiers.add(identifier)
            output = directory / f"member-{len(seen_identifiers):05d}.png"
            candidate.replace(output)
            outputs.append(str(output.resolve()))
        else:
            candidate.unlink(missing_ok=True)
        page_path.unlink(missing_ok=True)
        if not close_member_profile(window, directory, step + 1):
            raise RuntimeError("group_member_panel_not_recovered")

        before_path = directory / ".members-before-step-scroll.png"
        before_image, _ = wx.capture_live_window(window, before_path)
        scroll_member_results(window, delta=600)
        time.sleep(wait_seconds("WECHAT_DETAIL_SCROLL_DELAY", SCROLL_WAIT_SECONDS))
        after_path = directory / ".members-after-step.png"
        after_image, _ = wx.capture_live_window(window, after_path)
        changed = result_page_changed(
            crop_result_panel(before_image), crop_result_panel(after_image)
        )
        before_path.unlink(missing_ok=True)
        after_path.unlink(missing_ok=True)
        if not changed:
            break
    return outputs


def backup_open_group(
    window: dict[str, object],
    directory: Path,
    terms: tuple[str, ...],
    member_mode: str,
    maximum_pages: int,
    tesseract: Path,
    term_timeout: float | None = None,
) -> dict[str, object]:
    """Backup one opened group's visible member search results."""
    directory.mkdir(parents=True, exist_ok=True)
    for pattern in ("members-*.png", "member-*.png"):
        for stale in directory.glob(pattern):
            stale.unlink(missing_ok=True)
    window, _ = prepare_group_member_search(window, directory, tesseract)
    outputs: list[str] = []
    seen_identifiers: set[str] = set()
    exposed_identifiers: set[str] = set()
    selected_mode = member_mode
    decisions: dict[str, str] = {}
    timeout_seconds = term_timeout if term_timeout is not None else wait_seconds("WECHAT_MEMBER_TERM_TIMEOUT", 60.0)
    for term in terms:
        if wx.consume_skip_request():
            continue
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        try:
            if not reset_member_results_to_top(window, directory, term, deadline):
                raise MemberTermTimeout(term, "reset-top-not-confirmed")
            replace_search_text(window, term)
            ensure_term_time(deadline, term, "after-input")
            probe = directory / ".member-mode-probe.png"
            image, _ = wx.capture_live_window(window, probe)
            ensure_term_time(deadline, term, "after-capture")
            debug_step(directory, "after-input", image, term=term)
            lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
            rows = result_member_rows(image)
            if not rows:
                label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", term).strip("-") or "term"
                empty_path = directory / f"members-{label}-empty.png"
                probe.replace(empty_path)
                outputs.append(str(empty_path.resolve()))
                decisions[term] = "empty"
                debug_step(directory, "term-finished", term=term, reason="empty", pages=0)
                continue
            mode = selected_mode
            exposed_ids = visible_identifiers(lines, image)
            if list_ids_enabled() and not exposed_ids:
                exposed_ids = visible_identifiers_from_panel(tesseract, image, probe)
            exposed_identifiers.update(exposed_ids)
            if list_ids_enabled():
                mode = "list"
            elif mode == "auto":
                mode = "detail"
            decisions[term] = mode
            probe.unlink(missing_ok=True)
            if mode == "list":
                if wx.consume_skip_request():
                    continue
                outputs.extend(save_list_pages(window, directory, term, maximum_pages, deadline))
            else:
                debug_step(directory, "before-detail-clicks", image, term=term, rows=len(rows), mode=mode)
                outputs.extend(save_detail_pages(window, directory, term, maximum_pages, tesseract, seen_identifiers, deadline))
            ensure_term_time(deadline, term, "term-finished")
            debug_step(directory, "term-finished", term=term, reason="completed", pages=len(outputs))
        except MemberTermTimeout as error:
            decisions[term] = "timeout"
            debug_step(directory, "term-timeout", term=term, stage=error.stage, pages=len(outputs))
            return {
                "ok": False,
                "reason": "member_term_timeout",
                "term": term,
                "stage": error.stage,
                "member_mode": member_mode,
                "terms": list(terms),
                "decisions": decisions,
                "pages": outputs,
                "identifiers": sorted(seen_identifiers | exposed_identifiers),
            }
    return {
        "ok": bool(outputs),
        "member_mode": member_mode,
        "terms": list(terms),
        "decisions": decisions,
        "pages": outputs,
        "identifiers": sorted(seen_identifiers | exposed_identifiers),
    }


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.s <= MAX_PAGES:
        print(f"-s 必须在 1 到 {MAX_PAGES} 之间。")
        return 2
    terms = parse_terms(args.terms)
    if not terms:
        print("-k 至少需要一个搜索词。")
        return 2
    tesseract = open_group.resolve_tesseract(args.tesseract)
    if tesseract is None:
        print("未找到 Tesseract OCR。")
        return 2
    pid, pid_source = wx.select_pid(args.p, audit.DEFAULT_PANEL_CONFIG)
    if pid is None:
        print(f"无法确定微信 PID（{pid_source}）。")
        return 2
    window = audit.select_weixin_window(pid)
    if window is None:
        print(f"未找到 PID {pid} 的微信主窗口。")
        return 2
    activation = audit.activate_window(window)
    if not activation["activated"]:
        print(json.dumps({"ok": False, "activation": activation}, ensure_ascii=False))
        return 2
    args.o.mkdir(parents=True, exist_ok=True)
    try:
        window = activation["window"]
        if args.group:
            window = open_named_group(window, args.group, args.o, tesseract)
        result = backup_open_group(
            window, args.o, terms, args.member_mode, args.s, tesseract, args.term_timeout
        )
    except RuntimeError as error:
        result = {"ok": False, "reason": str(error), "pages": []}
    result.update({"target_pid": pid, "pid_source": pid_source})
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    (args.o / "result.json").write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
