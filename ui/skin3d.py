# -*- coding: utf-8 -*-
"""皮肤 3D 纸娃娃渲染：参照《我的世界》国际基岩版「开始游戏」页面的角色娃娃。

与旧实现的区别：旧版把整块侧面贴图沿斜向拉伸（侧面像一条飘带、头顶被拉歪）；
这里做真正的 3D 等距投影——每个部位是立方盒，只有三个面可见
（正面 / 左侧面 / 顶面），用 quadToQuad 把皮肤 UV 四边形映射到投影后的平行四边形，
并叠加面明暗，形成立体娃娃感。

几何：世界坐标 x 右 / y 上 / z 朝观察者；
相机 yaw=30°（角色向右转，露出其左侧面）+ pitch=12°（略俯视，露出头顶）。

像素风两条铁律（踩过坑）：
  · 放大一律最近邻（`SmoothPixmapTransform=False`），开双线性会糊成一片；
  · 皮肤 64x64 的第二层（帽子/外套/袖子/裤子）必须单独画，盒子比本体外扩，
    且紧跟在对应本体后面画 —— 漏了它角色就「没穿外面的衣服」。
"""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF, \
    QRadialGradient, QTransform

YAW = math.radians(30.0)
PITCH = math.radians(12.0)
_CY, _SY = math.cos(YAW), math.sin(YAW)
_CP, _SP = math.cos(PITCH), math.sin(PITCH)


def _proj(x, y, z):
    """世界坐标 -> (屏幕x, 屏幕y, 深度)"""
    x1 = x * _CY + z * _SY
    z1 = -x * _SY + z * _CY
    y2 = y * _CP - z1 * _SP
    z2 = y * _SP + z1 * _CP
    return x1, -y2, z2


def _part(name, box, front, side, top, no_top=False):
    return {"name": name, "box": box, "front": front, "side": side,
            "top": top, "no_top": no_top}


# ---------------------------------------------------------------- 盒子与 UV
# 盒子 = (x0, x1, y0, y1, z0, z1)；UV = (ux, uy, uw, uh)，皮肤 64x64 布局
#
# 角色面朝 +z（镜头在 +z 偏右），于是：
#   · 正面 = z1 那一面（UV 里的 front）；
#   · 头顶 = y1 那一面（top）；
#   · 镜头偏在角色**右前方**（yaw 30°），能看见的侧面是 x0 那一面 —— 那是
#     角色的**右侧**（面对面站着，他的右手在你的左手边）。
#
# 皮肤展开图上，一块侧面的 **右边缘** 才是贴着正面的那条边（[侧面][正面]
# 这样相邻），所以侧面的 u=0 必须落在**后**边缘。这两点以前都反了：
# 侧面错取了 +x 那一块（模板里它贴在正面的右边），u 方向也没翻 ——
# 对称皮肤看不出来，单边眼圈 / 单只手套 / 半边长袖就会左右反。
#
# 手臂与腿各自留 0.12 unit 的细缝，让部件分界清晰（基岩版娃娃的立体感来源）
# texOffs（u, v）+ 宽高深 →  face = (u+d, v+d)   side(-x) = (u, v+d)   top = (u+d, v)
HEAD = _part("head", (-4.0, 4.0, 12.0, 20.0, -4.0, 4.0),
             (8, 8, 8, 8), (0, 8, 8, 8), (8, 0, 8, 8))
HAT = _part("hat", (-4.45, 4.45, 12.45, 20.45, -4.45, 4.45),
            (40, 8, 8, 8), (32, 8, 8, 8), (40, 0, 8, 8))
BODY = _part("body", (-4.0, 4.0, 0.0, 12.0, -2.0, 2.0),
             (20, 20, 8, 12), (16, 20, 4, 12), (20, 16, 8, 4), no_top=True)
# 右手臂在 -x（镜头这一侧），左手臂在 +x（远侧）
ARM_R = _part("arm_r", (-8.12, -4.12, 0.0, 12.0, -2.0, 2.0),
              (44, 20, 4, 12), (40, 20, 4, 12), (44, 16, 4, 4))
ARM_L = _part("arm_l", (4.12, 8.12, 0.0, 12.0, -2.0, 2.0),
              (36, 52, 4, 12), (32, 52, 4, 12), (36, 48, 4, 4))
# 腿的顶面永远被躯干压着，画出来会翻到正面上去（腰带下面那块"屋顶"）
LEG_R = _part("leg_r", (-4.06, -0.06, -12.0, 0.0, -2.0, 2.0),
              (4, 20, 4, 12), (0, 20, 4, 12), (4, 16, 4, 4), no_top=True)
LEG_L = _part("leg_l", (0.06, 4.06, -12.0, 0.0, -2.0, 2.0),
              (20, 52, 4, 12), (16, 52, 4, 12), (20, 48, 4, 4), no_top=True)


def _grow(box, d):
    """盒子向外扩 d（外层贴图必须比本体大一圈，否则和本体共面会闪/被盖住）"""
    x0, x1, y0, y1, z0, z1 = box
    return (x0 - d, x1 + d, y0 - d, y1 + d, z0 - d, z1 + d)


