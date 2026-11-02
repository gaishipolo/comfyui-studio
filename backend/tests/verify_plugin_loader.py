"""引擎插件 live 验证入口。

import 本模块只定义 main，不登录、不建号、不起假服务；实际破坏性流程只在直接执行时加载。
"""
import runpy
from pathlib import Path


def main() -> None:
    from live_guard import require_live_ok

    require_live_ok()
    runpy.run_path(
        str(Path(__file__).with_name("_verify_plugin_loader_live.py")),
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
