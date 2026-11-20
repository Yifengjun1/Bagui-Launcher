# -*- coding: utf-8 -*-
"""Windows 磨砂玻璃（Acrylic 亚克力模糊）效果 + 圆角 + 深浅标题栏"""
import ctypes
import sys
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

GWL_STYLE = -16
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000

ACCENT_DISABLED = 1
ACCENT_ENABLE_GRADIENT = 2
ACCENT_ENABLE_TRANSPARENTGRADIENT = 3
ACCENT_ENABLE_BLURBEHIND = 4           # 普通 AERO 模糊（稳定）
ACCENT_ENABLE_ACRYLICBLURBEHIND = 5    # 亚克力磨砂（可能稍卡）

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWM_CP_ROUND = 2
DWM_CP_DONOTROUND = 1


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class _CompositionAttrData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


WCA_ACCENT_POLICY = 19


def _hwnd(widget):
    return int(widget.winId())


def enable_blur(widget, gradient_color=0x99ECEFF4, acrylic=False):
    """给窗口启用系统级磨砂玻璃背景。
    gradient_color 为 0xAABBGGRR 格式（ABGR）。"""
    if sys.platform != "win32":
        return False
    try:
        hwnd = _hwnd(widget)
        accent = _AccentPolicy()
        accent.AccentState = (ACCENT_ENABLE_ACRYLICBLURBEHIND if acrylic
                              else ACCENT_ENABLE_BLURBEHIND)
        accent.AccentFlags = 2
        accent.GradientColor = gradient_color
        data = _CompositionAttrData()
        data.Attribute = WCA_ACCENT_POLICY
        data.SizeOfData = ctypes.sizeof(accent)
        data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        ok = _user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        return bool(ok)
    except Exception:
        return False


def set_round_corners(widget, rounded=True):
    if sys.platform != "win32":
        return
    try:
        pref = DWM_CP_ROUND if rounded else DWM_CP_DONOTROUND
        v = ctypes.c_int(pref)
        _dwmapi.DwmSetWindowAttribute(_hwnd(widget), DWMWA_WINDOW_CORNER_PREFERENCE,
                                      ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def set_dark_titlebar(widget, dark=True):
    if sys.platform != "win32":
        return
    try:
        v = ctypes.c_int(1 if dark else 0)
        _dwmapi.DwmSetWindowAttribute(_hwnd(widget), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                      ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def set_border_color(widget, color=0x00000000):
    """color 为 0xAABBGGRR"""
    if sys.platform != "win32":
        return
    try:
        v = ctypes.c_uint(color)
        _dwmapi.DwmSetWindowAttribute(_hwnd(widget), DWMWA_BORDER_COLOR,
                                      ctypes.byref(v), 4)
    except Exception:
        pass
