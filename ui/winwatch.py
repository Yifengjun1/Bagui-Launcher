# -*- coding: utf-8 -*-
"""野窗口守卫（第八十二轮）：桌面上不许出现「带原生标题栏的顶层控件」。

**为什么需要这一层**
本软件所有窗口都是自绘无边框的（主窗 / 开场页 / 弹层 / 提示 / 引导小窗全部带
`Qt.FramelessWindowHint`）。所以只要桌面上冒出一块**带 Windows 原生标题栏**的
窗口，就一定是意外产物 —— 玩家录屏里那一列列「白标题栏 + 空白客户区」的小窗
（标题还写着「八桂启动器」）就是这么来的。

**根因**（`tools/_probe_r82.py` 可稳定复现）
网格里的控件习惯「先无父创建、再 addWidget」：

    tip = QLabel("正在搜索…")      # 此刻它是一只**顶层窗口**
    self.grid.addWidget(tip)       # addWidget 才把它挂到宿主上

`QLayout::addChildWidget()` 挂父窗后，会把「显示」这件事**排队**发出去
（`_q_showIfNotHidden`，QueuedConnection）。只要在这条排队事件送达之前控件
先被 `setParent(None)` 摘离（换列数 / 换一批结果 / 清空网格都会这么做），
这条 show 就落在一只**已经没有父窗的控件**上 —— 它当场变成一块带原生标题栏
的顶层窗口：标题取 `QCoreApplication::applicationName()`（所以是「八桂启动器」），
客户区空白，而 `setParent(None)` 又保留了原来的**父窗坐标**，于是这堆窗口
全部堆在屏幕左上角，正好排成原来的卡片网格。

**两层修法**
① 源头（`browse.py` / `loaderpanel.py`）：创建时就给父窗；摘离控件时只
   `hide()` + `deleteLater()`，**不再 `setParent(None)`**。
② 兜底（本文件）：任何「可见 + 非无边框 + 不是真对话框」的顶层控件，
   一露面就摁回去（隐藏 + 挂进一间永不显示的收容所），并写一条日志。
   这样即使以后又有人在别处写出同样的顺序，玩家也看不到那块白窗。
"""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

_GUARD = None
_GRAVE = None

# 允许「没有 FramelessWindowHint」的窗口类型：这些在 Windows 上本来就是
# 无标题栏的轻量窗（或者系统对话框），不是野窗口。
_OK_TYPES = (
    Qt.Popup,
    Qt.ToolTip,
    Qt.Tool,
    Qt.Dialog,
    Qt.Sheet,
    Qt.SplashScreen,
)

# 认得出是「真对话框」的类名（万一有人忘了给 FramelessWindowHint 也别误杀）
_OK_NAMES = frozenset((
    "QFileDialog", "QColorDialog", "QFontDialog", "QInputDialog",
    "QProgressDialog", "QMessageBox",
))


def _grave():
    """收容所：一只**从不显示**的空 QWidget。

    把野窗口 reparent 到它下面，之后就算有什么排队事件再喊 show()，
    子控件也永远显示不出来（父窗是隐藏的）。
    """
    global _GRAVE
    if _GRAVE is None:
        _GRAVE = QWidget()
        _GRAVE.setObjectName("bgl-win-graveyard")
        _GRAVE.resize(1, 1)
    return _GRAVE


def _note(text):
    try:
        from ..core import crashlog
        crashlog.note(text)
    except Exception:                                    # noqa: BLE001
        pass


class _Guard(QObject):
    """只看一件事：谁在桌面上摆出了一块带标题栏的窗口"""

    def __init__(self):
        super().__init__()
        self.hits = 0

    def _is_suspect(self, w):
        flags = w.windowFlags()
        if flags & Qt.FramelessWindowHint:
            return False
        if (flags & Qt.WindowType_Mask) in _OK_TYPES:
            return False
        if type(w).__name__ in _OK_NAMES:
            return False
        return True

    def eventFilter(self, obj, ev):
        try:
            if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
                if self._is_suspect(obj):
                    self.hits += 1
                    if self.hits <= 24:              # 日志别刷屏
                        _note("野窗口已拦下：%s %r %s"
                              % (type(obj).__name__, obj.windowTitle(),
                                 (obj.x(), obj.y(), obj.width(), obj.height())))
                    obj.hide()
                    obj.setParent(_grave())          # 之后再也 show 不出来
        except RuntimeError:
            pass
        except Exception:                            # noqa: BLE001
            pass
        return False


def install(app=None):
    """装在 QApplication 上（幂等）。返回守卫对象，方便测试断言命中次数。"""
    global _GUARD
    if _GUARD is not None:
        return _GUARD
    app = app or QApplication.instance()
    if app is None:
        return None
    _GUARD = _Guard()
    app.installEventFilter(_GUARD)
    return _GUARD


def hits():
    return 0 if _GUARD is None else int(_GUARD.hits)
