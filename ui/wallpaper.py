# -*- coding: utf-8 -*-
"""背景壁纸：照片幻灯片 + 与现实时间联动的天空调色。

为什么这么做（设计说明，改之前先看这段）：

- 背景图是现成的画，不能为了「日出日落」重画一遍。所以按 ``theme.sky_state()``
  对照片做**调色**：整体乘算压暗（夜里变深蓝）、天空区 SoftLight 染成当前天色、
  地平线附近加暖光、夜里叠星星与月亮辉光 —— 照片的天空因此仍跟着现实时间走。
- 幻灯片淡入淡出时**先叠图、后统一调色**：调色只跑一遍，过渡过程不会闪色，
  也不用每帧对两张图各调一次色。
- 缩放结果按窗口尺寸缓存（最近几张），换图只在切换那一下解码一次。
"""
import datetime
import math
import random
import time
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                           QRadialGradient)

from ..core import assets
from ..core.config import CONFIG
from . import theme

COUNT = 9
NAMES = tuple("bg%02d.jpg" % i for i in range(1, COUNT + 1))
SKY_H = 0.62        # 照片里「天空」占的高度比例（染色区域）
FADE_SECS = 0.9     # 淡入淡出时长（秒）

_src = OrderedDict()        # name -> 原图 QImage
_scaled = OrderedDict()     # (name, w, h) -> cover 裁剪后的 QImage
_thumbs = OrderedDict()     # (name, w, h) -> 缩略图（设置页用，别挤掉窗口大图）
# 调色结果缓存（第 52 轮）：`_grade` 每帧要在整幅图上叠 6 层全屏合成 + 110 颗星，
# 实测 1080p 单帧 112ms、900p 80ms、720p 52ms，而淡入定时器是 33ms —— 一帧都来不及，
# 主线程全程卡在 QPainter 的 C++ 里（PySide 调 C++ 会放 GIL），工作线程就在这缝隙里
# 抢到 GIL 触发 GC 去回收 PySide 临时对象 → access violation 落在 GC 那个线程上。
# 所以调色只在「图 / 尺寸 / 天色」变化时算一次，paint 里只 drawImage。
_graded = OrderedDict()     # (name, w, h, 天色指纹) -> 已调色的 QImage
_SRC_MAX = 2
_SCALED_MAX = 2
_GRADED_MAX = 4             # 淡入时新旧两张 + 跨天色边界各一份，4 张才不抖
# 天色指纹的时间粒度（秒）：星星的呼吸感靠它，太细缓存全失效、太粗天色跳变
_STATE_STEP = 20


# ---------------------------------------------------------------- 图像读取
def available():
    """实际存在的背景图文件名（打包漏带时也不会炸，直接退回程序化天空）"""
    return [n for n in NAMES if assets.asset_exists("bg", n)]


def source(name):
    if name in _src:
        _src.move_to_end(name)
        return _src[name]
    img = QImage(assets.asset("bg", name))
    if img.isNull():
        return None
    _src[name] = img
    while len(_src) > _SRC_MAX:
        _src.popitem(last=False)
    return img


