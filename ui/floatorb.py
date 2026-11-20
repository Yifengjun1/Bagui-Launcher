# -*- coding: utf-8 -*-
"""后台悬浮球（可拖动、左键唤回主窗口、右键选页面）

图案跟软件顶部状态栏中间那个徽标**完全一致**（`branding.paint_badge`），
不再是红白精灵球 —— 三处（状态栏 / 悬浮球 / 安装向导）共用一套绘制。

第四十六轮：**右键**在球旁边弹出一个小菜单（主页 / 整合包 / 模组 / 数据包 / 光影 /
材质包），点哪个就把窗口唤回来并直接进那一页。左键行为一点没变（还是「恢复窗口」）。
菜单按球所在的屏幕边缘翻边：默认朝球左边展开，左边放不下就翻到右边，上下夹在屏幕里。
"""
from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from ..core import winacrylic
from . import theme

# 右键菜单里的页面（键与主窗 switch_page / pages 一致）
ORB_PAGES = (
    ("home", "主页"),
    ("modpack", "整合包"),
    ("mod", "模组"),
    ("datapack", "数据包"),
    ("shader", "光影"),
    ("resourcepack", "材质包"),
)

# 第一一〇轮：页面下面的两个动作（走同一条 on_pick 回调，主窗 _orb_open_page
# 认「双下划线开头」的键分流；页面键没有这种写法，不会撞名）。
# 隐藏 = 球收掉、改由托盘兜底；强制退出 = 不问游戏死活直接退进程。
ORB_ACTIONS = (
    ("__hide__", "隐藏"),
    ("__quit__", "强制退出"),
)

_ROW_QSS = (
    "QPushButton{border:none;border-radius:7px;text-align:left;"
    "padding:0 10px;font-size:12.5px;color:%s;background:transparent;}"
    "QPushButton:hover{background:rgba(47,124,246,26);color:#2b6fe0;}"
    "QPushButton:pressed{background:rgba(47,124,246,48);}"
    "QPushButton:disabled{background:transparent;color:#b9c0cb;}"
) % theme.C_TEXT


