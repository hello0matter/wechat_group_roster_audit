import json
import unittest
import ctypes
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import wechat_group_roster_audit as audit
import quick_capture
import open_group
import wx


class PanelRatiosTests(unittest.TestCase):
    def test_accepts_panel_inside_window(self):
        self.assertTrue(audit.valid_panel(audit.PanelRatios(0.78, 0.14, 0.21, 0.82)))

    def test_rejects_panel_outside_window(self):
        self.assertFalse(audit.valid_panel(audit.PanelRatios(0.9, 0.2, 0.2, 0.7)))

    def test_rejects_empty_panel(self):
        self.assertFalse(audit.valid_panel(audit.PanelRatios(0.5, 0.5, 0, 0.4)))

    def test_reports_window_and_panel_overflow(self):
        window = {"left": 900, "top": 100, "width": 300, "height": 400}
        panel = audit.PanelRatios(0.8, 0.1, 0.2, 0.8)
        with patch.object(
            audit,
            "virtual_desktop_rect",
            return_value=audit.PixelRect(0, 0, 1000, 800),
        ):
            result = audit.capture_visibility(window, panel)
        self.assertFalse(result["fully_visible"])
        self.assertEqual(result["window_overflow"], {"left": 0, "top": 0, "right": 200, "bottom": 0})
        self.assertEqual(result["panel_overflow"], {"left": 0, "top": 0, "right": 200, "bottom": 0})

    def test_accepts_capture_fully_inside_virtual_desktop(self):
        window = {"left": 100, "top": 100, "width": 600, "height": 500}
        panel = audit.PanelRatios(0.7, 0.1, 0.25, 0.8)
        with patch.object(
            audit,
            "virtual_desktop_rect",
            return_value=audit.PixelRect(0, 0, 1000, 800),
        ):
            result = audit.capture_visibility(window, panel)
        self.assertTrue(result["fully_visible"])


class WindowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.windows = [
            {"pid": 100, "foreground": False, "width": 1000, "height": 800},
            {"pid": 200, "foreground": False, "width": 900, "height": 700},
        ]

    @patch.object(audit, "visible_weixin_windows")
    def test_requires_pid_for_multiple_background_windows(self, visible_windows):
        visible_windows.return_value = self.windows
        self.assertIsNone(audit.select_weixin_window())

    @patch.object(audit, "visible_weixin_windows")
    def test_selects_explicit_pid_result(self, visible_windows):
        visible_windows.return_value = [self.windows[1]]
        self.assertEqual(audit.select_weixin_window(200), self.windows[1])
        visible_windows.assert_called_once_with(200)

    @patch.object(audit, "visible_weixin_windows")
    def test_selects_only_foreground_window(self, visible_windows):
        self.windows[1]["foreground"] = True
        visible_windows.return_value = self.windows
        self.assertEqual(audit.select_weixin_window(), self.windows[1])

    @patch.object(audit.win32gui, "GetWindowPlacement")
    def test_uses_restore_rect_for_minimized_window(self, get_placement):
        get_placement.return_value = (0, 2, (-21333, -21333), (-1, -1), (100, 50, 1000, 750))
        self.assertEqual(
            audit.effective_window_rect(123, minimized=True),
            audit.PixelRect(100, 50, 1000, 750),
        )
        get_placement.assert_called_once_with(123)

    @patch.object(audit.win32gui, "GetWindowRect", return_value=(10, 20, 810, 620))
    def test_uses_current_rect_for_restored_window(self, get_rect):
        self.assertEqual(
            audit.effective_window_rect(123, minimized=False),
            audit.PixelRect(10, 20, 810, 620),
        )
        get_rect.assert_called_once_with(123)


