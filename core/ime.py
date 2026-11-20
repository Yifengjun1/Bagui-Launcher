# -*- coding: utf-8 -*-
"""启动游戏前把输入法切成英文，退出游戏后自动切回中文（只做 Windows）。

三条路子（前两条是主力，第三条只当尽力而为）：
  1. `ActivateKeyboardLayout(hkl, KLF_SETFORPROCESS)` —— 只改本进程的线程，
     对 java / 官方启动器那些别的进程**完全无效**；
  2. `SystemParametersInfoW(SPI_SETDEFAULTINPUTLANG = 0x5A, ...)` —— 会话级默认，
     能真的写进去（`SPI_GETDEFAULTINPUTLANG = 0x59` 读得回新值）；
  3. 广播 `WM_INPUTLANGCHANGEREQUEST` 实测**无效**（别的进程不跟着换），已删掉 ——
     少一个会碰到别人窗口的动作。

不可行：新进程的键盘布局。实测父线程继承、注册表 `HKCU\\Keyboard Layout\\Preload`、
SPI 默认、广播四条全试过，子进程永远拿系统当前输入语言 —— Vista 之后这事归输入法
框架管，外部进程改不了。别拿「新进程拿到 en」当验收标准，那条永远红。

所以定位很明确：**我们切了什么，退出时就原样还原什么**。玩家报的「退出游戏回来还是
英文」，真相是我们开局前把**启动器自己这条线程**切成了英文，退出时没人还原 ——
`restore()` 就是治这个，另外把会话级默认也一并还原。

刻意不做的事：
  · 不带 `SPIF_UPDATEINIFILE`：不写注册表、不永久改玩家的默认语言，重启即恢复；
  · 不碰 `HKCU\\Keyboard Layout\\Preload`（本机实测写了也没用，还会把玩家的输入法顺序搞乱）；
  · 不往别人的窗口投递按键、不结束别的进程、不发广播。
"""
import ctypes
import os

ENABLED_KEY = "ime_english"          # CONFIG 里的键，默认开

KLF_ACTIVATE = 0x00000001
KLF_SETFORPROCESS = 0x00000100
SPI_GETDEFAULTINPUTLANG = 0x0059
SPI_SETDEFAULTINPUTLANG = 0x005A
# ★ 千万别再把 SPI_SETDEFAULTINPUTLANG 写成 0x0022：
#   0x0022 是 **SPI_SETICONTITLELOGFONT（设图标标题字体）**，pvParam 必须指向
#   92 字节的 LOGFONT。拿它当「设默认输入语言」用、塞进去一个 8 字节 HKL，
#   系统会把「HKL + 后面 84 字节栈垃圾」当字体写进
#   HKCU\Control Panel\Desktop\WindowMetrics\IconFont →
#   **桌面图标和资源管理器的文件名全部变成空白**（2026-10-25 真实事故）。
#   教训：SPI_* 系常量必须逐个核对官方编号，调用一律声明 argtypes。
SPIF_SENDCHANGE = 0x02
EN_US = "00000409"
ZH_CN = "00000804"                   # 中文（简体）- 美式键盘
ZH_TW = "00000411"                   # 中文（繁体）

# 官启/游戏退出后用来判断「游戏还在跑吗」：Minecraft 的进程就是 java/javaw
JAVA_PROC_NAMES = frozenset(("java.exe", "javaw.exe"))

_hkl = 0
_zh_hkl = 0
_prev_hkl = 0        # 切换前的默认输入语言；非 0 表示「我们还欠一次切回」
_gen = 0             # 每切一次英文 +1，让之前挂着的等待任务自己失效


def available():
    return os.name == "nt"


def _user32():
    u = ctypes.windll.user32
    # 不声明 restype，64 位下返回的 HKL 会被截成 32 位 —— 后面全错
    u.LoadKeyboardLayoutW.restype = ctypes.c_void_p
    u.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    u.ActivateKeyboardLayout.restype = ctypes.c_void_p
    u.ActivateKeyboardLayout.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    # 显式声明：不声明就会按 c_int 猜参数，传错也照样执行（能写坏注册表外观设置）
    u.SystemParametersInfoW.restype = ctypes.c_int
    u.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_uint]
    return u


def english_hkl():
    """en-US 键盘布局句柄（加载一次就缓存）"""
    global _hkl
    if _hkl:
        return _hkl
    if not available():
        return 0
    try:
        u = _user32()
        h = u.LoadKeyboardLayoutW(EN_US, KLF_ACTIVATE)
        if not h:
            h = u.LoadKeyboardLayoutW(EN_US, 0)
        _hkl = int(h or 0)
    except Exception:
        _hkl = 0
    return _hkl


