# -*- coding: utf-8 -*-
"""标题栏：头像/玩家名 + 导航（整合包 模组▾ [精灵球] 光影 材质包）+ 窗口控制"""
from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from ..core import winacrylic
from . import theme
from .widgets import AsyncImageView, IconButton

# 顶栏当前字色（由 TitleBar.refresh_ink 维护）。
# 为什么存一份而不是每次 paint 现算：nav 按钮一次重绘要画 5 个控件，每个都去算
# sky_state 是浪费；更重要的是「玩家名」那行是通过样式表设的，两边必须同一个值。
_INK = None


def _cur_ink():
    return _INK if _INK is not None else theme.on_sky_ink()


class NavMenu(QWidget):
    """导航二级菜单（Modrinth 风格的展开选项），带展开动画"""

    ROW_H = 32
    WIDTH = 138

    def __init__(self, anchor, items, current, on_pick):
        super().__init__(anchor, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._anchor = anchor
        self._on_pick = on_pick
        self._rows = []
        self.setFixedWidth(self.WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignTop)
        self.card = QWidget()
        self.card.setObjectName("card")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        self.card.setStyleSheet(
            "QWidget#card{background:rgba(248,250,253,247);border-radius:12px;}")
        outer.addWidget(self.card)

        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(2)
        for key, text in items:
            b = QPushButton(text)
            b.setFixedHeight(self.ROW_H)
            b.setCursor(Qt.PointingHandCursor)
            b.setCheckable(True)
            b.setChecked(key == current)
            b.setStyleSheet(
                "QPushButton{background:transparent;border:none;text-align:left;"
                "padding-left:12px;color:#2a3442;font-size:13px;border-radius:8px;}"
                "QPushButton:hover{background:rgba(47,124,246,28);}"
                "QPushButton:checked{background:rgba(47,124,246,42);"
                "color:#1a63d8;font-weight:600;}")
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            cl.addWidget(b)
            self._rows.append(b)

        total = 12 + len(items) * self.ROW_H + max(0, len(items) - 1) * 2
        self.setFixedHeight(total)
        self.card.setMaximumHeight(total)
        self._full = total

    def popup(self):
        a = self.parentWidget()
        gp = a.mapToGlobal(a.rect().bottomLeft())
        x, y = gp.x(), gp.y() + 6
        self.move(x, y - 3)
        self.setWindowOpacity(0.0)
        self.show()
        # 展开动画：轻微下滑 + 淡入（比改控件高度稳定，不会被布局裁掉）
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(150)
        self._slide.setStartValue(QPoint(x, y - 3))
        self._slide.setEndValue(QPoint(x, y))
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.start()
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(150)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _pick(self, key):
        self.close()
        if self._on_pick:
            self._on_pick(key)

    def closeEvent(self, e):
        super().closeEvent(e)
        try:
            self._anchor.update()       # 收起后三角复位
        except RuntimeError:
            pass


class NavButton(QPushButton):
    def __init__(self, text, page_key, on_nav, parent=None):
        super().__init__(parent)
        self.page_key = page_key
        self.on_nav = on_nav
        self.setText(text)
        self.setFixedHeight(34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        self.setFont(f)
        self.setMinimumWidth(72)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        active = self.on_nav(self.page_key)
        ink = _cur_ink()                # 白天黑字 / 夜里白字（见 theme.on_sky_ink）
        if active:
            p.setBrush(QColor(255, 255, 255, 185))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 12, 12)
            p.setPen(QColor("#1f2733"))
        elif self.underMouse():
            p.setBrush(QColor(255, 255, 255, 55))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 12, 12)
            p.setPen(ink)
        else:
            p.setPen(ink)
        p.setFont(self.font())
        p.drawText(r, Qt.AlignCenter, self.text())