class CalibrationTests(unittest.TestCase):
    @patch.object(audit.win32gui, "GetCursorPos", return_value=(125, 250))
    def test_calculates_cursor_ratios(self, _):
        window = {"left": 25, "top": 50, "width": 400, "height": 400}
        result = audit.calibrate_cursor(window)
        self.assertEqual(
            result,
            {
                "inside_window": True,
                "cursor": [125, 250],
                "ratio": [0.25, 0.5],
                "window": window,
            },
        )

    def test_calculates_panel_ratios_from_two_points(self):
        window = {"left": 100, "top": 50, "width": 1000, "height": 500}
        self.assertEqual(
            audit.panel_from_points(window, (800, 100), (1050, 500)),
            audit.PanelRatios(0.7, 0.1, 0.25, 0.8),
        )

    def test_round_trips_panel_config(self):
        window = {"pid": 123}
        panel = audit.PanelRatios(0.7, 0.1, 0.25, 0.8)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            audit.save_panel_config(path, window, panel)
            self.assertEqual(audit.load_panel_config(path, 123), panel)

    def test_rejects_panel_config_for_another_pid(self):
        window = {"pid": 123}
        panel = audit.PanelRatios(0.7, 0.1, 0.25, 0.8)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            audit.save_panel_config(path, window, panel)
            self.assertIsNone(audit.load_panel_config(path, 456))
            self.assertEqual(audit.read_panel_config(path, 456)[1], "pid_mismatch:123")

    def test_reports_invalid_panel_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(audit.read_panel_config(path, 123), (None, "invalid"))

    def test_reports_missing_panel_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            self.assertEqual(audit.read_panel_config(path, 123), (None, "missing"))


class ActivationTests(unittest.TestCase):
    @patch.object(audit.time, "sleep")
    @patch.object(audit, "visible_weixin_windows")
    @patch.object(audit, "USER32")
    @patch.object(audit.win32process, "GetWindowThreadProcessId")
    @patch.object(audit.win32gui, "GetForegroundWindow", side_effect=[999, 123])
    @patch.object(audit.win32gui, "SetWindowPos")
    @patch.object(audit.win32gui, "IsWindowVisible", return_value=True)
    @patch.object(audit.win32gui, "IsIconic", return_value=False)
    def test_activates_and_refreshes_window(
        self,
        _is_iconic,
        _is_visible,
        _set_window_pos,
        _get_foreground,
        get_window_thread,
        user32,
        visible_windows,
        _sleep,
    ):
        window = {"hwnd": 123, "pid": 456}
        refreshed = {**window, "foreground": True}
        get_window_thread.side_effect = [(20, 456), (10, 999)]
        user32.AttachThreadInput.return_value = True
        visible_windows.return_value = [refreshed]
        self.assertEqual(
            audit.activate_window(window),
            {
                "activated": True,
                "was_hidden": False,
                "was_minimized": False,
                "input_threads_attached": True,
                "window": refreshed,
            },
        )
        self.assertEqual(
            user32.AttachThreadInput.call_args_list,
            [unittest.mock.call(10, 20, True), unittest.mock.call(10, 20, False)],
        )
        visible_windows.assert_called_once_with(456)

    @patch.object(audit.time, "sleep")
    @patch.object(audit, "visible_weixin_windows")
    @patch.object(audit, "USER32")
    @patch.object(audit.win32process, "GetWindowThreadProcessId")
    @patch.object(audit.win32gui, "GetForegroundWindow", side_effect=[999, 123])
    @patch.object(audit.win32gui, "SetWindowPos")
    @patch.object(audit.win32gui, "ShowWindow")
    @patch.object(audit.win32gui, "IsWindowVisible", return_value=False)
    @patch.object(audit.win32gui, "IsIconic", return_value=False)
    def test_shows_hidden_tray_window_before_activation(
        self,
        _is_iconic,
        _is_visible,
        show_window,
        _set_window_pos,
        _get_foreground,
        get_window_thread,
        user32,
        visible_windows,
        _sleep,
    ):
        window = {"hwnd": 123, "pid": 456}
        refreshed = {**window, "visible": True, "hidden": False}
        get_window_thread.side_effect = [(20, 456), (10, 999)]
        user32.AttachThreadInput.return_value = True
        visible_windows.return_value = [refreshed]

        result = audit.activate_window(window)

        self.assertTrue(result["activated"])
        self.assertTrue(result["was_hidden"])
        self.assertFalse(result["was_minimized"])
        show_window.assert_called_once_with(123, audit.win32con.SW_SHOW)

    @patch.object(audit.time, "sleep")
    @patch.object(audit, "visible_weixin_windows")
    @patch.object(audit, "USER32")
    @patch.object(audit.win32process, "GetWindowThreadProcessId")
    @patch.object(audit.win32gui, "GetForegroundWindow", side_effect=[999, 777, 123])
    @patch.object(audit.win32gui, "SetWindowPos")
    @patch.object(audit.win32gui, "IsWindowVisible", return_value=True)
    @patch.object(audit.win32gui, "IsIconic", return_value=False)
    def test_retries_when_first_foreground_request_is_rejected(
        self,
        _is_iconic,
        _is_visible,
        _set_window_pos,
        _get_foreground,
        get_window_thread,
        user32,
        visible_windows,
        _sleep,
    ):
        window = {"hwnd": 123, "pid": 456}
        refreshed = {**window, "foreground": True}
        get_window_thread.side_effect = [(20, 456), (10, 999)]
        user32.AttachThreadInput.return_value = True
        visible_windows.return_value = [refreshed]

        result = audit.activate_window(window)

        self.assertTrue(result["activated"])
        self.assertEqual(user32.SetForegroundWindow.call_count, 2)
        self.assertEqual(user32.SetFocus.call_count, 2)


