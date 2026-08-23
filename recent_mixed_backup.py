"""Single-pass recent conversation backup for people and groups."""

from __future__ import annotations

import re
import os
import time
from pathlib import Path

from PIL import Image

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


def safe_group_directory(base: Path, index: int, title: str | None) -> Path:
    label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", title or "group").strip("-")
    return base / f"group-{index:03d}-{label[:40] or 'group'}"


def enter_folded_chats(
    window: dict[str, object], directory: Path, tesseract: Path
) -> bool:
    """Enter Weixin's folded/minimized chat list from the recent list."""
    probe = directory / ".folded-chats-entry.png"
    image, _ = wx.capture_live_window(window, probe)
    lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    normalized = [open_group.normalize_text(line.text) for line in lines]
    if any(
        marker in text for text in normalized for marker in ("折叠的聊天", "折叠的群聊", "minimizedgroups")
    ):
        probe.unlink(missing_ok=True)
        return True
    open_group.click_screen_point(wx.sidebar_point(window, wx.CHAT_NAV))
    time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    wx.scroll_list_to_top(window)
    image, _ = wx.capture_live_window(window, probe)
    lines = open_group.run_ocr(tesseract, probe, psm=11, language="chi_sim+eng")
    entry = next(
        (
            line
            for line in wx.left_pane_lines(lines, image)
            if any(
                marker in open_group.normalize_text(line.text)
                for marker in ("折叠的聊天", "折叠的群聊", "minimizedgroups")
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
    preexisting_auxiliary_windows = wx.wechat_auxiliary_windows()

    def return_to_list(*, profile_open: bool = False) -> None:
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
            time.sleep(0.2)
        open_group.click_screen_point(wx.sidebar_point(current, wx.CHAT_NAV))
        time.sleep(wx.NAVIGATION_WAIT_SECONDS)
        if start_current_list:
            # Clicking the chat navigation icon leaves the folded list. Reopen
            # it before the next row is inspected.
            enter_folded_chats(current, directory, tesseract)

    activation = audit.activate_window(window)
    if activation["activated"]:
        window = activation["window"]
    if not start_current_list:
        open_group.click_screen_point(wx.sidebar_point(window, wx.CHAT_NAV))
        time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    wx.scroll_list_to_top(window)
    stop_reason = "maximum_pages"
    for page_index in range(wx.MAX_PAGES):
        frame = directory / ".recent-mixed-list.png"
        window = audit.select_weixin_window(int(window["pid"])) or window
        list_image, _ = wx.capture_live_window(window, frame)
        rows = wx.recent_conversation_rows(list_image)
        if not rows:
            frame.replace(directory / "recent-mixed-not-detected.png")
            stop_reason = "recent_chats_not_detected"
            break
        for row in rows:
            if len(people) >= people_limit and len(groups) >= group_limit:
                stop_reason = "selected_limits_reached"
                frame.unlink(missing_ok=True)
                return {
                    "ok": bool(people or groups),
                    "people": people,
                    "groups": groups,
                    "stop_reason": stop_reason,
                    "pages_scanned": page_index + 1,
                }
            window = audit.select_weixin_window(int(window["pid"])) or window
            open_group.click_screen_point(
                wx.screen_point_from_capture(
                    window,
                    list_image,
                    (row.left + row.right) // 2,
                    (row.top + row.bottom) // 2,
                )
            )
            time.sleep(wx.CHAT_OPEN_WAIT_SECONDS)
            open_group.click_screen_point(
                (int(window["left"]) + int(window["width"]) - 62, int(window["top"]) + 84)
            )
            time.sleep(wx.CONTACT_DETAIL_WAIT_SECONDS)
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
                time.sleep(wx.CONTACT_DETAIL_WAIT_SECONDS)
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
                        "stop_reason": "group_error_stop",
                        "pages_scanned": page_index + 1,
                    }
            result["title"] = title
            groups.append(result)
            if title_key:
                seen_groups.add(title_key)
            return_to_list()

        before, _ = wx.crop_left_pane(list_image)
        frame.unlink(missing_ok=True)
        wx.scroll_list(window)
        after_path = directory / ".recent-mixed-after.png"
        after_full, _ = wx.capture_live_window(window, after_path)
        after_path.unlink(missing_ok=True)
        after, _ = wx.crop_left_pane(after_full)
        if not wx.page_changed(before, after):
            stop_reason = "scrollbar_bottom"
            break
    result = {
        "ok": bool(people or groups),
        "people": people,
        "groups": groups,
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