# ---- 第二层（1.8 起 64x64 皮肤右侧/下侧那一片 alpha 外层）----
# 帽子扩 0.5（与原版一致），衣服/袖子/裤子扩 0.25；UV 全部按 64x64 布局
JACKET = _part("jacket", _grow(BODY["box"], 0.25),
               (20, 36, 8, 12), (16, 36, 4, 12), (20, 32, 8, 4), no_top=True)
SLEEVE_R = _part("sleeve_r", _grow(ARM_R["box"], 0.25),
                 (44, 36, 4, 12), (40, 36, 4, 12), (44, 32, 4, 4))
SLEEVE_L = _part("sleeve_l", _grow(ARM_L["box"], 0.25),
                 (52, 52, 4, 12), (48, 52, 4, 12), (52, 48, 4, 4))
PANTS_R = _part("pants_r", _grow(LEG_R["box"], 0.25),
                (4, 36, 4, 12), (0, 36, 4, 12), (4, 32, 4, 4), no_top=True)
PANTS_L = _part("pants_l", _grow(LEG_L["box"], 0.25),
                (4, 52, 4, 12), (0, 52, 4, 12), (4, 48, 4, 4), no_top=True)

# 外层部件：64x32 老皮肤只有帽子这一层，其余外层在 64x32 里根本不存在
OVERLAYS = (HAT, JACKET, SLEEVE_R, SLEEVE_L, PANTS_R, PANTS_L)

# 绘制顺序：远 -> 近（+x 那一侧更远）。每个部件画完立刻画它的外层，
# 否则外层会被后面更近的本体挡住（袖口、裤脚那圈颜色就没了）
_ORDER = [ARM_L, SLEEVE_L, LEG_L, PANTS_L, BODY, JACKET, HEAD, HAT,
          LEG_R, PANTS_R, ARM_R, SLEEVE_R]

# 面明暗（叠加黑色 alpha）：正面最亮，左侧面次之，顶面稍暗
_SHADE = {"front": 0, "left": 48, "top": 26}


def _face_points(part, face):
    x0, x1, y0, y1, z0, z1 = part["box"]
    if face == "front":                       # z = z1，正对观察者
        pts = [(x0, y1, z1), (x1, y1, z1), (x1, y0, z1), (x0, y0, z1)]
    elif face == "top":                       # y = y1，UV 行序 后->前
        pts = [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)]
    else:                                     # side: x = x0（角色右侧）
        # 展开图里侧面的**右边缘**挨着正面，所以 u=0 落在后边缘 (z0)：
        # 反过来写就成了「左右镜像」，不对称皮肤一眼看出错。
        pts = [(x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (x0, y0, z0)]
    return [QPointF(*_proj(*q)[:2]) for q in pts]


_UV_CACHE = {}


def _uv_has_content(skin, uv):
    """UV 区域是否含可见像素（全透明则整面跳过，避免透明面被阴影染成灰边）"""
    key = (id(skin), skin.cacheKey(), uv)
    hit = _UV_CACHE.get(key)
    if hit is not None:
        return hit
    ux, uy, uw, uh = [int(v) for v in uv]
    has = False
    sub = skin.copy(ux, uy, uw, uh)
    # 逐像素扫（最多 8x12=96 下）：以前按 1/6 抽点，一条细线、一枚单像素扣子
    # 正好落在抽不到的格子上，整张面就被判成"透明"丢掉 —— 细节就少一块。
    for yy in range(0, uh):
        for xx in range(0, uw):
            if ((sub.pixel(xx, yy) >> 24) & 0xFF) > 12:
                has = True
                break
        if has:
            break
    _UV_CACHE[key] = has
    if len(_UV_CACHE) > 300:
        _UV_CACHE.clear()
    return has


_FACE_CACHE = {}


def _face_image(skin, uv, shade):
    """取某个面的贴图（已按面明暗压暗）。

    明暗必须只作用在「这个面自己的像素」上：直接往画布上盖黑色半透明会把
    外层透明的那些像素下面的本体也一起抹黑，袖口/帽檐就会脏一圈。
    所以先在 8x12 的小图上用 SourceAtop 压暗，再整图映射过去。
    """
    key = (skin.cacheKey(), uv, shade)
    img = _FACE_CACHE.get(key)
    if img is not None:
        return img
    ux, uy, uw, uh = [int(v) for v in uv]
    img = skin.copy(ux, uy, uw, uh).convertToFormat(QImage.Format_ARGB32)
    if shade:
        q = QPainter(img)
        q.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        q.fillRect(img.rect(), QColor(0, 0, 0, shade))
        q.end()
    if len(_FACE_CACHE) > 400:
        _FACE_CACHE.clear()
    _FACE_CACHE[key] = img
    return img


def _draw_face(p, skin, part, face, s, ox, oy):
    ux, uy, uw, uh = part[face]
    if not _uv_has_content(skin, part[face]):
        return
    # src 以 (0,0) 起：贴图是按面裁好的小图，画的时候也从 (0,0) 画
    src = QPolygonF([QPointF(0, 0), QPointF(uw, 0),
                     QPointF(uw, uh), QPointF(0, uh)])
    dst = QPolygonF([QPointF(ox + q.x() * s, oy + q.y() * s)
                     for q in _face_points(part, face)])
    t = QTransform()
    try:
        ok = QTransform.quadToQuad(src, dst, t)
    except TypeError:
        t = QTransform.quadToQuad(src, dst)
        ok = t is not None
    if not ok or t is None:
        return
    img = _face_image(skin, part[face], _SHADE.get(face, 0))
    p.save()
    p.setTransform(t)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    # 像素风贴图：放大时用最近邻（否则 8x12 的贴图被双线性插值糊成一片）
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)
    p.drawImage(QRectF(0, 0, uw, uh), img, QRectF(0, 0, uw, uh))
    p.restore()


