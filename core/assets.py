# -*- coding: utf-8 -*-
"""资源文件定位：源码运行与 PyInstaller 单文件（frozen）都适用。

打包时用 ``--add-data`` 把 ``app/assets`` 塞进去（见 tools/build.py），
运行时 PyInstaller 会把它解到 ``sys._MEIPASS/app/assets``；源码运行就是
``app/assets``。两处路径不同，所以统一从这里取，别在别处硬拼。
"""
import os
import sys


def assets_root() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "") or os.path.dirname(sys.executable)
        return os.path.join(base, "app", "assets")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets")


def asset(*parts) -> str:
    return os.path.join(assets_root(), *parts)


def asset_exists(*parts) -> bool:
    try:
        return os.path.isfile(asset(*parts))
    except Exception:
        return False
