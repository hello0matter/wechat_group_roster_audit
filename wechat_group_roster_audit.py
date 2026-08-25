"""Read-only, consent-based inspection of the currently open Weixin group pane.

The default mode only reports whether the expected UI controls are present.
Export mode writes display names (群昵称) only; account identifiers are not read
or persisted.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import time
from pathlib import Path
from typing import NamedTuple


def enable_per_monitor_dpi_awareness() -> str:
    """Keep Win32 window rectangles and screenshots in physical pixels."""
    try:
        context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per_monitor"
    except (AttributeError, OSError):
        return "system_default"


DPI_AWARENESS = enable_per_monitor_dpi_awareness()
USER32 = ctypes.windll.user32

import psutil
import win32con
import win32api
import win32gui
import win32process
import win32ui
import pywintypes
from PIL import Image
from PIL import ImageDraw
from PIL import ImageGrab
from PIL import ImageStat
from pywinauto import Desktop
from pywinauto.application import Application
from pywinauto.findwindows import ElementNotFoundError


PROCESS_NAMES = {"WeChat.exe", "Weixin.exe"}
WEIXIN_WINDOW_CLASS = "Qt51514QWindowIcon"
DEFAULT_PANEL = (0.78, 0.14, 0.21, 0.82)
DEFAULT_PANEL_CONFIG = Path("panel_config.json")
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
PW_RENDERFULLCONTENT = 2


class PanelRatios(NamedTuple):
    x: float
    y: float
    width: float
    height: float


class PixelRect(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int


def find_processes() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name"]):
        if process.info["name"] in PROCESS_NAMES:
            found.append(process)
    return found


def effective_window_rect(hwnd: int, minimized: bool) -> PixelRect:
    if minimized:
        placement = win32gui.GetWindowPlacement(hwnd)
        left, top, right, bottom = placement[4]
    else:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return PixelRect(int(left), int(top), int(right), int(bottom))


def visible_weixin_windows(target_pid: int = 0) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    foreground_hwnd = win32gui.GetForegroundWindow()

    def callback(hwnd: int, _: int) -> bool:
        if win32gui.GetClassName(hwnd) != WEIXIN_WINDOW_CLASS:
            return True
        title = win32gui.GetWindowText(hwnd)
        if title not in {"Weixin", "微信"}:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if target_pid and pid != target_pid:
            return True
        try:
            process_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        if process_name != "Weixin.exe":
            return True
        visible = bool(win32gui.IsWindowVisible(hwnd))
        minimized = bool(win32gui.IsIconic(hwnd))
        rect = effective_window_rect(hwnd, minimized)
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 450 or height < 350:
            return True
        windows.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "title": title,
                "class_name": WEIXIN_WINDOW_CLASS,
                "left": rect.left,
                "top": rect.top,
                "width": width,
                "height": height,
                "visible": visible,
                "hidden": not visible,
                "minimized": minimized,
                "foreground": hwnd == foreground_hwnd,
            }
        )
        return True

    win32gui.EnumWindows(callback, 0)
    windows.sort(
        key=lambda item: (
            bool(item["foreground"]),
            int(item["width"]) * int(item["height"]),
        ),
        reverse=True,
    )
    return windows


def select_weixin_window(target_pid: int = 0) -> dict[str, object] | None:
    windows = visible_weixin_windows(target_pid)
    if target_pid or len(windows) == 1:
        return windows[0] if windows else None
    foreground = [window for window in windows if window["foreground"]]
    return foreground[0] if len(foreground) == 1 else None


def explain_window_selection_failure(target_pid: int = 0) -> str:
    windows = visible_weixin_windows(target_pid)
    if target_pid:
        return f"未找到 PID {target_pid} 对应的微信主窗口。"
    if len(windows) > 1:
        choices = ", ".join(str(window["pid"]) for window in windows)
        return f"检测到多个微信主窗口（PID: {choices}），请使用 --target-pid 明确选择。"
    return "未找到符合条件的微信主窗口。"


def activate_window(window: dict[str, object]) -> dict[str, object]:
    if "hwnd" not in window:
        return False
    hwnd = int(window["hwnd"])
    was_hidden = not bool(win32gui.IsWindowVisible(hwnd))
    was_minimized = bool(win32gui.IsIconic(hwnd))
    # A visible foreground WeChat surface is already usable. Touching its Qt
    # window state can blank the content layer, so leave it completely alone.
    if "width" in window and not was_hidden and not was_minimized and win32gui.GetForegroundWindow() == hwnd:
        return {
            "activated": True,
            "was_hidden": False,
            "was_minimized": False,
            "input_threads_attached": False,
            "window": dict(window),
        }
    # For a visible normal WeChat window, activate it by clicking its title bar
    # instead of changing Qt window state. This avoids the white-surface bug.
    if "width" in window and not was_hidden and not was_minimized:
        left, top, right, _ = map(int, win32gui.GetWindowRect(hwnd))
        point = (left + max(20, min((right - left) // 2, 300)), top + 18)
        try:
            win32api.SetCursorPos(point)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.25)
        except (OSError, win32api.error):
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return {
                "activated": True,
                "was_hidden": False,
                "was_minimized": False,
                "input_threads_attached": False,
                "window": dict(window),
            }
    if was_minimized:
        # Qt/Weixin is sensitive to repeated restore/maximize calls. Send one
        # native restore command and leave its previous size/state untouched.
        USER32.PostMessageW(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_RESTORE, 0)
        time.sleep(0.8)
    elif was_hidden:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        time.sleep(0.15)
    foreground_hwnd = win32gui.GetForegroundWindow()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
    foreground_thread = 0
    if foreground_hwnd:
        foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)
    attached = False
    try:
        if foreground_thread and foreground_thread != target_thread:
            attached = bool(USER32.AttachThreadInput(foreground_thread, target_thread, True))
        USER32.BringWindowToTop(hwnd)
        USER32.SetForegroundWindow(hwnd)
        USER32.SetFocus(hwnd)
        time.sleep(0.25)
        foreground_after_raise = win32gui.GetForegroundWindow()
        if foreground_after_raise != hwnd:
            # Windows can reject the first foreground request while another process owns focus.
            USER32.BringWindowToTop(hwnd)
            USER32.SetForegroundWindow(hwnd)
            USER32.SetFocus(hwnd)
            time.sleep(0.15)
            foreground_after_raise = win32gui.GetForegroundWindow()
    finally:
        if attached:
            USER32.AttachThreadInput(foreground_thread, target_thread, False)
    # Re-read the restored rectangle directly. GetWindowPlacement can retain
    # minimized coordinates for a short period and produce a 0x0/11px window.
    try:
        left, top, right, bottom = map(int, win32gui.GetWindowRect(hwnd))
        width, height = right - left, bottom - top
    except win32gui.error:
        width = height = 0
    refreshed = dict(window)
    if width >= 450 and height >= 350:
        refreshed.update(left=left, top=top, width=width, height=height, minimized=False, visible=True)
    else:
        current = visible_weixin_windows(int(window["pid"]))
        if current and ("width" not in current[0] or int(current[0].get("width", 0)) >= 450):
            refreshed = current[0]
    return {
        "activated": foreground_after_raise == hwnd,
        "was_hidden": was_hidden,
        "was_minimized": was_minimized,
        "input_threads_attached": attached,
        "window": refreshed,
    }


def recover_blank_surface(window: dict[str, object]) -> bool:
    """Trigger the same repaint as clicking the taskbar button once.

    Weixin can keep a visible but blank Qt surface after an auxiliary panel
    closes. A single minimize/restore cycle repaints it; no maximize or size
    change is performed.
    """
    if "hwnd" not in window:
        return False
    hwnd = int(window["hwnd"])
    if not win32gui.IsWindow(hwnd):
        return False
    # This mirrors clicking the taskbar button: a real minimize/restore
    # transition followed by activation and a client-area redraw. Qt/Weixin
    # may ignore a single SetForegroundWindow while its surface is blank.
    USER32.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    time.sleep(0.22)
    USER32.PostMessageW(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_RESTORE, 0)
    time.sleep(0.35)
    USER32.ShowWindow(hwnd, win32con.SW_RESTORE)
    USER32.BringWindowToTop(hwnd)
    USER32.SetForegroundWindow(hwnd)
    try:
        win32gui.SendMessage(hwnd, win32con.WM_NCACTIVATE, 1, 0)
        win32gui.RedrawWindow(
            hwnd,
            None,
            None,
            win32con.RDW_INVALIDATE
            | win32con.RDW_ERASE
            | win32con.RDW_UPDATENOW
            | win32con.RDW_ALLCHILDREN,
        )
    except win32gui.error:
        pass
    time.sleep(0.65)
    return win32gui.GetForegroundWindow() == hwnd


def virtual_desktop_rect() -> PixelRect:
    left = int(USER32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(USER32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(USER32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(USER32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return PixelRect(left, top, left + width, top + height)


def window_pixel_rect(window: dict[str, object]) -> PixelRect:
    left = int(window["left"])
    top = int(window["top"])
    return PixelRect(
        left,
        top,
        left + int(window["width"]),
        top + int(window["height"]),
    )


def panel_pixel_rect(window: dict[str, object], panel: PanelRatios) -> PixelRect:
    left = int(window["left"]) + round(int(window["width"]) * panel.x)
    top = int(window["top"]) + round(int(window["height"]) * panel.y)
    width = round(int(window["width"]) * panel.width)
    height = round(int(window["height"]) * panel.height)
    return PixelRect(left, top, left + width, top + height)


def rect_overflow(rect: PixelRect, bounds: PixelRect) -> dict[str, int]:
    return {
        "left": max(0, bounds.left - rect.left),
        "top": max(0, bounds.top - rect.top),
        "right": max(0, rect.right - bounds.right),
        "bottom": max(0, rect.bottom - bounds.bottom),
    }


def capture_visibility(
    window: dict[str, object],
    panel: PanelRatios,
) -> dict[str, object]:
    desktop = virtual_desktop_rect()
    window_rect = window_pixel_rect(window)
    panel_rect = panel_pixel_rect(window, panel)
    window_overflow = rect_overflow(window_rect, desktop)
    panel_overflow = rect_overflow(panel_rect, desktop)
    return {
        "fully_visible": not any(window_overflow.values()) and not any(panel_overflow.values()),
        "virtual_desktop": desktop._asdict(),
        "window_rect": window_rect._asdict(),
        "window_overflow": window_overflow,
        "panel_rect": panel_rect._asdict(),
        "panel_overflow": panel_overflow,
    }


def render_window_image(window: dict[str, object]) -> tuple[Image.Image | None, dict[str, object]]:
    hwnd = int(window["hwnd"])
    expected_size = (int(window["width"]), int(window["height"]))
    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, *expected_size)
        memory_dc.SelectObject(bitmap)
        rendered = bool(USER32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), PW_RENDERFULLCONTENT))
        info = bitmap.GetInfo()
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bitmap.GetBitmapBits(True),
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
        stddev = [round(value, 2) for value in ImageStat.Stat(image).stddev]
        metadata = {
            "source": "print_window",
            "rendered": rendered,
            "nonempty": any(value > 1 for value in stddev),
            "expected_size": list(expected_size),
            "actual_size": list(image.size),
            "stddev": stddev,
        }
        if not metadata["rendered"] or not metadata["nonempty"] or image.size != expected_size:
            return None, metadata
        return image, metadata
    except Exception as error:
        return None, {
            "source": "print_window",
            "rendered": False,
            "nonempty": False,
            "expected_size": list(expected_size),
            "error": str(error),
        }
    finally:
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if memory_dc is not None:
            memory_dc.DeleteDC()
        if source_dc is not None:
            source_dc.DeleteDC()
        if window_dc:
            win32gui.ReleaseDC(hwnd, window_dc)


def capture_window_image(
    window: dict[str, object],
    source: str,
) -> tuple[Image.Image, dict[str, object]]:
    print_window_failure = None
    if source in {"auto", "window"}:
        image, metadata = render_window_image(window)
        if image is not None:
            return image, metadata
        if source == "window":
            raise RuntimeError(json.dumps(metadata, ensure_ascii=False))
        print_window_failure = metadata

    rect = window_pixel_rect(window)
    overflow = rect_overflow(rect, virtual_desktop_rect())
    if any(overflow.values()):
        details = {
            "reason": "desktop_fallback_would_be_incomplete",
            "window_overflow": overflow,
        }
        if print_window_failure is not None:
            details["print_window_failure"] = print_window_failure
        raise RuntimeError(json.dumps(details, ensure_ascii=False))
    image = ImageGrab.grab(bbox=tuple(rect), all_screens=True).convert("RGB")
    nonempty = any(value > 1 for value in ImageStat.Stat(image).stddev)
    metadata = {
        "source": "desktop_capture",
        "rendered": True,
        "nonempty": nonempty,
        "actual_size": list(image.size),
    }
    if print_window_failure is not None:
        metadata["print_window_failure"] = print_window_failure
    if not nonempty:
        raise RuntimeError(json.dumps(metadata, ensure_ascii=False))
    return image, metadata


def capture_panel(
    window: dict[str, object],
    output: Path,
    panel: PanelRatios,
    source: str,
) -> dict[str, object]:
    window_image, source_metadata = capture_window_image(window, source)
    panel_box = (
        round(int(window["width"]) * panel.x),
        round(int(window["height"]) * panel.y),
        round(int(window["width"]) * (panel.x + panel.width)),
        round(int(window["height"]) * (panel.y + panel.height)),
    )
    image = window_image.crop(panel_box)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    rect = panel_pixel_rect(window, panel)
    return {
        "output": str(output.resolve()),
        "coordinate_space": "physical_pixels",
        "dpi_awareness": DPI_AWARENESS,
        "pixel_bbox": list(rect),
        "window_panel_box": list(panel_box),
        "image_size": list(image.size),
        "image_source": source_metadata,
    }


def preview_panel(
    window: dict[str, object],
    output: Path,
    panel: PanelRatios,
    source: str,
) -> dict[str, object]:
    width = int(window["width"])
    height = int(window["height"])
    image, source_metadata = capture_window_image(window, source)
    panel_box = (
        round(width * panel.x),
        round(height * panel.y),
        round(width * (panel.x + panel.width)),
        round(height * (panel.y + panel.height)),
    )
    draw = ImageDraw.Draw(image)
    line_width = max(3, round(min(width, height) * 0.004))
    draw.rectangle(panel_box, outline=(255, 0, 0), width=line_width)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return {
        "output": str(output.resolve()),
        "coordinate_space": "window_physical_pixels",
        "dpi_awareness": DPI_AWARENESS,
        "panel_box": list(panel_box),
        "image_size": list(image.size),
        "image_source": source_metadata,
    }


def calibrate_cursor(window: dict[str, object]) -> dict[str, object]:
    cursor_x, cursor_y = win32gui.GetCursorPos()
    width = int(window["width"])
    height = int(window["height"])
    ratio_x = (cursor_x - int(window["left"])) / width
    ratio_y = (cursor_y - int(window["top"])) / height
    return {
        "inside_window": 0 <= ratio_x <= 1 and 0 <= ratio_y <= 1,
        "cursor": [cursor_x, cursor_y],
        "ratio": [round(ratio_x, 4), round(ratio_y, 4)],
        "window": window,
    }


def panel_from_points(
    window: dict[str, object],
    top_left: tuple[int, int],
    bottom_right: tuple[int, int],
) -> PanelRatios:
    left = int(window["left"])
    top = int(window["top"])
    width = int(window["width"])
    height = int(window["height"])
    return PanelRatios(
        (top_left[0] - left) / width,
        (top_left[1] - top) / height,
        (bottom_right[0] - top_left[0]) / width,
        (bottom_right[1] - top_left[1]) / height,
    )


def save_panel_config(
    path: Path,
    window: dict[str, object],
    panel: PanelRatios,
) -> dict[str, object]:
    payload = {
        "version": 1,
        "target_pid": int(window["pid"]),
        "panel": {
            "x": round(panel.x, 6),
            "y": round(panel.y, 6),
            "width": round(panel.width, 6),
            "height": round(panel.height, 6),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_panel_config(
    path: Path,
    target_pid: int = 0,
) -> tuple[PanelRatios | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        config_pid = int(payload["target_pid"])
        if target_pid and config_pid != target_pid:
            return None, f"pid_mismatch:{config_pid}"
        panel = payload["panel"]
        result = PanelRatios(
            float(panel["x"]),
            float(panel["y"]),
            float(panel["width"]),
            float(panel["height"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None, "invalid"
    if not valid_panel(result):
        return None, "invalid"
    return result, "matched"


def load_panel_config(path: Path, target_pid: int = 0) -> PanelRatios | None:
    panel, _ = read_panel_config(path, target_pid)
    return panel


def calibrate_panel(
    window: dict[str, object],
    path: Path,
    delay: float,
) -> dict[str, object]:
    print(f"请在 {delay:g} 秒内把鼠标移到右侧面板左上角。", flush=True)
    time.sleep(delay)
    top_left = win32gui.GetCursorPos()
    print(f"已记录左上角 {top_left}；请在 {delay:g} 秒内移到右下角。", flush=True)
    time.sleep(delay)
    bottom_right = win32gui.GetCursorPos()
    panel = panel_from_points(window, top_left, bottom_right)
    if not valid_panel(panel):
        return {
            "calibrated": False,
            "reason": "calibration_points_outside_window_or_reversed",
            "top_left": list(top_left),
            "bottom_right": list(bottom_right),
        }
    config = save_panel_config(path, window, panel)
    return {
        "calibrated": True,
        "config_path": str(path.resolve()),
        "top_left": list(top_left),
        "bottom_right": list(bottom_right),
        "config": config,
    }


def valid_panel(panel: PanelRatios) -> bool:
    return (
        all(0 <= value <= 1 for value in panel)
        and panel.x + panel.width <= 1
        and panel.y + panel.height <= 1
        and panel.width > 0
        and panel.height > 0
    )


def connect_to_main_window(target_pid: int = 0) -> tuple[Application, object] | tuple[None, None]:
    # New Weixin 4.x keeps the top-level HWND as Qt51514QWindowIcon and
    # exposes the actual mmui::MainWindow only after attaching by HWND.
    visible_windows = visible_weixin_windows(target_pid)
    if not target_pid and len(visible_windows) > 1:
        return None, None
    selected = select_weixin_window(target_pid)
    if selected:
        window = Desktop(backend="uia").window(handle=int(selected["hwnd"]))
        if window.exists(timeout=1):
            return None, window
    for process in find_processes():
        if target_pid and process.pid != target_pid:
            continue
        try:
            app = Application(backend="uia").connect(process=process.pid)
            candidates = [
                app.window(class_name="WeChatMainWndForPC"),
                app.window(title="Weixin", class_name="Qt51514QWindowIcon"),
            ]
            for window in candidates:
                if window.exists(timeout=1):
                    return app, window
        except (ElementNotFoundError, RuntimeError):
            continue
    return None, None


def read_group_display_names(window: object) -> list[str]:
    member_list = window.child_window(control_type="List", title="聊天成员")
    names: list[str] = []
    for item in member_list.items():
        descendants = item.descendants()
        if len(descendants) <= 5:
            continue
        texts = descendants[5].texts()
        if not texts:
            continue
        name = texts[0].strip()
        if name and name not in {"添加", "移出"}:
            names.append(name)
    return list(dict.fromkeys(names))


def describe_ui(window: object) -> None:
    """Print structural diagnostics without dumping visible user data."""
    pairs = set()
    for element in window.descendants():
        try:
            info = element.element_info
            pairs.add((info.control_type or "", info.class_name or ""))
        except Exception:
            continue
    print("当前 UIA 控件类型/类名:")
    for control_type, class_name in sorted(pairs):
        print(f"  {control_type or '-'} / {class_name or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the open Weixin group member pane")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--export-nicknames", action="store_true", help="write 群昵称 only")
    parser.add_argument("--output", type=Path, default=Path("group_nicknames.csv"))
    parser.add_argument("--target-pid", type=int, default=0, help="select a specific Weixin.exe PID")
    actions.add_argument("--list-windows", action="store_true", help="list Weixin main windows and exit")
    actions.add_argument("--activate-window", action="store_true", help="activate the selected Weixin window")
    actions.add_argument("--calibrate-cursor", action="store_true", help="print cursor ratios within the selected window")
    actions.add_argument("--calibrate-panel", action="store_true", help="record panel corners and save their ratios")
    parser.add_argument("--panel-config", type=Path, default=DEFAULT_PANEL_CONFIG)
    parser.add_argument("--calibration-delay", type=float, default=5.0)
    actions.add_argument("--capture-panel", type=Path, help="capture the currently visible right panel once")
    actions.add_argument("--preview-panel", type=Path, help="save a window screenshot with the panel rectangle")
    parser.add_argument(
        "--activate-before-capture",
        action="store_true",
        help="activate the selected window before capture/preview",
    )
    parser.add_argument(
        "--capture-source",
        choices=("auto", "window", "screen"),
        default="auto",
        help="auto prefers PrintWindow and falls back to desktop capture",
    )
    parser.add_argument(
        "--panel",
        type=float,
        nargs=4,
        metavar=("X", "Y", "WIDTH", "HEIGHT"),
        default=None,
        help="right-panel rectangle as window-relative ratios",
    )
    args = parser.parse_args()

    if args.list_windows:
        print(json.dumps(visible_weixin_windows(args.target_pid), ensure_ascii=False, indent=2))
        return 0

    selected = select_weixin_window(args.target_pid)
    if args.activate_window:
        if selected is None:
            print(explain_window_selection_failure(args.target_pid))
            return 2
        result = activate_window(selected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["activated"] else 2

    if args.calibrate_cursor:
        if selected is None:
            print(explain_window_selection_failure(args.target_pid))
            return 2
        result = calibrate_cursor(selected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["inside_window"] else 2

    if args.calibrate_panel:
        if selected is None:
            print(explain_window_selection_failure(args.target_pid))
            return 2
        if args.calibration_delay < 1 or args.calibration_delay > 30:
            print("--calibration-delay 必须在 1 到 30 秒之间。")
            return 2
        result = calibrate_panel(selected, args.panel_config, args.calibration_delay)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["calibrated"] else 2

    if args.capture_panel or args.preview_panel:
        if selected is None:
            print(explain_window_selection_failure(args.target_pid))
            return 2
        activation = None
        if args.activate_before_capture:
            activation = activate_window(selected)
            if not activation["activated"]:
                print(json.dumps({"activation": activation}, ensure_ascii=False, indent=2))
                return 2
            selected = activation["window"]
        elif selected.get("minimized") or selected.get("hidden"):
            print(
                "目标微信窗口当前已最小化或缩到托盘。请添加 --activate-before-capture，"
                "或先运行 --activate-window。"
            )
            return 2
        configured_panel, config_status = read_panel_config(
            args.panel_config,
            int(selected["pid"]),
        )
        if args.panel is not None:
            panel = PanelRatios(*args.panel)
            panel_source = "command_line"
        elif configured_panel is not None:
            panel = configured_panel
            panel_source = str(args.panel_config.resolve())
        elif config_status.startswith("pid_mismatch:"):
            config_pid = config_status.partition(":")[2]
            print(
                f"{args.panel_config} 属于微信 PID {config_pid}，当前目标是 PID "
                f"{selected['pid']}。请重新运行 --calibrate-panel，或显式传入 --panel。"
            )
            return 2
        elif config_status == "invalid":
            print(
                f"{args.panel_config} 已损坏或面板范围无效。"
                "请删除后重新运行 --calibrate-panel，或显式传入 --panel。"
            )
            return 2
        else:
            panel = PanelRatios(*DEFAULT_PANEL)
            panel_source = "built_in_default"
        if not valid_panel(panel):
            print("--panel 必须是 0 到 1 的窗口相对比例，且不能超出窗口。")
            return 2
        visibility = capture_visibility(selected, panel)
        if args.capture_source == "screen" and not visibility["fully_visible"]:
            print(
                json.dumps(
                    {
                        "captured": False,
                        "reason": "desktop_capture_outside_virtual_desktop",
                        "visibility": visibility,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        try:
            if args.preview_panel:
                result = preview_panel(
                    selected,
                    args.preview_panel,
                    panel,
                    args.capture_source,
                )
                action = "preview"
            else:
                result = capture_panel(
                    selected,
                    args.capture_panel,
                    panel,
                    args.capture_source,
                )
                action = "capture"
        except RuntimeError as error:
            print(f"截图失败: {error}")
            return 2
        payload = {
            "window": selected,
            "panel": panel._asdict(),
            "panel_source": panel_source,
            "visibility": visibility,
            action: result,
        }
        if activation is not None:
            payload["activation"] = activation
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    processes = find_processes()
    if not processes:
        print("未找到 Weixin/WeChat 进程，请先打开已登录的微信。")
        return 1
    print("发现进程:", ", ".join(f"{p.info['name']}({p.pid})" for p in processes))
    print("请在微信中打开：目标群聊 -> 聊天成员 -> 查看更多。")
    print("等待 5 秒后进行只读控件探测……")
    time.sleep(5)

    _, window = connect_to_main_window(args.target_pid)
    if window is None:
        print(explain_window_selection_failure(args.target_pid))
        return 2
    try:
        names = read_group_display_names(window)
    except ElementNotFoundError:
        print("未找到“聊天成员”列表。请确认已打开“查看更多”。")
        describe_ui(window)
        return 3

    print(f"控件探测成功，可见群昵称数量：{len(names)}")
    if not args.export_nicknames:
        print("默认不导出。需要保存群昵称时，请显式添加 --export-nicknames。")
        return 0

    with args.output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["群昵称"])
        writer.writerows([[name] for name in names])
    print(f"已写入 {args.output}（仅群昵称，不包含微信号/WXID/手机号）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
