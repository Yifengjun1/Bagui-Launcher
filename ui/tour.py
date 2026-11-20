# -*- coding: utf-8 -*-
"""引导教程（第八十轮）：一步一步点亮关键入口，配一个小窗讲「这是什么、有什么用」。

三条硬要求都是踩过的坑得来的：

1. **高亮靠遮罩开洞**，不给控件套 QSS / 不画边框套在控件外层。
   主界面全是自绘控件（PillButton / NavButton / IconButton），一改样式就破坏
   原有观感；而且主页卡片 `refresh()` 会整块重建，套在旧控件上的东西直接消失。
2. **小窗不能抢焦点**（`WA_ShowWithoutActivating`）。「游戏管理」那四个步骤要
   开着 `Qt.Popup` 弹层，而 popup **一失焦就关**。普通窗口一被激活，popup 连
   高亮框一起没了，玩家看到的是「闪一下就没了」。带 `WA_ShowWithoutActivating`
   的窗口点按钮不会激活自己，popup 才留得住。
3. **目标每次现找，不缓存指针**。主页 `refresh()` 重建卡片、BrowsePage 换页重建
   子控件，缓存下来的 QWidget 会变成已销毁的野指针（PySide 里直接
   `Internal C++ object already deleted`）。

对外只有四个函数：`enabled()` / `start()` / `stop()` / `maybe_start()`。
"""
import weakref

from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..core.config import CONFIG
from . import theme

# ================================================================ 步骤表
# name  —— 图里那个选项叫什么（和界面上一个字都不能差，玩家要照着找）
# what  —— 它管什么、里面有什么、怎么用（一段话，别写成说明书）
# page  —— 走到这一步要先切到哪一页（None = 不动）
# popup —— 这一步的目标在「游戏管理」弹层里（要先把弹层打开）
# find  —— 现找目标控件；找不到返回 None（照常显示小窗，只是不画框）

def _text_btn(root, *words):
    """按文案找一个可见的按钮（主页卡片会重建，只能按文案现找）"""
    from PySide6.QtWidgets import QAbstractButton
    try:
        for b in root.findChildren(QAbstractButton):
            t = str(b.text() or "")
            if t and all(w in t for w in words) and b.isVisible():
                return b
    except Exception:                                  # noqa: BLE001
        pass
    return None


def _home(win, *words):
    page = getattr(win, "page_home", None)
    return _text_btn(page, *words) if page is not None else None


def _nav(win, key):
    tb = getattr(win, "titlebar", None)
    return tb.nav_buttons.get(key) if tb is not None else None


def _popup(win):
    """当前「游戏管理」弹层（没开 / 已关就是 None）"""
    page = getattr(win, "page_home", None)
    btn = getattr(page, "btn_games", None)
    p = getattr(btn, "_popup", None)
    return p if (p is not None and p.isVisible()) else None


def _gm(win, kind):
    p = _popup(win)
    if p is None:
        return None
    if kind in ("official", "app"):
        sw = getattr(p, "target_switch", None)
        return (getattr(sw, "_btns", {}) or {}).get(kind) if sw is not None else None
    if kind == "added":
        return getattr(p, "btn_added", None)
    return getattr(p, "btn_refresh", None)