class NavDropButton(QWidget):
    """带收放小三角的导航项：点文字切页，点三角展开二级选项"""

    TRI_W = 22          # 右侧三角热区宽度

    def __init__(self, items, on_nav, on_pick, parent=None):
        super().__init__(parent)
        self.items = list(items)              # [(key, 文案)]，第一项为默认
        self.current = self.items[0][0]
        self.on_nav = on_nav
        self.on_pick = on_pick
        self._menu = None
        self.setFixedHeight(34)
        self.setMinimumWidth(88)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        self.setFont(f)
        self.setToolTip("点击展开：模组 / 数据包")

    def label(self):
        for k, t in self.items:
            if k == self.current:
                return t
        return self.items[0][1]

    def set_current(self, key):
        if any(k == key for k, _ in self.items):
            self.current = key
            self.update()

    def _active(self):
        return any(self.on_nav(k) for k, _ in self.items)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        active = self._active()
        ink = _cur_ink()
        if active:
            p.setBrush(QColor(255, 255, 255, 185))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 12, 12)
            p.setPen(QColor("#1f2733"))
        elif self.underMouse():
            p.setBrush(QColor(255, 255, 255, 55))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 12, 12)
            p.setPen(ink)
        else:
            p.setPen(ink)
        p.setFont(self.font())
        body = QRectF(r.adjusted(10, 0, -(self.TRI_W + 2), 0))
        p.drawText(body, Qt.AlignCenter, self.label())
        # 小三角（展开时朝上）
        cx = r.right() - 12
        cy = r.center().y() + (1 if not self._is_open() else -1)
        p.setBrush(p.pen().color())
        p.setPen(Qt.NoPen)
        path = QPainterPath()
        path.moveTo(cx - 4, cy - 2)
        path.lineTo(cx + 4, cy - 2)
        path.lineTo(cx, cy + 3)
        path.closeSubpath()
        p.drawPath(path)

    def _is_open(self):
        return self._menu is not None and self._menu.isVisible()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if e.position().x() >= self.width() - self.TRI_W:
            self.open_menu()
        elif self.on_pick:
            self.on_pick(self.current)

    def open_menu(self):
        if self._menu is not None:
            try:
                self._menu.close()
                self._menu.deleteLater()
            except RuntimeError:
                pass
            self._menu = None
        m = NavMenu(self, self.items, self.current, self._picked)
        self._menu = m
        m.popup()
        self.update()

    def _picked(self, key):
        self.current = key
        self.update()
        if self.on_pick:
            self.on_pick(key)

    def hideEvent(self, e):
        super().hideEvent(e)
        try:
            if self._menu is not None:
                self._menu.close()
        except RuntimeError:
            pass


class PokeballButton(QWidget):
    """中央精灵球：单击回主页；长按 -> 收纳为悬浮球；右键 -> 打开设置（第六十八轮）"""
    clicked = Signal()
    long_pressed = Signal()
    settings_requested = Signal()
    LONG_MS = 600

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 46)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("单击返回主页 · 长按收纳为悬浮球 · 右键打开设置")
        self._timer = QTimer(self)
        self._timer.setInterval(self.LONG_MS)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire_long)
        self._long_fired = False
        self._pulse = 0.0

    def _fire_long(self):
        self._long_fired = True
        self.long_pressed.emit()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            # 右键：直接开设置。别顺手起长按计时器 —— 否则右键按久一点，
            # 松手时会同时「收纳成悬浮球」，设置窗跟着被藏起来。
            self._timer.stop()
            self._long_fired = True
            self.settings_requested.emit()
            e.accept()
            return
        if e.button() == Qt.LeftButton:
            self._long_fired = False
            self._timer.start()

    def mouseReleaseEvent(self, e):
        self._timer.stop()
        if e.button() == Qt.LeftButton and not self._long_fired:
            self.clicked.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # 中央徽标：绿圈黄花图（app/assets/logo/badge.png，白底已抠透明）
        # 与悬浮球 / 安装向导共用 branding.paint_badge，三处一模一样
        from .branding import paint_badge
        paint_badge(p, QRectF(self.rect()), hover=self.underMouse(),
                    pressed=self._timer.isActive())
        p.end()


