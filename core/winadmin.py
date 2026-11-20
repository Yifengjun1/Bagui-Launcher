# -*- coding: utf-8 -*-
"""Windows 管理员权限助手（**不再自动提权**）。

第一版是把 `requireAdministrator` 焊进 exe 清单、再加运行时
`ShellExecuteW("runas")` 兜底 —— 等于让整个软件一直跑在最高权限下。
用户第四十三轮提出「对电脑有安全风险」，已全部撤掉：

  · 打包不再带 `--uac-admin`（见 `tools/build.py`）；
  · `run.py` / `run_setup.py` 不再调用自提权；
  · 需要写「受保护位置」的功能一律改走**用户级**，不再要求提权。

实测过：本软件真正需要的操作都不需要管理员 ——
`mklink /J`（目录联接）普通用户就能建；快捷方式写在 `%USERPROFILE%\\Desktop` 与
`%APPDATA%\\Microsoft\\Windows\\Start Menu`；配置 / Java / 游戏目录全在 `%APPDATA%`、
`%LOCALAPPDATA%`；`launcher_profiles.json` 在玩家自己的游戏目录里。
唯一「以前靠管理员」的两处也已改成用户级：Java 装到
`%LOCALAPPDATA%\\Programs\\Osmanthus flower\\Java`；`JAVA_HOME` 与 `PATH` 写
`HKCU\\Environment`（只影响当前用户，不动系统环境变量）。

保留 `is_admin()` 只用于**读当前环境的权限等级**（决定写 HKLM 还是 HKCU、
允许时优先用系统目录里已有的 Java），不参与任何提权动作。
"""
import os


def os_name_nt() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    """当前进程是否具备管理员权限（只用来选「用户级」还是「系统级」写法）"""
    if not os_name_nt():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_admin_or_restart() -> bool:
    """**已废弃**：不再以管理员重启自身（第四十三轮）。

    留着这个名字只为兼容老调用点：恒返回当前是否管理员，绝不做提权动作。
    新代码不要再用它。
    """
    return is_admin()
