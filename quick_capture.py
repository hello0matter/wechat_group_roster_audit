"""One-click capture of the calibrated Weixin right-side panel."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import wechat_group_roster_audit as audit


def configured_pid(path: Path) -> tuple[int | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["target_pid"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None, "invalid"
    return (pid, "matched") if pid > 0 else (None, "invalid")


def timestamped_output(directory: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"right-panel-{timestamp}.png"


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the calibrated Weixin panel")
    parser.add_argument("--config", type=Path, default=audit.DEFAULT_PANEL_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--source",
        choices=("window", "auto"),
        default="window",
        help="window requires PrintWindow; auto may fall back to desktop capture",
    )
    args = parser.parse_args()

    pid, config_status = configured_pid(args.config)
    if pid is None:
        if config_status == "missing":
            print(f"缺少校准配置 {args.config}。请先运行 --calibrate-panel。")
        else:
            print(f"校准配置 {args.config} 已损坏或 target_pid 无效。")
        return 2

    panel, panel_status = audit.read_panel_config(args.config, pid)
    if panel is None:
        print(f"校准配置 {args.config} 中的面板范围无效（{panel_status}）。")
        return 2

    window = audit.select_weixin_window(pid)
    if window is None:
        print(
            f"配置中的微信 PID {pid} 已不存在。请运行 "
            "wechat_group_roster_audit.py --list-windows，选择当前 PID 后重新校准。"
        )
        return 2

    activation = audit.activate_window(window)
    if not activation["activated"]:
        print(json.dumps({"activation": activation}, ensure_ascii=False, indent=2))
        return 2

    output = timestamped_output(args.output_dir)
    try:
        capture = audit.capture_panel(
            activation["window"],
            output,
            panel,
            args.source,
        )
    except RuntimeError as error:
        print(f"截图失败: {error}")
        return 2

    print(
        json.dumps(
            {
                "captured": True,
                "target_pid": pid,
                "panel_config": str(args.config.resolve()),
                "capture": capture,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
