# -*- coding: utf-8 -*-
"""文件类型图标（第六十四轮 / 第六十五轮加 jar、zip 内部图标）：全部代码绘制或从包里读，
不引任何外部图片文件。

为什么自己做：`QStyle.SP_FileIcon` 给**每个文件同一个白纸图标** —— 一个 mods 目录
四十多个 jar 排下来长得一模一样，玩家分不清哪个是模组、哪个已经禁用了、哪个是图；
而材质包/光影/数据包混在一个文件夹里时更糊。这里按扩展名画成不同颜色的小方块，
`.disabled` 的压暗并划一道斜杠，图片直接上**真缩略图**（这才是「文件本体」）。

第六十五轮：模组 jar 里有 `assets/<modid>/icon.png`、材质包 zip 里有 `pack.png`，
就把那张图当图标（读包的活交给 `core/jaricon.py`，这里只负责画）。

⚠ 读包是**磁盘活**，一个 jar 约 10ms（实测 213 个整合包模组读一遍 4 秒）。所以给列表用
的入口是 `pixmap_async()`：立刻返回字母方块，同时把路径丢给后台线程，读好后发
`icons_ready` 信号，列表收到信号再把图标换上去 —— 绝不为了几个图标把主线程卡住四秒。
"""
import os
import threading

from PySide6.QtCore import (QBuffer, QObject, QRectF, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QImageReader, QPainter,
                           QPainterPath, QPen, QPixmap)

from ..core import gearcache, jaricon, modicons
from . import theme

