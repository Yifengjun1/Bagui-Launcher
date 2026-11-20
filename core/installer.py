# -*- coding: utf-8 -*-
"""安装逻辑：下载内容自动安排到微软官方启动器的游戏目录，并注册档案。
- 模组    -> <游戏目录>/mods
- 光影    -> <游戏目录>/shaderpacks
- 材质包  -> <游戏目录>/resourcepacks
- 整合包  -> 独立实例目录 + 写入 launcher_profiles.json 档案（官方启动器可直接识别）
             Fabric 加载器可自动下载版本 JSON；Forge/NeoForge 提示一次安装器。
"""
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from urllib.parse import quote

import requests

from .config import CONFIG, app_data_dir
from .modrinth import download_to
from .relink import LAUNCHER_PROCESSES as _LAUNCHER_PROCESSES
from .winadmin import is_admin  # noqa: F401  （保留导出：老调用点/测试会切换它判断权限分支）
from . import packicon, runtime

UA = "MC-GuanQi-Fuzhu-Gongju/1.0.0"

STORE_LAUNCHER_AUMID = r"Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"
# 实测（Get-StartApps）：开始菜单里 Java 版启动器的 AppID 是 …!Minecraft，
# 旧版本才是 …!App，两个都试一遍。
AUMID_CANDIDATES = [
    r"Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft",
    r"Microsoft.4297127D64EC6_8wekyb3d8bbwe!App",
]
# 经典版启动器可执行文件名：老安装包 MinecraftLauncher.exe、
# 新安装包 Minecraft Launcher.exe、Xbox/商店版 Minecraft.exe（三者都识别）
LAUNCHER_EXE_NAMES = ["MinecraftLauncher.exe", "Minecraft Launcher.exe", "Minecraft.exe"]
# 启动器真正的进程名：Xbox/GDK 版直启后出现的是 gamelaunchhelper.exe（不是 Minecraft.exe），
# 只盯 exe 自己的名字会误判「没起来」→ 走兜底 → 弹出文件夹窗口。
LAUNCHER_PROC_NAMES = {p.lower() for p in _LAUNCHER_PROCESSES}
LAUNCHER_DIRS = [
    r"C:\Program Files (x86)\Minecraft Launcher",
    r"C:\Program Files\Minecraft Launcher",
]
STORE_GAME_DIR = os.path.expandvars(
    r"%LOCALAPPDATA%\Packages\Microsoft.4297127D64EC6_8wekyb3d8bbwe\LocalCache\Roaming\.minecraft")


# ---------------------------------------------------------------- 官启
def find_launcher_exe():
    """返回 (kind, value)：kind in {'exe','aumid'}"""
    custom = CONFIG.get("launcher_path")
    if custom and os.path.isfile(custom):
        return "exe", custom
    for d in LAUNCHER_DIRS:
        for exe in LAUNCHER_EXE_NAMES:
            p = os.path.join(d, exe)
            if os.path.isfile(p):
                return "exe", p
    # 通过注册表卸载信息找经典启动器（安装位置可能自定义）
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    k = winreg.OpenKey(root, sub)
                except OSError:
                    continue
                with k:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(k, i)
                            i += 1
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(k, name) as sk:
                                dn, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if "Minecraft" in str(dn) and "Launcher" in str(dn):
                                    try:
                                        ic, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                    except OSError:
                                        continue
                                    for exe in LAUNCHER_EXE_NAMES:
                                        exe_path = os.path.join(str(ic), exe)
                                        if os.path.isfile(exe_path):
                                            return "exe", exe_path
                                    # DisplayIcon 可能直接指向 exe
                                    try:
                                        icon, _ = winreg.QueryValueEx(sk, "DisplayIcon")
                                        icon = str(icon).split(",")[0].strip()
                                        if icon.lower().endswith(".exe") and os.path.isfile(icon):
                                            return "exe", icon
                                    except OSError:
                                        pass
                        except OSError:
                            continue
    except Exception:
        pass
    # 商店版（Xbox）：目录带版本号且受 ACL 保护无法枚举，用 AUMID 启动最可靠
    return "aumid", STORE_LAUNCHER_AUMID


AUXILIARY_EXE_NAMES = {"gamelaunchhelper.exe", "uninstall.exe", "uninst.exe", "update.exe"}


def resolve_main_exe(path: str) -> str:
    """把用户选中的启动器路径规范化为真正的启动器主程序。
    常见误区：选到安装目录里的辅助程序（如 Minecraft Launcher\\Content\\gamelaunchhelper.exe），
    直接启动辅助程序会失败（甚至触发资源管理器窗口），需换用同目录的主程序。"""
    if not path:
        return path
    if os.path.basename(path).lower() in AUXILIARY_EXE_NAMES:
        d = os.path.dirname(path)
        # 优先同目录主程序，其次上一级
        for base in (d, os.path.dirname(d)):
            for name in LAUNCHER_EXE_NAMES:
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    return p
    return path


def _proc_names_toolhelp() -> set:
    """用 Windows 原生 API（Toolhelp 快照）一次拿到所有进程名（小写）。

    第九十八轮：`_all_processes` 以前只会起一个 `tasklist` 子进程。这台机器上
    实测 **1679ms / 次**，而 `_write_profiles()` 前后各查一次 → 3.4 秒。
    玩家点完「保存」返回主界面就卡这一下 —— 这就是那条「设置保存后返回页面
    卡一下」。原生快照不开子进程，实测 3~8ms。

    拿不到（非 Windows / API 失败）就返回空集合，调用方自己退回 tasklist。
    """
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        class _PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        TH32CS_SNAPPROCESS = 2
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        # INVALID_HANDLE_VALUE 在 64 位上是 0xFFFFFFFFFFFFFFFF，别拿 -1 比
        if not snap or snap == wintypes.HANDLE(-1).value:
            return set()
        out = set()
        try:
            pe = _PE32()
            pe.dwSize = ctypes.sizeof(_PE32)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                out.add(str(pe.szExeFile).lower())
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return out
    except Exception:                                      # noqa: BLE001
        return set()


def _all_processes() -> set:
    """所有进程名（小写）。原生快照优先，失败才退回 tasklist。"""
    names = _proc_names_toolhelp()
    if names:
        return names
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
            encoding="gbk", errors="replace", timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return set()
    names = set()
    for line in out.splitlines():
        m = re.match(r'"([^"]+)"', line.strip())
        if m:
            names.add(m.group(1).lower())
    return names


def launcher_running() -> bool:
    """官方启动器（含 Xbox/GDK 版的 gamelaunchhelper）是否在运行"""
    return bool(_all_processes() & LAUNCHER_PROC_NAMES)


def _wait_launcher(timeout: float) -> bool:
    import time
    end = time.time() + max(timeout, 0.5)
    while time.time() < end:
        if launcher_running():
            return True
        time.sleep(0.4)
    return False


def _launch_aumid(aumid: str):
    """用 AppsFolder 激活商店/GDK 版（不传文件路径 → 不会弹出资源管理器窗口）"""
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + aumid],
                     creationflags=subprocess.DETACHED_PROCESS)


def _discover_aumid(hint: str = "Minecraft Launcher") -> str:
    """从开始菜单应用列表里找真实的 AppID"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        for line in (r.stdout or "").splitlines():
            if "|" not in line:
                continue
            name, aid = line.split("|", 1)
            if hint.lower() in name.lower() and aid.strip():
                return aid.strip()
    except Exception:
        pass
    return ""


def _aumid_candidates(with_discovery: bool = False) -> list:
    out = []
    custom = str(CONFIG.get("launcher_aumid") or "").strip()
    if custom:
        out.append(custom)
    out.extend(AUMID_CANDIDATES)
    if with_discovery:
        d = _discover_aumid()
        if d and d not in out:
            out.append(d)
    return out


def _shell_start(path: str):
    """ShellExecute 打开（不会弹资源管理器窗口）"""
    os.startfile(path)  # noqa: S606


def launch_official_launcher():
    """启动官方启动器（失败时把输入法切回去）。

    内部实现见 _launch_official_inner；这里只包一层「没起来就别把玩家的默认
    输入语言留在 en-US」。
    """
    ok, msg = _launch_official_inner()
    if not ok:
        try:
            from . import ime
            ime.restore()
        except Exception:
            pass
    return ok, msg


def _launch_official_inner():
    """启动官方启动器。返回 (ok, 描述/错误信息)。

    踩过的坑：
    1) Xbox/GDK 版直启后真正的进程是 gamelaunchhelper.exe，只按 exe 名字判断会误判失败；
    2) 曾经用 `explorer.exe <exe 路径>` 兜底——**本程序以管理员运行时**，Explorer 拒绝
       替提权进程执行 exe，只会把所在文件夹打开，用户看到的就是「文件管理器跟着打开」。
       现在一律不再把文件路径交给 explorer，商店版只走 shell:AppsFolder。
    """
    kind, v = find_launcher_exe()
    if kind == "aumid":
        try:
            from . import ime
            ime.launch_switch()      # 官启接着要拉游戏：默认输入语言先切成英文
        except Exception:
            pass
        return _launch_store_app(v)

    exe = resolve_main_exe(v)
    try:
        saved = CONFIG.get("launcher_path")
        if saved and os.path.normcase(saved) != os.path.normcase(exe):
            CONFIG.set("launcher_path", exe)
    except Exception:
        pass
    cwd = os.path.dirname(exe) or None
    name = os.path.basename(exe)

    if launcher_running():
        return True, "官方启动器（已在运行）"
    try:
        from . import ime
        ime.launch_switch()          # 官启接着要拉游戏：默认输入语言先切成英文
    except Exception:
        pass
    # 自动中文（第五十五轮）：官启这条路由它自己读 options.txt，我们趁启动前
    # 先把 lang 写成 zh_cn —— 只动这一行，其它键位/音量原样保留。
    try:
        from . import langfix
        from . import paths as _p
        for _gd in {_p.official_game_dir(), detect_game_dir()}:
            if _gd and os.path.isdir(_gd):
                langfix.apply(_gd)
    except Exception:
        pass

    # 商店 / GDK 安装（Xbox 应用装的 D:\Xbox\...\Content\Minecraft.exe 就是这种）：
    # 用 AUMID 激活最稳 —— 直接 Popen 这个 exe 有时只会拉起一个壳，
    # 而 `shell:AppsFolder\<AUMID>` 是系统认可的激活方式，也不会弹资源管理器窗口。
    # 第四十三轮：这层不再要求「先以管理员运行」（软件已不提权了）。
    packaged = _is_packaged_install(exe)
    errors = []

    def try_aumid(with_discovery: bool = False) -> bool:
        for a in _aumid_candidates(with_discovery):
            try:
                _launch_aumid(a)
            except Exception as e:                        # noqa: BLE001
                errors.append(str(e))
                continue
            if _wait_launcher(8.0):
                return True
        return False

    # 1) 商店/GDK 版：先 AUMID（系统激活，不弹文件夹；普通权限同样有效）
    if packaged and try_aumid():
        return True, "微软商店版官方启动器"

    # 2) 直接启动 exe
    try:
        subprocess.Popen([exe], cwd=cwd, creationflags=subprocess.DETACHED_PROCESS)
        if _wait_launcher(12.0):
            return True, "经典版官方启动器"
    except Exception as e:                                # noqa: BLE001
        errors.append(str(e))

    # 3) ShellExecute（不会打开文件夹窗口）
    try:
        _shell_start(exe)
        if _wait_launcher(8.0):
            return True, "经典版官方启动器"
    except Exception as e:                                # noqa: BLE001
        errors.append(str(e))

    # 4) 商店版 / 开始菜单里的 UWP 入口（含动态发现真实 AppID）
    if try_aumid() or try_aumid(with_discovery=True):
        return True, "微软商店版官方启动器"

    if errors:
        return False, (f"无法启动 {name}（{errors[0]}）。请确认该文件能双击打开，"
                       f"或在「设置 → 官方启动器」中重新选择启动器主程序（当前：{exe}）")
    return False, (f"未能启动 {name}：进程没有出现。可先从开始菜单手动打开一次，"
                   f"或在「设置 → 官方启动器」中重新选择启动器主程序（当前：{exe}）")


def _is_packaged_install(exe: str) -> bool:
    """Xbox 应用 / 商店安装的 GDK 包：目录里有 MicrosoftGame.Config 或 .xsp"""
    d = os.path.dirname(exe or "")
    if not d or not os.path.isdir(d):
        return False
    if "\\windowsapps\\" in d.lower():
        return True
    try:
        for n in os.listdir(d)[:80]:
            low = n.lower()
            if low == "microsoftgame.config" or low.endswith(".xsp"):
                return True
    except Exception:
        pass
    return bool(os.path.isfile(os.path.join(d, "MicrosoftGame.Config")))


def _launch_store_app(aumid: str):
    """商店版：AUMID 只能经 explorer 激活"""
    for a in [aumid] + [x for x in _aumid_candidates(True) if x != aumid]:
        try:
            _launch_aumid(a)
        except Exception as e:                            # noqa: BLE001
            return False, f"启动失败：{e}"
        if _wait_launcher(8.0):
            return True, "微软商店版官方启动器"
    custom = CONFIG.get("launcher_path")
    if custom and not os.path.isfile(custom):
        return False, (f"设置里保存的启动器路径已失效：{custom}。"
                       f"请在「设置 → 官方启动器」中重新选择主程序")
    return False, ("未检测到官方启动器，请先在「设置 → 官方启动器」中选择 "
                   "MinecraftLauncher.exe 或 Minecraft.exe")


# ---------------------------------------------------------------- 目录
def detect_game_dir():
    cfg = CONFIG.get("game_dir")
    if cfg and os.path.isdir(cfg):
        return cfg
    appdata_mc = os.path.expandvars(r"%APPDATA%\.minecraft")
    if os.path.isdir(appdata_mc) and (
            os.path.isfile(os.path.join(appdata_mc, "launcher_profiles.json"))
            or os.path.isdir(os.path.join(appdata_mc, "versions"))):
        return appdata_mc
    if os.path.isdir(STORE_GAME_DIR):
        return STORE_GAME_DIR
    return appdata_mc


def download_dir() -> str:
    """第四十五轮：**下载 / 安装**要落到哪个游戏目录。

    跟 `detect_game_dir()`（本启动器自己那份，独立启动 / 游戏管理都读它）分开：
    玩家在主页选了「官方」就把内容装进 `official_assets\\.minecraft` 交给官方启动器，
    选了「应用」就装进本启动器自己的 `.minecraft`。
    """
    try:
        from . import paths
        return paths.download_dir()
    except Exception:
        return detect_game_dir()


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def java_args_for(mb: int, auto_min: bool | None = None) -> str:
    """按指定 MB 生成 JVM 内存参数；0 / 负数返回空串。"""
    try:
        mb = int(mb or 0)
    except Exception:
        mb = 0
    if mb <= 0:
        return ""
    mb = max(512, min(mb, 65536))
    if auto_min is None:
        auto_min = bool(CONFIG.get("memory_auto_min", True))
    xms = mb // 2 if auto_min else min(mb, 1024)
    return f"-Xms{xms}M -Xmx{mb}M"


def memory_java_args() -> str:
    """按设置生成 JVM 内存参数；未设置返回空串（交给官方启动器默认）。

    「自动配置」算出来的值在设置页保存时就已经写进 `memory_mb`，
    所以这里不必再判一次模式 —— 判了反而会让「手改配置里的 memory_mb」
    这种老用法突然失效（老测试也是这么直接设值的）。
    """
    try:
        mb = int(CONFIG.get("memory_mb") or 0)
    except Exception:
        mb = 0
    return java_args_for(mb)


# ---------------------------------------------------------------- 整合包内存
def _norm_mb(v) -> int:
    """把各种来源的内存值规范成 MB（过大的按字节处理）"""
    try:
        n = int(float(v))
    except Exception:
        return 0
    if n <= 0:
        return 0
    if n > 65536:               # 多半是字节
        n = n // (1024 * 1024)
    if n < 512:
        return 0
    return min(n, 65536)


def _ini_value(path: str, key: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, _, val = line.partition("=")
                    if k.strip().lower() == key.lower():
                        return val.strip()
    except Exception:
        pass
    return ""


def _json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def pack_declared_memory(instance_dir: str) -> tuple[int, str]:
    """整合包自己声明的分配内存。返回 (MB, 来源说明)；没声明返回 (0, "")。

    目前认得这些写法：
    - instance.cfg（MultiMC / Prism / ATLauncher）: MaxMemAlloc=4096
    - minecraftinstance.json（第三方客户端）: allocatedMemory
    - modrinth.index.json / manifest.json 自定义字段: memory / recommendedMemory
    - .mcassist.json（本软件上次安装时记下的）
    """
    if not instance_dir or not os.path.isdir(instance_dir):
        return 0, ""

    mb = _norm_mb(_ini_value(os.path.join(instance_dir, "instance.cfg"), "MaxMemAlloc"))
    if mb:
        return mb, "整合包自带 instance.cfg（MultiMC/Prism）"

    cfg = _json_file(os.path.join(instance_dir, "minecraftinstance.json"))
    for k in ("allocatedMemory", "memory", "recommendedMemory"):
        mb = _norm_mb(cfg.get(k))
        if mb:
            return mb, "整合包自带 minecraftinstance.json"

    for name in ("modrinth.index.json", "manifest.json", "mmc-pack.json"):
        d = _json_file(os.path.join(instance_dir, name))
        for k in ("memory", "recommendedMemory", "memoryMb", "maxMemory"):
            mb = _norm_mb(d.get(k))
            if mb:
                return mb, f"整合包清单 {name}"
        # 有些整合包 manifest 把建议内存写在 minecraft 段里
        mc = d.get("minecraft")
        if isinstance(mc, dict):
            for k in ("memory", "recommendedMemory"):
                mb = _norm_mb(mc.get(k))
                if mb:
                    return mb, f"整合包清单 {name}"

    own = _json_file(os.path.join(instance_dir, ".mcassist.json"))
    mb = _norm_mb(own.get("memory_mb"))
    if mb:
        return mb, "上次安装时的分配记录"
    return 0, ""


def apply_memory_to_profile(profile_id: str, mb: int,
                            game_dir: str | None = None,
                            auto_min: bool | None = None) -> tuple[bool, str]:
    """给单个官启档案写/清 javaArgs（整合包安装后分配内存用）。"""
    gdir = game_dir or detect_game_dir()
    lp = os.path.join(gdir, "launcher_profiles.json")
    if not os.path.isfile(lp):
        return False, "未找到官方启动器档案文件"
    if launcher_running():
        return False, "官方启动器正在运行，请先退出它再修改内存分配"
    try:
        with open(lp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:                                # noqa: BLE001
        return False, f"读取档案失败：{e}"
    prof = (data.get("profiles") or {}).get(profile_id)
    if not isinstance(prof, dict):
        return False, "档案不存在（可能已被官方启动器清理）"
    args = java_args_for(mb, auto_min)
    if args:
        prof["javaArgs"] = args
    else:
        prof.pop("javaArgs", None)
    ok, msg = _write_profiles(data, gdir)
    if not ok:
        return False, msg
    _remember_profile(profile_id, prof, gdir)
    return True, (f"已分配 {mb} MB 运行内存" if args else "已改为官方启动器默认内存")


def apply_memory_to_profiles(game_dir: str | None = None) -> tuple[bool, str]:
    """把内存设置同步到本软件创建的所有官启档案（javaArgs）。返回 (是否改动, 说明)"""
    gdir = game_dir or detect_game_dir()
    lp = os.path.join(gdir, "launcher_profiles.json")
    if not os.path.isfile(lp):
        return False, "未找到档案文件"
    try:
        with open(lp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"读取档案失败：{e}"
    args = memory_java_args()
    known = {str(v.get("name") or "") for v in (CONFIG.get("installed") or {}).values()}
    changed = 0
    for pid, prof in (data.get("profiles") or {}).items():
        if not isinstance(prof, dict):
            continue
        name = str(prof.get("name") or "")
        if prof.get("type") != "custom":
            continue
        if known and name not in known:
            gd = str(prof.get("gameDir") or "")
            try:
                base = os.path.normcase(os.path.abspath(gdir)) + os.sep
                if not os.path.normcase(os.path.abspath(gd)).startswith(base):
                    continue
            except Exception:
                continue
        if args:
            if prof.get("javaArgs") != args:
                prof["javaArgs"] = args
                changed += 1
        elif "javaArgs" in prof:
            prof.pop("javaArgs", None)
            changed += 1
    if not changed:
        return False, "内存设置无需改动"
    ok, msg = _write_profiles(data, gdir)
    if not ok:
        return False, msg
    return True, f"已更新 {changed} 个档案的内存参数"


def apply_memory_to_profile(profile_id: str, mb: int,
                            game_dir: str | None = None) -> tuple[bool, str]:
    """只改**一个**档案的 javaArgs（「各游戏独立设置 → 运行内存」用）。

    `mb <= 0` 表示让这个档案回到官启默认（删掉 javaArgs 键）。
    这一步和全局 `apply_memory_to_profiles()` 共用同一套原子写与进程检查：
    官启运行时写档案会被它退出时整表覆盖，必须拒绝。
    """
    pid = str(profile_id or "")
    if not pid:
        return False, "这个游戏没有对应的官启档案"
    gdir = game_dir or detect_game_dir()
    lp = os.path.join(gdir, "launcher_profiles.json")
    if not os.path.isfile(lp):
        return False, "未找到档案文件"
    try:
        with open(lp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"读取档案失败：{e}"
    prof = (data.get("profiles") or {}).get(pid)
    if not isinstance(prof, dict):
        return False, "档案里没有这一项（可能已被删除）"
    args = java_args_for(mb)
    if args:
        if prof.get("javaArgs") == args:
            return False, "内存设置无需改动"
        prof["javaArgs"] = args
    else:
        if "javaArgs" not in prof:
            return False, "内存设置无需改动"
        prof.pop("javaArgs", None)
    ok, msg = _write_profiles(data, gdir)
    if not ok:
        return False, msg
    return True, "已更新这个启动项的内存参数"


def total_ram_gb() -> int:
    """本机物理内存（GB，向下取整）"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return max(1, int(st.ullTotalPhys / 1024 ** 3))
    except Exception:
        return 8


