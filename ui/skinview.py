# -*- coding: utf-8 -*-
"""皮肤 3D 展示控件：加载皮肤 → 2.5D 渲染 → 浮动动画"""
import os

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..core.asyncio_qt import run_async
from . import theme
from .skin3d import fit_scale, render_skin_3d

# 内置兜底皮肤：程序化默认形象（64x64）
_FALLBACK_SKIN = None


def _fallback_skin() -> QImage:
    global _FALLBACK_SKIN
    if _FALLBACK_SKIN is not None:
        return _FALLBACK_SKIN
    img = QImage(64, 64, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setPen(Qt.NoPen)

    def r(x, y, w, h, c):
        p.fillRect(x, y, w, h, QColor(*c))
    skin, hair, eye_w, eye_b = (245, 220, 190), (74, 48, 30), (255, 255, 255), (60, 90, 160)
    shirt, shirt_d = (0, 150, 190), (0, 120, 155)
    shirt_n = (0, 168, 210)                     # 内侧（一般看不到，画齐而已）
    pants, pants_d = (63, 81, 181), (52, 67, 160)
    shoes = (55, 71, 79)
    # 头
    r(8, 8, 8, 8, skin); r(8, 8, 8, 2, hair); r(8, 10, 2, 2, hair); r(14, 10, 2, 2, hair)
    # 右脸(0,8)是镜头看到的那一面，头顶(8,0)，左脸(16,8)看不到但要画齐
    r(0, 8, 8, 8, hair); r(8, 0, 8, 8, hair); r(16, 8, 8, 8, hair)
    r(10, 12, 1, 1, eye_b); r(13, 12, 1, 1, eye_b)
    # 身体：正面 + 右侧面（第七十二轮起侧面吃的是 (16,20) 这一块）
    r(20, 20, 8, 12, shirt); r(20, 30, 8, 2, shirt_d)
    r(16, 20, 4, 12, shirt_d); r(20, 16, 8, 4, shirt); r(28, 20, 4, 12, shirt_d)
    # 右臂右腿（镜头这一侧，看到的是 40/0 那两条外侧边）
    r(44, 20, 4, 12, shirt); r(40, 20, 4, 12, shirt_d); r(48, 20, 4, 12, shirt_n)
    r(44, 16, 4, 4, shirt)
    r(44, 30, 4, 2, skin); r(40, 30, 4, 2, skin)
    r(4, 20, 4, 12, pants); r(0, 20, 4, 12, pants_d); r(4, 16, 4, 4, pants)
    r(8, 20, 4, 12, pants_d)
    r(4, 30, 4, 2, shoes); r(0, 30, 4, 2, shoes)
    # 左臂左腿（64x64 独立区）：在远侧，画齐就行
    r(36, 52, 4, 12, shirt); r(32, 52, 4, 12, shirt_d); r(36, 48, 4, 4, shirt)
    r(40, 52, 4, 12, shirt_n)
    r(36, 62, 4, 2, skin)
    r(20, 52, 4, 12, pants); r(16, 52, 4, 12, pants_d); r(20, 48, 4, 4, pants)
    r(24, 52, 4, 12, pants_d)
    r(20, 62, 4, 2, shoes)
    p.end()
    _FALLBACK_SKIN = img
    return img


def fetch_skin(name: str, uuid: str | None = None, skin_url: str | None = None) -> QImage:
    """多源获取皮肤原图（线程中调用）。返回 QImage（失败给内置默认）"""
    from ..core.session import save_image
    candidates = []
    if skin_url:
        candidates.append(skin_url)
    if uuid:
        candidates.append(f"https://crafatar.com/skins/{uuid}")
    candidates.append(f"https://minotar.net/skin/{name}")
    for u in candidates:
        p = save_image(u, timeout=20)
        if p:
            img = QImage(p)
            if not img.isNull() and img.width() >= 64:
                return img
            try:
                os.remove(p)
            except OSError:
                pass
    return _fallback_skin()


class SkinView(QWidget):
    """3D 角色展示：浮动动画 + 完整 contain 显示"""

    def __init__(self, radius=12, placeholder="", parent=None):
        super().__init__(parent)
        self.radius = radius
        self.placeholder = placeholder
        self._skin = None
        self._phase = 0.0
        self._loading = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # ---- 加载 ----
    def load_skin(self, name: str, uuid: str | None = None,
                  skin_url: str | None = None):
        if self._loading:
            return
        self._loading = True

        def done(img):
            self._loading = False
            if self._dead():
                return
            if img and not img.isNull():
                self._skin = img
                if not self._timer.isActive():
                    self._timer.start()
                self.update()

        def err(e):
            self._loading = False
        run_async(fetch_skin, name or "Steve", uuid, skin_url,
                  on_done=done, on_err=err)

    def clear(self):
        self._skin = None
        self._timer.stop()
        self.update()

    def _dead(self):
        try:
            self.update.__self__  # noqa: B018
            return False
        except RuntimeError:
            return True

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 1 / 30.0) % 1.0
        self.update()

    # ---- 绘制 ----
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        p.setClipPath(path)
        # 深色玻璃底（与磨砂界面协调）
        grad = QColor(16, 25, 42, 165)
        p.fillRect(self.rect(), grad)
        if self._skin is not None:
            dpr = max(1.0, float(self.devicePixelRatioF()))
            s = fit_scale(self.width() * dpr, self.height() * dpr)
            render = render_skin_3d(self._skin, scale=s, phase=self._phase)
            if render is not None:
                # 直接 1:1 绘制，不再二次缩放（HIDPI 下按 dpr 提高分辨率）
                render.setDevicePixelRatio(dpr)
                w = render.width() / dpr
                h = render.height() / dpr
                x = (self.width() - w) / 2
                y = (self.height() - h) / 2
                p.drawImage(QPointF(x, y), render)
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