# 扩展名 → (底色, 方块里的字)
_KIND = {
    "jar": ("#3f9d5a", "J"),          # 模组
    "zip": ("#c2853a", "Z"),
    "mrpack": ("#c2853a", "M"),
    "7z": ("#c2853a", "7"),
    "rar": ("#c2853a", "R"),
    "png": ("#3d7ec7", "P"),
    "jpg": ("#3d7ec7", "P"),
    "jpeg": ("#3d7ec7", "P"),
    "webp": ("#3d7ec7", "P"),
    "gif": ("#3d7ec7", "P"),
    "json": ("#5b6b7f", "≡"),
    "toml": ("#5b6b7f", "≡"),
    "cfg": ("#5b6b7f", "≡"),
    "conf": ("#5b6b7f", "≡"),
    "properties": ("#5b6b7f", "≡"),
    "yml": ("#5b6b7f", "≡"),
    "yaml": ("#5b6b7f", "≡"),
    "txt": ("#6b7684", "T"),
    "md": ("#6b7684", "M"),
    "log": ("#6b7684", "≡"),
    "mcmeta": ("#7a5cc4", "{}"),
    "fsh": ("#7a5cc4", "S"),
    "vsh": ("#7a5cc4", "S"),
    "glsl": ("#7a5cc4", "S"),
    "dat": ("#8b95a3", "•"),
    "dll": ("#8b95a3", "D"),
    "exe": ("#8b95a3", "E"),
    "java": ("#a2703e", "j"),
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
# 里面可能自带图标的包（读包的逻辑在 core/jaricon.py）
_ARCHIVE_EXT = {".jar", ".zip", ".mrpack"}
_DISABLED_RGBA = (150, 160, 172)

_CACHE = {}          # (kind, size, disabled) → QPixmap
_THUMB_CACHE = {}    # (path, mtime, size) → QPixmap
_ICON_CACHE = {}     # (path, mtime, size, disabled) → QPixmap 或 None（包里没图标）

# ---------------- 后台读包（第六十五轮）----------------
# 一个 jar 约 10ms，一个几百个模组的目录同步读就是好几秒。主线程只负责「排队 + 收结果」，
# 磁盘活全在一条 daemon 线程里干，读完发 icons_ready，列表再把图标换上去。
_RAW = {}            # (path, mtime) → bytes（b"" = 读过，包里没图标）
_RAW_KEYS = set()    # 已排过队 / 已读过的 (path, mtime)，防止重复排
_TODO = []           # 待读队列
_DONE = {}           # 工作线程写、主线程读走
_LOCK = threading.Lock()
_THREAD = None
_TIMER = None

# 第六十七轮：这堆路径属于哪个游戏（"import" / "app"），决定图标字节落到哪一摊缓存。
# 只有「独立游戏设置 → 概览」那条链路会登记；没登记的（内置文件管理器、下载页）不进缓存。
_SCOPE = {}          # normcase(abspath(path)) → scope


class _Bus(QObject):
    """图标读好了的信号总线（只在内核层用，外面拿 `bus()`）

    第六十七轮补丁：信号带一个 `[(path, mtime)]` 列表 —— 这一轮**刚**读到字节的那几个。
    原来信号不带参数，收到之后只能整表重刷（213 行 × 0.6ms = 135ms），而后台线程
    每 60ms 就发一次，于是主线程整段时间都在重贴没变的图标（实测 4 秒里 2.1 秒花在
    这上面，单帧最长 460ms）。现在列表只换那几行。
    """
    icons_ready = Signal(list)


_BUS = None


def bus():
    """懒建信号总线 —— QObject 要在 QApplication 之后造才稳"""
    global _BUS
    if _BUS is None:
        _BUS = _Bus()
    return _BUS


def _official_cached(path: str) -> bytes:
    """官网上这个项目的图标 —— **只读本地缓存**，绝不联网（主线程用这个）"""
    try:
        pid = modicons.pid_for_file(path)
    except Exception:                                     # noqa: BLE001
        return b""
    if not pid:
        return b""
    return modicons.get_cached(pid) or b""


def _official_fetch(path: str) -> bytes:
    """联网取官网图标（**只能在后台线程调**），成功会落盘缓存"""
    try:
        pid = modicons.pid_for_file(path)
    except Exception:                                     # noqa: BLE001
        return b""
    if not pid:
        return b""
    return modicons.fetch(pid) or b""


def _norm_key(path: str) -> str:
    """登记用的键：**去掉 .disabled** —— 禁用/启用只是改名（mtime 不变），
    去掉后缀两边才能落到同一个缓存归属上（否则一改名就得重开 jar）。"""
    p = str(path or "")
    base = os.path.basename(p)
    if base.lower().endswith(".disabled"):
        p = os.path.join(os.path.dirname(p), base[:-len(".disabled")])
    return os.path.normcase(os.path.abspath(p))


def _scope_of(path: str) -> str:
    """这个文件登记过缓存归属吗（没登记返回空串 = 不落盘）"""
    return _SCOPE.get(_norm_key(path), "")


def set_scope(paths, scope: str):
    """登记一批路径的缓存归属（概览面板建行时调）"""
    if not scope:
        return
    for p in paths or ():
        try:
            _SCOPE[_norm_key(p)] = scope
        except (OSError, ValueError):
            continue


def _shrink(data: bytes, box: int = 64) -> bytes:
    """把包里的原图缩到 ≤64px 再存缓存。

    为什么不在缓存里存原图：Minecraft 模组图标常见 512×512，`QImage.fromData`
    解一张要 8~10ms —— 213 个就是 2 秒，缓存等于白做。缩到 64px 再编码成 png，
    解一张 <0.2ms，而列表最大也就显示 36px，看不出差别。
    """
    if not data:
        return data
    img = QImage.fromData(data)
    if img.isNull():
        return data
    if max(img.width(), img.height()) <= box:
        return data
    small = img.scaled(box, box, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    small.save(buf, "PNG")
    out = bytes(buf.data())
    buf.close()
    return out or data


def _read_bytes(path: str, mtime, allow_net: bool) -> bytes:
    """取一个包的图标字节：**先看磁盘缓存**，没有再开包（省掉大量 unzip）。

    `allow_net=False` 时官网兜底只读本地已下载的那份 —— 主线程绝不能联网。
    """
    scope = _scope_of(path)
    if scope:
        got = gearcache.icon_get(path, mtime, scope)
        if got is not None:
            return got
    try:
        data, _entry = jaricon.read_icon(path)
    except Exception:                                     # noqa: BLE001
        data = None
    if not data:
        data = _official_fetch(path) if allow_net else _official_cached(path)
    data = _shrink(data or b"")
    if scope:
        gearcache.icon_put(path, mtime, scope, data)
    return data


def _worker():
    """后台读包线程。

    两件事一起干：先看 jar / zip 里有没有图标，没有就按安装记录去官网取一张
    （取到的落盘缓存，以后离线也有）。**每干完一个就让一下 GIL**（`sleep`）：
    zip 解析基本都是纯 Python 的字节搬运，不给主线程留空隙的话，一个 200 个模组的
    目录一边读一边画，界面会明显发涩（实测不 sleep 时主线程建 213 行要 1.2 秒）。
    反正玩家看不见这条线程，慢一点无所谓。

    第六十七轮：这一步也顺手把字节写进「概览缓存」，同一次打开里再滚回来、
    或者禁用/启用后 reload，都不用重新开 jar。
    """
    import time
    while True:
        with _LOCK:
            if not _TODO:
                return                # 队列空了就退出，下次排队再起线程
            path, mtime = _TODO.pop(0)
        try:
            data = _read_bytes(path, mtime, allow_net=True)
        except Exception:                                     # noqa: BLE001
            data = b""
        with _LOCK:
            _DONE[(path, mtime)] = data or b""
        time.sleep(0.008)


def _drain():
    """主线程定时器：把后台读好的字节收进缓存，再通知界面换图标"""
    with _LOCK:
        done = dict(_DONE)
        _DONE.clear()
    if not done:
        return
    _RAW.update(done)
    _trim(_RAW, 1200, 400)
    try:
        bus().icons_ready.emit(list(done.keys()))
    except Exception:                                         # noqa: BLE001
        pass


def _enqueue(path: str):
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return
    _enqueue_key(key)


def _enqueue_key(key):
    """真正的排队（key 已经是 (path, mtime)，不再摸磁盘）"""
    global _THREAD, _TIMER
    if key in _RAW_KEYS:
        return
    _RAW_KEYS.add(key)
    with _LOCK:
        _TODO.append(key)
    if _TIMER is None:
        _TIMER = QTimer()
        _TIMER.setInterval(60)
        _TIMER.timeout.connect(_drain)
        _TIMER.start()
    if _THREAD is None or not _THREAD.is_alive():
        _THREAD = threading.Thread(target=_worker, name="jaricon", daemon=True)
        _THREAD.start()


def _ext(name: str) -> str:
    low = str(name or "").lower()
    if low.endswith(".disabled"):
        low = low[:-len(".disabled")]
    base = os.path.basename(low)
    return base.rsplit(".", 1)[-1] if "." in base else ""


def _tile(size: int, bg: str, text: str, alpha: int = 255) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    r = QRectF(0.6, 0.6, size - 1.2, size - 1.2)
    rad = size * 0.27
    if alpha < 255:
        p.setOpacity(alpha / 255.0)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    p.drawRoundedRect(r, rad, rad)
    f = QFont(theme.FONT)
    f.setPixelSize(max(7, int(round(size * 0.52))))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(255, 255, 255))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, text)
    p.end()
    return pm