class TitleBar(QWidget):
    minimize_clicked = Signal()
    max_restore_clicked = Signal()
    close_clicked = Signal()
    avatar_clicked = Signal()
    settings_requested = Signal()        # 精灵球右键（第六十八轮）

    def __init__(self, window, on_nav, orb_click, orb_long, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.setFixedHeight(64)
        self._on_nav = on_nav

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 8, 8)
        lay.setSpacing(8)

        # 左：头像 + 玩家名
        self.avatar = AsyncImageView(radius=10, placeholder="")
        self.avatar.setFixedSize(42, 42)
        self.avatar.setCursor(Qt.PointingHandCursor)
        self.avatar.mousePressEvent = lambda e: self.avatar_clicked.emit()
        lay.addWidget(self.avatar)
        self.name_label = QLabel("未登录")
        self.name_label.setStyleSheet(
            f"color:{theme.on_sky_ink_css()};font-size:14px;font-weight:600;")
        self.name_label.setCursor(Qt.PointingHandCursor)
        self.name_label.mousePressEvent = lambda e: self.avatar_clicked.emit()
        lay.addWidget(self.name_label)

        lay.addStretch(1)

        # 中：导航
        self.nav_wrap = QWidget()
        nav = QHBoxLayout(self.nav_wrap)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(6)
        self.nav_buttons = {}
        # 整合包
        b = NavButton("整合包", "modpack", on_nav)
        b.clicked.connect(lambda _=False, k="modpack": self.window_ref.switch_page(k))
        nav.addWidget(b)
        self.nav_buttons["modpack"] = b
        # 模组 ▾（展开：模组 / 数据包）
        self.nav_drop = NavDropButton([("mod", "模组"), ("datapack", "数据包")],
                                      on_nav, self._drop_pick)
        nav.addWidget(self.nav_drop)
        self.nav_buttons["mod"] = self.nav_drop
        self.nav_buttons["datapack"] = self.nav_drop
        self.orb = PokeballButton()
        self.orb.clicked.connect(orb_click)
        self.orb.long_pressed.connect(orb_long)
        self.orb.settings_requested.connect(self.settings_requested)
        nav.addWidget(self.orb)
        for text, key in [("光影", "shader"), ("材质包", "resourcepack")]:
            b = NavButton(text, key, on_nav)
            b.clicked.connect(lambda _=False, k=key: self.window_ref.switch_page(k))
            nav.addWidget(b)
            self.nav_buttons[key] = b
        lay.addWidget(self.nav_wrap)

        lay.addStretch(1)

        # 右：窗口控制（悬停用白色柔光，与深色背景协调；NoFocus 去掉点击后的框选）
        hover = QColor(255, 255, 255, 60)
        btn_min = IconButton("min", 34, 14, hover_bg=hover)
        btn_min.clicked.connect(self.minimize_clicked)
        btn_max = IconButton("square", 34, 13, hover_bg=hover)
        btn_max.clicked.connect(self.max_restore_clicked)
        self.btn_max = btn_max
        btn_close = IconButton("close", 34, 14, hover_bg=hover)
        btn_close.clicked.connect(self.close_clicked)
        self.btn_min = btn_min
        self.btn_close = btn_close

        class _CloseBtn(IconButton):
            def paintEvent(self, ev):
                p = QPainter(self)
                p.setRenderHint(QPainter.Antialiasing)
                if self._hover:
                    p.setBrush(QColor(232, 74, 66) if not self.isDown() else QColor(210, 60, 54))
                    p.setPen(Qt.NoPen)
                    p.drawRoundedRect(QRectF(self.rect()), 10, 10)
                color = "#ffffff" if self._hover else self.icon_color
                theme.draw_icon(p, "close", QRectF(self.rect()).adjusted(
                    (self.width() - self.icon_sz) / 2, (self.height() - self.icon_sz) / 2,
                    -(self.width() - self.icon_sz) / 2, -(self.height() - self.icon_sz) / 2),
                    color=color)
        btn_close.__class__ = _CloseBtn
        lay.addWidget(btn_min)
        lay.addWidget(btn_max)
        lay.addWidget(btn_close)
        self.refresh_ink()      # 立刻按当前天色定字色（白天黑字 / 夜里白字）

    # ---- 事件 ----
    def is_active(self, key):
        return self.window_ref.current_page == key

    def _drop_pick(self, key):
        self.nav_drop.set_current(key)
        self.window_ref.switch_page(key)

    def sync_nav(self, key):
        """外部切页后同步「模组 ▾」当前子项与高亮"""
        try:
            if key in ("mod", "datapack"):
                self.nav_drop.set_current(key)
            for b in self.nav_buttons.values():
                b.update()
        except RuntimeError:
            pass

    def _draggable_hit(self, pos):
        hit = self.childAt(pos)
        return hit in (None, self, self.nav_wrap)

    # ---- 昼夜配色 ----
    def refresh_ink(self, day=None):
        """按当前天色刷新顶栏字色（白天黑字 / 夜里白字）。

        主窗每 90 秒的 `_sky_tick` 会调它一次；天色没跨过阈值就什么都不做，
        不会每 90 秒白刷一遍样式表。
        """
        try:
            global _INK
            c = theme.on_sky_ink(day)
            key = (c.red(), c.green(), c.blue())
            if key == getattr(self, "_ink_key", None):
                return
            self._ink_key = key
            _INK = c
            self.name_label.setStyleSheet(
                f"color:{theme.on_sky_ink_css(day)};font-size:14px;font-weight:600;")
            # 窗口控制图标也跟着走：白天深色、夜里浅色，否则夜里三个按钮几乎看不见
            night = c.lightness() > 128
            hover = QColor(255, 255, 255, 60) if night else QColor(20, 26, 38, 46)
            css = theme.on_sky_ink_css(day)
            for b in (getattr(self, "btn_min", None), getattr(self, "btn_max", None),
                      getattr(self, "btn_close", None)):
                if b is None:
                    continue
                b.icon_color = css
                b.hover_bg = hover
                b.update()
            for b in self.nav_buttons.values():
                b.update()
            self.orb.update()
        except RuntimeError:
            pass

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._draggable_hit(e.position().toPoint()):
            self._drag_off = (e.globalPosition().toPoint()
                              - self.window().frameGeometry().topLeft())
            self._native_moved = False
            try:
                wh = self.window().windowHandle()
                if wh is not None:
                    self._native_moved = bool(wh.startSystemMove())
            except Exception:
                self._native_moved = False

    def mouseMoveEvent(self, e):
        # 原生拖动失败时的手动兜底
        if (getattr(self, "_drag_off", None) is not None
                and not getattr(self, "_native_moved", True)
                and e.buttons() & Qt.LeftButton):
            self.window().move(e.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, e):
        self._drag_off = None

    def mouseDoubleClickEvent(self, e):
        self.max_restore_clicked.emit()

    def set_session(self, name=None, avatar_image=None):
        """avatar_image: 已渲染好的 QImage（在线程中获取后回调）"""
        self.name_label.setText(name or "未登录")
        self.name_label.setStyleSheet(
            f"color:{theme.on_sky_ink_css()};font-size:14px;font-weight:600;")
        if avatar_image is not None:
            self.avatar.setImage(avatar_image)
        elif name is None:
            self.avatar.img = None
            self.avatar._url = None
            self.avatar.update()

    def set_maximized(self, maximized):
        self.btn_max.icon_name = "restore" if maximized else "square"
        self.btn_max.update()
