# -*- coding: utf-8 -*-
"""安装向导入口

和主程序一样**不再自动提权**（第四十三轮）：默认装到
`%LOCALAPPDATA%\\Programs\\Osmanthus flower`，快捷方式写用户桌面 / 用户开始菜单，
全程不需要管理员。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.setup_wizard import main  # noqa: E402

if __name__ == "__main__":
    main()
