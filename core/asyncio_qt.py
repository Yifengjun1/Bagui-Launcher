# -*- coding: utf-8 -*-
"""线程池异步任务助手：后台执行 + 结果派发回主线程

实现说明（为什么不用 QRunnable + Signal）：
QThreadPool 对 autoDelete 的 QRunnable 在 run() 返回后立即析构，导致作为信号
载体的 QObject 被一并回收，已排队的主线程回调事件会被静默丢弃（图标不显示、
回调随机丢失的根源）。这里改用纯 Python 执行器 + 主线程定时派发队列，回调必达。
"""
import collections
import threading

from PySide6.QtCore import QObject, QThreadPool, QTimer

_pool = QThreadPool.globalInstance()
try:
    _pool.setMaxThreadCount(max(8, _pool.maxThreadCount() * 2))
except Exception:
    pass

from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=12, thread_name_prefix="mcgq")

_pending = collections.deque()
_lock = threading.Lock()
_timer = None


class _Dispatcher(QObject):
    pass


_dispatcher = _Dispatcher()


def _drain():
    """主线程：派发所有已完成回调"""
    while True:
        with _lock:
            if not _pending:
                return
            item = _pending.popleft()
        kind, cb, arg = item
        try:
            if kind == "done":
                cb(arg)
            else:
                cb(str(arg))
        except RuntimeError:
            pass  # 目标控件已销毁
        except Exception:
            import traceback
            traceback.print_exc()


def run_async(fn, *args, on_done=None, on_err=None, **kwargs):
    """在工作线程执行 fn，完成后在主线程回调 on_done(result) / on_err(str)"""
    global _timer

    def _job():
        # 工作线程里**绝不能跑 GC**（第七十一轮闪退根因）：主线程刚换掉的
        # 旧控件 / QPixmap 恰好在这一轮回收，就会在工作线程里被析构 →
        # QWidget/QPixmap 跨线程析构 = 访问冲突，进程无声消失。
        from . import gcguard
        gcguard.enter()
        try:
            try:
                res, exc = fn(*args, **kwargs), None
            except Exception as e:  # noqa: BLE001
                res, exc = None, e
        finally:
            gcguard.leave()
        with _lock:
            _pending.append(("done" if exc is None else "err",
                             on_done if exc is None else on_err,
                             res if exc is None else f"{exc}"))

    _executor.submit(_job)

    if _timer is None:
        _timer = QTimer(_dispatcher)
        _timer.setInterval(40)
        _timer.timeout.connect(_drain)
        _timer.start()


def pool_stats():
    return {"active": _pool.activeThreadCount(), "max": _pool.maxThreadCount()}