STEPS = [
    # ---------------- 主页 ----------------
    {
        "name": "浏览整合包", "page": None,
        "what": "打开整合包市场：按下载量 / 更新时间翻，也能直接搜中文名（比如"
                "「ATM10 天空」）。点卡片看详情和所有版本，装的时候会自己安排到"
                "官方启动器或本启动器，不用手动搬文件。",
        "find": lambda w: _home(w, "浏览整合包"),
    },
    {
        "name": "打开游戏目录", "page": None,
        "what": "直接打开本启动器自己的 .minecraft 目录。存档（saves）、模组"
                "（mods）、日志（logs）都在里面；游戏起不来时先来这里看"
                " logs\\latest.log 的最后几行。",
        "find": lambda w: _home(w, "打开游戏目录"),
    },
    {
        "name": "下载游戏", "page": None,
        "what": "装一个干净的游戏版本：先选游戏版本（1.21.1 等），再选加载器"
                "（原版 / Forge / NeoForge / Fabric / OptiFine）；弹层里还能选"
                "「下载到 官方 / 应用」。整合包自带版本时这里不用点。",
        "find": lambda w: getattr(getattr(w, "page_home", None), "btn_loader", None),
    },
    {
        "name": "Java下载", "page": None,
        "what": "游戏靠 Java 跑。这里能看装了哪些 Java，也能一键补上缺的那个："
                "1.20.5 以上要 Java 21，1.17~1.20.4 要 17，1.16 及更早要 8。"
                "启动报「找不到 Java」就来这儿。",
        "find": lambda w: getattr(getattr(w, "page_home", None), "btn_java", None),
    },
    {
        "name": "游戏管理", "page": None,
        "what": "本机所有游戏版本和实例的清单。点 ▶ 选「独立启动」要跑哪个，"
                "一行里还能进游戏目录、改设置、删除（进回收站可还原）；"
                "下拉列表里挑一个就能直接启动。",
        "find": lambda w: getattr(getattr(w, "page_home", None), "btn_games", None),
    },
    # ---------------- 顶部状态栏 ----------------
    {
        "name": "整合包", "page": None,
        "what": "顶部第一个页签，回到整合包市场 —— 和主页的「浏览整合包」是同一页。"
                "整合包 / 模组 / 光影 / 材质包这几页右边会多一根白竖条，"
                "点它就能选要启动的游戏。",
        "find": lambda w: _nav(w, "modpack"),
    },
    {
        "name": "右侧白竖条", "page": "modpack",
        "what": "开着独立启动时，下载页最右边这根细白条是「选要启动的游戏」的把手。"
                "点一下，面板会顺着它向左展开；在里面点某个整合包或版本，"
                "主页启动按钮就切成它，点「启动」直接开局。再点一次白条收回去。",
        "find": lambda w: getattr(w, "_drawer_handle", None),
    },
    {
        "name": "模组/数据包", "page": None,
        "what": "一个页签装两种内容，点旁边的小箭头在「模组」和「数据包」之间切。"
                "下载的内容会装进所选游戏目录的 mods / datapacks。",
        "find": lambda w: _nav(w, "mod"),
    },
    {
        "name": "主页图标", "page": None,
        "what": "中间那个精灵球：左键回主页；右键直接打开设置；长按把窗口收成"
                "桌面上的小悬浮球（点悬浮球回来，球旁边还能选页面）。",
        "find": lambda w: getattr(getattr(w, "titlebar", None), "orb", None),
    },
    {
        "name": "光影", "page": None,
        "what": "光影包市场，下载后装进 shaderpacks。游戏里要装了 Iris 或 OptiFine"
                "才能在「选项 → 视频设置 → 光影」里选到。",
        "find": lambda w: _nav(w, "shader"),
    },
    {
        "name": "材质包", "page": None,
        "what": "材质包市场，下载后装进 resourcepacks。进游戏在「选项 → 资源包」"
                "里启用，可以多套叠着用（排上面的优先）。",
        "find": lambda w: _nav(w, "resourcepack"),
    },
    # ---------------- 游戏管理 ----------------
    {
        "name": "官方", "page": "home", "popup": True,
        "what": "只看「官方启动器」那条目录里的游戏。独立启动开着时这里是灰的 —— "
                "那时候游戏都在「应用」目录里。",
        "find": lambda w: _gm(w, "official"),
    },
    {
        "name": "应用", "page": "home", "popup": True,
        "what": "只看本启动器自己的目录（.minecraft）。「下载到 应用」装的东西、"
                "独立启动跑的游戏都在这儿。",
        "find": lambda w: _gm(w, "app"),
    },
    {
        "name": "勾（已导入）", "page": "home", "popup": True,
        "what": "登记过的其它启动器目录（PCL2 / HMCL / Prism…）。可以「添加已有"
                "文件夹」把现成的整合包零拷贝登记进来，点运行就用原目录启动，"
                "不额外占空间。",
        "find": lambda w: _gm(w, "added"),
    },
    {
        "name": "刷新", "page": "home", "popup": True,
        "what": "重新扫描当前这条目录。在别处新装 / 删掉了版本，点一下就同步过来，"
                "不用重开软件。",
        "find": lambda w: _gm(w, "refresh"),
    },
]