def scaled(name, w, h):
    """按窗口尺寸做 cover 裁剪（居中），结果进 LRU 缓存"""
    w, h = max(int(w), 1), max(int(h), 1)
    key = (name, w, h)
    if key in _scaled:
        _scaled.move_to_end(key)
        return _scaled[key]
    src = source(name)
    if src is None:
        return None
    im = src.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    if im.width() > w or im.height() > h:
        im = im.copy((im.width() - w) // 2, (im.height() - h) // 2, w, h)
    _scaled[key] = im
    while len(_scaled) > _SCALED_MAX:
        _scaled.popitem(last=False)
    return im


def thumb(name, w, h):
    """设置页的小预览图（单独缓存，避免把窗口大图挤出去）"""
    key = (name, int(w), int(h))
    if key in _thumbs:
        return _thumbs[key]
    src = source(name)
    if src is None:
        return None
    im = src.scaled(int(w), int(h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    if im.width() > w or im.height() > h:
        im = im.copy((im.width() - int(w)) // 2, (im.height() - int(h)) // 2,
                     int(w), int(h))
    _thumbs[key] = im
    return im


# ---------------------------------------------------------------- 时间因子
def _hour(now=None) -> float:
    if now is None:
        now = datetime.datetime.now()
    return now.hour + now.minute / 60.0 + now.second / 3600.0


def warm_factor(now=None) -> float:
    """日出/日落强度（0~1）：离 6:30 / 18:20 越近越暖"""
    h = _hour(now)
    d = min(abs(h - 6.5), abs(h - 18.33))
    return max(0.0, min(1.0, 1.0 - d / 1.8))


def _a(c, alpha: int) -> QColor:
    """取色但换 alpha（渐变里用它做「渐隐到无」）"""
    col = QColor(c)
    col.setAlpha(max(0, min(255, int(alpha))))
    return col


# ---------------------------------------------------------------- 幻灯片状态
class Wallpaper:
    """当前展示哪张图 + 淡入进度。

    只负责「哪张图、渐变到几成」和「怎么画」，定时器由主窗管。
    """

    def __init__(self):
        self.names = available()
        self.enabled = bool(CONFIG.get("slideshow", True))
        self.secs = int(CONFIG.get("slide_secs") or 15)
        self.pick = int(CONFIG.get("bg_pick") or 0)
        self.index = self.pick if self.names else 0
        self.prev = -1
        self.fade = 1.0

    # ---- 状态 ----
    def active(self) -> bool:
        return bool(self.names)

    def count(self) -> int:
        return len(self.names)

    def current_name(self):
        if not self.names:
            return ""
        return self.names[self.index % len(self.names)]

    def sync(self):
        """重新读设置：开关、秒数、手动选的那张"""
        self.enabled = bool(CONFIG.get("slideshow", True))
        self.secs = int(CONFIG.get("slide_secs") or 15)
        self.pick = int(CONFIG.get("bg_pick") or 0)
        if not self.names:
            self.index = 0
        elif not self.enabled:
            self.index = self.pick % len(self.names)
        elif not (0 <= self.index < len(self.names)):
            self.index = 0
        self.prev = -1
        self.fade = 1.0

    def jump(self, i, w=0, h=0):
        """直接切到第 i 张（不做淡入），用于手动选图 / 关掉幻灯片"""
        if not self.names:
            return
        self.index = int(i) % len(self.names)
        self.prev = -1
        self.fade = 1.0
        if w and h:
            graded(self.current_name(), w, h)   # 预生成调色图，绘制时只剩 drawImage

    def advance(self, w=0, h=0) -> bool:
        """切下一张并开始淡入；返回是否真的切了"""
        if not self.names or len(self.names) < 2:
            return False
        self.prev = self.index % len(self.names)
        self.index = (self.index + 1) % len(self.names)
        self.fade = 0.0
        if w and h:
            graded(self.current_name(), w, h)
        return True

    def step(self, dt: float) -> bool:
        """推进淡入；返回 True 表示还在动（主窗据此决定是否继续 33ms 刷新）"""
        if self.fade >= 1.0:
            return False
        self.fade = min(1.0, self.fade + max(dt, 0.001) / FADE_SECS)
        if self.fade >= 1.0:
            self.prev = -1
            return False
        return True

    # ---- 绘制 ----
    def render(self, painter: QPainter, rect: QRectF, now=None):
        """只做 drawImage：调色早在 `graded()` 里算好并缓存了。

        第 52 轮改法：原来这里是「先叠两张图，再对合成结果统一调色」，因为调色要
        跑在合成后的整幅图上。现在两张图各自调好色再按透明度叠 —— 乘算层对线性
        混合是可分配的，淡入结果与老做法一致，SoftLight/Plus 的差异在 0.9 秒的
        过渡里看不出来，代价换来的是单帧 112ms → 十几毫秒。
        """
        if not self.names:
            return
        w, h = int(rect.width()), int(rect.height())
        if w <= 0 or h <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        ox, oy = rect.x(), rect.y()
        base_op = painter.opacity()     # 主窗口是 0.97 半透明，别被淡入过程改掉
        cur = graded(self.current_name(), w, h, now)
        fading = self.prev >= 0 and self.fade < 1.0
        if fading:
            # 底：新图打底（调色后的图已不透明，随便一张都能铺满）
            if cur is not None:
                painter.drawImage(QPointF(ox, oy), cur)
            old = graded(self.names[self.prev % len(self.names)], w, h, now)
            if old is not None:
                # 上面按「还剩几成」压旧图，等价于老做法的 old + cur*fade
                k = 1.0 - max(0.0, min(1.0, self.fade))
                painter.setOpacity(base_op * k)
                painter.drawImage(QPointF(ox, oy), old)
                painter.setOpacity(base_op)
        elif cur is not None:
            painter.drawImage(QPointF(ox, oy), cur)
        painter.restore()


# ---------------------------------------------------------------- 调色（离屏）
def _state_key(st, warm, tsec_q):
    """天色指纹：把浮点天色量化成可比较的整数，避免每帧都判成「变了」。"""
    def q(v, step):
        return int(round(float(v) / step))

    def rgb(c):
        col = QColor(c)                  # theme._mix 返回 QColor，不是 tuple
        return (col.red(), col.green(), col.blue())

    return (q(st["day"], 0.02),
            rgb(st["top"]), rgb(st["mid"]), rgb(st["bottom"]),
            q(st["star"], 4),
            q(st["body_x"], 0.004),
            q(st["body_y"], 0.004),
            q(warm, 0.05),
            int(tsec_q))


def graded(name, w, h, now=None):
    """取出「已调到当前天色」的整窗图（离屏缓存）。

    为什么必须有这层缓存（第 52 轮，崩溃根因）：这一步原来在每次 paint 里现算，
    1080p 单帧 112ms、900p 80ms、720p 52ms，而淡入定时器 33ms —— 一帧都来不及，
    主线程全程卡在 QPainter 的 C++ 里；PySide 调 C++ 会释放 GIL，工作线程就在这
    缝隙抢到 GIL 触发 GC，回收 PySide 临时对象（QImage/QBrush 的隐式共享缓冲）时
    踩到 raster engine 正在用的内存 → access violation 落在 GC 那个线程，主线程和
    正在跑的依赖分析/下载全成了背锅的。

    缓存键 = (图名, 宽, 高, 天色指纹)，只在天色跨过 `_STATE_STEP` 秒的粒度、
    换图或改窗口尺寸时才重算一次。
    """
    w, h = max(int(w), 1), max(int(h), 1)
    st = theme.sky_state(now)
    warm = warm_factor(now)
    tsec_q = int(time.time() // _STATE_STEP) * _STATE_STEP
    key = (name, w, h, _state_key(st, warm, tsec_q))
    hit = _graded.get(key)
    if hit is not None:
        _graded.move_to_end(key)
        return hit
    base = scaled(name, w, h)
    if base is None:
        return None
    out = base.copy()          # 深拷贝：scaled 那张还要留给别的尺寸/天色复用
    p = QPainter(out)
    _paint_grade(p, QRectF(0, 0, w, h), st, warm, tsec_q)
    p.end()
    _graded[key] = out
    while len(_graded) > _GRADED_MAX:
        _graded.popitem(last=False)
    return out


def _paint_grade(painter: QPainter, rect: QRectF, st, warm, tsec_q):
    """把照片拉到「当前时刻」：压暗 + 天空染色 + 暖光 + 星星 + 暗角"""
    w, h = rect.width(), rect.height()
    day = max(0.0, min(1.0, float(st["day"])))
    night = 1.0 - day

    # ① 整体乘算：白天纯白（原图原样），夜里压成深蓝
    mul = theme._mix((26, 38, 74), (255, 255, 255), day)
    painter.setCompositionMode(QPainter.CompositionMode_Multiply)
    painter.fillRect(rect, mul)

    # ② 天空区染色：SoftLight 保留云的明暗，只把色相拽到当前天色
    skyrect = QRectF(rect.x(), rect.y(), w, h * SKY_H)
    g = QLinearGradient(0, 0, 0, skyrect.height())
    sky_a = int(90 + 70 * night)
    g.setColorAt(0.00, _a(st["top"], sky_a + 40))
    g.setColorAt(0.55, _a(st["mid"], sky_a + 30))
    g.setColorAt(0.88, _a(st["bottom"], sky_a))
    g.setColorAt(1.00, _a(st["bottom"], 0))
    painter.setCompositionMode(QPainter.CompositionMode_SoftLight)
    painter.fillRect(skyrect, QBrush(g))

    # ③ 日出/日落暖光：加色混合，落在天体那一侧的地平线上方
    if warm > 0.01:
        gx = rect.x() + w * (0.26 + 0.48 * float(st["body_x"]))
        gy = rect.y() + h * 0.44
        rad = max(w, h) * 0.62
        rg = QRadialGradient(QPointF(gx, gy), rad)
        rg.setColorAt(0.0, QColor(255, 176, 104, int(96 * warm)))
        rg.setColorAt(0.42, QColor(255, 148, 92, int(46 * warm)))
        rg.setColorAt(1.0, QColor(255, 140, 90, 0))
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.fillRect(rect, QBrush(rg))

    # ④ 星星（照抄程序化天空的观感，但用 Screen 叠在照片上）
    #    呼吸幅度压到 12%、相位用 `tsec_q`：同一份缓存内相位一致，换缓存时才跳一下，
    #    这样既不会让缓存每帧失效，看上去也不像死掉。
    if st["star"] > 6:
        rng = random.Random(42)
        painter.setCompositionMode(QPainter.CompositionMode_Screen)
        painter.setPen(Qt.NoPen)
        for i in range(int(110 * min(1.0, st["star"] / 160.0))):
            x = rect.x() + rng.uniform(0, w)
            y = rect.y() + rng.uniform(0, h * SKY_H * 0.92)
            s = rng.choice([1.2, 1.6, 2.2])
            tw = 0.88 + 0.12 * math.sin(tsec_q * 0.12 + i * 2.1)
            a = max(0, min(255, int(st["star"] * tw)))
            painter.setBrush(QColor(255, 255, 255, a))
            painter.drawRect(QRectF(x, y, s, s))

    # ⑤ 月亮辉光（照片里自带的是太阳，夜里给它换成一轮冷光）
    if night > 0.35:
        mx = rect.x() + w * float(st["body_x"])
        my = rect.y() + h * float(st["body_y"])
        rr = max(w, h) * 0.22
        mg = QRadialGradient(QPointF(mx, my), rr)
        mg.setColorAt(0.0, QColor(238, 246, 255, int(150 * night)))
        mg.setColorAt(0.35, QColor(206, 226, 255, int(58 * night)))
        mg.setColorAt(1.0, QColor(198, 220, 255, 0))
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.fillRect(rect, QBrush(mg))

    # ⑥ 暗角：亮背景上也要保证卡片和文字读得清
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    vg = QRadialGradient(QPointF(rect.center()), max(w, h) * 0.78)
    vg.setColorAt(0.0, QColor(0, 0, 0, 0))
    vg.setColorAt(0.58, QColor(0, 0, 0, int(24 + 34 * night)))
    vg.setColorAt(1.0, QColor(6, 12, 24, int(96 + 52 * night)))
    painter.fillRect(rect, QBrush(vg))

