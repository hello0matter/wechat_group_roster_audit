"""Unified UIA/OCR backend used by the portable GUI."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout


def invoke(module, argv: list[str]) -> tuple[int, str]:
    old_argv = sys.argv
    output = io.StringIO()
    sys.argv = [module.__file__ or module.__name__, *argv]
    try:
        with redirect_stdout(output):
            code = int(module.main())
    finally:
        sys.argv = old_argv
    return code, output.getvalue()


def main() -> int:
    groups = "--groups" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "--backend"]
    if "--ocr" in args:
        args.remove("--ocr")
        import wx

        return invoke(wx, ["-m", "saved", "-n", *args])[0]

    import uia_backup

    code, output = invoke(uia_backup, args)
    print(output, end="")
    if code == 0:
        return code
    if '"reason": "pywechat2_unavailable"' not in output:
        return code

    import wx

    # OCR supports both saved groups and the regular Contacts list.
    # Contact details need OCR to locate each row; saved-group fallback remains
    # screenshot-only because group member extraction is intentionally paused.
    fallback_args = ["-m", "saved", "-n"] if groups else ["-m", "chat", "-f"]
    for index, arg in enumerate(args):
        if arg in {"-s", "--limit", "-o", "--output"} and index + 1 < len(args):
            fallback_args.extend(["-s" if arg in {"-s", "--limit"} else "-o", args[index + 1]])
    try:
        fallback_code, fallback_output = invoke(wx, fallback_args)
        print("UIA 不可见，已自动切换 OCR\n" + fallback_output, end="")
        return fallback_code
    except Exception as error:
        print(
            "UIA 不可见，OCR 回退也失败\n"
            + __import__("json").dumps(
                {"ok": False, "reason": "ocr_fallback_failed", "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
