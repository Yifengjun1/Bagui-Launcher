# -*- coding: utf-8 -*-
"""通用 UI 组件：磨砂卡片 / 胶囊按钮 / 收放下拉 / 头像 / Toast"""
import os

from PySide6.QtCore import (Property, QEasingCurve, QEvent, QObject, QPoint,
                            QPointF, QPropertyAnimation, QRect, QRectF, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QImage, QIntValidator, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout,
                               QWidget)

from ..core.asyncio_qt import run_async
from . import theme

CURRENT_WINDOW = None  # 由 MainWindow 设置，用于磨砂取景


def _win_blur():
    return CURRENT_WINDOW.blurred_image() if CURRENT_WINDOW else None


def _in_win(w):
    """w 是否还在 CURRENT_WINDOW 的父子链里（磨砂取景前必须先确认）。

    控件被 `removeWidget → hide → deleteLater()` 换掉之后，Python 侧仍可能握着旧引用；
    这时 `mapTo(主窗, ...)` 会在 Qt 里打
    「QWidget::mapTo(): parent must be in parent hierarchy」并返回一份错误坐标
    （第 52 轮崩溃日志里连着刷了 25 行），所以先判断关系再算位置。
    """
    win = CURRENT_WINDOW
    if win is None or w is None:
        return False
    try:
        return w is win or win.isAncestorOf(w)
    except RuntimeError:            # C++ 侧已销毁
        return False


