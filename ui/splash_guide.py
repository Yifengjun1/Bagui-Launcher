# -*- coding: utf-8 -*-
"""欢迎页「使用指南」链接：定位并打开随安装的指南文档。

文档随安装向导放进 `Osmanthus flower\\User Guide\\`（= 指南的英文名）目录，
简体/繁体各 docx+pdf 两份。打开逻辑：

* 界面是繁体（台式/港式/澳式任一）→ 用繁体版；简体 → 用简体版；
* 同语言里 **能开 .docx 就用 docx，不能就用 pdf**（注册表只读检测）；
* 选中的打不开 → 回退该语言的另一格式，再回退另一语言；
* 一份都没有 → 明确提示，绝不静默。

打开用 `os.startfile`（Windows Shell 关联程序），比内置渲染可靠。
docx 阅读器检测用标准库 `winreg`，不依赖 pywin32。
"""
import os
import sys

from ..core import crashlog, paths

# 简体 / 繁体各一份 docx + pdf
DOCX_NAME = "八桂启动器使用指南.docx"
PDF_NAME = "八桂启动器使用指南.pdf"
DOCX_TRAD = "八桂启动器使用指南-繁体.docx"
PDF_TRAD = "八桂启动器使用指南-繁体.pdf"

GUIDE_FOLDER = "User Guide"          # 「使用指南」的英文名（玩家指定）


def guide_dir() -> str:
    """指南所在目录 = 安装根目录下的 User Guide 文件夹。"""
    try:
        root = paths.install_root()
    except Exception:
        root = (getattr(sys, "_MEIPASS", "") or
                os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.join(root, GUIDE_FOLDER)


def docx_reader_available() -> bool:
    """系统是否有能打开 .docx 的关联程序（Word / WPS 等）。

    只读注册表，不启动任何程序。异常一律按「没有阅读器」处理
    —— 宁可走 pdf，也不把玩家丢给打不开的 docx。
    """
    if os.name != "nt":
        return False
    try:
        import winreg
    except Exception:
        return False

    progids = []
    try:
        uc = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.docx\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uc) as k:
            val, _ = winreg.QueryValueEx(k, "ProgId")
            if val:
                progids.append(str(val))
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".docx") as k:
            val, _ = winreg.QueryValueEx(k, "")
            if val:
                progids.append(str(val))
    except OSError:
        pass
    progids += ["docxfile", "WPS.Docx.6", "Word.Document.12", "Word.Document.8"]

    for pid in progids:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                pid + r"\shell\open\command") as k:
                cmd, _ = winreg.QueryValueEx(k, "")
                if cmd and str(cmd).strip():
                    return True
        except OSError:
            continue
    return False


def _order() -> list:
    """按当前界面语言 + 阅读器可用性给出候选文件名优先级。"""
    try:
        from ..core import i18n
        traditional = i18n.is_traditional()
    except Exception:
        traditional = False

    if traditional:
        primary_docx, primary_pdf = DOCX_TRAD, PDF_TRAD
        fallback_docx, fallback_pdf = DOCX_NAME, PDF_NAME
    else:
        primary_docx, primary_pdf = DOCX_NAME, PDF_NAME
        fallback_docx, fallback_pdf = DOCX_TRAD, PDF_TRAD

    order = []
    if docx_reader_available():
        order += [primary_docx, primary_pdf]
    else:
        # 没有阅读器：pdf 优先于 docx（docx 点了只会弹「选择打开方式」）
        order += [primary_pdf, primary_docx]
    order += [fallback_docx, fallback_pdf]
    return order


def find_guide() -> str:
    """按语言 + 阅读器优先级返回存在的指南文件绝对路径，都没有返回空串。"""
    root = guide_dir()
    for name in _order():
        p = os.path.join(root, name)
        if os.path.isfile(p):
            return p
    return ""


def open_guide() -> str:
    """打开指南，返回被打开的文件路径；失败返回空串并留痕 + 提示。"""
    root = guide_dir()
    tried = []
    for name in _order():
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        tried.append(p)
        try:
            os.startfile(p)                      # noqa: S606  Windows Shell
            return p
        except Exception as e:
            crashlog.note("使用指南：打开失败 %s | %r" % (p, e))
            continue
    if tried:
        crashlog.note("使用指南：所有候选都打不开 %s" % tried)
        _toast("无法打开使用指南。请安装 Word / WPS 或 PDF 阅读器后重试。")
    else:
        crashlog.note("使用指南：找不到文档（User Guide 目录为空或不存在）")
        _toast("找不到使用指南文档，请确认安装目录下 User Guide 文件夹里有指南文件。")
    return ""


def _toast(msg: str):
    """优先桌面级提示（欢迎页期间主窗可能还没露出来），失败退 stderr。"""
    try:
        from .widgets import DeskToast
        DeskToast.show_desktop(msg)
        return
    except Exception:
        pass
    try:
        from PySide6.QtWidgets import QApplication
        from .widgets import Toast
        parent = QApplication.activeWindow()
        Toast.show_toast(parent, msg)
        return
    except Exception:
        pass
    try:
        print("[使用指南]", msg, file=sys.stderr, flush=True)
    except Exception:
        pass