# ---------------------------------------------------------------- 实例目录（PCL2 语义）
def safe_dir_name(name: str, fallback: str = "Modpack") -> str:
    """把整合包名变成合法目录名（Windows 不允许 \\ / : * ? " < > |）"""
    return re.sub(r'[\\/:*?"<>|]+', "_", str(name or "")).strip().strip(" ._") or fallback


def versions_dir(game_dir: str) -> str:
    return os.path.join(game_dir, "versions")


# 搬实例时**不要**带走的东西：
# 目录——启动器/游戏的共享体积（几十 G，且我们本来就共享），
#       以及纯日志/缓存/回收站，搬过来只会让目标目录虚胖。
INSTANCE_SKIP = {"libraries", "assets", "runtime", "versions", "bin", "logs",
                 "crash-reports", "backups", "downloads", "staging", "webcache2",
                 "quickplay", "screenshots", "cache", "temp", ".minecraft",
                 ".git", ".gradle", ".cache", "__pycache__"}
# 文件——别的启动器自己的元数据（留着会让两个启动器抢同一份配置）
INSTANCE_SKIP_FILES = {"instance.cfg", "mmc-pack.json", "minecraftinstance.json",
                       "profile.json", "meta.json", "launcher_profiles.json",
                       "launcher_profiles.json.bak", "usercache.json"}


def is_pack_dir(path: str) -> bool:
    """这个目录是「整合包实例」而不是纯运行版本？

    第一百一十二轮：主页下载的独立加载器版本带 `standalone_version: true`
    标记 —— 它是玩家要直接玩的游戏，不是整合包实例，所有调用方（版本列表
    不跳过、瘦身扫描不碰、占名判定按纯版本算）都按「不是包」处理。
    """
    mk = os.path.join(path, ".mcassist.json")
    if os.path.isfile(mk):
        m = _json_file(mk) or {}
        if m.get("standalone_version"):
            return False
        return True
    name = os.path.basename(str(path).rstrip("\\/"))
    return bool(_json_file(os.path.join(path, name + ".json")).get("mcassist"))


def isolated_dir(game_dir: str, name: str) -> str:
    """实例目录 = `<游戏目录>/versions/<包名>`。

    这是 PCL2 的版本隔离语义：隔离出来的实例本身就是 versions 里的一个版本目录，
    模组 / 存档 / 配置全在里面，官启版本列表里看到的就是它。
    以前我们把实例建在 `.minecraft/<包名>`（versions 外面），官启既不算隔离，
    和其他版本的存档也混在一起 —— 这就是「版本隔离跟没效果一样」。
    """
    base = safe_dir_name(name)
    vroot = versions_dir(game_dir)
    path = os.path.join(vroot, base)

    def _is_version(p):
        return any(os.path.isfile(os.path.join(p, base + ext))
                   for ext in (".json", ".jar"))

    # 同名目录已被真实版本占着（里面的 json 是游戏版本而不是我们的实例）→ 加序号
    if os.path.isdir(path) and not is_pack_dir(path) and _is_version(path):
        for i in range(2, 100):
            alt = f"{base}-{i}"
            p2 = os.path.join(vroot, alt)
            if not os.path.isdir(p2):
                return p2
    return path


# 薄壳里**要保留**的字段：让启动器和本软件认得出「这是什么版本」
# （mainClass/libraries 决定加载器，javaVersion 决定挑哪个 Java，clientVersion 决定 mc 号）
_SHELL_KEEP = ("libraries", "mainClass", "assetIndex", "assets", "javaVersion",
               "clientVersion", "complianceLevel", "minimumLauncherVersion", "logging")


def _mc_num(*cands) -> str:
    """从 '1.20.1-forge-47.4.18' 这类字符串里抠出原版号 '1.20.1'"""
    for c in cands:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(c or ""))
        if m:
            return m.group(1)
    return ""


_LOADER_MAIN = ("bootstraplauncher", "modlauncher", "fabric", "quilt")


def _is_loader_main(main: str) -> bool:
    """这个 mainClass 是不是加载器的主类（原版是 net.minecraft.client.main.Main）"""
    m = str(main or "").lower()
    return any(k in m for k in _LOADER_MAIN)