TOTAL = len(STEPS)


# ================================================================ 高亮遮罩
class TourOverlay(QWidget):
    """盖在主窗上的半透明遮罩，目标那块挖空 + 描边。

    `WA_TransparentForMouseEvents`：遮罩不该拦鼠标 —— 玩家边看引导边点也正常，
    而且拦了之后「浏览整合包」这类步骤根本没法自己动手试。
    """

    def __init__(self, win):
        super().__init__(win)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._hole = None
        self.hide()

    def set_hole(self, rect):
        """rect：主窗坐标系里的目标矩形；None = 不挖洞（整块压暗）"""
        old = self._hole
        self._hole = QRect(rect) if rect is not None else None
        if old == self._hole and self.isVisible():
            return                                   # 没变就别重画（每 260ms 调一次）
        self.update()

    def follow_geometry(self):
        win = self.parentWidget()
        if win is None:
            return
        if self.geometry() != win.rect():
            self.setGeometry(win.rect())

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self._hole
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        if h is not None and not h.isEmpty():
            path.addRoundedRect(QRectF(h), 10, 10)
        path.setFillRule(Qt.OddEvenFill)
        p.fillPath(path, QColor(12, 18, 28, 122))
        if h is None or h.isEmpty():
            return
        # 外发光：两圈越来越淡的描边，比纯细线更容易「一眼看到」
        rr = QRectF(h)
        for grow, alpha, wid in ((6, 26, 8), (3, 52, 5)):
            p.setPen(QPen(QColor(47, 124, 246, alpha), wid))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rr.adjusted(-grow, -grow, grow, grow), 12, 12)
        p.setPen(QPen(QColor(47, 124, 246, 235), 2.0))
        p.drawRoundedRect(rr, 10, 10)


class TourRing(QWidget):
    """弹层里用的描边（弹层是独立顶层窗口，主窗遮罩盖不到它）。"""

    def __init__(self, host, target):
        super().__init__(host)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.host = host
        self.target = target
        self.move_to_target()

    def move_to_target(self):
        t, host = self.target, self.host
        if t is None or host is None:
            return
        try:
            tl = t.mapTo(host, QPoint(0, 0))
        except Exception:                              # noqa: BLE001
            return
        self.setGeometry(QRect(tl, t.size()).adjusted(-5, -5, 5, 5))
        self.raise_()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(47, 124, 246, 60), 7))
        p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 14, 14)
        p.setPen(QPen(QColor(47, 124, 246, 235), 2.0))
        p.drawRoundedRect(r, 10, 10)


