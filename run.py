# -*- coding: utf-8 -*-
"""源码一键启动：省掉「记得先进项目目录、记得用 .venv 里那个 python」。

    python run.py            # 命令行里跑（双击 run.py 也一样）
    python run.py --check    # 只检查解释器和依赖，不启动程序

三件事：
1) 用别的解释器起（系统的 python、IDE 里随便挑了一个）就换项目里的 .venv 重来一遍——
   这个项目的依赖装在 .venv 里，拿系统 python 直接跑 main.py 只会 ImportError；
2) 起来前先探一眼依赖，缺了就把要装的命令打出来（不自动装，免得动你的环境）；
3) main.py 的退出码原样传回去；出错时停一下，双击启动也能看清报错。

要跑打包好的 exe 就别用这个：直接开 run\\jev-chat-windows.exe（或 dist 里那份）。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(ROOT, "main.py")
_SELF = os.path.abspath(__file__)
# pip 包名 → 导入名不一样的几个，这里按导入名探
_DEPS = ("PySide6", "qfluentwidgets", "numpy", "rapidocr_onnxruntime",
         "windows_capture", "openai", "typesafe_sdk", "anthropic", "google.genai")
_VENV_PYTHON = os.path.join(ROOT, ".venv", "Scripts" if os.name == "nt" else "bin",
                            "python.exe" if os.name == "nt" else "python")


def _missing() -> list[str]:
    """缺哪些依赖。找不到的模块名原样报出来，方便对着 requirements.txt 看。"""
    out = []
    for name in _DEPS:
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
        except (ImportError, ValueError):  # 父包坏了之类，也算缺
            out.append(name)
    return out


def _venv_python() -> str | None:
    """项目里的 .venv 解释器；没有（或者就是它本身）返回 None。"""
    if not os.path.exists(_VENV_PYTHON):
        return None
    same = os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(_VENV_PYTHON)
    return None if same else _VENV_PYTHON


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    rest = [a for a in argv if a != "--check"]

    if not os.path.exists(MAIN):
        print(f"找不到 {MAIN}——run.py 要放在项目根目录（和 main.py 同一层）。")
        return 2

    python = _venv_python()
    if python:
        print(f"换成项目里的解释器重来一遍：{python}")
        return subprocess.call([python, _SELF, *argv], cwd=ROOT)

    missing = _missing()
    print(f"解释器：{sys.executable}")
    if missing:
        print("缺少依赖：" + "、".join(missing))
        print("装一下再跑：")
        print(f'  "{sys.executable}" -m pip install -r "{os.path.join(ROOT, "requirements.txt")}"')
        return 1
    print("依赖齐全。")
    if check_only:
        return 0

    print("启动中…（关掉助手的窗口就是退出）")
    return subprocess.call([sys.executable, MAIN, *rest], cwd=ROOT)


def _pause(code: int) -> None:
    """双击启动时控制台会在程序退出后立刻关掉，报错就看不见了——出错才停一下。"""
    if not code or not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input("\n出错了，按回车关闭…")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    _code = main(sys.argv[1:])
    _pause(_code)
    raise SystemExit(_code)