def _pack_shell(vid: str, vid_src: str, name: str, base: dict | None = None) -> dict:
    """整合包实例的启动档案（第四十八轮）：**只有启动参数交给继承链**。

    老版本是把基底 json 整份抄过来再补 `inheritsFrom` —— 于是启动器按
    「父参数 + 子参数」合并时参数全重复（两个 `--gameDir`、两个 `-cp`、两个 `-p`），
    ModLauncher 直接抛 `MultipleArgumentsForOptionException`，游戏窗口一闪都不出现；
    官启 / PCL2 里同样是「打不开」。

    所以这里**丢掉 `arguments` / `minecraftArguments` / `downloads`**（参数与下载信息
    全部沿继承链拿父版本的），但保留 `libraries` / `mainClass` / `javaVersion` /
    `clientVersion` 这些「身份信息」—— 没有它们，本软件认不出加载器、挑错 Java，
    官启也不知道该用哪个 Java 跑。
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    b = base or {}
    out = {
        "id": vid,
        "inheritsFrom": vid_src,
        "type": str(b.get("type") or "release"),
        "time": str(b.get("time") or now),
        "releaseTime": str(b.get("releaseTime") or now),
        # 自述标记：这目录是整合包实例，不是可复用的运行版本
        #（列表去重、目录判定、运行版本扫描都靠它）
        "mcassist": {"pack": str(name), "base": vid_src},
    }
    for k in _SHELL_KEEP:
        v = b.get(k)
        if v not in (None, "", [], {}):
            out[k] = v
    # 原版号写进 clientVersion：壳的 inheritsFrom 指的是加载器版本，
    # 启动器/本软件靠 clientVersion 才知道「这是 1.20.1」（挑 Java 用）
    mcver = str(b.get("clientVersion") or "") or _mc_num(b.get("inheritsFrom"),
                                                         vid_src, vid)
    if mcver:
        out["clientVersion"] = mcver
    return out


def ensure_pack_version(game_dir: str, base_vid: str, name: str) -> tuple[str, str]:
    """在 versions/<包名>/ 里造一个「以整合包命名」的运行版本，返回 (版本 id, 说明)。

    官启的版本列表 = `versions` 下每个带 `<目录名>.json` 的子目录，所以实例目录
    要出现在里面，就必须有自己的 json。这里写一份**不带启动参数**的档案：

        {"id": "<包名>", "inheritsFrom": "<base_vid>", "mcassist": {...},
         "libraries": […], "mainClass": …, "javaVersion": …}

    库、资源、**启动参数**全部沿继承链从 base_vid 拿（参数只此一份，不会被拼两遍）；
    身份信息（libraries/mainClass/javaVersion）留在本档案里，好让启动器认出加载器、
    挑对 Java。base_vid 自己找不到时原样返回，宁可退化成「用原版本 id」，
    也不能写出一个官启解析不了的档案。
    """
    vid_src = str(base_vid or "").strip()
    if not vid_src:
        return "", ""
    vroot = versions_dir(game_dir)
    from . import paths as _paths
    base_json = _paths.version_json_lookup(game_dir, vid_src)
    if not os.path.isfile(base_json):
        return vid_src, ""
    vid = safe_dir_name(name)
    if not vid or os.path.normcase(vid) == os.path.normcase(vid_src):
        return vid_src, ""
    target_dir = os.path.join(vroot, vid)
    target_json = os.path.join(target_dir, vid + ".json")
    if os.path.isfile(target_json):
        old = _json_file(target_json)
        ometa = old.get("mcassist") or {}
        if ometa.get("merged"):
            # 第一百零九轮：实例档案已经是自包含的（PCL-CE MergeJson 语义），
            # 再按壳写一遍等于把「不依赖 versions/<加载器>」这件事又退回去。
            return vid, ""
        if ometa.get("base") == vid_src:
            # 已经造过（同一个基底）→ 顺手把老版本的「胖壳」瘦下来，再幂等返回
            slim_pack_json(target_json)
            return vid, ""
        if not old.get("mcassist"):
            # 这个同名目录是**真正安装的游戏版本**（不是我们的实例目录）。
            # 覆盖它等于毁掉一个能启动的版本，宁可退化成用原版本 id。
            return vid_src, ""
    base = _json_file(base_json)
    if not base:
        return vid_src, ""
    # 防御（第五十三轮）：基底自己就是「裸原版号 + 加载器主类」的畸形档案时别写壳
    # —— 那说明原版目录被加载器档案盖过，照着它写出来的壳继承链是坏的。
    if re.match(r"^\d+\.\d+(?:\.\d+)?$", vid_src) and not base.get("inheritsFrom") \
            and _is_loader_main(base.get("mainClass")):
        return vid_src, ""
    data = _pack_shell(vid, vid_src, name, base)
    try:
        _ensure_dir(target_dir)
        with open(target_json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        return vid_src, ""
    return vid, f"已在版本列表里生成「{vid}」"


def _chain_vids(game_dir: str, vid: str, limit: int = 8) -> list[str]:
    """版本 id → 它自己 + 一路上继承的父版本 id（最多 8 层）"""
    out, cur, seen = [], str(vid or "").strip(), set()
    while cur and cur not in seen and len(out) < limit:
        seen.add(cur)
        out.append(cur)
        from . import paths as _paths
        d = _json_file(_paths.version_json_lookup(game_dir, cur))
        cur = str(d.get("inheritsFrom") or "").strip()
    return out


def slim_pack_json(path: str) -> bool:
    """去掉实例档案里**重复的启动参数**（老版本的「胖壳」遗留），返回是否改动。

    判据：带 `mcassist.base` 标记，却还留着自己的 arguments / minecraftArguments /
    downloads —— 那就是当年整份抄基底留下的。参数删掉（交给继承链），
    libraries / mainClass / javaVersion 保留（认加载器、挑 Java 要靠它们）。

    第一百零九轮：`mcassist.merged` 的档案**绝不动**。那是
    `merge_pack_runtime()` 按 PCL-CE 的 MergeJson 语义合出来的自包含档案：
    参数、库、主类全在自己身上，本来就该有 —— 把它瘦回去等于又变成
    依赖 `versions/<加载器>` 的壳，而那份父版本正是玩家会删掉的东西。
    """
    d = _json_file(path)
    meta = d.get("mcassist") or {}
    if meta.get("merged"):
        return False
    base_vid = str(meta.get("base") or "")
    if not base_vid:
        return False
    if not (d.get("arguments") or d.get("minecraftArguments") or d.get("downloads")):
        return False                      # 已经没有重复的启动参数了
    vid = str(d.get("id") or os.path.basename(os.path.dirname(path)))
    shell = _pack_shell(vid, base_vid, str(meta.get("pack") or vid), d)
    try:
        # 先留一份原档：万一某个包靠自己 json 里的特有字段启动，还能一键还原
        bak = path + ".bak"
        if not os.path.isfile(bak):
            shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(shell, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _dedup_libs(libs) -> list:
    """按 maven 坐标去重，**后面的覆盖前面的**（子版本优先，与 Mojang 规范一致）"""
    out, pos = [], {}
    for l in (libs or []):
        n = str((l or {}).get("name") or "") if isinstance(l, dict) else ""
        if not n:
            out.append(l)
            continue
        if n in pos:
            out[pos[n]] = l
        else:
            pos[n] = len(out)
            out.append(l)
    return out


def loader_json_complete(d: dict) -> bool:
    """版本 json 是不是「真安装器产物」——瘦壳 clone 不算。

    第一百零九轮的现场：`versions/neoforge-21.1.248/neoforge-21.1.248.json`
    被写成了一份 libraries 与实例壳逐条相同的 clone（47 条，全是
    fancymodloader / loader / asm 骨架），没有加载器主坐标、没有安装器写的
    启动参数。启动器按它组 classpath，里面就没有 `neoforge-*-client.jar` /
    `client-*-srg.jar` / `client-extra`，于是开窗口前抛
    `NoClassDefFoundError: net/minecraft/client/gui/screens/LoadingOverlay`。

    判据（任一成立即认为完整）：
      · libraries 里有 `net.neoforged:neoforge` / `net.minecraftforge:forge` 主坐标；
      · arguments.game 里有安装器写的 `--fml.neoForgeVersion` / `--fml.forgeVersion`。
    """
    d = d or {}
    for l in (d.get("libraries") or []):
        n = str((l or {}).get("name") or "").lower()
        if n.startswith("net.neoforged:neoforge:") or \
                n.startswith("net.minecraftforge:forge:"):
            return True
    args = ((d.get("arguments") or {}).get("game") or [])
    for a in args:
        t = str(a if isinstance(a, str) else (a or {}).get("value") or "")
        if t.startswith("--fml.neoForgeVersion") or t.startswith("--fml.forgeVersion"):
            return True
    return False


def merge_pack_runtime(game_dir: str, instance_dir: str, base_vid: str = "",
                       progress=None) -> tuple[bool, str]:
    """把加载器运行档案**合并进实例 json** 并移除 `inheritsFrom`（自包含）。

    第一百零九轮，采纳 PCL-CE `MergeJson` 的做法。它的语义是：
      实例 json = 原版档案 + 各加载器档案依次合并 → `Remove("inheritsFrom")`
      → `Remove("jar")` → 写回。于是实例自己就是完整运行版本，不再需要
      `versions/<加载器id>/` 那一份。

    我们以前的实例是「瘦壳 + 继承父版本」，父版本一旦被删 / 被写成半个壳，
    实例就找不到 MC 本体类。玩家删掉那个「多出来的目录」后点补全，
    补全流程又把父版本重建成瘦壳，开局直接 `NoClassDefFoundError` 秒退。

    合并后：
      · `inheritsFrom` / `jar` 移除，`id` = 实例目录名；
      · libraries 按坐标去重（子版本优先）；
      · arguments 用 gamelaunch._merge 的规则合并（同一个选项绝不出两次）；
      · 打 `mcassist.merged = true` 标记，`slim_pack_json` 见到就不动它。

    返回 (成功, 说明)。失败时什么都不写。
    """
    d = os.path.abspath(str(instance_dir or ""))
    if not d or not os.path.isdir(d):
        return False, "实例目录不见了"
    inst = os.path.basename(d.rstrip("\\/"))
    if not inst:
        return False, "认不出实例目录名"
    vroot = versions_dir(game_dir)
    shell_p = os.path.join(vroot, inst, inst + ".json")
    shell = _json_file(shell_p)
    if not shell:
        return False, "实例档案不存在"
    smeta = shell.get("mcassist") or {}
    if smeta.get("merged") and not shell.get("inheritsFrom"):
        return True, ""                     # 已经是自包含档案，幂等返回
    src = str(base_vid or "").strip() or str(shell.get("inheritsFrom") or "").strip()
    if not src:
        try:
            _m = _json_file(os.path.join(d, ".mcassist.json")) or {}
        except Exception:                   # noqa: BLE001
            _m = {}
        src = str(_m.get("base_version") or "").strip()
    if src and os.path.normcase(src) == os.path.normcase(inst):
        src = ""
    if not src:
        return False, "不知道实例该继承哪个运行版本"
    # 加载器版本可能已挪到全局 loaders/ 目录（见 paths.loaders_dir），先在本目录找，
    # 找不到再去 loaders/ 找，别写死 versions/<src>。
    from . import paths as _paths
    parent_p = _paths.version_json_lookup(game_dir, src)
    parent = _json_file(parent_p)
    if not parent:
        return False, (f"运行版本 {src} 的 json 不在了，"
                       f"得先用官方安装器把它装回来")
    if not loader_json_complete(parent):
        return False, (f"运行版本 {src} 的 json 不完整（没有加载器主坐标，"
                       f"是半个壳），合并进去也起不来")
    try:
        from . import gamelaunch as _gl
        base_data, _chain = _gl.resolve_version(game_dir, src)
        merged = _gl._merge(base_data, shell)
    except Exception as e:                  # noqa: BLE001
        return False, f"合并运行档案失败：{str(e)[:70]}"
    for k in ("inheritsFrom", "jar", "minecraftArguments"):
        merged.pop(k, None)
    merged["id"] = inst
    merged["libraries"] = _dedup_libs(merged.get("libraries") or [])
    meta = dict(smeta)
    meta.pop("base", None)
    meta.update({"merged": True, "merged_from": src})
    if not meta.get("pack"):
        meta["pack"] = inst
    merged["mcassist"] = meta
    if not merged.get("clientVersion"):
        merged["clientVersion"] = str(parent.get("clientVersion")
                                      or _mc_num(parent.get("inheritsFrom"), src, inst) or "")
    if progress:
        progress(f"已把 {src} 合并进实例（以后不再依赖那个目录）")
    try:
        bak = shell_p + ".premerge.bak"
        if not os.path.isfile(bak):
            shutil.copy2(shell_p, bak)      # 留一份原档，出问题能一键还原
        tmp = shell_p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, shell_p)
    except Exception as e:                  # noqa: BLE001
        return False, f"写实例档案失败：{str(e)[:60]}"
    return True, f"已把运行版本 {src} 合并进实例"


def mirror_base_jar(game_dir: str, vid: str, base_vid: str = "") -> str:
    """给整合包实例目录补一个 `<包名>.jar`（硬链接到基底 jar，跨卷才真复制）。

    官启按 profile 的 `lastVersionId` 去 `versions/<包名>/<包名>.jar` 找客户端 jar；
    只有 json 没有 jar 时，官启 / 部分第三方工具会认定「客户端 jar 缺失」而不启动。
    同卷 NTFS 硬链接几乎瞬时、不占额外空间，内容与基底完全一致。
    """
    vroot = versions_dir(game_dir)
    dst = os.path.join(vroot, vid, vid + ".jar")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return dst
    seeds = _chain_vids(game_dir, base_vid) if base_vid else []
    for cand in seeds + _chain_vids(game_dir, vid):
        src = os.path.join(vroot, cand, cand + ".jar")
        if not (os.path.isfile(src) and os.path.getsize(src) > 0):
            continue
        try:
            _ensure_dir(os.path.dirname(dst))
            try:
                os.link(src, dst)          # NTFS 硬链接：0 字节成本
            except OSError:
                shutil.copy2(src, dst)     # 跨卷 / 非 NTFS 才真复制
            return dst
        except Exception:
            continue
    return ""


def slim_all_pack_jsons(game_dir: str, do_mirror: bool = True) -> tuple[int, int]:
    """一次性整理已装好的整合包：胖壳 json 瘦身 + 补 `<包名>.jar`。

    返回 (瘦身个数, 补 jar 个数)。对官启 / 应用两个目录都要跑（升级时各跑一次）。
    """
    slimmed = jarred = 0
    vroot = versions_dir(game_dir)
    try:
        names = os.listdir(vroot)
    except Exception:
        return 0, 0
    for n in names:
        p = os.path.join(vroot, n)
        if not os.path.isdir(p) or not is_pack_dir(p):
            continue
        if slim_pack_json(os.path.join(p, n + ".json")):
            slimmed += 1
        if do_mirror:
            jp = os.path.join(p, n + ".jar")
            had = os.path.isfile(jp) and os.path.getsize(jp) > 0
            if mirror_base_jar(game_dir, n) and not had:
                jarred += 1          # 只统计「这次新补的」，幂等跑不会重复计数
    return slimmed, jarred


# 只在升级后跑一次（第四十八轮）：老实例的胖壳档案会害得游戏起不来
PACK_SHELL_KEY = "pack_shell_v48"


def migrate_pack_shells() -> str:
    """升级后一次性整理：老整合包瘦身 + 补 jar。返回给玩家看的一句话。"""
    if CONFIG.get(PACK_SHELL_KEY):
        return ""
    CONFIG.set(PACK_SHELL_KEY, True)
    from . import paths
    slim_total = jar_total = 0
    seen = set()
    for gd in (paths.app_game_dir(), paths.official_game_dir()):
        key = os.path.normcase(os.path.abspath(gd or ""))
        if not gd or key in seen:
            continue
        seen.add(key)
        try:
            s, j = slim_all_pack_jsons(gd)
        except Exception:
            continue
        slim_total += s
        jar_total += j
    if not (slim_total or jar_total):
        return ""
    bits = []
    if slim_total:
        bits.append(f"整理 {slim_total} 个整合包的启动档案")
    if jar_total:
        bits.append(f"补上 {jar_total} 个客户端 jar")
    return "已" + "、".join(bits)


# ---------------------------------------------------------------- 档案登记表
def _remember_profile(pid: str, prof: dict, root: str = ""):
    """记下我们自己写的档案（官启把它吃掉时用来补回）

    root = 这份档案属于哪个游戏目录。补回时只补当前游戏目录的，
    否则在 A 目录点自检会把 B 目录的档案也塞进 A 的 launcher_profiles.json。
    """
    try:
        rec = dict(CONFIG.get("profiles_wanted") or {})
        old = rec.get(pid) if isinstance(rec.get(pid), dict) else {}
        rec[pid] = {
            "name": str(prof.get("name") or ""),
            "game_dir": str(prof.get("gameDir") or ""),
            "version_id": str(prof.get("lastVersionId") or ""),
            "java_args": str(prof.get("javaArgs") or ""),
            "icon": str(prof.get("icon") or "Grass_Block"),
            "root": str(root or old.get("root") or ""),
            # 已经被补过一次：再被吃掉就是用户自己在官启里删了，不再打扰
            "healed": bool(old.get("healed")),
        }
        CONFIG.set("profiles_wanted", rec)
    except Exception:
        pass


def _forget_profile(pid: str):
    try:
        rec = dict(CONFIG.get("profiles_wanted") or {})
        if rec.pop(str(pid), None) is not None:
            CONFIG.set("profiles_wanted", rec)
    except Exception:
        pass


def remember_import(path: str, meta: dict):
    """记下导入进来的实例/版本（官启档案被吃掉也照样能在「游戏管理」里看到）"""
    try:
        rec = dict(CONFIG.get("imported") or {})
        rec[os.path.normcase(os.path.realpath(path))] = dict(
            meta, path=path, time=datetime.now().isoformat(timespec="seconds"))
        CONFIG.set("imported", rec)
    except Exception:
        pass


def forget_import(path: str):
    try:
        rec = dict(CONFIG.get("imported") or {})
        key = os.path.normcase(os.path.realpath(path))
        if rec.pop(key, None) is not None:
            CONFIG.set("imported", rec)
    except Exception:
        pass


def imported_records(game_dir: str | None = None) -> list:
    """导入登记表里的活记录（目录还在的）。

    `game_dir` 给了就按「官方 / 应用」分：只收落在这一条目录里的，
    **并且丢掉明确落在另一条目录里的**（搬家之后那份 `official_assets` 里的
    实例跑到「应用」标签里、两边各登记一份，就是第九十九轮玩家截图里那串
    「一模一样」的重复行）。

    但**两条目录都不在**的记录要留 —— 那是玩家用「添加已有文件夹」从别的
    启动器接管的实例（第 23 轮起「游戏管理」就支持列外部实例），一刀切按
    `gd` 过滤会把它们藏起来。
    """
    def _norm(p):
        try:
            return os.path.normcase(os.path.realpath(str(p or "")))
        except Exception:                                 # noqa: BLE001
            return ""

    def _under(p, r):
        return bool(r) and (p == r or p.startswith(r + os.sep))

    gd_key = _norm(game_dir) if game_dir else ""
    ours = []
    if gd_key:
        try:
            from . import paths as _p
            for cand in (_p.app_game_dir(), _p.official_game_dir()):
                k = _norm(cand)
                if k and k not in ours:
                    ours.append(k)
        except Exception:                                 # noqa: BLE001
            pass
        if gd_key not in ours:
            ours.append(gd_key)
    out = []
    for rec in (CONFIG.get("imported") or {}).values():
        if not isinstance(rec, dict):
            continue
        p = str(rec.get("path") or "")
        if not p or not os.path.isdir(p):
            continue
        if ours:
            rp = _norm(p)
            in_ours = next((r for r in ours if _under(rp, r)), "")
            if in_ours:
                # 属于我们两条目录之一 → 只有当前这条收
                if gd_key and _under(rp, gd_key):
                    out.append(rec)
                continue
        out.append(rec)                                  # 真·外部文件夹
    return out


# 一次性自愈（第九十九轮）：1.6.8 ~ 1.6.9 那次「总路径搬家」把登记表里的
# name / mc / version_id / kind 全切成了 `新路径 + 原串[22:]`。版本号被毁掉之后
# `kind == "version"` 这条判断失效，于是**纯版本也挤进「整合包」那一栏**。
IMPORTS_HEALED_KEY = "imports_healed_v170"


def _pathish(v) -> bool:
    """这串字是不是被切坏成「盘符 + 路径」的样子（判据见 paths._looks_abs_path）"""
    s = str(v or "")
    return len(s) >= 3 and s[1] == ":" and s[2] in "\\/"


def heal_imported(game_dir: str | None = None, force: bool = False) -> int:
    """修被切坏的导入登记表：能从磁盘上认回来的字段照磁盘重建。

    · `path` 一直是好的（它本来就是绝对路径），所以版本 id = 目录名；
    · 目录里有 `<目录名>.json` → 它是**版本**，不是整合包（这一条最关键：
      kind 一旦被切坏，纯版本就会冒充整合包出现在列表里）；
    · mc / 加载器照 version json 回查，整合包则读实例自己的 `.mcassist.json`；
    · 顺手把「目录已经不在了」的幽灵记录清掉。
    `force=False` 时只在第一次升级后跑一遍（结果记在配置里）。
    """
    if not force and CONFIG.get(IMPORTS_HEALED_KEY):
        return 0
    from . import paths as _paths
    gds = []
    if game_dir:
        gds.append(game_dir)
    else:
        for g in (_paths.app_game_dir(), _paths.official_game_dir()):
            if g:
                gds.append(g)
    fixed = 0
    rec = dict(CONFIG.get("imported") or {})
    out = {}
    for key, item in rec.items():
        if not isinstance(item, dict):
            continue
        p = str(item.get("path") or "")
        if not p or not os.path.isdir(p):
            fixed += 1                     # 幽灵记录：直接不写回去
            continue
        base = os.path.basename(os.path.normpath(p))
        broken = (str(item.get("kind") or "") not in ("version", "instance")
                  or _pathish(item.get("name")) or _pathish(item.get("mc"))
                  or _pathish(item.get("version_id"))
                  or _pathish(item.get("source")))
        if broken and base:
            is_ver = os.path.isfile(os.path.join(p, base + ".json"))
            meta = {}
            mk = os.path.join(p, ".mcassist.json")
            if os.path.isfile(mk):
                meta = _json_file(mk) or {}
            info = {}
            if is_ver:
                try:
                    info = runtime.describe_version(p) or {}
                except Exception:                         # noqa: BLE001
                    info = {}
            item["kind"] = "version" if is_ver else "instance"
            item["version_id"] = base if is_ver else str(
                meta.get("version_id") or info.get("id") or base)
            item["name"] = str(meta.get("pack") or base)
            item["mc"] = str(info.get("mc") or meta.get("mc") or "")
            item["loader"] = str(info.get("loader") or meta.get("loader") or "")
            item["loader_ver"] = str(info.get("loader_ver")
                                     or meta.get("loader_ver") or "")
            item["source"] = ""            # 来源名已经不可考，别留一串路径
            item["healed"] = True
            fixed += 1
        nk = os.path.normcase(os.path.realpath(p))
        out[nk] = item
    if fixed:
        CONFIG.set("imported", out)
    CONFIG.set(IMPORTS_HEALED_KEY, True)
    return fixed


def heal_rebased_metadata() -> int:
    """第一百零一轮：修被「总路径搬家」切坏的三张登记表语义字段。

    1.6.x 的 `_rebase_after_move` 把短字符串切片成空串，于是：
      · `added_groups[gid].name` 全变成新总路径 →「已导入的游戏」里五个文件夹
        同名（玩家截图里五行 `D:\\MC\\Osmanthus flower` 就是它）；
      · `added_dirs[key].name/kind/mc/...` 全坏 → 游戏管理名字错、纯版本被
        当成整合包（`kind` 不再是 `version`/`instance`）；
      · `imported[key].time/profile_id` 长着一串路径。
    第九十九轮的 `heal_imported()` 只修了 `imported` 的一部分字段，还是一次性
    标记（玩家这份配置早已置过 True），剩下的坏数据永远不会再被碰。

    这里补一张**每次启动都检测**的网：只改「看起来是绝对路径」或 kind 不合法
    的那几个字段；`path` / `game_dir` / `group` / `imported_from` 这些真正的路径
    一个字节都不动，条目数也不变。没坏就一次盘都不写（纯字符串比对，很轻）。
    """
    from datetime import datetime
    from . import importer as _imp

    now = datetime.now().isoformat(timespec="seconds")
    fixed = 0

    def _meta(p):
        """照磁盘把一条记录能认回来的字段重建（与 heal_imported 同一套判据）。"""
        base = os.path.basename(os.path.normpath(p)) if p else ""
        is_ver = bool(p) and os.path.isfile(os.path.join(p, base + ".json"))
        meta = {}
        if p and os.path.isfile(os.path.join(p, ".mcassist.json")):
            meta = _json_file(os.path.join(p, ".mcassist.json")) or {}
        info = {}
        if is_ver:
            try:
                info = runtime.describe_version(p) or {}
            except Exception:                            # noqa: BLE001
                info = {}
        return base, is_ver, meta, info

    # ---- ① 分类文件夹：截图里五行同名的直接来源 ----
    groups = CONFIG.get("added_groups") or {}
    if isinstance(groups, dict) and groups:
        out, n = {}, 0
        for gid, g in groups.items():
            if not isinstance(g, dict):
                out[gid] = g
                continue
            it = dict(g)
            if _pathish(it.get("name")):
                # path 一直是好的：照玩家当初「添加已有文件夹」的默认命名重建
                it["name"] = _imp.default_group_name(it.get("path") or gid)
                n += 1
            if _pathish(it.get("time")):
                it["time"] = now
                n += 1
            out[gid] = it
        if n:
            CONFIG.set("added_groups", out)
            fixed += n

    # ---- ② 已登记的目录 ----
    rec = CONFIG.get("added_dirs") or {}
    if isinstance(rec, dict) and rec:
        out, n = {}, 0
        fields = ("name", "mc", "loader", "loader_ver", "source",
                  "version_id", "time")
        for key, r in rec.items():
            if not isinstance(r, dict):
                out[key] = r
                continue
            it = dict(r)
            p = str(it.get("path") or "")
            kind_bad = str(it.get("kind") or "") not in ("version", "instance")
            if not kind_bad and not any(_pathish(it.get(f)) for f in fields):
                out[key] = it
                continue
            base, is_ver, meta, info = _meta(p)
            if not base:
                base = os.path.basename(os.path.normpath(str(key)))
            if _pathish(it.get("name")):
                it["name"] = str(meta.get("pack") or base)
                n += 1
            if kind_bad:
                it["kind"] = "version" if is_ver else "instance"
                n += 1
            if _pathish(it.get("mc")):
                it["mc"] = str(info.get("mc") or meta.get("mc") or "")
                n += 1
            if _pathish(it.get("loader")):
                it["loader"] = str(info.get("loader") or meta.get("loader") or "")
                n += 1
            if _pathish(it.get("loader_ver")):
                it["loader_ver"] = str(info.get("loader_ver")
                                       or meta.get("loader_ver") or "")
                n += 1
            if _pathish(it.get("source")):
                it["source"] = ""        # 来源名已不可考，别留一串路径
                n += 1
            if _pathish(it.get("version_id")):
                vid = ""
                if p:
                    try:
                        _, vid = _imp.launch_ref({"path": p})
                    except Exception:                    # noqa: BLE001
                        vid = ""
                it["version_id"] = str(vid or info.get("id")
                                       or meta.get("version_id") or base)
                n += 1
            if _pathish(it.get("time")):
                it["time"] = now
                n += 1
            out[key] = it
        if n:
            CONFIG.set("added_dirs", out)
            fixed += n

    # ---- ③ 导入登记表：第九十九轮漏下的 time / profile_id ----
    imp = CONFIG.get("imported") or {}
    if isinstance(imp, dict) and imp:
        out, n = {}, 0
        for k, r in imp.items():
            if not isinstance(r, dict):
                out[k] = r
                continue
            it = dict(r)
            if _pathish(it.get("time")):
                it["time"] = now
                n += 1
            if _pathish(it.get("profile_id")):
                p = str(it.get("path") or "")
                it["profile_id"] = str(it.get("version_id")
                                       or os.path.basename(os.path.normpath(p)))
                n += 1
            out[k] = it
        if n:
            CONFIG.set("imported", out)
            fixed += n
    return fixed


def absorb_instance(src: str, target: str, progress=None) -> tuple[bool, str, int]:
    """把别的启动器的实例内容搬进 `target`（官启实例目录），返回 (ok, 说明, 文件数)。

    为什么要搬：官启的「安装」只认 gameDir 指向的目录，档案指向 Prism/PCL2 的
    原目录时，官启一重启、或者玩家清理了别的启动器，这套包就废了；而且原目录
    不在游戏目录里，官启的版本列表也看不到它。搬进 versions/<包名> 之后才算
    「真的配置进官方启动器」。

    三种情况：
      · 源目录就在本游戏目录下面（旧版把实例建在 .minecraft/<包名>）→ 同盘改名，瞬时；
      · 源在别的盘/别的启动器 → 逐文件复制（带字节进度），原实例**保持不动**，
        玩家确认没问题后再自己删；
      · 空间不够 → 先拦下，报需要/可用，绝不写到一半满盘。
    """
    src = os.path.abspath(src)
    target = os.path.abspath(target)
    if not os.path.isdir(src):
        return False, "原实例目录不存在（可能已被删除或移动）", 0
    if os.path.normcase(os.path.realpath(src)) == os.path.normcase(os.path.realpath(target)):
        return True, "", 0

    def tell(t):
        if progress:
            try:
                progress(t)
            except Exception:
                pass

    # ① 同盘改名：源目录的上一级就是本游戏目录 → 它本来就是我们的地盘
    parent = os.path.normcase(os.path.realpath(os.path.dirname(os.path.realpath(src))))
    gd = os.path.normcase(os.path.realpath(os.path.dirname(os.path.realpath(target))))
    gd = os.path.dirname(gd)                       # target 在 versions 下，往上再退一层
    if parent == os.path.normcase(os.path.realpath(gd)) and not os.path.exists(target):
        try:
            _ensure_dir(os.path.dirname(target))
            os.replace(src, target)
            return True, "已从游戏目录里归位（同盘改名，没复制）", 0
        except Exception:
            pass                                   # 改名失败就退回复制

    # ② 逐文件复制
    items, total = [], 0
    try:
        names = os.listdir(src)
    except Exception as e:
        return False, f"读不了原实例目录：{e}", 0
    for name in names:
        low = name.lower()
        p = os.path.join(src, name)
        if low in INSTANCE_SKIP or low in INSTANCE_SKIP_FILES or os.path.islink(p):
            continue
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        size = os.path.getsize(fp)
                    except OSError:
                        size = 0
                    items.append((os.path.relpath(fp, src), size))
                    total += size
        elif os.path.isfile(p):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            items.append((name, size))
            total += size

    try:
        free = shutil.disk_usage(os.path.dirname(target) or target).free
    except Exception:
        free = 0
    if total and free and free < total * 1.02:
        return False, (f"磁盘空间不够：这套包要 {fmt_size(total)}，"
                       f"目标盘只剩 {fmt_size(free)}。先清点空间，或直接把原启动器"
                       f"里的这套包删掉再导"), 0

    done = 0
    _ensure_dir(target)
    for i, (rel, size) in enumerate(items):
        dst = os.path.join(target, rel)
        try:
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(os.path.join(src, rel), dst)
        except Exception as e:
            return False, f"复制失败（{rel}）：{e}", len(items)
        done += size
        if i % 25 == 0 or size >= 8 << 20:
            tell(f"复制 {os.path.basename(rel)}（{fmt_size(done)} / {fmt_size(total)}）")
    return True, "", len(items)


def _register_profile(game_dir: str, name: str, instance_dir: str,
                      version_id: str | None,
                      memory_mb: int = 0,
                      select: bool = True,
                      need_loader: str = "",
                      need_lver: str = "") -> tuple[bool, str]:
    """把整合包档案写进官方启动器的 launcher_profiles.json。

    `select=False` 时不改 `settings.profileName`（玩家原本选中的那项保持不动）——
    批量「配置至官方启动器」用这个，不能把玩家原来的默认启动项顶掉。

    `need_loader` / `need_lver`：这个实例必须跑在哪个加载器上。给了以后，
    兜底选版本时**只认装了该加载器的版本**，一个都没有就直接失败返回 ——
    绝不悄悄绑到纯原版上（第九十五轮：加载器没装上，档案被绑到 1.20.1，
    329 个模组一个都没加载，玩家却在「游戏管理」里看到「已注册到官方启动器」）。

    ⚠ 关键：`lastVersionId` 绝不能留空。官方启动器点开始游戏时会先
    resolveVersionID，空值导致 "Resolved custom: to (空)" →
    Request version details for: (空) → REQUEST_FAILED /
    Unable to prepare assets for download。

    ⚠ 第二个坑（实测）：官启退出时会把**内存里的**档案表整个写回磁盘。
    如果在它运行期间写入，文件里会短暂出现我们的档案，官启一关就没了
    （`.bak` 里还留着、正式文件已经清空）——「导入没反应」就是这么来的。
    所以写之前必须再查一次进程，写完还要回读确认。
    """
    if launcher_running():
        return False, "官方启动器正在运行，先完全退出它再写入启动项（否则会被它覆盖）"
    lp = os.path.join(game_dir, "launcher_profiles.json")
    try:
        with open(lp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"profiles": {}, "settings": {}}
    data.setdefault("profiles", {})
    vid = (version_id or "").strip()
    if not vid or not os.path.isfile(
            os.path.join(game_dir, "versions", vid, vid + ".json")):
        # 兜底：本目录里挑一个真正装好的版本（要加载器就只挑带加载器的）
        vid = runtime.pick_any_version(game_dir, need_loader=need_loader,
                                      need_lver=need_lver)
        if not vid and need_loader:
            cn = runtime.LOADER_CN.get(need_loader, need_loader)
            ver = f" {need_lver}" if need_lver else ""
            return False, (f"{cn}{ver} 还没装上，暂时不给这个整合包绑运行版本"
                           f"—— 绑到原版只会让包里的模组一个都不加载。"
                           f"装好加载器后点「修复整合包档案」重试，或重新导入一次")
    if not vid:
        return False, ("官方启动器里还没有任何可用版本，无法为整合包绑定运行版本。"
                       "请先在官方启动器安装对应的游戏版本与加载器")
    pid = CONFIG.profile_id()
    prof = {
        "name": name,
        "type": "custom",
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "lastUsed": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "gameDir": instance_dir,
        "icon": "Grass_Block",
        "lastVersionId": vid,
    }
    _args = java_args_for(memory_mb) if memory_mb else memory_java_args()
    if _args:
        prof["javaArgs"] = _args
    data["profiles"][pid] = prof
    # 让官方启动器下次打开时默认选中这个档案（批量配置传 select=False，不动原有选择）
    if select:
        data.setdefault("settings", {})["profileName"] = name
    # 先记「应该存在的档案」再写：写失败（多半是官启刚好开着）时，
    # 下次启动自检还能按这张表补回去，不会白装一场。
    _remember_profile(pid, prof, game_dir)
    ok, msg = _write_profiles(data, game_dir)
    if not ok:
        return False, msg
    return True, pid


def _download_zip_file(url: str, dest: str, progress_cb=None, cancel_flag=None,
                       expected_size=0):
    """单文件下载入口（并发引擎在 core/dl.py）。

    保留这个函数名是刻意的：整合包依赖下载、单文件下载、测试桩都走它一处，
    换引擎时不用逐个调用点改。
    """
    _ensure_dir(os.path.dirname(dest))
    return download_to(url, dest, progress_cb=progress_cb,
                       cancel_flag=cancel_flag, expected_size=expected_size)


def _save_pack_icon(instance_dir: str, url: str, cancel_flag=None) -> bool:
    """把整合包项目页的图标落一份到实例目录（icon.png）。

    游戏管理里显示的是「整合包自己的图标」，但 .mrpack / CF zip 里通常不带图标，
    只能在安装时从项目页抓一次存下来。失败不影响安装，界面会回退到加载器徽章。
    已有 icon.png（整合包 overrides 自带、或别的启动器放的）一律不覆盖。
    cancel_flag 跟安装共用：玩家点取消时不会卡在这张图上。
    """
    if not url or not instance_dir:
        return False
    dest = os.path.join(instance_dir, "icon.png")
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return True
        _download_zip_file(url, dest, cancel_flag=cancel_flag)
        ok = os.path.isfile(dest) and os.path.getsize(dest) > 0
        if not ok and os.path.isfile(dest):
            os.remove(dest)
        return ok
    except Exception:
        try:
            if os.path.isfile(dest) and os.path.getsize(dest) == 0:
                os.remove(dest)
        except OSError:
            pass
        return False


def _download_deps(jobs, cb, cancel_flag, on_item, total, shown=0,
                   concurrency=6, bytes_cb=None, records=None):
    """并发下载整合包依赖。

    - 多个文件同时下（默认 6 路），小文件不再排队；
    - 单文件内部再按 Range 分段（见 core/dl.py）。

    jobs: [{"index","row","url","rel","dir","size"}]
    records: 传一个 list 进来，成功的每一项会追加 {path,size,sha1,url}
             —— 整合包清单（manifest.json 的 installed 段）就靠它，
             以后「自动补全」按这份记录核对和重下（第五十四轮）。
    返回失败项名字列表。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lock = threading.Lock()
    state = {"done_items": 0, "sizes": {}, "dones": {}}
    failed = []
    pct_state = {}          # 每个文件上次写到明细里的整数百分比（0~99）

    def _emit_bytes(idx, done, tot):
        with lock:
            state["dones"][idx] = done
            if tot:
                state["sizes"][idx] = tot
            d = sum(state["dones"].values())
            t = sum(state["sizes"].values())
            n = state["done_items"]
        if bytes_cb:
            bytes_cb(d, t)
        cb(5 + int(n / max(total, 1) * 85), 100,
           f"下载依赖 {n}/{total}" if total else "下载依赖")
        # 明细里的单文件百分比：只在整数位变了才写一次，免得每块都抢锁
        if tot:
            p = int(min(99, max(0, done * 100 / tot)))
            if pct_state.get(idx) != p:
                pct_state[idx] = p
                on_item(idx, "run", f"{p}%")

    def one(job):
        idx = job["index"]
        url = job["url"]
        src_url = str(url)
        rel = job["rel"]
        base_dir = job["dir"]
        if cancel_flag and cancel_flag[0]:
            on_item(idx, "skip", "已取消")
            return
        # A6：清单自带的备用下载地址（换源用）；末尾再排一次首个地址 = 重试
        urls = [str(u) for u in (job.get("urls") or [url]) if u] or [url]
        tries = (urls + [urls[0]])[:4]
        sha1 = str(job.get("sha1") or "")
        want_size = int(job.get("size") or 0)
        # A3：Modrinth files[].path 走的是**下载落盘**这条路，与 zip 条目侧的
        #     commonpath 防护不是一回事，这里再守一道，绝不写出实例目录。
        safe = safe_dep_path(rel)
        if not safe:
            with lock:
                failed.append(f"{rel}（路径越界）")
                state["done_items"] += 1
            on_item(idx, "fail", "路径越界")
            return
        rel = safe
        dest = os.path.join(base_dir, rel.replace("/", os.sep))
        try:
            if os.path.commonpath([os.path.abspath(base_dir),
                                   os.path.abspath(dest)]) != os.path.abspath(base_dir):
                raise RuntimeError("路径越界")
        except (ValueError, RuntimeError):
            with lock:
                failed.append(f"{rel}（路径越界）")
                state["done_items"] += 1
            on_item(idx, "fail", "路径越界")
            return
        on_item(idx, "run")
        last = None
        for attempt_url in tries:
            try:
                _download_zip_file(attempt_url, dest, cancel_flag=cancel_flag,
                                   expected_size=want_size,
                                   progress_cb=lambda d, t: _emit_bytes(idx, d, t))
                # A6：下完即按清单的 fileSize / hashes.sha1 验，坏文件删掉重来；
                #     以前只是把 sha1_of(dest) **记进**清单，等于验了个寂寞。
                if job.get("verify"):
                    from .packfill import sha1_of
                    if want_size and os.path.getsize(dest) != want_size:
                        raise RuntimeError(
                            f"文件大小与清单不符（{os.path.getsize(dest)}/{want_size}）")
                    if sha1 and sha1_of(dest).lower() != sha1.lower():
                        raise RuntimeError("SHA1 与清单不符（文件可能损坏或被篡改）")
            except Exception as e:                         # noqa: BLE001
                if "已取消" in str(e):
                    on_item(idx, "skip", "已取消")
                    raise
                last = e
                try:
                    if os.path.isfile(dest):
                        os.remove(dest)
                except OSError:
                    pass
                if len(tries) > 1:
                    on_item(idx, "run", "校验失败，重试 / 换源")
                continue
            on_item(idx, "done")
            with lock:
                state["done_items"] += 1
            if records is not None:
                try:
                    from .packfill import sha1_of
                    records.append({
                        "path": str(rel).replace("\\", "/"),
                        "size": os.path.getsize(dest),
                        "sha1": sha1_of(dest),
                        "url": src_url,
                    })
                except Exception:
                    pass
            return
        with lock:
            failed.append(f"{rel or url}（{str(last)[:50]}）")
            state["done_items"] += 1
        on_item(idx, "fail", str(last or "下载失败")[:60])

    if not jobs:
        return failed
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(jobs)))) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:                         # noqa: BLE001
                if "已取消" in str(e):
                    for other in futs:
                        other.cancel()
                    break
    return failed


