"""Open one explicitly named group chat in an already logged-in Weixin window."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from datetime import datetime, timezone
from collections import defaultdict
from ctypes import wintypes
from pathlib import Path
from typing import NamedTuple

import win32api
import win32con
import win32gui
import win32process
from PIL import ImageGrab

import quick_capture
import wechat_group_roster_audit as audit


DEFAULT_TESSERACT = Path(r"D:\Program Files\Tesseract-OCR\tesseract.exe")
SEARCH_X_RATIO = 175 / 1342
SEARCH_Y_RATIO = 84 / 1192


class OcrLine(NamedTuple):
    text: str
    left: int
    top: int
    right: int
    bottom: int


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", INPUT_UNION)]


def normalize_text(value: str) -> str:
    return "".join(value.split()).casefold()


def parse_tsv_lines(tsv: str) -> list[OcrLine]:
    grouped: dict[tuple[int, int, int, int], list[tuple[int, str, int, int, int, int]]] = (
        defaultdict(list)
    )
    rows = tsv.splitlines()
    if not rows:
        return []
    headers = rows[0].split("\t")
    for raw_row in rows[1:]:
        values = raw_row.split("\t", len(headers) - 1)
        if len(values) != len(headers):
            continue
        row = dict(zip(headers, values, strict=True))
        text = row.get("text", "").strip()
        if not text or row.get("level") != "5":
            continue
        try:
            key = tuple(int(row[name]) for name in ("page_num", "block_num", "par_num", "line_num"))
            left = int(row["left"])
            top = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            word_num = int(row["word_num"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped[key].append((word_num, text, left, top, left + width, top + height))

    lines = []
    for words in grouped.values():
        words.sort()
        lines.append(
            OcrLine(
                " ".join(word[1] for word in words),
                min(word[2] for word in words),
                min(word[3] for word in words),
                max(word[4] for word in words),
                max(word[5] for word in words),
            )
        )
    return sorted(lines, key=lambda line: (line.top, line.left))


def find_group_result(lines: list[OcrLine], group_name: str) -> OcrLine | None:
    expected = normalize_text(group_name)
    safe_headings = [
        line
        for line in lines
        if normalize_text(line.text) == "mostused"
        or (
            "group" in normalize_text(line.text)
            and "chat" in normalize_text(line.text)
        )
    ]
    section_headings = [
        line
        for line in lines
        if normalize_text(line.text) == "mostused"
        or "chathistory" in normalize_text(line.text)
        or "internetsearchresults" in normalize_text(line.text)
        or normalize_text(line.text) == "contacts"
        or "serviceaccounts" in normalize_text(line.text)
        or (
            "group" in normalize_text(line.text)
            and "chat" in normalize_text(line.text)
        )
    ]

    for heading in safe_headings:
        end_top = min(
            (line.top for line in section_headings if line.top > heading.bottom),
            default=float("inf"),
        )
        candidates = [
            line
            for line in lines
            if line.top > heading.bottom
            and line.top < end_top
            and expected in normalize_text(line.text)
        ]
        if candidates:
            return min(candidates, key=lambda line: (line.top, line.left))
    return None


def resolve_tesseract(value: Path | None) -> Path | None:
    if value is not None:
        return value if value.is_file() else None
    portable = Path(sys.executable).resolve().parent / "tesseract" / "tesseract.exe"
    if portable.is_file():
        return portable
    if DEFAULT_TESSERACT.is_file():
        return DEFAULT_TESSERACT
    executable = shutil.which("tesseract")
    return Path(executable) if executable else None


def run_ocr(
    tesseract: Path,
    image: Path,
    psm: int = 11,
    language: str = "chi_sim+eng",
) -> list[OcrLine]:
    if os.environ.get("WECHAT_OCR_BACKEND", "tesseract").lower() == "paddle":
        return run_paddle_ocr(image)
    command = [
        str(tesseract),
        str(image),
        "stdout",
        "-l",
        language,
        "--psm",
        str(psm),
        "tsv",
    ]
    for attempt in range(2):
        ocr_env = os.environ.copy()
        # Tesseract on Windows otherwise inherits a legacy ANSI code page and
        # replaces Chinese TSV text before it reaches Python.
        ocr_env["LC_ALL"] = "C.UTF-8"
        ocr_env["LANG"] = "C.UTF-8"
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=ocr_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            raw_stdout = result.stdout
            if isinstance(raw_stdout, bytes):
                # Tesseract follows the active Windows code page on some
                # installations and UTF-8 on others.  Replacement decoding
                # made Chinese group names impossible to match.
                try:
                    raw_stdout = raw_stdout.decode("utf-8")
                except UnicodeDecodeError:
                    raw_stdout = raw_stdout.decode("gb18030", errors="replace")
            return parse_tsv_lines(raw_stdout)
        if attempt == 0:
            time.sleep(0.25)
    raw_stderr = result.stderr
    if isinstance(raw_stderr, bytes):
        raw_stderr = raw_stderr.decode("utf-8", errors="replace")
    raise RuntimeError(raw_stderr.strip() or f"tesseract exited {result.returncode}")


@lru_cache(maxsize=1)
def _paddle_engine():
    from paddleocr import PaddleOCR

    model_size = os.environ.get("WECHAT_PADDLE_MODEL", "server").lower()
    model_kwargs = {}
    if model_size == "mobile":
        model_kwargs.update(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
        )
    return PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        **model_kwargs,
    )


def run_paddle_ocr(image: Path) -> list[OcrLine]:
    """Run PaddleOCR and normalize its polygon result to the local OCR type."""
    output: list[OcrLine] = []
    for result in _paddle_engine().predict(str(image)):
        texts = result.get("rec_texts", [])
        boxes = result.get("rec_boxes", [])
        scores = result.get("rec_scores", [])
        for text, box, score in zip(texts, boxes, scores):
            if not str(text).strip() or float(score) < 0.2:
                continue
            coordinates = [int(value) for value in box]
            output.append(OcrLine(str(text), min(coordinates[0::2]), min(coordinates[1::2]), max(coordinates[0::2]), max(coordinates[1::2])))
    return output


def gui_thread_handles() -> tuple[int, int, int]:
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not audit.USER32.GetGUIThreadInfo(0, ctypes.byref(info)):
        raise ctypes.WinError()
    return int(info.hwndActive or 0), int(info.hwndFocus or 0), int(info.hwndCaret or 0)


def _input_log_path() -> Path | None:
    value = os.environ.get("WECHAT_INPUT_LOG", "").strip()
    if not value:
        return None
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log_input(action: str, *, point: tuple[int, int], delta: int | None = None) -> None:
    """Append a best-effort audit record for every synthetic mouse action."""
    path = _input_log_path()
    if path is None:
        return
    try:
        cursor = tuple(map(int, win32gui.GetCursorPos()))
    except (OSError, win32gui.error):
        cursor = None
    try:
        foreground = int(win32gui.GetForegroundWindow() or 0)
        _, pid = win32process.GetWindowThreadProcessId(foreground) if foreground else (0, 0)
    except (OSError, win32gui.error):
        foreground, pid = 0, 0
    event: dict[str, object] = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "action": action,
        "point": [int(point[0]), int(point[1])],
        "cursor_before": list(cursor) if cursor else None,
        "foreground_hwnd": foreground,
        "foreground_pid": int(pid),
    }
    if delta is not None:
        event["delta"] = int(delta)
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def wait_if_paused() -> None:
    """Block before every low-level input while the GUI pause marker exists."""
    pause_path = os.environ.get("WECHAT_PAUSE_FILE")
    if not pause_path:
        return
    stop_path = os.environ.get("WECHAT_STOP_FILE")
    pause_file = Path(pause_path)
    while pause_file.exists():
        if stop_path and Path(stop_path).exists():
            return
        time.sleep(0.08)


def click_screen_point(point: tuple[int, int]) -> None:
    """Click through SendInput; current Weixin ignores legacy mouse_event clicks."""
    wait_if_paused()
    x, y = map(int, point)
    _log_input("click", point=(x, y))
    desktop = audit.virtual_desktop_rect()
    width = max(1, desktop.right - desktop.left - 1)
    height = max(1, desktop.bottom - desktop.top - 1)
    events = (INPUT * 3)(
        INPUT(
            type=0,
            mi=MOUSEINPUT(
                round((x - desktop.left) * 65535 / width),
                round((y - desktop.top) * 65535 / height),
                0,
                0x0001 | 0x4000 | 0x8000,
                0,
                0,
            ),
        ),
        INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, 0x0002, 0, 0)),
        INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, 0x0004, 0, 0)),
    )
    if audit.USER32.SendInput(3, events, ctypes.sizeof(INPUT)) != 3:
        raise ctypes.WinError()
    time.sleep(max(0.0, float(os.environ.get("WECHAT_CLICK_DELAY", "0.06"))))


def scroll_screen_point(point: tuple[int, int], delta: int) -> None:
    """Move to a list and send a modern wheel input event.

    Newer Qt Weixin builds accept SendInput clicks but ignore the legacy
    ``mouse_event`` wheel message. Keeping the move and wheel in one input
    batch also prevents the pointer from briefly landing on a contact row.
    """
    wait_if_paused()
    x, y = map(int, point)
    _log_input("scroll", point=(x, y), delta=delta)
    desktop = audit.virtual_desktop_rect()
    width = max(1, desktop.right - desktop.left - 1)
    height = max(1, desktop.bottom - desktop.top - 1)
    absolute_move = MOUSEINPUT(
        round((x - desktop.left) * 65535 / width),
        round((y - desktop.top) * 65535 / height),
        0,
        0x0001 | 0x4000 | 0x8000,
        0,
        0,
    )
    wheel = MOUSEINPUT(0, 0, ctypes.c_ulong(int(delta)).value, 0x0800, 0, 0)
    events = (INPUT * 2)(INPUT(type=0, mi=absolute_move), INPUT(type=0, mi=wheel))
    if audit.USER32.SendInput(2, events, ctypes.sizeof(INPUT)) != 2:
        raise ctypes.WinError()


def send_trusted_keys(
    window: dict[str, object], key_positions: list[tuple[float, float]]
) -> None:
    """Send keys through Windows' signed UIAccess on-screen keyboard."""
    osk = win32gui.FindWindow("OSKMainClass", None)
    launched = not bool(osk)
    if launched:
        win32api.ShellExecute(
            0,
            "open",
            r"C:\Windows\System32\osk.exe",
            None,
            None,
            win32con.SW_SHOWNORMAL,
        )
        for _attempt in range(30):
            osk = win32gui.FindWindow("OSKMainClass", None)
            if osk and win32gui.IsWindowVisible(osk):
                break
            time.sleep(0.1)
    if not osk or not win32gui.IsWindowVisible(osk):
        raise RuntimeError("trusted_search_focus_unavailable")
    activation = audit.activate_window(window)
    if not activation["activated"]:
        raise RuntimeError("weixin_activation_failed")
    left, top, right, bottom = win32gui.GetWindowRect(osk)
    width = right - left
    height = bottom - top
    try:
        for x_ratio, y_ratio in key_positions:
            click_screen_point(
                (left + round(width * x_ratio), top + round(height * y_ratio))
            )
    finally:
        if launched:
            try:
                win32gui.PostMessage(osk, win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
    time.sleep(0.25)


def focus_global_search(window: dict[str, object]) -> None:
    """Focus the Weixin search box without opening the on-screen keyboard."""
    point = (
        int(window["left"]) + round(int(window["width"]) * SEARCH_X_RATIO),
        int(window["top"]) + round(int(window["height"]) * SEARCH_Y_RATIO),
    )
    click_screen_point(point)
    time.sleep(0.2)


def open_search_result_with_keyboard(
    window: dict[str, object], selection_index: int
) -> None:
    """Choose a focused Weixin search result using native key events."""
    activation = audit.activate_window(window)
    if not activation["activated"]:
        raise RuntimeError("weixin_activation_failed")
    for _ in range(max(0, selection_index)):
        win32api.keybd_event(win32con.VK_DOWN, 0, 0, 0)
        win32api.keybd_event(win32con.VK_DOWN, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.04)
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)


