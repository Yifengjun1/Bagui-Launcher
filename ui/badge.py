# -*- coding: utf-8 -*-
"""行首图标：优先画对象自己的图标，没有才画加载器徽章。

游戏管理（gamemanager）和导入窗口（importui）共用这套控件：
一处调好，两个列表的观感就一致，不必各写一份「圆角裁图」的代码。

图标来源由 core.packicon 在工作线程里探好（磁盘路径 / data URI 字符串），
这里只负责解码 + 圆角裁切；读不出来（文件被删、格式不支持、data 坏了）
自动退回加载器徽章，绝不显示空方块。
"""
import base64

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from . import theme


def icon_pixmap(item, side):
    """行数据的 icon 字段 → 正方形 QPixmap；任何异常都返回 None（交给徽章兜底）。"""
    v = str(item.get("icon") or "")
    if not v:
        return None
    pm = QPixmap()
    if str(item.get("icon_kind") or "") == "data":
        b64 = v.split(",", 1)[-1].strip()
        b64 += "=" * (-len(b64) % 4)          # base64 缺 padding 会直接抛 binascii.Error
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None
        if not pm.loadFromData(raw):
            return None
    elif not pm.load(v):
        return None
    if pm.isNull() or pm.width() <= 0 or pm.height() <= 0:
        return None
    # 非正方形的图标按短边居中裁切，缩放时不会拉变形
    s = min(pm.width(), pm.height())
    pm = pm.copy((pm.width() - s) // 2, (pm.height() - s) // 2, s, s)
    return pm.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class LoaderBadge(QWidget):
    """行首图标：整合包/版本自己的图标 > 加载器徽章。"""

    def __init__(self, item, kind, size=32, parent=None):
        super().__init__(parent)
        self.key = theme.loader_badge_kind(item.get("loader"), kind)
        self.icon_src = str(item.get("icon_src") or "")
        self.pix = icon_pixmap(item, size * 3)
        self.setFixedSize(size, size)
        ld = theme.badge_label(self.key)
        if self.key == "vanilla":
            ld_txt = "原版：没有加装模组加载器"
        elif self.key == "unknown":
            ld_txt = "加载器未知（没有元数据，或不是本软件装的）"
        else:
            ld_txt = ld + " 加载器"
        self.setToolTip(f"图标：{self.icon_src}\n加载器：{ld_txt}"
                        if self.pix is not None else ld_txt)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            # 导入窗口里「已导入」的行是禁用的，文字会被 Qt 自动压暗；
            # 图标是自己画的，不手动减淡就会比文字亮一截
            p.setOpacity(0.45)
        r = QRectF(self.rect())
        if self.pix is None:
            theme.draw_loader_badge(p, self.key, r)
            return
        rad = r.width() * 0.30
        inner = r.adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(inner, rad, rad)
        p.save()
        p.setClipPath(clip)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        dpr = self.devicePixelRatioF() or 1.0
        pm = self.pix.scaled(int(round(self.width() * dpr)),
                             int(round(self.height() * dpr)),
                             Qt.KeepAspectRatioByExpanding,
                             Qt.SmoothTransformation)
        # 缩完仍是设备像素，必须回填 DPR，否则 drawPixmap 会按逻辑像素放大画出界
        pm.setDevicePixelRatio(dpr)
        p.drawPixmap(QPointF((self.width() - pm.width() / dpr) / 2,
                             (self.height() - pm.height() / dpr) / 2), pm)
        p.restore()
        # 图标底色接近白卡片时没有边界，补一圈极淡的描边
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(31, 39, 51, 46), 1))
        p.drawRoundedRect(inner, rad, rad)