# ================================================================ 箭头按钮
class _Arrow(QWidget):
    clicked = Signal()

    def __init__(self, direction, parent=None):
        super().__init__(parent)
        self.d = direction                            # -1 左 / +1 右
        self.usable = True
        self._hover = False
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.usable:
            self.clicked.emit()
            e.accept()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._hover and self.usable:
            p.setBrush(QColor(47, 124, 246, 28))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 7, 7)
        col = QColor(47, 124, 246) if self.usable else QColor(150, 160, 172)
        col.setAlpha(255 if self.usable else 130)
        p.setPen(QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cx, cy = self.width() / 2, self.height() / 2
        d = self.d
        p.drawLine(QPointF(cx - 4 * d, cy), QPointF(cx + 4 * d, cy))
        p.drawLine(QPointF(cx + 4 * d, cy), QPointF(cx - 1 * d, cy - 4))
        p.drawLine(QPointF(cx + 4 * d, cy), QPointF(cx - 1 * d, cy + 4))


# ================================================================ 小窗
class TourWindow(QWidget):
    """引导小窗：名称 / 作用 + 右下角「← 3/14 →」。

    参照图就是一张白卡：左边两行「名称：」「作用：」，右下角翻页。所以这里
    不用磨砂，实心白底 + 深色字，保证在任意壁纸上都读得清。
    """

    prev = Signal()
    next = Signal()
    skip = Signal()
    W = 348

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 不抢焦点：见文件头第 2 条 —— 抢了就把游戏管理的弹层关掉
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(self.W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.setSpacing(6)

        self.lb_name = QLabel("")
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(True)
        self.lb_name.setFont(f)
        self.lb_name.setStyleSheet("color:#1f2733;background:transparent;")
        self.lb_name.setWordWrap(True)
        lay.addWidget(self.lb_name)

        self.lb_what = QLabel("")
        f2 = QFont(theme.FONT)
        f2.setPixelSize(11)
        self.lb_what.setFont(f2)
        self.lb_what.setStyleSheet("color:#46525f;background:transparent;")
        self.lb_what.setWordWrap(True)
        lay.addWidget(self.lb_what)

        foot = QHBoxLayout()
        foot.setSpacing(2)
        # 「跳过」贴在左下角（参照图那边是空的，但没有任何出口的引导会把人
        # 烦到：不想看的人得连点 14 下才能关掉）
        self.lb_skip = QLabel("跳过")
        f4 = QFont(theme.FONT)
        f4.setPixelSize(10)
        self.lb_skip.setFont(f4)
        self.lb_skip.setStyleSheet("color:#9aa5b1;background:transparent;")
        self.lb_skip.setCursor(Qt.PointingHandCursor)
        self.lb_skip.setToolTip("不再看引导（设置 → 常规 → 引导教程 可以重新打开）")
        self.lb_skip.mousePressEvent = lambda e: self.skip.emit()
        foot.addWidget(self.lb_skip)
        foot.addStretch(1)
        self.lb_cnt = QLabel("")
        f3 = QFont(theme.FONT)
        f3.setPixelSize(11)
        f3.setBold(True)
        self.lb_cnt.setFont(f3)
        self.lb_cnt.setStyleSheet("color:#5a6675;background:transparent;")
        foot.addWidget(self.lb_cnt)
        self.b_prev = _Arrow(-1)
        self.b_prev.clicked.connect(self.prev)
        foot.addWidget(self.b_prev)
        self.b_next = _Arrow(1)
        self.b_next.clicked.connect(self.next)
        foot.addWidget(self.b_next)
        lay.addLayout(foot)

    # ---------- 内容 ----------
    def set_step(self, name, what, i, total):
        self.lb_name.setText(f"名称：{name}")
        self.lb_what.setText("作用：" + what)
        self.lb_cnt.setText(f"{i + 1}/{total}")
        self.b_prev.usable = i > 0
        self.b_prev.update()
        self.b_next.usable = i < total - 1
        self.b_next.update()
        self.adjustSize()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:               # 右键也算「不看了」
            self.skip.emit()
            return
        super().mousePressEvent(e)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(9, 9, -9, -9)
        # 外阴影（三层越来越淡）：桌面杂色背景下卡片边缘也能立起来
        for grow, alpha in ((5, 22), (3, 34), (1.5, 46)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 30, 44, alpha))
            p.drawRoundedRect(r.adjusted(-grow, -grow + 2, grow, grow + 2), 14, 14)
        p.setBrush(QColor(255, 255, 255, 248))
        p.setPen(QPen(QColor(31, 39, 51, 34), 1))
        p.drawRoundedRect(r, 12, 12)

    def place(self, anchor, win_rect):
        """摆在小窗旁边：优先在目标下方，放不下就上方，再不行贴窗口右下角。

        `anchor` / `win_rect` 都是**全局**坐标。
        """
        self.adjustSize()
        w, h = self.width(), self.height()
        gap = 14
        if anchor is not None and not anchor.isEmpty():
            x = anchor.center().x() - w // 2
            y = anchor.bottom() + gap
            if y + h > win_rect.bottom() - 8:
                y = max(win_rect.top() + 8, anchor.top() - gap - h)
            x = max(win_rect.left() + 8, min(x, win_rect.right() - w - 8))
        else:
            x = win_rect.right() - w - 24
            y = win_rect.bottom() - h - 24
        self._at(x, y)

    def place_beside(self, anchor, avoid, win_rect):
        """摆在 `avoid`（弹层）**外侧**：右 → 左 → 下 → 上，都放不下才退回 `place()`。

        引导走到「游戏管理」那四步时，弹层里是一列游戏；按 `place()` 摆在
        目标下方会正好压在列表上 —— 玩家既看不清列表，也分不清哪块是引导卡。
        弹层本身不宽（430），窗口两侧通常还有地方。
        """
        self.adjustSize()
        w, h = self.width(), self.height()
        gap = 12
        if avoid is not None and not avoid.isEmpty():
            cy = (anchor.center().y() if anchor is not None and not anchor.isEmpty()
                  else avoid.center().y())
            for x, y in ((avoid.right() + gap, cy - h // 2),
                         (avoid.left() - gap - w, cy - h // 2),
                         (avoid.center().x() - w // 2, avoid.bottom() + gap),
                         (avoid.center().x() - w // 2, avoid.top() - gap - h)):
                r = QRect(int(x), int(y), w, h)
                if (r.left() >= win_rect.left() + 8
                        and r.right() <= win_rect.right() - 8
                        and r.top() >= win_rect.top() + 8
                        and r.bottom() <= win_rect.bottom() - 8):
                    self._at(x, y)
                    return True
        self.place(anchor, win_rect)
        return False

    def _at(self, x, y):
        pos = QPoint(int(x), int(y))
        if self.pos() != pos:
            self.move(pos)
        if not self.isVisible():
            self.show()
        self.raise_()


# ================================================================ 控制器
class Tour(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.i = 0
        self.overlay = TourOverlay(win)
        self.card = TourWindow()
        self.card.prev.connect(self.prev)
        self.card.next.connect(self.next)
        self.card.skip.connect(self.finish)
        self.ring = None
        self._popup = None
        self._stopped = False
        self._token = 0                     # 切页要等一帧：过期的延迟回调靠它作废
        self._tick = QTimer(self)
        self._tick.setInterval(260)
        self._tick.timeout.connect(self._follow)
        win.installEventFilter(self)

    # ---------- 生命周期 ----------
    def start(self, index=0):
        self.i = max(0, min(int(index), TOTAL - 1))
        # 「卡片隐藏」会让内容区在鼠标不动 N 秒后整块隐去 —— 引导期间正好没人
        # 动鼠标，高亮框会突然指向一片空白。这里先把它暂停，走完再恢复。
        try:
            self.win.auto_hide.pause()     # 顺带会把已经隐去的内容恢复出来
        except Exception:                              # noqa: BLE001
            pass
        self.overlay.follow_geometry()
        self.overlay.show()
        self.overlay.raise_()
        self._tick.start()
        self._goto(self.i)

    def stop(self, finished=False):
        self._stopped = True
        self._token += 1
        self._tick.stop()
        self._close_ring()
        self._keep_popup(None)
        try:
            self.card.hide()
        except Exception:                              # noqa: BLE001
            pass
        try:
            self.overlay.hide()
        except Exception:                              # noqa: BLE001
            pass
        try:
            self.win.auto_hide.resume()    # 引导结束，把「卡片隐藏」还回去
        except Exception:                              # noqa: BLE001
            pass
        if finished:
            CONFIG.set("tour_done", True)

    def finish(self):
        self.stop(finished=True)

    # ---------- 翻页 ----------
    def next(self):
        if self.i >= TOTAL - 1:
            self.finish()
            return
        self._goto(self.i + 1)

    def prev(self):
        if self.i <= 0:
            return
        self._goto(self.i - 1)

    def _goto(self, i):
        self.i = i
        step = STEPS[i]
        self._token += 1
        token = self._token
        # 弹层：这一步要弹层就开（不是就收起，免得盖住主页）
        self._keep_popup(bool(step.get("popup")))
        want = step.get("page")
        if want and getattr(self.win, "current_page", "") != want:
            try:
                self.win.switch_page(want)
            except Exception as e:                     # noqa: BLE001
                _note("引导：切页失败 %s" % e)
        # 切页 / 弹层统计都要时间：等一拍再量目标位置，否则量到的是旧几何
        delay = 260 if step.get("popup") else 150
        QTimer.singleShot(delay, lambda: self._paint_step(token))

    def _paint_step(self, token):
        if token != self._token:
            return                                     # 已经翻到别的步骤了
        step = STEPS[self.i]
        self.card.set_step(step["name"], step["what"], self.i, TOTAL)
        self._follow()

    # ---------- 跟随 ----------
    def _follow(self):
        if self._stopped:
            return
        # 有模态窗（设置 / 登录 / 提示框）时先让路：小窗是置顶的，不让路就会盖在
        # 设置窗上面，玩家根本点不到；等它关了再自己回来。
        try:
            if QApplication.activeModalWidget() is not None:
                self.card.hide()
                self.overlay.hide()
                return
            if not self.overlay.isVisible() and self.win.isVisible():
                self.overlay.follow_geometry()
                self.overlay.show()
                self.overlay.raise_()
        except Exception:                              # noqa: BLE001
            pass
        step = STEPS[self.i]
        self.overlay.follow_geometry()
        target = None
        try:
            target = step["find"](self.win)
        except Exception as e:                         # noqa: BLE001
            _note("引导：找目标失败 %s" % e)
        hole = None
        if target is not None and target.isVisible() and target.width() > 2:
            if step.get("popup"):
                # 弹层是独立顶层窗口，主窗遮罩挖不到它 → 在弹层里单独描边
                if self.ring is None or self.ring.target is not target:
                    self._close_ring()
                    host = target.window()
                    try:
                        self.ring = TourRing(host, target)
                        self.ring.show()
                    except Exception:                  # noqa: BLE001
                        self.ring = None
                elif self.ring is not None:
                    self.ring.move_to_target()
            else:
                try:
                    tl = target.mapTo(self.win, QPoint(0, 0))
                    hole = QRect(tl, target.size()).adjusted(-5, -5, 5, 5)
                except Exception:                      # noqa: BLE001
                    hole = None
        self.overlay.set_hole(hole)
        # 小窗：贴着目标摆；没有目标就落到窗口右下角
        g_anchor = QRect()
        if hole is not None:
            g_anchor = QRect(self.win.mapToGlobal(hole.topLeft()), hole.size())
        elif target is not None and target.isVisible():
            g_anchor = QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
        win_rect = QRect(self.win.mapToGlobal(QPoint(0, 0)), self.win.size())
        g_popup = None
        if step.get("popup") and target is not None:
            # 目标在「游戏管理」弹层里：小窗要避开整个弹层，别压在列表上
            try:
                host = target.window()
                if host is not None and host is not self.win and host.isVisible():
                    g_popup = QRect(host.mapToGlobal(QPoint(0, 0)), host.size())
            except Exception:                          # noqa: BLE001
                g_popup = None
        if g_popup is not None:
            self.card.place_beside(g_anchor if not g_anchor.isEmpty() else None,
                                   g_popup, win_rect)
        else:
            self.card.place(g_anchor if not g_anchor.isEmpty() else None, win_rect)

    # ---------- 弹层 ----------
    def _keep_popup(self, want):
        page = getattr(self.win, "page_home", None)
        btn = getattr(page, "btn_games", None)
        if btn is None:
            return
        cur = _popup(self.win)
        # 玩家自己开的那一个是 `Qt.Popup`（独占鼠标），引导期间点不动小窗，
        # 换成不独占的那个（`open_for_tour`）。
        if want and cur is not None and not getattr(cur, "nograb", False):
            try:
                cur.close()
            except Exception:                          # noqa: BLE001
                pass
            cur = None
        if want and cur is None:
            try:
                opener = getattr(btn, "open_for_tour", None)
                if opener is not None:
                    self._popup = opener()
                else:                                  # 老接口兜底
                    btn._toggle()
                    self._popup = getattr(btn, "_popup", None)
            except Exception as e:                     # noqa: BLE001
                _note("引导：打开游戏管理失败 %s" % e)
        elif not want and cur is not None:
            try:
                cur.close()
            except Exception:                          # noqa: BLE001
                pass

    def _close_ring(self):
        if self.ring is not None:
            try:
                self.ring.hide()
                self.ring.deleteLater()
            except Exception:                          # noqa: BLE001
                pass
            self.ring = None

    # ---------- 窗口事件 ----------
    def eventFilter(self, obj, ev):
        if obj is self.win:
            t = ev.type()
            if t in (QEvent.Resize, QEvent.Move):
                try:
                    self.overlay.follow_geometry()
                    if self.ring is not None:
                        self.ring.move_to_target()
                except Exception:                      # noqa: BLE001
                    pass
            elif t in (QEvent.Hide, QEvent.WindowStateChange):
                # 主窗最小化 / 收进托盘：小窗和遮罩一并收掉，别留在桌面上
                try:
                    if not self.win.isVisible() or self.win.isMinimized():
                        self.card.hide()
                        self.overlay.hide()
                except Exception:                      # noqa: BLE001
                    pass
        return False


def _note(text):
    try:
        from ..core import crashlog
        crashlog.note(text)
    except Exception:                                  # noqa: BLE001
        pass


# ================================================================ 对外
_LIVE = []


def enabled() -> bool:
    return bool(CONFIG.get("tour_enabled", True))


def done() -> bool:
    return bool(CONFIG.get("tour_done", False))


def running() -> bool:
    """有没有一轮正在跑。**已经收掉的不算** —— Tour 的 parent 是主窗，
    对象不会因为关掉就销毁，只看「引用还在不在」会永远为真。"""
    for r in list(_LIVE):
        t = r()
        if t is not None and not getattr(t, "_stopped", False):
            return True
    return False


def _live_ref():
    for r in list(_LIVE):
        obj = r()
        if obj is not None and not getattr(obj, "_stopped", False):
            return obj
    return None


def start(win, index=0):
    """起一轮引导。已经有一轮在跑就先收掉（重复点「引导教程」不该叠两层遮罩）。"""
    stop(win)
    try:
        t = Tour(win)
        t.start(index)
        _LIVE.append(weakref.ref(t))
        return t
    except Exception as e:                             # noqa: BLE001
        _note("引导：启动失败 %s" % e)
        return None


def stop(win=None, finished=False):
    t = _live_ref()
    if t is not None:
        try:
            t.stop(finished=finished)
        except Exception as e:                         # noqa: BLE001
            _note("引导：收尾失败 %s" % e)


def maybe_start(win):
    """主窗出场时调：开关开着、且还没走完 → 从第一步开始。

    `MCASSIST_NO_TOUR=1` 时一次都不起：回归脚本会成批创建主窗，
    遮罩和置顶小窗会盖住它们要抓的图、也会抢走切页。
    """
    import os as _os
    if str(_os.environ.get("MCASSIST_NO_TOUR") or "").strip() in ("1", "true", "yes"):
        return None
    if not enabled() or done() or running():
        return None
    return start(win, 0)


def reset():
    """再走一遍（设置里重新打开开关时用）。"""
    CONFIG.set("tour_done", False)