def set_cursor_pos(point: tuple[int, int]) -> None:
    """Move the cursor with a Win32 fallback for pywin32 SetCursorPos failures."""
    wait_if_paused()
    x, y = map(int, point)
    try:
        win32api.SetCursorPos((x, y))
        return
    except (OSError, win32api.error):
        pass
    if not ctypes.windll.user32.SetCursorPos(x, y):
        # SendInput works in sessions where SetCursorPos is denied by the UI broker.
        width = max(1, ctypes.windll.user32.GetSystemMetrics(0) - 1)
        height = max(1, ctypes.windll.user32.GetSystemMetrics(1) - 1)
        move = MOUSEINPUT(round(x * 65535 / width), round(y * 65535 / height), 0, 0x0001 | 0x8000, 0, 0)
        if ctypes.windll.user32.SendInput(1, ctypes.byref(INPUT(type=0, mi=move)), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError()


def select_all() -> None:
    wait_if_paused()
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def press_escape() -> None:
    """Dismiss a contact detail pane without navigating the list."""
    wait_if_paused()
    win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
    win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)


def send_unicode_text(value: str) -> None:
    wait_if_paused()
    code_units = value.encode("utf-16-le")
    inputs = []
    for offset in range(0, len(code_units), 2):
        code_unit = int.from_bytes(code_units[offset : offset + 2], "little")
        inputs.extend(
            [
                INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004, 0, 0)),
                INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004 | 0x0002, 0, 0)),
            ]
        )
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = audit.USER32.SendInput(len(array), array, ctypes.sizeof(INPUT))
    if sent != len(array):
        raise ctypes.WinError()


