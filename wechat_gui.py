"""Desktop control panel for the optional pywechat2 UIA backup workflow."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import ctypes
import ctypes.wintypes
import tkinter as tk
import shutil
import tempfile
import urllib.request
import zipfile
import psutil
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = ROOT / "gui_config.json"
HELP_FILE = ROOT / "说明.txt"
DEFAULT_PYWECHAT = Path(r"D:\tmp\anjian\pj\st\tmp\pywechat2")
MODE_LABELS = {"auto": "自动", "list": "只截图", "detail": "打开详情"}
MODE_VALUES = {label: value for value, label in MODE_LABELS.items()}
HOTKEY_MIGRATIONS = {
    "Ctrl+Shift+Q": "Ctrl+Alt+Q",
    "Ctrl+Shift+E": "Ctrl+Alt+E",
    "Ctrl+Shift+S": "Ctrl+Alt+S",
}


HELP_TEXT = """微信可见界面备份 - 使用说明

一、开始前
1. 先打开微信并完成登录，不要锁屏。
2. 保持微信窗口可见；不要在运行期间手动拖动微信窗口。
3. 第一次测试建议只勾选“最近会话：群”，群上限设为 1。

二、任务
最近会话：联系人：扫描最近聊天中的个人。
最近会话：群：扫描最近聊天中的群，包括“折叠的聊天”列表中的群。
通讯录：全部联系人：逐个打开联系人资料并截图微信号。
通讯录：保存的群：扫描通讯录中的保存群。

三、群成员
成员搜索默认是 1,a-z，表示先搜索数字 1，再搜索 26 个英文字母。
群成员只截图（禁止点击资料卡）：默认开启。只滚动并截图成员列表，绝不点击成员。
群策略“自动”：有公开微信号时只截图，否则按详情策略处理。
群策略“只截图”：始终只滚动截图。
群策略“打开详情”：逐个打开资料卡；只有关闭“列表已有微信号时只截图”后才会强制使用。

四、输出
结果在 portable\\artifacts\\gui-workflow\\ 下，每个任务有 result.json 和 PNG 图片。

五、暂停和停止
可使用界面按钮，或在“全局设置”中修改快捷键。保存配置后下一次运行生效。
默认启动 Ctrl+Alt+Q，暂停 Ctrl+Alt+E，停止 Ctrl+Alt+S。