# ---------------------------------------------------------------- 简单安装
# 内容类型 → 落地的子文件夹名（第五十七轮：按「所选游戏」分配时也用它）
CONTENT_DIRS = {"mod": "mods", "shader": "shaderpacks",
                "resourcepack": "resourcepacks"}
CONTENT_CN = {"mod": "模组", "shader": "光影", "resourcepack": "材质包",
              "datapack": "数据包"}
# 文件名里不能出现的字符（Windows 全量）；改了名也不能让 copy2 失败
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


def datapack_target_dir(game_dir: str | None = None, instance: str = "") -> str:
    """数据包要装进存档里，取「最近玩过的世界」的 datapacks 目录。

    `instance`（第五十七轮）给了就只看**那个游戏**自己的 saves —— 按所选游戏
    分配时不能拿别的实例的存档。它一个存档都没有时退回 `<实例>/datapacks`：
    那里虽然不是官方位置，但「这个游戏的数据包」放这儿最讲得通，玩家自己
    拷进存档也方便；总比一句「还没找到存档」把安装卡死好。
    """
    base = instance or game_dir or detect_game_dir()
    saves = os.path.join(base, "saves")
    best, best_t = "", -1.0
    if os.path.isdir(saves):
        try:
            for n in os.listdir(saves):
                p = os.path.join(saves, n)
                lv = os.path.join(p, "level.dat")
                if os.path.isdir(p) and os.path.isfile(lv):
                    t = os.path.getmtime(lv)
                    if t > best_t:
                        best, best_t = n, t
        except OSError:
            pass
    if not best:
        if instance:
            return _ensure_dir(os.path.join(instance, "datapacks"))
        raise RuntimeError(
            "还没找到存档：数据包要装进世界目录，先在游戏里创建一个世界再安装")
    return _ensure_dir(os.path.join(saves, best, "datapacks"))


def content_dir(kind: str, game_dir: str | None = None, instance: str = "") -> str:
    """某种内容该落到哪个文件夹。

    `instance` = 玩家在「选择要启动的游戏」里点的那个实例目录：给了就装进
    `<实例>/mods`、`<实例>/shaderpacks`…，**这才是「按所选游戏分配」**；
    没给（还没选游戏）就退回全局的官方/应用根目录，跟以前一样。
    """
    if kind == "datapack":
        return datapack_target_dir(instance or game_dir, instance)
    sub = CONTENT_DIRS.get(kind)
    if not sub:
        raise ValueError("未知类型")
    return _ensure_dir(os.path.join(instance or game_dir or detect_game_dir(), sub))


def safe_dest_name(name: str, fallback: str) -> str:
    """把玩家输入的文件名洗干净；**扩展名只补不改**（游戏只认后缀）。

    空名 → 回退源文件名；带路径分隔符/通配符 → 换成下划线（否则 copy2 直接抛）；
    玩家把 `.jar` 删了当文件夹名写 → 自动补回去，不然装进去游戏根本不加载。
    """
    n = _BAD_FILENAME.sub("_", str(name or "").strip()).strip(" .")
    if not n:
        n = os.path.basename(str(fallback or "")) or "download"
    ext = os.path.splitext(os.path.basename(str(fallback or "")))[1]
    if ext and not n.lower().endswith(ext.lower()):
        n = os.path.splitext(n)[0] + ext
    return n[:120] or os.path.basename(str(fallback or "")) or "download"


def install_simple(kind: str, src_path: str, game_dir: str | None = None,
                   progress_cb=None, dest_name: str = "",
                   instance: str = "", target_dir: str = "") -> str:
    """安装模组/光影/材质包/数据包（复制文件）。返回目标目录。

    dest_name   （第五十七轮）保存前自定义的文件名，只有模组/数据包会被界面允许改；
                不给就用源文件名。
    instance    （第五十七轮）所选游戏的实例目录，给了就装进它的内容子文件夹。
    target_dir  （第五十七轮）**已经算好的目标文件夹**。保存对话框会把路径显示给
                玩家看，实际就必须落到那一个 —— 这里再按 instance 重算一次的话，
                万一重算和显示的不一样（例如数据包兜底到别处），玩家看到的和装进去
                的就对不上了。
    """
    folder = _ensure_dir(target_dir) if target_dir else content_dir(kind, game_dir,
                                                                    instance)
    dest = os.path.join(folder, safe_dest_name(dest_name, src_path))
    if progress_cb:
        progress_cb(0, 1)
    shutil.copy2(src_path, dest)
    if progress_cb:
        progress_cb(1, 1)
    return folder


# ---------------------------------------------------------------- 整合包识别
# 判断「这是不是整合包」看的不是扩展名（很多整合包就是 .zip），而是里面有什么：
#   · modrinth.index.json          → Modrinth .mrpack
#   · mmc-pack.json / instance.cfg → Prism / MultiMC / HMCL 导出的实例
#   · mods/ + config|options.txt   → 别人打包好的「整个 .minecraft」实例
_PACK_MARKS = ("modrinth.index.json", "mmc-pack.json", "instance.cfg",
               "minecraftinstance.json", "modpack.json", "mcbbs.packmeta")
_LAUNCHER_PACK_MARKS = ("modpack.zip", "modpack.mrpack")
# 套一层文件夹时，这一层的名字不可能是「内容目录」本身
_NOT_A_WRAPPER = {"mods", "assets", "data", "shaders", "config", "libs",
                  "overrides", "meta-inf", "resources", "saves", "logs",
                  "resourcepacks", "shaderpacks", "datapacks"}


def zip_wrapper(names) -> str:
    """压缩包是不是「整包被套了一层文件夹」→ 返回 `xxx/`；没有返回 ""。

    玩家右键「压缩到 zip」时几乎总是套一层，直接按原名解压会解成
    `<实例>/<包名>/mods`，游戏一个模组都读不到。
    """
    tops = set()
    for n in names or []:
        n = str(n).replace("\\", "/").lstrip("/")
        if not n or n.endswith("/"):
            continue
        if "/" not in n:
            return ""
        tops.add(n.split("/", 1)[0])
        if len(tops) > 1:
            return ""
    if len(tops) != 1:
        return ""
    top = tops.pop()
    if top.lower() in _NOT_A_WRAPPER:
        return ""
    return top + "/"


# ---------------------------------------------------------------- 第一百零四轮：外来整合包的边界与编码
# PCL-CE 只读研究（Modpack/Core.cs、Modrinth.cs、MultiMC.cs）里
# 四条能直接引用的硬约束，全在这段落地。原则与第 75 轮一致：被拒的条目要
# **记账**，绝不静默跳过，否则「装完了」会变成「装全了」的假象。

def fix_zip_filenames(zf) -> int:
    """修 zip 条目里没打 UTF-8 标记的中文名（cp437 → GB18030），返回修好的条数。

    Python 的 zipfile 对 `flag_bits & 0x800 == 0` 的条目一律按 cp437 解码，
    国内打包器写出来的 GBK/GB18030 名字就会变成 `ÖÐÎÄ.jar` 这种乱码；
    PCL-CE `ExtractModpackFiles()` 用 GB18030 解。做法是把 filename 按 cp437
    编码**回原始字节**，再用 GB18030 解码。少数包写的是「未打标记的 UTF-8」，
    GB18030 解出来仍是乱码，所以再准备 UTF-8 兜底（两种都失败就保持原样）。

    改完必须重建 `zf.NameToInfo`：`ZipFile.open(name)` 是查这张表而不是
    `infolist()`，只改 filename 不改表会让后面 `zf.open(raw)` 直接 KeyError。
    """
    fixed = 0
    for info in zf.infolist():
        try:
            if info.flag_bits & 0x800:
                continue                       # 已经是 UTF-8，别动
            raw = info.filename.encode("cp437")
            if not any(b >= 0x80 for b in raw):
                continue                       # 纯 ASCII，两种编码结果一致
            want = info.filename
            for enc in ("gb18030", "utf-8"):
                try:
                    cand = raw.decode(enc)
                except UnicodeDecodeError:
                    continue
                if cand and "\ufffd" not in cand and not any(
                        ord(c) < 32 for c in cand):
                    want = cand
                    break
            if want != info.filename:
                info.filename = want
                fixed += 1
        except (UnicodeEncodeError, UnicodeDecodeError, AttributeError):
            continue
    if fixed:
        zf.NameToInfo = {i.filename: i for i in zf.infolist()}
    return fixed


def safe_dep_path(p) -> str:
    """Modrinth `files[].path` 越界校验：合法返回规范化相对路径，否则空串。

    清单是外来的，`../../AppData/...`、`/etc/...`、`C:\\Windows\\...`、
    `\\\\server\\share` 都能让 `os.path.join` 写实例外面去。zip 条目侧已有
    commonpath 兜底，这条守的是**另开一条路的下载落盘**。
    """
    s = str(p or "").replace("\\", "/").strip()
    if not s or s.startswith("/") or s.startswith("//"):
        return ""
    if re.match(r"^[A-Za-z]:", s) or "\x00" in s:
        return ""
    parts = s.split("/")
    if any(x in ("..", ".") for x in parts):
        return ""                              # 规范允许 `./`，这里一并拒绝更稳
    s = "/".join(x for x in parts if x)
    return s or ""


def validate_target_path(p) -> tuple[bool, str]:
    """实例目标路径含 `!` / `;` 时拒绝（PCL-CE `_ValidateModpackTargetPath`）。

    Forge/NeoForge 的版本隔离与部分老启动器会把这两个字符当分隔符/参数符，
    装完能看见、点启动却必然报错 —— 宁可导入前说清楚，也不要留一台坏实例。
    """
    s = str(p or "")
    if "!" in s or ";" in s:
        return False, ("整合包的目标路径里不能带 ! 或 ;（Forge / 部分启动器会把"
                       "它们当成参数分隔符，装完会起不来）：" + s)
    return True, ""


_MMC_VERSION_JSON = re.compile(
    r"(?:^|/)versions/([^/]+)/\1\.json$", re.I)


def infer_minecraft_root(names) -> str:
    """压缩兜底：按 `versions/<v>/<v>.json` 正则反推 .minecraft 根前缀。

    有些「带启动器包」的 zip 里是一整个启动器目录（`.minecraft/...`、
    `PCL/.minecraft/...`），直接铺开会把 versions 解到实例根的上一层语义里。
    返回 `xxx/.minecraft/` 这一层（含尾斜杠），找不到返回空串。
    """
    for n in names or []:
        s = str(n).replace("\\", "/").lstrip("/")
        m = _MMC_VERSION_JSON.search(s)
        if m:
            pre = s[:s.lower().rfind("versions/")]
            return pre
    return ""


def parse_mmc_patches(zf, names) -> dict:
    """MultiMC / Prism 的 `patches/*.json` → 可应用的参数补丁。

    formatVersion=1 的 patch 常用 `+jvmArgs` / `+gameArgs` / `+tweakers` /
    `mainClass`。完整实现还要合并 `+libraries`、按 order 逐层覆盖，工作量大；
    这里做「参数层」这部分（能直接接到我们自己的启动命令上），组件版本仍由
    `mmc-pack.json` 的 components 负责，其余字段记进 `unapplied` 如实留痕。
    """
    out = {"jvm": [], "game": [], "unapplied": [], "files": 0}
    cand = [n for n in (names or [])
            if re.search(r"(?:^|/)patches/[^/]+\.json$", str(n), re.I)]
    for n in sorted(cand, key=lambda x: str(x).lower()):
        try:
            d = json.loads(zf.read(n))
        except Exception:                              # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        out["files"] += 1
        for k in ("+jvmArgs", "+gameArgs"):
            v = d.get(k)
            vals = v if isinstance(v, list) else ([v] if isinstance(v, str) else [])
            out["jvm" if k == "+jvmArgs" else "game"].extend(
                str(x) for x in vals if str(x or "").strip())
        tw = d.get("+tweakers")
        for t in (tw if isinstance(tw, list) else ([tw] if tw else [])):
            out["game"].extend(["--tweakClass", str(t)])
        for k in ("+libraries", "+traits", "mainClass", "assets", "fileId"):
            if d.get(k):
                out["unapplied"].append(k)
    return out


# ---------------------------------------------------------------- 解压条目规划
# 第九十四轮：以前只解 `overrides/` 这一种写法，玩家报「导入后文件不齐全」。
# 实测会漏的四种包（现在全按 PCL2 那套「整包铺开」处理）：
#   ① Modrinth 规范里的 `client-overrides/`（只给客户端的配置/模组）—— 以前**一个字节
#      都没解**；`server-overrides/` 反过来，客户端不该要；
#   ② 大写 `Overrides/`、下划线 `client_overrides/` 这类大小写/拼写变体 —— 以前按
#      `startswith("overrides/")` 精确比，一条都匹配不上；
#   ③ Windows 打包器写出来的 `mods\xxx.jar`（反斜杠）—— 以前拿原始名字比前缀，同样全落空；
#   ④ 玩家自己重打包、把 `mods/ config/` 直接放在 zip 根的「松散整合包」且带 manifest ——
#      以前只要有 manifest 就只认 `overrides/`，根目录那份整个丢掉。
_PACK_META_FILES = {"manifest.json", "modrinth.index.json", "modlist.html",
                    "manifest.json.sha1", "modrinth.index.json.sha1",
                    "manifest.json.sha512", "modrinth.index.json.sha512"}
_JUNK_DIRS = ("__macosx/", ".git/", ".svn/", "__pycache__/", ".idea/")
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini", ".gitignore"}
# 这些顶层名绝不可能只是「外面套的一层文件夹」
_PACK_TOPS = {"mods", "config", "resourcepacks", "shaderpacks", "datapacks",
              "saves", "options.txt", "servers.dat", "overrides",
              "client-overrides", "client_overrides", "server-overrides",
              "scripts", "kubejs", "defaultconfigs", "patchouli_books",
              "global_packs", "resources", "local", "logs", "crash-reports"}
_OVERRIDE_PREFIXES = ("overrides/", "client-overrides/", "client_overrides/")


