# -*- coding: utf-8 -*-
"""崩溃日志：把「闪退」变成一份看得见的文件。

为什么要这一层：PySide6 的 Python 异常基本都被 `asyncio_qt._drain()` 兜住了，
真正让进程**凭空消失、连报错都没有**的只剩两类：

  ① **原生崩溃**（访问冲突 0xC0000005 / 栈溢出 / C++ 对象已析构仍被使用）；
  ② **Qt 的 qFatal**（比如 QPainter 在 paintEvent 之外用、QPixmap 在工作线程创建）。

这两类 Python 层面看不到任何痕迹，只有 `faulthandler` 能把出事那一刻**所有线程
的 C/Python 栈直接写进文件**。所以软件一启动就把它挂上，下一次崩了就能直接读到
「死在哪个函数、哪个线程、哪个窗口」。

日志位置：`%APPDATA%\\bgl_datat_config\\logs\\crash.log`（便携 / 测试走 MCASSIST_DATA_DIR）。

用法：`main()` 里建完 QApplication 后立刻 `install()`。
"""
import faulthandler
import os
import sys
import threading
import time
import traceback

_HEAD_MARKERS = (
    "Fatal Python error",
    "Windows fatal exception",
    "Unhandled exception",
    "---- 未捕获异常",
)
_THREAD_MARKERS = (
    "Current thread 0x",
    "Thread 0x",
)
_MARKERS = _HEAD_MARKERS + _THREAD_MARKERS

_fh = None
_path = ""
_prev = ""            # 上次运行留下的致命段（install() 时读出来）
_installed = False


def log_dir() -> str:
    from .config import app_data_dir
    d = os.path.join(app_data_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def log_path() -> str:
    global _path
    if not _path:
        _path = os.path.join(log_dir(), "crash.log")
    return _path


def _read_tail(path: str, limit: int = 200000) -> str:
    try:
        n = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if n > limit:
                f.seek(n - limit)
            return f.read()
    except Exception:
        return ""


def _split_fatal(text: str) -> str:
    """从旧日志里挑出最后一次致命段（致命标记之后到文件尾）。

    faulthandler 的格式是「`Windows fatal exception: access violation`」空行
    再跟「`Current thread 0x…`」+ 栈，所以要从**表头**那行开始切；只认
    「Current thread」会把最关键的那一行丢掉。
    """
    def last(markers):
        idx = -1
        for m in markers:
            i = text.rfind(m)
            if i > idx:
                idx = i
        return idx

    idx = last(_HEAD_MARKERS)
    if idx < 0:
        idx = last(_THREAD_MARKERS)
    if idx < 0:
        return ""
    return text[idx:].strip()


def previous_fatal() -> str:
    """上一次运行是不是崩的；是就返回能看懂的那一段（否则空串）"""
    return _prev


def install() -> str:
    """挂上所有能留痕的钩子，返回日志路径（失败也不影响软件运行）"""
    global _fh, _prev, _installed
    if _installed:
        return log_path()
    _installed = True
    path = log_path()
    _prev = _split_fatal(_read_tail(path))

    try:
        from .config import APP_NAME, APP_VERSION
        ver = f"{APP_NAME} {APP_VERSION}"
    except Exception:
        ver = "?"
    try:
        _fh = open(path, "a", encoding="utf-8", errors="replace")
        _fh.write("\n" + "=" * 72 + "\n")
        _fh.write("启动 %s  pid=%s  %s\n" % (
            ver, os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
        _fh.write("平台 %s  Python %s  exe=%s\n" % (
            sys.platform, sys.version.split()[0], sys.executable))
        _fh.flush()
        # 关键：原生崩溃时把各线程栈刷进同一个文件
        faulthandler.enable(file=_fh, all_threads=True)
    except Exception:
        _fh = None

    def _hook(exc_type, exc, tb):
        try:
            from .config import APP_VERSION
        except Exception:
            APP_VERSION = "?"
        try:
            if _fh is not None:
                _fh.write("\n---- 未捕获异常 v%s %s ----\n"
                          % (APP_VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
                traceback.print_exception(exc_type, exc, tb, file=_fh)
                _fh.flush()
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thook(args):
        try:
            if _fh is not None:
                _fh.write("\n---- 线程 %s 未捕获异常 ----\n"
                          % getattr(args.thread, "name", "?"))
                traceback.print_exception(args.exc_type, args.exc_value,
                                          args.exc_traceback, file=_fh)
                _fh.flush()
        except Exception:
            pass
        try:
            threading.__excepthook__(args)
        except Exception:
            pass

    try:
        threading.excepthook = _thook
    except Exception:
        pass

    # Qt 自己的警告 / qFatal 也要留痕：QPixmap 跨线程之类只有 Qt 会喊
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def _qt(mode, ctx, msg):
            try:
                line = "[qt] %s\n" % msg
                if mode == QtMsgType.QtFatalMsg:
                    line = "[qt FATAL] %s\n" % msg
                if _fh is not None:
                    _fh.write(line)
                    _fh.flush()
                sys.stderr.write(line)
            except Exception:
                pass

        qInstallMessageHandler(_qt)
    except Exception:
        pass

    return path


def note(msg: str):
    """方便在关键路径上留一句「到这一步了」（排查闪退时非常有用）"""
    try:
        if _fh is not None:
            _fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
            _fh.flush()
    except Exception:
        pass