class OrbMenu(QWidget):
    """悬浮球右键菜单：白底圆角小卡片，一行一个页面。"""

    ROW_H = 32
    PAD_X, PAD_Y = 7, 7
    GAP = 10                     # 与球之间的间距

    def __init__(self, on_pick):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.on_pick = on_pick
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self.PAD_X, self.PAD_Y, self.PAD_X, self.PAD_Y)
        lay.setSpacing(1)
        self._btns = []
        for key, cn in ORB_PAGES:
            b = QPushButton(cn)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(self.ROW_H)
            b.setFont(QFont(theme.FONT, 9))
            b.setStyleSheet(_ROW_QSS)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            lay.addWidget(b)
            self._btns.append(b)
        # 第一一〇轮：分隔线 + 「隐藏 / 强制退出」两个动作行
        self._btn_quit = None
        sep = QWidget(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#e6e9ef;border:none;")
        lay.addWidget(sep)
        lay.addSpacing(4)
        for key, cn in ORB_ACTIONS:
            b = QPushButton(cn)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(self.ROW_H)
            b.setFont(QFont(theme.FONT, 9))
            b.setStyleSheet(_ROW_QSS)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            if key == "__quit__":
                # 第一一一轮：强制退出只对「游戏正在跑」可用 —— 默认置灰，
                # 每次弹出菜单前由 FloatOrb 按主窗给的回调刷新
                b.setEnabled(False)
                self._btn_quit = b
            lay.addWidget(b)
            self._btns.append(b)
        self.setFixedWidth(104)
        self.adjustSize()

    def set_quit_enabled(self, on):
        """游戏在跑 → 「强制退出」亮起；没跑 → 灰掉、点了也没反应"""
        if self._btn_quit is not None:
            self._btn_quit.setEnabled(bool(on))

    # ---------------- 外观 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor("#c9d0da"), 1))
        p.setBrush(QColor(255, 255, 255, 250))
        p.drawRoundedRect(r, 10, 10)
        p.end()

    # ---------------- 弹出位置 ----------------
    def popup_at(self, orb):
        """贴着球展开：优先左边，放不下翻右边，纵向以球为中心并夹进可用区。"""
        scr = (QGuiApplication.screenAt(orb.mapToGlobal(orb.rect().center()))
               or orb.screen() or QGuiApplication.primaryScreen())
        area = scr.availableGeometry() if scr is not None else None
        og = orb.geometry()                       # 球的全局矩形（顶层窗口）
        w, h = self.width(), self.height()
        cy = og.center().y()
        if area is not None:
            x = og.left() - w - self.GAP          # 默认：朝左展开
            if x < area.left() + 2:
                x = og.right() + self.GAP         # 左边没地方了 → 翻到右边
            if x + w > area.right() - 2:          # 两边都挤：压回屏幕内
                x = max(area.left() + 2, area.right() - w - 2)
            y = cy - h // 2
            y = max(area.top() + 2, min(y, area.bottom() - h - 2))
        else:
            x, y = og.left() - w - self.GAP, cy - h // 2
        self.move(int(x), int(y))
        first = self._btns[0] if self._btns else None
        if first is not None:
            first.setFocus()
        self.show()
        self.raise_()

    def _pick(self, key):
        # 先收起菜单，再回调：回调里可能要开窗口 / 动画，别被 Popup 抢住鼠标
        self.hide()
        if callable(self.on_pick):
            self.on_pick(key)


class FloatOrb(QWidget):
    SIZE = 56

    def __init__(self, on_restore, on_menu=None, on_can_quit=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._on_restore = on_restore
        self._on_menu = on_menu
        # 第一一一轮：「强制退出」的可用性由主窗按「游戏是否在跑」实时给
        self._on_can_quit = on_can_quit
        self._menu = None
        self._long_fired = False
        self._pop_anim = None
        # 球挪动时的回调：加载进度条 / 气泡挂在球外面（独立小窗），
        # 拖动时要跟着走（见 ui/orbring.py，第五十四轮）
        self._on_move = None

    def moveEvent(self, e):
        super().moveEvent(e)
        cb = self._on_move
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        from .branding import paint_badge
        # 拖起来（_long_fired）当按下态压小一点，悬停放大 — 和状态栏手感一致
        paint_badge(p, QRectF(self.rect()), pressed=self._long_fired,
                    hover=self.underMouse())
        p.end()

    # ---------------- 右键菜单 ----------------
    def _show_menu(self):
        if self._menu is not None and self._menu.isVisible():
            self._menu.hide()
            return
        if not callable(self._on_menu):
            return
        if self._menu is None:
            self._menu = OrbMenu(self._pick_page)
        # 第一一一轮：每次弹出前刷新「强制退出」的可用态（游戏可能刚关/刚开）
        if callable(self._on_can_quit):
            try:
                self._menu.set_quit_enabled(bool(self._on_can_quit()))
            except Exception:                            # noqa: BLE001
                self._menu.set_quit_enabled(False)
        self._menu.popup_at(self)

    def _pick_page(self, key):
        if callable(self._on_menu):
            try:
                self._on_menu(key)
            except Exception:
                pass

    def contextMenuEvent(self, e):
        # 右键：不弹系统菜单，弹自己的（菜单项见 ORB_PAGES）
        e.accept()

    # ---------------- 鼠标 ----------------
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._show_menu()
            return
        if e.button() == Qt.LeftButton:
            self._long_fired = False
            self._dragging = False
            self._manual = False
            self._press_pos = e.globalPosition().toPoint()
            self._press_local = self.pos()

    def mouseMoveEvent(self, e):
        # 超过阈值即进入拖动：优先系统级拖动，失败则手动跟手
        press = getattr(self, "_press_pos", None)
        if press is None:
            return
        if not getattr(self, "_dragging", False):
            if (e.globalPosition().toPoint() - press).manhattanLength() <= 6:
                return
            self._dragging = True
            self._long_fired = True
            self.update()
            try:
                wh = self.windowHandle()
                if wh is None or not wh.startSystemMove():
                    self._manual = True
            except Exception:
                self._manual = True
        if getattr(self, "_manual", False):
            self.move(self._press_local + e.globalPosition().toPoint() - press)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and not getattr(self, "_long_fired", False):
            self._on_restore()
        self._dragging = False
        self._manual = False
        self._press_pos = None

    def show_at(self, global_pos):
        """在指定点弹出（带缩放动画）"""
        self.move(int(global_pos.x() - self.SIZE / 2), int(global_pos.y() - self.SIZE / 2))
        self.setWindowOpacity(0.0)
        self.show()
        self._pop_anim = QPropertyAnimation(self, b"windowOpacity")
        self._pop_anim.setDuration(260)
        self._pop_anim.setStartValue(0.0)
        self._pop_anim.setEndValue(1.0)
        self._pop_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._pop_anim.start()

    def fade_out(self, cb=None):
        if self._menu is not None:
            try:
                self._menu.hide()
            except Exception:
                pass
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(200)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)

        def _hide():
            self.hide()
            if cb:
                cb()
        anim.finished.connect(_hide)
        anim.start()
        self._fade = anim  # 防 GC