def _glyph_tile(size: int, glyph: str, bg: str, disabled: bool = False) -> QPixmap:
    """类型化兜底图标：同色圆角方块 + 白色线描字形（第九十二轮）。

    为什么不是字母块：光影包 99% 不带 `pack.png`（实测本机 15 个全没有，只有
    `shaders/**/noise.png` 那种贴图），模组也有三成作者根本没放图标。以前一律画
    「绿底 J / 橙底 Z」，一屏看下去像一堆坏图；换成「立方体=模组、太阳=光影、
    相框=材质包、菱形=数据包」，至少一眼能分清这堆东西是什么。
    """
    key = ("glyph", glyph, size, bg, bool(disabled))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    if disabled:
        p.setOpacity(0.62)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    rad = size * 0.27
    p.drawRoundedRect(QRectF(0.6, 0.6, size - 1.2, size - 1.2), rad, rad)
    inset = size * 0.24
    theme.draw_icon(p, glyph, QRectF(inset, inset, size - 2 * inset, size - 2 * inset),
                    color="#ffffff", w=max(1.2, size * 0.072))
    p.end()
    if disabled:
        pm = _slash(pm, size)
    _CACHE[key] = pm
    return pm


# 按**所在目录**判断这块该画什么（key 全小写；`mods` 之类的目录名玩家不会改）
_GLYPH_BY_DIR = (
    ("shaderpacks", ("shader", "#8a6fb0")),
    ("resourcepacks", ("resourcepack", "#4d7fb5")),
    ("datapacks", ("datapack", "#b08a3e")),
    ("mods", ("mods", "#3f9d5a")),
)


