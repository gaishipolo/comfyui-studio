"""破坏性 live 脚本的「目标可丢弃」自检。

会打真实实例 / 改真实库 / 杀进程的脚本（见 test_live_guard.py 的清单）在
做任何 I/O 之前必须调用 require_live_ok()。只有显式设置
COMFYUI_STUDIO_LIVE_TESTS_OK=1 才放行。

为什么：这些脚本硬编码 admin/admin123 且默认指向 127.0.0.1:8000（恰好也是
生产端口）。对着「恰好还是默认密码」的在用实例跑一遍，就是生产数据事故。
靠 README 警告不如靠脚本自己拒绝执行。
"""
import os
import sys
from pathlib import Path

ENV_FLAG = "COMFYUI_STUDIO_LIVE_TESTS_OK"


def require_live_ok() -> None:
    if os.environ.get(ENV_FLAG, "").strip() != "1":
        script = Path(sys.argv[0]).name if sys.argv else "<script>"
        sys.exit(
            f"[{script}] 这是破坏性/live 脚本：会打真实实例并修改真实数据。\n"
            f"请先确认目标是一次性测试实例，然后设置 {ENV_FLAG}=1 再运行：\n"
            f"  PowerShell:  $env:{ENV_FLAG}='1'; python tests/{script}\n"
            f"  cmd/bash:    set {ENV_FLAG}=1 && python tests/{script}\n"
            f"（未设置时拒绝执行，防止误伤生产数据）"
        )
