# -*- coding: utf-8 -*-
"""看游戏窗口出来没有（第五十四轮）

悬浮球上那条加载进度要「跟着游戏实际窗口走」，所以这里只做一件事：
问 Windows 这个进程现在有没有窗口、窗口显示出来没有。

三档状态：
  none     进程在跑，但还没有游戏窗口（JVM 正在装配、加载类）
  created  窗口对象建出来了，但还没显示（MC 在这之后才开始渲染主界面）
  visible  窗口真的显示出来了 —— 进度到 100% 就是这一刻

只用 user32 的只读查询（EnumWindows / GetWindowRect / IsWindowVisible），
不注入、不挂钩子、不碰游戏进程。
"""
import ctypes
import os
from ctypes import wintypes

# 窗口类名：老版 LWJGL 用 "LWJGL"，新版 GLFW 用 "GLFW30"
_CLS_HINT = ("lwjgl", "glfw", "sunawtframe", "jawt")
# 标题关键字：万一类名变了（某些加载器会换），按标题兜一层
_TITLE_HINT = ("minecraft", "forge", "fabric", "neoforge", "quilt")


def _enum(pid: int = 0) -> list:
    """枚举顶层窗口（pid=0 表示全部）。非 Windows 返回空列表。"""
    out = []
    if os.name != "nt":
        return out
    try:
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lp):
            try:
                p = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if pid and int(p.value) != int(pid):
                    return True
                ln = int(user32.GetWindowTextLengthW(hwnd) or 0)
                tb = ctypes.create_unicode_buffer(ln + 2)
                user32.GetWindowTextW(hwnd, tb, ln + 2)
                cb = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cb, 256)
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                out.append({"pid": int(p.value), "title": tb.value,
                            "cls": cb.value,
                            "visible": bool(user32.IsWindowVisible(hwnd)),
                            "w": int(r.right - r.left),
                            "h": int(r.bottom - r.top)})
            except Exception:
                pass
            return True                    # 继续枚举

        fn = proto(_cb)                # 一定要留住引用：当场被回收会直接崩
        user32.EnumWindows(fn, 0)
    except Exception:
        return out
    return out

def _looks_like_game(w: dict) -> bool:
    cls = str(w.get("cls") or "").lower()
    title = str(w.get("title") or "").lower()
    if any(k in cls for k in _CLS_HINT):
        return True
    return any(k in title for k in _TITLE_HINT)


def game_window_state(pid: int = 0) -> str:
    """'visible' / 'created' / 'none'（pid=0 表示全系统找）"""
    wins = [w for w in _enum(pid) if _looks_like_game(w)]
    if not wins:
        return "none"
    for w in wins:
        # 主窗口至少得有个像样的尺寸；MC 还会建几个 0×0 的隐藏窗口，别认它们
        if w["visible"] and w["w"] > 240 and w["h"] > 160:
            return "visible"
    return "created"


def game_window_title(pid: int = 0) -> str:
    """游戏窗口标题（拿来显示「Minecraft 1.20.1」这种，没有就空串）"""
    for w in _enum(pid):
        if _looks_like_game(w) and w["visible"] and w["w"] > 240:
            return str(w.get("title") or "")
    return ""
