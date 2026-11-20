# -*- coding: utf-8 -*-
"""主题：配色、QSS、图标与精灵球绘制、磨砂背景生成"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPolygonF, QRadialGradient)

FONT = "Microsoft YaHei UI"

# ---- 配色（参考设计图：深蓝夜景 + 白色磨砂卡）----
C_TEXT = "#1f2733"
C_TEXT_DIM = "#5a6675"
C_TEXT_ONBLUE = "#ffffff"
C_ACCENT = "#2f7cf6"          # 官启蓝
C_ACCENT_HOVER = "#4a8df8"
C_ACCENT_DOWN = "#2565cf"
C_CARD = QColor(255, 255, 255, 96)       # 磨砂卡底
C_CARD_SOLID = QColor(255, 255, 255, 140)
C_BORDER = QColor(255, 255, 255, 130)
C_PILL = QColor(255, 255, 255, 110)
C_SEARCH = QColor(255, 255, 255, 150)
C_NAV_ACTIVE = QColor(255, 255, 255, 170)


def qss():
    return f"""
* {{
    font-family: "{FONT}";
    outline: none;
}}
QLabel {{ color: {C_TEXT}; background: transparent; }}
QLabel[dim=true] {{ color: {C_TEXT_DIM}; }}
QLabel[onblue=true] {{ color: {C_TEXT_ONBLUE}; }}

QLineEdit {{
    background: rgba(255,255,255,150);
    border: 1px solid rgba(255,255,255,140);
    border-radius: 12px;
    padding: 0 14px;
    color: {C_TEXT};
    font-size: 13px;
}}
QLineEdit:focus {{ background: rgba(255,255,255,185); border-color: {C_ACCENT}; }}
QLineEdit::placeholder {{ color: rgba(90,102,117,160); }}

QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(31,39,51,70); border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: rgba(31,39,51,110); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{
    background: rgba(31,39,51,70); border-radius: 4px; min-width: 30px;
}}

/* ⚠️ 提示框的真实类名是 QTipLabel（QToolTip 只是个静态工具类，不是 widget 类）。
   只写 QToolTip 那条选择器时**一条也匹配不上** —— 那就是「黑底灰字」的根因：
   样式没命中，Qt 回落到深色模式的调色板。两个类名都写上才保险。 */
QToolTip, QTipLabel {{
    background-color: #ffffff; color: {C_TEXT};
    border: 1px solid #c9d0da; padding: 5px 9px;
}}

