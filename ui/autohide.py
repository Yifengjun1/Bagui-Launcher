# -*- coding: utf-8 -*-
"""内容自动隐藏（第三十三轮：从主页卡片提升到整个内容区）。

设置 → 常规 →「卡片隐藏」开起来以后：
  · 鼠标在**内容区**里动 → 重新计时（不显示，只是把「该藏了」往后推）；
  · 鼠标停在**顶部状态栏**上、或者**移出窗口**（切到别的程序）→ 计时继续走，到点就藏；
  · 鼠标从状态栏挪回内容区、或者从窗口外挪回来 → 只是重新计时，**不会**把内容叫回来；
  · **只有点一下**（窗口里任意位置）才恢复显示。

藏的是「当前页 + 右侧启动选择抽屉」，顶部状态栏（titlebar）永远不动。
主页和其它下载页一视同仁 —— 之前只藏主页卡片，切到光影/材质包那些页就不管用了。

为什么藏「整页 widget」而不是给每页加一个卡片接口：
  各页结构差别太大（主页是一整块 FrostCard，下载页是若干卡片 + 侧栏），
  统一 hide() 当前 stack 页最省事，而且视觉上就是「只剩背景 + 状态栏」。
"""
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from ..core.config import CONFIG


class ContentAutoHide(QObject):
    """挂在主窗口上：一个计时器 + 一个应用级事件过滤，管所有页面的隐藏/恢复。"""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._on = False
        self._ms = 3000
        self._hidden = False
        self._filter_on = False
        self._page = None            # 藏起来的那一页
        self._panel = False          # 抽屉当时是不是开着的
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_now)

    # ---------------- 开关 / 计时 ----------------
    def apply(self):
        """按配置重装（启动时、设置保存后各调一次）"""
        self._on = bool(CONFIG.get("card_auto_hide"))
        secs = int(CONFIG.get("card_hide_secs") or 3)
        self._ms = max(1, min(600, secs)) * 1000
        if not self._on:
            self._timer.stop()
            self._uninstall()
            self.show_now(restart=False)
            return
        self.show_now(restart=False)
        if self.win.isVisible():
            self._install()
            self._timer.start(self._ms)

    def pause(self):
        """窗口要收成浮球 / 要退出了：停表 + 卸过滤，内容恢复（下次进来是正常样子）"""
        self._timer.stop()
        self._uninstall()
        self.show_now(restart=False)

    def resume(self):
        """窗口回来了：重新装过滤、重新计时"""
        if not self._on:
            return
        self._install()
        self._timer.start(self._ms)

    def reset(self):
        """切页 / 开抽屉：内容立刻恢复，计时从头开始"""
        self.show_now(restart=False)
        if self._on and self.win.isVisible():
            self._timer.start(self._ms)

    def _ensure_running(self):
        """计时器没在跑就起一个（鼠标在状态栏 / 在窗口外的时候用）"""
        if self._hidden or not self._on:
            return
        if not self._timer.isActive():
            self._timer.start(self._ms)

    # ---------------- 藏 / 显 ----------------
    def hide_now(self):
        if not self._on or self._hidden or not self.win.isVisible():
            return
        # 最小化时说不上「看着窗口发呆」，还原时不该是一片空白
        if self.win.isMinimized():
            self._timer.start(self._ms)
            return
        # 有模态弹窗（设置、导入…）时先不藏，等它关掉再说
        if QApplication.activeModalWidget() is not None:
            self._timer.start(self._ms)
            return
        stack = getattr(self.win, "stack", None)
        page = stack.currentWidget() if stack is not None else None
        self._page = page
        if page is not None:
            page.hide()
        panel = getattr(self.win, "page_launch", None)
        if panel is not None and panel.isVisible():
            self._panel = True
            panel.hide()
        # 第一百轮：那根白竖条也是内容区的一部分，藏内容时别把它单独留在屏幕上
        h = getattr(self.win, "_drawer_handle", None)
        if h is not None and h.isVisible():
            self._handle = True
            h.hide()
        self._hidden = True

    def show_now(self, restart=True):
        if self._hidden:
            page, self._page = self._page, None
            if page is not None:
                try:
                    page.show()
                except RuntimeError:
                    pass
            if self._panel:
                self._panel = False
                panel = getattr(self.win, "page_launch", None)
                if panel is not None:
                    try:
                        lay = getattr(self.win, "_layout_launch_panel", None)
                        if lay:
                            lay()
                        panel.show()
                        panel.raise_()
                    except RuntimeError:
                        pass
            if getattr(self, "_handle", False):
                self._handle = False
                sync = getattr(self.win, "_sync_drawer_handle", None)
                if sync is not None:
                    try:
                        sync()
                    except RuntimeError:
                        pass
            self._hidden = False
        if restart and self._on and self.win.isVisible():
            self._timer.start(self._ms)

    def poke(self, click):
        """有交互就重新计时；藏着的时候只有「点一下」才把它叫回来"""
        if not self._on:
            return
        if self._hidden:
            if click:
                self.show_now(restart=True)
            return
        self._timer.start(self._ms)

    # ---------------- 事件过滤 ----------------
    def _install(self):
        if self._filter_on:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._filter_on = True

    def _uninstall(self):
        if not self._filter_on:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._filter_on = False

    def _in_titlebar(self, gpt):
        """这个全局坐标是不是落在顶部状态栏上（状态栏不算「在用内容」）"""
        tb = getattr(self.win, "titlebar", None)
        if tb is None:
            return False
        try:
            return tb.rect().contains(tb.mapFromGlobal(gpt))
        except RuntimeError:
            return False

    def eventFilter(self, obj, ev):
        # 只认本窗口里的事件：托盘的 DeskToast、别的弹窗不该重置计时
        try:
            w = obj if isinstance(obj, QWidget) else None
            if w is None or w.window() is not self.win:
                return False
        except RuntimeError:
            return False
        if not self._on or not self.win.isVisible():
            return False
        t = ev.type()
        if t == QEvent.MouseButtonPress:
            self.poke(True)
        elif t in (QEvent.MouseMove, QEvent.Wheel, QEvent.KeyPress,
                   QEvent.TouchBegin):
            gpt = None
            try:
                gpt = ev.globalPosition().toPoint()
            except Exception:
                gpt = None
            if gpt is not None and self._in_titlebar(gpt):
                self._ensure_running()      # 停在状态栏上：让它到点就藏
            else:
                self.poke(False)
        elif t == QEvent.Leave and w is self.win:
            self._ensure_running()          # 鼠标出窗：接着倒计时
        elif t == QEvent.Enter and w is self.win:
            self.poke(False)                # 回到窗内：重新计时，但不显示
        return False
