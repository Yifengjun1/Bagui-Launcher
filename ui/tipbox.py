# -*- coding: utf-8 -*-
"""自绘提示框（白底黑字，全应用统一）。

**为什么不直接用系统 QToolTip**：
Qt 的提示框真实类名是 `QTipLabel`（`QToolTip` 只是个静态工具类，不是 widget 类），
而它继承自 `QLabel` —— 我们全局样式表里的 `QLabel { background: transparent; }`
会命中它，于是提示框变成**透明**，露出背后的深色天空；文字又跟着 `QLabel` 的深色走，
玩家看到的就是「黑底灰字」。Windows 深色模式下更糟：调色板本身就是黑的。

所以干脆自己画：圆角白底、深色字、细边框，跟随光标、按屏幕夹取，
任何控件（主页按钮 / Java / 游戏管理 / 弹层里的图标）都同一套观感。

拦截方式：在 QApplication 上装事件过滤器，吃掉 `QEvent.ToolTip`，改画自己的。
"""
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from . import theme


class _Tip(QWidget):
    PAD_X, PAD_Y = 11, 8
    RADIUS = 8
    MAX_W = 340            # 单行最长宽度，超了自动折行
    GAP_X, GAP_Y = 14, 20  # 相对光标的位置
    LINGER_MS = 15000      # 兜底自动消失（正常是鼠标移开就收）

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self._font = QFont(theme.FONT)
        self._font.setPixelSize(12)
        self._lines = []
        self._last = ("", None)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    # ---------------- 文本折行 ----------------
    def _wrap(self, text):
        fm = QFontMetrics(self._font)
        out = []
        for raw in str(text).split("\n"):
            raw = raw.rstrip()
            if not raw:
                out.append("")
                continue
            cur = ""
            for ch in raw:
                if cur and fm.horizontalAdvance(cur + ch) > self.MAX_W:
                    out.append(cur)
                    cur = ch
                else:
                    cur += ch
            out.append(cur)
        return out or [""]

    # ---------------- 显示 ----------------
    def show_tip(self, text, gpos: QPoint, owner=None):
        text = str(text or "")
        if not text:
            self.hide()
            return
        if self._last[0] == text and self._last[1] == owner and self.isVisible():
            self._timer.start(self.LINGER_MS)
            return
        self._lines = self._wrap(text)
        fm = QFontMetrics(self._font)
        w = max((fm.horizontalAdvance(ln) for ln in self._lines), default=0) + 2 * self.PAD_X
        h = len(self._lines) * fm.lineSpacing() + 2 * self.PAD_Y
        w = max(w, 60)
        h = max(h, 26)

        scr = QGuiApplication.screenAt(gpos) or QGuiApplication.primaryScreen()
        area = scr.availableGeometry() if scr is not None else None
        x, y = gpos.x() + self.GAP_X, gpos.y() + self.GAP_Y
        if area is not None:
            if x + w > area.right() - 2:
                x = gpos.x() - w - 6
            if y + h > area.bottom() - 2:
                y = gpos.y() - h - 8
            x = max(area.left() + 2, min(x, area.right() - w - 2))
            y = max(area.top() + 2, min(y, area.bottom() - h - 2))
        self._last = (text, owner)
        self.setGeometry(x, y, w, h)
        self.show()
        self.raise_()
        self._timer.start(self.LINGER_MS)
        self.update()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._last = ("", None)
        self._timer.stop()

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor("#c9d0da"), 1))
        p.setBrush(QColor(255, 255, 255, 248))
        p.drawRoundedRect(r, self.RADIUS, self.RADIUS)
        p.setPen(QColor(theme.C_TEXT))
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        y = self.PAD_Y + fm.ascent()
        for ln in self._lines:
            p.drawText(QPointF(self.PAD_X, y), ln)
            y += fm.lineSpacing()


class _TipFilter(QObject):
    """吃掉全应用的 ToolTip 事件，改画 _Tip；顺手在恰当的时候收起来。"""

    HIDE_ON = (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel,
               QEvent.KeyPress, QEvent.WindowDeactivate, QEvent.Hide,
               QEvent.FocusOut)

    def __init__(self, tip: _Tip):
        super().__init__()
        self.tip = tip

    def eventFilter(self, obj, ev):
        # 退出阶段（Qt 拆对象、解释器收尾）事件还会进来：一次异常就会递归报错，
        # 所以整段包起来，出任何问题就当没看见。
        try:
            return self._handle(obj, ev)
        except Exception:
            return False

    def _handle(self, obj, ev):
        t = ev.type()
        if t == QEvent.ToolTip:
            try:
                txt = obj.toolTip() if isinstance(obj, QWidget) else ""
                vis = obj.isVisible() if isinstance(obj, QWidget) else False
            except Exception:
                txt, vis = "", False
            if txt and vis:
                self.tip.show_tip(txt, ev.globalPos(), obj)
                return True          # 吞掉：系统那个黑底灰字的不要再弹
            return False
        if t in self.HIDE_ON and self.tip.isVisible():
            try:
                self.tip.hide()
            except Exception:
                pass
        return False


_TIP = None


def install(app: QApplication):
    """挂上自绘提示框（main / 安装向导各自调一次，幂等）"""
    global _TIP
    if _TIP is not None:
        return _TIP
    _TIP = _Tip()
    filt = _TipFilter(_TIP)
    filt.setParent(app)
    app.installEventFilter(filt)
    app._mc_tip = _TIP          # 防 GC
    app._mc_tip_filter = filt
    return _TIP
