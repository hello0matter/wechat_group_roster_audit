"""Single-pass recent conversation backup for people and groups."""

from __future__ import annotations

import re
import os
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw
from PIL import ImageStat

import group_member_backup
import open_group
import wechat_group_roster_audit as audit
import wx


def chat_title(lines: list[open_group.OcrLine], image: Image.Image) -> str | None:
    """Return the best OCR candidate from the active chat header."""
    candidates = [
        line.text.strip()
        for line in lines
        if line.top < round(image.height * 0.13)
        and line.left > round(image.width * 0.34)
        and line.right < round(image.width * 0.82)
        and len(open_group.normalize_text(line.text)) >= 2
    ]
    return max(candidates, key=len, default=None)


def matches_group_keywords(title: str | None, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return True
    if title is None:
        return False
    normalized = open_group.normalize_text(title)
    return any(open_group.normalize_text(keyword) in normalized for keyword in keywords)


def row_is_draft(
    image: Image.Image,
    row: open_group.OcrLine,
    tesseract: Path,
    directory: Path,
) -> bool:
    """Confirm 草稿 with OCR; red pixels alone are not sufficient."""
    if not wx.recent_row_has_red_ink(image, row):
        return False
    # The row is already at native resolution; use a moderate enlargement for
    # Chinese glyphs while keeping the candidate OCR cheap.
    crop = image.crop(
        (
            round(image.width * 0.16),
            max(0, row.top - 6),
            round(image.width * 0.72),
            min(image.height, row.bottom + 8),
        )
    ).resize(
        (round(image.width * 0.56 * 2), max(1, (row.bottom - row.top + 14) * 2)),
        Image.Resampling.LANCZOS,
    )
    probe = directory / f".draft-row-{row.top}.png"
    crop.save(probe)
    try:
        lines = open_group.run_ocr(tesseract, probe, psm=7, language="chi_sim+eng")
    except RuntimeError:
        return False
    finally:
        probe.unlink(missing_ok=True)
    return any("草稿" in line.text for line in lines)


FOLDED_MARKERS = ("\u6298\u53e0\u7684\u804a\u5929", "\u6298\u53e0\u7684\u7fa4\u804a", "minimizedgroups")


def folded_marker_line(
    lines: list[open_group.OcrLine], image: Image.Image
) -> open_group.OcrLine | None:
    """Return a folded-chat marker that is actually in Weixin's left pane."""
    candidates = [
        line
        for line in wx.left_pane_lines(lines, image)
        if any(marker in open_group.normalize_text(line.text) for marker in FOLDED_MARKERS)
    ]
    return min(candidates, key=lambda line: (line.top, line.left), default=None)


def folded_view_visible(
    image: Image.Image, lines: list[open_group.OcrLine]
) -> bool:
    """Require a top folded heading and rows below it.

    The normal recent list can contain the folded-chat entry row. Checking
    only OCR text therefore confuses that row with the already-open folded
    pane. A real folded pane has the marker near the top and at least one
    folded-list row below the marker.
    """
    marker = folded_marker_line(lines, image)
    if marker is None or marker.top > round(image.height * 0.22):
        return False
    rows = wx.folded_conversation_rows(image)
    return any(row.top > marker.bottom + 8 for row in rows)


def row_has_folded_entry(
    image: Image.Image,
    row: open_group.OcrLine,
    tesseract: Path,
    directory: Path,
) -> bool:
    """Recognize the special folded entry using only the clicked list row."""
    crop_box = (
        round(image.width * 0.14),
        max(0, row.top - 12),
        round(image.width * 0.72),
        min(image.height, row.bottom + 16),
    )
    crop = image.crop(crop_box).resize(
        ((crop_box[2] - crop_box[0]) * 2, (crop_box[3] - crop_box[1]) * 2),
        Image.Resampling.LANCZOS,
    )
    probe = directory / f".folded-entry-row-{row.top}.png"
    crop.save(probe)
    try:
        lines = open_group.run_ocr(tesseract, probe, psm=7, language="chi_sim+eng")
    except RuntimeError:
        return False
    finally:
        probe.unlink(missing_ok=True)
    return any(
        any(marker in open_group.normalize_text(line.text) for marker in FOLDED_MARKERS)
        for line in lines
    )


def opened_left_has_draft(
    image: Image.Image,
    tesseract: Path,
    probe: Path,
    row: open_group.OcrLine,
) -> bool:
    """Detect a draft in the clicked row using a local post-click OCR crop."""
    y_center = (row.top + row.bottom) // 2
    crop_box = (
        round(image.width * 0.14),
        max(0, y_center - 90),
        round(image.width * 0.72),
        min(image.height, y_center + 90),
    )
    crop = image.crop(crop_box).resize(
        ((crop_box[2] - crop_box[0]) * 2, (crop_box[3] - crop_box[1]) * 2),
        Image.Resampling.LANCZOS,
    )
    crop_path = probe.with_name(f"{probe.stem}-row-draft.png")
    crop.save(crop_path)
    try:
        lines = open_group.run_ocr(tesseract, crop_path, psm=7, language="chi_sim+eng")
    except RuntimeError:
        return False
    finally:
        crop_path.unlink(missing_ok=True)
    return any("\u8349\u7a3f" in line.text for line in lines)

def folded_scope_visible(
    image: Image.Image, tesseract: Path, probe: Path
) -> bool:
    """Recognize the folded-chat scope after its entry has been clicked."""
    try:
        lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    except RuntimeError:
        return False
    return folded_view_visible(image, lines)

def chat_surface_ready(image: Image.Image) -> bool:
    """Reject the blank right pane shown while a folded row is still opening."""
    crop = image.crop((round(image.width * 0.43), round(image.height * 0.12), image.width, image.height))
    rgb = crop.convert("RGB")
    # The empty pane only contains the pale background and the WeChat logo.
    # Requiring both variation and a small amount of dark content avoids
    # clicking the settings button while a folded row is still transitioning.
    pixels = list(rgb.getdata())
    dark_ratio = sum(1 for red, green, blue in pixels if max(red, green, blue) < 180) / max(len(pixels), 1)
    # Normal chats with mostly light message bubbles can have low global
    # variance. The old 120/0.012 gate rejected real chats and made the group
    # branch appear to do nothing. Keep a small dark-content guard for the
    # genuinely blank WeChat logo surface, but accept light normal chats.
    return sum(ImageStat.Stat(rgb).stddev) > 55 and dark_ratio > 0.008


def safe_group_directory(base: Path, index: int, title: str | None) -> Path:
    label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", title or "group").strip("-")
    return base / f"group-{index:03d}-{label[:40] or 'group'}"


def save_audit_frame(directory: Path, step: int, stage: str, image: Image.Image) -> None:
    """Persist each recent-list transition for coordinate and skip auditing."""
    audit_dir = directory / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    image.save(audit_dir / f"step-{step:04d}-{stage}.png")


def save_audit_event(directory: Path, event: dict[str, object]) -> None:
    """Record the image-to-click binding used for one automation decision."""
    audit_dir = directory / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    input_log = os.environ.get("WECHAT_INPUT_LOG")
    if input_log:
        event.setdefault("input_log", input_log)
    with (audit_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def mark_click_on_frame(
    directory: Path, step: int, stage: str, image: Image.Image, click_xy: tuple[int, int]
) -> str:
    """Save a copy of image with a red crosshair at click_xy; return the filename."""
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    x, y = click_xy
    r = 18
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=3)
    draw.line([x - r * 2, y, x + r * 2, y], fill=(255, 0, 0), width=2)
    draw.line([x, y - r * 2, x, y + r * 2], fill=(255, 0, 0), width=2)
    filename = f"step-{step:04d}-{stage}-marked.png"
    audit_dir = directory / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    marked.save(audit_dir / filename)
    return filename


def row_audit(row: open_group.OcrLine) -> dict[str, object]:
    return {
        "text": row.text,
        "left": row.left,
        "top": row.top,
        "right": row.right,
        "bottom": row.bottom,
    }


def enter_folded_chats(
    window: dict[str, object], directory: Path, tesseract: Path
) -> bool:
    """Enter Weixin's folded/minimized chat list from the recent list."""
    probe = directory / ".folded-chats-entry.png"
    image, _ = wx.capture_live_window(window, probe)
    try:
        lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    except RuntimeError:
        lines = []
    folded_marker = folded_marker_line(lines, image)
    # A top marker is only an already-open folded pane when actual folded rows
    # are visible below it. A recent-list folded-entry row must still be clicked.
    if folded_view_visible(image, lines):
        probe.unlink(missing_ok=True)
        return True
    if folded_marker is not None:
        open_group.click_screen_point(
            wx.screen_point_from_capture(
                window,
                image,
                (folded_marker.left + folded_marker.right) // 2,
                (folded_marker.top + folded_marker.bottom) // 2,
            )
        )
        time.sleep(max(wx.NAVIGATION_WAIT_SECONDS, wx.chat_open_delay()))
        probe.unlink(missing_ok=True)
        return True
    # Chinese folded labels are frequently missed in a full-window OCR pass.
    # Retry only the left list, enlarged, before giving up on the scope.
    left_box = (
        round(image.width * 0.05),
        round(image.height * 0.08),
        round(image.width * 0.40),
        round(image.height * 0.96),
    )
    left_crop = image.crop(left_box).resize(
        ((left_box[2] - left_box[0]) * 2, (left_box[3] - left_box[1]) * 2),
        Image.Resampling.LANCZOS,
    )
    left_probe = directory / ".folded-left-ocr.png"
    left_crop.save(left_probe)
    try:
        left_lines = open_group.run_ocr(tesseract, left_probe, psm=6, language="chi_sim+eng")
    except RuntimeError:
        left_lines = []
    finally:
        left_probe.unlink(missing_ok=True)
    entry = next(
        (
            line
            for line in left_lines
            if any(
                marker in open_group.normalize_text(line.text)
                for marker in FOLDED_MARKERS
            )
        ),
        None,
    )
    if entry is not None:
        x = round(left_box[0] + entry.left / 2)
        y = round(left_box[1] + entry.top / 2)
        open_group.click_screen_point(wx.screen_point_from_capture(window, image, x, y))
        time.sleep(max(wx.NAVIGATION_WAIT_SECONDS, wx.chat_open_delay()))
        probe.unlink(missing_ok=True)
        return True
    open_group.click_screen_point(wx.sidebar_point(window, wx.CHAT_NAV))
    time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    wx.scroll_list_to_top(window)
    image, _ = wx.capture_live_window(window, probe)
    try:
        lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    except RuntimeError:
        lines = []
    entry = next(
        (
            line
            for line in wx.left_pane_lines(lines, image)
            if any(
                marker in open_group.normalize_text(line.text)
                for marker in FOLDED_MARKERS
            )
        ),
        None,
    )
    if entry is None:
        probe.unlink(missing_ok=True)
        return False
    open_group.click_screen_point(
        wx.screen_point_from_capture(
            window,
            image,
            (entry.left + entry.right) // 2,
            (entry.top + entry.bottom) // 2,
        )
    )
    time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    probe.unlink(missing_ok=True)
    return True


def save_recent_mixed(
    window: dict[str, object],
    directory: Path,
    tesseract: Path,
    *,
    include_people: bool,
    include_groups: bool,
    people_limit: int,
    group_limit: int,
    group_keywords: tuple[str, ...],
    member_terms: tuple[str, ...],
    member_mode: str,
    member_pages: int,
    start_current_list: bool = False,
    include_folded: bool = True,
) -> dict[str, object]:
    """Scan the recent list once and dispatch each row by chat settings layout."""
    directory.mkdir(parents=True, exist_ok=True)
    people: list[str] = []
    groups: list[dict[str, object]] = []
    seen_people: set[str] = set()
    seen_groups: set[str] = set()
    skipped_drafts = 0
    skipped_people = 0
    preexisting_auxiliary_windows = wx.wechat_auxiliary_windows()
    include_folded = include_folded and os.environ.get("WECHAT_FOLDED_GROUPS", "1").lower() not in {"0", "false", "no", "off"}

    def return_to_list(*, profile_open: bool = False, settings_open: bool = False) -> None:
        wx.close_wechat_auxiliary_windows(
            wx.wechat_auxiliary_windows() - preexisting_auxiliary_windows
        )
        current = audit.select_weixin_window(int(window["pid"])) or window
        activation = audit.activate_window(current)
        if activation["activated"]:
            current = activation["window"]
        if profile_open:
            open_group.click_screen_point(
                (int(current["left"]) + int(current["width"]) - 25, int(current["top"]) + 600)
            )
            time.sleep(wx.profile_delay())
        if settings_open:
            # Group member backup leaves the chat-settings pane open. The
            # sidebar Chat button is already selected, so clicking it again
            # does not restore a clean list. Toggle the same header menu to
            # close the pane before the next list capture.
            open_group.click_screen_point(
                (int(current["left"]) + int(current["width"]) - 62, int(current["top"]) + 84)
            )
            time.sleep(wx.settings_delay())
        if start_current_list:
            # Folded chats has its own back arrow. Clicking the global chat
            # navigation closes that scope and can make the next row vanish.
            open_group.click_screen_point(
                (int(current["left"]) + round(int(current["width"]) * 0.09),
                 int(current["top"]) + round(int(current["height"]) * 0.18))
            )
            time.sleep(wx.NAVIGATION_WAIT_SECONDS)
        else:
            open_group.click_screen_point(wx.sidebar_point(current, wx.CHAT_NAV))
            time.sleep(wx.NAVIGATION_WAIT_SECONDS)

    activation = audit.activate_window(window)
    if activation["activated"]:
        window = activation["window"]
    current_folded = False
    if not start_current_list:
        current_probe = directory / ".current-chat-scope.png"
        current_image, _ = wx.capture_live_window(window, current_probe)
        try:
            current_lines = open_group.run_ocr(
                tesseract, current_probe, psm=11, language="chi_sim+eng"
            )
        except RuntimeError:
            current_lines = []
        current_folded = folded_view_visible(current_image, current_lines)
        current_probe.unlink(missing_ok=True)
    if not start_current_list and not current_folded:
        open_group.click_screen_point(wx.sidebar_point(window, wx.CHAT_NAV))
        time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    if current_folded:
        start_current_list = True
        include_folded = False
    if not start_current_list:
        wx.scroll_list_to_top(window)
    stop_reason = "maximum_pages"
    for page_index in range(wx.MAX_PAGES):
        frame = directory / ".recent-mixed-list.png"
        window = audit.select_weixin_window(int(window["pid"])) or window
        list_image, _ = wx.capture_live_window(window, frame)
        rows = (
            wx.folded_conversation_rows(list_image)
            if start_current_list
            else wx.recent_conversation_rows(list_image)
        )
        if start_current_list and not rows:
            # Give the folded pane a moment to finish its transition before
            # declaring it empty and leaving the scope.
            time.sleep(wx.NAVIGATION_WAIT_SECONDS)
            list_image, _ = wx.capture_live_window(window, frame)
            rows = wx.folded_conversation_rows(list_image)
        if not rows:
            # A visible Weixin window can still be on the chat surface after
            # activation (especially when the previous group left its
            # settings pane open). Recover the list before declaring the
            # recent-chat task dead. Do not click arbitrary rows while the
            # list has not been verified.
            recovered = False
            for retry_index in range(1, 4):
                save_audit_frame(
                    directory,
                    page_index + 1,
                    f"list-retry-{retry_index}-before",
                    list_image,
                )
                current = audit.select_weixin_window(int(window["pid"])) or window
                open_group.click_screen_point(wx.sidebar_point(current, wx.CHAT_NAV))
                time.sleep(wx.navigation_delay())
                retry_path = directory / f".recent-list-retry-{retry_index}.png"
                retry_image, _ = wx.capture_live_window(current, retry_path)
                retry_rows = (
                    wx.folded_conversation_rows(retry_image)
                    if start_current_list
                    else wx.recent_conversation_rows(retry_image)
                )
                save_audit_frame(
                    directory,
                    page_index + 1,
                    f"list-retry-{retry_index}-after",
                    retry_image,
                )
                retry_path.unlink(missing_ok=True)
                if retry_rows:
                    list_image = retry_image
                    rows = retry_rows
                    recovered = True
                    break
            if not recovered:
                frame.replace(directory / "recent-mixed-not-detected.png")
                stop_reason = "recent_chats_not_detected"
                break
        row = min(rows, key=lambda candidate: candidate.top)
        for row in (row,):
            if wx.consume_skip_request():
                continue
            if len(people) >= people_limit and len(groups) >= group_limit:
                stop_reason = "selected_limits_reached"
                frame.unlink(missing_ok=True)
                return {
                    "ok": bool(people or groups),
                    "people": people,
                    "groups": groups,
                    "skipped_drafts": skipped_drafts,
                    "skipped_people": skipped_people,
                    "stop_reason": stop_reason,
                    "pages_scanned": page_index + 1,
                }
            window = audit.select_weixin_window(int(window["pid"])) or window
            folded_entry_candidate = (
                not start_current_list
                and row_has_folded_entry(list_image, row, tesseract, directory)
            )
            red_candidate = (
                not start_current_list
                and wx.recent_row_has_red_ink(list_image, row)
            )
            save_audit_frame(directory, page_index + 1, "before-click", list_image)
            save_audit_event(
                directory,
                {
                    "step": page_index + 1,
                    "stage": "row-classification",
                    "scope": "recent",
                    "row": row_audit(row),
                    "folded_entry_candidate": folded_entry_candidate,
                    "red_candidate": red_candidate,
                },
            )
            click_capture = (
                round(list_image.width * (0.18 if start_current_list else 0.135)),
                (row.top + row.bottom) // 2,
            )
            click_screen = wx.screen_point_from_capture(
                window, list_image, *click_capture
            )
            marked_filename = mark_click_on_frame(
                directory, page_index + 1, "before-click", list_image, click_capture
            )
            save_audit_event(
                directory,
                {
                    "step": page_index + 1,
                    "stage": "before-click",
                    "scope": "folded" if start_current_list else "recent",
                    "image": "before-click",
                    "click_marked_image": marked_filename,
                    "image_size": list_image.size,
                    "row": row_audit(row),
                    "visible_rows": [row_audit(candidate) for candidate in rows],
                    "click_capture": click_capture,
                    "click_screen": click_screen,
                },
            )
            open_group.click_screen_point(click_screen)
            time.sleep(wx.chat_open_delay())
            opened_probe = directory / ".recent-opened-probe.png"
            opened_image, _ = wx.capture_live_window(window, opened_probe)
            save_audit_frame(directory, page_index + 1, "after-click", opened_image)
            save_audit_event(
                directory,
                {
                    "step": page_index + 1,
                    "stage": "after-click",
                    "scope": "folded" if start_current_list else "recent",
                    "image": "after-click",
                    "image_size": opened_image.size,
                    "click_capture": click_capture,
                    "click_screen": click_screen,
                    "chat_surface_ready": chat_surface_ready(opened_image),
                    "folded_rows": [
                        row_audit(candidate)
                        for candidate in wx.folded_conversation_rows(opened_image)
                    ],
                },
            )
            if not start_current_list and folded_entry_candidate:
                # Record the folded entry in place, but do not enter it yet.
                # The normal recent list must be scanned from top to bottom;
                # folded chats are entered exactly once after this pass.
                save_audit_frame(directory, page_index + 1, "folded-entry-found", opened_image)
                save_audit_event(
                    directory,
                    {
                        "step": page_index + 1,
                        "stage": "folded-entry-found",
                        "scope": "recent",
                        "click_capture": click_capture,
                        "click_screen": click_screen,
                    },
                )
                opened_probe.unlink(missing_ok=True)
                return_to_list()
                continue
            if red_candidate and opened_left_has_draft(
                opened_image, tesseract, opened_probe, row
            ):
                skipped_drafts += 1
                save_audit_frame(directory, page_index + 1, "skip-draft-after-click", opened_image)
                save_audit_event(
                    directory,
                    {
                        "step": page_index + 1,
                        "stage": "skip-draft-after-click",
                        "scope": "folded" if start_current_list else "recent",
                        "click_capture": click_capture,
                        "click_screen": click_screen,
                    },
                )
                opened_probe.unlink(missing_ok=True)
                return_to_list()
                continue
            if not chat_surface_ready(opened_image) and start_current_list:
                # A folded row can remain selected while the right pane is still
                # blank. Never click the settings button in this state, and do
                # not return through the global chat navigation: retry the same
                # visible bottom row inside the folded scope.
                folded_opened = False
                for retry_index in range(1, 4):
                    save_audit_frame(
                        directory,
                        page_index + 1,
                        f"blank-retry-{retry_index}-before",
                        opened_image,
                    )
                    time.sleep(max(wx.NAVIGATION_WAIT_SECONDS, wx.chat_open_delay()))
                    retry_path = directory / f".folded-retry-{page_index + 1}-{retry_index}.png"
                    retry_image, _ = wx.capture_live_window(window, retry_path)
                    save_audit_frame(
                        directory,
                        page_index + 1,
                        f"blank-retry-{retry_index}-wait",
                        retry_image,
                    )
                    if chat_surface_ready(retry_image):
                        opened_probe.unlink(missing_ok=True)
                        opened_probe = retry_path
                        opened_image = retry_image
                        folded_opened = True
                        break
                    retry_rows = wx.folded_conversation_rows(retry_image)
                    retry_row = max(retry_rows, key=lambda candidate: candidate.bottom, default=None)
                    if retry_row is None:
                        retry_path.unlink(missing_ok=True)
                        continue
                    save_audit_frame(
                        directory,
                        page_index + 1,
                        f"blank-retry-{retry_index}-click",
                        retry_image,
                    )
                    open_group.click_screen_point(
                        wx.screen_point_from_capture(
                            window,
                            retry_image,
                            round(retry_image.width * 0.18),
                            (retry_row.top + retry_row.bottom) // 2,
                        )
                    )
                    time.sleep(max(wx.NAVIGATION_WAIT_SECONDS, wx.chat_open_delay()))
                    opened_probe.unlink(missing_ok=True)
                    opened_probe = directory / f".folded-retry-{page_index + 1}-{retry_index}-after-click.png"
                    opened_image, _ = wx.capture_live_window(window, opened_probe)
                    save_audit_frame(
                        directory,
                        page_index + 1,
                        f"blank-retry-{retry_index}-after-click",
                        opened_image,
                    )
                    retry_path.unlink(missing_ok=True)
                    if chat_surface_ready(opened_image):
                        folded_opened = True
                        break
                if not folded_opened:
                    save_audit_frame(
                        directory, page_index + 1, "blank-pane-no-settings", opened_image
                    )
                    opened_probe.unlink(missing_ok=True)
                    # Do not pretend this row was handled and do not scroll
                    # while the right pane is still blank. Stop the folded pass
                    # with the audit frames intact so the next run can retry it.
                    stop_reason = "folded_row_open_failed"
                    frame.unlink(missing_ok=True)
                    return {
                        "ok": bool(people or groups),
                        "people": people,
                        "groups": groups,
                        "skipped_drafts": skipped_drafts,
                        "skipped_people": skipped_people,
                        "stop_reason": stop_reason,
                        "pages_scanned": page_index + 1,
                    }
            if not chat_surface_ready(opened_image):
                opened_probe.unlink(missing_ok=True)
                return_to_list()
                continue
            opened_probe.unlink(missing_ok=True)
            open_group.click_screen_point(
                (int(window["left"]) + int(window["width"]) - 62, int(window["top"]) + 84)
            )
            time.sleep(wx.profile_delay())
            settings_path = directory / ".recent-mixed-settings.png"
            settings_image, _ = wx.capture_live_window(window, settings_path)
            settings_lines = open_group.run_ocr(
                tesseract, settings_path, psm=11, language="chi_sim+eng"
            )
            title = chat_title(settings_lines, settings_image)
            title_key = open_group.normalize_text(title or "")
            direct_avatar = wx.direct_chat_avatar(settings_lines, settings_image)
            tiles = wx.chat_settings_member_tiles(settings_image)
            settings_path.unlink(missing_ok=True)

            if direct_avatar is not None:
                if not include_people or len(people) >= people_limit:
                    return_to_list()
                    continue
                auxiliary_before = wx.wechat_auxiliary_windows()
                open_group.click_screen_point(
                    wx.screen_point_from_capture(window, settings_image, *direct_avatar)
                )
                time.sleep(wx.profile_delay())
                candidate = directory / ".recent-person.png"
                candidate_image, _ = wx.capture_live_window(window, candidate)
                candidate_lines = open_group.run_ocr(
                    tesseract, candidate, psm=11, language="chi_sim+eng"
                )
                identifier = wx.contact_identifier(candidate_lines, candidate_image)
                if identifier is not None and identifier not in seen_people:
                    seen_people.add(identifier)
                    output = directory / f"recent-person-{len(people) + 1:04d}.png"
                    candidate.replace(output)
                    people.append(str(output.resolve()))
                    return_to_list(profile_open=True)
                else:
                    candidate.unlink(missing_ok=True)
                    auxiliary = wx.wait_for_new_wechat_auxiliary_windows(auxiliary_before)
                    wx.close_wechat_auxiliary_windows(auxiliary)
                    return_to_list(profile_open=identifier is not None)
                continue

            is_group = len(tiles) >= 3 or group_member_backup.settings_visible(settings_lines)
            if not is_group or not include_groups or len(groups) >= group_limit:
                return_to_list()
                continue
            if not matches_group_keywords(title, group_keywords):
                return_to_list()
                continue
            if title_key and title_key in seen_groups:
                return_to_list()
                continue
            group_dir = safe_group_directory(directory / "groups", len(groups) + 1, title)
            try:
                result = group_member_backup.backup_open_group(
                    window,
                    group_dir,
                    member_terms,
                    member_mode,
                    member_pages,
                    tesseract,
                )
            except RuntimeError as error:
                result = {"ok": False, "reason": str(error), "pages": []}
                if os.environ.get("WECHAT_GROUP_ERROR_POLICY", "skip") == "stop":
                    groups.append({**result, "title": title})
                    return {
                        "ok": bool(people or groups),
                        "people": people,
                        "groups": groups,
                        "skipped_drafts": skipped_drafts,
                        "skipped_people": skipped_people,
                        "stop_reason": "group_error_stop",
                        "pages_scanned": page_index + 1,
                    }
            result["title"] = title
            groups.append(result)
            if title_key:
                seen_groups.add(title_key)
            return_to_list(settings_open=True)

        before, _ = wx.crop_left_pane(list_image)
        frame.unlink(missing_ok=True)
        returned_path = directory / ".recent-mixed-returned.png"
        returned_image, _ = wx.capture_live_window(window, returned_path)
        save_audit_frame(directory, page_index + 1, "after-return", returned_image)
        returned_path.unlink(missing_ok=True)
        scroll_before_path = directory / ".recent-mixed-before-scroll.png"
        scroll_before, _ = wx.capture_live_window(window, scroll_before_path)
        save_audit_frame(directory, page_index + 1, "before-scroll", scroll_before)
        scroll_point = wx.point_in_window(window, wx.LIST_SCROLL_POINT)
        scroll_delta = wx.recent_scroll_delta()
        save_audit_event(
            directory,
            {
                "step": page_index + 1,
                "stage": "scroll",
                "scope": "folded" if start_current_list else "recent",
                "before_image": "before-scroll",
                "scroll_screen": scroll_point,
                "delta": scroll_delta,
            },
        )
        wx.scroll_list(window, delta=scroll_delta)
        scroll_before_path.unlink(missing_ok=True)
        after_path = directory / ".recent-mixed-after.png"
        after_full, _ = wx.capture_live_window(window, after_path)
        save_audit_frame(directory, page_index + 1, "after-scroll", after_full)
        save_audit_event(
            directory,
            {
                "step": page_index + 1,
                "stage": "after-scroll",
                "scope": "folded" if start_current_list else "recent",
                "after_image": "after-scroll",
                "scroll_screen": scroll_point,
                "delta": scroll_delta,
                "image_size": after_full.size,
            },
        )
        after_path.unlink(missing_ok=True)
        after, _ = wx.crop_left_pane(after_full)
        if not wx.page_changed(before, after):
            stop_reason = "scrollbar_bottom"
            break
    result = {
        "ok": bool(people or groups),
        "people": people,
        "groups": groups,
        "skipped_drafts": skipped_drafts,
        "skipped_people": skipped_people,
        "stop_reason": stop_reason,
        "pages_scanned": page_index + 1,
    }
    if (
        include_folded
        and include_groups
        and len(groups) < group_limit
        and enter_folded_chats(window, directory, tesseract)
    ):
        folded = save_recent_mixed(
            window,
            directory / "folded",
            tesseract,
            include_people=False,
            include_groups=True,
            people_limit=0,
            group_limit=group_limit - len(groups),
            group_keywords=group_keywords,
            member_terms=member_terms,
            member_mode=member_mode,
            member_pages=member_pages,
            start_current_list=True,
            include_folded=False,
        )
        result["groups"].extend(folded["groups"])
        result["ok"] = bool(result["people"] or result["groups"])
        result["folded"] = folded
    return result