def _dir_glyph(path: str):
    """这个包所在的目录像什么 → (glyph, 颜色)；认不出返回 None"""
    parts = [p for p in
             str(path or "").replace("\\", "/").split("/")[:-1] if p]
    # 从**最靠后**的一层往前找：`mods/` 里放的光影包仍按 mods 算，
    # 而 `resourcepacks/<包装出来的 datapacks>/` 按最近的那层算
    for part in reversed(parts):
        low = part.lower()
        for name, spec in _GLYPH_BY_DIR:
            if low == name:
                return spec
    return None


def _slash(pm: QPixmap, size: int) -> QPixmap:
    """禁用标记：一道深色斜杠（只把方块压暗不够显眼，尤其在小尺寸下）"""
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(QColor(74, 84, 96, 235), max(1.5, size * 0.13),
                  Qt.SolidLine, Qt.RoundCap))
    p.drawLine(int(size * 0.24), int(size * 0.76),
               int(size * 0.76), int(size * 0.24))
    p.end()
    return pm


def _folder(size: int) -> QPixmap:
    key = ("folder", size, False)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    w = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#a8b8cc"))
    p.drawRoundedRect(QRectF(w * 0.05, w * 0.18, w * 0.44, w * 0.24),
                      w * 0.07, w * 0.07)          # 上面那个小舌头
    p.setBrush(QColor("#8fa4bd"))
    p.drawRoundedRect(QRectF(w * 0.05, w * 0.29, w * 0.90, w * 0.53),
                      w * 0.08, w * 0.08)          # 主体
    p.end()
    _CACHE[key] = pm
    return pm


def _fit(img, size: int) -> QPixmap:
    """等比放到盖满 size×size，再居中裁成正方形（缩略图、包里图标都用它）"""
    pm = QPixmap.fromImage(img)
    if pm.width() != size or pm.height() != size:
        pm = pm.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation)
    x = max(0, (pm.width() - size) // 2)
    y = max(0, (pm.height() - size) // 2)
    return pm.copy(x, y, min(size, pm.width()), min(size, pm.height()))


def _round(pm: QPixmap, size: int) -> QPixmap:
    """圆角裁剪 —— 跟字母方块同一个圆角半径，摆一起才像一套"""
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.6, 0.6, size - 1.2, size - 1.2),
                        size * 0.24, size * 0.24)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def _trim(cache, limit, drop):
    """浏览大目录时不无限吃内存"""
    if len(cache) > limit:
        for k in list(cache)[:drop]:
            cache.pop(k, None)


