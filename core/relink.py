# -*- coding: utf-8 -*-
"""游戏目录 mklink 迁移：
把 %APPDATA%\\.minecraft 的真实数据搬到其他盘，原位置留 NTFS junction（mklink /J）。
官方启动器仍从原路径读写，但磁盘空间实际占用在目标盘（C 盘只剩一个链接，几乎为 0）。
mklink /J 为目录联接：无需开发者模式，支持跨本地分区；不影响商店版（其目录在
LOCALAPPDATA Packages 下，迁移需以同样方式对其操作）。
"""
import os
import subprocess

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
LAUNCHER_PROCESSES = ["Minecraft.exe", "MinecraftLauncher.exe",
                      "Minecraft Launcher.exe", "gamelaunchhelper.exe"]


def is_junction(path) -> bool:
    """reparse point（junction/symlink）检测。注意 os.path.islink 对 junction 返回 False。"""
    if not path or not os.path.exists(path):
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def link_target(path) -> str:
    """返回链接的真实目标；普通目录返回 ''"""
    try:
        real = os.path.realpath(str(path))
        return real if os.path.normcase(real) != os.path.normcase(str(path)) else ""
    except Exception:
        return ""


def launcher_running() -> list:
    """正在运行的官方启动器相关进程名列表。

    第九十八轮：以前只会起 `tasklist` 子进程，实测 **1.68 秒一次**（进程多的机器
    更慢）。现在先走 installer 里的原生快照（Toolhelp，~20ms），失败才退回 tasklist。
    """
    names = set()
    try:
        from .installer import _proc_names_toolhelp      # 函数内 import：避免循环引用
        names = _proc_names_toolhelp()
    except Exception:                                     # noqa: BLE001
        names = set()
    if names:
        want = {p.lower() for p in LAUNCHER_PROCESSES}
        return [p for p in LAUNCHER_PROCESSES if p.lower() in names]
    running = []
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=15, encoding="gbk", errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout or ""
        low = out.lower()
        running = [p for p in LAUNCHER_PROCESSES if '"' + p.lower() + '"' in low]
    except Exception:
        pass
    return running


def commands(src: str, dst: str) -> str:
    """命令提示符手动迁移命令（展示 + 一键复制用）"""
    return (
        f'rem 1. 退出官方启动器后，把数据搬到目标盘\r\n'
        f'robocopy "{src}" "{dst}" /E /MOVE /COPY:DAT /DCOPY:DAT /R:1 /W:1\r\n'
        f'rem 2. 原位置创建目录联接（数据实际存目标盘）\r\n'
        f'mklink /J "{src}" "{dst}"'
    )


def migrate(src: str, dst: str, progress=None) -> tuple[bool, str]:
    """阻塞执行迁移（请放工作线程调用）：
    1. 校验：源不是链接、目标非空、官方启动器已退出
    2. robocopy /E /MOVE 把数据搬到 dst
    3. mklink /J 在 src 原位建联接
    progress(frac 0~1, text)；返回 (ok, message)
    """
    def cb(f, t):
        if progress:
            progress(f, t)

    if is_junction(src):
        return False, "该目录已经是链接，无需重复迁移"
    dst = os.path.abspath(dst)
    src = os.path.abspath(src)
    if os.path.normcase(src) == os.path.normcase(dst):
        return False, "目标目录不能与当前目录相同"
    if os.path.isdir(dst) and os.listdir(dst):
        return False, f"目标目录非空，请选择空目录：{dst}"
    running = launcher_running()
    if running:
        return False, "请先退出官方启动器（检测到：" + "、".join(running) + "）"

    try:
        os.makedirs(dst, exist_ok=True)
    except Exception as e:
        return False, f"无法创建目标目录：{e}"

    if os.path.isdir(src):
        cb(0.05, "正在搬移游戏数据（文件多时需要几分钟）…")
        try:
            rc = subprocess.run(
                ["robocopy", src, dst, "/E", "/MOVE", "/COPY:DAT", "/DCOPY:DAT",
                 "/R:1", "/W:1", "/NFL", "/NDL", "/NP", "/NJH"],
                capture_output=True, text=True, timeout=4 * 3600,
                encoding="gbk", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            return False, f"数据搬移失败：{e}"
        # robocopy 退出码 0~7 算成功（8 及以上有失败项）
        if rc.returncode > 7:
            return False, f"数据搬移失败（robocopy 退出码 {rc.returncode}），目标目录保留已完成部分"
        cb(0.85, "创建目录链接（mklink /J）…")

    if os.path.exists(src):
        try:
            os.rmdir(src)  # /MOVE 后应只剩空壳；非空会抛 OSError 由下面捕获
        except OSError as e:
            return False, f"原目录无法清空（可能有文件被占用）：{e}"

    try:
        # 注意：必须用拆分参数形式 —— cmd /c 后接整串引号会被 cmd 剥引号导致"语法不正确"
        rc = subprocess.run(["cmd", "/c", "mklink", "/J", src, dst],
                            capture_output=True, text=True,
                            encoding="gbk", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return False, f"创建链接失败：{e}"
    if rc.returncode != 0:
        return False, "创建链接失败：" + ((rc.stdout or "") + (rc.stderr or "")).strip()

    cb(1.0, "迁移完成")
    ok = is_junction(src) and os.path.isdir(dst)
    return (ok, "迁移完成" if ok else "迁移完成，但校验未通过，请检查目录")