def _bbox():
    xs, ys = [], []
    for part in _ORDER:
        x0, x1, y0, y1, z0, z1 = part["box"]
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    sx, sy, _ = _proj(x, y, z)
                    xs.append(sx)
                    ys.append(sy)
    return min(xs), max(xs), min(ys), max(ys)


def fit_scale(width: float, height: float, fill: float = 0.94) -> float:
    """按控件可用区域反算合适的渲染倍率，做到 1:1 出图（避免二次缩放发虚）"""
    X0, X1, Y0, Y1 = _bbox()
    body_w = max(X1 - X0, 1e-6)
    body_h = max((Y1 - Y0) + 7.0, 1e-6)          # +7 为脚下阴影预留
    pad = 12.0
    sw = (fill * max(width, 1) - pad) / body_w
    sh = (fill * max(height, 1) - pad) / body_h
    return max(1.0, min(sw, sh))


def render_skin_3d(skin: QImage, scale: float = 8.0, phase: float = 0.0):
    """渲染 3D 娃娃。phase: 浮动动画相位（0~1 循环）"""
    if skin is None or skin.isNull():
        return None
    skin = skin.convertToFormat(QImage.Format_ARGB32)
    s = float(scale)
    bob = math.sin(phase * math.tau) * s * 0.45

    X0, X1, Y0, Y1 = _bbox()
    pad = 6
    ground_pad = int(7 * s)                       # 底部阴影空间
    W = int((X1 - X0) * s) + pad * 2
    H = int((Y1 - Y0) * s) + pad * 2 + ground_pad
    canvas = QImage(W, H, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing, True)
    # 皮肤是 8x12 这种极小贴图，放大几十倍：一旦开平滑插值就糊成一片色块边界，
    # 所以整张画布都走最近邻，只让阴影椭圆自己带抗锯齿
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)

    ox = pad - X0 * s
    oy = pad - Y0 * s + bob

    # ---- 脚下光晕 + 阴影（世界 y=-12 平面中心）----
    gx, gy, _ = _proj(0, -12, 0)
    cx, cy = ox + gx * s, oy + gy * s + 2
    glow = QRadialGradient(QPointF(cx, cy), 5.2 * s)
    glow.setColorAt(0, QColor(96, 148, 226, 85))
    glow.setColorAt(1, QColor(96, 148, 226, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(glow)
    p.drawEllipse(QPointF(cx, cy), 5.6 * s, 1.9 * s)
    p.setBrush(QColor(24, 32, 46, 165))
    p.drawEllipse(QPointF(cx, cy), 3.1 * s, 0.95 * s)
    p.setBrush(QColor(58, 72, 96, 190))
    p.drawEllipse(QPointF(cx, cy - 0.15 * s), 2.5 * s, 0.72 * s)

    # ---- 依远近视序绘制各部位三面 ----
    order = list(_ORDER)
    if skin.height() < 64:
        # 64x32 老皮肤没有独立的左臂/左腿区域：复用右肢贴图，位置仍在左侧
        arm_l = dict(ARM_L)
        arm_l.update({k: ARM_R[k] for k in ("front", "side", "top")})
        leg_l = dict(LEG_L)
        leg_l.update({k: LEG_R[k] for k in ("front", "side", "top")})
        order = [arm_l, leg_l, BODY, HEAD, HAT, LEG_R, ARM_R]
        # 顺序仍是 远(+x) -> 近(-x)：左臂左腿在 +x（复用右手右腿的贴图），
        # 真正的右手右腿原样画在 -x 那一侧。
    for part in order:
        # 躯干顶面被头部完全遮住，腿顶面被躯干压着 —— 这两个画出来就会
        # 翻到正面上去（腰带下面那块"屋顶"），直接跳过。
        if not part.get("no_top"):
            _draw_face(p, skin, part, "top", s, ox, oy)
        _draw_face(p, skin, part, "side", s, ox, oy)
        _draw_face(p, skin, part, "front", s, ox, oy)

    p.end()
    return canvas