def pack_entries(names, override_names=(), root_prefix="") -> list:
    """整合包条目 → `[(zip 里的原始名字, 落地相对路径)]`。

    这是**我们自己**的做法（第 94 轮）：整包都当内容铺开，只把清单本身和
    打包垃圾剔掉，再统一剥掉 `overrides/` 那一层。PCL-CE 的主路径其实是
    `ExtractModpackFiles(临时目录)` + `CopyOverrideDirectory(清单指定的 1~2 个
    目录)`，只复制指定覆写目录；只有 `InstallPackCompress` 压缩兜底才整包解。
    我们这样铺对根上松散 mods / 大写 Overrides / 反斜杠 / client-overrides
    更全，代价是必须显式剔垃圾与服务端专属目录（下面都做了）。

    第一百零四轮新增两个入参：
    - `override_names`：CF `manifest.json` 的 `overrides` 字段里自定义的目录名

    - `root_prefix`：`infer_minecraft_root()` 反推出来的 `.minecraft/` 层，
      先剥掉它再走原来的「剥包装 ≤2 层」，带启动器包的 zip 才不会错位。
    """
    prefixes = list(_OVERRIDE_PREFIXES)
    for o in override_names:
        k = str(o or "").replace("\\", "/").strip("/").lower() + "/"
        if len(k) > 1 and k not in prefixes:
            prefixes.append(k)
    rp = str(root_prefix or "").replace("\\", "/").rstrip("/") + "/" \
        if root_prefix else ""
    ents = []
    for n in names or []:
        s = str(n).replace("\\", "/").lstrip("/")
        if s and not s.endswith("/"):
            if rp and s.lower().startswith(rp.lower()):
                s = s[len(rp):]
            ents.append([n, s])
    # ① 剥外层包装文件夹（`包名/…`、`.minecraft/…`），最多两层
    for _ in range(2):
        heads, bad = set(), False
        for _raw, s in ents:
            if "/" not in s:
                bad = True
                break
            heads.add(s.split("/", 1)[0])
            if len(heads) > 1:
                bad = True
                break
        if bad or not heads:
            break
        head = heads.pop()
        if head.lower() in _PACK_TOPS:      # 根上就是内容目录 → 不是包装
            break
        pre = head + "/"
        ents = [[raw, s[len(pre):]] for raw, s in ents]
    # ② 逐条定落地路径
    out = []
    for raw, s in ents:
        low = s.lower()
        if ".." in s.split("/"):
            continue                        # 越界路径（zip slip）：绝不写到实例外面
        if low in _PACK_META_FILES:         # 清单/说明：单独处理，不铺
            continue
        parts = low.split("/")
        if any(p in _JUNK_FILES for p in parts) or low.startswith(_JUNK_DIRS):
            continue
        if low.startswith("server-overrides/"):   # 服务端专用，客户端不要
            continue
        # 落盘优先级：overrides / 自定义覆写目录(0) → 根上直接放的(1)
        # → client-overrides(2)。client 放最后，同路径撞上以客户端那份为准。
        rank = 1
        for pre in prefixes:
            if low.startswith(pre):
                s = s[len(pre):]
                rank = 2 if "client" in pre else 0
                break
        if not s or s.endswith("/"):
            continue
        out.append((rank, raw, s))
    out.sort(key=lambda t: t[0])            # 稳定排序：同优先级保持 zip 里的顺序
    return [(raw, s) for _r, raw, s in out]


def _pack_meta(data: dict) -> dict:
    """从 modrinth.index.json / manifest.json 里读名字、MC 版本、加载器"""
    out = {"name": str((data or {}).get("name") or ""), "mc": "",
           "loader": "", "loader_ver": ""}
    deps = (data or {}).get("dependencies") or {}
    if isinstance(deps, dict):
        out["mc"] = str(deps.get("minecraft") or "")
        for k, v in (("neoforge", "neoforge"), ("forge", "forge"),
                     ("fabric-loader", "fabric"), ("quilt-loader", "quilt")):
            if deps.get(k):
                out["loader"], out["loader_ver"] = v, str(deps[k])
                break
    mcobj = (data or {}).get("minecraft") or {}
    if isinstance(mcobj, dict):
        out["mc"] = out["mc"] or str(mcobj.get("version") or "")
        for l in (mcobj.get("modLoaders") or []):
            lid = str(l.get("id") or "")
            for pre, kind in (("neoforge-", "neoforge"), ("forge-", "forge"),
                              ("fabric-", "fabric"), ("quilt-", "quilt")):
                if lid.startswith(pre):
                    out["loader"], out["loader_ver"] = kind, lid[len(pre):]
                    break
            if out["loader"]:
                break
    return out


# ---------------------------------------------------------------- 整合包加载器坐标
# 第一百零八轮：以前 install_modpack 只从 modrinth.index.json / manifest.json 里
# 读 dependencies。可 pack_kind 明明还认识 HMCL / MCBBS / MultiMC
# 客户端实例 / 带启动器包 —— 那几种格式的 MC 版本和加载器坐标在安装时
# **一个字节都没被读**：deps 空 → lver 空 → ensure_runtime 里 `and lver`
# 那段整段跳过 → 官方安装器从不下载，导入「成功」却永远缺模组加载器。
# 这里把「读坐标」统一成一份，识别层读到的东西安装层必须接得住。
_LOADER_DEP_KEYS = ("neoforge", "forge", "fabric-loader", "quilt-loader")


def _normalize_loader_ver(kind, ver, mc=""):
    """forge-47.4.10 / 1.20.1-forge-47.4.10 / 47.4.10 统一成 47.4.10。

    绝不猜版本号：拿不到带数字的版本串就返回 ''（宁可报错不许静默降级）。
    """
    v = str(ver or "").strip()
    if not v:
        return ""
    mcs = str(mc or "")
    # 两种顺序都见过：1.20.1-forge-47.4.10 / forge-1.20.1-47.4.10
    for _ in range(2):
        if mcs and v.lower().startswith(mcs.lower() + "-"):
            v = v[len(mcs) + 1:]
        pre = str(kind or "").lower() + "-"
        if v.lower().startswith(pre):
            v = v[len(pre):]
    return v if re.search(r"\d", v) else ""


def _deps_from_meta(data) -> dict:
    """MR / CF / HMCL / MCBBS / CF 实例 / Modrinth 实例的清单 → 统一 deps。"""
    out = {}
    if not isinstance(data, dict):
        return out

    def put_mc(v):
        v = str(v or "").strip()
        if v and not out.get("minecraft"):
            out["minecraft"] = v

    def put(kind, v):
        key = {"fabric": "fabric-loader", "quilt": "quilt-loader",
               "forge": "forge", "neoforge": "neoforge"}.get(kind)
        if not key:
            return
        ver = _normalize_loader_ver(kind, v, out.get("minecraft", ""))
        if ver and not out.get(key):
            out[key] = ver

    # ① 通用格式
    d = data.get("dependencies") or {}
    if isinstance(d, dict):
        put_mc(d.get("minecraft"))
        for k in _LOADER_DEP_KEYS:
            if d.get(k):
                put({"fabric-loader": "fabric", "quilt-loader": "quilt"}.get(k, k),
                    d[k])
    mo = data.get("minecraft") or {}
    if isinstance(mo, dict):
        put_mc(mo.get("version"))
        for l in (mo.get("modLoaders") or []):
            lid = str((l or {}).get("id") or "")
            for pre, kind in (("neoforge-", "neoforge"), ("forge-", "forge"),
                             ("fabric-", "fabric"), ("quilt-", "quilt")):
                if lid.lower().startswith(pre):
                    put(kind, lid[len(pre):])
                    break
    # ② HMCL / MCBBS：mcVersion + xxxVersion + addons[]
    put_mc(data.get("mcVersion") or data.get("gameVersion") or "")
    for fld, kind in (("forgeVersion", "forge"), ("neoforgeVersion", "neoforge"),
                      ("fabricVersion", "fabric"), ("quiltVersion", "quilt")):
        if data.get(fld):
            put(kind, data[fld])
    addons = data.get("addons") or []
    if isinstance(addons, list):
        for a in addons:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id") or "").lower()
            ver = a.get("version") or a.get("ver") or ""
            if aid == "minecraft":
                put_mc(ver)
            elif aid in ("forge", "neoforge", "fabric", "fabric-loader",
                         "quilt", "quilt-loader"):
                put(aid.split("-")[0], ver)
    # ③ 第三方客户端实例 minecraftinstance.json
    bml = data.get("baseModLoader") or {}
    if isinstance(bml, dict):
        nm = str(bml.get("name") or "")
        for pre, kind in (("neoforge-", "neoforge"), ("forge-", "forge"),
                          ("fabric-", "fabric"), ("quilt-", "quilt")):
            if nm.lower().startswith(pre):
                put(kind, nm[len(pre):])
                break
        if bml.get("forgeVersion"):
            put("forge", bml["forgeVersion"])
    # ④ Modrinth App 的 profile.json
    if data.get("game_version"):
        put_mc(data["game_version"])
    ld = str(data.get("loader") or "").lower().split("-")[0]
    if ld in ("forge", "neoforge", "fabric", "quilt") and data.get("loader_version"):
        put(ld, data["loader_version"])
    return out


def _deps_from_mmc(data) -> dict:
    """MultiMC / Prism 的 mmc-pack.json → components[] 里认加载器。"""
    out = {}
    if not isinstance(data, dict):
        return out
    for c in (data.get("components") or []):
        if not isinstance(c, dict):
            continue
        uid = str(c.get("uid") or "").lower()
        ver = str(c.get("version") or c.get("cachedVersion") or "")
        if not ver:
            continue
        if uid == "net.minecraft":
            out.setdefault("minecraft", ver)
        elif uid.startswith("net.minecraftforge"):
            out.setdefault("forge", _normalize_loader_ver("forge", ver))
        elif uid.startswith("net.neoforged"):
            out.setdefault("neoforge", _normalize_loader_ver("neoforge", ver))
        elif "fabric-loader" in uid:
            out.setdefault("fabric-loader", _normalize_loader_ver("fabric", ver))
        elif "quilt-loader" in uid:
            out.setdefault("quilt-loader", _normalize_loader_ver("quilt", ver))
    return {k: v for k, v in out.items() if v}


def _deps_from_instance_cfg(text) -> dict:
    """MultiMC 的 instance.cfg：IntendedVersion=1.20.1-forge-47.4.10。"""
    out = {}
    m = re.search(r"(?im)^\s*IntendedVersion\s*=\s*(\S+)", text or "")
    if not m:
        return out
    mm = re.match(r"^(\d+\.\d+(?:\.\d+)?)-(forge|neoforge|fabric|quilt)-(.+)$",
                  m.group(1).strip(), re.I)
    if not mm:
        return out
    kind = mm.group(2).lower()
    out["minecraft"] = mm.group(1)
    out[{"fabric": "fabric-loader", "quilt": "quilt-loader"}.get(kind, kind)] = \
        mm.group(3)
    return out


def read_pack_deps(zf, lowmap, meta_raw, depth=0) -> tuple[dict, str]:
    """从 zip 里**任意一种**整合包清单读出 MC / 加载器坐标 → (deps, source)。

    只读 zip，不联网不写盘；读不到就留空，绝不编造版本号（第九十五轮铁律）。
    带启动器包（modpack.zip / .mrpack）会再读一层内嵌清单。
    """
    out, source = {}, ""

    def grab(name):
        raw = meta_raw(name)
        if not raw:
            return None
        try:
            return json.loads(zf.read(raw))
        except Exception:                                  # noqa: BLE001
            return None

    def merge(d, src):
        nonlocal source
        for k, v in (d or {}).items():
            if v and not out.get(k):
                out[k] = v
        if d and not source:
            source = src

    for nm, src in (("modrinth.index.json", "mrpack"),
                    ("modpack.json", "hmcl"),
                    ("mcbbs.packmeta", "mcbbs"),
                    ("profile.json", "modrinth-profile")):
        d = grab(nm)
        if d is not None:
            merge(_deps_from_meta(d), src)
    d = grab("mmc-pack.json")
    if d is not None:
        merge(_deps_from_mmc(d), "multimc")
    raw = meta_raw("instance.cfg")
    if raw:
        try:
            merge(_deps_from_instance_cfg(zf.read(raw).decode("utf-8", "replace")),
                  "multimc-cfg")
        except Exception:                                  # noqa: BLE001
            pass
    if depth < 1:
        for mark in _LAUNCHER_PACK_MARKS:
            raw = lowmap.get(mark) or ""
            if not raw:
                cand = [n for n in lowmap.values()
                        if n.lower().endswith("/" + mark)]
                raw = cand[0] if cand else ""
            if not raw:
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(raw))) as iz:
                    inn = [n.replace("\\", "/") for n in iz.namelist()
                           if not n.endswith("/")]
                    w = zip_wrapper(inn)
                    imap = {(n[len(w):] if w and n.startswith(w) else n).lower(): n
                            for n in inn}

                    def imeta(name):
                        if name in imap:
                            return imap[name]
                        c = [n for n in inn if n.lower().endswith("/" + name)]
                        c.sort(key=lambda x: x.count("/"))
                        return c[0] if c else ""

                    d2, s2 = read_pack_deps(iz, imap, imeta, depth + 1)
                    merge(d2, "launcherpack:" + s2)
            except Exception:                              # noqa: BLE001
                pass
            break
    return out, source


def pack_kind(path: str) -> dict:
    """这个文件到底是什么？（第六十二轮：拖进窗口时先认一认）

    返回 {"kind","format","name","mc","loader","loader_ver","wrapper","why"}：
      · kind   = modpack / mod / resourcepack / shader / datapack / unknown
      · format = mrpack / instance / raw（只有整合包才有意义）
      · why    = 一句给玩家看的判断依据（"里面没有整合包清单，只有 mods 和其它配置"）
    判断顺序很重要：先认清单文件，再认「jar 改名成 zip」，最后才看目录结构。
    """
    p = str(path or "")
    base = os.path.basename(p)
    out = {"kind": "unknown", "format": "", "name": os.path.splitext(base)[0],
           "mc": "", "loader": "", "loader_ver": "", "wrapper": "", "why": ""}
    if not os.path.isfile(p):
        out["why"] = "文件不见了"
        return out
    low = base.lower()
    if low.endswith(".mrpack"):
        out.update(kind="modpack", format="mrpack", why="Modrinth 整合包（.mrpack）")
        try:
            with zipfile.ZipFile(p) as zf:
                meta = _pack_meta(json.loads(zf.read("modrinth.index.json")))
            for k in ("name", "mc", "loader", "loader_ver"):
                if meta.get(k):
                    out[k] = meta[k]
        except Exception:                              # noqa: BLE001
            pass
        return out
    if low.endswith(".jar"):
        out.update(kind="mod", why="这是模组（.jar），不是整合包")
        return out
    if not low.endswith(".zip"):
        out["why"] = "只认 .zip / .mrpack"
        return out
    try:
        with zipfile.ZipFile(p) as zf:
            raw = [n.replace("\\", "/") for n in zf.namelist()]
            files = [n for n in raw if not n.endswith("/")]
            wrap = zip_wrapper(files)
            out["wrapper"] = wrap
            inner = [n[len(wrap):] if wrap else n for n in files]
            lowset = {n.lower() for n in inner}

            def read(name):
                with zf.open(wrap + name) as f:
                    return f.read()

            # ---- ① 有清单文件 = 整合包 ----
            # 第一百零四轮：补 HMCL / MCBBS / 带启动器包三种识别。
            # 这三种必须排在「manifest.json」通用识别之前，否则 MCBBS 的
            # manifest（只有 addons、没有 minecraft 段）会被当成认不出的普通 zip。
            if "modpack.json" in lowset:
                out.update(kind="modpack", format="hmcl",
                           why="里面有 modpack.json（HMCL 整合包）")
                try:
                    d = json.loads(read("modpack.json"))
                    out["name"] = str(d.get("name") or out["name"])
                    out["mc"] = str(d.get("mcVersion") or d.get("gameVersion") or "")
                    fv = str(d.get("forgeVersion") or "")
                    nf = str(d.get("neoforgeVersion") or "")
                    if nf:
                        out["loader"], out["loader_ver"] = "neoforge", nf
                    elif fv:
                        out["loader"], out["loader_ver"] = "forge", fv
                except Exception:                      # noqa: BLE001
                    pass
                return out
            if "mcbbs.packmeta" in lowset:
                out.update(kind="modpack", format="mcbbs",
                           why="里面有 mcbbs.packmeta（MCBBS 整合包）")
                try:
                    d = json.loads(read("mcbbs.packmeta"))
                    out["name"] = str(d.get("name") or out["name"])
                    out["mc"] = str((d.get("mcVersion") or d.get("gameVersion") or ""))
                except Exception:                      # noqa: BLE001
                    pass
                return out
            if any(m in lowset for m in _LAUNCHER_PACK_MARKS):
                out.update(kind="modpack", format="launcherpack",
                           why="里面套着一个 modpack.zip / modpack.mrpack（带启动器包）")
                return out
            if "modrinth.index.json" in lowset:
                out.update(kind="modpack", format="mrpack",
                           why="里面有 modrinth.index.json（Modrinth 整合包）")
                try:
                    meta = _pack_meta(json.loads(read("modrinth.index.json")))
                    for k in ("name", "mc", "loader", "loader_ver"):
                        if meta.get(k):
                            out[k] = meta[k]
                except Exception:                      # noqa: BLE001
                    pass
                return out
            if "manifest.json" in lowset:
                has_ov = any(n.lower().startswith("overrides/") for n in inner)
                try:
                    data = json.loads(read("manifest.json"))
                except Exception:                      # noqa: BLE001
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if isinstance(data.get("addons"), (list, dict)):
                    out.update(kind="modpack", format="mcbbs",
                               why="manifest.json 里有 addons（MCBBS 整合包）")
                    out["name"] = str(data.get("name") or out["name"])
                    out["mc"] = str((data.get("minecraft") or {}).get("version")
                                    or data.get("mcVersion") or "")
                    return out
            if any(m in lowset for m in _PACK_MARKS):
                fmt = "instance"
                out.update(kind="modpack", format=fmt,
                           why="这是别的启动器导出的整合包实例")
                try:
                    if "mmc-pack.json" in lowset:
                        d = json.loads(read("mmc-pack.json"))
                        for c in (d.get("components") or []):
                            uid = str(c.get("uid") or "").lower()
                            if uid == "net.minecraft":
                                out["mc"] = str(c.get("version") or "")
                            elif "forge" in uid:
                                out["loader"] = "neoforge" if "neo" in uid else "forge"
                                out["loader_ver"] = str(c.get("version") or "")
                            elif "fabric" in uid:
                                out["loader"] = "fabric"
                                out["loader_ver"] = str(c.get("version") or "")
                    elif "minecraftinstance.json" in lowset:
                        meta = _pack_meta(json.loads(read("minecraftinstance.json")))
                        for k in ("mc", "loader", "loader_ver"):
                            if meta.get(k):
                                out[k] = meta[k]
                except Exception:                      # noqa: BLE001
                    pass
                return out

            # ---- ② 是模组 jar 改名成 .zip？----
            if ("meta-inf/manifest.mf" in lowset
                    and ({"fabric.mod.json", "meta-inf/mods.toml",
                          "meta-inf/neoforge.mods.toml", "mcmod.info"} & lowset)):
                out.update(kind="mod", why="这是模组（jar 改名成 zip 了）")
                return out

            # ---- ③ 光影 / 材质包 / 数据包 ----
            if any(n.lower().startswith("shaders/") or "/shaders/" in n.lower()
                   for n in inner):
                out.update(kind="shader", why="里面有 shaders/ 目录（光影包）")
                return out
            if "pack.mcmeta" in lowset:
                if any(n.lower().startswith("data/") for n in inner):
                    out.update(kind="datapack", why="里面有 pack.mcmeta + data/（数据包）")
                else:
                    out.update(kind="resourcepack",
                               why="里面有 pack.mcmeta + assets/（材质包）")
                return out

            # ---- ④ 没有清单、但明显是整个游戏目录 = 松散整合包 ----
            # 第一百零四轮：压缩兜底先看 `versions/<v>/<v>.json`。带启动器的整包
            # 常常是 `<名字>/.minecraft/versions/...`，反推出根前缀后由
            # pack_entries(root_prefix=...) 剥掉，才不会把 versions 解成两层。
            mroot = infer_minecraft_root(inner)
            if mroot:
                out.update(kind="modpack", format="raw", wrapper=mroot,
                           why=("里面有 versions/<v>/<v>.json，按 .minecraft 根 "
                                + mroot + " 反推（压缩兜底）"))
                return out
            subs = {n.split("/", 1)[0].lower() for n in inner if "/" in n}
            if "mods" in subs and ({"config", "options.txt", "saves",
                                    "resourcepacks", "shaderpacks"} & subs):
                out.update(kind="modpack", format="raw",
                           why="里面是完整的游戏目录（mods/ + 配置），按整合包处理")
                return out
            if "mods" in subs:
                out.update(kind="modpack", format="raw",
                           why="里面有一个 mods/ 文件夹，按整合包处理")
                return out
    except zipfile.BadZipFile:
        out["why"] = "这个 zip 打不开（可能没下载完）"
        return out
    except Exception as e:                             # noqa: BLE001
        out["why"] = f"读不出来：{str(e)[:60]}"
        return out
    out["why"] = "里面没有整合包清单，也没有 mods / 光影 / 材质包的痕迹"
    return out


