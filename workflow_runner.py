"""Composable backup workflow used by both the GUI and portable runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path

from PIL import Image

import group_member_backup
import open_group
import recent_mixed_backup
import wechat_group_roster_audit as audit
import wx


TASKS = ("recent_people", "recent_groups", "contacts", "saved_groups")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run selected visible Weixin backup tasks")
    result.add_argument("-t", "--tasks", default=",".join(TASKS))
    result.add_argument("-g", "--groups", default="", help="comma-separated group filters")
    result.add_argument("-k", "--terms", default="1,a-z", help="group member search terms")
    result.add_argument("-M", "--member-mode", choices=("auto", "list", "detail"), default="auto")
    result.add_argument("-n", "--people-limit", type=int, default=1000)
    result.add_argument("-G", "--group-limit", type=int, default=1000)
    result.add_argument("-s", "--member-pages", type=int, default=1000)
    result.add_argument("-E", "--group-error", choices=("skip", "stop"), default="skip")
    result.add_argument("-o", "--output", type=Path, default=Path("artifacts/workflow"))
    result.add_argument("-p", "--pid", type=int)
    result.add_argument("--tesseract", type=Path)
    return result


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.replace("，", ",").split(",") if item.strip()))


def parse_tasks(value: str) -> tuple[str, ...]:
    tasks = parse_csv(value)
    unknown = set(tasks) - set(TASKS)
    if unknown:
        raise ValueError("unknown tasks: " + ", ".join(sorted(unknown)))
    return tasks


def discover_saved_group_names(
    window: dict[str, object],
    directory: Path,
    tesseract: Path,
    maximum: int,
) -> list[str]:
    """Read visible names from the expanded Saved Groups section."""
    open_group.click_screen_point(wx.sidebar_point(window, wx.CONTACTS_NAV))
    time.sleep(wx.NAVIGATION_WAIT_SECONDS)
    wx.scroll_list_to_top(window)
    if not wx.expand_saved_groups(window, tesseract, directory):
        return []
    names: list[str] = []
    section_started = False
    for index in range(wx.MAX_PAGES):
        frame = directory / f".saved-discovery-{index + 1:03d}.png"
        image, _ = wx.capture_live_window(window, frame)
        lines = open_group.run_ocr(tesseract, frame, psm=11, language="chi_sim+eng")
        pane_lines = wx.left_pane_lines(lines, image)
        start, end, continues = wx.saved_group_section_bounds(pane_lines, section_started)
        if start is None:
            frame.unlink(missing_ok=True)
            break
        section_started = True
        left = round(image.width * wx.SAVED_GROUP_DISCOVERY_LEFT_RATIO)
        right = round(image.width * wx.LEFT_PANE[2])
        bottom = image.height if end is None else end
        if bottom > start:
            # Prefer the full-list OCR coordinates.  Enlarging a narrow crop
            # makes avatar pixels and disclosure arrows look like fake names
            # (for example ``WE!``), while the full pass preserves Chinese
            # labels and their row positions.
            for line in pane_lines:
                if not (start < line.top < bottom):
                    continue
                if line.left < round(image.width * 0.11):
                    continue
                name = re.sub(r"^[>vVyY\s]+", "", line.text).strip()
                normalized = open_group.normalize_text(name)
                if normalized and wx.section_kind(name) is None and name not in names:
                    names.append(name)
                    if len(names) >= maximum:
                        break
        frame.unlink(missing_ok=True)
        if len(names) >= maximum or not continues:
            break
        before, _ = wx.crop_left_pane(image)
        wx.scroll_list(window)
        after_path = directory / ".saved-discovery-after.png"
        after, _ = wx.capture_live_window(window, after_path)
        after_path.unlink(missing_ok=True)
        after_pane, _ = wx.crop_left_pane(after)
        if not wx.page_changed(before, after_pane):
            break
    return names[:maximum]


def backup_named_groups(
    window: dict[str, object],
    directory: Path,
    names: tuple[str, ...] | list[str],
    terms: tuple[str, ...],
    mode: str,
    pages: int,
    tesseract: Path,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for index, name in enumerate(names, 1):
        group_dir = recent_mixed_backup.safe_group_directory(directory, index, name)
        try:
            window = group_member_backup.open_named_group(window, name, group_dir, tesseract)
            result = group_member_backup.backup_open_group(
                window, group_dir, terms, mode, pages, tesseract
            )
        except RuntimeError as error:
            result = {"ok": False, "reason": str(error), "pages": []}
            if os.environ.get("WECHAT_GROUP_ERROR_POLICY", "skip") == "stop":
                result["title"] = name
                results.append(result)
                break
        result["title"] = name
        results.append(result)
    return results


def run(args: argparse.Namespace) -> dict[str, object]:
    tasks = parse_tasks(args.tasks)
    group_filters = parse_csv(args.groups)
    terms = group_member_backup.parse_terms(args.terms)
    os.environ["WECHAT_GROUP_ERROR_POLICY"] = args.group_error
    if not tasks:
        raise ValueError("at least one task is required")
    if not terms and ({"recent_groups", "saved_groups"} & set(tasks)):
        raise ValueError("group tasks require at least one member search term")
    if min(args.people_limit, args.group_limit, args.member_pages) < 1:
        raise ValueError("limits must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    # One shared input trace covers recent, contacts, saved groups, and group
    # member subflows.  Low-level click/scroll helpers append to this file.
    os.environ["WECHAT_INPUT_LOG"] = str((args.output / "input-events.jsonl").resolve())
    tesseract = open_group.resolve_tesseract(args.tesseract)
    if tesseract is None:
        raise RuntimeError("tesseract_not_found")
    pid, pid_source = wx.select_pid(args.pid, audit.DEFAULT_PANEL_CONFIG)
    if pid is None:
        raise RuntimeError(f"weixin_pid_not_found:{pid_source}")
    window = audit.select_weixin_window(pid)
    if window is None:
        raise RuntimeError(f"weixin_window_not_found:{pid}")
    activation = audit.activate_window(window)
    if not activation["activated"]:
        raise RuntimeError("weixin_activation_failed")
    window = activation["window"]
    selected_directories = {
        "recent": bool({"recent_people", "recent_groups"} & set(tasks)),
        "contacts": "contacts" in tasks,
        "saved-groups": "saved_groups" in tasks,
    }
    for name, selected in selected_directories.items():
        path = args.output / name
        if selected and path.is_dir():
            shutil.rmtree(path)
    results: dict[str, object] = {}

    recent_tasks = {"recent_people", "recent_groups"} & set(tasks)
    if recent_tasks:
        results["recent"] = recent_mixed_backup.save_recent_mixed(
            window,
            args.output / "recent",
            tesseract,
            include_people="recent_people" in tasks,
            include_groups="recent_groups" in tasks,
            people_limit=args.people_limit if "recent_people" in tasks else 0,
            group_limit=args.group_limit if "recent_groups" in tasks else 0,
            group_keywords=group_filters,
            member_terms=terms,
            member_mode=args.member_mode,
            member_pages=args.member_pages,
        )

    if "contacts" in tasks:
        contacts_dir = args.output / "contacts"
        contacts_dir.mkdir(parents=True, exist_ok=True)
        window = audit.select_weixin_window(pid) or window
        activation = audit.activate_window(window)
        if activation["activated"]:
            window = activation["window"]
        open_group.click_screen_point(wx.sidebar_point(window, wx.CONTACTS_NAV))
        time.sleep(wx.NAVIGATION_WAIT_SECONDS)
        expanded = wx.expand_contacts(window, contacts_dir, tesseract)
        if expanded:
            pages, reason = wx.save_contact_detail_pages(
                window, contacts_dir, args.people_limit, tesseract
            )
            results["contacts"] = {"ok": bool(pages), "pages": pages, "stop_reason": reason}
        else:
            results["contacts"] = {"ok": False, "pages": [], "stop_reason": "contacts_not_found"}

    if "saved_groups" in tasks:
        saved_dir = args.output / "saved-groups"
        saved_dir.mkdir(parents=True, exist_ok=True)
        names: tuple[str, ...] | list[str] = group_filters
        if not names:
            window = audit.select_weixin_window(pid) or window
            activation = audit.activate_window(window)
            if activation["activated"]:
                window = activation["window"]
            names = discover_saved_group_names(
                window, saved_dir, tesseract, args.group_limit
            )
        groups = backup_named_groups(
            window,
            saved_dir / "groups",
            names[: args.group_limit],
            terms,
            args.member_mode,
            args.member_pages,
            tesseract,
        )
        results["saved_groups"] = {
            "ok": any(bool(group.get("ok")) for group in groups),
            "discovered": list(names),
            "groups": groups,
        }

    return {
        "ok": any(bool(value.get("ok")) for value in results.values() if isinstance(value, dict)),
        "tasks": list(tasks),
        "target_pid": pid,
        "pid_source": pid_source,
        "results": results,
    }


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except (RuntimeError, ValueError) as error:
        result = {"ok": False, "reason": str(error), "results": {}}
    args.output.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    (args.output / "result.json").write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