QProgressDialog, QDialog {{ background: transparent; }}
"""


def apply_light_tooltip(app):
    """把提示框钉死成「白底黑字」（第四十五轮）。

    只改 QSS 不够：Windows 11 深色模式下 ToolTipBase 是近黑色、ToolTipText 是灰字，
    而**任何**自己 setStyleSheet 过的控件（我们到处都用 `#objectName{{...}}`）弹提示框时
    不再走 QApplication 的 QSS，回落到调色板 → 就是玩家看到的「黑底灰字」。
    所以 QSS 和调色板必须一起改。
    """
    try:
        from PySide6.QtGui import QPalette
    except Exception:
        return
    white, dark = QColor(255, 255, 255), QColor(C_TEXT)
    pal = app.palette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        try:
            pal.setColor(group, QPalette.ToolTipBase, white)
            pal.setColor(group, QPalette.ToolTipText, dark)
        except Exception:
            pass
    try:
        app.setPalette(pal)
    except Exception:
        pass


# ================================================================ 精灵球
def draw_pokeball(painter: QPainter, rect: QRectF, pressed=False, hover=False):
    """绘制精灵球：红顶 / 白底 / 黑带 / 中央按钮"""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    cx = rect.center().x()
    cy = rect.center().y()
    r = min(rect.width(), rect.height()) / 2 - 1
    # 阴影
    sh = QRadialGradient(QPointF(cx, cy + r * 0.15), r * 1.15)
    sh.setColorAt(0.62, QColor(0, 0, 0, 70 if pressed else 45))
    sh.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.setBrush(QBrush(sh))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(cx, cy + r * 0.18), r * 1.05, r * 1.02)
    # 顶部红色半球
    top = QLinearGradient(cx, cy - r, cx, cy)
    top.setColorAt(0, QColor(255, 92, 84) if not hover else QColor(255, 118, 106))
    top.setColorAt(1, QColor(226, 56, 52))
    painter.setBrush(QBrush(top))
    path = QPainterPath()
    path.moveTo(cx - r, cy)
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 180, -180)
    path.closeSubpath()
    painter.drawPath(path)
    # 底部白色半球
    bot = QLinearGradient(cx, cy, cx, cy + r)
    bot.setColorAt(0, QColor(250, 250, 252))
    bot.setColorAt(1, QColor(214, 218, 226))
    painter.setBrush(QBrush(bot))
    path2 = QPainterPath()
    path2.moveTo(cx - r, cy)
    path2.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 180, 180)
    path2.closeSubpath()
    painter.drawPath(path2)
    # 中间黑带
    band_h = r * 0.30
    painter.setBrush(QColor(28, 30, 36))
    painter.drawRect(QRectF(cx - r, cy - band_h / 2, 2 * r, band_h))
    # 高光
    hl = QRadialGradient(QPointF(cx - r * 0.35, cy - r * 0.45), r * 0.75)
    hl.setColorAt(0, QColor(255, 255, 255, 110))
    hl.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setBrush(QBrush(hl))
    painter.drawEllipse(QPointF(cx - r * 0.35, cy - r * 0.45), r * 0.55, r * 0.40)
    # 中央按钮
    pr = r * 0.34
    painter.setBrush(QColor(28, 30, 36))
    painter.drawEllipse(QPointF(cx, cy), pr * 1.18, pr * 1.18)
    btn = QRadialGradient(QPointF(cx - pr * 0.3, cy - pr * 0.3), pr * 1.8)
    btn.setColorAt(0, QColor(255, 255, 255))
    btn.setColorAt(0.55, QColor(235, 238, 244))
    btn.setColorAt(1, QColor(205, 210, 220))
    painter.setBrush(QBrush(btn))
    painter.drawEllipse(QPointF(cx, cy), pr, pr)
    inner = QColor(40, 44, 52, 60) if pressed else QColor(255, 255, 255, 60)
    painter.setBrush(inner)
    painter.drawEllipse(QPointF(cx, cy), pr * 0.55, pr * 0.55)
    # 轮廓
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(24, 26, 32, 160), 1.2))
    painter.drawEllipse(QPointF(cx, cy), r, r)
    painter.restore()


# ================================================================ 常用图标（矢量绘制）
def _pen(painter, color, w):
    painter.setPen(QPen(QColor(color), w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))


def draw_icon(painter: QPainter, name: str, rect: QRectF, color="#1f2733", w=1.8):
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    r = rect
    m = r.width() * 0.14
    x, y, wd, ht = r.x() + m, r.y() + m, r.width() - 2 * m, r.height() - 2 * m
    if name == "search":
        _pen(painter, color, w)
        painter.drawEllipse(QRectF(x, y, wd * 0.72, wd * 0.72))
        painter.drawLine(QPointF(x + wd * 0.72, y + wd * 0.72),
                         QPointF(x + wd, y + wd))
    elif name == "chev":
        _pen(painter, color, w)
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.18, y + ht * 0.32),
            QPointF(x + wd * 0.5, y + ht * 0.68),
            QPointF(x + wd * 0.82, y + ht * 0.32)]))
    elif name == "chev_left":
        _pen(painter, color, w)
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.66, y + ht * 0.16),
            QPointF(x + wd * 0.30, y + ht * 0.5),
            QPointF(x + wd * 0.66, y + ht * 0.84)]))
    elif name == "chev_right":
        _pen(painter, color, w)
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.34, y + ht * 0.16),
            QPointF(x + wd * 0.70, y + ht * 0.5),
            QPointF(x + wd * 0.34, y + ht * 0.84)]))
    elif name == "filter":
        _pen(painter, color, w)
        painter.drawLine(QPointF(x, y + ht * 0.25), QPointF(x + wd, y + ht * 0.25))
        painter.drawLine(QPointF(x + wd * 0.18, y + ht * 0.5), QPointF(x + wd * 0.82, y + ht * 0.5))
        painter.drawLine(QPointF(x + wd * 0.36, y + ht * 0.75), QPointF(x + wd * 0.64, y + ht * 0.75))
    elif name == "gear":
        import math
        cx, cy = r.center().x(), r.center().y()
        rr = min(wd, ht) / 2
        _pen(painter, color, w)
        for i in range(8):
            a = i * math.pi / 4
            painter.drawLine(QPointF(cx + math.cos(a) * rr * 0.72, cy + math.sin(a) * rr * 0.72),
                             QPointF(cx + math.cos(a) * rr * 1.0, cy + math.sin(a) * rr * 1.0))
        painter.drawEllipse(QPointF(cx, cy), rr * 0.68, rr * 0.68)
        painter.drawEllipse(QPointF(cx, cy), rr * 0.3, rr * 0.3)
    elif name == "download":
        _pen(painter, color, w)
        painter.drawLine(QPointF(x + wd / 2, y), QPointF(x + wd / 2, y + ht * 0.62))
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.24, y + ht * 0.38),
            QPointF(x + wd * 0.5, y + ht * 0.66),
            QPointF(x + wd * 0.76, y + ht * 0.38)]))
        painter.drawLine(QPointF(x, y + ht * 0.95), QPointF(x + wd, y + ht * 0.95))
    elif name == "refresh":
        # 通用刷新图标：开口圆环 + 缺口处的实心箭头（顺时针语义）。
        # 关键在「缺多少」和「箭头多大」——12~15px 下如果只画细箭头，
        # 缩完就只剩一个 C，会被当成电源符号。所以箭头用实心三角，尺寸按半径走。
        import math as _m
        _pen(painter, color, w)
        rr = min(wd, ht) * 0.38
        cx, cy = x + wd / 2, y + ht / 2
        box = QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr)
        # 缺口留在右上象限：圆环覆盖 90°→360°（Qt 角度逆时针为正）
        painter.drawArc(box, 90 * 16, 270 * 16)
        # 箭头压在圆环顶端，指向右侧（补上缺口，形成顺时针方向感）
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygonF([
            QPointF(cx + rr * 1.05, cy - rr),                    # 尖端
            QPointF(cx - rr * 0.15, cy - rr - rr * 0.62),        # 上翼
            QPointF(cx - rr * 0.15, cy - rr + rr * 0.62)]))      # 下翼
    elif name == "import_game":
        # 导入到本启动器：右侧半开的收纳框 + 从左推进去的粗箭头。
        # 小尺寸下线条一多就糊成一坨，所以只用「一个框 + 一个箭头」两笔，
        # 描边也跟着尺寸收细（13px 用 1.8 会直接变成实心块）。
        _pen(painter, color, min(w, max(1.1, wd * 0.105)))
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.60, y + ht * 0.15),
            QPointF(x + wd * 0.92, y + ht * 0.15),
            QPointF(x + wd * 0.92, y + ht * 0.85),
            QPointF(x + wd * 0.60, y + ht * 0.85)]))
        painter.drawLine(QPointF(x + wd * 0.05, y + ht * 0.50),
                         QPointF(x + wd * 0.50, y + ht * 0.50))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygonF([
            QPointF(x + wd * 0.60, y + ht * 0.50),
            QPointF(x + wd * 0.34, y + ht * 0.26),
            QPointF(x + wd * 0.34, y + ht * 0.74)]))
    elif name == "check":
        _pen(painter, color, w)
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.15, y + ht * 0.52),
            QPointF(x + wd * 0.42, y + ht * 0.8),
            QPointF(x + wd * 0.88, y + ht * 0.2)]))
    elif name == "close":
        _pen(painter, color, w)
        painter.drawLine(QPointF(x, y), QPointF(x + wd, y + ht))
        painter.drawLine(QPointF(x + wd, y), QPointF(x, y + ht))
    elif name == "plus":
        # 第 105 轮：游戏管理头部的「创建分类」按钮
        _pen(painter, color, w)
        painter.drawLine(QPointF(x + wd / 2, y + ht * 0.08),
                         QPointF(x + wd / 2, y + ht * 0.92))
        painter.drawLine(QPointF(x + wd * 0.08, y + ht / 2),
                         QPointF(x + wd * 0.92, y + ht / 2))
    elif name == "min":
        _pen(painter, color, w)
        painter.drawLine(QPointF(x, y + ht / 2), QPointF(x + wd, y + ht / 2))
    elif name == "square":
        _pen(painter, color, w * 0.9)
        painter.drawRect(QRectF(x, y, wd, ht))
    elif name == "restore":
        _pen(painter, color, w * 0.9)
        painter.drawRect(QRectF(x, y + ht * 0.2, wd * 0.8, ht * 0.8))
        painter.drawLine(QPointF(x + wd * 0.2, y + ht * 0.2), QPointF(x + wd * 0.2, y))
        painter.drawLine(QPointF(x + wd * 0.2, y), QPointF(x + wd, y))
        painter.drawLine(QPointF(x + wd, y), QPointF(x + wd, y + ht * 0.8))
        painter.drawLine(QPointF(x + wd, y + ht * 0.8), QPointF(x + wd * 0.8, y + ht * 0.8))
    elif name == "folder":
        _pen(painter, color, w)
        path = QPainterPath()
        path.moveTo(x, y + ht * 0.85)
        path.lineTo(x, y + ht * 0.2)
        path.lineTo(x + wd * 0.42, y + ht * 0.2)
        path.lineTo(x + wd * 0.55, y + ht * 0.35)
        path.lineTo(x + wd, y + ht * 0.35)
        path.lineTo(x + wd, y + ht * 0.85)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "play":
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(x + wd * 0.2, y), QPointF(x + wd, y + ht / 2), QPointF(x + wd * 0.2, y + ht)]))
    elif name == "grid":
        painter.setBrush(Qt.NoBrush)
        _pen(painter, color, w)
        g = wd * 0.06
        half = (wd - g) / 2
        for dx in (0, half + g):
            for dy in (0, half + g):
                painter.drawRoundedRect(QRectF(x + dx, y + dy, half, half), 2, 2)
    elif name == "trash":
        painter.setBrush(Qt.NoBrush)
        _pen(painter, color, w)
        painter.drawLine(QPointF(x + wd * 0.08, y + ht * 0.22),
                         QPointF(x + wd * 0.92, y + ht * 0.22))
        painter.drawLine(QPointF(x + wd * 0.36, y + ht * 0.22),
                         QPointF(x + wd * 0.42, y + ht * 0.1))
        painter.drawLine(QPointF(x + wd * 0.42, y + ht * 0.1),
                         QPointF(x + wd * 0.58, y + ht * 0.1))
        painter.drawLine(QPointF(x + wd * 0.58, y + ht * 0.1),
                         QPointF(x + wd * 0.64, y + ht * 0.22))
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.18, y + ht * 0.22),
            QPointF(x + wd * 0.24, y + ht * 0.92),
            QPointF(x + wd * 0.76, y + ht * 0.92),
            QPointF(x + wd * 0.82, y + ht * 0.22)]))
    elif name == "disable":
        # 禁用（关闭）：圆圈 + 对角斜杠，通用「关掉 / 隐藏」符号
        _pen(painter, color, w)
        painter.drawEllipse(QRectF(x + wd * 0.08, y + ht * 0.08,
                                   wd * 0.84, ht * 0.84))
        painter.drawLine(QPointF(x + wd * 0.22, y + ht * 0.22),
                         QPointF(x + wd * 0.78, y + ht * 0.78))
    elif name == "enable":
        # 启用（开启）：圆圈 + 实心内点，通用「开 / 显示」符号
        _pen(painter, color, w)
        painter.drawEllipse(QRectF(x + wd * 0.08, y + ht * 0.08,
                                   wd * 0.84, ht * 0.84))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(x + wd * 0.34, y + ht * 0.34,
                                   wd * 0.32, ht * 0.32))
    elif name == "mods":
        # 模组：等轴测立方体（文件方块）
        _pen(painter, color, w)
        painter.drawPolygon(QPolygonF([
            QPointF(x + wd * 0.50, y + ht * 0.10),
            QPointF(x + wd * 0.92, y + ht * 0.32),
            QPointF(x + wd * 0.92, y + ht * 0.72),
            QPointF(x + wd * 0.50, y + ht * 0.94),
            QPointF(x + wd * 0.08, y + ht * 0.72),
            QPointF(x + wd * 0.08, y + ht * 0.32)]))
        painter.drawLine(QPointF(x + wd * 0.08, y + ht * 0.32),
                         QPointF(x + wd * 0.50, y + ht * 0.53))
        painter.drawLine(QPointF(x + wd * 0.92, y + ht * 0.32),
                         QPointF(x + wd * 0.50, y + ht * 0.53))
        painter.drawLine(QPointF(x + wd * 0.50, y + ht * 0.53),
                         QPointF(x + wd * 0.50, y + ht * 0.94))
    elif name == "resourcepack":
        # 材质包：相框 + 太阳 + 山（一张图）
        _pen(painter, color, w)
        painter.drawRoundedRect(QRectF(x, y + ht * 0.12, wd, ht * 0.76), 2, 2)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(x + wd * 0.30, y + ht * 0.36),
                            wd * 0.09, wd * 0.09)
        _pen(painter, color, w)
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.12, y + ht * 0.82),
            QPointF(x + wd * 0.44, y + ht * 0.46),
            QPointF(x + wd * 0.66, y + ht * 0.82)]))
    elif name == "shader":
        # 光影：太阳（圆心 + 八道光芒）
        import math as _m
        _pen(painter, color, w)
        cx, cy = x + wd * 0.5, y + ht * 0.5
        rr = min(wd, ht) * 0.20
        painter.drawEllipse(QPointF(cx, cy), rr, rr)
        for i in range(8):
            a = i * _m.pi / 4.0
            r0, r1 = rr * 1.55, rr * 2.25
            painter.drawLine(
                QPointF(cx + _m.cos(a) * r0, cy + _m.sin(a) * r0),
                QPointF(cx + _m.cos(a) * r1, cy + _m.sin(a) * r1))
    elif name == "datapack":
        # 数据包：三层堆叠的菱形（分层）
        _pen(painter, color, w)
        for cy in (y + ht * 0.26, y + ht * 0.52, y + ht * 0.78):
            painter.drawPolygon(QPolygonF([
                QPointF(x + wd * 0.50, cy - ht * 0.11),
                QPointF(x + wd * 0.92, cy + ht * 0.01),
                QPointF(x + wd * 0.50, cy + ht * 0.13),
                QPointF(x + wd * 0.08, cy + ht * 0.01)]))
    elif name == "world":
        # 存档：地球（经纬圆 + 赤道）—— 第九十一轮，给概览「存档文件夹」那行用
        _pen(painter, color, w)
        rr = min(wd, ht) * 0.42
        cx, cy = x + wd * 0.5, y + ht * 0.5
        painter.drawEllipse(QPointF(cx, cy), rr, rr)
        painter.drawEllipse(QPointF(cx, cy), rr * 0.44, rr)
        painter.drawLine(QPointF(cx - rr, cy), QPointF(cx + rr, cy))
    elif name == "logout":
        _pen(painter, color, w)
        painter.drawRect(QRectF(x, y + ht * 0.2, wd * 0.45, ht * 0.6))
        painter.drawLine(QPointF(x + wd * 0.55, y + ht / 2), QPointF(x + wd, y + ht / 2))
        painter.drawPolyline(QPolygonF([
            QPointF(x + wd * 0.78, y + ht * 0.28), QPointF(x + wd, y + ht / 2),
            QPointF(x + wd * 0.78, y + ht * 0.72)]))
    elif name == "help":
        # 圆圈 + 问号（第五十五轮：设置右上角「?」，切换各页说明文字）。
        # 问号用字体画而不是路径：14~16px 下手画的弧线+竖+点会糊成一团墨点，
        # 字体的 hinting 在这么小的尺寸上反而干净（同样的做法见标题栏的 ×）。
        cx, cy = r.center().x(), r.center().y()
        s = min(wd, ht)
        painter.setBrush(Qt.NoBrush)
        _pen(painter, color, max(1.2, w * 0.9))
        painter.drawEllipse(QPointF(cx, cy), s * 0.5, s * 0.5)
        f = QFont(FONT)
        f.setPixelSize(max(8, int(round(s * 0.80))))
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor(color))
        painter.drawText(QRectF(cx - s * 0.5, cy - s * 0.60, s, s),
                         Qt.AlignHCenter | Qt.AlignVCenter, "?")
    elif name == "volume" or name == "volume_off":
        _pen(painter, color, w * 0.95)
        # 喇叭本体：底座方块 + 向外张开的喇叭口
        path = QPainterPath()
        path.moveTo(x + wd * 0.06, y + ht * 0.36)
        path.lineTo(x + wd * 0.28, y + ht * 0.36)
        path.lineTo(x + wd * 0.5, y + ht * 0.12)
        path.lineTo(x + wd * 0.5, y + ht * 0.88)
        path.lineTo(x + wd * 0.28, y + ht * 0.64)
        path.lineTo(x + wd * 0.06, y + ht * 0.64)
        path.closeSubpath()
        painter.drawPath(path)
        if name == "volume":
            # 两道声波（右侧半圆）
            painter.drawArc(QRectF(x + wd * 0.36, y + ht * 0.3,
                                   wd * 0.34, ht * 0.4), -60 * 16, 120 * 16)
            painter.drawArc(QRectF(x + wd * 0.44, y + ht * 0.16,
                                   wd * 0.46, ht * 0.68), -60 * 16, 120 * 16)
        else:
            # 静音：一道斜叉
            painter.drawLine(QPointF(x + wd * 0.6, y + ht * 0.26),
                             QPointF(x + wd * 0.96, y + ht * 0.74))
            painter.drawLine(QPointF(x + wd * 0.96, y + ht * 0.26),
                             QPointF(x + wd * 0.6, y + ht * 0.74))
    painter.restore()


# ================================================================ 加载器徽章（游戏管理行首图标）
# 「Forge 47.4.4」和「NeoForge 21.1.172」摆在一起时，光看文字得逐字比对才分得清；
# 给每种加载器一个固定底色 + 固定图形，扫一眼就够了。
# 图形都走「大色块 + 白描」，因为行首只有 32px，细节一律看不清。
LOADER_BADGES = {
    "vanilla":  ((92, 158, 78),   "原版"),
    "forge":    ((178, 92, 44),   "Forge"),
    "neoforge": ((58, 108, 182),  "NeoForge"),
    "fabric":   ((172, 142, 82),  "Fabric"),
    "quilt":    ((140, 96, 190),  "Quilt"),
    "unknown":  ((134, 146, 160), "加载器未知"),
}

# 铁砧轮廓（Forge 家族共用）：上宽 -> 收腰 -> 再放宽的底座
_ANVIL = [(0.04, 0.20), (0.96, 0.20), (0.86, 0.42), (0.62, 0.46),
          (0.70, 0.72), (0.90, 0.82), (0.90, 1.00), (0.10, 1.00),
          (0.10, 0.82), (0.30, 0.72), (0.38, 0.46), (0.14, 0.42)]


def loader_badge_kind(loader, kind="version"):
    """item['loader'] → 徽章键。

    版本行没有加载器就是原版；整合包行没识别出加载器只能算「未知」
    （整合包不可能是原版，标成原版会误导）。
    """
    ld = str(loader or "").strip().lower()
    if ld in LOADER_BADGES:
        return ld
    if not ld:
        return "vanilla" if kind == "version" else "unknown"
    return "unknown"


def badge_label(key):
    return LOADER_BADGES.get(key, LOADER_BADGES["unknown"])[1]


def draw_loader_badge(painter, key, rect):
    """画一枚加载器徽章：圆角方块（按加载器配色）+ 白色图形。"""
    fill, _label = LOADER_BADGES.get(key, LOADER_BADGES["unknown"])
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    r = QRectF(rect)
    rad = r.width() * 0.30
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(*fill))
    painter.drawRoundedRect(r, rad, rad)
    # 上亮下暗：纯色贴纸在白卡片上会显得「飘」，一点厚度感就能压住
    gr = QLinearGradient(r.topLeft(), r.bottomLeft())
    gr.setColorAt(0.0, QColor(255, 255, 255, 54))
    gr.setColorAt(0.55, QColor(255, 255, 255, 0))
    gr.setColorAt(1.0, QColor(0, 0, 0, 34))
    painter.setBrush(QBrush(gr))
    painter.drawRoundedRect(r, rad, rad)

    # 图形区：四周各留 22%，保证图形不会顶到圆角
    ix, iy = r.width() * 0.22, r.height() * 0.22
    x, y = r.x() + ix, r.y() + iy
    wd, ht = r.width() - 2 * ix, r.height() - 2 * iy
    white = QColor(255, 255, 255, 238)
    stroke = max(1.1, wd * 0.115)

    def P(ux, uy):
        return QPointF(x + wd * ux, y + ht * uy)

    def _white_pen(scale=1.0):
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(white, stroke * scale, Qt.SolidLine,
                            Qt.RoundCap, Qt.RoundJoin))

    def _fill():
        painter.setPen(Qt.NoPen)
        painter.setBrush(white)

    if key == "vanilla":
        # 等距方块（顶面填实 = 草方块那块绿顶）
        _fill()
        painter.drawPolygon(QPolygonF([P(0.50, 0.00), P(1.00, 0.28),
                                       P(0.50, 0.56), P(0.00, 0.28)]))
        _white_pen(0.85)
        painter.drawPolyline(QPolygonF([P(0.00, 0.28), P(0.00, 0.74),
                                        P(0.50, 1.00), P(1.00, 0.74),
                                        P(1.00, 0.28)]))
        painter.drawLine(P(0.50, 0.56), P(0.50, 1.00))
    elif key == "forge":
        _fill()
        painter.drawPolygon(QPolygonF([P(a, b) for a, b in _ANVIL]))
    elif key == "neoforge":
        # 铁砧（缩小挪到左下）+ 右上角四角星 = 「Neo」
        painter.save()
        painter.translate(x, y + ht * 0.30)
        painter.scale(0.78, 0.78)
        _fill()
        # 注意：这里必须用「相对原点的局部坐标」，translate/scale 会把绝对坐标一起缩放，
        # 直接用 P() 会把铁砧推出徽章外（试过，整枚只剩一颗星）。
        painter.drawPolygon(QPolygonF([QPointF(wd * a, ht * b)
                                       for a, b in _ANVIL]))
        painter.restore()
        cx, cy, sr = x + wd * 0.82, y + ht * 0.19, wd * 0.26
        _fill()
        painter.drawPolygon(QPolygonF([
            QPointF(cx, cy - sr), QPointF(cx + sr * 0.32, cy - sr * 0.32),
            QPointF(cx + sr, cy), QPointF(cx + sr * 0.32, cy + sr * 0.32),
            QPointF(cx, cy + sr), QPointF(cx - sr * 0.32, cy + sr * 0.32),
            QPointF(cx - sr, cy), QPointF(cx - sr * 0.32, cy - sr * 0.32)]))
    elif key == "fabric":
        # 线轴：左右两块法兰 + 中间绕线，挖两道缝出来
        bw = wd * 0.17
        _fill()
        painter.drawRoundedRect(QRectF(x, y, bw, ht), 1.2, 1.2)
        painter.drawRoundedRect(QRectF(x + wd - bw, y, bw, ht), 1.2, 1.2)
        painter.drawRect(QRectF(x + bw * 0.6, y + ht * 0.22,
                                wd - bw * 1.2, ht * 0.56))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*fill))
        for uy in (0.40, 0.55):
            painter.drawRect(QRectF(x + bw * 0.6, y + ht * uy,
                                    wd - bw * 1.2, ht * 0.055))
    elif key == "quilt":
        # 拼布：3×3 方格，中心留空，省得糊成一坨
        s = 0.27
        g = (1.0 - 3 * s) / 2
        _fill()
        for i in range(3):
            for j in range(3):
                if i == 1 and j == 1:
                    continue
                painter.drawRoundedRect(
                    QRectF(x + wd * i * (s + g), y + ht * j * (s + g),
                           wd * s, ht * s), 1.0, 1.0)
    else:
        # 问号：上钩 + 短颈 + 点
        _white_pen(1.05)
        painter.drawArc(QRectF(x + wd * 0.18, y + ht * 0.02,
                               wd * 0.64, ht * 0.56), 160 * 16, -250 * 16)
        painter.drawLine(QPointF(x + wd * 0.50, y + ht * 0.58),
                         QPointF(x + wd * 0.50, y + ht * 0.68))
        painter.setPen(Qt.NoPen)
        painter.setBrush(white)
        painter.drawEllipse(QPointF(x + wd * 0.50, y + ht * 0.90),
                            wd * 0.09, wd * 0.09)
    painter.restore()


# ================================================================ 动态天空
# 一天关键帧：(小时, 顶部色, 中部色, 底部色, 星空alpha, 白天程度0~1, 地平线基色)
# 颜色经过压暗处理，保证白色磨砂卡与深色文字始终可读
_SKY_KEYS = [
    (0.0,  (10, 16, 34),   (18, 30, 60),    (26, 44, 76),    190, 0.0, (12, 21, 38), (8, 16, 32)),
    (4.0,  (12, 20, 44),   (24, 38, 74),    (36, 58, 92),    170, 0.0, (12, 21, 38), (8, 16, 32)),
    (5.4,  (32, 30, 72),   (88, 68, 114),   (160, 110, 126), 95,  0.15, (24, 30, 44), (14, 20, 34)),
    (6.6,  (80, 92, 148),  (214, 146, 118), (246, 188, 148), 18,  0.55, (40, 62, 52), (24, 44, 36)),
    (8.0,  (66, 124, 190), (120, 170, 222), (178, 210, 238), 0,   0.9,  (44, 82, 58), (28, 58, 40)),
    (12.0, (54, 120, 196), (100, 164, 226), (168, 206, 240), 0,   1.0,  (46, 88, 60), (30, 62, 42)),
    (16.0, (60, 122, 194), (112, 168, 224), (184, 208, 232), 0,   0.95, (44, 82, 58), (28, 58, 40)),
    (18.2, (86, 88, 146),  (224, 144, 108), (248, 192, 148), 12,  0.55, (40, 60, 50), (24, 42, 34)),
    (19.4, (44, 40, 88),   (112, 72, 108),  (188, 116, 106), 72,  0.18, (24, 28, 44), (14, 20, 34)),
    (20.8, (18, 24, 54),   (34, 46, 86),    (54, 74, 110),   145, 0.0,  (12, 21, 38), (8, 16, 32)),
    (24.0, (10, 16, 34),   (18, 30, 60),    (26, 44, 76),    190, 0.0,  (12, 21, 38), (8, 16, 32)),
]


def _mix(c1, c2, t):
    """两色线性插值。入参既可以是 (r,g,b) 元组，也可以是已算出来的 QColor。"""
    a = (c1.red(), c1.green(), c1.blue()) if isinstance(c1, QColor) else c1
    b = (c2.red(), c2.green(), c2.blue()) if isinstance(c2, QColor) else c2
    return QColor(int(a[0] + (b[0] - a[0]) * t),
                  int(a[1] + (b[1] - a[1]) * t),
                  int(a[2] + (b[2] - a[2]) * t))


def sky_state(now=None):
    """按当前本地时间插值出天空状态（平滑过渡，无需整点跳变）"""
    import datetime
    import math
    if now is None:
        now = datetime.datetime.now()
    hour = now.hour + now.minute / 60.0 + now.second / 3600.0
    cur = _SKY_KEYS[-1]
    nxt = _SKY_KEYS[-1]
    for i in range(len(_SKY_KEYS) - 1):
        if _SKY_KEYS[i][0] <= hour <= _SKY_KEYS[i + 1][0]:
            cur, nxt = _SKY_KEYS[i], _SKY_KEYS[i + 1]
            break
    span = max(nxt[0] - cur[0], 1e-6)
    t = (hour - cur[0]) / span
    ts = (1 - math.cos(t * math.pi)) / 2          # 余弦平滑
    bodyname = "sun" if 6.2 < hour < 18.8 else "moon"
    # 天体轨迹：白天/夜晚各自从左侧升到右侧
    if bodyname == "sun":
        bt = (hour - 6.2) / 12.6
    else:
        bh = hour + 24 if hour < 6.2 else hour
        bt = ((bh - 18.8) % 24) / 11.4
    bt = min(max(bt, 0.0), 1.0)
    import math as _m
    return {
        "top": _mix(cur[1], nxt[1], ts),
        "mid": _mix(cur[2], nxt[2], ts),
        "bottom": _mix(cur[3], nxt[3], ts),
        "star": int(cur[4] + (nxt[4] - cur[4]) * ts),
        "day": cur[5] + (nxt[5] - cur[5]) * ts,
        "ground": _mix(cur[6], nxt[6], ts),
        "ground2": _mix(cur[7], nxt[7], ts),
        "body": bodyname,
        "body_x": 0.10 + 0.80 * bt,
        "body_y": 0.34 - 0.20 * _m.sin(_m.pi * bt),
    }


def on_sky_ink(day=None, dim=False):
    """顶部状态栏的字色：白天黑字、夜里白字（第四十五轮）。

    背景要么是照片（`wallpaper._grade` 按同一个 `day` 压暗/提亮）、要么是程序化天空，
    两种模式下 `sky_state()["day"]` 都能代表「那一片到底有多亮」，所以字色跟着它走。
    0.34 以下纯白、0.66 以上纯黑，中间（黎明/黄昏）线性过渡，不会突然跳一下。
    """
    if day is None:
        try:
            day = sky_state()["day"]
        except Exception:
            day = 0.0
    d = max(0.0, min(1.0, float(day)))
    t = (d - 0.34) / 0.32
    light, dark = QColor("#ffffff"), QColor("#1f2733")
    if t <= 0:
        c = QColor(light)
    elif t >= 1:
        c = QColor(dark)
    else:
        c = _mix(light, dark, t)
    if dim:
        c.setAlpha(190)
    return c


def on_sky_ink_css(day=None, dim=False):
    c = on_sky_ink(day, dim)
    return f"#{c.red():02x}{c.green():02x}{c.blue():02x}"


def _shade(c: QColor, d: int):
    """方块明度微扰（d 为 -100~100 的百分数增量），用来做 MC 那种方块噪点"""
    return c.lighter(int(100 + d)) if d >= 0 else c.darker(int(100 - d))


def _longji_terraces(painter: QPainter, w: float, h: float, st: dict, seed=20240):
    """地面：广西桂林龙胜「龙脊梯田」的方块化剪影。

    为什么用矩形画：MC 的观感来自「一切都是方块」，所以这里关掉抗锯齿、
    只画整数像素矩形，并按方块做明度微扰（像贴图的方块噪点）。
    颜色全部由 sky_state 的昼夜参数插值 —— 白天金绿稻田、黄昏暖橙、
    夜里深青，不会和天空色打架。

    结构（由远及近）：方块喀斯特峰林 → 层叠梯田（田面/田埂/水田/稻穗）
    → MC 方块树 → 晨雾。
    """
    import math
    import random
    rng = random.Random(seed)
    day = float(st["day"])
    horizon = h * 0.665

    # ---------- 调色板（随昼夜插值）----------
    karst_far = _mix(_mix(st["ground"], st["bottom"], 0.58), (255, 255, 255), 0.06)
    karst_near = _shade(_mix(st["ground"], st["bottom"], 0.34), -16)
    # 混合比例全部乘 day：夜里如果还按固定比例掺金色，稻田会自己发光，
    # 比天上的月亮还亮 —— 这是夜景最容易翻车的地方。
    rice_lit = _mix(st["ground2"], (216, 202, 132), 0.06 + 0.72 * day)  # 受光稻田（金绿）
    rice_mid = _mix(st["ground2"], (168, 176, 98), 0.05 + 0.55 * day)   # 中间调
    wall = _mix(st["ground"], (72, 64, 44), 0.20 + 0.34 * day)          # 田埂土壁（压暗）
    water = _mix(st["ground2"], st["bottom"], 0.26)                      # 水田镜面（别太亮）
    trunk = _mix(st["ground"], (84, 62, 40), 0.5)                        # 树干
    leaf = _mix(st["ground2"], (64, 124, 60), 0.06 + 0.52 * day)         # 树叶

    painter.setRenderHint(QPainter.Antialiasing, False)
    painter.setPen(Qt.NoPen)

    # ---------- 远景：两层方块喀斯特峰林（桂林那种陡峰）----------
    def peaks(color, top_lo, top_hi, w_lo, w_hi, seg_h, step_ratio):
        px = -90.0
        while px < w + 90:
            bw = rng.uniform(w_lo, w_hi)
            top = horizon - h * rng.uniform(top_lo, top_hi)
            seg = max(6, int(round(h * seg_h)))
            y = horizon + seg
            ratio = 1.0
            while y > top:
                c = _shade(color, -int((horizon - y) / max(horizon - top, 1) * 10))
                painter.setBrush(c)
                painter.drawRect(QRectF(int(px + bw * (1 - ratio) / 2), int(y - seg),
                                        max(3, int(bw * ratio)), seg + 1))
                y -= seg
                ratio = max(0.30, ratio - rng.uniform(0.055, 0.085))
            px += bw * step_ratio

    peaks(karst_far, 0.045, 0.135, 52, 96, 0.014, 0.62)     # 远：淡、矮
    peaks(karst_near, 0.022, 0.115, 28, 58, 0.017, 0.80)    # 近：深、参差

    # ---------- 山脚：一片起伏的暗色山体 ----------
    # 峰林画完再铺一层，上沿做成缓坡（不是平直的一条线），下沿落到画面底部。
    # 梯田会盖住它的大部分，只在山跟田交界处露一条，充当「山脚缓坡」。
    bx = -16
    while bx < w + 16:
        bw = rng.choice([20, 26, 34])
        foot = horizon - h * 0.012 + math.sin(bx / 330.0) * h * 0.014 \
            - (bx - w * 0.5) * 0.028
        painter.setBrush(_shade(karst_near, rng.randint(-4, 4)))
        painter.drawRect(QRectF(bx, foot, bw, h - foot + 6))
        bx += bw

    # ---------- 中景→近景：层叠梯田 ----------
    # 层与层的垂直间距固定（band），每层厚度接近 band 但略小，让相邻层之间留出
    # 一条细细的土缝 —— 那就是「田埂」，一眼能看出一级一级的层次。
    levels = 9
    bottom = h * 1.02
    # 每级的厚度按等比递增（近大远小）。固定厚度正是「斜条纹地毯」的成因 ——
    # 真实的梯田越靠近镜头，一级比一级高，间距是发散的。
    _total = bottom - horizon
    _r = 1.20
    thick = [_total * (_r - 1) / (_r ** levels - 1) * _r ** i for i in range(levels)]
    edges = [horizon + sum(thick[:i]) for i in range(levels + 1)]
    band = max(thick)
    # 稻田色块：真梯田是不同熟期的稻子拼成的色块，单一颜色就会像化纤条纹
    # 注意第 4 个色块原来是固定 0.55 混向一个常量绿 —— 白天没问题，
    # 夜里就成了一条自己发光的绿带，这里同样乘 day。
    patch = [rice_lit, rice_mid, _shade(rice_mid, -7),
             _mix(rice_lit, (128, 146, 72), 0.10 + 0.45 * day)]

    def level_params(i):
        t = i / (levels - 1)
        th = thick[i]
        face = max(3, int(round(th * 0.62)))
        wallh = max(3, int(round(th * 0.38)))
        return dict(t=t, y_base=edges[i + 1], bw=max(7, int(round(10 + 20 * t))),
                    face=face, wallh=wallh, dim=(-7 if i % 2 else 0),
                    amp=h * (0.014 + 0.030 * t), x0=w * (0.52 + 0.012 * i),
                    slope=0.075 + 0.075 * t)

    def contour(p, bx):
        """第 i 级的等高线（y 值）。倾斜 + 正弦起伏 + 抛物弯曲 ——
        只有正弦会画成平行波浪纹，加倾斜才有「绕着山包一级级下来」的坡地感。"""
        wave = (0.5 - 0.5 * math.cos(bx / (250.0 - 110.0 * p["t"]) + p["t"] * 7.0)) * p["amp"]
        bend = ((bx - p["x0"]) / max(w, 1)) ** 2 * h * (0.022 + 0.040 * p["t"])
        tilt = -(bx - w * 0.5) * p["slope"]
        return p["y_base"] + wave + bend + tilt

    # ---------- 山体打底 ----------
    # 梯田一级一级的，层与层之间必然有缝；没有这层底就会漏出天空色，
    # 看起来像「横条浮在空中」。底面的上沿跟着最上面那一级的等高线走。
    soil = _shade(wall, -20)
    p0 = level_params(0)
    bx = -16
    while bx < w + 16:
        bw = rng.choice([18, 24, 30, 38])
        top = contour(p0, bx) - p0["face"] - p0["wallh"] - 2
        painter.setBrush(_shade(soil, rng.randint(-6, 6)))
        painter.drawRect(QRectF(bx, top, bw, h - top + 4))
        bx += bw

    for i in range(levels):
        p = level_params(i)
        t, bw, face, wallh = p["t"], p["bw"], p["face"], p["wallh"]
        dim, level_c = p["dim"], patch[(i * 3 + 1) % len(patch)]
        bx = -bw
        while bx < w + bw:
            wh = max(3, int(round(wallh * rng.uniform(0.82, 1.14))))  # 埂高不等才自然
            top = int(round(contour(p, bx) - wh - face))
            # 田面
            painter.setBrush(_shade(level_c, dim + rng.randint(-4, 4)))
            painter.drawRect(QRectF(bx, top, bw, face))
            # 水田：田面换成映着天空的镜面（龙脊灌水期的招牌画面）
            if rng.random() < 0.04:
                painter.setBrush(_shade(water, rng.randint(-5, 8)))
                painter.drawRect(QRectF(bx, top, bw, face))
            # 稻穗：田面顶端稀稀拉拉的短竖条
            if face >= 5 and rng.random() < 0.30:
                rh = max(2, face // 3)
                painter.setBrush(_shade(level_c, -12))
                painter.drawRect(QRectF(int(bx + bw * 0.34), top, max(2, int(bw * 0.18)), rh))
            # 田埂前壁：土色；顶上一道被照亮的埂沿，底下压暗，立体感靠这三段
            painter.setBrush(_shade(wall, dim + rng.randint(-6, 6)))
            painter.drawRect(QRectF(bx, top + face, bw, wh))
            ridge = max(1, int(round(wh * 0.30)))
            painter.setBrush(_shade(wall, dim + 9))
            painter.drawRect(QRectF(bx, top + face, bw, ridge))
            painter.setBrush(_shade(wall, dim - 14))
            painter.drawRect(QRectF(bx, top + face + wh - max(1, wh // 3),
                                    bw, max(1, wh // 3)))
            bx += bw

    # ---------- 坡顶树线：最高一级田背后的灌木/竹林 ----------
    # 顺带把「山」和「田」之间那条交界线打散，不然像一堵墙根。
    bx = -8
    while bx < w + 8:
        if rng.random() < 0.62:
            bx_c = int(bx)
            base_y = int(round(contour(p0, bx_c))) - p0["face"] // 2
            for _ in range(rng.randint(1, 3)):
                cw = rng.choice([3, 4, 5, 6])
                ch = rng.choice([3, 4, 6])
                painter.setBrush(_shade(leaf, -18 + rng.randint(-6, 10)))
                painter.drawRect(QRectF(bx_c + rng.randint(-3, 4),
                                        base_y - ch, cw, ch))
        bx += rng.randint(9, 20)

    # ---------- 近景方块树（MC 橡树：1 格树干 + 分层树冠）----------
    for fx, scale in ((0.10, 1.25), (0.60, 1.05), (0.92, 0.85)):
        b = max(6, int(round(9 * scale)))
        tx = int(w * fx)
        ty = int(h - band * (0.55 + 0.5 * scale))
        painter.setBrush(_shade(trunk, rng.randint(-5, 5)))
        painter.drawRect(QRectF(tx, ty - b * 3, b, b * 3))
        for k, wide in enumerate((5, 5, 3)):
            painter.setBrush(_shade(leaf, rng.randint(-8, 8)))
            painter.drawRect(QRectF(tx - b * (wide - 1) / 2,
                                    ty - b * 3 - b * (k + 1), b * wide, b))

    # ---------- 晨雾：山腰一条薄薄的方块雾带（白天才有）----------
    mist_a = int(max(0.0, 9 + 30 * day - 14 * abs(day - 0.55)))
    if mist_a > 2:
        y = horizon + h * 0.012
        bx = -20
        while bx < w + 20:
            bw = rng.choice([26, 34, 42])
            painter.setBrush(QColor(255, 255, 255, mist_a + rng.randint(-4, 4)))
            painter.drawRect(QRectF(bx, y + rng.randint(-2, 2), bw, max(3, int(h * 0.012))))
            bx += bw


def paint_background(painter: QPainter, rect: QRectF, now=None):
    """动态天空背景：天空色随现实时间渐变，含日月轨迹、云层漂移与方块地平线"""
    import math
    import random
    import time
    w, h = rect.width(), rect.height()
    st = sky_state(now)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, False)

    # ---- 天空渐变（随时间三段插值）----
    sky = QLinearGradient(0, 0, 0, h)
    sky.setColorAt(0.0, st["top"])
    sky.setColorAt(0.5, st["mid"])
    sky.setColorAt(1.0, st["bottom"])
    painter.fillRect(rect, QBrush(sky))

    # ---- 星星（夜间，微弱闪烁）----
    if st["star"] > 4:
        rng = random.Random(42)
        tsec = time.time()
        painter.setPen(Qt.NoPen)
        for i in range(110):
            x = rng.uniform(0, w)
            y = rng.uniform(0, h * 0.68)
            s = rng.choice([1.2, 1.6, 2.2])
            tw = 0.62 + 0.38 * math.sin(tsec * 0.5 + i * 2.1)
            a = max(0, min(255, int(st["star"] * tw)))
            painter.setBrush(QColor(255, 255, 255, a))
            painter.drawRect(QRectF(x, y, s, s))

    # ---- 日/月 ----
    painter.setRenderHint(QPainter.Antialiasing, True)
    bx, by = w * st["body_x"], h * st["body_y"]
    if st["body"] == "sun":
        g = QRadialGradient(QPointF(bx, by), 150)
        g.setColorAt(0, QColor(255, 246, 220, 235))
        g.setColorAt(0.25, QColor(255, 232, 180, 110))
        g.setColorAt(1, QColor(255, 232, 180, 0))
        painter.setBrush(QBrush(g))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(bx, by), 150, 150)
        painter.setBrush(QColor(255, 248, 226))
        painter.drawEllipse(QPointF(bx, by), 30, 30)
    else:
        g = QRadialGradient(QPointF(bx, by), 120)
        g.setColorAt(0, QColor(255, 250, 230, 225))
        g.setColorAt(0.25, QColor(255, 245, 210, 115))
        g.setColorAt(1, QColor(255, 245, 210, 0))
        painter.setBrush(QBrush(g))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(bx, by), 120, 120)
        painter.setBrush(QColor(250, 246, 226))
        painter.drawEllipse(QPointF(bx, by), 32, 32)

    # ---- 云层（按真实时间缓慢漂移）----
    drift = time.time() * 0.8
    clouds = [(0.14, 0.20, 90, 26), (0.55, 0.13, 120, 30), (0.80, 0.30, 80, 22)]
    cloud_alpha = int(28 + 52 * st["day"])
    for base_x, cy, cw, ch in clouds:
        cx = (base_x * (w + 260) + drift) % (w + 260) - 130
        painter.setBrush(QColor(255, 255, 255, cloud_alpha))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(cx, h * cy, cw, ch), ch / 2, ch / 2)
        painter.drawRoundedRect(QRectF(cx + cw * 0.18, h * cy - ch * 0.55, cw * 0.62, ch * 1.1),
                                ch / 2, ch / 2)

    # ---- 地面：龙脊梯田（方块化；天空部分保持原样）----
    painter.setRenderHint(QPainter.Antialiasing, False)
    _longji_terraces(painter, w, h, st)
    painter.restore()
