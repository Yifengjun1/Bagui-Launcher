# -*- coding: utf-8 -*-
"""打包入口：python run.py 直接运行

注意：**不再自动提权**（第四十三轮）。以前这里会调 `ensure_admin_or_restart()`
把整个进程拉到管理员，用户提出对电脑有安全风险，已撤掉；需要系统级写权限的
功能都改成用户级（%APPDATA% / %LOCALAPPDATA% / HKCU）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
