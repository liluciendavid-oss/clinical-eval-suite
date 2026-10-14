#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
console.py — 跨平台输出编码处理

为什么需要这个文件
------------------
本项目会在三种环境下运行：作者的 Windows（默认代码页 cp936/GBK）、
面试官可能 clone 后运行的 macOS/Linux（UTF-8）、以及 GitHub Actions（UTF-8）。

Windows 的 GBK 代码页无法编码 `⚠ ✔ →` 这类符号，直接 print 会抛
UnicodeEncodeError 让整个脚本崩溃——**一个纯显示问题不应该让评测流水线挂掉**。

因此统一约定：
  1. 所有脚本入口第一行调用 `console.setup()`，把 stdout/stderr 切到 UTF-8；
  2. 切换失败时退化为 `errors="replace"`，保证永不崩溃；
  3. 正文中的状态标记一律用 ASCII 安全形式：`[OK]` `[!]` `[x]` `[->]`，
     这样即使在无法重配置编码的老旧 cmd 里也完整可读。

在旧版 Windows cmd 中若仍看到中文乱码，执行 `chcp 65001` 即可。
"""

from __future__ import annotations

import io
import sys

# ASCII 安全的状态标记（不要用 emoji / 装饰性 Unicode）
OK = "[OK]"
WARN = "[!]"
FAIL = "[x]"
ARROW = "[->]"
BULLET = "-"


def setup() -> None:
    """把标准流切到 UTF-8。幂等，可在任何脚本入口安全调用。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif hasattr(stream, "buffer"):
                wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                           errors="replace", line_buffering=True)
                if stream is sys.stdout:
                    sys.stdout = wrapped
                else:
                    sys.stderr = wrapped
        except Exception:
            # 编码重配置失败不该终止评测流程；退化为 ASCII 转义输出
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass


def encoding_report() -> str:
    """返回当前流的编码，写进 manifest 便于复现者核对环境。"""
    return f"stdout={getattr(sys.stdout, 'encoding', '?')} stderr={getattr(sys.stderr, 'encoding', '?')}"
