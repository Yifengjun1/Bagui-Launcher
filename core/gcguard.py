# -*- coding: utf-8 -*-
"""后台线程里不许跑 GC —— 闪退（0xC0000005）的真凶。

第七十一轮定位到的现场（`<数据目录>\\logs\\crash.log`）：

    Windows fatal exception: access violation
    Current thread 0x000088a8:
      Garbage-collecting
      ... json/decoder.py -> installer._json_file -> is_pack_dir
      ... list_game_versions -> gamemanager.work -> asyncio_qt._job

也就是说：主线程刚把「游戏管理」里几十行控件换成新的，旧的 `_GameRow` / 图标
`QPixmap` 变成垃圾；恰好这时**工作线程**里一次 `json.load` 分配触发了一轮 GC，
于是这些 Qt 对象在工作线程里被析构 —— QPixmap / QWidget 的析构必须在 GUI 线程，
跨线程析构就是访问冲突，进程直接消失，Python 层连异常都没有。

所以规则：**GC 只在主线程跑**。
  · `enter()/leave()` 把工作线程包起来，期间 `gc.disable()`；
  · 计数器保证并发时不会有人提前 `enable()`；
  · `tune()` 在启动时跑一次：`gc.freeze()` 冻结启动期对象（不再每轮扫描），
    阈值调大（主线程也少跑几轮）。

用法：
    with gcguard.paused():
        ...工作线程里的一段...
或者装饰器 `@gcguard.guarded`。
"""
import contextlib
import functools
import gc
import threading

_lock = threading.Lock()
_active = 0            # 当前有多少个工作线程正处在「禁 GC」区
_was_enabled = False   # 进区之前 GC 是开着的吗（原本关了就别替玩家打开）
_tuned = False


def enter():
    global _active, _was_enabled
    with _lock:
        _active += 1
        if _active == 1:
            _was_enabled = gc.isenabled()
            if _was_enabled:
                gc.disable()


def leave():
    global _active
    with _lock:
        if _active > 0:
            _active -= 1
        if _active == 0 and _was_enabled:
            gc.enable()


@contextlib.contextmanager
def paused():
    enter()
    try:
        yield
    finally:
        leave()


def guarded(fn):
    """装饰器：整段函数执行期间关掉 GC（给线程池的 job 用）"""
    @functools.wraps(fn)
    def wrap(*a, **kw):
        enter()
        try:
            return fn(*a, **kw)
        finally:
            leave()
    return wrap


def tune():
    """启动时调一次：冻结启动期对象 + 放宽阈值，减少主线程的 GC 频率。

    必须在建主窗口**之前**调用 —— 冻住的对象永远不会被回收，
    已经建好的控件被冻住就等于泄漏。
    """
    global _tuned
    if _tuned:
        return
    _tuned = True
    try:
        gc.freeze()
    except Exception:                                  # noqa: BLE001
        pass
    try:
        # 默认 (700, 10, 10) 对「一秒建几十个控件」的界面太敏感：每建几行就全表扫一遍。
        gc.set_threshold(20000, 50, 100)
    except Exception:                                  # noqa: BLE001
        pass


def stats() -> dict:
    return {"active": _active, "enabled": gc.isenabled(),
            "counts": list(gc.get_count())}