def desktop_window_capture(window: dict[str, object], output: Path) -> None:
    # A stale dictionary can contain a tiny taskbar/transition rectangle after
    # Weixin is restored. Refresh it from the real HWND before validating or
    # capturing; mutate in place so coordinate mapping stays consistent.
    try:
        left, top, right, bottom = map(int, win32gui.GetWindowRect(int(window["hwnd"])))
        if right - left >= 450 and bottom - top >= 350:
            window.update({"left": left, "top": top, "width": right - left, "height": bottom - top})
    except (KeyError, OSError, win32gui.error, TypeError, ValueError):
        pass
    visibility = audit.capture_visibility(window, audit.PanelRatios(0, 0, 1, 1))
    if not visibility["fully_visible"]:
        raise RuntimeError(
            "微信窗口未完全位于屏幕内，无法可靠识别搜索分类："
            + json.dumps(visibility["window_overflow"], ensure_ascii=False)
        )
    rect = audit.window_pixel_rect(window)
    output.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=tuple(rect), all_screens=True).save(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Open one named Weixin group chat")
    parser.add_argument("group_name", help="exact group name to open")
    parser.add_argument("--config", type=Path, default=audit.DEFAULT_PANEL_CONFIG)
    parser.add_argument("--tesseract", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()

    tesseract = resolve_tesseract(args.tesseract)
    if tesseract is None:
        print("未找到 Tesseract OCR。请安装或通过 --tesseract 指定路径。")
        return 2
    pid, status = quick_capture.configured_pid(args.config)
    if pid is None:
        print(f"无法从 {args.config} 读取目标微信 PID（{status}）。")
        return 2
    window = audit.select_weixin_window(pid)
    if window is None:
        print(f"配置中的微信 PID {pid} 已不存在，请重新选择窗口并校准。")
        return 2

    activation = audit.activate_window(window)
    if not activation["activated"]:
        print(json.dumps({"activation": activation}, ensure_ascii=False, indent=2))
        return 2
    window = activation["window"]
    hwnd = int(window["hwnd"])
    search_point = (
        int(window["left"]) + round(int(window["width"]) * SEARCH_X_RATIO),
        int(window["top"]) + round(int(window["height"]) * SEARCH_Y_RATIO),
    )
    click_screen_point(search_point)
    time.sleep(0.35)
    active, focus, caret = gui_thread_handles()
    if (active, focus, caret) != (hwnd, hwnd, hwnd):
        print(
            json.dumps(
                {
                    "opened": False,
                    "reason": "global_search_did_not_receive_caret",
                    "handles": {"active": active, "focus": focus, "caret": caret},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    select_all()
    time.sleep(0.1)
    send_unicode_text(args.group_name)
    time.sleep(1.5)
    results_image = args.output_dir / "group-search-results.png"
    try:
        desktop_window_capture(window, results_image)
        lines = run_ocr(tesseract, results_image)
    except RuntimeError as error:
        print(f"群搜索验证失败: {error}")
        return 2
    match = find_group_result(lines, args.group_name)
    if match is None:
        print(
            json.dumps(
                {
                    "opened": False,
                    "reason": "exact_group_not_found_in_group_chats",
                    "query": args.group_name,
                    "search_results": str(results_image.resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3

    result_point = (
        int(window["left"]) + (match.left + match.right) // 2,
        int(window["top"]) + (match.top + match.bottom) // 2,
    )
    click_screen_point(result_point)
    time.sleep(1.2)
    opened_image = args.output_dir / "opened-group.png"
    desktop_window_capture(window, opened_image)
    opened_lines = run_ocr(tesseract, opened_image)
    title_verified = any(
        line.top < round(int(window["height"]) * 0.12)
        and line.left > round(int(window["width"]) * 0.3)
        and normalize_text(args.group_name) in normalize_text(line.text)
        for line in opened_lines
    )
    print(
        json.dumps(
            {
                "opened": title_verified,
                "target_pid": pid,
                "group_name": args.group_name,
                "result_line": match._asdict(),
                "result_click": list(result_point),
                "search_results": str(results_image.resolve()),
                "opened_group": str(opened_image.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if title_verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
