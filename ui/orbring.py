# -*- coding: utf-8 -*-
"""悬浮球上的「游戏加载」提示（第五十四轮）

玩家点独立启动后主窗口收成小球，这里负责在球上把「游戏到底起来没有」讲清楚：

  t=0          聊天气泡：「游戏进程已启动」
  t≈0.6s       绕球一圈的环形进度条出现并开始转
  t=3s         气泡改成「游戏加载中… 42%」，百分比跟着游戏实际状态走
  窗口显示出来  立刻 100%，气泡「加载完成 · 100%」，1.6 秒后一起淡出

百分比不是瞎转圈，来自 `core/winwatch.py` 查到的**真实窗口状态**：
  · 进程在跑、还没有游戏窗口      → 时间曲线慢慢爬到 88%
  · 窗口对象建出来了、还没显示    → 上限抬到 96%（这一步通常只剩几秒）
  · 窗口显示出来了                → 100%
所以「窗口出现时进度条到 100%」是硬条件。

两个都是独立的置顶透明小窗（不放大悬浮球本身）：球还是 56×56，拖动、
左键恢复、右键菜单的命中和以前一模一样，进度条只是在它外面画一圈。
"""
import math
import time

from PySide6.QtCore import (QObject, QRectF, Qt, QTimer, QPropertyAnimation,
                            QEasingCurve)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

from ..core import winwatch
from . import theme

# 置顶、无边框、鼠标穿透：绝不抢焦点、绝不挡住球和其它窗口的点击
_FLAGS = (Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
          | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)


def _fade_out(w, ms=260):
    if w is None or not w.isVisible():
        return
    anim = QPropertyAnimation(w, b"windowOpacity", w)
    anim.setDuration(ms)
    anim.setStartValue(float(w.windowOpacity()))
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.InCubic)
    anim.finished.connect(w.hide)
    anim.start()
    w._fade = anim              # 防 GC