class CaptureSourceTests(unittest.TestCase):
    def setUp(self):
        self.window = {
            "hwnd": 123,
            "left": 100,
            "top": 50,
            "width": 200,
            "height": 100,
        }

    @patch.object(audit, "render_window_image")
    def test_auto_uses_print_window_when_available(self, render_window_image):
        expected = audit.Image.new("RGB", (200, 100), "red")
        metadata = {"source": "print_window", "rendered": True}
        render_window_image.return_value = (expected, metadata)

        image, result_metadata = audit.capture_window_image(self.window, "auto")

        self.assertIs(image, expected)
        self.assertEqual(result_metadata, metadata)

    @patch.object(audit.ImageGrab, "grab")
    @patch.object(
        audit,
        "virtual_desktop_rect",
        return_value=audit.PixelRect(0, 0, 1000, 800),
    )
    @patch.object(audit, "render_window_image")
    def test_auto_falls_back_to_desktop_capture(
        self,
        render_window_image,
        _virtual_desktop,
        grab,
    ):
        failure = {"source": "print_window", "rendered": False}
        render_window_image.return_value = (None, failure)
        desktop_image = audit.Image.new("RGB", (200, 100), "red")
        desktop_image.paste("blue", (0, 0, 100, 100))
        grab.return_value = desktop_image

        image, metadata = audit.capture_window_image(self.window, "auto")

        self.assertEqual(image.size, (200, 100))
        self.assertEqual(metadata["source"], "desktop_capture")
        self.assertEqual(metadata["print_window_failure"], failure)
        grab.assert_called_once_with(bbox=(100, 50, 300, 150), all_screens=True)

    @patch.object(
        audit,
        "virtual_desktop_rect",
        return_value=audit.PixelRect(0, 0, 250, 120),
    )
    @patch.object(audit, "render_window_image")
    def test_auto_rejects_incomplete_desktop_fallback(
        self,
        render_window_image,
        _virtual_desktop,
    ):
        render_window_image.return_value = (
            None,
            {"source": "print_window", "rendered": False},
        )

        with self.assertRaisesRegex(RuntimeError, "desktop_fallback_would_be_incomplete"):
            audit.capture_window_image(self.window, "auto")

    @patch.object(audit.ImageGrab, "grab")
    @patch.object(
        audit,
        "virtual_desktop_rect",
        return_value=audit.PixelRect(0, 0, 1000, 800),
    )
    def test_screen_source_rejects_blank_capture(self, _virtual_desktop, grab):
        grab.return_value = audit.Image.new("RGB", (200, 100), "black")

        with self.assertRaisesRegex(RuntimeError, '"nonempty": false'):
            audit.capture_window_image(self.window, "screen")

    @patch.object(audit, "render_window_image")
    def test_window_source_does_not_fall_back(self, render_window_image):
        render_window_image.return_value = (
            None,
            {"source": "print_window", "rendered": False},
        )

        with self.assertRaises(RuntimeError):
            audit.capture_window_image(self.window, "window")

    @patch.object(audit, "capture_window_image")
    def test_capture_panel_crops_window_relative_box(self, capture_window_image):
        capture_window_image.return_value = (
            audit.Image.new("RGB", (200, 100), "red"),
            {"source": "print_window"},
        )
        panel = audit.PanelRatios(0.5, 0.2, 0.25, 0.5)

        with TemporaryDirectory() as directory:
            result = audit.capture_panel(
                self.window,
                Path(directory) / "panel.png",
                panel,
                "auto",
            )

        self.assertEqual(result["window_panel_box"], [100, 20, 150, 70])
        self.assertEqual(result["image_size"], [50, 50])


