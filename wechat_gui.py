"""Desktop control panel for the optional pywechat2 UIA backup workflow."""

from __future__ import annotations

import json
import os
import queue
import re
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
import uuid
import psutil
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from box_backup import BoxOAuthClient, IncrementalBoxUploader
from secret_store import delete_secret_keys, load_secret, update_secret
from stream_backup import IncrementalWebDavUploader
import open_group
import wx
import wechat_group_roster_audit as audit


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = ROOT / "gui_config.json"
CLOUD_CREDENTIALS = ROOT / "cloud_credentials.dat"
HELP_FILE = ROOT / "说明.txt"
DEFAULT_PYWECHAT = Path(r"D:\tmp\anjian\pj\st\tmp\pywechat2")
MODE_LABELS = {"auto": "自动", "list": "只截图", "detail": "打开详情"}
MODE_VALUES = {label: value for value, label in MODE_LABELS.items()}
CLOUD_TYPE_LABELS = {"none": "\u4e0d\u4e0a\u4f20", "webdav": "WebDAV", "box": "Box"}
CLOUD_TYPE_VALUES = {label: value for value, label in CLOUD_TYPE_LABELS.items()}

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
        self.active_uploaders: set[object] = set()
        self.backup_running = False
        self.backup_paused = False
        self.run_generation = 0
        self.last_hotkey_at: dict[str, float] = {}
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
        self.member_term_timeout = tk.DoubleVar(value=float(self.config_data.get("member_term_timeout", 40.0)))
        self.group_search_prefix = tk.IntVar(value=int(self.config_data.get("group_search_prefix", 4)))
        self.ocr_backend = tk.StringVar(value=str(self.config_data.get("ocr_backend", "paddle")))
        self.paddle_model = tk.StringVar(value=str(self.config_data.get("paddle_model", "mobile")))
        self.click_delay = tk.DoubleVar(value=float(self.config_data.get("click_delay", 0.06)))
        self.scroll_delay = tk.DoubleVar(value=float(self.config_data.get("scroll_delay", 0.15)))
        self.profile_delay = tk.DoubleVar(value=float(self.config_data.get("profile_delay", 0.55)))
        self.chat_open_delay = tk.DoubleVar(value=float(self.config_data.get("chat_open_delay", 1.2)))
        self.navigation_delay = tk.DoubleVar(value=float(self.config_data.get("navigation_delay", 0.45)))
        self.settings_delay = tk.DoubleVar(value=float(self.config_data.get("settings_delay", 0.55)))
        self.recent_scroll_delta = tk.IntVar(value=int(self.config_data.get("recent_scroll_delta", -120)))
        self.member_scroll_delta = tk.IntVar(value=int(self.config_data.get("member_scroll_delta", 12000)))
        self.minimize_after_start = tk.BooleanVar(value=bool(self.config_data.get("minimize_after_start", True)))
        self.group_error_policy = tk.StringVar(value=str(self.config_data.get("group_error_policy", "skip")))
        legacy_cloud_enabled = bool(self.config_data.get("cloud_enabled", False))
        saved_cloud_type = str(self.config_data.get("cloud_type", "webdav" if legacy_cloud_enabled else "none"))
        self.cloud_type = tk.StringVar(value=CLOUD_TYPE_LABELS.get(saved_cloud_type, "\u4e0d\u4e0a\u4f20"))
        self.cloud_remark = tk.StringVar(value=str(self.config_data.get("cloud_remark", "\u5fae\u4fe1\u5907\u4efd")))
        self.cloud_url = tk.StringVar(value=str(self.config_data.get("cloud_url", "")))
        self.cloud_user = tk.StringVar(value=str(self.config_data.get("cloud_user", "")))
        self.cloud_secret = tk.StringVar(value="")
        self.cloud_interval = tk.DoubleVar(value=float(self.config_data.get("cloud_interval", 1.0)))
        self.box_client_id = tk.StringVar(value=str(self.config_data.get("box_client_id", "")))
        self.box_client_secret = tk.StringVar(value="")
        self.box_redirect_uri = tk.StringVar(value=str(self.config_data.get("box_redirect_uri", "http://127.0.0.1:53682/callback")))
        self.box_target_folder = tk.StringVar(value=str(self.config_data.get("box_target_folder", "WechatRosterBackup")))
        self.hotkey_start = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_start", "Ctrl+Alt+Q")), str(self.config_data.get("hotkey_start", "Ctrl+Alt+Q"))))
        self.hotkey_pause = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_pause", "Ctrl+Alt+E")), str(self.config_data.get("hotkey_pause", "Ctrl+Alt+E"))))
        self.hotkey_stop = tk.StringVar(value=HOTKEY_MIGRATIONS.get(str(self.config_data.get("hotkey_stop", "Ctrl+Alt+S")), str(self.config_data.get("hotkey_stop", "Ctrl+Alt+S"))))
        self.hotkey_maximize = tk.StringVar(value=str(self.config_data.get("hotkey_maximize", "Ctrl+Alt+B")))
        self.hotkey_minimize = tk.StringVar(value=str(self.config_data.get("hotkey_minimize", "Ctrl+Alt+N")))
        self.skip_file = ROOT / "skip.request"
        self.stop_file = ROOT / "stop.request"
        self.pause_file = ROOT / "pause.request"
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
        self.start_button = ttk.Button(actions, text="开始选中任务", command=lambda: self.run_selected(force_restart=True))
        self.start_button.grid(row=4, column=1, sticky="e", pady=(10, 0))
        controls = ttk.Frame(actions)
        controls.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.pause_button = ttk.Button(controls, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop_backup, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="全局设置", command=self.open_global_settings).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="保存配置", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="测试识别", command=self.test_recognition).pack(side="left", padx=(8, 0))
        actions.columnconfigure(1, weight=1)

        log_frame = ttk.Frame(main)
        log_frame.pack(fill="both", expand=True, pady=(8, 4))
        self.log = tk.Text(log_frame, height=12, state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)
        ttk.Button(main, text="放大查看输出", command=self.open_log_viewer).pack(anchor="e")
        ttk.Label(main, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

    def log_text(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_log_viewer(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("输出详情")
        dialog.geometry("900x650")
        frame = ttk.Frame(dialog, padding=8)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none")
        text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self.log.get("1.0", "end"))
        text.configure(state="disabled")

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
            "member_term_timeout": self.member_term_timeout.get(),
            "group_search_prefix": self.group_search_prefix.get(),
            "ocr_backend": self.ocr_backend.get(),
            "paddle_model": self.paddle_model.get(),
            "click_delay": self.click_delay.get(),
            "scroll_delay": self.scroll_delay.get(),
            "profile_delay": self.profile_delay.get(),
            "chat_open_delay": self.chat_open_delay.get(),
            "navigation_delay": self.navigation_delay.get() if hasattr(self, "navigation_delay") else 0.45,
            "settings_delay": self.settings_delay.get() if hasattr(self, "settings_delay") else self.profile_delay.get(),
            "recent_scroll_delta": self.recent_scroll_delta.get(),
            "member_scroll_delta": self.member_scroll_delta.get(),
            "minimize_after_start": self.minimize_after_start.get(),
            "group_error_policy": self.group_error_policy.get(),
            "cloud_type": CLOUD_TYPE_VALUES.get(self.cloud_type.get(), "none"),
            "cloud_enabled": CLOUD_TYPE_VALUES.get(self.cloud_type.get(), "none") != "none",
            "cloud_remark": self.cloud_remark.get().strip(),
            "cloud_url": self.cloud_url.get().strip(),
            "cloud_user": self.cloud_user.get().strip(),
            "cloud_interval": self.cloud_interval.get(),
            "box_client_id": self.box_client_id.get().strip(),
            "box_redirect_uri": self.box_redirect_uri.get().strip(),
            "box_target_folder": self.box_target_folder.get().strip(),
            "hotkey_start": self.hotkey_start.get(),
            "hotkey_pause": self.hotkey_pause.get(),
            "hotkey_stop": self.hotkey_stop.get(),
            "hotkey_maximize": self.hotkey_maximize.get(),
            "hotkey_minimize": self.hotkey_minimize.get(),
        }
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        credentials: dict[str, str] = {}
        if self.cloud_secret.get():
            credentials["webdav_secret"] = self.cloud_secret.get()
            self.cloud_secret.set("")
        if self.box_client_secret.get():
            credentials["box_client_secret"] = self.box_client_secret.get()
            self.box_client_secret.set("")
        if credentials:
            update_secret(CLOUD_CREDENTIALS, credentials)
        self.config_data = data
        self.log_text("\u5168\u5c40\u914d\u7f6e\u5df2\u4fdd\u5b58")

    def open_global_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("全局设置")
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        rows = (("OCR 引擎", self.ocr_backend), ("PaddleOCR 模型", self.paddle_model), ("\u70b9\u51fb\u540e\u5ef6\u8fdf\uff08\u79d2\uff09", self.click_delay), ("\u6eda\u52a8\u540e\u7b49\u5f85\uff08\u79d2\uff09", self.scroll_delay), ("\u6253\u5f00\u4f1a\u8bdd\u540e\u7b49\u5f85\uff08\u79d2\uff09", self.chat_open_delay), ("\u6253\u5f00\u8d44\u6599\u5361\u540e\u5ef6\u8fdf\uff08\u79d2\uff09", self.profile_delay), ("\u8fd4\u56de/\u5bfc\u822a\u7b49\u5f85\uff08\u79d2\uff09", self.navigation_delay), ("\u8bbe\u7f6e\u9762\u677f\u7b49\u5f85\uff08\u79d2\uff09", self.settings_delay), ("\u6700\u8fd1\u4f1a\u8bdd\u6eda\u52a8\u91cf", self.recent_scroll_delta), ("\u7fa4\u6210\u5458\u6eda\u52a8\u8ddd\u79bb", self.member_scroll_delta), ("\u5355\u4e2a\u641c\u7d22\u8bcd\u8d85\u65f6\uff08\u79d2\uff09", self.member_term_timeout), ("\u7fa4\u540d\u641c\u7d22\u524d\u7f00\u5b57\u7b26\u6570", self.group_search_prefix))
        for row, (label, variable) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if variable is self.ocr_backend:
                ttk.Combobox(body, textvariable=variable, values=("tesseract", "paddle"), state="readonly", width=12).grid(row=row, column=1, padx=10, pady=4)
            elif variable is self.paddle_model:
                ttk.Combobox(body, textvariable=variable, values=("mobile", "server"), state="readonly", width=12).grid(row=row, column=1, padx=10, pady=4)
            else:
                ttk.Entry(body, textvariable=variable, width=12).grid(row=row, column=1, padx=10, pady=4)
        strategy_row = len(rows)
        ttk.Label(body, text="\u7fa4\u7b56\u7565").grid(row=strategy_row, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.member_mode, values=tuple(MODE_LABELS.values()), state="readonly", width=10).grid(row=strategy_row, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(body, text="\u81ea\u52a8 / \u53ea\u622a\u56fe / \u6253\u5f00\u8be6\u60c5").grid(row=strategy_row, column=2, sticky="w")
        ttk.Checkbutton(body, text="\u7fa4\u6210\u5458\u53ea\u622a\u56fe\uff08\u7981\u6b62\u70b9\u51fb\u8d44\u6599\u5361\uff09", variable=self.list_if_id).grid(row=strategy_row + 1, column=0, columnspan=3, sticky="w", pady=4)
        limits = (("\u8054\u7cfb\u4eba\u4e0a\u9650", self.people_limit, 100000), ("\u7fa4\u4e0a\u9650", self.group_limit, 100000), ("\u6bcf\u4e2a\u641c\u7d22\u8bcd\u9875\u6570", self.member_pages, 1000))
        limits_start = strategy_row + 2
        for row, (label, variable, maximum) in enumerate(limits, limits_start):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Spinbox(body, from_=1, to=maximum, textvariable=variable, width=12).grid(row=row, column=1, sticky="w", padx=10, pady=4)
        policy_row = limits_start + len(limits)
        ttk.Label(body, text="\u7fa4\u6210\u5458\u5931\u8d25\u7b56\u7565").grid(row=policy_row, column=0, sticky="w", pady=4)
        ttk.Combobox(body, textvariable=self.group_error_policy, values=("skip", "stop"), state="readonly", width=10).grid(row=policy_row, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(body, text="\u8df3\u8fc7\u8be5\u7fa4\u7ee7\u7eed / \u9047\u9519\u505c\u6b62").grid(row=policy_row, column=2, sticky="w")
        fold_row = policy_row + 1
        ttk.Checkbutton(body, text="\u5904\u7406\u6298\u53e0\u7684\u804a\u5929\uff08\u5176\u4e2d\u901a\u5e38\u662f\u7fa4\uff09", variable=self.task_folded_groups).grid(row=fold_row, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Checkbutton(body, text="\u5f00\u59cb\u540e\u6700\u5c0f\u5316\u5de5\u5177\u7a97\u53e3", variable=self.minimize_after_start).grid(row=fold_row + 1, column=0, columnspan=3, sticky="w", pady=4)
        hotkeys = (("\u542f\u52a8\u5feb\u6377\u952e", self.hotkey_start), ("\u6682\u505c\u5feb\u6377\u952e", self.hotkey_pause), ("\u505c\u6b62\u5feb\u6377\u952e", self.hotkey_stop), ("\u6700\u5927\u5316\u5feb\u6377\u952e", self.hotkey_maximize), ("\u6700\u5c0f\u5316\u5feb\u6377\u952e", self.hotkey_minimize))
        hotkey_start = fold_row + 2
        for row, (label, variable) in enumerate(hotkeys, hotkey_start):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=variable, width=18).grid(row=row, column=1, padx=10, pady=4)
        buttons = ttk.Frame(body)
        buttons.grid(row=hotkey_start + len(hotkeys) + 1, column=0, columnspan=3, pady=(12, 0), sticky="e")
        ttk.Button(buttons, text="保存并关闭", command=lambda: (self.save_config(), self._restart_hotkeys(), dialog.destroy())).pack(side="right")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="\u4e91\u589e\u91cf\u5907\u4efd\u8bbe\u7f6e", command=self.open_cloud_settings).pack(side="right", padx=(0, 8))

    def open_cloud_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("\u4e91\u589e\u91cf\u5907\u4efd")
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="\u4e91\u7c7b\u578b").grid(row=0, column=0, sticky="w", pady=4)
        cloud_combo = ttk.Combobox(
            body,
            textvariable=self.cloud_type,
            values=tuple(CLOUD_TYPE_LABELS.values()),
            state="readonly",
            width=18,
        )
        cloud_combo.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(body, text="\u4efb\u52a1\u5907\u6ce8").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.cloud_remark, width=48).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)
        ttk.Label(body, text="\u626b\u63cf\u95f4\u9694\uff08\u79d2\uff09").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.cloud_interval, width=12).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=4)

        provider = ttk.Frame(body)
        provider.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        webdav_frame = ttk.LabelFrame(provider, text="WebDAV", padding=10)
        box_frame = ttk.LabelFrame(provider, text="Box API", padding=10)

        webdav_fields = (
            ("WebDAV \u6839\u5730\u5740", self.cloud_url, False),
            ("\u7528\u6237\u540d", self.cloud_user, False),
            ("\u5bc6\u7801 / Token", self.cloud_secret, True),
        )
        for row, (label, variable, secret) in enumerate(webdav_fields):
            ttk.Label(webdav_frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(webdav_frame, textvariable=variable, width=48, show="*" if secret else "").grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
        webdav_frame.columnconfigure(1, weight=1)

        box_fields = (
            ("Client ID", self.box_client_id, False),
            ("Client Secret", self.box_client_secret, True),
            ("\u56de\u8c03\u5730\u5740", self.box_redirect_uri, False),
            ("Box \u76ee\u6807\u76ee\u5f55", self.box_target_folder, False),
        )
        for row, (label, variable, secret) in enumerate(box_fields):
            ttk.Label(box_frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(box_frame, textvariable=variable, width=48, show="*" if secret else "").grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
        ttk.Label(
            box_frame,
            text="\u540c\u4e00\u4e2a Box \u5e94\u7528\u53ef\u5728\u591a\u53f0\u7535\u8111\u4f7f\u7528\uff1b\u6bcf\u53f0\u7535\u8111\u9996\u6b21\u5fc5\u987b\u5206\u522b\u6d4f\u89c8\u5668\u6388\u6743\u3002",
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 4))
        box_buttons = ttk.Frame(box_frame)
        box_buttons.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(box_buttons, text="\u767b\u5f55 Box", command=self.login_box).pack(side="left")
        ttk.Button(box_buttons, text="\u5220\u9664 Box \u767b\u5f55", command=self.clear_box_login).pack(side="left", padx=8)
        box_frame.columnconfigure(1, weight=1)

        def refresh_provider(*_args: object) -> None:
            webdav_frame.pack_forget()
            box_frame.pack_forget()
            selected = CLOUD_TYPE_VALUES.get(self.cloud_type.get(), "none")
            if selected == "webdav":
                webdav_frame.pack(fill="x")
            elif selected == "box":
                box_frame.pack(fill="x")

        cloud_combo.bind("<<ComboboxSelected>>", refresh_provider)
        refresh_provider()
        ttk.Label(
            body,
            text="\u6bcf\u8f6e\u4efb\u52a1\u521b\u5efa\u72ec\u7acb\u76ee\u5f55\uff1b\u6587\u4ef6\u7a33\u5b9a\u540e\u7acb\u5373\u589e\u91cf\u4e0a\u4f20\uff0c\u4e2d\u65ad\u540e\u4fdd\u7559\u5df2\u4e0a\u4f20\u7ed3\u679c\u3002",
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="\u5220\u9664 WebDAV \u51ed\u636e", command=self.clear_webdav_login).pack(side="left")
        ttk.Button(buttons, text="\u6d4b\u8bd5\u8fde\u63a5", command=self.test_cloud_connection).pack(side="left", padx=8)
        ttk.Button(buttons, text="\u4fdd\u5b58\u5e76\u5173\u95ed", command=lambda: (self.save_config(), dialog.destroy())).pack(side="left")
        body.columnconfigure(1, weight=1)

    def _box_proxies(self) -> dict[str, str]:
        mode = self.proxy_mode.get()
        if mode in {"??", "DIRECT", "Direct"}:
            return {}
        scheme = "http" if mode == "HTTP" else "socks5h"
        value = f"{scheme}://{self.proxy_host.get().strip()}:{self.proxy_port.get().strip()}"
        return {"http": value, "https": value}

    def _box_oauth(self) -> BoxOAuthClient:
        stored = load_secret(CLOUD_CREDENTIALS)
        client_secret = self.box_client_secret.get() or stored.get("box_client_secret", "")
        return BoxOAuthClient(
            client_id=self.box_client_id.get(),
            client_secret=client_secret,
            redirect_uri=self.box_redirect_uri.get(),
            credential_path=CLOUD_CREDENTIALS,
            proxies=self._box_proxies(),
        )

    def login_box(self) -> None:
        self.save_config()
        self.status_var.set("等待 Box 浏览器授权...")

        def work() -> None:
            try:
                self._box_oauth().login(on_authorize=lambda _url: self.events.put(("log", "已打开 Box 登录页面，请授权当前账号。")))
            except Exception as error:
                self.events.put(("log", f"Box 登录失败：{error}"))
                self.events.put(("status", "Box \u767b\u5f55\u5931\u8d25"))
                return
            self.events.put(("log", "Box 登录成功，令牌已使用 Windows DPAPI 加密保存。"))
            self.events.put(("status", "Box 登录成功"))
        threading.Thread(target=work, daemon=True).start()

    def clear_box_login(self) -> None:
        delete_secret_keys(CLOUD_CREDENTIALS, "box_access_token", "box_refresh_token", "box_expires_at")
        self.log_text("已删除当前 Windows 用户保存的 Box 登录令牌。")

    def clear_webdav_login(self) -> None:
        delete_secret_keys(CLOUD_CREDENTIALS, "webdav_secret", "secret")
        self.cloud_secret.set("")
        self.log_text("已删除 WebDAV 凭据。")

    def test_cloud_connection(self) -> None:
        provider = CLOUD_TYPE_VALUES.get(self.cloud_type.get(), "none")
        try:
            if provider == "webdav":
                stored = load_secret(CLOUD_CREDENTIALS)
                secret = self.cloud_secret.get() or stored.get("webdav_secret", stored.get("secret", ""))
                uploader = IncrementalWebDavUploader(
                    ROOT / "artifacts",
                    url=self.cloud_url.get().strip(),
                    user=self.cloud_user.get().strip(),
                    secret=secret,
                    remark=self.cloud_remark.get().strip(),
                    interval=self.cloud_interval.get(),
                    proxies=self._box_proxies(),
                )
                uploader.test_connection()
                message = "WebDAV \u8fde\u63a5\u6210\u529f\u3002"
            elif provider == "box":
                uploader = IncrementalBoxUploader(
                    ROOT / "artifacts",
                    oauth=self._box_oauth(),
                    target_folder=self.box_target_folder.get(),
                    remark=self.cloud_remark.get(),
                    interval=self.cloud_interval.get(),
                )
                account = uploader.test_connection()
                account_name = account.get("name") or account.get("login") or "\u5f53\u524d\u8d26\u53f7"
                message = f"Box \u8fde\u63a5\u6210\u529f\uff1a{account_name}"
            else:
                raise ValueError("\u8bf7\u5148\u9009\u62e9 WebDAV \u6216 Box")
        except Exception as error:
            messagebox.showerror("\u4e91\u5907\u4efd", f"\u8fde\u63a5\u5931\u8d25\uff1a{error}")
            return
        messagebox.showinfo("\u4e91\u5907\u4efd", message)

    def open_help(self) -> None:
        HELP_FILE.write_text(HELP_TEXT, encoding="utf-8")
        os.startfile(str(HELP_FILE))

    def test_recognition(self) -> None:
        """Capture the current WeChat view and export raw OCR for diagnostics."""
        window = audit.select_weixin_window()
        if window is None:
            messagebox.showwarning("测试识别", "未找到可见的微信窗口。")
            return
        tesseract = open_group.resolve_tesseract(None)
        if tesseract is None and self.ocr_backend.get() == "tesseract":
            messagebox.showerror("测试识别", "未找到 Tesseract OCR。")
            return
        output = Path(tempfile.mkdtemp(prefix="wechat-ocr-"))
        image_path = output / "screen.png"
        image, _ = wx.capture_live_window(window, image_path)
        previous_backend = os.environ.get("WECHAT_OCR_BACKEND")
        previous_model = os.environ.get("WECHAT_PADDLE_MODEL")
        os.environ["WECHAT_OCR_BACKEND"] = self.ocr_backend.get()
        os.environ["WECHAT_PADDLE_MODEL"] = self.paddle_model.get()
        try:
            lines = open_group.run_ocr(tesseract or Path("tesseract"), image_path, psm=11, language="chi_sim+eng")
        finally:
            if previous_backend is None:
                os.environ.pop("WECHAT_OCR_BACKEND", None)
            else:
                os.environ["WECHAT_OCR_BACKEND"] = previous_backend
            if previous_model is None:
                os.environ.pop("WECHAT_PADDLE_MODEL", None)
            else:
                os.environ["WECHAT_PADDLE_MODEL"] = previous_model
        rows = [
            f"窗口: PID={window.get('pid')} 位置=({window.get('left')},{window.get('top')}) "
            f"大小={window.get('width')}x{window.get('height')}",
            f"OCR: {self.ocr_backend.get()} ({tesseract or f'PaddleOCR {self.paddle_model.get()} 模型'})",
            f"识别行数: {len(lines)}",
        ]
        rows.extend(
            f"{index:03d}. ({line.left},{line.top},{line.right},{line.bottom}) {line.text}"
            for index, line in enumerate(lines, 1)
        )
        self.log_text("\n".join(rows))
        image_path.unlink(missing_ok=True)
        output.rmdir()
        self.status_var.set(f"识别完成，共 {len(lines)} 行，详情已输出")

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
                (5, self.hotkey_maximize.get(), "hotkey_maximize"),
                (6, self.hotkey_minimize.get(), "hotkey_minimize"),
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
        if not self.backup_running:
            return
        if self.backup_paused:
            self.pause_file.unlink(missing_ok=True)
            self.backup_paused = False
            self.pause_button.configure(text="暂停")
            self.status_var.set("正在执行选中任务...")
            return
        self.pause_file.write_text("pause\n", encoding="ascii")
        self.backup_paused = True
        self.pause_button.configure(text="恢复")
        self.status_var.set("已暂停，可修改全局配置后按 Ctrl+Alt+Q 恢复")

    def stop_backup(self) -> None:
        self.stop_file.write_text("stop\n", encoding="ascii")
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

    def run_selected(self, *, force_restart: bool = False) -> None:
        """Start one workflow, or explicitly replace the current workflow."""
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
        if self.backup_running and not force_restart:
            self.log_text("任务正在运行，启动快捷键不会创建第二个任务。")
            return
        if force_restart and self.backup_running:
            self.log_text("正在终止旧任务并重新开始。")
            self.run_generation += 1
            self.stop_file.write_text("restart\n", encoding="ascii")
            self.pause_file.unlink(missing_ok=True)
            for process in list(self.active_processes):
                self._kill_process_tree(process)
            self.active_processes.clear()
            self.backup_running = False
            self.backup_paused = False
            self.pause_button.configure(text="暂停")
        self.save_config()
        self.stop_file.unlink(missing_ok=True)
        self.pause_file.unlink(missing_ok=True)
        remark = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", self.cloud_remark.get().strip()).strip("_") or "测试连接"
        run_name = f"{remark}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        output = ROOT / "artifacts" / "gui-workflow" / run_name
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "format": "wechat-roster-visible-backup-v1",
                    "run_name": run_name,
                    "remark": remark,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "tasks": tasks,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        uploader = None
        cloud_provider = CLOUD_TYPE_VALUES.get(self.cloud_type.get(), "none")
        if cloud_provider != "none":
            try:
                if cloud_provider == "webdav":
                    stored = load_secret(CLOUD_CREDENTIALS)
                    uploader = IncrementalWebDavUploader(
                        output,
                        url=self.cloud_url.get().strip(),
                        user=self.cloud_user.get().strip(),
                        secret=stored.get("webdav_secret", stored.get("secret", "")),
                        remark=remark,
                        remote_folder=run_name,
                        interval=self.cloud_interval.get(),
                        proxies=self._box_proxies(),
                    )
                else:
                    uploader = IncrementalBoxUploader(
                        output,
                        oauth=self._box_oauth(),
                        target_folder=self.box_target_folder.get(),
                        remark=remark,
                        remote_folder=run_name,
                        interval=self.cloud_interval.get(),
                    )
                uploader.start()
                self.active_uploaders.add(uploader)
                self.log_text(f"\u4e91\u589e\u91cf\u5907\u4efd\u5df2\u542f\u52a8\uff08{cloud_provider}\uff09\uff1a{run_name}")
            except Exception as error:
                self.log_text(f"\u4e91\u589e\u91cf\u5907\u4efd\u542f\u52a8\u5931\u8d25\uff0c\u91c7\u96c6\u4ecd\u4f1a\u7ee7\u7eed\uff1a{error}")
                uploader = None
        # While running the editable GUI from Python, always use the current
        # source runner so fixes can be tested immediately.  The bundled EXE
        # is reserved for a frozen/portable GUI distribution.
        portable_runner = ROOT / "wechat_backup_runner.exe"
        use_bundle = bool(getattr(sys, "frozen", False))
        if use_bundle and portable_runner.exists():
            interpreter = str(portable_runner)
            bundled_root = ROOT / "pywechat2"
            script_args = ["--pywechat-root", str(bundled_root)] if bundled_root.is_dir() else []
        else:
            runner_source = ROOT / "backup_runner.py"
            if getattr(sys, "frozen", False) or not runner_source.exists():
                messagebox.showerror("运行环境", "未找到 wechat_backup_runner.exe。请确认 GUI 与统一运行时位于同一目录。")
                return
            interpreter = sys.executable
            script_args = [str(runner_source), "--pywechat-root", self.root_var.get()]
        args = [interpreter, *script_args, "--workflow", "-t", ",".join(tasks), "-g", self.group_filters.get().strip(), "-k", self.member_terms.get().strip(), "-M", MODE_VALUES.get(self.member_mode.get(), "auto"), "-n", str(self.people_limit.get()), "-G", str(self.group_limit.get()), "-s", str(self.member_pages.get()), "-o", str(output)]
        env = proxy_env({"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()})
        env.update({
            "WECHAT_CLICK_DELAY": str(self.click_delay.get()),
            "WECHAT_SCROLL_DELAY": str(self.scroll_delay.get()),
            "WECHAT_PROFILE_DELAY": str(self.profile_delay.get()),
            "WECHAT_CHAT_OPEN_DELAY": str(self.chat_open_delay.get()),
            "WECHAT_NAVIGATION_DELAY": str(self.navigation_delay.get()),
            "WECHAT_SETTINGS_DELAY": str(self.settings_delay.get()),
            "WECHAT_RECENT_SCROLL_DELTA": str(self.recent_scroll_delta.get()),
            "WECHAT_MEMBER_SCROLL_DELTA": str(self.member_scroll_delta.get()),
            "WECHAT_MEMBER_TERM_TIMEOUT": str(self.member_term_timeout.get()),
            "WECHAT_GROUP_SEARCH_PREFIX": str(self.group_search_prefix.get()),
            "WECHAT_OCR_BACKEND": self.ocr_backend.get(),
            "WECHAT_PADDLE_MODEL": self.paddle_model.get(),
            "WECHAT_CONFIG_FILE": str(CONFIG),
            "WECHAT_LIST_IF_ID": "1" if self.list_if_id.get() else "0",
            "WECHAT_GROUP_ERROR_POLICY": self.group_error_policy.get(),
            "WECHAT_FOLDED_GROUPS": "1" if self.task_folded_groups.get() else "0",
            "WECHAT_SKIP_FILE": str(self.skip_file),
            "WECHAT_STOP_FILE": str(self.stop_file),
            "WECHAT_PAUSE_FILE": str(self.pause_file),
        })
        self.status_var.set("正在执行选中任务…")
        self.backup_running = True
        self.backup_paused = False
        self.run_generation += 1
        generation = self.run_generation
        self.pause_button.configure(text="暂停")
        self.start_button.state(["disabled"])
        self.pause_button.state(["!disabled"])
        self.stop_button.state(["!disabled"])
        try:
            process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="gb18030", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            if uploader is not None:
                uploader.stop()
                self.active_uploaders.discard(uploader)
            self.backup_running = False
            self.backup_paused = False
            self.status_var.set(f"启动失败: {error}")
            self.start_button.state(["!disabled"])
            self.pause_button.state(["disabled"])
            self.stop_button.state(["disabled"])
            self.pause_button.configure(text="暂停")
            return
        self.active_processes.add(process)
        def work() -> None:
            try:
                output_text, _ = process.communicate()
                self.events.put(("log", output_text))
                self.events.put(("status", "备份完成" if process.returncode == 0 else "备份失败"))
            finally:
                self.active_processes.discard(process)
                if uploader is not None:
                    uploader.stop()
                    self.active_uploaders.discard(uploader)
                    errors = []
                    while not uploader.errors.empty():
                        errors.append(uploader.errors.get_nowait())
                    if errors:
                        self.events.put(("log", "云上传未完成文件：\n" + "\n".join(errors[-20:])))
                    else:
                        self.events.put(("log", f"云增量备份已完成：{uploader.remote_folder}"))
                self.events.put(("backup_done", generation))
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
        for uploader in list(self.active_uploaders):
            uploader.stop()
        self.active_uploaders.clear()
        self.destroy()

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind.startswith("hotkey_"):
                    now = time.monotonic()
                    previous = self.last_hotkey_at.get(kind, 0.0)
                    if now - previous < 0.35:
                        continue
                    self.last_hotkey_at[kind] = now
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
                    if value != self.run_generation:
                        continue
                    self.backup_running = False
                    self.backup_paused = False
                    self.pause_file.unlink(missing_ok=True)
                    self.start_button.state(["!disabled"])
                    self.pause_button.state(["disabled"])
                    self.stop_button.state(["disabled"])
                    self.pause_button.configure(text="暂停")
                elif kind == "hotkey_start":
                    if self.backup_running and (self.backup_paused or self.pause_file.exists()):
                        self.backup_paused = True
                        self.toggle_pause()
                    elif not self.backup_running:
                        self.pause_file.unlink(missing_ok=True)
                        self.run_selected()
                    else:
                        self.log_text("任务正在运行，启动快捷键不会创建第二个任务。")
                elif kind == "hotkey_pause":
                    self.toggle_pause()
                elif kind == "hotkey_stop":
                    self.stop_backup()
                elif kind == "hotkey_skip":
                    self.skip_current()
                elif kind == "hotkey_maximize":
                    self.deiconify()
                    self.state("normal")
                    self.lift()
                    self.attributes("-topmost", True)
                    self.after(150, lambda: self.attributes("-topmost", False))
                elif kind == "hotkey_minimize":
                    self.iconify()
        except queue.Empty:
            pass
        self.after(150, self._poll)


if __name__ == "__main__":
    App().mainloop()