class OrbRing(QWidget):
    """绕悬浮球一圈的环形进度条（含一段不停转的高光，表示还在动）"""

    DIAM = 78
    PEN = 3.6

    def __init__(self):
        super().__init__(None, _FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(self.DIAM, self.DIAM)
        self._pct = 0.0
        self._spin = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)
        self.hide()

    def _advance(self):
        self._spin = (self._spin + 7.5) % 360.0
        self.update()

    def set_progress(self, pct):
        self._pct = max(0.0, min(1.0, float(pct)))
        self.update()

    def place_on(self, orb):
        c = orb.mapToGlobal(orb.rect().center())
        self.move(int(c.x() - self.width() / 2), int(c.y() - self.height() / 2))

    def start_spin(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop_spin(self):
        self._timer.stop()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        half = self.PEN / 2 + 1.5
        r = QRectF(self.rect()).adjusted(half, half, -half, -half)
        # 轨道：深色半透明，深色壁纸上也看得见
        p.setPen(QPen(QColor(24, 32, 46, 78), self.PEN, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, 0, 360 * 16)
        # 转着的高光：告诉玩家「在动，不是卡住」
        if self._pct < 0.999:
            p.setPen(QPen(QColor(255, 255, 255, 165), self.PEN,
                          Qt.SolidLine, Qt.RoundCap))
            p.drawArc(r, int(-self._spin * 16), int(-42 * 16))
        # 进度弧：从正上方顺时针
        if self._pct > 0.001:
            p.setPen(QPen(QColor(theme.C_ACCENT), self.PEN + 0.6,
                          Qt.SolidLine, Qt.RoundCap))
            p.drawArc(r, 90 * 16, int(-max(10.0, self._pct * 360.0) * 16))
        if self._pct >= 0.999:
            p.setPen(QPen(QColor(255, 255, 255, 200), self.PEN - 1.2,
                          Qt.SolidLine, Qt.RoundCap))
            p.drawArc(r.adjusted(2.6, 2.6, -2.6, -2.6), 0, 360 * 16)
        p.end()


class OrbBubble(QWidget):
    """贴在球边上的聊天气泡（白底 + 小尖角，跟着球走）"""

    H = 36
    PAD = 16
    TAIL = 7
    # 环比球大一圈（DIAM 78 vs 56，左右各多 11px）：气泡本体要让开 11，再加小尖角 7，
    # 留 2px 余量 —— 不然环一出来就压在气泡的小尖角上
    GAP = 20

    def __init__(self):
        super().__init__(None, _FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._font = QFont(theme.FONT)
        self._font.setPixelSize(12)
        self._font.setBold(True)
        self._text = ""
        self._tail_right = True
        self.setFixedSize(120, self.H)
        self.hide()

    def text(self):
        return self._text

    def say(self, text, orb=None, animate=True):
        self._text = str(text)
        self._resize()
        if orb is not None:
            self.place_on(orb)
        first = not self.isVisible()
        if first:
            self.setWindowOpacity(0.0 if animate else 1.0)
            self.show()
            self.raise_()
            if animate:
                anim = QPropertyAnimation(self, b"windowOpacity", self)
                anim.setDuration(200)
                anim.setStartValue(0.0)
                anim.setEndValue(1.0)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.start()
                self._fade = anim
        self.update()

    def _resize(self):
        fm = QFontMetrics(self._font)
        w = fm.horizontalAdvance(self._text) + self.PAD * 2 + self.TAIL + 2
        self.setFixedSize(max(104, w), self.H)

    def place_on(self, orb):
        scr = (QGuiApplication.screenAt(orb.mapToGlobal(orb.rect().center()))
               or orb.screen() or QGuiApplication.primaryScreen())
        area = scr.availableGeometry() if scr is not None else None
        og = orb.geometry()                     # 顶层窗口 → 已经是全局坐标
        w, h = self.width(), self.height()
        left = og.left() - w - self.GAP
        if area is not None and left < area.left() + 2:
            left = og.right() + self.GAP        # 左边放不下 → 翻到右边
            self._tail_right = False
        else:
            self._tail_right = True
        top = og.center().y() - h // 2
        if area is not None:
            left = max(area.left() + 2, min(left, area.right() - w - 2))
            top = max(area.top() + 2, min(top, area.bottom() - h - 2))
        self.move(int(left), int(top))

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        body = QRectF(r)
        if self._tail_right:
            body.setRight(r.right() - self.TAIL)
        else:
            body.setLeft(r.left() + self.TAIL)
        path = QPainterPath()
        path.addRoundedRect(body, 11, 11)
        # 小尖角：朝球那一侧
        cy = r.center().y()
        tail = QPainterPath()
        if self._tail_right:
            tail.moveTo(body.right() - 0.5, cy - 5.5)
            tail.lineTo(r.right() - 1, cy)
            tail.lineTo(body.right() - 0.5, cy + 5.5)
        else:
            tail.moveTo(body.left() + 0.5, cy - 5.5)
            tail.lineTo(r.left() + 1, cy)
            tail.lineTo(body.left() + 0.5, cy + 5.5)
        tail.closeSubpath()
        path = path.united(tail)
        p.setPen(QPen(QColor(196, 205, 218, 235), 1))
        p.setBrush(QColor(255, 255, 255, 246))
        p.drawPath(path)
        p.setFont(self._font)
        p.setPen(QPen(QColor(42, 52, 66)))
        p.drawText(body.adjusted(self.PAD - 4, 0, -self.PAD + 4, 0),
                   int(Qt.AlignCenter), self._text)
        p.end()


class OrbLoadTrack(QObject):
    """把「进程已启动 → 加载中 → 加载完成」这段时间落在球旁边的调度器。

    只在 UI 线程里跑（QTimer 200ms 一跳），跳一次只做几件事：
    查一次窗口状态（EnumWindows 级，1~3ms）、算百分比、挪一下两个小窗。
    """

    TICK_MS = 200
    RING_DELAY = 0.55          # 气泡先单独显示这么久，再出环
    SWITCH_SEC = 3.0           # 3 秒后气泡换成「游戏加载中… N%」
    HOLD_DONE_SEC = 1.6        # 到 100% 后再停留这么久，然后淡出

    def __init__(self, orb):
        super().__init__(orb)
        self._orb = orb
        self._ring = None
        self._bubble = None
        self._pid = 0
        self._t0 = 0.0
        self._pct = 0.0
        self._stage = 0                 # 0=已启动提示 / 1=环形进度 / 2=完成
        self._win = "none"
        self._done_at = 0.0
        self._text = ""
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ---------------- 对外 ----------------
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self, pid=0, name=""):
        self._pid = int(pid or 0)
        self._t0 = time.monotonic()
        self._pct = 2.0
        self._stage = 0
        self._win = "none"
        self._text = ""
        self._ensure()
        for w in (self._bubble, self._ring):
            try:
                if getattr(w, "_fade", None) is not None:
                    w._fade.stop()          # 上一局的淡出还在跑，别把这一局一起隐藏
                w.setWindowOpacity(1.0)
            except Exception:
                pass
        self._ring.set_progress(0.0)
        self._ring.hide()
        self._ring.stop_spin()
        self._say("游戏进程已启动")
        # 球拖动时要跟着挪（FloatOrb 的 moveEvent 会回调这个）
        try:
            self._orb._on_move = self._follow
        except Exception:
            pass
        self._timer.start()
        self._follow()

    def stop(self, fade=True):
        self._timer.stop()
        try:
            if self._orb is not None and getattr(self._orb, "_on_move", None) == self._follow:
                self._orb._on_move = None
        except Exception:
            pass
        self._stage = -1
        if self._ring is not None:
            self._ring.stop_spin()
        if fade:
            _fade_out(self._bubble, 240)
            _fade_out(self._ring, 240)
        else:
            for w in (self._bubble, self._ring):
                if w is not None:
                    w.hide()

    # ---------------- 内部 ----------------
    def _ensure(self):
        if self._ring is None:
            self._ring = OrbRing()
        if self._bubble is None:
            self._bubble = OrbBubble()

    def _say(self, text, animate=True):
        if text == self._text and self._bubble.isVisible():
            self._bubble.place_on(self._orb)
            return
        self._text = text
        self._bubble.say(text, self._orb, animate=animate)

    def _follow(self):
        if self._bubble is not None and self._bubble.isVisible():
            self._bubble.place_on(self._orb)
        if self._ring is not None and self._ring.isVisible():
            self._ring.place_on(self._orb)

    def _target(self, el):
        """目标百分比：窗口显示出来=100，否则按状态给上限 + 时间曲线"""
        if self._win == "visible":
            return 100.0
        cap = 96.0 if self._win == "created" else 88.0
        return max(12.0, min(cap, cap * (1.0 - math.exp(-el / 8.0))))

    def _tick(self):
        if self._orb is None or not self._orb.isVisible():
            self.stop()
            return
        el = time.monotonic() - self._t0

        if self._stage == 0:
            if el >= self.RING_DELAY:
                self._stage = 1
                self._ring.set_progress(self._pct / 100.0)
                self._ring.place_on(self._orb)
                self._ring.setWindowOpacity(0.0)
                self._ring.show()
                self._ring.raise_()
                anim = QPropertyAnimation(self._ring, b"windowOpacity", self._ring)
                anim.setDuration(220)
                anim.setStartValue(0.0)
                anim.setEndValue(1.0)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.start()
                self._ring._fade = anim
                self._ring.start_spin()
            self._follow()
            return

        if self._stage == 2:
            if time.monotonic() - self._done_at >= self.HOLD_DONE_SEC:
                self.stop()
            else:
                self._follow()
            return

        # stage 1：进程还活着吗
        if self._pid:
            from ..core import gamelaunch
            if gamelaunch.pid() != self._pid:
                self.stop()                  # 游戏自己退了（主窗口那边也会恢复）
                return
        self._win = winwatch.game_window_state(self._pid)
        target = self._target(el)
        if target >= 100.0:
            self._pct = 100.0           # 窗口真的出来了：直接满格，不再慢慢爬
        elif target > self._pct:
            self._pct = min(target, self._pct + max(1.2, (target - self._pct) * 0.45))
        self._ring.set_progress(self._pct / 100.0)

        if self._pct >= 99.6:
            self._finish()
            return

        if el >= self.SWITCH_SEC:
            self._say(f"游戏加载中... {int(self._pct)}%")
        else:
            self._follow()

    def _finish(self):
        self._stage = 2
        self._done_at = time.monotonic()
        self._pct = 100.0
        self._ring.set_progress(1.0)
        self._ring.stop_spin()
        self._say("加载完成 · 100%")