class QuickCaptureTests(unittest.TestCase):
    def test_reads_configured_pid(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text('{"target_pid": 6800}', encoding="utf-8")
            self.assertEqual(quick_capture.configured_pid(path), (6800, "matched"))

    def test_rejects_invalid_configured_pid(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text('{"target_pid": 0}', encoding="utf-8")
            self.assertEqual(quick_capture.configured_pid(path), (None, "invalid"))

    @patch("quick_capture.audit.visible_weixin_windows")
    def test_auto_selects_only_running_weixin_when_config_pid_is_stale(self, visible_windows):
        visible_windows.return_value = [{"pid": 26764}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text('{"target_pid": 6800}', encoding="utf-8")

            self.assertEqual(
                quick_capture.select_pid(None, path),
                (26764, "auto_single_window:matched"),
            )

    @patch("quick_capture.audit.visible_weixin_windows")
    def test_does_not_guess_when_multiple_weixin_windows_exist(self, visible_windows):
        visible_windows.return_value = [{"pid": 26764}, {"pid": 30001}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text('{"target_pid": 6800}', encoding="utf-8")

            self.assertEqual(quick_capture.select_pid(None, path), (6800, str(path.resolve())))

    def test_builds_timestamped_output(self):
        now = quick_capture.datetime(2026, 8, 7, 22, 30, 45, 123456)
        self.assertEqual(
            quick_capture.timestamped_output(Path("artifacts"), now),
            Path("artifacts/right-panel-20260807-223045-123456.png"),
        )


class OpenGroupTests(unittest.TestCase):
    def test_win32_input_structure_has_native_size(self):
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(open_group.INPUT), expected_size)

    def test_parses_tsv_words_into_lines(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t20\t40\t15\t95\tGroup\n"
            "5\t1\t1\t1\t1\t2\t55\t20\t35\t15\t95\tChats\n"
        )
        self.assertEqual(
            open_group.parse_tsv_lines(tsv),
            [open_group.OcrLine("Group Chats", 10, 20, 90, 35)],
        )

    def test_finds_exact_result_only_in_group_chats_section(self):
        lines = [
            open_group.OcrLine("Codex交流群2", 120, 70, 300, 90),
            open_group.OcrLine("Group Chats", 140, 210, 260, 230),
            open_group.OcrLine("Codex 交流群 2", 160, 270, 350, 300),
            open_group.OcrLine("Chat History", 140, 350, 280, 370),
            open_group.OcrLine("Codex交流群2", 160, 420, 350, 450),
        ]
        self.assertEqual(
            open_group.find_group_result(lines, "Codex交流群2"),
            lines[2],
        )

    def test_does_not_use_chat_history_match(self):
        lines = [
            open_group.OcrLine("Group Chats", 140, 210, 260, 230),
            open_group.OcrLine("Another group", 160, 270, 350, 300),
            open_group.OcrLine("Chat History", 140, 350, 280, 370),
            open_group.OcrLine("Target group", 160, 420, 350, 450),
        ]
        self.assertIsNone(open_group.find_group_result(lines, "Target group"))

    def test_accepts_exact_result_in_most_used_section(self):
        lines = [
            open_group.OcrLine("Most used", 140, 120, 230, 140),
            open_group.OcrLine("Codex 交流群 2", 160, 180, 350, 210),
            open_group.OcrLine("Chat History", 140, 260, 280, 280),
            open_group.OcrLine("Codex交流群2", 160, 340, 350, 370),
        ]
        self.assertEqual(
            open_group.find_group_result(lines, "Codex交流群2"),
            lines[1],
        )


class WxCommandTests(unittest.TestCase):
    def test_default_mode_targets_group_chat(self):
        args = wx.parser().parse_args([])
        self.assertEqual(args.m, "chat")
        self.assertFalse(args.f)
        self.assertFalse(args.n)
        self.assertIsNone(args.s)

    def test_persists_a_result_next_to_the_captured_pages(self):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            payload = {"ok": True, "stop_reason": "left_saved_groups"}
            with patch("builtins.print") as print_result:
                wx.emit_result(output_dir, payload)

            self.assertEqual(
                json.loads((output_dir / "result.json").read_text(encoding="utf-8")),
                payload,
            )
            print_result.assert_called_once()

    def test_saved_groups_launcher_uses_the_scoped_capture_command(self):
        launcher = Path("capture_saved_groups.cmd").read_text(encoding="utf-8")
        self.assertIn('wx.py -m saved -n -o "%OUTPUT%"', launcher)
        self.assertIn('result.json', launcher)

    def test_contacts_navigation_uses_the_calibrated_sidebar_position(self):
        self.assertEqual(wx.sidebar_point({"left": 500, "top": 262}, wx.CONTACTS_NAV), (555, 504))

    @patch("wx.audit.visible_weixin_windows")
    def test_auto_selects_only_running_weixin_when_config_pid_is_stale(self, visible_windows):
        visible_windows.return_value = [{"pid": 26764}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            path.write_text('{"target_pid": 6800}', encoding="utf-8")

            self.assertEqual(wx.select_pid(None, path), (26764, "auto_single_window:matched"))

    def test_search_waits_keep_no_ocr_path_faster(self):
        self.assertLess(
            wx.SEARCH_RESULT_WAIT_NO_OCR_SECONDS,
            wx.SEARCH_RESULT_WAIT_OCR_SECONDS,
        )

    def test_group_scroll_is_faster_than_the_previous_fixed_delay(self):
        self.assertEqual(wx.LIST_SCROLL_DELTA, -12000)
        self.assertLess(wx.LIST_SCROLL_SETTLE_SECONDS, 0.65)
        self.assertEqual(wx.LIST_TOP_SCROLL_STEPS, 3)

    @patch("wx.scroll_list")
    @patch("wx.open_group.run_ocr")
    @patch("wx.capture_live_window")
    def test_saved_capture_uses_english_ocr_for_section_boundaries(
        self, capture_live_window, run_ocr, _scroll_list
    ):
        with TemporaryDirectory() as directory:
            capture_live_window.return_value = (
                audit.Image.new("RGB", (1000, 800), "white"),
                {"source": "screen"},
            )
            run_ocr.return_value = [
                open_group.OcrLine("Saved Groups", 10, 20, 140, 40),
                open_group.OcrLine("Official Accounts", 10, 100, 150, 120),
            ]

            wx.save_saved_group_pages(
                {"left": 0, "top": 0, "width": 1000, "height": 800},
                Path(directory),
                1,
                Path("tesseract"),
            )

            self.assertEqual(
                run_ocr.call_args.kwargs, {"psm": 11, "language": "eng"}
            )

    def test_saved_group_search_expands_before_scanning(self):
        source = Path(wx.__file__).read_text(encoding="utf-8")
        saved_search_branch = source.index("if saved_group_search:")
        expand = source.index("expand_saved_groups(window, tesseract, args.o)", saved_search_branch)
        scan = source.index("find_saved_group(", expand)
        self.assertLess(expand, scan)

    def test_short_options(self):
        args = wx.parser().parse_args(["-m", "saved", "-f", "-q", "alice", "-n", "-s", "3"])
        self.assertEqual((args.m, args.f, args.q, args.n, args.s), ("saved", True, "alice", True, 3))

    def test_page_comparison_stops_for_nearly_identical_frames(self):
        previous = audit.Image.new("RGB", (100, 100), "white")
        identical = previous.copy()
        changed = previous.copy()
        changed.paste("black", (0, 0, 20, 20))

        self.assertFalse(wx.page_changed(previous, identical))
        self.assertTrue(wx.page_changed(previous, changed))

    def test_detects_a_scrollbar_thumb_at_the_bottom_of_the_list(self):
        image = audit.Image.new("RGB", (100, 100), "white")
        image.paste("#a6a6a7", (90, 88, 94, 100))

        self.assertTrue(wx.scrollbar_reached_bottom(image))

    def test_does_not_treat_a_mid_list_scrollbar_thumb_as_the_bottom(self):
        image = audit.Image.new("RGB", (100, 100), "white")
        image.paste("#a6a6a7", (90, 35, 94, 52))

        self.assertFalse(wx.scrollbar_reached_bottom(image))

    def test_saved_search_is_limited_by_page_count(self):
        args = wx.parser().parse_args(["-m", "saved", "-q", "Target", "-s", "4"])
        self.assertEqual((args.m, args.q, args.s, args.n), ("saved", "Target", 4, False))

    def test_saved_mode_documents_preview_semantics(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("绝不会自动点 `Join Group`", readme)
        self.assertIn("saved_group_preview", readme)
        self.assertIn('python wx.py -m saved -q "IT30" -s 5', readme)

    def test_finds_group_only_inside_allowed_section(self):
        lines = [
            open_group.OcrLine("Group Chats", 10, 20, 100, 40),
            open_group.OcrLine("Target", 20, 60, 100, 80),
            open_group.OcrLine("Chat History", 10, 100, 120, 120),
            open_group.OcrLine("Other", 20, 140, 100, 160),
        ]
        self.assertEqual(wx.find_exact_result(lines, "Target", {"groups"}), lines[1])
        self.assertIsNone(wx.find_exact_result(lines, "Other", {"groups"}))

    def test_recognizes_saved_groups_heading_with_ocr_icon_noise(self):
        lines = [
            open_group.OcrLine("Y Saved Groups", 10, 20, 120, 40),
            open_group.OcrLine("Target", 20, 60, 100, 80),
        ]
        self.assertEqual(wx.find_exact_result(lines, "Target", {"saved_groups"}), lines[1])

    def test_saved_group_search_continues_without_repeated_heading(self):
        lines = [open_group.OcrLine("Target", 20, 60, 100, 80)]
        self.assertEqual(
            wx.find_saved_group_result(lines, "Target", True),
            (lines[0], True),
        )

    def test_finds_saved_group_heading(self):
        lines = [
            open_group.OcrLine("Y Saved Groups", 10, 20, 120, 40),
            open_group.OcrLine("Target", 20, 60, 100, 80),
        ]
        self.assertEqual(wx.saved_group_heading(lines), lines[0])

    def test_recognizes_chinese_section_headings(self):
        self.assertEqual(wx.section_kind("保存的群聊"), "saved_groups")
        self.assertEqual(wx.section_kind("公众号与视频号"), "official_accounts")
        self.assertEqual(wx.section_kind("联系人"), "contacts")

    @patch("wx.scroll_list")
    @patch("wx.open_group.run_ocr")
    @patch("wx.capture_live_window")
    def test_saved_capture_stops_before_contacts_section(
        self, capture_live_window, run_ocr, scroll_list
    ):
        with TemporaryDirectory() as directory:
            capture_live_window.return_value = (
                audit.Image.new("RGB", (1000, 800), "white"),
                {"source": "screen"},
            )
            run_ocr.return_value = [
                open_group.OcrLine("Saved Groups", 10, 20, 140, 40),
                open_group.OcrLine("One Group", 20, 60, 150, 80),
                open_group.OcrLine("Contacts", 10, 100, 120, 120),
            ]

            outputs, stop_reason = wx.save_saved_group_pages(
                {"left": 0, "top": 0, "width": 1000, "height": 800},
                Path(directory),
                10,
                Path("tesseract"),
            )

            self.assertEqual(stop_reason, "left_saved_groups")
            self.assertEqual(len(outputs), 1)
            self.assertTrue(Path(outputs[0]).is_file())
            scroll_list.assert_not_called()

    @patch("wx.scroll_list")
    @patch("wx.open_group.run_ocr")
    @patch("wx.capture_live_window")
    def test_saved_capture_keeps_a_collapsed_saved_groups_section_visible(
        self, capture_live_window, run_ocr, scroll_list
    ):
        with TemporaryDirectory() as directory:
            capture_live_window.return_value = (
                audit.Image.new("RGB", (1000, 800), "white"),
                {"source": "screen"},
            )
            run_ocr.return_value = [
                open_group.OcrLine("Saved Groups", 10, 20, 140, 40),
                open_group.OcrLine("Official Accounts", 10, 100, 150, 120),
            ]

            outputs, stop_reason = wx.save_saved_group_pages(
                {"left": 0, "top": 0, "width": 1000, "height": 800},
                Path(directory),
                10,
                Path("tesseract"),
            )

            self.assertEqual(stop_reason, "left_saved_groups")
            self.assertEqual(len(outputs), 1)
            scroll_list.assert_not_called()

    def test_saved_group_search_stops_at_next_contacts_section(self):
        lines = [
            open_group.OcrLine("Y Saved Groups", 10, 20, 120, 40),
            open_group.OcrLine("Other Group", 20, 60, 100, 80),
            open_group.OcrLine("Official Accounts", 10, 100, 150, 120),
            open_group.OcrLine("Target", 20, 140, 100, 160),
        ]
        self.assertEqual(wx.find_saved_group_result(lines, "Target", False), (None, False))

    def test_saved_group_bounds_exclude_following_section(self):
        lines = [
            open_group.OcrLine("Y Saved Groups", 10, 20, 120, 40),
            open_group.OcrLine("Official Accounts", 10, 100, 150, 120),
        ]
        self.assertEqual(wx.saved_group_section_bounds(lines, False), (40, 100, False))

    @patch("wx.open_group.run_ocr")
    def test_expanded_saved_group_text_maps_back_to_original_coordinates(self, run_ocr):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "saved.png"
            audit.Image.new("RGB", (400, 600), "white").save(image_path)
            run_ocr.return_value = [open_group.OcrLine("Target", 30, 60, 180, 90)]

            match = wx.find_saved_group_in_expanded_text(
                Path("tesseract"), image_path, "Target", 100, 500
            )

            self.assertEqual(match, open_group.OcrLine("Target", 106, 120, 156, 130))
            self.assertEqual(run_ocr.call_args.kwargs, {"psm": 6})

    def test_left_pane_crop_returns_offset(self):
        image = audit.Image.new("RGB", (1000, 800), "white")
        cropped, offset = wx.crop_left_pane(image)
        self.assertEqual(offset, (65, 28))
        self.assertEqual(cropped.size, (295, 740))

    def test_maps_full_capture_coordinates_to_the_live_window(self):
        window = {"left": 677, "top": 209, "width": 1344, "height": 1138}
        image = audit.Image.new("RGB", (672, 569), "white")

        self.assertEqual(wx.screen_point_from_capture(window, image, 106, 140), (889, 489))

    @patch("wx.open_group.run_ocr")
    def test_verifies_target_in_opened_chat_header(self, run_ocr):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "opened.png"
            audit.Image.new("RGB", (1000, 800), "white").save(image_path)
            run_ocr.return_value = [open_group.OcrLine("Target Group", 10, 10, 100, 30)]

            self.assertTrue(wx.verify_opened_title(Path("tesseract"), image_path, "Target Group"))

            header_path = image_path.with_name("opened-header.png")
            self.assertTrue(header_path.is_file())
            run_ocr.assert_called_once_with(Path("tesseract"), header_path)

    def test_title_verification_rejects_a_different_chat(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "opened.png"
            audit.Image.new("RGB", (1000, 800), "white").save(image_path)
            with patch("wx.open_group.run_ocr", return_value=[open_group.OcrLine("Other", 1, 1, 2, 2)]):
                self.assertFalse(wx.verify_opened_title(Path("tesseract"), image_path, "Target"))

    def test_recognizes_join_group_preview_from_green_button(self):
        image = audit.Image.new("RGB", (1000, 800), "white")
        image.paste("#07c160", (620, 300, 800, 380))
        self.assertEqual(wx.saved_group_preview_state(image), "join_group")

    def test_keeps_unknown_preview_when_join_button_is_absent(self):
        image = audit.Image.new("RGB", (1000, 800), "white")
        self.assertEqual(wx.saved_group_preview_state(image), "unknown")

    @patch("wx.time.sleep")
    @patch("wx.verify_opened_title", side_effect=[False, True])
    @patch("wx.capture_live_window")
    @patch("wx.open_group.click_screen_point")
    def test_retries_only_the_same_chat_result_point(
        self,
        click_screen_point,
        capture_live_window,
        verify_opened_title,
        _sleep,
    ):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            capture_live_window.return_value = (
                audit.Image.new("RGB", (100, 100), "white"),
                {"source": "screen"},
            )

            metadata, title_verified, attempts, opened_path = wx.click_result_and_verify_chat(
                {"pid": 6800},
                (10, 20),
                Path("tesseract"),
                output_dir,
                "Target",
            )

            self.assertEqual(metadata, {"source": "screen"})
            self.assertTrue(title_verified)
            self.assertEqual(attempts, 2)
            self.assertEqual(opened_path, output_dir / "opened.png")
            self.assertEqual(click_screen_point.call_args_list, [unittest.mock.call((10, 20))] * 2)
            self.assertEqual(capture_live_window.call_count, 2)
            self.assertEqual(verify_opened_title.call_count, 2)

    @patch("wx.capture_live_window")
    @patch("wx.capture_full_window")
    def test_visible_pages_use_live_capture_after_interaction(self, full_capture, live_capture):
        with TemporaryDirectory() as directory:
            image = audit.Image.new("RGB", (100, 100), "red")
            live_capture.return_value = (image, {"source": "screen"})

            outputs, stop_reason = wx.save_visible_pages(
                {"left": 0, "top": 0, "width": 100, "height": 100},
                Path(directory),
                1,
                live=True,
            )

            self.assertEqual(len(outputs), 1)
            self.assertEqual(stop_reason, "maximum_pages")
            live_capture.assert_called_once()
            full_capture.assert_not_called()

    @patch("wx.scroll_list")
    @patch("wx.capture_live_window")
    @patch("wx.capture_full_window")
    def test_auto_capture_stops_without_saving_the_duplicate_bottom_page(
        self,
        full_capture,
        live_capture,
        scroll_list,
    ):
        with TemporaryDirectory() as directory:
            image = audit.Image.new("RGB", (100, 100), "red")
            full_capture.return_value = (image, {"source": "window"})
            live_capture.return_value = (image, {"source": "screen"})

            outputs, stop_reason = wx.save_visible_pages(
                {"left": 0, "top": 0, "width": 100, "height": 100},
                Path(directory),
                None,
                live=False,
            )

            self.assertEqual(len(outputs), 1)
            self.assertEqual(stop_reason, "page_unchanged")
            self.assertTrue(Path(outputs[0]).is_file())
            full_capture.assert_called_once()
            live_capture.assert_called_once()
            scroll_list.assert_called_once()

    @patch("wx.open_group.desktop_window_capture")
    def test_live_capture_uses_current_desktop_frame(self, desktop_capture):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "live.png"
            audit.Image.new("RGB", (20, 10), "red").save(output)

            image, metadata = wx.capture_live_window({"pid": 6800}, output)

            desktop_capture.assert_called_once_with({"pid": 6800}, output)
            self.assertEqual(image.size, (20, 10))
            self.assertEqual(metadata, {"source": "screen"})


if __name__ == "__main__":
    unittest.main()
