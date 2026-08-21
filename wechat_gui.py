"""Desktop control panel for the optional pywechat2 UIA backup workflow."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG = ROOT / "gui_config.json"
DEFAULT_PYWECHAT = Path(r"D:\tmp\anjian\pj\st\tmp\pywechat2")


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
        self.geometry("760x520")
        self.minsize(700, 460)
        self.config_data = load_config()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.active_processes: set[subprocess.Popen[str]] = set()
        self.backup_running = False
        bundled_root = ROOT / "pywechat2"
        default_root = bundled_root if bundled_root.is_dir() else DEFAULT_PYWECHAT
        self.root_var = tk.StringVar(value=str(self.config_data.get("pywechat_root", default_root)))
        self.proxy_mode = tk.StringVar(value=str(self.config_data.get("proxy_mode", "SOCKS5")))
        self.proxy_host = tk.StringVar(value=str(self.config_data.get("proxy_host", "127.0.0.1")))
        self.proxy_port = tk.StringVar(value=str(self.config_data.get("proxy_port", "7891")))
        self.version_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(150, self._poll)
        self.refresh_versions()

    def _build(self) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="pywechat2 UIA 备份", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")

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
        self.contacts_button = ttk.Button(actions, text="备份联系人截图", command=lambda: self.run_backup(False))
        self.contacts_button.pack(side="left", padx=(0, 8))
        self.groups_button = ttk.Button(actions, text="群成员详情（暂未启用）", state="disabled")
        self.groups_button.pack(side="left")
        ttk.Label(actions, text="数量上限").pack(side="left", padx=(20, 5))
        self.limit = tk.IntVar(value=10)
        ttk.Spinbox(actions, from_=1, to=500, textvariable=self.limit, width=7).pack(side="left")

        self.log = tk.Text(main, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 4))
        ttk.Label(main, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

    def log_text(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save_config(self) -> None:
        data = {"pywechat_root": self.root_var.get().strip(), "proxy_mode": self.proxy_mode.get(), "proxy_host": self.proxy_host.get().strip(), "proxy_port": self.proxy_port.get().strip()}
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.config_data = data
        self.log_text("代理和路径配置已保存")

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

    def run_backup(self, groups: bool) -> None:
        if self.backup_running:
            self.log_text("已有备份任务正在运行，请等待完成或关闭窗口终止任务。")
            return
        self.save_config()
        output = ROOT / "artifacts" / ("gui-saved-groups" if groups else "gui-contacts")
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
        args = [interpreter, *script_args, "-s", str(self.limit.get()), "-o", str(output)]
        if groups:
            args.append("--groups")
        self.status_var.set("正在执行 UIA 备份...")
        self.backup_running = True
        self.contacts_button.state(["disabled"])
        self.groups_button.state(["disabled"])
        def work() -> None:
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                env=proxy_env({"mode": self.proxy_mode.get(), "host": self.proxy_host.get(), "port": self.proxy_port.get()}),
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

    def close_app(self) -> None:
        """Stop child runners before closing so no automation remains behind."""
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
                    self.contacts_button.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(150, self._poll)


if __name__ == "__main__":
    App().mainloop()
