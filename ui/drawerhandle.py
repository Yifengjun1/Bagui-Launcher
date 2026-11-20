# -*- coding: utf-8 -*-
"""内容区右缘的「启动选择」抽屉把手 —— 第一百轮。

玩家开着独立启动逛整合包 / 模组·数据包 / 光影 / 材质包时，右边缘会立一根
细白竖条；点它，「选择要启动的游戏」就从这根竖条里**向左展开**。
以前得「同一个顶部页签再点一下」才开，玩家根本发现不了。

为什么自己画：工程里没有现成的 handle / rail 控件。`FrostCard` 是整张磨砂卡、
`PillButton` 是横向胶囊，都不合适。这里照它们的观感（半透明白 + 白描边 +
多层淡影 + 胶囊圆头）画一根竖的，靠 `theme` 同一套颜色。
**不挂 QGraphicsEffect**：父控件已经可能挂了 opacity，父子都挂会让子控件
整体不画（第八十五轮）。
"""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

HANDLE_W = 14          # 控件宽（含透明命中区，别做得太细点不中）
BAR_W = 5              # 真正画出来的那根白条


class DrawerHandle(QWidget):
    """一根贴右缘的细白竖条，点一下发出 `clicked`。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drawerHandle")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(HANDLE_W)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点这里选要启动的游戏")
        self._hover = False
        self._open = False

    # ---------------- 状态 ----------------
    def set_open(self, v: bool):
        v = bool(v)
        if v != self._open:
            self._open = v
            self.update()

    def is_open(self) -> bool:
        return self._open

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    # ---------------- 自绘 ----------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        h = self.height()
        if h < 24:
            p.end()
            return

        # 白条贴控件右侧，上下留 3px，两端是圆头胶囊
        x = HANDLE_W - BAR_W - 3
        r = QRectF(x + 0.5, 3.5, BAR_W, h - 7)
        rad = BAR_W / 2.0

        # 三层越来越淡的影：桌面杂色背景下这根细条也能立住
        for grow, alpha in ((4.0, 16), (2.5, 26), (1.2, 36)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 30, 44, alpha))
            p.drawRoundedRect(r.adjusted(-grow, 0, grow, 0), rad, rad)

        lit = self._hover or self._open
        p.setBrush(QColor(255, 255, 255, 240 if lit else 190))
        p.setPen(QPen(QColor(255, 255, 255, 215), 1))
        p.drawRoundedRect(r, rad, rad)

        cx, cy = r.center().x(), r.center().y()
        # 中间三道极细握把纹，暗示「这里能点」
        p.setPen(QPen(QColor(90, 102, 117, 150 if lit else 105), 1))
        for dy in (-4, 0, 4):
            p.drawLine(QPointF(cx - 1, cy + dy), QPointF(cx + 1, cy + dy))

        # 白条左边一个小箭头：收起时朝左（点它会展开），展开时朝右（点它收回去）
        ax = float(x - 4)
        p.setPen(QPen(QColor(47, 124, 246, 200 if lit else 135), 1.4))
        d = 1.7 if self._open else -1.7
        p.drawLine(QPointF(ax + d, cy - 4), QPointF(ax - d, cy))
        p.drawLine(QPointF(ax - d, cy), QPointF(ax + d, cy + 4))
        p.end()