# ---------------------------------------------------------------- 整合包安装
def install_modpack(path: str, progress_cb=None, cancel_flag=None,
                    on_details=None, on_detail=None, bytes_cb=None,
                    icon_url: str = "", game_dir: str | None = None) -> dict:
    """安装整合包（.mrpack 等）。自动安排进官方启动器。
    返回 {name, instance_dir, profile_ok, warnings:[...], loader_hint,
          loader_missing}
    `loader_missing=True` 表示加载器没装上：文件都解压好了，但**不给它绑运行
    版本**（绑原版 = 模组一个都不加载），要玩家先装好加载器再「修复整合包档案」。
    progress_cb(done, total, text)
    on_details(rows)            一次性给出依赖明细 [{name,size,status}]
    on_detail(index, status)    更新第 index 条明细的状态
    bytes_cb(done, total)       字节级进度（并发下载时由引擎汇总）
    icon_url                    项目页图标，装完存成 <实例>/icon.png，让游戏管理显示整合包自己的图标
    game_dir                    装到哪条路径（第四十五轮：应用 / 官方），缺省按设置
    """
    _cb = progress_cb or (lambda done, total, text="": None)

    def cb(done, total, text=""):
        _cb(done, total, text)

    def mark(i, status, note=""):
        if on_detail and i is not None and i >= 0:
            try:
                on_detail(i, status, note)
            except Exception:
                pass

    game_dir = game_dir or download_dir()
    result = {"name": os.path.splitext(os.path.basename(path))[0],
              "instance_dir": "", "profile_ok": False, "warnings": [],
              "loader_hint": "", "runtime_notes": [], "icon_saved": False,
              "loader_missing": False}
    cb(0, 100, "读取整合包信息…")

    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        raise RuntimeError(f"无法打开整合包：{e}")

    with zf:
        # 第一百零四轮：没打 UTF-8 标记的中文条目名先按 cp437→GB18030 修回来，
        # 必须在 namelist() 之前做，否则解压规划里到处都是 `ÖÐÎÄ.jar`。
        result["zip_names_fixed"] = fix_zip_filenames(zf)
        names = zf.namelist()
        # 第六十二轮：整包被套了一层文件夹（右键「压缩到 zip」的默认结果）时，
        # 所有相对路径都以那一层开头。直接按原名解压会解成
        # `<实例>/<包名>/mods`，游戏一个模组都读不到 —— 这里统一剥掉。
        # 第九十四轮：反斜杠（Windows 打包器）也一并归一化，并且清单文件按
        # **小写名**查（有些包写 `Manifest.json`）。
        _files = [n.replace("\\", "/") for n in names if not n.endswith("/")]
        wrap = zip_wrapper(_files)
        rels = [(n, (n[len(wrap):] if wrap and n.startswith(wrap) else n))
                for n in _files]
        lowmap = {s.lower(): raw for raw, s in rels}

        def _meta_raw(name):
            """找清单文件在 zip 里的真名：先认根上/剥掉包装后那份，再退「从浅到深」
            找同名的（有的包把它放进了一层文件夹，也有 config 里恰好同名的，
            所以按层数排序取最浅的那份）。"""
            if name in lowmap:
                return lowmap[name]
            cand = [_n for _n in _files if _n.lower().endswith("/" + name)]
            cand.sort(key=lambda _x: _x.count("/"))
            return cand[0] if cand else ""

        pack_name = result["name"]
        deps = {}
        file_list = []          # [(url, relative_path, size)]
        dep_meta = {}           # index -> {"urls", "sha1", "size"}（第 104 轮）
        override_names = []
        root_prefix = infer_minecraft_root(_files)
        cf_manifest = {}
        # A9：env.client=optional 不弹窗，明确默认「下载」，可由配置改成跳过，
        #     无论哪种都把看见/跳过的文件记账（result + 实例 .mcassist.json）。
        optional_default = bool(CONFIG.get("pack_optional_client", True))
        optional_seen, optional_skipped = [], []
        rejected = []           # 被安全校验拒掉的清单条目：结尾要算「没装全」

        if _meta_raw("modrinth.index.json"):           # Modrinth .mrpack
            idx = json.loads(zf.read(_meta_raw("modrinth.index.json")))
            pack_name = idx.get("name") or pack_name
            deps = idx.get("dependencies", {})
            for f in idx.get("files", []):
                urls = [str(u) for u in (f.get("downloads") or []) if u]
                env = f.get("env") or {}
                client = str(env.get("client") or "")
                if client == "unsupported":
                    continue            # 服务端专用文件，客户端别下
                if client == "optional":
                    optional_seen.append(str(f.get("path") or ""))
                    if not optional_default:
                        optional_skipped.append(str(f.get("path") or ""))
                        continue
                rel = safe_dep_path(f.get("path"))
                if not rel or not urls:
                    # 第 75 轮铁律：绝不能「装完了」却悄悄少一个文件。
                    _nm = str(f.get("path") or f.get("downloads") or "")[:70]
                    rejected.append(_nm)
                    result["warnings"].append(
                        "Modrinth 清单有文件被拒绝（路径越界 / 没有下载地址）：" + _nm)
                    continue
                h = f.get("hashes") or {}
                file_list.append((urls[0], rel, int(f.get("fileSize") or 0)))
                dep_meta[len(file_list) - 1] = {
                    "urls": urls, "sha1": str(h.get("sha1") or ""),
                    "size": int(f.get("fileSize") or 0)}
            result["optional_files"] = {
                "client": "download" if optional_default else "skip",
                "seen": optional_seen[:20], "skipped": optional_skipped[:20]}
            if optional_skipped:
                result["warnings"].append(
                    f"按设置跳过了 {len(optional_skipped)} 个可选客户端文件"
                    "（env.client=optional）")

        # 第一百零八轮：pack_kind 认识多种整合包格式，上面只填了

        # MR / CF 两种的 deps。这里把其余格式的 MC / 加载器坐标一并读回来 ——
        # 少了 lver，ensure_runtime 里 `and lver` 那一段整段跳过，官方安装器
        # 一次都不会下载，玩家看到的就是「导入成功但始终缺模组加载器」。
        # 标准 MR / CF 包的 deps 已经齐了，循环里 `not deps.get(k)` 不会覆盖它。
        more, dep_source = read_pack_deps(zf, lowmap, _meta_raw)
        for _k, _v in (more or {}).items():
            if _v and not deps.get(_k):
                deps[_k] = _v
        result["dep_source"] = dep_source

        # 实例目录放进 versions/（PCL2 的版本隔离语义）：官启版本列表里能看到它，
        # 存档/模组也只归它一份，和游戏根目录、其它版本都不串。
        _target = isolated_dir(game_dir, pack_name)
        ok_path, why_path = validate_target_path(_target)
        if not ok_path:
            raise RuntimeError(why_path)
        instance_dir = _ensure_dir(_target)
        result["name"] = pack_name
        result["instance_dir"] = instance_dir

        # 依赖明细：解压 + 每个依赖文件 + 运行版本（进度窗里逐条显示「正在下什么」）
        _shown = file_list[:300]
        rows = [{"name": "解压整合包文件", "status": "run"}]
        for _u, _rel, _sz in _shown:
            nm = _rel or _u
            rows.append({"name": str(nm), "size": int(_sz or 0), "status": "wait"})
        if len(file_list) > len(_shown):
            rows.append({"name": f"其余 {len(file_list) - len(_shown)} 个依赖文件",
                         "status": "wait"})
        rows.append({"name": "准备运行版本", "status": "wait"})
        if on_details:
            try:
                on_details(rows)
            except Exception:
                pass
        row_overrides = 0
        row_extra = 1 + len(_shown) if len(file_list) > len(_shown) else -1
        row_runtime = len(rows) - 1

        # 1) 解压整合包里的文件（我们的整包铺开做法，见 pack_entries 的说明；
        #    PCL-CE 主路径只复制清单指定的 overrides 目录）
        plan = pack_entries(names, override_names=override_names,
                            root_prefix=root_prefix)
        result["override_dirs"] = list(override_names)
        result["minecraft_root"] = root_prefix
        # A8（尽量实现）：MultiMC / Prism 的 patches/*.json —— 参数层接到本实例
        # 的自定义 JVM / 游戏参数上；+libraries / traits / mainClass 这些没法
        # 直接套进我们的版本 json，记进 unapplied 如实列为遗留。
        try:
            patches = parse_mmc_patches(zf, names)
        except Exception:                              # noqa: BLE001
            patches = {"jvm": [], "game": [], "unapplied": [], "files": 0}
        if patches.get("files"):
            result["mmc_patches"] = {
                "files": patches["files"], "jvm": patches["jvm"][:20],
                "game": patches["game"][:20],
                "unapplied": sorted(set(patches["unapplied"]))}
            try:
                from . import launchprefs as _lp
                _kv = {}
                if patches["jvm"]:
                    _kv["jvm"] = " ".join(patches["jvm"])
                if patches["game"]:
                    _kv["game"] = " ".join(patches["game"])
                if _kv:
                    _lp.set_game_conf({"path": instance_dir}, **_kv)
            except Exception as _e:                    # noqa: BLE001
                result["warnings"].append(
                    f"MultiMC 参数补丁没能应用：{str(_e)[:60]}")
            if patches["unapplied"]:
                result["warnings"].append(
                    "MultiMC 补丁里还有未应用字段（"
                    + "、".join(sorted(set(patches["unapplied"]))) + "）")
        cb(5, 100, "解压整合包文件…")
        zipped, lost_zip = [], []
        _root = os.path.abspath(instance_dir)
        for _zi, (raw, rel) in enumerate(plan):
            # 第九十八轮：解压这一段也要能取消。上千个文件的整合包解压要几秒到
            # 几十秒，以前这里只在「某个文件出错」时才顺便看一眼取消标志，
            # 玩家就在这段时间里看到窗口一直停在「正在取消」。
            if cancel_flag and cancel_flag[0] and _zi % 64 == 0:
                raise RuntimeError("已取消")
            target = os.path.join(instance_dir, rel.replace("/", os.sep))
            # 再兜一道：解出来的路径必须还在实例目录里（压缩包是外来的，不可信）
            try:
                if os.path.commonpath([_root, os.path.abspath(target)]) != _root:
                    lost_zip.append(f"{rel}（路径越界）")
                    continue
            except ValueError:
                continue
            try:
                _ensure_dir(os.path.dirname(target))
                with zf.open(raw) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d)
                zipped.append(rel)
            except Exception as _e:                    # noqa: BLE001
                lost_zip.append(f"{rel}（{str(_e)[:40]}）")
                if cancel_flag and cancel_flag[0]:
                    raise RuntimeError("已取消")
        result["from_zip"] = len(zipped)
        if lost_zip:
            result["warnings"].append(
                f"{len(lost_zip)} 个文件没能从整合包里解压出来："
                + "、".join(lost_zip[:3])
                + (f" 等 {len(lost_zip)} 个" if len(lost_zip) > 3 else ""))
        # 记下「整合包里该有哪些文件」——以后核对能发现「解压时就没落地」的缺口
        try:
            with open(os.path.join(instance_dir, ".mcassist_zip.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"files": zipped}, f, ensure_ascii=False)
        except Exception:                              # noqa: BLE001
            pass

        # 2) 并发下载依赖文件
        mark(row_overrides, "done")
        total = len(file_list)
        failed = []
        records = []            # 实际下到的文件（path/size/sha1/url）→ 落进清单
        jobs = []
        for i, (u, r, sz) in enumerate(file_list):
            ridx = (i + 1) if (i + 1) <= len(_shown) else row_extra
            sub, hint_unknown, modules = "", False, []
            meta = dep_meta.get(i, {})
            jobs.append({"index": i, "row": ridx, "url": u, "rel": r, "sub": sub,
                         "dir": instance_dir, "size": int(sz or 0),
                         "hint_unknown": hint_unknown, "modules": modules,
                         # A6：清单自带的备用下载地址与 sha1 / fileSize
                         "urls": list(meta.get("urls") or [u]),
                         "sha1": str(meta.get("sha1") or ""),
                         "verify": bool(meta.get("sha1") or meta.get("size"))})
        if jobs:
            failed = _download_deps(
                jobs, cb, cancel_flag,
                on_item=lambda i, st, note="", _jobs=jobs: mark(
                    _jobs[i]["row"], st, note),
                total=total, shown=len(_shown), bytes_cb=bytes_cb,
                records=records)
            result["warnings"].extend(f"下载失败：{f}" for f in failed)
            if cancel_flag and cancel_flag[0]:
                raise RuntimeError("已取消")

        # 3) 运行基础：必须让 <游戏目录>/versions 里存在可启动的版本，
        #    否则官方启动器点开始游戏会报 REQUEST_FAILED /
        #    Unable to prepare assets for download（lastVersionId 解析为空）。
        mark(row_runtime, "run")
        mc = deps.get("minecraft") or ""
        kind = ""
        lver = ""
        for k in ("neoforge", "forge", "fabric-loader", "quilt-loader"):
            if deps.get(k):
                kind = {"fabric-loader": "fabric",
                        "quilt-loader": "quilt"}.get(k, k)
                lver = deps[k]
                break
        vid = ""
        loader_hint = ""
        if not mc or (kind and not lver):
            # 第一百零八轮：清单没写全时，照已解压出来的实例目录再认一次。
            # importer.instance_env 是三合一识别器（MMC / CF 实例 / MR 实例），
            # 还会从 mods 里的 forge-1.20.1-47.4.10.jar 这类文件名补回版本号；
            # 以前这里只调 runtime.infer_instance_env，它只返回 (mc, kind)，
            # lver 永远是空 → 加载器安装分支永远不触发。
            try:
                from . import importer as _imp
                mc2, kind2, lver2 = _imp.instance_env(instance_dir)
            except Exception:                              # noqa: BLE001
                mc2, kind2, lver2 = "", "", ""
            if not mc2:
                mc3, kind3 = runtime.infer_instance_env(instance_dir)
                mc2, kind2 = mc2 or mc3, kind2 or kind3
            mc = mc or mc2
            kind = kind or kind2
            lver = lver or lver2
        result["loader_spec"] = {"mc": mc, "loader": kind, "loader_ver": lver,
                                 "source": result.get("dep_source") or "inferred"}
        if mc:
            cb(90, 100, "准备运行版本…")
            if cancel_flag and cancel_flag[0]:
                raise RuntimeError("已取消")
            # 第一百零九轮：加载器安装（下载安装器 + 跑官方 installer 1~3 分钟）
            # 以前固定 `cb(93, 100, t)`，进度条焊在 93% 不动，玩家不知道在等什么。
            # 现在按阶段推进：下载安装器 94→96（有 Content-Length 时按 MB 比例），
            # 运行安装器 96→99（按已跑秒数缓慢走），文本全程透传到导入详情。
            _rt_pct = [92]

            def _rt_prog(text):
                t = str(text or "")
                try:
                    if "下载安装器" in t:
                        _rt_pct[0] = max(_rt_pct[0], 94)
                        m = re.search(r"([\d.]+) / ([\d.]+) MB", t)
                        if m:
                            frac = float(m.group(1)) / max(0.001, float(m.group(2)))
                            _rt_pct[0] = 94 + int(2 * max(0.0, min(1.0, frac)))
                    elif "安装器运行中" in t:
                        _rt_pct[0] = min(99, max(_rt_pct[0], 96) + 1)
                    elif "安装器" in t:
                        _rt_pct[0] = max(_rt_pct[0], 96)
                    else:
                        _rt_pct[0] = min(99, _rt_pct[0] + 1)
                except Exception:                          # noqa: BLE001
                    pass
                cb(_rt_pct[0], 100, t)
                mark(row_runtime, "run", t[:70])

            vid, notes = runtime.ensure_runtime(
                game_dir, mc, kind, lver,
                progress=_rt_prog, cancel_flag=cancel_flag,
                # 第一百零八轮：导入是「从零装一份」，不能沿用本机那份
                # 版本 json 在、jar 不全的半成品（见 C 组验收）。
                verify_loader=True)
            result["runtime_notes"] = notes
            for n in notes:
                if "缺少" in n:
                    result["warnings"].append(n)
            if not vid:
                loader_hint = notes[-1] if notes else ""
                cn = runtime.LOADER_CN.get(kind, kind)
                if kind and not lver:
                    # 第一百零八轮：这不是网络问题，是这份包压根没写加载器版本号。
                    # 不猜版本、不拿空版本号拼 installer URL（必然 404，
                    # 玩家重装多少次都过不去），明确说清缺的是什么。
                    result["loader_missing"] = True
                    result["warnings"].append(
                        f"整合包没写 {cn} 的版本号，不能凭空猜一个去下载安装器。"
                        f"请到「设置 → 启动项」为 Minecraft {mc} 手动安装对应"
                        f"版本的 {cn}，或换一份带完整清单的整合包重新导入。")
                elif kind:
                    # 第九十五轮：这里以前是静默的。ensure_runtime 装加载器失败 →
                    # vid 空 → 档案兜底绑到纯原版 → 「导入成功、能进游戏、模组全不加载」。
                    # 现在明确记账，界面会照 warnings 弹提示，且档案不再被绑到原版。
                    result["loader_missing"] = True
                    result["warnings"].append(
                        f"{cn} {lver or ''} 没装上，这个整合包现在启动不了"
                        f"（硬绑到原版 Minecraft {mc} 的话，包里的模组一个都不会加载）。"
                        f"已经按版本号自动补装过一次，请到「设置 → 启动项」"
                        f"重新安装对应版本的 {cn} 后点「修复整合包档案」重试"
                        .replace("  ", " "))
                else:
                    # 连加载器都认不出来：以前一声不吭把档案绑到纯原版。
                    # 有些包本来就只是原版 + 数据包，所以只提示、不硬拦。
                    result["loader_unknown"] = True
                    result["warnings"].append(
                        f"没能从整合包里认出模组加载器（Minecraft {mc}）。"
                        "如果进游戏后模组一个都没加载，多半是这份包缺加载器清单。")
            mark(row_runtime, "done" if vid else "fail",
                 "" if vid else (notes[-1] if notes else "")[:60])
        else:
            loader_hint = "无法识别整合包需要的游戏版本，请先在官方启动器里安装对应版本"
            mark(row_runtime, "fail", "无法识别游戏版本")
        if kind and vid:
            cn = runtime.LOADER_CN.get(kind, kind)
            ver = lver or ""
            if not lver:
                info = runtime.describe_version(
                    os.path.join(game_dir, "versions", vid))
                ver = (info or {}).get("loader_ver", "")
            if ver:
                loader_hint = f"已绑定 {cn} {ver}（Minecraft {mc}），可直接启动"
        base_vid = vid
        if vid:
            # 让版本列表里出现「以整合包命名」的那一项（实例目录就是它自己）。
            # 名字必须取**实际目录名**：撞名时 isolated_dir 会退到「包名-2」，
            # 用 pack_name 会把 json 写到另一个目录里去。
            pack_vid, vnote = ensure_pack_version(
                game_dir, vid,
                os.path.basename(instance_dir.rstrip("\\/")) or pack_name)
            if vnote:
                # 只是说明，不是问题：别混进 warnings（那栏是给玩家看故障的）
                result["version_note"] = vnote
            if pack_vid:
                vid = pack_vid
            # 实例目录补一个 `<包名>.jar`（硬链接到基底 jar，同卷 0 成本）：
            # 官启按 profile 的 lastVersionId 去 versions/<包名>/ 找客户端 jar，
            # 只有 json 没有 jar 时它会认定「客户端 jar 缺失」直接不启动 ——
            # 这就是玩家说的「整合包文件夹里少了个 jar」。
            if vid:
                result["client_jar"] = os.path.basename(
                    mirror_base_jar(game_dir, vid, base_vid) or "")
        result["loader_hint"] = loader_hint
        result["version_id"] = vid

        # 4) 写入 launcher_profiles.json（lastVersionId 必填）
        # 内存：优先用整合包自己声明的分配值；没声明就先按设置里的全局值写，
        # 还没设置的话由界面弹窗让玩家手动分配（apply_memory_to_profile 补写）。
        result["icon_saved"] = _save_pack_icon(instance_dir, icon_url, cancel_flag)
        mem_mb, mem_src = pack_declared_memory(instance_dir)
        result["memory_mb"] = mem_mb
        result["memory_src"] = mem_src
        cb(96, 100, "注册到官方启动器…")
        ok, info_msg = _register_profile(game_dir, pack_name, instance_dir, vid,
                                         memory_mb=mem_mb,
                                         need_loader=kind, need_lver=lver)
        result["profile_ok"] = ok
        result["profile_id"] = info_msg if ok else ""
        if not ok:
            result["warnings"].append(
                f"档案暂时没写进官方启动器：{info_msg}")
        # 实例标记与档案成没写成无关：官启把档案吃掉、或者这次被占用没写成，
        # 「游戏管理」都还得能认出这台整合包。
        try:
            with open(os.path.join(instance_dir, ".mcassist.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"pack": pack_name, "mc": mc, "loader": kind,
                           "loader_ver": lver, "version_id": vid,
                           # 第一百零八轮：把「当时认到的是哪种清单的哪个加载器」
                           # 记下来，下次核对就能看出是包没写还是我们没读到。
                           "loader_spec": result.get("loader_spec") or {},
                           "base_version": base_vid,
                           "profile_id": result["profile_id"],
                           "memory_mb": mem_mb,
                           # 第 104 轮：外来整合包的识别/选择结果一律记账，
                           # 以后核对能看出「当时跳过了什么、按哪个目录铺的」
                           "optional_client": str((result.get("optional_files") or {})
                                                  .get("client", "download")),
                           "overrides": list(override_names),
                           "minecraft_root": root_prefix,
                           "mmc_patches": result.get("mmc_patches") or {},
                           "zip_names_fixed": int(result.get("zip_names_fixed") or 0)},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # 4.5) 第九十六轮：装完就把它设成**独立启动目标**。
        # 以前到这一步就结束了 —— 只留一份官启档案，玩家得去官启的档案列表里找它。
        # 可这软件本来就能独立启动，玩家的疑问很直接：「都是独立启动了，怎么运行个
        # 导入的整合包还得走官方启动器」。目标记「实例名 + 实例目录」，官启档案只是
        # 顺带配的一份，不再是能不能玩的前提。
        if vid:
            try:
                from . import gamelaunch as _gl
                _gl.set_launch_target("instance", vid, instance_dir, pack_name,
                                      mem_mb, game_dir=game_dir)
                result["launch_target"] = vid
                if _gl.standalone_enabled():
                    _gl.set_launch_mode("standalone")
                    result["launch_mode"] = "standalone"
            except Exception as _e:                       # noqa: BLE001
                result["warnings"].append(f"没能设为独立启动目标：{str(_e)[:50]}")

        # 5) 清单落地 + 自动补全（第五十四轮）
        #    把 zip 里那份 manifest.json / modrinth.index.json 留在实例目录
        #    （内容原样，另加 installed 段：这次真下到了哪些文件），然后照清单
        #    核一遍，缺的补回来 —— CF 公开端点限流 / CDN 502 掉的模组就是在这里
        #    被捡回来的。开关：设置 → 常规 → 自动补全（默认开）。
        try:
            from . import packfill as _pf
            _raw, _fname = None, ""
            for _cand in (_pf.INDEX, _pf.MANIFEST):
                _hit = _meta_raw(_cand)
                if _hit:
                    _raw, _fname = zf.read(_hit), _cand
                    break
            if _raw is not None:
                _pf.write_index(instance_dir, _raw, _fname, records=records)
            if _pf.enabled():
                cb(96, 100, "核对整合包清单…")

                def _fill_cb(d, t, text=""):
                    cb(96 + int(max(0, min(94, int(d))) * 3 / 94), 100,
                       text or "核对整合包清单…")

                _r = _pf.fill(instance_dir, cancel_flag=cancel_flag,
                              progress_cb=_fill_cb)
                if _r.get("ok"):
                    result["auto_fill"] = {"fmt": _r.get("fmt", ""),
                                           "checked": int(_r.get("checked") or 0),
                                           "filled": int(_r.get("filled") or 0),
                                           "failed": list(_r.get("failed") or [])}
                if _r.get("filled"):
                    result["warnings"].append(
                        f"自动补全补回 {_r['filled']} 个清单里缺的文件")
                # 第九十四轮：装完还要核一遍「整合包里解压出来的那些文件」在不在。
                # 清单里没有它们（config / 资源包 / 默认按键…），所以以前丢了也查不出。
                _zm = list(_r.get("zip_missing") or [])
                if _zm:
                    result["zip_missing"] = _zm[:60]
                    result["warnings"].append(
                        f"整合包里有 {len(_zm)} 个文件没解压出来"
                        f"（{'、'.join(_zm[:3])}…），建议重新导入一次")
                # 第七十五轮：装完必须报「清单要 N 个、目录里只有 M 个」。
                # 以前补全悄悄跳过换不到地址的文件，进度条照样 100% 完成 ——
                # 玩家进服被「服务器发送含有未知键的注册表」踢出来才知道少了模组。
                _left = list(_r.get("still_missing") or [])
                if _left:
                    result["short"] = _left
                    _head = "、".join(_left[:3])
                    _more = f" 等 {len(_left)} 个" if len(_left) > 3 else ""
                    result["warnings"].append(
                        f"整合包还缺 {len(_left)} 个文件（{_head}{_more}），"
                        f"清单见 缺失文件.txt —— 缺模组会让进服被服务端踢出")
                    failed.extend(_left)
        except Exception as _e:                      # noqa: BLE001
            result["warnings"].append(f"自动补全跳过：{str(_e)[:60]}")

        # 被路径校验拒掉的条目也算「清单要了但没装上」，不能让进度条 100% 骗人
        failed.extend(rejected)
        if failed:
            miss = os.path.join(instance_dir, "缺失文件.txt")
            with open(miss, "w", encoding="utf-8") as f:
                f.write("\n".join(failed))
            result["warnings"].append(f"{len(failed)} 个文件未能自动下载，清单见 缺失文件.txt")

    cb(100, 100, "完成")
    return result


def repair_runtime(instance_dir: str, game_dir: str | None = None) -> tuple[bool, str]:
    """给一台实例补齐「能启动」所需的一切：加载器版本 → 版本壳 → 客户端 jar
    → 官启档案（配不上不算失败）→ 独立启动目标。

    第九十六轮：这是**不依赖官启档案**的修复入口。整合包导入时加载器没装上
    （第 96 轮查明是「镜像补丁包验签失败」），会留下一台这样的实例：文件齐全、
    游戏管理里看得见、就是起不来，而「修复整合包档案」只能修**已经写进官启**的
    档案 —— 档案本身没写成时它无事可做，玩家就卡死在「得去官方启动器」这一步。

    返回 (成功与否, 给玩家看的一句话)。
    """
    d = str(instance_dir or "")
    if not d or not os.path.isdir(d):
        return False, "实例目录不见了"
    gd = game_dir or detect_game_dir()
    name_dir = os.path.basename(d.rstrip("\\/"))
    meta = {}
    mp = os.path.join(d, ".mcassist.json")
    if os.path.isfile(mp):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    mc = str(meta.get("mc") or "")
    kind = str(meta.get("loader") or "")
    lver = str(meta.get("loader_ver") or "")
    if not mc or not kind:
        imc, ikind = runtime.infer_instance_env(d)
        mc = mc or imc
        kind = kind or ikind
    if not mc:
        return False, "认不出这台实例的 MC 版本"
    name = str(meta.get("pack") or name_dir)

    # 第一百零九轮：补全/修复必须**真装全**才复用。以前这里默认
    # verify_loader=False，本机留下半个版本（json 在、jar 不全）就「沿用已有版本」
    # 直接返回，之后每次补全都显示成功却什么都没修。
    vid, notes = runtime.ensure_runtime(gd, mc, kind, lver,
                                        verify_loader=bool(kind in ("forge", "neoforge")))
    if not vid:
        cn = runtime.LOADER_CN.get(kind, kind) if kind else "运行版本"
        return False, (notes[-1] if notes else f"{cn} {lver} 装不上".strip())
    base_vid = vid
    pack_vid, _vnote = ensure_pack_version(gd, vid, name_dir or name)
    if pack_vid:
        vid = pack_vid
    try:
        mirror_base_jar(gd, vid, base_vid)
    except Exception:                                     # noqa: BLE001
        pass
    # 采纳 PCL-CE MergeJson：把加载器档案合并进实例、去掉 inheritsFrom。
    # 以后玩家把 versions/<加载器> 那个「多出来的目录」删掉，实例照样能启动 ——
    # 这也正是这次崩溃的现场：删了目录 → 补全重建出一个瘦壳 → 开局缺 MC 本体类。
    merged, mnote = False, ""
    try:
        merged, mnote = merge_pack_runtime(gd, d, base_vid)
    except Exception as _e:                               # noqa: BLE001
        merged, mnote = False, f"合并运行档案失败：{str(_e)[:60]}"

    mem_mb = int(meta.get("memory_mb") or 0)
    ok_prof, pid = _register_profile(gd, name, d, vid, memory_mb=mem_mb,
                                     need_loader=kind, need_lver=lver)
    # 标记与独立启动目标：档案写不写成都要落，独立启动根本不需要它
    try:
        meta.update({"pack": name, "mc": mc, "loader": kind, "loader_ver": lver,
                     "version_id": vid,
                     # 合并成功后实例自包含，base_version 指向自己 —— 不能再指向
                     # 那个随时会被玩家删掉的 `versions/<加载器>` 目录
                     "base_version": (name_dir or vid) if merged else base_vid,
                     "profile_id": pid if ok_prof else "",
                     "memory_mb": mem_mb})
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from . import gamelaunch as _gl
        _gl.set_launch_target("instance", vid, d, name, mem_mb, game_dir=gd)
    except Exception:                                     # noqa: BLE001
        pass
    cn = runtime.LOADER_CN.get(kind, kind) if kind else ""
    tail = f"{cn} {lver or ''}".strip()
    if kind in ("forge", "neoforge") and not merged:
        # 合并没成功 = 实例仍然只能靠 versions/<加载器> 那一份启动。
        # 那就必须确认它真的装全了，否则就是「补全说成功、开局 10 秒崩」。
        try:
            _ok_l, _why_l = runtime.loader_ready(gd, kind, lver, mc)
        except Exception:                                 # noqa: BLE001
            _ok_l, _why_l = True, ""
        if not _ok_l:
            return False, (f"{tail} 没装全（{_why_l or '运行版本不完整'}），"
                           f"实例也没法自包含：{mnote or '合并运行档案失败'}")
    tail_note = ("（已合并进实例，不再依赖 versions/ 里的加载器目录）"
                 if merged else "")
    if ok_prof:
        return True, f"已绑定运行版本 {vid}{tail_note}" + (f"（{tail}）" if tail else "")
    return True, (f"运行版本已经是 {vid}{tail_note}，但启动项没写进官方启动器 —— "
                  f"不影响独立启动")


def _instance_has_content(p: str) -> bool:
    if not os.path.isdir(p):
        return False
    for sub in ("mods", "config", "saves", "options.txt", "kubejs", "resourcepacks"):
        if os.path.exists(os.path.join(p, sub)):
            return True
    return False


def repair_installed_profiles(game_dir: str | None = None) -> list[str]:
    """修复本软件装过的整合包档案（阻塞，放工作线程）。

    修三类历史问题：
    1. 档案 `lastVersionId` 为空/失效 —— 官启点开始游戏直接报
       REQUEST_FAILED / Unable to prepare assets for download；
    2. 实例内容在 `versions/<包名>`（旧版误放），档案 gameDir 却指向空目录
       —— 官启创建空目录后找不到 mods，且版本列表里多出一个无 json 的假版本；
    3. 游戏目录里没有可启动的运行版本 —— 复用本机其它启动器的版本补齐。
    返回人类可读的修复说明列表。
    """
    gdir = game_dir or detect_game_dir()
    lp = os.path.join(gdir, "launcher_profiles.json")
    if not os.path.isfile(lp):
        return []
    try:
        with open(lp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        return []
    known = {str(v.get("name") or "") for v in (CONFIG.get("installed") or {}).values()}
    fixed, changed = [], False
    # 用本模块的检测（relink 那份是给「启动」流程用的另一套），
    # 这样运行中判定和写档案用同一个来源，不会两处结论打架。
    if launcher_running():
        # 官启退出时会把内存里的 profiles 写回磁盘，覆盖我们的修复；
        # 运行中搬 versions 下的实例也有锁文件风险，一律等它退出。
        return ["官方启动器正在运行，请先退出它再修复（否则档案会被启动器覆盖）"]

    # --- ①) 先把实例档案整理干净（第四十八轮）：胖壳 → 薄壳，并补上 `<包名>.jar`
    # 胖壳会让启动器把父版本参数拼两遍（两个 --gameDir）；缺 jar 则官启直接不启动。
    try:
        _slimmed, _jarred = slim_all_pack_jsons(gdir)
        if _slimmed:
            fixed.append(f"整理 {_slimmed} 个整合包的启动档案（去掉了重复的启动参数）")
        if _jarred:
            fixed.append(f"补上 {_jarred} 个整合包的客户端 jar")
    except Exception:
        pass

    # --- 0) 补回被官启吃掉的档案
    # 官启有已知回归：退出时用内存态覆盖整个 launcher_profiles.json，自定义
    # 档案连带消失（.bak 里还在、正式文件已经空了）。我们自己写过的档案都记在
    # profiles_wanted 里，这里按表补回；同一条只补一次，再没了就当作玩家自己删了。
    wanted = {k: dict(v) for k, v in (CONFIG.get("profiles_wanted") or {}).items()
              if isinstance(v, dict)}
    wanted_before = json.dumps(wanted, sort_keys=True, ensure_ascii=False)
    for pid, rec in list(wanted.items()):
        if pid in profiles:
            continue
        rec_root = str(rec.get("root") or "")
        if rec_root and os.path.normcase(os.path.realpath(rec_root)) != \
                os.path.normcase(os.path.realpath(gdir)):
            continue                                  # 别的游戏目录的档案，别串过来
        gdir_rec = str(rec.get("game_dir") or "")
        if gdir_rec and not os.path.isdir(gdir_rec):
            continue                                  # 实例都被删了，别造幽灵档案
        if rec.get("healed"):
            wanted.pop(pid, None)
            continue
        now_s = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        prof = {
            "name": str(rec.get("name") or pid),
            "type": "custom",
            "created": now_s,
            "lastUsed": now_s,
            "gameDir": gdir_rec,
            "icon": str(rec.get("icon") or "Grass_Block"),
            "lastVersionId": str(rec.get("version_id") or ""),
        }
        if rec.get("java_args"):
            prof["javaArgs"] = rec["java_args"]
        profiles[pid] = prof
        rec["healed"] = True
        changed = True
        fixed.append(f"已补回被官方启动器覆盖的档案「{prof['name']}」")
    if json.dumps(wanted, sort_keys=True, ensure_ascii=False) != wanted_before:
        CONFIG.set("profiles_wanted", wanted)

    for pid, prof in list(profiles.items()):
        if not isinstance(prof, dict):
            continue
        name = str(prof.get("name") or "")
        if not name or (known and name not in known and prof.get("type") != "custom"):
            continue
        gd = str(prof.get("gameDir") or "")
        # 不是本软件装的档案：只在 gameDir 落在本游戏目录内时才动，避免误改用户自己的配置
        if known and name not in known and gd:
            try:
                base = os.path.normcase(os.path.abspath(gdir)) + os.sep
                if not os.path.normcase(os.path.abspath(gd)).startswith(base):
                    continue
            except Exception:
                continue
        # --- 定位实例内容实际所在
        candidates = []
        if gd:
            candidates.append(gd)
        candidates.append(isolated_dir(gdir, name))      # PCL2 规范位置：versions 里
        candidates.append(os.path.join(gdir, name))      # 旧版位置：游戏根目录
        content_dir = ""
        for c in candidates:
            if _instance_has_content(c):
                content_dir = c
                break
        if not content_dir:
            continue                      # 空实例，交给运行时补，不在这一步搬

        # --- 实例目录归位：PCL2 的语义是「实例本身就在 versions/<名字> 里」
        # 旧版本把实例建在游戏根目录（versions 外面），官启既不当它隔离，
        # 玩家也分不清哪个目录是谁。同盘改名是瞬时操作，搬不动就原地不动。
        target = isolated_dir(gdir, name)
        legacy_top = os.path.join(gdir, name)
        cur_real = os.path.normcase(os.path.realpath(content_dir))
        tgt_real = os.path.normcase(os.path.realpath(target))
        top_real = os.path.normcase(os.path.realpath(legacy_top))
        if cur_real != tgt_real and cur_real == top_real and not os.path.exists(target):
            try:
                _ensure_dir(os.path.dirname(target))
                os.replace(legacy_top, target)
                content_dir = target
                changed = True
                fixed.append(f"「{name}」实例已归位到 versions 里：{target}")
            except Exception:
                content_dir = legacy_top       # 搬不动就先指向原地，别丢数据
        elif (not _instance_has_content(content_dir)
              and _instance_has_content(target)):
            content_dir = target

        # --- 运行版本
        env = {}
        mk = os.path.join(content_dir, ".mcassist.json")
        if os.path.isfile(mk):
            try:
                env = json.load(open(mk, encoding="utf-8"))
            except Exception:
                env = {}
        mc = str(env.get("mc") or "")
        kind = str(env.get("loader") or "")
        lver = str(env.get("loader_ver") or "")
        if not mc or not kind:
            imc, ikind = runtime.infer_instance_env(content_dir)
            mc = mc or imc
            kind = kind or ikind

        vid = str(prof.get("lastVersionId") or "")
        ok_version = bool(vid) and os.path.isfile(
            os.path.join(gdir, "versions", vid, vid + ".json"))
        # 第五十三轮：两种「看着有版本、其实启动不了」的情况都当没装，交给 ensure_runtime：
        #   ① lastVersionId 直接就是裸原版号，但整合包明明要加载器；
        #   ② 档案自己不写启动参数（我们的薄壳本该靠继承链拿），继承的却是**裸原版** ——
        #      说明加载器那一层（1.20.1-forge-47.4.10）根本没进来，`-p` 之类的
        #      ModLauncher 参数全丢了，启动就是「窗口一闪都不出」。
        if ok_version and kind and mc:
            cur = str(vid)
            d = runtime._read_json(os.path.join(gdir, "versions", cur, cur + ".json"))
            parent = str(d.get("inheritsFrom") or "")
            thin = not d.get("arguments") and not d.get("minecraftArguments")
            if os.path.normcase(cur) == os.path.normcase(mc) or (
                    thin and parent and os.path.normcase(parent) == os.path.normcase(mc)):
                ok_version = False
                fixed.append(f"「{name}」的运行版本链断在加载器那层（{cur} → {parent or mc}），"
                             f"重新安装加载器并重绑")
        note = []
        if not ok_version:
            nv, notes = runtime.ensure_runtime(gdir, mc, kind, lver)
            note.extend(notes)
            if nv:
                prof["lastVersionId"] = nv
                vid = nv
                changed = True
                fixed.append(f"「{name}」已绑定运行版本 {nv}")
            else:
                fixed.append(f"「{name}」仍缺运行版本：{notes[-1] if notes else '未知'}")
        # --- gameDir 指向真实内容目录
        if os.path.normcase(os.path.realpath(gd or "")) != \
                os.path.normcase(os.path.realpath(content_dir)):
            prof["gameDir"] = content_dir
            changed = True
        # --- 实例在 versions 里 → 版本列表里必须有它自己的 json（官启只认这个）
        if vid and os.path.normcase(os.path.realpath(content_dir)).startswith(
                os.path.normcase(os.path.realpath(os.path.join(gdir, "versions")))):
            pack_vid, vnote = ensure_pack_version(gdir, vid, name)
            if pack_vid and pack_vid != vid:
                vid = pack_vid
                if vnote:
                    fixed.append(f"「{name}」{vnote}")
        if prof.get("lastVersionId") != vid:
            prof["lastVersionId"] = vid
            changed = True

    if changed:
        ok, msg = _write_profiles(data, gdir)
        if not ok:
            fixed.append(f"档案写入失败：{msg}")
        # 修完之后把登记表同步成最新状态：目录/版本都可能刚改过，
        # 不同步的话下次补回来的还是旧路径（旧路径已经不存在，补也补不上）。
        known_pids = set((CONFIG.get("profiles_wanted") or {}).keys())
        for pid, prof in profiles.items():
            if pid in known_pids and isinstance(prof, dict):
                _remember_profile(pid, prof, gdir)
    return fixed


def downloads_dir() -> str:
    d = os.path.join(app_data_dir(), "downloads")
    _ensure_dir(d)
    return d


# ================================================================ 启动项（官启档案）
def _profiles_path(game_dir: str | None = None) -> str:
    return os.path.join(game_dir or detect_game_dir(), "launcher_profiles.json")


def read_profiles(game_dir: str | None = None) -> dict:
    """读 launcher_profiles.json（不存在返回空骨架）"""
    try:
        with open(_profiles_path(game_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("bad json")
        data.setdefault("profiles", {})
        data.setdefault("settings", {})
        return data
    except Exception:
        return {"profiles": {}, "settings": {}}


def _write_profiles(data: dict, game_dir: str | None = None) -> tuple[bool, str]:
    """写 launcher_profiles.json（原子写 + 回读校验）。

    官启有「退出时用内存态覆盖整个档案表」的行为，所以：
      · 写之前查一次进程（运行中就拒绝，不要写了个寂寞）；
      · 先写临时文件再 os.replace，避免写到一半被打断留下坏 json
        （坏 json 会让官启把所有安装项重置成「最新版本/最新快照」）；
      · 写完回读一遍，确认该在的档案真的在文件里。
    """
    if launcher_running():
        return False, "官方启动器正在运行，请先退出它再修改启动项（否则会被启动器覆盖）"
    lp = _profiles_path(game_dir)
    tmp = lp + ".tmp"
    try:
        if os.path.exists(lp):
            shutil.copy2(lp, lp + ".bak")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, lp)
    except Exception as e:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, f"写入启动项失败：{e}"
    # 回读校验：文件里少了任何一条我们刚写的档案，就当没写成
    want = set((data.get("profiles") or {}).keys())
    got = set((read_profiles(game_dir).get("profiles") or {}).keys())
    lost = want - got
    if lost:
        return False, "写进去了但回读不到（官方启动器可能正在改写这个文件），请退出官启后重试"
    if launcher_running():
        return False, "写入期间官方启动器被打开了，先退出它再点一次（否则这次修改会被覆盖）"
    return True, "已更新启动项"


TYPE_CN = {"latest-release": "最新正式版", "latest-snapshot": "最新快照",
           "custom": "自定义", "latest_release": "最新正式版",
           "latest_snapshot": "最新快照"}

_HEX32 = re.compile(r"^[0-9a-f]{32}$", re.I)


def _pretty_profile_name(name: str, ptype: str) -> str:
    """官启自动创建的档案名是一串 32 位 hex，直接显示没法看"""
    if _HEX32.match(name or ""):
        return f"{TYPE_CN.get(ptype, '启动项')}（官启自动创建）"
    return name


def list_profiles(game_dir: str | None = None) -> list[dict]:
    """官启里所有启动项（档案），附带隔离状态与类型说明

    返回 [{id, name, type, type_cn, version_id, game_dir, java_args,
           isolated, instance(是否独立实例目录), exists(运行版本是否存在)}]
    """
    gd = game_dir or detect_game_dir()
    data = read_profiles(gd)
    default_name = str((data.get("settings") or {}).get("profileName") or "")
    out = []
    for pid, p in (data.get("profiles") or {}).items():
        if not isinstance(p, dict):
            continue
        gdir = str(p.get("gameDir") or "")
        vid = str(p.get("lastVersionId") or "")
        ver_dir = os.path.join(gd, "versions", vid) if vid else ""
        isolated = bool(gdir) and os.path.normcase(os.path.realpath(gdir)).startswith(
            os.path.normcase(os.path.realpath(os.path.join(gd, "versions"))))
        is_instance = bool(gdir) and not isolated and \
            os.path.normcase(os.path.realpath(gdir)) != os.path.normcase(os.path.realpath(gd))
        out.append({
            "id": pid,
            "name": str(p.get("name") or pid),
            "label": _pretty_profile_name(str(p.get("name") or pid),
                                          str(p.get("type") or "custom")),
            "type": str(p.get("type") or "custom"),
            "type_cn": TYPE_CN.get(str(p.get("type") or ""), "自定义"),
            "version_id": vid,
            "game_dir": gdir,
            "java_args": str(p.get("javaArgs") or ""),
            "isolated": isolated,
            "instance": is_instance,
            "exists": bool(ver_dir) and os.path.isfile(os.path.join(ver_dir, vid + ".json")),
            "default": str(p.get("name") or "") == default_name,
        })
    out.sort(key=lambda x: (not x["default"], x["name"].lower()))
    return out


def set_default_profile(profile_id: str, game_dir: str | None = None) -> tuple[bool, str]:
    """把某个启动项设为官启默认选中项（下次打开官启直接用它）"""
    gd = game_dir or detect_game_dir()
    data = read_profiles(gd)
    prof = (data.get("profiles") or {}).get(profile_id)
    if not isinstance(prof, dict):
        return False, "启动项不存在，刷新后重试"
    data.setdefault("settings", {})["profileName"] = str(prof.get("name") or "")
    ok, msg = _write_profiles(data, gd)
    return (True, f"已设为默认启动项：{prof.get('name')}") if ok else (False, msg)


def set_profile_isolation(profile_id: str, isolated: bool,
                          game_dir: str | None = None) -> tuple[bool, str]:
    """版本隔离：把该启动项的游戏目录切到 versions/<运行版本>，存档/模组互不干扰"""
    gd = game_dir or detect_game_dir()
    data = read_profiles(gd)
    prof = (data.get("profiles") or {}).get(profile_id)
    if not isinstance(prof, dict):
        return False, "启动项不存在，刷新后重试"
    vid = str(prof.get("lastVersionId") or "")
    if not vid:
        return False, "该启动项还没绑定运行版本，先在官启里启动一次"
    if not os.path.isfile(os.path.join(gd, "versions", vid, vid + ".json")):
        return False, f"运行版本 {vid} 目录不完整，先修复整合包档案"
    cur = str(prof.get("gameDir") or "")
    root = os.path.normcase(os.path.realpath(gd))
    vroot = os.path.normcase(os.path.realpath(os.path.join(gd, "versions")))
    if isolated:
        real_cur = os.path.normcase(os.path.realpath(cur)) if cur else ""
        if real_cur and real_cur != root and not real_cur.startswith(vroot):
            # 第三方启动器的实例（Prism/HMCL…）：目录就是它的身份，不能挪
            return False, "该启动项已有独立实例目录，无需再隔离"
        target = os.path.join(gd, "versions", vid)
        if real_cur.startswith(vroot) and os.path.normcase(os.path.realpath(target)) != real_cur:
            # 已经隔离过了（只是目录名和版本 id 不同，例如整合包命名的实例）
            return True, f"已是隔离目录：{cur}"
        _ensure_dir(target)
        prof["gameDir"] = target
    else:
        left = cur
        prof["gameDir"] = ""
    ok, msg = _write_profiles(data, gd)
    if ok:
        prof.setdefault("type", "custom")
        _remember_profile(profile_id, prof, gd)
        if isolated:
            return True, "已开启版本隔离（存档与模组独立，目录在 versions 里）"
        if left and os.path.normcase(os.path.realpath(left)).startswith(vroot):
            return True, f"已关闭版本隔离（改回游戏根目录）；原来的文件仍在 {left}"
        return True, "已关闭版本隔离（使用游戏根目录）"
    return False, msg


# ================================================================ 游戏清单 / 删除
_SKIP_DIRS = {"versions", "libraries", "assets", "runtime", "logs", "crash-reports",
              "saves", "mods", "shaderpacks", "resourcepacks", "config",
              "defaultconfigs", "screenshots", "backups", "downloads", "cache",
              "server-resource-packs", "bluemap", "kubejs", "journeymap"}


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_reparse(path: str) -> bool:
    """目录联接 / 符号链接（junction / symlink）。

    `os.path.islink()` 对 Windows 的 junction 返回 **False**，所以
    `os.walk(followlinks=False)` 也拦不住它 —— 必须自己看
    FILE_ATTRIBUTE_REPARSE_POINT 这一位。
    """
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs is None or attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


_SIZE_MAX_FILES = 15000
_SIZE_SECONDS = 2.0


def _dir_size(path: str, max_files: int = _SIZE_MAX_FILES,
              seconds: float = _SIZE_SECONDS) -> tuple[int, bool]:
    """目录体积，返回 (字节数, 是否被截断)。

    这是「统计体积时闪退」的修复重点：统计体积只是显示用，**绝不能**因为一个
    畸形目录把整个软件带走。三条硬约束，缺一条都可能出事：

      · **不跨目录联接**（junction / symlink）。玩家的 `.minecraft` 常常整目录
        链到别的盘，里面还可能有指回上级的链接；`os.walk(followlinks=False)`
        在 Windows 上拦不住 junction（`islink()` 对它是 False）→ 会一路走进去，
        既慢又可能绕成死循环，工作线程出不来，界面上永远停在「正在统计体积…」；
      · **文件数上限**，默认 1.5 万；
      · **墙上时钟上限**，默认 2 秒。单层几十万文件的巨目录靠文件数上限兜不住
        时间，超时就提前收工（多出来的体积标 `+` 号，玩家看得出来是「至少这么大」）。

    任何异常一律吞掉并当成「扫到多少算多少」——统计本身失败不该影响软件。
    """
    total = 0
    n = 0
    if not path:
        return 0, False
    try:
        if not os.path.isdir(path):
            return 0, False
        deadline = time.monotonic() + max(0.05, float(seconds))
        for root, dirs, files in os.walk(path):
            # 就地改 dirs 才能剪枝：把联接目录整枝剪掉
            if dirs:
                dirs[:] = [d for d in dirs
                           if not is_reparse(os.path.join(root, d))]
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                except Exception:
                    pass
                n += 1
                if n >= max_files:
                    return total, True
            if time.monotonic() > deadline:
                return total, True
    except Exception:
        return total, True
    return total, False


def fmt_size(n: int) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def list_game_versions(game_dir: str | None = None) -> list[dict]:
    """<游戏目录>/versions 下所有可识别版本（含大小）"""
    gd = game_dir or detect_game_dir()
    vdir = os.path.join(gd, "versions")
    out = []
    try:
        names = os.listdir(vdir)
    except Exception:
        return out
    for n in names:
        path = os.path.join(vdir, n)
        # 整合包实例也是 versions 里的一个目录，但它是「整合包」，归实例列表显示，
        # 两边都列会让玩家看到两条一模一样的记录，删哪条都不知道。
        if is_pack_dir(path):
            continue
        # 单个版本目录出问题（json 坏掉、权限、盘符掉线）不许影响整张列表
        try:
            info = runtime.describe_version(path)
        except Exception:
            info = None
        if not info:
            continue
        # 加载器版本（neoforge-xxx / forge-xxx / fabric-xxx / quilt-xxx）不是「游戏」，
        # 它们被单独挪到全局 loaders/ 目录了，这里即使还在 .minecraft/versions 里也别列出来，
        # 否则玩家在「游戏管理 / 选择要启动的游戏」里会看到一堆不能单独玩的加载器条目。
        # 但第一百一十二轮：玩家从主页「下载游戏」主动装的加载器版本是**要玩的独立
        # 游戏**（目录带 standalone 标记），必须照常显示 —— 不止 NeoForge，
        # Forge / Fabric / Quilt 同理。
        _mk = _json_file(os.path.join(path, ".mcassist.json")) or {}
        if str(info.get("loader") or "") in ("forge", "neoforge", "fabric", "quilt") \
                and not info.get("pack") and not _mk.get("standalone_version"):
            continue
        size, capped = _dir_size(path)
        info.update(size=size, size_capped=capped, path=path)
        # 版本目录自带图标（HMCL 的实例版本会放 icon.png）就带上，没有留给徽章
        try:
            packicon.apply_to(info, path)
        except Exception:
            pass
        out.append(info)
    out.sort(key=lambda v: v["id"].lower())
    return out


def list_game_instances(game_dir: str | None = None) -> list[dict]:
    """整合包实例：.mcassist.json 标记 / 导入登记表 / 官启档案 gameDir 指向的目录"""
    gd = game_dir or detect_game_dir()
    root = os.path.normcase(os.path.realpath(gd))
    vroot = os.path.normcase(os.path.realpath(os.path.join(gd, "versions")))
    found: dict[str, dict] = {}

    def merge(path, name, meta=None, extra=None):
        real = os.path.normcase(os.path.realpath(path))
        item = found.get(real)
        if item is None:
            item = {
                "name": name, "path": path,
                "mc": "", "loader": "", "loader_ver": "",
                "version_id": "", "profile_id": "", "memory_mb": 0,
                "has_meta": False, "imported": False, "source": "",
            }
            found[real] = item
        if not item.get("name"):
            item["name"] = name
        for k, v in (meta or {}).items():
            if v and not item.get(k):
                item[k] = v
        item.update(extra or {})
        return item

    # 0) 导入登记表：官启把我们的档案吃掉时，「游戏管理」照样得能看到
    #    只认**落在这条目录里**的记录（官方 / 应用两个标签页各看各的）
    for rec in imported_records(gd):
        if str(rec.get("kind") or "") == "version":
            continue          # 纯版本在「游戏版本」那一栏，别在这边重复列一条
        path = str(rec.get("path") or "")
        merge(path, str(rec.get("name") or os.path.basename(path)), {
            "mc": rec.get("mc") or "", "loader": rec.get("loader") or "",
            "loader_ver": rec.get("loader_ver") or "",
            "version_id": rec.get("version_id") or "",
            "profile_id": rec.get("profile_id") or "",
        }, {"imported": True, "source": rec.get("source") or ""})

    # 1) .mcassist.json 标记（本软件安装 / 导入时的落地标记）
    #    实例目录现在在 versions/ 里（PCL2 语义），所以两处都要扫。
    for base in (gd, os.path.join(gd, "versions")):
        try:
            names = os.listdir(base)
        except Exception:
            continue
        for n in names:
            path = os.path.join(base, n)
            if not os.path.isdir(path) or n.lower() in _SKIP_DIRS:
                continue
            mk = os.path.join(path, ".mcassist.json")
            if not os.path.isfile(mk):
                continue
            meta = _json_file(mk)
            # 第一百一十二轮：主页下载的独立加载器版本（standalone）不是整合包
            # 实例 —— 它已经列在「游戏版本」里了，这边再列一条就重复
            if meta.get("standalone_version"):
                continue
            merge(path, str(meta.get("pack") or meta.get("name") or n), {
                "mc": meta.get("mc") or "", "loader": meta.get("loader") or "",
                "loader_ver": meta.get("loader_ver") or "",
                "version_id": meta.get("version_id") or "",
                "profile_id": meta.get("profile_id") or "",
                "memory_mb": _norm_mb(meta.get("memory_mb")),
            }, {"has_meta": True, "imported": bool(meta.get("imported")),
                "source": meta.get("source") or ""})

    # 2) 官启档案里 gameDir 指向的实例目录
    #    · 游戏根目录本身 → 不是实例；
    #    · **别把另一条目录里的东西列进来**（第九十九轮）：看「应用」时，官方那份
    #      `official_assets` 里的实例以前会被原样列进来，玩家看到的就是「官方的
    #      跑到应用里了」。但**只挡我们自己的两条目录** —— 玩家从别的启动器接管
    #      的、gameDir 指到外面的外部实例是正经功能，得照旧列出来（第 23 轮）。
    #    · versions 下面：只有带实例内容（mods/saves/config…）的才算，
    #      纯运行版本目录（只有 json+jar）不进这个列表。
    _sibs = []
    try:
        from . import paths as _p
        for _cand in (_p.app_game_dir(), _p.official_game_dir()):
            _k = os.path.normcase(os.path.realpath(_cand))
            if _k and _k != root and _k not in _sibs:
                _sibs.append(_k)
    except Exception:                                     # noqa: BLE001
        _sibs = []
    for p in list_profiles(gd):
        gdir = p.get("game_dir") or ""
        if not gdir or not os.path.isdir(gdir):
            continue
        real = os.path.normcase(os.path.realpath(gdir))
        if real == root or real == vroot:
            continue
        if any(real == s or real.startswith(s + os.sep) for s in _sibs):
            continue
        if real.startswith(vroot) and not _instance_has_content(gdir):
            continue
        merge(gdir, p["name"], {"version_id": p["version_id"]}, {
            "profile_id": p["id"], "default": p["default"],
        })

    # 3) 档案里没写 mc / 加载器（导入进来的第三方实例都是这样）→ 用 version_id 回查版本 json。
    #    不回查的话，界面上这些整合包只剩一个名字，徽章也只能显示「未知」。
    for item in found.values():
        vid = str(item.get("version_id") or "")
        if not vid or (item.get("loader") and item.get("mc")):
            continue
        try:
            info = runtime.describe_version(os.path.join(gd, "versions", vid))
        except Exception:
            info = None
        if not info:
            continue
        if not item.get("mc"):
            item["mc"] = info.get("mc") or ""
        if not item.get("loader"):
            item["loader"] = info.get("loader") or ""
            item["loader_ver"] = info.get("loader_ver") or ""

    # 4) 整合包自己的图标：实例目录里找（HMCL/Prism/Modrinth App/自带 overrides），
    #    再到官方档案里找自定义图标（data:image/…）。都没有就让界面回退到加载器徽章。
    raw_profiles = (read_profiles(gd).get("profiles") or {})
    for item in found.values():
        pid = str(item.get("profile_id") or "")
        p_icon = str((raw_profiles.get(pid) or {}).get("icon") or "")
        try:
            packicon.apply_to(item, item["path"], p_icon)
        except Exception:
            pass

    out = []
    for key, item in found.items():
        # 盘符掉线 / 目录被删 / 权限不足都不许打断整张列表
        try:
            if not os.path.isdir(item["path"]):
                continue
        except Exception:
            continue
        size, capped = _dir_size(item["path"])
        item.update(size=size, size_capped=capped)
        out.append(item)
    out.sort(key=lambda x: str(x["name"]).lower())
    return out


def move_to_recycle(path: str) -> tuple[bool, str]:
    """删除到回收站（可还原），不直接永久删除玩家数据"""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False, "路径不存在（可能已被删除）"
    try:
        import ctypes
        from ctypes import wintypes

        class _SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path + "\0"          # ctypes 会再补一个 \0 -> 双 null 结尾
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0:
            return False, f"移到回收站失败（错误码 {rc}），请先退出游戏与启动器"
        if op.fAnyOperationsAborted:
            return False, "操作已取消"
        if os.path.exists(path):
            return False, "文件仍被占用（请先退出游戏与官方启动器）"
        return True, "已移入回收站"
    except Exception as e:                                   # noqa: BLE001
        return False, f"删除失败：{e}"


def delete_path_permanent(path: str) -> tuple[bool, str]:
    """第 122 轮：彻底删除（不走回收站，不可恢复）。供「删除方式」里的彻底删除用。"""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False, "路径不存在（可能已被删除）"
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        if os.path.exists(path):
            return False, "文件仍被占用（请先退出游戏与官方启动器）"
        return True, "已彻底删除（不可恢复）"
    except Exception as e:                                   # noqa: BLE001
        return False, f"彻底删除失败：{e}"


def _remove_path(path: str, permanent: bool) -> tuple[bool, str]:
    """按删除方式分派：permanent 永久删，否则进回收站（默认，可还原）。"""
    return delete_path_permanent(path) if permanent else move_to_recycle(path)


def delete_game_version(version_id: str, game_dir: str | None = None,
                        permanent: bool = False) -> tuple[bool, str]:
    """删除一个游戏版本目录；默认移入回收站，permanent=True 则彻底删除。"""
    if launcher_running():
        return False, "官方启动器正在运行，请先退出再删除"
    gd = game_dir or detect_game_dir()
    path = os.path.join(gd, "versions", version_id)
    if not os.path.isdir(path):
        return False, "版本目录不存在（可能已被删除）"
    refs = [p["name"] for p in list_profiles(gd) if p["version_id"] == version_id]
    ok, msg = _remove_path(path, permanent)
    if not ok:
        return False, msg
    if refs:
        msg += f"；{len(refs)} 个启动项仍引用它（{refs[0]}…），请在启动项里重新绑定版本"
    return True, msg


def delete_game_instance(path: str, profile_id: str = "",
                         game_dir: str | None = None,
                         permanent: bool = False) -> tuple[bool, str]:
    """删除整合包实例目录并清理档案/安装记录；默认移入回收站，permanent=True 彻底删除。"""
    if launcher_running():
        return False, "官方启动器正在运行，请先退出再删除"
    gd = game_dir or detect_game_dir()
    target = os.path.normcase(os.path.realpath(path))
    ok, msg = _remove_path(path, permanent)
    if not ok:
        return False, msg

    # 档案：按 gameDir 匹配或指定 id
    data = read_profiles(gd)
    removed = []
    for pid, p in list((data.get("profiles") or {}).items()):
        if not isinstance(p, dict):
            continue
        gdir = str(p.get("gameDir") or "")
        same = bool(gdir) and os.path.normcase(os.path.realpath(gdir)) == target
        if same or (profile_id and pid == profile_id):
            data["profiles"].pop(pid, None)
            removed.append(str(p.get("name") or pid))
            _forget_profile(pid)
    if removed:
        _write_profiles(data, gd)
    # 导入登记表：实例都没了，别再留一条幽灵记录（否则自检会想给它补档案）
    forget_import(path)

    # 安装记录：路径在实例目录下的都作废
    for pid, rec in list((CONFIG.get("installed") or {}).items()):
        rp = str((rec or {}).get("path") or "")
        if rp and os.path.normcase(os.path.realpath(rp)).startswith(target):
            CONFIG.forget_installed(pid)

    if removed:
        msg += f"；已移除启动项「{removed[0]}」"
    return True, msg


def installed_project_for_path(path: str) -> str:
    """反查：磁盘路径对应的 project_id（删除后清理用）"""
    target = os.path.normcase(os.path.realpath(path))
    for pid, rec in (CONFIG.get("installed") or {}).items():
        rp = str((rec or {}).get("path") or "")
        if rp and os.path.normcase(os.path.realpath(rp)) == target:
            return pid
    return ""


def open_folder(path: str):
    os.startfile(path)  # noqa: S606
