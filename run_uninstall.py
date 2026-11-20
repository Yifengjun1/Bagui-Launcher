# -*- coding: utf-8 -*-
"""卸载程序入口（第五十一轮）。

装进 `<安装目录>\\卸载八桂启动器.exe`，和主程序并排。
和主程序一样不提权：要删的东西都在当前用户自己的目录里。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.uninstaller import main  # noqa: E402

if __name__ == "__main__":
    main()