def chinese_hkl():
    """中文键盘布局句柄（简体优先、繁体兜底）。拿不到返回 0"""
    global _zh_hkl
    if _zh_hkl:
        return _zh_hkl
    if not available():
        return 0
    try:
        u = _user32()
        for code in (ZH_CN, ZH_TW):
            h = u.LoadKeyboardLayoutW(code, KLF_ACTIVATE)
            if not h:
                h = u.LoadKeyboardLayoutW(code, 0)
            if h:
                _zh_hkl = int(h)
                break
    except Exception:
        _zh_hkl = 0
    return _zh_hkl


def current_default_hkl():
    """用户眼里的「原来的输入法」句柄 —— 切英文前拿它记账，退出时照它还原。

    优先级：注册表 `HKCU\\Keyboard Layout\\Preload` 的 "1"（设置里排第一的输入法，
    本机实测 00000804）→ `SPI_GETDEFAULTINPUTLANG` → 本线程布局。
    前两个在本机并不总是相等（SPI 读到 0x04090409 而 Preload 是中文），
    按玩家真实体验，Preload 才是「我平时打字用的那个」，所以排第一。
    """
    if not available():
        return 0
    klid = _preload_klid()
    if klid:
        h = _load_layout(klid)
        if h:
            return h
    try:
        u = _user32()
        val = ctypes.c_void_p(0)
        if u.SystemParametersInfoW(SPI_GETDEFAULTINPUTLANG, 0,
                                   ctypes.byref(val), 0):
            if val.value:
                return int(val.value) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        pass
    try:
        return int(ctypes.windll.user32.GetKeyboardLayout(0)) & 0xFFFFFFFF
    except Exception:
        return 0


def _preload_klid():
    """注册表里排第一的键盘布局 KLID（如 "00000804" / "d0010804"）"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Keyboard Layout\Preload")
        try:
            val, _ = winreg.QueryValueEx(k, "1")
        finally:
            winreg.CloseKey(k)
        s = str(val or "").strip()
        return s if len(s) == 8 else ""
    except Exception:
        return ""


def _load_layout(klid):
    """按 KLID 加载键盘布局（"d0010804" 这类 TSF 输入法可能加载不了 → 0）"""
    try:
        u = _user32()
        h = u.LoadKeyboardLayoutW(klid, KLF_ACTIVATE)
        if not h:
            h = u.LoadKeyboardLayoutW(klid, 0)
        return int(h or 0)
    except Exception:
        return 0


def _set_default(hkl):
    """把会话默认输入语言设成 hkl，并把本线程也切过去。

    `SystemParametersInfoW` 一律显式声明 argtypes：不声明的话 ctypes 按 c_int 猜，
    参数一旦给错（比如常量写错），系统照样执行、而且会**写坏注册表里的外观设置**。
    """
    if not hkl:
        return False
    try:
        u = _user32()
        val = ctypes.c_void_p(int(hkl))
        ok = u.SystemParametersInfoW(SPI_SETDEFAULTINPUTLANG, 0,
                                     ctypes.byref(val), SPIF_SENDCHANGE)
        act = u.ActivateKeyboardLayout(ctypes.c_void_p(int(hkl)), KLF_SETFORPROCESS)
        return bool(ok or act)
    except Exception:
        return False


def switch_to_english():
    """切英文。成功返回 True；任何异常都吞掉（换不了输入法也不该耽误开局）。"""
    global _prev_hkl, _gen
    if not available() or os.environ.get("MCASSIST_NO_IME"):
        return False
    hkl = english_hkl()
    if not hkl:
        return False
    if not _prev_hkl:
        # 记住原来是什么（中文/日文/whatever），退出游戏时放回去。
        # 记不到、或记下来的正好就是 en-US，就退回中文键盘，别让玩家卡在英文。
        cur = current_default_hkl()
        if not cur or cur == hkl:
            cur = chinese_hkl()
        _prev_hkl = cur or 0
    _gen += 1
    return _set_default(hkl)


def pending():
    """我们是不是还欠一次「切回中文」"""
    return bool(_prev_hkl)


def restore():
    """把默认输入语言切回启动游戏前的那个；没切过就什么都不做。

    成功返回 True。失败也把状态清掉，免得反复重试。
    """
    global _prev_hkl
    if not _prev_hkl:
        return False
    if not available() or os.environ.get("MCASSIST_NO_IME"):
        _prev_hkl = 0
        return False
    hkl = _prev_hkl
    _prev_hkl = 0
    return _set_default(hkl)


def generation():
    """当前这一局的编号：等待任务拿它判断「是不是又开了一局」"""
    return _gen


def launch_switch(enabled=None):
    """启动游戏 / 官方启动器之前调用：设置里开着才切（默认开）。"""
    if enabled is None:
        from .config import CONFIG
        enabled = bool(CONFIG.get(ENABLED_KEY, True))
    if not enabled:
        return False
    return switch_to_english()