def _thumb(path: str, size: int):
    """图片文件 → 真缩略图。读不出来返回 None（调用方退回字母方块）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, mtime, size)
    hit = _THUMB_CACHE.get(key)
    if hit is not None:
        return hit if not hit.isNull() else None
    try:
        rd = QImageReader(path)
        rd.setAutoTransform(True)
        org = rd.size()
        if org.isValid() and org.width() > 0 and org.height() > 0:
            # 先让解码器直接缩到目标尺寸：1920×1080 的截图按 18px 解，比解完再缩快一个量级
            rd.setScaledSize(org.scaled(QSize(size, size), Qt.KeepAspectRatioByExpanding))
        img = rd.read()
    except Exception:                                    # noqa: BLE001
        img = None
    if img is None or img.isNull():
        _THUMB_CACHE[key] = QPixmap()
        return None
    out = _round(_fit(img, size), size)
    _THUMB_CACHE[key] = out
    _trim(_THUMB_CACHE, 600, 200)
    return out


def _pack_icon(path: str, size: int, disabled: bool, allow_sync: bool = True,
               enqueue: bool = True, mtime=None):
    """jar 里的 `assets/<modid>/icon.png`、zip 里的 `pack.png`。

    读不到返回 None（调用方退回字母方块），**读不到也要进缓存** —— 否则一个没有
    图标的模组目录，每次刷新都要把四十个 jar 各打开一遍。

    `allow_sync=False` 是给列表用的：没读过就只排队、立刻返回 None，等 `icons_ready`
    再回来取。**这种情况不写 `_ICON_CACHE`**，否则那个 None 会被当成「这包没图标」永久记住。
    `enqueue=False`（`pixmap_deferred`）连排队都不排 —— 建几百行时统一 `prewarm` 一次。
    `mtime` 给了就不 stat：`scandir` 拿到的 mtime 本来就不花钱，省下来的
    `getmtime`（这个目录里一次约 0.7ms）乘 213 就是那「卡一下」的一大半。
    """
    if mtime is None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
    key = (path, mtime, size, disabled)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    raw_key = (path, mtime)
    if raw_key in _RAW:
        data = _RAW[raw_key]
    elif allow_sync:
        # 主线程：先看概览缓存 / 再开包 / 官网兜底只读本地（绝不联网）
        data = _read_bytes(path, mtime, allow_net=False)
        _RAW[raw_key] = data or b""
        _trim(_RAW, 1200, 400)
    elif enqueue:
        _enqueue_key(raw_key)
        return None
    else:
        return None
    out = None
    if data:
        img = QImage.fromData(data)
        if not img.isNull():
            out = _round(_fit(img, size), size)
            if disabled:
                out = _slash(out, size)
    _ICON_CACHE[key] = out
    _trim(_ICON_CACHE, 800, 300)
    return out


def pixmap_for(name_or_path: str, size: int = 18, is_dir: bool = False) -> QPixmap:
    """给文件名（或完整路径）画一个图标。

    `is_dir=True` 画文件夹；否则按扩展名。`.disabled` 会压暗 + 划斜杠。
    jar / zip 里的图标**当场读**（小列表、探针用这个）；列表请用 `pixmap_async`。
    """
    return _pixmap(name_or_path, size, is_dir, allow_sync=True)


def pixmap_async(name_or_path: str, size: int = 18, is_dir: bool = False,
                 mtime=None) -> QPixmap:
    """列表专用：包内图标没读过时立刻返回字母方块，同时排队去后台读。

    读好后 `bus().icons_ready` 会发一次信号，调用方收到再调一遍本函数就拿到真图了
    （那时是纯内存缓存，几百行也不卡）。`mtime` 已知（清单里带来的）就不 stat。
    """
    return _pixmap(name_or_path, size, is_dir, allow_sync=False, mtime=mtime)


def pixmap_deferred(name_or_path: str, size: int = 18, is_dir: bool = False,
                    mtime=None) -> QPixmap:
    """建列表行专用：**连排队都不排**，只查缓存。

    给「一次建几百行」的场景：如果每行都顺手 `_enqueue`（还要 `getmtime`），
    再加上后台线程抢 GIL，打开一个 213 个模组的目录要 1.2 秒。这里只做内存查找，
    建完行统一 `prewarm()` 一次，读好了 `icons_ready` 再换那几行。

    `mtime`（`scandir` 顺手拿到的）给了就**一次 stat 都不做**。
    """
    return _pixmap(name_or_path, size, is_dir, allow_sync=False, enqueue=False,
                   mtime=mtime)


def _pixmap(name_or_path: str, size: int, is_dir: bool, allow_sync: bool,
            enqueue: bool = True, mtime=None) -> QPixmap:
    size = max(8, int(size))
    full = str(name_or_path or "")
    base = os.path.basename(full)
    disabled = base.lower().endswith(".disabled")
    if is_dir:
        return _folder(size)
    ext = _ext(base)
    # 建几百行时不给 mtime（省掉 stat）也能走下面：只查缓存，不碰磁盘
    is_file = True if mtime is not None else os.path.isfile(full)
    # 图片先试真缩略图（「文件本体」）
    if (not disabled and ("." + ext) in _IMAGE_EXT and is_file):
        th = _thumb(full, size)
        if th is not None:
            return th
    # 模组 jar / 材质包 zip：包里自带图标就用它（第六十五轮）
    if ("." + ext) in _ARCHIVE_EXT and is_file:
        pk = _pack_icon(full, size, disabled, allow_sync=allow_sync,
                        enqueue=enqueue, mtime=mtime)
        if pk is not None:
            return pk
        # 第九十二轮：包里真没图（光影包基本都没有）→ 按所在目录画类型字形，
        # 别再给一个「橙底 Z」让玩家以为图标全挂了。
        spec = _dir_glyph(full)
        if spec is not None:
            return _glyph_tile(size, spec[0], spec[1], disabled)
    # 缓存键必须带**完整**扩展名：以前未知类型统一写成 ""，于是 .xyz 和 .abc
    # 撞同一个槽 —— 先画的字母方块被后画的直接复用了
    key = (ext, size, disabled)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    bg, txt = _KIND.get(ext, ("#95a0ae", (ext[:1].upper() or "?")))
    pm = _tile(size, bg, txt, alpha=_DISABLED_RGBA[0] if disabled else 255)
    if disabled:
        pm = _slash(pm, size)
    _CACHE[key] = pm
    return pm


def icon_for(name_or_path: str, size: int = 18, is_dir: bool = False) -> QIcon:
    return QIcon(pixmap_for(name_or_path, size, is_dir))


def icon_for_async(name_or_path: str, size: int = 18, is_dir: bool = False) -> QIcon:
    return QIcon(pixmap_async(name_or_path, size, is_dir))


def icon_for_deferred(name_or_path: str, size: int = 18, is_dir: bool = False,
                      mtime=None) -> QIcon:
    return QIcon(pixmap_deferred(name_or_path, size, is_dir, mtime=mtime))


def prewarm(paths, scope: str = ""):
    """先排队一批路径（列表建完行就调它，别等第一次绘制才发现要读包）

    `scope` 给了就登记进「概览缓存」（"import" / "app"），读出来的图标字节会落盘，
    同一次打开里再滚回来 / 禁用一个再刷新都不用重新开 jar。
    """
    paths = [str(p) for p in (paths or ())]
    if scope:
        set_scope(paths, scope)
    for p in paths:
        try:
            if jaricon.is_archive(p) and os.path.isfile(p):
                _enqueue(p)
        except OSError:
            continue


def prewarm_pairs(pairs, scope: str = ""):
    """`[(path, mtime)]` 版 `prewarm` —— mtime 已知就**一次 stat 都不做**。

    内置文件管理器列目录时 `scandir` 已经把 mtime 一起返回了（Windows 上是
    目录枚举顺带的信息，不额外花钱）。`prewarm` 那版每个文件要 `isfile` + `getmtime`
    两次 stat，213 个就是 330ms —— 这正是「打开文件夹卡那么一下」的另一个来源。
    """
    pairs = [(str(p), mt) for p, mt in (pairs or ())]
    if not pairs:
        return
    if scope:
        set_scope([p for p, _ in pairs], scope)
    for p, mt in pairs:
        try:
            if not jaricon.is_archive(p):
                continue
        except Exception:                                 # noqa: BLE001
            continue
        if mt:
            _enqueue_key((p, mt))
        else:
            # 老清单里没有 mtime（上一版缓存的 JSON）→ 自己 stat 一次，
            # 免得用 0 当键排队：读出来的字节落在 (path, 0) 上，
            # 而列表拿真 mtime 去取，永远取不到 —— 图标就再也不显示了。
            _enqueue(p)


def wait_idle(timeout=8.0) -> bool:
    """等后台队列清空（探针 / 截图脚本用；界面里千万别调）"""
    import time
    end = time.time() + max(0.0, float(timeout))
    while time.time() < end:
        with _LOCK:
            busy = bool(_TODO)
        if not busy and (_THREAD is None or not _THREAD.is_alive()):
            break
        _drain()
        time.sleep(0.02)
    _drain()
    return True


def clear_cache():
    """改完文件（禁用/启用/删除）后调用 —— 缩略图是按 mtime 缓存的，一般不用清"""
    _CACHE.clear()
    _THUMB_CACHE.clear()
    _ICON_CACHE.clear()
    _RAW.clear()
    _RAW_KEYS.clear()