六、故障排查
如果微信白屏，先手动点击微信窗口一次，等待重绘后再运行。
如果截图数量异常，先把群上限和每个搜索词页数设为 1，查看 result.json 和日志。
"""


def load_config() -> dict[str, object]:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pywechat_root": str(DEFAULT_PYWECHAT), "proxy_mode": "SOCKS5", "proxy_host": "127.0.0.1", "proxy_port": "7891"}


def proxy_env(values: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    mode = values.get("mode", "SOCKS5")
    host = values.get("host", "127.0.0.1")
    port = values.get("port", "7891")
    if mode == "直连":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    else:
        scheme = "http" if mode == "HTTP" else "socks5h"
        url = f"{scheme}://{host}:{port}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = url
    return env


def github_json(url: str, values: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "WechatRosterGUI"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy_env(values)))
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("微信通讯录备份工具")
        self.geometry("900x640+40+30")
        self.minsize(780, 600)
        self.config_data = load_config()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.active_processes: set[subprocess.Popen[str]] = set()
        self.backup_running = False
        self.backup_paused = False
        self.hotkey_thread_id = 0
        self.hotkey_shutdown = threading.Event()
        bundled_root = ROOT / "pywechat2"
        default_root = bundled_root if bundled_root.is_dir() else DEFAULT_PYWECHAT
        self.root_var = tk.StringVar(value=str(self.config_data.get("pywechat_root", default_root)))
        self.proxy_mode = tk.StringVar(value=str(self.config_data.get("proxy_mode", "SOCKS5")))
        self.proxy_host = tk.StringVar(value=str(self.config_data.get("proxy_host", "127.0.0.1")))
        self.proxy_port = tk.StringVar(value=str(self.config_data.get("proxy_port", "7891")))
        self.version_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.task_recent_people = tk.BooleanVar(value=bool(self.config_data.get("task_recent_people", True)))
        self.task_recent_groups = tk.BooleanVar(value=bool(self.config_data.get("task_recent_groups", True)))
        self.task_contacts = tk.BooleanVar(value=bool(self.config_data.get("task_contacts", True)))
        self.task_saved_groups = tk.BooleanVar(value=bool(self.config_data.get("task_saved_groups", False)))
        self.task_folded_groups = tk.BooleanVar(value=bool(self.config_data.get("task_folded_groups", True)))
        self.group_filters = tk.StringVar(value=str(self.config_data.get("group_filters", "")))
        self.member_terms = tk.StringVar(value=str(self.config_data.get("member_terms", "1,a-z")))
        saved_mode = str(self.config_data.get("member_mode", "auto"))
        self.member_mode = tk.StringVar(value=MODE_LABELS.get(saved_mode, saved_mode))
        self.list_if_id = tk.BooleanVar(value=bool(self.config_data.get("list_if_id", True)))
        self.people_limit = tk.IntVar(value=int(self.config_data.get("people_limit", 1000)))
        self.group_limit = tk.IntVar(value=int(self.config_data.get("group_limit", 1000)))
        self.member_pages = tk.IntVar(value=int(self.config_data.get("member_pages", 1000)))
        self.click_delay = tk.DoubleVar(value=float(self.config_data.get("click_delay", 0.06)))
        self.scroll_delay = tk.DoubleVar(value=float(self.config_data.get("scroll_delay", 0.15)))
        self.profile_delay = tk.DoubleVar(value=float(self.config_data.get("profile_delay", 0.55)))
        self.chat_open_delay = tk.DoubleVar(value=float(self.config_data.get("chat_open_delay", 1.2)))
        self.navigation_delay = tk.DoubleVar(value=float(self.config_data.get("navigation_delay", 0.45)))
        self.settings_delay = tk.DoubleVar(value=float(self.config_data.get("settings_delay", 0.55)))
        self.recent_scroll_delta = tk.IntVar(value=int(self.config_data.get("recent_scroll_delta", -120)))
        self.minimize_after_start = tk.BooleanVar(value=bool(self.config_data.get("minimize_after_start", True)))
        self.group_error_policy = tk.StringVar(value=str(self.config_data.get("group_error_policy", "skip")))
        self.hotkey_start = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_start", "Ctrl+Alt+Q")), str(self.config_data.get("hotkey_start", "Ctrl+Alt+Q"))))
        self.hotkey_pause = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_pause", "Ctrl+Alt+E")), str(self.config_data.get("hotkey_pause", "Ctrl+Alt+E"))))
        self.hotkey_stop = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_stop", "Ctrl+Alt+S")), str(self.config_data.get("hotkey_stop", "Ctrl+Alt+S"))))
        self.skip_file = ROOT / "skip.request"
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self._start_hotkeys()
        self.after(150, self._poll)
        self.refresh_versions()

    def _build(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        title = ttk.Frame(main)
        title.pack(fill="x")
        ttk.Label(title, text="微信可见界面备份", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Button(title, text="?", width=3, command=self.open_help).pack(side="right")

        path = ttk.LabelFrame(main, text="pywechat2 版本", padding=10)
        path.pack(fill="x", pady=(12, 8))
        ttk.Label(path, text="目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(path, textvariable=self.root_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(path, text="浏览", command=self.choose_root).grid(row=0, column=2)
        ttk.Label(path, text="版本/提交").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.version_combo = ttk.Combobox(path, textvariable=self.version_var, state="readonly")
        self.version_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Button(path, text="刷新版本", command=self.refresh_versions).grid(row=1, column=2, pady=(8, 0))
        ttk.Button(path, text="切换到选中版本", command=self.switch_version).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Button(path, text="检查/获取更新", command=self.fetch_updates).grid(row=2, column=2, pady=(8, 0))
        path.columnconfigure(1, weight=1)

        proxy = ttk.LabelFrame(main, text="全局网络代理", padding=10)
        proxy.pack(fill="x", pady=8)
        ttk.Label(proxy, text="模式").grid(row=0, column=0, sticky="w")
        ttk.Combobox(proxy, textvariable=self.proxy_mode, values=("直连", "HTTP", "SOCKS5"), state="readonly", width=12).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(proxy, text="地址").grid(row=0, column=2, sticky="w")
        ttk.Entry(proxy, textvariable=self.proxy_host, width=16).grid(row=0, column=3, padx=8)
        ttk.Label(proxy, text="端口").grid(row=0, column=4, sticky="w")
        ttk.Entry(proxy, textvariable=self.proxy_port, width=8).grid(row=0, column=5, padx=8)
        ttk.Button(proxy, text="保存代理", command=self.save_config).grid(row=0, column=6)

        actions = ttk.LabelFrame(main, text="备份操作", padding=10)
        actions.pack(fill="x", pady=8)
        ttk.Checkbutton(actions, text="最近会话：联系人", variable=self.task_recent_people).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(actions, text="最近会话：群", variable=self.task_recent_groups).grid(row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Checkbutton(actions, text="通讯录：全部联系人", variable=self.task_contacts).grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Checkbutton(actions, text="通讯录：保存的群", variable=self.task_saved_groups).grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(5, 0))

        ttk.Label(actions, text="群名筛选").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(actions, textvariable=self.group_filters).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        member_options = ttk.Frame(actions)
        member_options.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(member_options, text="成员搜索").pack(side="left")
        ttk.Entry(member_options, textvariable=self.member_terms, width=18).pack(side="left", padx=(8, 20))
        self.start_button = ttk.Button(actions, text="开始选中任务", command=self.run_selected)
        self.start_button.grid(row=4, column=1, sticky="e", pady=(10, 0))
        controls = ttk.Frame(actions)
        controls.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.pause_button = ttk.Button(controls, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop_backup, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="全局设置", command=self.open_global_settings).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="保存配置", command=self.save_config).pack(side="left", padx=(8, 0))
        actions.columnconfigure(1, weight=1)

        self.log = tk.Text(main, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 4))
        ttk.Label(main, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

    def log_text(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save_config(self) -> None:
        data = {
            "pywechat_root": self.root_var.get().strip(),
            "proxy_mode": self.proxy_mode.get(),
            "proxy_host": self.proxy_host.get().strip(),
            "proxy_port": self.proxy_port.get().strip(),
            "task_recent_people": self.task_recent_people.get(),
            "task_recent_groups": self.task_recent_groups.get(),
            "task_contacts": self.task_contacts.get(),
            "task_saved_groups": self.task_saved_groups.get(),
            "task_folded_groups": self.task_folded_groups.get(),
            "group_filters": self.group_filters.get().strip(),
            "member_terms": self.member_terms.get().strip(),
            "member_mode": MODE_VALUES.get(self.member_mode.get(), "auto"),
            "list_if_id": self.list_if_id.get(),
            "people_limit": self.people_limit.get(),
            "group_limit": self.group_limit.get(),
            "member_pages": self.member_pages.get(),
            "click_delay": self.click_delay.get(),
            "scroll_delay": self.scroll_delay.get(),
            "profile_delay": self.profile_delay.get(),
            "chat_open_delay": self.chat_open_delay.get(),
            "navigation_delay": self.navigation_delay.get() if hasattr(self, "navigation_delay") else 0.45,
            "settings_delay": self.settings_delay.get() if hasattr(self, "settings_delay") else self.profile_delay.get(),
            "recent_scroll_delta": self.recent_scroll_delta.get(),
            "minimize_after_start": self.minimize_after_start.get(),
            "group_error_policy": self.group_error_policy.get(),
            "hotkey_start": self.hotkey_start.get(),
            "hotkey_pause": self.hotkey_pause.get(),
            "hotkey_stop": self.hotkey_stop.get(),
        }
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.config_data = data
        self.log_text("全局配置已保存")

    def open_global_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("全局设置")
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        rows = (('点击后延迟（秒）', self.click_delay), ('滚动后等待（秒）', self.scroll_delay), ('打开会话后等待（秒）', self.chat_open_delay), ('打开资料卡后延迟（秒）', self.profile_delay), ('返回/导航等待（秒）', self.navigation_delay), ('设置面板等待（秒）', self.settings_delay))
        scroll_label = '最近会话滚动量'
        rows = (*rows, (scroll_label, self.recent_scroll_delta))
        for row, (label, variable) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=variable, width=12).grid(row=row, column=1, padx=10, pady=4)
        ttk.Label(body, text="群策略").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.member_mode, values=tuple(MODE_LABELS.values()), state="readonly", width=10).grid(row=7, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(body, text="自动 / 只截图 / 打开详情").grid(row=7, column=2, sticky="w")
        ttk.Checkbutton(body, text="群成员只截图（禁止点击资料卡）", variable=self.list_if_id).grid(row=8, column=0, columnspan=3, sticky="w", pady=4)
        limits = (("联系人上限", self.people_limit, 100000), ("群上限", self.group_limit, 100000), ("每个搜索词页数", self.member_pages, 1000))
        for row, (label, variable, maximum) in enumerate(limits, 9):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Spinbox(body, from_=1, to=maximum, textvariable=variable, width=12).grid(row=row, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(body, text="群成员失败策略").grid(row=12, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.group_error_policy, values=("skip", "stop"), state="readonly", width=10).grid(row=12, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(body, text="跳过该群继续 / 遇错停止").grid(row=12, column=2, sticky="w")
        ttk.Checkbutton(body, text="处理折叠的聊天（其中通常是群）", variable=self.task_folded_groups).grid(row=13, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(body, text="开始后最小化工具窗口", variable=self.minimize_after_start).grid(row=14, column=0, columnspan=3, sticky="w", pady=4)
        hotkeys = (("启动快捷键", self.hotkey_start), ("暂停快捷键", self.hotkey_pause), ("停止快捷键", self.hotkey_stop))
        for row, (label, variable) in enumerate(hotkeys, 15):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=variable, width=18).grid(row=row, column=1, padx=10, pady=4)
        buttons = ttk.Frame(body)
        buttons.grid(row=19, column=0, columnspan=3, pady=(12, 0), sticky="e")
        ttk.Button(buttons, text="保存并关闭", command=lambda: (self.save_config(), self._restart_hotkeys(), dialog.destroy())).pack(side="right")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(0, 8))

    def open_help(self) -> None:
        HELP_FILE.write_text(HELP_TEXT, encoding="utf-8")
        os.startfile(str(HELP_FILE))

    def _hotkey_parts(self, value: str) -> tuple[int, int]:
        names = [part.strip().upper() for part in value.replace(" ", "").split("+") if part.strip()]
        modifiers = 0
        for name, flag in (("CTRL", 0x0002), ("SHIFT", 0x0004), ("ALT", 0x0001), ("WIN", 0x0008)):
            if name in names:
                modifiers |= flag
        key = names[-1] if names else "Q"
        return modifiers, ord(key[-1])

    def _start_hotkeys(self) -> None:
        self.hotkey_shutdown.clear()

        def worker() -> None:
            user32 = ctypes.windll.user32
            thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            self.hotkey_thread_id = thread_id
            bindings = (
                (1, self.hotkey_start.get(), "hotkey_start"),
                (2, self.hotkey_pause.get(), "hotkey_pause"),
                (3, self.hotkey_stop.get(), "hotkey_stop"),
                (4, "Ctrl+Alt+J", "hotkey_skip"),
            )
            # RegisterHotKey can silently fail when an older GUI instance or
            # another utility already owns the combination. Use it only as a
            # best-effort registration and always keep the polling fallback.
            registration = []
            for hotkey_id, value, _name in bindings:
                modifiers, key = self._hotkey_parts(value)
                registered = bool(user32.RegisterHotKey(0, hotkey_id, modifiers | 0x4000, key))
                registration.append({"id": hotkey_id, "value": value, "registered": registered})
            try:
                (ROOT / "hotkey_status.json").write_text(
                    json.dumps(
                        {"pid": os.getpid(), "mode": "polling_fallback", "bindings": registration},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass

            previous = {hotkey_id: False for hotkey_id, _value, _name in bindings}

            def is_down(value: str) -> bool:
                modifiers, key = self._hotkey_parts(value)
                required = []
                if modifiers & 0x0002:
                    required.append(0x11)  # VK_CONTROL
                if modifiers & 0x0004:
                    required.append(0x10)  # VK_SHIFT
                if modifiers & 0x0001:
                    required.append(0x12)  # VK_MENU / Alt
                if not all(bool(user32.GetAsyncKeyState(code) & 0x8000) for code in required):
                    return False
                if modifiers & 0x0008 and not (
                    bool(user32.GetAsyncKeyState(0x5B) & 0x8000)
                    or bool(user32.GetAsyncKeyState(0x5C) & 0x8000)
                ):
                    return False
                return bool(user32.GetAsyncKeyState(key) & 0x8000)

            try:
                while not self.hotkey_shutdown.is_set():
                    for hotkey_id, value, name in bindings:
                        down = is_down(value)
                        if down and not previous[hotkey_id]:
                            self.events.put((name, None))
                        previous[hotkey_id] = down
                    time.sleep(0.04)
            finally:
                for hotkey_id, _value, _name in bindings:
                    user32.UnregisterHotKey(0, hotkey_id)

        threading.Thread(target=worker, daemon=True).start()

    def _restart_hotkeys(self) -> None:
        self.hotkey_shutdown.set()
        if self.hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
        self._start_hotkeys()

    def toggle_pause(self) -> None:
        if not self.backup_running or not self.active_processes:
            return
        for process in list(self.active_processes):
            try:
                target = psutil.Process(process.pid)
                if self.backup_paused:
                    target.resume()
                else:
                    target.suspend()
            except (OSError, psutil.Error):
                pass
        self.backup_paused = not self.backup_paused
        self.pause_button.configure(text="继续" if self.backup_paused else "暂停")
        self.status_var.set("已暂停" if self.backup_paused else "正在执行选中任务...")

    def stop_backup(self) -> None:
        for process in list(self.active_processes):
            self._kill_process_tree(process)
        self.status_var.set("已停止")

    def skip_current(self) -> None:
        try:
            count = int(self.skip_file.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            count = 0
        self.skip_file.write_text(str(count + 1), encoding="ascii")
        self.log_text(f"已请求跳过 {count + 1} 个群/联系人")

    def _kill_process_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)

    def choose_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.root_var.get())
        if selected:
            self.root_var.set(selected)
            self.save_config()
            self.refresh_versions()

    def command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, cwd=self.root_var.get(), env=proxy_env({"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()}), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as error:
            raise RuntimeError("未找到 Git。版本刷新、更新和切换需要目标机安装 Git for Windows。") from error

    def refresh_versions(self) -> None:
        def work() -> None:
            try:
                result = self.command(["git", "for-each-ref", "--sort=-committerdate", "--format=%(refname:short) %(objectname:short) %(committerdate:short)", "refs/heads", "refs/tags"])
                remote = self.command(["git", "ls-remote", "--heads", "origin"])
            except RuntimeError as error:
                try:
                    latest = github_json("https://api.github.com/repos/Hello-Mr-Crab/pywechat/commits?per_page=20", {"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()})
                    values = [f"github/{item['sha'][:8]} {item['commit']['author']['date'][:10]} {item['commit']['message'].splitlines()[0]}" for item in latest]
                    self.events.put(("versions", values))
                    self.events.put(("log", "Git 不可用，已改用 GitHub API 显示版本。"))
                except Exception as api_error:
                    self.events.put(("log", f"Git 和 GitHub API 都不可用: {api_error}"))
                return
            values = [line for line in result.stdout.splitlines() if line.strip()]
            values += [f"origin/{line.split()[1].removeprefix('refs/heads/')} {line.split()[0][:8]}" for line in remote.stdout.splitlines() if len(line.split()) >= 2]
            self.events.put(("versions", list(dict.fromkeys(values))))
        threading.Thread(target=work, daemon=True).start()

    def fetch_updates(self) -> None:
        self.status_var.set("正在检查更新...")
        def work() -> None:
            try:
                result = self.command(["git", "fetch", "origin", "--prune"])
            except RuntimeError as error:
                try:
                    self.download_latest_source()
                    self.events.put(("log", "未找到 Git，已通过 GitHub API 下载最新 pywechat2 源码。"))
                    self.events.put(("status", "更新完成"))
                except Exception as api_error:
                    self.events.put(("log", f"无 Git 更新失败: {api_error}"))
                    self.events.put(("status", "更新失败"))
                return
            self.events.put(("log", result.stdout + result.stderr))
            self.events.put(("refresh", None))
        threading.Thread(target=work, daemon=True).start()

    def download_latest_source(self, ref: str = "main") -> None:
        root = Path(self.root_var.get())
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "pywechat.zip"
            request = urllib.request.Request(f"https://github.com/Hello-Mr-Crab/pywechat/archive/{ref}.zip", headers={"User-Agent": "WechatRosterGUI"})
            values = {"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()}
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy_env(values)))
            with opener.open(request, timeout=90) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            extracted = Path(directory) / "extract"
            with zipfile.ZipFile(archive) as package:
                package.extractall(extracted)
            source_root = next(extracted.glob("pywechat-*"))
            staged = root / ".src-update"
            if staged.exists():
                shutil.rmtree(staged)
            shutil.copytree(source_root / "src", staged)
            if (root / "src").exists():
                shutil.rmtree(root / "src")
            staged.rename(root / "src")

    def switch_version(self) -> None:
        ref = self.version_var.get().split()[0]
        if not ref:
            messagebox.showwarning("版本切换", "请先选择版本。")
            return
        if not messagebox.askyesno("确认切换", f"将切换 pywechat2 到 {ref}，是否继续？"):
            return
        if ref.startswith("github/"):
            try:
                self.download_latest_source(ref.removeprefix("github/"))
                self.log_text(f"已通过 GitHub API 切换到 {ref}")
                self.status_var.set("版本切换完成")
            except Exception as error:
                self.log_text(f"版本切换失败: {error}")
                self.status_var.set("版本切换失败")
            return
        command = ["git", "checkout", ref]
        if ref.startswith("origin/"):
            command = ["git", "checkout", "-B", ref.removeprefix("origin/"), ref]
        try:
            result = self.command(command)
        except RuntimeError as error:
            self.log_text(str(error))
            return
        self.log_text(result.stdout + result.stderr)
        self.status_var.set("版本切换完成" if result.returncode == 0 else "版本切换失败")

    def run_selected(self) -> None:
        if self.backup_running:
            self.log_text("已有备份任务正在运行，请等待完成或关闭窗口终止任务。")
            return
        task_values = (
            ("recent_people", self.task_recent_people.get()),
            ("recent_groups", self.task_recent_groups.get()),
            ("contacts", self.task_contacts.get()),
            ("saved_groups", self.task_saved_groups.get()),
        )
        tasks = [name for name, selected in task_values if selected]
        if not tasks:
            messagebox.showwarning("备份操作", "请至少勾选一个任务。")
            return
        self.save_config()
        output = ROOT / "artifacts" / "gui-workflow"
        portable_runner = ROOT / "wechat_backup_runner.exe"
        if portable_runner.exists():
            interpreter = str(portable_runner)
            bundled_root = ROOT / "pywechat2"
            script_args = ["--pywechat-root", str(bundled_root)] if bundled_root.is_dir() else []
        else:
            # Source checkout fallback for development; portable builds must use
            # the single bundled runner above and never require uia_backup.py.
            runner_source = ROOT / "backup_runner.py"
            if getattr(sys, "frozen", False) or not runner_source.exists():
                messagebox.showerror(
                    "运行环境",
                    "未找到 wechat_backup_runner.exe。请确认 GUI 与统一运行时位于同一目录。",
                )
                return
            interpreter = sys.executable
            script_args = [str(runner_source), "--pywechat-root", self.root_var.get()]
        args = [
            interpreter,
            *script_args,
            "--workflow",
            "-t",
            ",".join(tasks),
            "-g",
            self.group_filters.get().strip(),
            "-k",
            self.member_terms.get().strip(),
            "-M",
            MODE_VALUES.get(self.member_mode.get(), "auto"),
            "-n",
            str(self.people_limit.get()),
            "-G",
            str(self.group_limit.get()),
            "-s",
            str(self.member_pages.get()),
            "-o",
            str(output),
        ]
        env = proxy_env({"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()})
        env.update({
            "WECHAT_CLICK_DELAY": str(self.click_delay.get()),
            "WECHAT_SCROLL_DELAY": str(self.scroll_delay.get()),
            "WECHAT_PROFILE_DELAY": str(self.profile_delay.get()),
            "WECHAT_CHAT_OPEN_DELAY": str(self.chat_open_delay.get()),
            "WECHAT_NAVIGATION_DELAY": str(self.navigation_delay.get()),
            "WECHAT_SETTINGS_DELAY": str(self.settings_delay.get()),
            "WECHAT_RECENT_SCROLL_DELTA": str(self.recent_scroll_delta.get()),
            "WECHAT_LIST_IF_ID": "1" if self.list_if_id.get() else "0",
            "WECHAT_GROUP_ERROR_POLICY": self.group_error_policy.get(),
            "WECHAT_FOLDED_GROUPS": "1" if self.task_folded_groups.get() else "0",
            "WECHAT_SKIP_FILE": str(self.skip_file),
        })
        self.status_var.set("正在执行选中任务...")
        self.backup_running = True
        self.backup_paused = False
        self.start_button.state(["disabled"])
        self.pause_button.state(["!disabled"])
        self.stop_button.state(["!disabled"])
        def work() -> None:
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="gb18030",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.active_processes.add(process)
            try:
                output, _ = process.communicate()
                self.events.put(("log", output))
                self.events.put(("status", "备份完成" if process.returncode == 0 else "备份失败"))
            finally:
                self.active_processes.discard(process)
                self.events.put(("backup_done", None))
        threading.Thread(target=work, daemon=True).start()
        if self.minimize_after_start.get():
            self.after(150, self.iconify)

    def close_app(self) -> None:
        """Stop child runners before closing so no automation remains behind."""
        self.hotkey_shutdown.set()
        if self.hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
        for process in list(self.active_processes):
            if process.poll() is None:
                try:
                    # Kill the whole tree: the runner can have spawned Tesseract
                    # or other short-lived OCR helpers.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        check=False,
                    )
                except OSError:
                    pass
        self.destroy()

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "versions":
                    self.version_combo["values"] = value
                    if value and not self.version_var.get():
                        self.version_var.set(value[0])
                elif kind == "log":
                    self.log_text(str(value))
                elif kind == "status":
                    self.status_var.set(str(value))
                elif kind == "refresh":
                    self.refresh_versions()
                elif kind == "backup_done":
                    self.backup_running = False
                    self.backup_paused = False
                    self.start_button.state(["!disabled"])
                    self.pause_button.state(["disabled"])
                    self.stop_button.state(["disabled"])
                    self.pause_button.configure(text="暂停")
                elif kind == "hotkey_start":
                    self.run_selected()
                elif kind == "hotkey_pause":
                    self.toggle_pause()
                elif kind == "hotkey_stop":
                    self.stop_backup()
                elif kind == "hotkey_skip":
                    self.skip_current()
        except queue.Empty:
            pass
        self.after(150, self._poll)


if __name__ == "__main__":
    App().mainloop()