def anchor_limits(anchor=None):
    """弹层可用范围：优先「主窗口矩形」，主窗口太小才退回屏幕可用区。

    弹层（下拉面板 / 小三角菜单）如果只按屏幕夹位置，在窗口底部点开时会跑到
    窗口外面 —— 看起来就是「展开超出了窗口」。统一先认窗口。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication
    scr = None
    if anchor is not None:
        try:
            scr = QGuiApplication.screenAt(anchor)
        except Exception:
            scr = None
    scr = scr or QGuiApplication.primaryScreen()
    avail = scr.availableGeometry()
    win = CURRENT_WINDOW
    if win is not None:
        try:
            wr = QRect(win.mapToGlobal(win.rect().topLeft()), win.size())
            wr = wr.intersected(avail)
            # 窗口本身小到放不下弹层时才退回屏幕，否则一律以窗口为界
            if wr.width() >= 240 and wr.height() >= 220:
                return wr
        except Exception:
            pass
    return avail


def place_popup(limit, w, h, below_pt, above_pt, margin=8, gap=6):
    """在 limit 里摆弹层：优先锚点下方 → 放不下翻到上方 → 再放不下就夹进来。

    below_pt / above_pt 是锚点全局的「下沿左角 / 上沿左角」。
    """
    from PySide6.QtCore import QPoint
    left_min = limit.left() + margin
    left_max = max(left_min, limit.right() - w - margin + 1)
    x = max(left_min, min(below_pt.x(), left_max))
    y = below_pt.y() + gap
    if y + h > limit.bottom() - margin + 1:
        up = above_pt.y() - h - gap
        y = up if up >= limit.top() + margin else max(
            limit.top() + margin, limit.bottom() - h - margin + 1)
    return QPoint(x, y)


def _rounded_region(rect, radius):
    """圆角矩形 QRegion（用于 popup 窗口遮罩）"""
    from PySide6.QtGui import QRegion
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    return QRegion(path.toFillPolygon().toPolygon())


# ================================================================ 磨砂卡片
class FlowLayout(QLayout):
    """流式布局：放不下就换行（第一百零九轮）。

    主页「启动选项」下面那排胶囊（浏览整合包 / 打开游戏目录 / 加载器 / Java /
    游戏管理）以前是 `QHBoxLayout` + `addStretch(1)`：窗口一缩，右边直接顶出
    可见区被裁掉；窗口拉宽又是一长串。换成流式布局后按可用宽度自动折行 ——
    窄窗两行、宽窗一行，跟随玩家拖动的窗口尺寸走。

    实现按 Qt 官方的 FlowLayout 示例（`heightForWidth` + `_do_layout`），
    只做了两处适配：
      · `sizeHint()` 给最小宽（父容器在 QScrollArea 里时不会算出 0 宽）；
      · `expandingDirections` 返回空 —— 不跟相邻控件抢水平空间。
    """

    def __init__(self, parent=None, margin=0, h_spacing=8, v_spacing=8):
        super().__init__(parent)
        self._items = []
        self._h = int(h_spacing)
        self._v = int(v_spacing)
        self.setContentsMargins(margin, margin, margin, margin)

    # ---- QLayout 必需接口
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        if 0 <= i < len(self._items):
            return self._items[i]
        return None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, max(0, int(width)), 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            try:
                size = size.expandedTo(self._item_size(it))
            except Exception:                     # noqa: BLE001
                pass
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    @staticmethod
    def _item_size(it):
        """一项该占多大。

        不能只用 `it.sizeHint()`：`QWidgetItem.sizeHint()` 在 widget 还没
        显示 / polish 时会返回 (0,0)（实测 offscreen 下 5 个按钮全是 0，
        结果整个布局高度算成 0）。所以直接从 widget 取 sizeHint，并用
        minimum/maximum 夹一下 —— `setFixedSize(140,36)` 这种也能算对。
        """
        wd = it.widget() if hasattr(it, "widget") else None
        if wd is not None:
            w = wd.sizeHint()
            mn = wd.minimumSize()
            if mn.width() > 0:
                w.setWidth(max(w.width(), mn.width()))
            if mn.height() > 0:
                w.setHeight(max(w.height(), mn.height()))
            mx = wd.maximumSize()
            if 0 < mx.width() < 16777215:
                w.setWidth(min(w.width(), mx.width()))
            if 0 < mx.height() < 16777215:
                w.setHeight(min(w.height(), mx.height()))
            return w
        return it.sizeHint()

    # ---- 真正排布
    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = eff.x(), eff.y()
        line_h = 0
        for it in self._items:
            try:
                w = self._item_size(it)
            except Exception:                     # noqa: BLE001
                continue
            if x + w.width() > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y = y + line_h + self._v
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), w))
            x = x + w.width() + self._h
            line_h = max(line_h, w.height())
        return y + line_h - rect.y() + m.bottom()


class FrostCard(QWidget):
    """磨砂玻璃卡片：从主窗口模糊背景图中取景"""

    def __init__(self, parent=None, radius=16, tint=150, border=True):
        super().__init__(parent)
        self.radius = radius
        self.tint = tint
        self.border = border
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        p.setClipPath(path)
        img = _win_blur()
        if img and _in_win(self):
            pos = self.mapTo(CURRENT_WINDOW, self.rect().topLeft())
            x0 = max(0, pos.x())
            y0 = max(0, pos.y())
            x1 = min(img.width(), pos.x() + self.width())
            y1 = min(img.height(), pos.y() + self.height())
            if x1 > x0 and y1 > y0:
                p.drawImage(QRectF(x0 - pos.x(), y0 - pos.y(), x1 - x0, y1 - y0),
                            img, QRectF(x0, y0, x1 - x0, y1 - y0))
        p.fillRect(self.rect(), QColor(255, 255, 255, self.tint))
        if self.border:
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
            p.drawPath(path)


# ================================================================ 贴边浮层
class _OutsideCloser(QObject):
    """点窗外 / 按 Esc / 主窗一移动就收起浮层。

    自己接管这件事，是为了不用 `Qt.Popup`（原因见 PopupPanel 的文档）。
    """

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    def eventFilter(self, obj, ev):
        # 退出阶段事件还会进来：整段包住，出任何问题就当没看见
        try:
            return self._handle(obj, ev)
        except Exception:                                 # noqa: BLE001
            return False

    def _handle(self, obj, ev):
        p = self.panel
        t = ev.type()
        if t == QEvent.Move:
            # 主窗被拖动：浮层是独立顶层窗口，不会跟着走 → 直接收起。
            # 只认 Move，不认 Resize —— 弹出瞬间主窗常因为布局/滚动条变一下尺寸，
            # 认了 Resize 就会「刚 show 就被自己关掉」。
            if obj is p.parentWidget():
                p.close()
            return False
        if t == QEvent.MouseButtonPress:
            gp = ev.globalPosition().toPoint()
            if p.geometry().contains(gp):
                return False
            anc = getattr(p, "anchor", None)
            if anc is not None:
                try:
                    if anc.isVisible() and \
                            anc.rect().contains(anc.mapFromGlobal(gp)):
                        # 点在「展开按钮」上：交给按钮自己的 toggle 逻辑，
                        # 不然先关再开，点一下等于没反应
                        return False
                except RuntimeError:
                    pass
            p.close()
        elif t == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
            p.close()
        return False


class PopupPanel(QWidget):
    """贴在按钮旁边的浮层（磨砂卡片 + 自己管关闭）。

    **为什么不用 `Qt.Popup`**（第五十三轮实测）：
      1. 「下载游戏」那个面板用 `Qt.Popup` 时，`show()` 之后**第一次事件循环就被
         发 Close**：事件序列 `Show → ShowToParent → PolishRequest → Close → Hide`，
         10ms 内就没了 —— 现场表现就是「点『下载游戏』展不开」（连闪都没有）。
         换成 `Qt.Tool` 后同样的调用序列能稳定留在屏幕上。
         注：一个只有 QLabel 的裸 `Qt.Popup` 复现不出来，所以不是 Qt.Popup 的通病，
         而是这块面板（三个下拉 + 磨砂卡 + 「官方 / 应用」胶囊）和 popup 抓取之间
         的相互作用；`Qt.Tool` 绕开了这一整类问题。
      2. `Qt.Popup` 里放 `QComboBox` 本来就是雷：点下拉那一瞬间，下拉自己的弹出
         视图会抢走 popup 抓取，整个浮层跟着消失。加载器版本那栏正好是三个下拉。
    收起交给 `_OutsideCloser`（点窗外 / Esc / 主窗移动）。
    """

    def __init__(self, parent, anchor=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.anchor = anchor
        self._closer = None

    # ---------------- 关闭管理 ----------------
    def _install_closer(self):
        if self._closer is not None:
            return
        app = QApplication.instance()
        if app is None:
            return
        self._closer = _OutsideCloser(self)
        app.installEventFilter(self._closer)

    def _drop_closer(self):
        app = QApplication.instance()
        if app is not None and self._closer is not None:
            try:
                app.removeEventFilter(self._closer)
            except RuntimeError:
                pass
        self._closer = None

    def hideEvent(self, e):
        self._drop_closer()
        super().hideEvent(e)

    def closeEvent(self, e):
        self._drop_closer()
        super().closeEvent(e)

    # ---------------- 弹出 ----------------
    def place(self):
        """算出贴着锚点、又完全留在主窗里的位置（返回 (x, y)）"""
        anchor = self.anchor
        if anchor is None:
            return None
        base = anchor.mapToGlobal(QPoint(0, 0))
        win = anchor.window()
        wg = win.frameGeometry()
        gap = 6
        # 水平：优先跟按钮左对齐，右边放不下就往左推，最后再夹进窗口
        x = min(base.x(), wg.right() - self.width() - 8)
        x = max(x, wg.left() + 8)
        # 垂直：优先往按钮下方；下方不够就翻到上方；两边都不够就夹进窗口
        below = base.y() + anchor.height() + gap
        above = base.y() - self.height() - gap
        if below + self.height() <= wg.bottom() - 8:
            y = below
        elif above >= wg.top() + 8:
            y = above
        else:
            y = max(wg.top() + 8, wg.bottom() - self.height() - 8)
        return x, y

    def popup(self):
        self.adjustSize()
        pos = self.place()
        if pos is not None:
            self.move(*pos)
        self.show()
        self.raise_()
        self._install_closer()


# ================================================================ 省略号标签
class ElideLabel(QLabel):
    """按宽度自动打省略号的单行标签。

    直接让 QLabel 截断会把字裁成半个（「4.7 GB+」看起来像「4.7 GI」），
    名字一长也会顶到右边的按钮上；这里在 resize/show 时按实际宽度 elide。
    注意 text() 仍是 QLabel 语义（当前显示的省略文本），完整文本走 full_text()。
    """

    def __init__(self, text="", parent=None):
        super().__init__("", parent)
        self._full = str(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._apply()

    def full_text(self):
        return self._full

    def setText(self, t):
        self._full = str(t)
        self._apply()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply()

    def showEvent(self, ev):
        # 样式表里的 font-size 要等 polish 之后才生效，首次 show 再量一次才准
        super().showEvent(ev)
        self._apply()

    def _apply(self):
        fm = self.fontMetrics()
        shown = fm.elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)


# ================================================================ 图标按钮
class IconButton(QPushButton):
    def __init__(self, icon_name, size=34, icon_size=16, color=None,
                 hover_bg=None, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.icon_color = color or theme.C_TEXT
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)      # 去掉点击后的焦点框选
        self.hover_bg = hover_bg or QColor(31, 39, 51, 26)
        self.icon_sz = icon_size
        self._hover = False
        self._active = False          # 第八十八轮：可切换的「激活态」（筛选开关用）

    def set_active(self, v):
        """激活态：常驻淡蓝底 + 蓝色图标（不作为按钮的禁用态）"""
        v = bool(v)
        if v != self._active:
            self._active = v
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        # 激活态：常驻淡蓝底，让「已开启的筛选」一眼可见
        if self._active and enabled:
            p.setBrush(QColor(47, 124, 246, 46))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(self.rect()), 10, 10)
        if enabled and (self._hover or self.isDown()):
            p.setBrush(QColor(0, 0, 0, 42) if self.isDown() else self.hover_bg)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(self.rect()), 10, 10)
        color = theme.C_ACCENT if (self._active and enabled) else self.icon_color
        if self.isDown():
            color = "#151b24"
        elif not enabled:
            color = "#c2cad4"          # 禁用：箭头/图标变灰（翻页到头时用）
        theme.draw_icon(p, self.icon_name, QRectF(self.rect()).adjusted(
            (self.width() - self.icon_sz) / 2, (self.height() - self.icon_sz) / 2,
            -(self.width() - self.icon_sz) / 2, -(self.height() - self.icon_sz) / 2),
            color=color)


# ================================================================ 胶囊开关
class GlyphIcon(QWidget):
    """一个只画图标的小方块（`theme.draw_icon` 的通用外壳）。

    最初是 `importui._GIcon`（第六十九轮，文件夹标题行用）；第九十一轮提到这里，
    因为「独立游戏设置 → 概览」的五条文件夹行也要在名称左边放同款类型图标 ——
    复制一份自绘控件下去，改成两处各画一版，迟早画得不一样。

    注意：只用于纯装饰，`WA_TransparentForMouseEvents` 保证点击穿透到父行的
    「整行可点」逻辑上。
    """

    def __init__(self, name, size=15, color="#5b8f6d", w=1.7, parent=None):
        super().__init__(parent)
        self._name = name
        self._color = color
        self._w = w
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        theme.draw_icon(p, self._name, QRectF(0, 0, self.width(), self.height()),
                        color=self._color, w=self._w)
        p.end()


class PillSwitch(QWidget):
    """胶囊式开关（设置页用）：整条轨道可点，圆点滑过去。

    不用系统 QCheckBox：它的配色跟不上磨砂玻璃，而且缩到 44×24 后
    勾选框的方块形状很扎眼。这里全是自绘，只有「开/关」两种状态。
    """

    toggled = Signal(bool)

    def __init__(self, on=False, parent=None, w=46, h=24):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self._on = bool(on)
        self._pos = 1.0 if self._on else 0.0     # 0=关 1=开（做动画）
        self._anim = None
        self.setToolTip("点击切换")

    def isChecked(self):
        return self._on

    def setChecked(self, v, animate=True):
        v = bool(v)
        if v == self._on:
            return
        self._on = v
        if animate:
            a = QPropertyAnimation(self, b"knob", self)
            a.setDuration(150)
            a.setStartValue(self._pos)
            a.setEndValue(1.0 if v else 0.0)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.start()
            self._anim = a
        else:
            self._pos = 1.0 if v else 0.0
        self.update()
        self.toggled.emit(v)

    def _get_knob(self):
        return self._pos

    def _set_knob(self, v):
        self._pos = float(v)
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.isEnabled():
            return
        self.setChecked(not self._on)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = QColor(theme.C_ACCENT)
        off = QColor(150, 160, 174, 130)
        # 轨道颜色随位置过渡，滑块走到一半时颜色也走到一半
        t = max(0.0, min(1.0, self._pos))
        col = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
            int(off.alpha() + (255 - off.alpha()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - 6 - d) * t
        p.setBrush(QColor(255, 255, 255, 245))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


# ================================================================ 小三角菜单
class MiniMenu(QWidget):
    """贴在按钮下方的小菜单（独立启动 / 官方启动器两选一）。

    rows = [{"label": str, "sub": str, "checked": bool, "enabled": bool,
             "cb": callable}]
    """

    def __init__(self, rows, parent=None, width=224):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.rows = rows or []
        self.on_closed = None
        self._hover = -1
        self.setFixedWidth(width)
        self.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = FrostCard(self, radius=12, tint=246)
        outer.addWidget(self.card)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        self._items = []
        for i, row in enumerate(self.rows):
            it = _MiniMenuRow(row, i, self)
            lay.addWidget(it)
            self._items.append(it)
        self.adjustSize()

    def show_at(self, anchor):
        """贴在 anchor 下方（下方放不下就翻到上方），整体不出主窗口。

        anchor 可能已经被 `deleteLater()` / 隐藏掉（列表刷新换卡后仍握着旧引用），
        这时 `mapToGlobal` 只会打警告并给出错误坐标，菜单会飘到左上角。所以先确认
        它还活着且可见，不满足就退回鼠标位置——至少点哪儿弹哪儿。
        """
        from PySide6.QtGui import QCursor
        ok = False
        below = above = None
        try:
            ok = anchor is not None and anchor.isVisible()
            if ok:
                below = anchor.mapToGlobal(anchor.rect().bottomLeft())
                above = anchor.mapToGlobal(anchor.rect().topLeft())
        except RuntimeError:                    # C++ 侧已销毁
            ok = False
        if not ok:
            pos = QCursor.pos()
            below = above = pos
        lim = anchor_limits(below)
        self.move(place_popup(lim, self.width(), self.height(), below, above))
        self.show()
        self.raise_()

    def pick(self, index):
        self.close()
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        if row and row.get("enabled", True) and row.get("cb"):
            row["cb"]()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if self.on_closed:
            try:
                self.on_closed()
            except Exception:
                pass


class _MiniMenuRow(QWidget):
    def __init__(self, row, index, menu):
        super().__init__()
        self.row = row
        self.index = index
        self.menu = menu
        self._hover = False
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(38 if row.get("sub") else 32)
        self.setCursor(Qt.PointingHandCursor)
        if not row.get("enabled", True):
            self.setEnabled(False)
        self._style()

    def _style(self):
        if self._hover and self.isEnabled():
            self.setStyleSheet("QWidget{background:rgba(47,124,246,26);"
                               "border-radius:8px;}")
        else:
            self.setStyleSheet("QWidget{background:transparent;border-radius:8px;}")

    def enterEvent(self, e):
        self._hover = True
        self._style()

    def leaveEvent(self, e):
        self._hover = False
        self._style()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.isEnabled():
            self.menu.pick(self.index)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        row = self.row
        dim = not self.isEnabled()
        x = 8
        if row.get("checked"):
            theme.draw_icon(p, "check", QRectF(x, (self.height() - 12) / 2, 12, 12),
                            color="#2f7cf6" if not dim else "#c2cad4", w=2.0)
        x += 20
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#c2cad4") if dim else QColor(theme.C_TEXT))
        sub = str(row.get("sub") or "")
        if sub:
            p.drawText(QRectF(x, 4, self.width() - x - 6, 16),
                       Qt.AlignVCenter | Qt.AlignLeft, str(row.get("label") or ""))
            f2 = QFont(theme.FONT)
            f2.setPixelSize(10)
            p.setFont(f2)
            p.setPen(QColor("#aab4c0") if dim else QColor("#6a7482"))
            fm = p.fontMetrics()
            p.drawText(QRectF(x, 19, self.width() - x - 6, 14),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(sub, Qt.ElideRight, int(self.width() - x - 10)))
        else:
            p.drawText(QRectF(x, 0, self.width() - x - 6, self.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, str(row.get("label") or ""))


# ================================================================ 带小三角的胶囊
class SplitPill(QWidget):
    """一个胶囊里两个热区：左边主按钮，右边小三角（点它出菜单）。

    为什么不用「主按钮 + 独立的三角按钮」并排：两个胶囊贴在一起中间必然有缝，
    看起来像两个不相关的控件；这里整体画一个圆角胶囊，中间画一条半透明竖线分隔，
    才像系统里的「分离按钮」。

    · `set_arrow_visible(False)`：整条胶囊变回普通按钮（设置里关掉独立启动时的形态）
    · `set_primary_enabled(False)`：左区变灰但三角还能点（没选游戏时的形态）
    """

    primary = Signal()
    arrow = Signal()
    blocked = Signal()

    def __init__(self, text="", kind="accent", icon=None, parent=None, height=46):
        super().__init__(parent)
        self.kind = kind
        self.icon_name = icon
        self._text = text
        self._hover = 0            # 0 无 / 1 主区 / 2 三角
        self._down = 0
        self._arrow = False
        self._arrow_up = False
        self._primary_on = True
        self._zone_w = 30
        self.setFixedHeight(height)
        self.setMinimumWidth(96)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    # ---- 属性 ----
    def text(self):
        return self._text

    def setText(self, t):
        self._text = str(t)
        self.update()

    def set_arrow_visible(self, v):
        v = bool(v)
        if v == self._arrow:
            return
        self._arrow = v
        self.updateGeometry()
        self.update()

    def arrow_visible(self):
        return self._arrow

    def set_arrow_up(self, v):
        self._arrow_up = bool(v)
        self.update()

    def set_primary_enabled(self, v):
        self._primary_on = bool(v)
        self.update()

    def primary_enabled(self):
        return self._primary_on

    def sizeHint(self):
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self._text) + 44 + (20 if self.icon_name else 0)
        return QSize(max(w + (self._zone_w if self._arrow else 0), 96), self.height())

    def _zone_at(self, x):
        if self._arrow and x >= self.width() - self._zone_w:
            return 2
        return 1

    # ---- 交互 ----
    def mouseMoveEvent(self, e):
        z = self._zone_at(e.position().x())
        if z != self._hover:
            self._hover = z
            self.update()

    def leaveEvent(self, e):
        self._hover = 0
        self._down = 0
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._down = self._zone_at(e.position().x())
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        z, self._down = self._down, 0
        self.update()
        if z != self._zone_at(e.position().x()):
            return
        if z == 2:
            self.arrow.emit()
        elif self._primary_on:
            self.primary.emit()
        else:
            self.blocked.emit()

    # ---- 绘制 ----
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._down:
            r = r.adjusted(1.0, 1.2, -1.0, -1.2)
        accent = self.kind == "accent"
        if accent:
            if not self._primary_on:
                bg = QColor(176, 186, 198)          # 未选游戏：整条灰掉
            elif self._down == 1:
                bg = QColor(theme.C_ACCENT_DOWN)
            elif self._hover == 1:
                bg = QColor(theme.C_ACCENT_HOVER)
            else:
                bg = QColor(theme.C_ACCENT)
        else:
            a = 232 if self._down else (168 if self._hover == 1 else 130)
            bg = QColor(255, 255, 255, a)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)

        zone = self._zone_w if self._arrow else 0
        if self._arrow:
            # 三角区：hover / 按下时压一层暗色，和主区区分开
            if self._hover == 2 or self._down == 2:
                path = QPainterPath()
                path.addRoundedRect(r, r.height() / 2, r.height() / 2)
                p.setClipPath(path)
                p.setBrush(QColor(0, 0, 0, 42 if self._down == 2 else 26))
                p.drawRect(QRectF(r.right() - zone, r.top(), zone, r.height()))
                p.setClipping(False)
            # 灰态里分隔线也压暗，不然一条亮线显得三角还是「激活」的
            p.setPen(QPen(QColor(255, 255, 255,
                                 70 if (accent and not self._primary_on) else 90), 1))
            xz = r.right() - zone
            p.drawLine(QPointF(xz, r.top() + 9), QPointF(xz, r.bottom() - 9))

        # 文字（左区居中）
        col = QColor("#ffffff") if accent else QColor(theme.C_TEXT)
        if accent and not self._primary_on:
            col = QColor(255, 255, 255, 235)
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(accent)
        p.setFont(f)
        fm = p.fontMetrics()
        iw = 18 if self.icon_name else 0
        avail = self.width() - zone - 30 - iw
        txt = fm.elidedText(self._text, Qt.ElideRight, max(20, int(avail)))
        tw = fm.horizontalAdvance(txt)
        total = tw + (iw + 6 if iw else 0)
        x = (self.width() - zone - total) / 2
        yoff = 1 if (self._down == 1 and not accent) else 0
        if self.icon_name:
            theme.draw_icon(p, self.icon_name,
                            QRectF(x, (self.height() - 16) / 2 + yoff, 16, 16), color=col)
            x += iw + 6
        p.setPen(col)
        p.drawText(QRectF(x, yoff, tw + 2, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, txt)

        # 小三角（展开时朝上）
        if self._arrow:
            cx = r.right() - zone / 2
            cy = r.center().y() + (0 if not self._down else 1)
            s = 4.6
            tri = QPainterPath()
            if self._arrow_up:
                tri.moveTo(cx - s, cy + s * 0.55)
                tri.lineTo(cx + s, cy + s * 0.55)
                tri.lineTo(cx, cy - s * 0.65)
            else:
                tri.moveTo(cx - s, cy - s * 0.55)
                tri.lineTo(cx + s, cy - s * 0.55)
                tri.lineTo(cx, cy + s * 0.65)
            tri.closeSubpath()
            p.setPen(Qt.NoPen)
            if not accent:
                p.setBrush(QColor(theme.C_TEXT))
            elif self._primary_on:
                p.setBrush(QColor(255, 255, 255, 245))
            else:
                p.setBrush(QColor(255, 255, 255, 205))
            p.drawPath(tri)


# ================================================================ 胶囊按钮
class PillButton(QPushButton):
    clicked2 = Signal()

    def __init__(self, text, kind="ghost", icon=None, parent=None):
        super().__init__(parent)
        self.kind = kind          # ghost | accent | solid
        self.icon_name = icon
        self._text = text
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(34)
        self.setMinimumWidth(64)

    def setText(self, t):
        self._text = t
        self.update()

    def text(self):
        return self._text

    def sizeHint(self):
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self._text) + 42 + (18 if self.icon_name else 0)
        return QSize(max(w, 64), 34)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.5)          # 禁用态半透明，点击反馈更明确
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        down = self.isDown()
        hover = self.underMouse()
        if down:
            # 按下态：轻微内缩，形成明确的点击反馈
            r = r.adjusted(1.2, 1.6, -1.2, -1.6)
        if self.kind == "accent":
            g = QColor(theme.C_ACCENT_DOWN if down else
                       (theme.C_ACCENT_HOVER if hover else theme.C_ACCENT))
            p.setBrush(g)
            p.setPen(Qt.NoPen)
        else:
            # 按下：底色加深 + 边框收紧（此前 down 与 hover 差异过小，点按无反馈感）
            alpha = 232 if down else (168 if hover else 130)
            p.setBrush(QColor(255, 255, 255, alpha))
            p.setPen(QPen(QColor(31, 39, 51, 120 if down else 60), 1.4 if down else 1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        fm = self.fontMetrics()
        tw = fm.horizontalAdvance(self._text)
        iw = 16 if self.icon_name else 0
        total = tw + (iw + 6 if iw else 0)
        x = (self.width() - total) / 2
        yoff = 1 if down and self.kind != "accent" else 0   # 按下时文字下移 1px
        if self.icon_name:
            ic_col = "#ffffff" if self.kind == "accent" else theme.C_TEXT
            ic_rect = QRectF(x, (self.height() - 16) / 2 + yoff, 16, 16)
            ang = getattr(self, "_chev_angle", 0.0)
            if self.icon_name == "chev" and abs(ang) > 0.01:
                p.save()
                p.translate(ic_rect.center())
                p.rotate(ang)
                theme.draw_icon(p, self.icon_name, QRectF(-8, -8, 16, 16), color=ic_col)
                p.restore()
            else:
                theme.draw_icon(p, self.icon_name, ic_rect, color=ic_col)
            x += iw + 6
        p.setPen(QColor("#ffffff") if self.kind == "accent" else QColor(theme.C_TEXT))
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(self.kind == "accent")
        p.setFont(f)
        p.drawText(QRectF(x, yoff, tw + 2, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)


# ================================================================ 收放下拉
class DropDownMenu(QWidget):
    """弹出面板（独立窗口，纯白磨砂卡片样式）"""

    def __init__(self, sections, multi, on_change, width=260, max_h=380,
                 parent_btn=None, on_closed=None, all_label=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Popup)
        self._closed_at = 0.0
        self.on_closed = on_closed
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.sections = sections          # [(title, [(value, label, checked)])]
        self.multi = multi
        self.on_change = on_change
        self._rows = []
        self._all_row = None
        self.all_label = all_label
        self.setFixedWidth(width)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        card = FrostCard(radius=14, tint=238, parent=self)
        outer.addWidget(card)
        card.raise_()

        lay = QVBoxLayout()
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 4, 0)
        il.setSpacing(0)
        if all_label:
            # 「全部」置顶：未选任何项时默认选它（= 不加该维度过滤）
            any_checked = any(c for _t, its in sections for _v, _l, c in its)
            self._all_row = self._make_row("__all__", all_label, not any_checked)
            il.addWidget(self._all_row)
        for title, items in sections:
            if title:
                t = QLabel(title)
                t.setStyleSheet("color:#77828f;font-size:11px;font-weight:600;padding:8px 6px 4px;")
                il.addWidget(t)
            for value, label, checked in items:
                row = self._make_row(value, label, checked)
                il.addWidget(row)
        il.addStretch(1)
        scroll.setWidget(inner)
        scroll.setFixedHeight(min(max_h, 40 + sum(
            34 + (26 if t else 0) for t, its in sections for _ in its)
            + (34 if all_label else 0)))
        lay.addWidget(scroll)
        if self.multi:
            foot = QHBoxLayout()
            foot.addStretch(1)
            clear = QPushButton("清除已选")
            clear.setCursor(Qt.PointingHandCursor)
            clear.setFixedHeight(28)
            clear.setStyleSheet(
                "QPushButton{background:rgba(47,124,246,25);border:none;border-radius:8px;"
                "color:#2f7cf6;font-weight:600;padding:0 14px;}"
                "QPushButton:hover{background:rgba(47,124,246,45);}")
            clear.clicked.connect(self._clear_all)
            foot.addWidget(clear)
            lay.addLayout(foot)
        card.setLayout(lay)
        self._apply_blur()

    def _apply_blur(self):
        # 系统级模糊在部分机器上会导致 DWM 崩溃，改为纯白磨砂卡片样式
        pass

    def _make_row(self, value, label, checked):
        """选项行：悬停仅高亮，左键点击才切换选中（第一次选中、再次点击取消）"""
        row = QLabel(label)
        row.setProperty("v", value)
        row.setFixedHeight(32)
        row.setCursor(Qt.PointingHandCursor)
        row._checked = bool(checked)          # 统一用 _checked（此前 setChecked/Checked 混用导致悬停误选）

        def render(r, hover=False):
            if r._checked:
                r.setStyleSheet("QLabel{background:rgba(47,124,246,32);color:#2f7cf6;"
                                "font-size:13px;border-radius:8px;padding:0 10px;font-weight:600;}")
            elif hover:
                r.setStyleSheet("QLabel{background:rgba(31,39,51,16);color:#1f2733;"
                                "font-size:13px;border-radius:8px;padding:0 10px;}")
            else:
                r.setStyleSheet("QLabel{background:transparent;color:#1f2733;"
                                "font-size:13px;border-radius:8px;padding:0 10px;}")
        row._render = lambda hover=False, r=row: render(r, hover)
        row._render()
        self._rows.append(row)

        def enter(e, r=row):
            render(r, hover=True)             # 只改外观，不改变已选状态

        def leave(e, r=row):
            render(r, hover=False)

        def click(e, r=row):
            if e.button() != Qt.LeftButton:
                return
            if str(r.property("v") or "") == "__all__":
                # 「全部」：清空该维度所有具体选项（= 不过滤）
                for other in self._rows:
                    other._checked = (other is r)
                    other._render()
                self._notify()
                if not self.multi:
                    QTimer.singleShot(40, self.close)
                return
            r._checked = not r._checked       # 左键 toggle：选中 / 取消
            if self._all_row is not None and r._checked:
                self._all_row._checked = False
                self._all_row._render()
            if not self.multi:
                for other in self._rows:
                    if other is not r:
                        other._checked = False
                for other in self._rows:
                    other._render()
                QTimer.singleShot(40, self.close)   # 单选点完即收起
            else:
                r._render()
            self._sync_all_row()
            self._notify()

        row.enterEvent = enter
        row.leaveEvent = leave
        row.mousePressEvent = click
        return row

    def _sync_all_row(self):
        """具体选项全空时，把「全部」重新勾上（空选 == 全部）"""
        if self._all_row is None:
            return
        others = [r for r in self._rows if r is not self._all_row]
        if not any(getattr(r, "_checked", False) for r in others):
            self._all_row._checked = True
            self._all_row._render()

    def _clear_all(self):
        """清除已选：全部回到未选状态（= 回到“全部/默认”）"""
        for r in self._rows:
            r._checked = False
            r._render()
        self._sync_all_row()
        self._notify()
        QTimer.singleShot(40, self.close)

    def _notify(self):
        vals = [r.property("v") for r in self._rows if getattr(r, "_checked", False)]
        vals = [v for v in vals if v and v not in ("_loading", "__all__")]
        if self.on_change:
            self.on_change(vals)

    def showEvent(self, e):
        super().showEvent(e)
        # 圆角遮罩：即使分层窗口回退为不透明，四角也不会出现突兀直角
        from PySide6.QtGui import QRegion
        self.setMask(_rounded_region(self.rect(), 14))

    def closeEvent(self, e):
        import time
        self._closed_at = time.monotonic()
        try:
            if self.on_closed:
                self.on_closed()
        except Exception:
            pass
        super().closeEvent(e)

    def popup_below(self, btn):
        self.adjustSize()
        ok = False
        below = above = lim = None
        try:
            ok = btn is not None and btn.isVisible()
            if ok:
                below = btn.mapToGlobal(btn.rect().bottomLeft())
                above = btn.mapToGlobal(btn.rect().topLeft())
                # 以主窗口为界：窗口底部那条筛选栏点开时翻到按钮上方，而不是被夹到窗口外
                lim = anchor_limits(btn.mapToGlobal(btn.rect().center()))
        except Exception:                    # C++ 侧已销毁 / 锚点不是控件
            ok = False
        if not ok:
            # 按钮已经不在了（换页 / 列表重建后仍握着旧引用）：`mapToGlobal` 只会
            # 打警告并给出错误坐标，菜单于是飘到屏幕左上角 —— 那块白色卡片正是
            # 玩家说的「左边凭空多出一片弹窗」。退回鼠标位置，至少点哪儿弹哪儿。
            from PySide6.QtGui import QCursor
            pos = QCursor.pos()
            below = above = pos
            lim = anchor_limits(pos)
        self.move(place_popup(lim, self.width(), self.height(), below, above))
        self.show()
        self._apply_blur()


class DropDown(PillButton):
    """胶囊收放下拉：点击在按钮下方展开内容；展开期间下三角旋转成上三角"""

    def __init__(self, label, sections, multi=False, on_change=None,
                 width=260, parent=None, all_label=None):
        super().__init__(label, "ghost", "chev", parent)
        self._default_label = label
        self._sections = sections
        self._multi = multi
        self._on_change = on_change
        self._width = width
        self._all_label = all_label
        self._chev_angle = 0.0
        self._chev_anim = None
        self.menu = self._new_menu()

    def _new_menu(self):
        return DropDownMenu(self._sections, self._multi, self._on_change,
                            width=self._width, parent_btn=self,
                            all_label=self._all_label,
                            on_closed=lambda: self._anim_chev(0.0))

    # ---- 箭头旋转动画（0=下三角，180=上三角）----
    def _get_chev(self):
        return self._chev_angle

    def _set_chev(self, v):
        self._chev_angle = float(v)
        self.update()

    chev_angle = Property(float, _get_chev, _set_chev)

    def _anim_chev(self, target):
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        anim = QPropertyAnimation(self, b"chev_angle", self)
        anim.setDuration(190)
        anim.setStartValue(self._chev_angle)
        anim.setEndValue(float(target))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._chev_anim = anim   # 防 GC 回收

    def rebuild(self, sections):
        """重新构建下拉内容（异步数据到达后调用）"""
        self._sections = sections
        try:
            self.menu.close()
            self.menu.deleteLater()
        except Exception:
            pass
        self.menu = self._new_menu()

    def mousePressEvent(self, e):
        import time
        now = time.monotonic()
        # 刚由弹出层自身关闭（点了按钮/空白处）时，忽略这次点击避免"关了又开"
        if now - getattr(self.menu, "_closed_at", 0.0) < 0.35:
            return
        if self.menu.isVisible():
            self.menu.close()          # 收起 -> closeEvent 复位箭头为下三角
            return
        self.menu.popup_below(self)
        self._anim_chev(180.0)         # 展开 -> 旋转成上三角

    def set_value_label(self, text=None):
        self.setText(text or self._default_label)


# ================================================================ 翻页
class Pager(QWidget):
    """翻页控件：‹ + 可手输页码 / 总页数 + ›

    - 页码可直接改：输入后回车或失焦即跳转，越界自动钳到 [1, 总页数]；
    - 首页时 ‹ 变灰禁用，末页时 › 变灰禁用（配合「禁止翻页」的语义）；
    - 总页数由调用方按实际结果总数算好后 set_total() 告知。
    """

    changed = Signal(int)              # 发出目标页码（1 起）

    H = 34

    def __init__(self, page=1, parent=None):
        super().__init__(parent)
        self.page = max(1, int(page))
        self.total_pages = 1
        self.setFixedHeight(self.H)
        # 必须固定宽度策略：筛选行里项少的时候（数据包页）多余空间会被均摊进来，
        # 翻页控件会被拉成几百像素宽的空白胶囊。
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "Pager{background:rgba(255,255,255,150);"
            "border:1px solid rgba(31,39,51,16);border-radius:12px;}")

        h = QHBoxLayout(self)
        h.setContentsMargins(4, 3, 4, 3)
        h.setSpacing(2)

        self.btn_prev = IconButton("chev_left", 26, 14, color="#41505f")
        self.btn_prev.setToolTip("上一页")
        self.btn_prev.clicked.connect(self._prev)
        h.addWidget(self.btn_prev)

        self.ed = QLineEdit(str(self.page))
        self.ed.setFixedSize(38, 24)
        self.ed.setAlignment(Qt.AlignCenter)
        self.ed.setValidator(QIntValidator(1, 9999, self))
        # padding 必须显式清零：全局样式给 QLineEdit 设了 0 14px 内边距，
        # 38px 宽的页码框光内边距就吃掉 28px，数字会被挤成一条竖线。
        self.ed.setStyleSheet(
            "QLineEdit{background:transparent;border:none;padding:0;"
            "color:#1f2733;font-size:13px;font-weight:600;}"
            "QLineEdit:focus{background:rgba(47,124,246,26);"
            "border-radius:7px;padding:0;}")
        self.ed.returnPressed.connect(self._commit)
        self.ed.editingFinished.connect(self._commit)
        h.addWidget(self.ed)

        self.lb = QLabel("/ 1")
        self.lb.setStyleSheet("color:#7c8794;font-size:12px;")
        h.addWidget(self.lb)

        self.btn_next = IconButton("chev_right", 26, 14, color="#41505f")
        self.btn_next.setToolTip("下一页")
        self.btn_next.clicked.connect(self._next)
        h.addWidget(self.btn_next)

        self._sync()

    # ---- 对外 ----
    def set_total(self, total_hits, page_size):
        pages = max(1, (int(total_hits or 0) + page_size - 1) // page_size)
        self.total_pages = pages
        if self.page > pages:
            self.page = pages
        self._sync()

    def set_pages(self, pages):
        """直接给总页数。

        「全部」平台时每页是两个源各一页拼起来的（48 条），页数只能是两源页数
        的较大者，和「结果总数 / 每页条数」不是一回事，所以单独给入口。
        """
        self.total_pages = max(1, int(pages or 1))
        if self.page > self.total_pages:
            self.page = self.total_pages
        self._sync()

    def set_page(self, page):
        self.page = max(1, min(int(page or 1), self.total_pages))
        self._sync()

    def reset(self):
        self.page = 1
        self._sync()

    # ---- 内部 ----
    def _sync(self):
        self.lb.setText(f"/ {self.total_pages}")
        self.ed.setText(str(self.page))
        self.btn_prev.setEnabled(self.page > 1)
        self.btn_next.setEnabled(self.page < self.total_pages)
        self.btn_prev.update()
        self.btn_next.update()

    def _commit(self):
        try:
            want = int(self.ed.text() or "1")
        except ValueError:
            want = self.page
        want = max(1, min(want, self.total_pages))
        if want != self.page:
            self.page = want
            self._sync()
            self.changed.emit(self.page)
        else:
            self._sync()               # 越界输入回显成合法值

    def _prev(self):
        if self.page > 1:
            self.page -= 1
            self._sync()
            self.changed.emit(self.page)

    def _next(self):
        if self.page < self.total_pages:
            self.page += 1
            self._sync()
            self.changed.emit(self.page)


# ================================================================ 异步图片视图
class AsyncImageView(QWidget):
    def __init__(self, radius=10, placeholder="头像", parent=None):
        super().__init__(parent)
        self.radius = radius
        self.img = None
        self.placeholder = placeholder
        self.ph_sub = ""
        self._url = None

    def _alive(self):
        """C++ 对象是否仍有效（卡片可能已被 deleteLater）"""
        try:
            self.update.__self__  # noqa: B018
            return True
        except RuntimeError:
            return False

    def load(self, url, size=None):
        if url and url.startswith("file://"):
            path = url[7:]
            if path.startswith("/"):
                path = path[1:]
            self.img = QImage(path)
            if self.img.isNull():
                self.img = None
            self.update()
            return
        if url == self._url and self.img:
            return
        self._url = url
        if not url:
            self.img = None
            self.update()
            return
        from ..core.session import save_image

        def done(path):
            if not self._alive():
                return
            if path and url == self._url:
                self.img = QImage(path)
                if self.img.isNull():
                    self.img = None
                self.update()

        def err(e):
            if self._alive():
                self.img = None
                self.update()
        run_async(save_image, url, on_done=done, on_err=err)

    def setImage(self, qimage):
        self.img = qimage
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        p.setClipPath(path)
        if self.img and not self.img.isNull():
            scaled = self.img.scaled(self.width(), self.height(),
                                     Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx = (scaled.width() - self.width()) / 2
            sy = (scaled.height() - self.height()) / 2
            p.drawImage(QRectF(0, 0, self.width(), self.height()), scaled,
                        QRectF(sx, sy, self.width(), self.height()))
        else:
            # 无图占位：深色玻璃风（与磨砂界面协调）
            p.fillRect(self.rect(), QColor(18, 27, 44, 150))
            if self.placeholder:
                p.setPen(QColor(210, 220, 234, 200))
                p.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
            p.setBrush(QColor(255, 255, 255, 36))
            p.setPen(Qt.NoPen)
            # 简易人形剪影
            cx = self.width() / 2
            head_r = min(self.width(), self.height()) * 0.11
            body_top = self.height() * 0.52
            p.drawEllipse(QRectF(cx - head_r, self.height() * 0.24, head_r * 2, head_r * 2))
            p.drawRoundedRect(QRectF(cx - head_r * 1.6, body_top, head_r * 3.2,
                                     self.height() * 0.5), head_r, head_r)


# ================================================================ Toast
class Toast(QLabel):
    """软件内提示条：显示在主窗口顶部（标题栏下方），淡入 + 淡出，不遮挡操作区"""

    _current = None
    COLORS = {"info": "#2f7cf6", "success": "#2e9e5b", "error": "#d9534f"}

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._eff = None
        self._anim = None
        self.hide()

    @classmethod
    def show_toast(cls, parent, text, ms=2400, kind="info"):
        if cls._current is None:
            cls._current = cls(parent)
        t = cls._current
        t.setParent(parent)
        accent = cls.COLORS.get(kind, cls.COLORS["info"])
        t.setStyleSheet(
            "QLabel{background:rgba(24,30,42,238);color:#ffffff;font-size:13px;"
            f"border-radius:14px;padding:11px 22px;border:1px solid {accent};}}")
        t.setText(text)
        t.adjustSize()
        max_w = max(parent.width() - 60, 200)
        if t.width() > max_w:
            t.setFixedWidth(max_w)
            t.setWordWrap(True)
            t.adjustSize()
        t.move((parent.width() - t.width()) // 2, 62)
        t.show()
        t.raise_()
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        eff = QGraphicsOpacityEffect(t)
        eff.setOpacity(0.0)
        t.setGraphicsEffect(eff)
        t._eff = eff
        # 淡入
        anim = QPropertyAnimation(eff, b"opacity", t)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        t._anim = anim
        # 代数：update_text() 会让之前排的淡出回调作废，免得「正在显示下载进度
        # 时被上一轮的超时回调淡掉」（第 59 轮）
        t._gen = getattr(t, "_gen", 0) + 1
        gen = t._gen
        QTimer.singleShot(ms, lambda: t._fade_out(gen))

    @classmethod
    def update_text(cls, parent, text, ms=7000, kind="info"):
        """只换文字，不重播淡入动画。

        第 59 轮加的：启动前补库 / 补资源可能要几十秒到几分钟（新版 MC 的
        assets 有 460 MB），得让玩家看见进度在动。用 show_toast 反复调会每次
        都重播一遍淡入，闪得没法看。
        """
        t = cls._current
        if t is None or not t.isVisible():
            cls.show_toast(parent, text, ms=ms, kind=kind)
            return
        t._gen = getattr(t, "_gen", 0) + 1
        gen = t._gen
        t.setWordWrap(False)
        t.setMinimumWidth(0)
        t.setMaximumWidth(16777215)
        t.setText(text)
        t.adjustSize()
        max_w = max(parent.width() - 60, 200)
        if t.width() > max_w:
            t.setFixedWidth(max_w)
            t.setWordWrap(True)
            t.adjustSize()
        t.move((parent.width() - t.width()) // 2, 62)
        t.raise_()
        QTimer.singleShot(ms, lambda: t._fade_out(gen))

    def _fade_out(self, gen=None):
        # 代数对不上说明其间又弹了新内容，这次淡出作废
        if gen is not None and gen != getattr(self, "_gen", 0):
            return
        if self._eff is None:
            return
        anim = QPropertyAnimation(self._eff, b"opacity", self)
        anim.setDuration(360)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim  # 防 GC


class DeskToast(QLabel):
    """窗口已隐藏（收进托盘/浮球）时的提示条。

    原来这里用 QSystemTrayIcon.showMessage —— 那是 Windows 自己的通知气泡，
    样式跟软件不搭。改成软件自绘的悬浮提示：深色圆角 + 强调色描边，
    贴在屏幕右下角，鼠标穿透，2.6 秒后淡出。
    """

    _current = None

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint
                            | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._eff = None
        self._anim = None
        self.hide()

    @classmethod
    def show_desktop(cls, text, ms=2600, kind="info"):
        from PySide6.QtGui import QGuiApplication
        if cls._current is None:
            cls._current = cls()
        t = cls._current
        accent = Toast.COLORS.get(kind, Toast.COLORS["info"])
        t.setStyleSheet(
            "QLabel{background:rgba(24,30,42,240);color:#ffffff;font-size:13px;"
            f"border-radius:14px;padding:12px 22px;border:1px solid {accent};}}")
        t.setText(text)
        t.adjustSize()
        scr = QGuiApplication.primaryScreen()
        av = scr.availableGeometry()
        t.move(av.right() - t.width() - 28, av.bottom() - t.height() - 24)
        t.show()
        t.raise_()
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        eff = QGraphicsOpacityEffect(t)
        eff.setOpacity(0.0)
        t.setGraphicsEffect(eff)
        t._eff = eff
        anim = QPropertyAnimation(eff, b"opacity", t)
        anim.setDuration(200)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        t._anim = anim
        QTimer.singleShot(ms, t._fade_out)

    def _fade_out(self):
        if self._eff is None:
            return
        anim = QPropertyAnimation(self._eff, b"opacity", self)
        anim.setDuration(420)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim


class MemoryField(QWidget):
    """运行内存分配控件：大字显示 + 滑条 + 手动输入 + -Xms 勾选。

    设置页与「整合包分配内存」弹窗共用同一套外观与交互，用户看到的分配方式一致。
    """

    changed = Signal(int)

    SLIDER_QSS = (
        "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,36);border-radius:3px;}"
        "QSlider::sub-page:horizontal{background:#2f7cf6;border-radius:3px;}"
        "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
        "border-radius:8px;background:#ffffff;border:2px solid #2f7cf6;}")
    SPIN_QSS = (
        "QSpinBox{background:rgba(255,255,255,190);border:1px solid rgba(31,39,51,45);"
        "border-radius:8px;padding:0 8px;font-size:12px;color:#2a3442;}"
        "QSpinBox:focus{border:1px solid rgba(47,124,246,160);}"
        "QSpinBox::up-button,QSpinBox::down-button{width:0;border:none;}")

    def __init__(self, parent=None, mb=0, max_mb=0, total_gb=0, auto_min=True):
        super().__init__(parent)
        from ..core.installer import total_ram_gb
        total_gb = int(total_gb or total_ram_gb())
        max_mb = int(max_mb or min(65536, max(2048, (total_gb - 2) * 1024)))
        self.max_mb = max_mb
        cur = int(mb or 0)
        if cur <= 0:
            # 第六十七轮：默认值 4G 起步（跟 ram.auto_alloc 的下限对齐）
            cur = int(min(8192, max(4096, total_gb * 1024 // 2)))
        cur = max(1024, min(cur, max_mb))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.big = QLabel("")
        f = QFont(theme.FONT)
        f.setPixelSize(26)
        f.setBold(True)
        self.big.setFont(f)
        self.big.setStyleSheet("color:#2f7cf6;")
        row.addWidget(self.big)
        self.hint = QLabel(f"本机物理内存 {total_gb} GB · 建议不超过 "
                           f"{max(2048, total_gb * 1024 // 2) // 1024} GB（留空间给系统）")
        self.hint.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.hint.setWordWrap(True)
        row.addWidget(self.hint, 1)
        lay.addLayout(row)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1024, max_mb)
        self.slider.setSingleStep(256)
        self.slider.setPageStep(1024)
        self.slider.setValue(cur)
        self.slider.setStyleSheet(self.SLIDER_QSS)
        lay.addWidget(self.slider)

        inrow = QHBoxLayout()
        inrow.setSpacing(8)
        lb = QLabel("手动输入")
        lb.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;")
        inrow.addWidget(lb)
        self.spin = QSpinBox()
        self.spin.setRange(1024, max_mb)
        self.spin.setSingleStep(256)
        self.spin.setSuffix(" MB")
        self.spin.setValue(cur)
        self.spin.setFixedHeight(32)
        self.spin.setFixedWidth(140)
        self.spin.setStyleSheet(self.SPIN_QSS)
        inrow.addWidget(self.spin)
        self.gb = QLabel("")
        self.gb.setStyleSheet("color:#5a6675;font-size:11px;")
        inrow.addWidget(self.gb)
        inrow.addStretch(1)
        lay.addLayout(inrow)

        self.cb_xms = QCheckBox("同时把最小内存固定为一半（-Xms），减少启动卡顿")
        self.cb_xms.setChecked(bool(auto_min))
        self.cb_xms.setStyleSheet("color:#4c5866;font-size:12px;")
        lay.addWidget(self.cb_xms)

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self._label(cur)

    # ---- 取值 ----
    def value(self) -> int:
        return int(self.spin.value())

    def auto_min(self) -> bool:
        return bool(self.cb_xms.isChecked())

    def set_value(self, mb):
        cur = max(1024, min(int(mb or 0), self.max_mb))
        self.spin.setValue(cur)

    # ---- 内部联动 ----
    def _from_slider(self, v):
        self.spin.blockSignals(True)
        self.spin.setValue(int(v))
        self.spin.blockSignals(False)
        self._label(v)

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(v))
        self.slider.blockSignals(False)
        self._label(v)

    def _label(self, mb):
        mb = int(mb)
        self.big.setText(f"{mb / 1024:.1f} GB".replace(".0 GB", " GB"))
        self.gb.setText(f"→ 官启 -Xmx{mb}M")
        self.changed.emit(mb)


def launch_official_async(window_ref):
    """后台启动官方启动器并给出软件内提示。

    启动后需要轮询 tasklist 确认进程真的起来（最长约 10 秒），
    不能放在 UI 线程里做，否则窗口会假死。
    """
    from ..core import installer

    Toast.show_toast(window_ref, "正在启动官方启动器…", ms=1600)

    def done(res):
        ok, info = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
        if ok:
            Toast.show_toast(window_ref, f"已启动{info}", kind="success")
            # 主窗口据此自动收成小精灵，并在官启退出后恢复（见 MainWindow.on_official_launched）
            hook = getattr(window_ref, "on_official_launched", None)
            if hook:
                try:
                    hook(info)
                except Exception:
                    pass
        else:
            Toast.show_toast(window_ref, info, ms=6000, kind="error")

    def err(e):
        Toast.show_toast(window_ref, f"启动失败：{e}", ms=5200, kind="error")

    run_async(installer.launch_official_launcher, on_done=done, on_err=err)


# ================================================================ 展开式单选
class ChoiceExpander(QWidget):
    """一行「标题 + 当前值 + ▸」，点开就地列出所有选项（每个选项带一句说明）。

    为什么不用下拉弹层：这些选项每个都有一句解释（「隔离所有」＝存档与模组彻底分开），
    弹层里塞不下、也读不完；设置页本身可以滚，就地展开最直观，还免了
    QPopup 定高/越界/点外面关闭那一整套坑。

    · `options` = [(key, 名称, 说明)]，key 由调用方约定（可以是路径字符串）
    · `changed(key)` 只在玩家真的换了一项之后发；`set_value(k)` 不会发信号
    """

    changed = Signal(str)

    def __init__(self, label="", options=None, current="", parent=None, height=44):
        super().__init__(parent)
        self._options = list(options or [])
        self._value = str(current or "")
        self._open = False
        self._rows = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.head = QWidget()
        self.head.setAttribute(Qt.WA_StyledBackground, True)
        self.head.setObjectName("chHead")
        self.head.setFixedHeight(height)
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setStyleSheet(
            "#chHead{background:rgba(255,255,255,170);border-radius:10px;}"
            "#chHead:hover{background:rgba(255,255,255,225);}")
        h = QHBoxLayout(self.head)
        h.setContentsMargins(12, 4, 10, 4)
        h.setSpacing(8)
        self.lb_label = QLabel(label)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        self.lb_label.setFont(f)
        self.lb_label.setStyleSheet("background:transparent;color:#1f2733;")
        h.addWidget(self.lb_label)
        h.addStretch(1)
        self.lb_value = QLabel("")
        self.lb_value.setStyleSheet("background:transparent;color:#2f7cf6;font-size:11.5px;")
        h.addWidget(self.lb_value)
        self.arrow = QLabel("▸")
        self.arrow.setStyleSheet("background:transparent;color:#8a95a3;font-size:11px;")
        self.arrow.setFixedWidth(12)
        h.addWidget(self.arrow)
        lay.addWidget(self.head)

        self.body = QWidget()
        self.body.setAttribute(Qt.WA_StyledBackground, True)
        self.body.setObjectName("chBody")
        self.body.setStyleSheet(
            "#chBody{background:rgba(255,255,255,130);border-radius:10px;}")
        self.bl = QVBoxLayout(self.body)
        self.bl.setContentsMargins(6, 6, 6, 6)
        self.bl.setSpacing(2)
        self.body.setVisible(False)
        lay.addWidget(self.body)

        self.head.mousePressEvent = self._on_head_click     # noqa: SLF001
        self._sync()

    # ---------------- 对外 ----------------
    def value(self):
        return self._value

    def set_value(self, key, notify=False):
        key = str(key or "")
        changed = key != self._value
        self._value = key
        self._sync()
        if changed and notify:
            self.changed.emit(key)

    def set_options(self, options, current=None):
        self._options = list(options or [])
        if current is not None:
            self._value = str(current or "")
        self._rebuild()
        self._sync()

    def is_open(self):
        return self._open

    def set_open(self, on):
        self._open = bool(on)
        self.body.setVisible(self._open)
        self.arrow.setText("▾" if self._open else "▸")
        if self._open:
            self._rebuild()
        self._refit()

    def label_text(self):
        for k, n, _d in self._options:
            if str(k) == self._value:
                return n
        return ""

    # ---------------- 内部 ----------------
    def _on_head_click(self, e):
        if e.button() == Qt.LeftButton:
            self.set_open(not self._open)

    def _sync(self):
        n = self.label_text()
        self.lb_value.setText(n or ("未选择" if not self._value else self._value))
        self._refresh_rows()

    def _rebuild(self):
        while self.bl.count():
            it = self.bl.takeAt(0)
            w = it.widget()
            if w is not None:
                # 第八十二轮：先 hide。setParent(None) 会把控件变成一只顶层窗口，
                # 而 addWidget 挂父窗时排队的那个 show 一旦落到它头上，桌面上就会
                # 多一块带原生标题栏的空白小窗（详见 browse.FlowLayout._drop_rows）
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._rows = []
        for key, name, desc in self._options:
            row = QWidget()
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setObjectName("chRow")
            row.setCursor(Qt.PointingHandCursor)
            row.setFixedHeight(42 if desc else 32)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 4, 8, 4)
            rl.setSpacing(8)
            dot = QLabel("○")
            dot.setFixedWidth(12)
            dot.setStyleSheet("background:transparent;font-size:11px;color:#9aa4b0;")
            rl.addWidget(dot, 0, Qt.AlignTop if desc else Qt.AlignVCenter)
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(1)
            nm = QLabel(name)
            nm.setStyleSheet("background:transparent;font-size:11.5px;color:#2a3442;")
            col.addWidget(nm)
            if desc:
                dc = QLabel(desc)
                dc.setWordWrap(True)
                dc.setStyleSheet("background:transparent;font-size:10px;color:#7b8794;")
                col.addWidget(dc)
            rl.addLayout(col, 1)
            row.mousePressEvent = (lambda ev, k=str(key): self._pick(k, ev))  # noqa: SLF001
            self.bl.addWidget(row)
            self._rows.append((str(key), row, dot, nm))
        self._refresh_rows()

    def _refresh_rows(self):
        for key, row, dot, nm in self._rows:
            on = key == self._value
            dot.setText("●" if on else "○")
            dot.setStyleSheet("background:transparent;font-size:11px;color:%s;"
                              % ("#2f7cf6" if on else "#9aa4b0"))
            row.setStyleSheet(
                "#chRow{background:rgba(47,124,246,26);border-radius:8px;}"
                if on else "#chRow{background:transparent;border-radius:8px;}")
            f = QFont(theme.FONT)
            f.setPixelSize(11.5)
            f.setBold(on)
            nm.setFont(f)

    def _pick(self, key, ev=None):
        self.set_value(key)
        self.set_open(False)
        self.changed.emit(key)

    def _refit(self):
        """内容高度变了 → 通知外层滚动页重新对齐（否则展开的选项被裁掉）"""
        self.updateGeometry()
        w = self.parentWidget()
        while w is not None:
            fit = getattr(w, "fit", None)
            if callable(fit):
                try:
                    fit()
                except Exception:
                    pass
            w = w.parentWidget()


class ProgressBar(QWidget):
    """水平进度条（第六十八轮）。

    用来表示「配置至官方启动器」这种按条数推进的长任务。不用 QProgressBar：
    系统那套在老式描边上配绿色块，放在磨砂卡片上很突兀；这里只要一条淡底 +
    圆角蓝色填充，六行代码就够，还能跟着卡片配色走。
    """

    def __init__(self, parent=None, height=8, tint="#2f7cf6"):
        super().__init__(parent)
        self._v = 0.0
        self._tint = tint
        self.setFixedHeight(int(height))
        self.setMinimumWidth(120)

    def value(self) -> float:
        return self._v

    def set_value(self, v):
        try:
            v = max(0.0, min(1.0, float(v or 0)))
        except (TypeError, ValueError):
            v = 0.0
        if abs(v - self._v) > 0.0005:
            self._v = v
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if r.width() <= 1 or r.height() <= 1:
            p.end()
            return
        rad = r.height() / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(31, 39, 51, 26))
        p.drawRoundedRect(r, rad, rad)
        if self._v > 0:
            # 最小宽度 = 一个圆头，否则刚开始时看着像一颗小圆点
            w = max(r.height(), r.width() * self._v)
            p.setBrush(QColor(self._tint))
            p.drawRoundedRect(QRectF(r.left(), r.top(), w, r.height()), rad, rad)
        p.end()
