# -*- coding: utf-8 -*-
"""创建 Windows 快捷方式（.lnk）：ctypes 直调 COM（IShellLinkW + IPersistFile）。

为什么不走 PowerShell / WScript：向导以管理员身份运行时，PowerShell 的引号与
中文路径转义容易失败，且策略受限环境下会被拦截；COM 调用无外部进程、无编码问题。
"""
import ctypes
import os
from ctypes import HRESULT, POINTER, byref, c_void_p, c_wchar_p, wintypes

_CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str):
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    return g


def _vcall(iface, index, restype, argtypes):
    """取接口第 index 个虚表函数"""
    vt = ctypes.cast(iface, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vt[index])


def create_shortcut(lnk_path: str, target: str, workdir: str | None = None,
                    icon: str | None = None, desc: str = "") -> bool:
    """创建 .lnk 快捷方式，返回是否成功（文件已落盘）"""
    if os.name != "nt":
        return False
    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitialize(None)
    except Exception:
        pass
    link = c_void_p()
    try:
        clsid = _guid(_CLSID_SHELLLINK)
        iid = _guid(_IID_ISHELLLINKW)
        # CLSCTX_INPROC_SERVER = 1
        ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid), byref(link))
        if not link:
            return False

        set_path = _vcall(link, 20, HRESULT, [c_wchar_p])
        set_workdir = _vcall(link, 9, HRESULT, [c_wchar_p])
        set_icon = _vcall(link, 17, HRESULT, [c_wchar_p, ctypes.c_int])
        set_desc = _vcall(link, 7, HRESULT, [c_wchar_p])

        set_path(link, str(target))
        wd = workdir or os.path.dirname(str(target))
        if wd:
            set_workdir(link, wd)
        if icon:
            set_icon(link, str(icon), 0)
        if desc:
            set_desc(link, desc)

        # QueryInterface -> IPersistFile
        iid_pf = _guid(_IID_IPERSISTFILE)
        pf = c_void_p()
        qi = _vcall(link, 0, HRESULT, [POINTER(GUID), POINTER(c_void_p)])
        qi(link, byref(iid_pf), byref(pf))
        if not pf:
            return False
        save = _vcall(pf, 6, HRESULT, [c_wchar_p, wintypes.BOOL])
        save(pf, str(lnk_path), True)
        # Release
        for p in (pf, link):
            rel = _vcall(p, 2, ctypes.c_ulong, [])
            try:
                rel(p)
            except Exception:
                pass
    except Exception:
        return False
    # 注意：不调用 CoUninitialize —— Qt 自身已初始化 COM（STA），此处卸载会破坏
    # Qt 的 COM 状态（进程退出时曾出现段错误）。COM 由进程生命周期自动回收。
    return os.path.isfile(lnk_path)


def create_shortcut_fallback(lnk_path: str, target: str) -> bool:
    """兜底：写 VBScript（UTF-16LE）用 WScript.Shell 创建，cscript 执行"""
    import subprocess
    import tempfile
    vbs = (
        'Set ws = CreateObject("WScript.Shell")\r\n'
        f'Set s = ws.CreateShortcut("{lnk_path}")\r\n'
        f's.TargetPath = "{target}"\r\n'
        f's.WorkingDirectory = "{os.path.dirname(target)}"\r\n'
        f's.IconLocation = "{target},0"\r\n'
        's.Save\r\n'
    )
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        os.close(fd)
        with open(tmp, "w", encoding="utf-16-le") as f:
            f.write(vbs)
        subprocess.run(["cscript", "//nologo", tmp], timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return os.path.isfile(lnk_path)


# ────────────────────────────── 真实「桌面 / 开始菜单」目录（第 50 轮）──────
# 以前向导直接拼 `%USERPROFILE%\Desktop`。玩家把桌面挪到别的盘（或 OneDrive
# 接管「已知文件夹」）时，那个目录只是个空壳 → 快捷方式建进去，桌面上什么都
# 看不到（本机实测：真实桌面在 E:/UserData/Desktop，而 %USERPROFILE%\Desktop
# 也存在，只是没人在看它）。开始菜单同理，`%APPDATA%` 在个别提权/精简环境
# 里甚至是空串，拼出来是相对路径。
#
# 正确来源是 Shell 的 Known Folder；取不到再退注册表 User Shell Folders，
# 最后才退回环境变量。三档都不要求管理员权限。
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"   # 当前用户开始菜单\程序


def _known_folder(fid: str) -> str:
    """SHGetKnownFolderPath：Shell 认的「真·已知文件夹」"""
    if os.name != "nt":
        return ""
    try:
        p = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            byref(_guid(fid)), 0, None, byref(p))
        if hr != 0 or not p.value:
            return ""
        val = p.value
        try:
            ctypes.windll.ole32.CoTaskMemFree(p)
        except Exception:
            pass
        return val
    except Exception:
        return ""


def _shell_folder(name: str) -> str:
    """注册表 `User Shell Folders\\<name>`（REG_EXPAND_SZ，含 %USERPROFILE% 之类）"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        ) as k:
            raw = winreg.QueryValueEx(k, name)[0]
        val = os.path.expandvars(str(raw))
        return val if os.path.isabs(val) else ""
    except Exception:
        return ""


def _pick_dir(cands) -> str:
    """候选里挑第一个**已存在**的；都不存在就返回第一个非空的
    （交给调用方 makedirs —— 真实桌面理论上一定存在，但不排除被手动删掉）。"""
    seen, uniq = set(), []
    for c in cands:
        if not c:
            continue
        c = os.path.normpath(c)
        k = os.path.normcase(c)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    for c in uniq:
        try:
            if os.path.isdir(c):
                return c
        except OSError:
            pass
    return uniq[0] if uniq else ""


def desktop_dir() -> str:
    """玩家**真正在看**的那个桌面（可能被挪到别的盘 / OneDrive）。"""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return _pick_dir([
        _known_folder(_FOLDERID_DESKTOP),
        _shell_folder("Desktop"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
    ])


def startmenu_dir() -> str:
    """当前用户开始菜单的「程序」目录（开始菜单里能搜到的那些）。"""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    return _pick_dir([
        _known_folder(_FOLDERID_PROGRAMS),
        _shell_folder("Programs"),
        os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"),
    ])
