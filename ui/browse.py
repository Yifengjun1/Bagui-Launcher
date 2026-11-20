# -*- coding: utf-8 -*-
"""浏览页：整合包 / 模组 / 光影 / 材质包 共用
搜索栏（输入框 + 版本/筛选 + 搜索）+ 下拉筛选行 + 结果网格 + 导入
"""
import os
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..core import bbsmc
from ..core import cnnames as cn_names
from ..core import cnsearch, dl, gamelaunch, installer, modrinth
from ..core.asyncio_qt import run_async
from ..core.config import CONFIG
from ..core.session import current_session
from ..core.tasks import TASKS, progress_for
from . import theme
from .dialogs import DetailDialog, ProgressDailog
from .widgets import (AsyncImageView, DropDown, FrostCard, IconButton,
                      Pager, PillButton, Toast, launch_official_async)

def icon_url_for(hit):
    """缩略图标：CDN 支持 ?imwidth=64（原图最大 512px 太慢）"""
    url = hit.get("icon_url")
    if url and "?" not in url:
        return url + "?imwidth=64"
    return url

SORT_ITEMS = [("relevance", "相关性"), ("downloads", "总下载量"),
              ("follows", "人气"), ("newest", "最新包")]
PLATFORM_ITEMS = [("Modrinth", "Modrinth")]
LOADER_ITEMS = [("forge", "Forge"), ("neoforge", "NeoForge"), ("fabric", "Fabric")]
# 数据包在 Modrinth 上只有 datapack 这一个加载器标签（/v2/tag/loader 实测），
# 所以数据包页的「模组加载器」只放这一项，选了也不会过滤掉内容。
LOADER_ITEMS_DATAPACK = [("datapack", "数据包")]
ALL = "全部"          # 下拉「全部」= 该维度不过滤（平台=双平台合并）

TYPE_NAMES = {"modpack": "整合包", "mod": "模组", "datapack": "数据包",
              "shader": "光影", "resourcepack": "材质包"}


# ================================================================ FlowLayout
class FlowLayout(QVBoxLayout):
    """按可用宽度自动决定列数的卡片网格。

    第五十九轮改的（原来叫「固定 3 列」）：

    玩家反馈「在其他设备上缩放窗口时卡片不发生变化」。实测下来卡片**宽度**
    是跟着变的，不变的是**列数** —— 永远三等分。1080p@100% 时每张 293px，
    还算能看；但换到 125% / 150% 缩放的机器或小屏，内容区只剩 640px 上下，
    三等分后每张不到 200px，58px 的图标 + 标题 + 简介 + 三个标签全挤成一团，
    看起来就像「卡片根本没响应窗口」。

    现在按 `CARD_MIN` 反算列数：640px 会自动降到 2 列，1280px 能给到 4 列。
    """
    CARD_MIN = 268          # 一张卡片至少这么宽，再窄标题就截没了
    GAP = 12
    MAX_COLS = 6

    def __init__(self, parent=None, cols=3):
        super().__init__(parent)
        self.cols = max(1, int(cols))
        self._cards = []
        self._fillers = []
        self._rows = []

    @classmethod
    def cols_for(cls, width):
        """这个宽度下最多能排几列"""
        w = int(width or 0)
        if w <= 0:
            return 1
        n = (w + cls.GAP) // (cls.CARD_MIN + cls.GAP)
        return max(1, min(cls.MAX_COLS, int(n)))

    def _take_widgets(self, item, out, subs=None):
        """把一个布局项里所有 widget 收进 out，行布局本身收进 subs（要一起销毁）"""
        sub = item.layout()
        if sub is not None:
            if subs is not None:
                subs.append(sub)
            while sub.count():
                self._take_widgets(sub.takeAt(0), out, subs)
            return
        w = item.widget()
        if w is not None:
            out.append(w)

    def _drop_rows(self):
        """摘掉所有布局行。

        卡片（`self._cards` 里的）只从布局里摘下来、不销毁 —— QWidgetItem
        不拥有 widget，takeAt 之后它还在 host 上，重新 addWidget 就能再受管。
        其它东西（提示 label、空出来的行布局、占位 spacer）顺手销毁，
        否则反复重建会越堆越多。

        **第七十四轮修的 bug**：以前只处理「直接挂在网格上的 widget」，而行布局
        （`addLayout(row)`）里的卡片是靠 `sub.takeAt(0)` 摘的 —— 摘完既没
        `hide()` 也没 `deleteLater()`，于是它们**继续以旧几何留在 grid_host 上**。
        换一批搜索结果 / 换列数重建之后，旧卡片还在原地画着，新卡片叠上去 =
        玩家看到的「卡片重叠、文字串到隔壁卡片里」。
        现在递归收集行里的 widget，不保留的一律**先 hide 再排队销毁**。

        **第八十二轮把 `setParent(None)` 拿掉了**（玩家录屏：屏幕左侧排着一列列
        空白「八桂启动器」小窗）。原因：`addWidget` 挂父窗之后，「显示」是
        **排队**发的（`_q_showIfNotHidden`）。如果这条排队事件还没送达，控件就先被
        `setParent(None)` 摘成无父的 —— 等它一到，这条 show 就落在一只**顶层控件**
        上，当场变成一块带 Windows 原生标题栏的窗口（标题取 applicationName，
        客户区空白），而且 `setParent(None)` 保留了旧坐标，于是全部堆在屏幕左上角、
        正好排成原来的网格。现在只 hide：既立刻不画（74 轮的问题照样解决），
        又永远没有机会变成窗口。
        """
        for f in self._fillers:
            try:
                f.hide()
                f.deleteLater()
            except RuntimeError:
                pass
        self._fillers = []
        keep_id = {id(w) for w in self._cards}
        dead, subs = [], []
        while self.count():
            self._take_widgets(self.takeAt(0), dead, subs)
        for w in dead:
            if id(w) in keep_id:
                continue
            try:
                w.hide()                   # 先隐藏：立刻不再参与绘制
                w.deleteLater()            # 再排队销毁（别在信号里直接 delete）
            except RuntimeError:
                pass
        for sub in subs:
            try:
                sub.deleteLater()
            except RuntimeError:
                pass
        self._rows = []

    def add_cards(self, widgets):
        self._cards = [w for w in widgets]
        self._rebuild()

    def set_cols(self, cols):
        """换列数；真的变了才重建，返回是否重建过"""
        cols = max(1, min(self.MAX_COLS, int(cols or 1)))
        if cols == self.cols:
            return False
        self.cols = cols
        self._rebuild()
        return True

    def _rebuild(self):
        self._drop_rows()
        if not self._cards:
            return
        row = None
        for i, w in enumerate(self._cards):
            if i % self.cols == 0:
                row = QHBoxLayout()
                row.setSpacing(self.GAP)
                self.addLayout(row)
                self._rows.append(row)
            row.addWidget(w, 1)
        # 末行补透明占位，保证同一行的卡片等宽（否则最后一行会被拉宽）
        # 第八十二轮：占位控件必须**带父窗创建** —— 无父的 QWidget 在被
        # addWidget 挂上之前本身就是一只顶层窗口，一旦排队中的 show 落到它头上，
        # 桌面上就会多一块空白标题栏窗口（详见 `_drop_rows` 的注释）
        rem = len(self._cards) % self.cols
        if rem and row is not None:
            for _ in range(self.cols - rem):
                filler = QWidget(self.parentWidget())
                filler.setStyleSheet("background:transparent;")
                row.addWidget(filler, 1)
                self._fillers.append(filler)


# ================================================================ 结果卡片
class ChipLabel(QLabel):
    """可省略截断的标签 chip（最小宽度 0，保证网格不溢出）"""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMaximumHeight(19)
        self.setStyleSheet(
            "QLabel{background:rgba(47,124,246,34);color:#2f7cf6;"
            "font-size:10px;border-radius:8px;padding:2px 7px;}")

    def setText(self, t):
        self._text = t
        super().setText(t)

    def paintEvent(self, ev):
        fm = self.fontMetrics()
        avail = max(0, self.width() - 14)
        self._elided = fm.elidedText(self._text, Qt.ElideRight, avail)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(47, 124, 246, 34))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        p.setPen(QColor("#2f7cf6"))
        p.setFont(self.font())
        p.drawText(self.rect().adjusted(7, 0, -7, 0), Qt.AlignVCenter, self._elided)


class ResultCard(FrostCard):
    def __init__(self, hit, page, parent=None):
        super().__init__(parent, radius=14, tint=140)
        self.hit = hit
        self.page = page
        self.setCursor(Qt.PointingHandCursor)
        # 第五十八轮：卡片多了一行「中文简介」，高度 122 → 140
        self.setFixedHeight(140)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 12, 10)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        icon = AsyncImageView(radius=10, placeholder="")
        icon.setFixedSize(58, 58)
        icon.load(icon_url_for(hit))
        top.addWidget(icon)

        info = QVBoxLayout()
        info.setSpacing(2)
        # 中文名优先（内置词库查不到就老实显示英文原名，绝不机翻）
        # 第七十六轮：BBSMC 补到的中文名是**后台补的**（搜索先返回英文，
        # 一两百毫秒后才变中文），所以标题/简介得存成属性，
        # 由 `refresh_cn()` 就地改文本 —— 绝不能重建整张卡片（会闪、会丢滚动位置）。
        raw_title = str(hit.get("title") or hit.get("name") or "")
        self.raw_title = raw_title
        self.title_lb = QLabel(cn_names.title_of(hit) or raw_title)
        title = self.title_lb
        tf = QFont(theme.FONT)
        tf.setPixelSize(13)
        tf.setBold(True)
        title.setFont(tf)
        # 高度写死：卡片是固定高度的，让 QVBoxLayout 自己压会把 11px 的那行
        # 文字压到 11px 高（笔画被切一半，看着像「灰条」）
        title.setFixedHeight(20)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        title.setToolTip(raw_title if cn_names.cn_name(hit) else "")
        info.addWidget(title)
        self.desc_lb = QLabel(cn_names.desc_of(hit) or "暂无简介")
        desc = self.desc_lb
        desc.setStyleSheet("color:#6b7786;font-size:11px;")
        desc.setFixedHeight(16)
        desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        desc.setToolTip(str(hit.get("description") or hit.get("summary") or ""))
        info.addWidget(desc)
        dl = hit.get("downloads", hit.get("downloadCount", 0))
        author = hit.get("author") or ((hit.get("authors") or [{}])[0].get("name", "-"))
        meta = QLabel(f"{author} · {_fmt(dl)} 下载")
        meta.setStyleSheet("color:#5a6675;font-size:11px;")
        meta.setFixedHeight(15)
        meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        info.addWidget(meta)
        tags = (hit.get("display_categories") or hit.get("categories") or [])[:3]
        chips = QHBoxLayout()
        chips.setSpacing(4)
        for c in tags:
            if not isinstance(c, str):
                continue
            chip = ChipLabel(modrinth.cn_category(c))
            chips.addWidget(chip, 1)
        chips.addStretch(0)
        info.addLayout(chips)
        top.addLayout(info, 1)
        lay.addLayout(top)

        bottom = QHBoxLayout()
        self.state_label = QLabel("")
        self.state_label.setStyleSheet("color:#77828f;font-size:10px;")
        bottom.addWidget(self.state_label)
        bottom.addStretch(1)
        self._pid = str(hit.get("project_id") or hit.get("id") or "")
        self.btn = PillButton("下载", "accent")
        self.btn.setFixedHeight(27)
        bottom.addWidget(self.btn)
        self.sync_installed()
        lay.addLayout(bottom)

    def refresh_cn(self):
        """读到新中文名就就地换文本（BBSMC 后台补词后调用，**不重建卡片**）。

        `cn_names.title_of/desc_of` 每次都现查（内置库 → 玩家 json → BBSMC
        缓存三层），所以这里重设一次文本就够；查不到的仍然显示英文原名。

        就地改而不是重建：重建会让整页闪一下、丢掉滚动位置，而且卡片一多
        就是几百个控件重新 polish（第七十四轮刚为此把明细卡改成虚拟列表）。
        """
        try:
            # 第一百一十六轮：繁体界面显示的是转换后文本，比较须按简体原文，
            # 否则每次刷新都误判「变了」而重复 setText（虚拟列表里会累积）。
            from . import i18n_ui as _iu
            cn = cn_names.title_of(self.hit) or self.raw_title
            if _iu.original_text(self.title_lb) != cn:
                self.title_lb.setText(cn)
            self.title_lb.setToolTip(
                self.raw_title if cn_names.cn_name(self.hit) else "")
            d = cn_names.desc_of(self.hit) or "暂无简介"
            if _iu.original_text(self.desc_lb) != d:
                self.desc_lb.setText(d)
        except (AttributeError, RuntimeError):
            pass

    def sync_installed(self):
        """按磁盘实际状态 + 所选游戏的能力刷新按钮。

        · 已经装在这个游戏里     → 「已安装」（灰、不可点）
        · 所选游戏装不了这类内容 → 「不可下载」（灰、不可点，悬停有原因）
          （第六十一轮：原版跑不了模组和光影；1.13 以前没有数据包）
        · 其它                   → 「下载」（蓝、可点）
        """
        installed = CONFIG.is_installed(self._pid, target_scope())
        note = gamelaunch.caps_note(self.page.ptype, target_caps())
        if getattr(self, "_dl_connected", False):
            try:
                self.btn.clicked.disconnect()
            except Exception:                      # noqa: BLE001
                pass
            self._dl_connected = False
        if installed:
            self.btn.setText("已安装")
            self.btn.kind = "ghost"
            self.btn.setEnabled(False)
            self.btn.setToolTip("")
            self.btn.setCursor(Qt.ArrowCursor)
            self.state_label.setText("")
        elif note:
            self.btn.setText("不可下载")
            self.btn.kind = "ghost"
            self.btn.setEnabled(False)
            self.btn.setToolTip(note)
            self.btn.setCursor(Qt.WhatsThisCursor)
            self.state_label.setText("原版装不了" if (target_caps() or {}).get(
                "vanilla") else "当前游戏不支持")
            self.state_label.setToolTip(note)
        else:
            self.btn.setText("下载")
            self.btn.kind = "accent"
            self.btn.setEnabled(True)
            self.btn.setToolTip("")
            self.btn.setCursor(Qt.PointingHandCursor)
            self.state_label.setText("")
            self.btn.clicked.connect(self._download)
            self._dl_connected = True
        self.btn.update()

    def mousePressEvent(self, e):
        # 打开详情
        if e.button() == Qt.LeftButton:
            d = DetailDialog(
                self.window(), self.hit, self._download,
                installed=CONFIG.is_installed(self._pid, target_scope()),
                ctx={"versions": list(self.page.state.get("versions") or []),
                     "loaders": list(self.page.state.get("loaders") or [])},
                on_dep_download=self.page.download_dep,
                on_dep_download_many=self.page.download_deps,
                kind=self.page.ptype, caps=target_caps(),
                # 第七十六轮：详情窗里的「汉化」按钮 → 回本页走下载/核对/覆盖
                on_langpack=self.page.start_langpack)
            d.exec()

    def _download(self, version=None):
        """version = 详情页选中的往期版本行（没选就是 None = 自动最新）"""
        self.page.download_hit(self.hit, version=version)


def open_folder(path, highlight="", parent=None):
    """打开安装目录给玩家看（第五十七轮：装完自动弹出来）。

    第六十轮：默认走**内置**文件管理器（设置 → 常规 →「文件管理器」开着时），
    刚写进去的文件会被选中并滚到可见 —— 不再弹 Windows 资源管理器；玩家把
    那个开关关掉才用系统的。

    单独一个函数是为了探针能整块 monkeypatch —— 无头测试不该真的弹窗口。
    """
    if not path:
        return False
    from .fspicker import use_builtin
    if use_builtin():
        try:
            from .fspicker import pick_view
            pick_view(parent, "文件已保存到这里", path, highlight=highlight)
            return True
        except Exception:                          # noqa: BLE001
            pass
    try:
        if os.path.isdir(path):
            os.startfile(path)          # noqa: S606
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 目标游戏
# 这两样都被每张卡片调用（一页 24 张），所以带上「按目标缓存」：换游戏才重算。
_TARGET_CACHE = {"key": None, "caps": {}, "scope": ""}


def target_scope():
    """当前所选游戏的实例目录（空串 = 没选 / 纯版本，按官方目录算）"""
    _sync_target_cache()
    return _TARGET_CACHE.get("scope") or ""


def target_caps():
    """当前所选游戏能装什么（原版不能装模组/光影 —— 第六十一轮）"""
    _sync_target_cache()
    return _TARGET_CACHE.get("caps") or {}


def _sync_target_cache():
    t = gamelaunch.launch_target()
    key = (t.get("version_id"), t.get("path"), t.get("game_dir"),
           t.get("name"), CONFIG.get("game_dir"))
    if _TARGET_CACHE["key"] == key:
        return
    _TARGET_CACHE["key"] = key
    try:
        _TARGET_CACHE["scope"] = gamelaunch.target_instance_dir()
    except Exception:                              # noqa: BLE001
        _TARGET_CACHE["scope"] = ""
    try:
        _TARGET_CACHE["caps"] = gamelaunch.target_caps()
    except Exception:                              # noqa: BLE001
        _TARGET_CACHE["caps"] = {}


def _fmt(n):
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}k"
        return str(n)
    except Exception:
        return str(n)


# ================================================================ 浏览页
class BrowsePage(QWidget):
    """整合包/模组/光影/材质包 共用浏览页"""

    def __init__(self, window, ptype, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.ptype = ptype
        self.state = {
            "query": "", "versions": [], "categories": [], "loaders": [],
            "platform": "Modrinth", "sort": "relevance",
        }
        # 翻页：page 从 1 起，page_size 与搜索请求的 limit 保持一致
        self.page = 1
        self.page_size = 24
        self.total_hits = 0
        # Modrinth 总数缓存（翻页时用来统计 + 判断是否已翻完）
        self._mr_total = 0
        self.hits = []
        self._busy = False
        # 下载互斥（_dl_busy 会覆盖这两个）：默认给个空实现，
        # 免得「取消保存 / 取消版本确认」时撞上没初始化过的属性
        self._dl_running = False
        self._dl_reset = lambda: None
        # 第七十六轮：
        # · `_pending_lang` = 「点汉化时包还没装」→ 装完要接着打汉化的那段状态
        # · `_cn_gen`      = 中文名后台补全的代数（翻页/换词后作废旧回调）
        self._pending_lang = None
        self._cn_gen = 0

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        from .pages import Sidebar
        self.sidebar = Sidebar(window)
        root.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)

        # ---- 搜索栏卡 ----
        search_card = FrostCard(radius=16, tint=150)
        search_card.setFixedHeight(110)
        sc = QVBoxLayout(search_card)
        sc.setContentsMargins(16, 16, 16, 14)
        sc.setSpacing(10)

        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.search_edit = QLineEdit()
        # 第五十七轮：支持中文搜索 —— 占位文案要明说，玩家不会自己去试
        self.search_edit.setPlaceholderText(
            f"搜索{TYPE_NAMES.get(ptype, '内容')}，可直接用中文（如「机械动力」「优化」）…")
        self.search_edit.setFixedHeight(36)
        self.search_edit.returnPressed.connect(self.do_search)
        srow.addWidget(self.search_edit, 1)

        self.dd_version_bar = None
        # 数据包同样按游戏版本筛（1.20.1 的数据包装到 1.21 上不通用）
        if ptype in ("modpack", "mod", "datapack"):
            self.dd_version_bar = self._make_version_dd("版本")
            srow.addWidget(self.dd_version_bar)
        self.dd_cats = self._make_cats_dd()
        srow.addWidget(self.dd_cats)
        btn_search = IconButton("search", 36, 17, color="#2f7cf6")
        btn_search.clicked.connect(self.do_search)
        srow.addWidget(btn_search)
        sc.addLayout(srow)

        prow = QHBoxLayout()
        prow.setSpacing(8)
        if ptype in ("shader", "resourcepack"):
            self.dd_version_low = self._make_version_dd("游戏版本")
            prow.addWidget(self.dd_version_low)
        self.dd_platform = self._make_platform_dd()
        prow.addWidget(self.dd_platform)
        self.dd_sort = self._make_sort_dd()
        prow.addWidget(self.dd_sort)
        if ptype in ("modpack", "mod", "datapack"):
            self.dd_loader = self._make_loader_dd()
            prow.addWidget(self.dd_loader)
        prow.addStretch(1)
        # 翻页：位于「模组加载器」与「导入」之间
        self.pager = Pager(1)
        self.pager.changed.connect(self._goto_page)
        prow.addWidget(self.pager)
        import_name = "导入" + TYPE_NAMES[ptype]
        btn_import = PillButton(import_name, "ghost", icon="folder")
        btn_import.clicked.connect(self._import_file)
        prow.addWidget(btn_import)
        sc.addLayout(prow)

        right.addWidget(search_card)

        # ---- 结果区 ----
        result_wrap = FrostCard(radius=16, tint=110)
        rl = QVBoxLayout(result_wrap)
        rl.setContentsMargins(12, 10, 12, 6)
        # 结果统计
        self.stat = QLabel("")
        self.stat.setStyleSheet("color:#7c8794;font-size:12px;background:transparent;")
        self.stat.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.stat.hide()
        rl.addWidget(self.stat)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:transparent;")
        self.grid = FlowLayout(self.grid_host, cols=3)
        self.grid.setSpacing(12)
        self.scroll.setWidget(self.grid_host)
        rl.addWidget(self.scroll)
        right.addWidget(result_wrap, 1)

        root.addLayout(right, 1)

        self._labels_loaded = False
        self._lazy_init_done = False

    # ---------- 下拉构造 ----------
    # 文案规则：没选过时显示分类名（目标平台 / 排序 / 模组加载器 / 版本 / 筛选），
    # 选了才换成所选选项的名字；清除已选或取消勾选后回到分类名。
    def _make_platform_dd(self):
        items = [(v, l, v == self.state["platform"]) for v, l in PLATFORM_ITEMS]
        return DropDown("目标平台", [("", items)], multi=False,
                        on_change=self._on_platform, width=210, all_label="全部")

    def _make_sort_dd(self):
        items = [(v, l, v == self.state["sort"]) for v, l in SORT_ITEMS]
        return DropDown("相关排序", [("", items)], multi=False,
                        on_change=self._on_sort, width=200, all_label="全部")

    def _loader_items(self):
        return (LOADER_ITEMS_DATAPACK if self.ptype == "datapack"
                else LOADER_ITEMS)

    def _make_loader_dd(self):
        items = [(v, l, v in self.state["loaders"]) for v, l in self._loader_items()]
        return DropDown("模组加载器", [("", items)], multi=True,
                        on_change=self._on_loaders, width=200, all_label="全部")

    def _make_version_dd(self, label):
        dd = DropDown(label, [("", [("_loading", "加载中…", False)])],
                      multi=True, on_change=self._on_versions, width=290,
                      all_label="全部")
        run_async(modrinth.fetch_game_versions, on_done=self._fill_versions,
                  on_err=lambda e: None)
        return dd

    def _fill_versions(self, gv):
        rel = gv.get("release", [])[:120]
        snap = gv.get("snapshot", [])[:60]
        sections = [
            ("发布版本", [(v, v, False) for v in rel]),
            ("快照", [(v, v, False) for v in snap]),
        ]
        dd = self.dd_version_bar or self.dd_version_low
        if dd:
            dd.rebuild(sections)

    def _make_cats_dd(self):
        dd = DropDown("筛选", [("", [("_loading", "加载中…", False)])],
                      multi=True, on_change=self._on_cats, width=280,
                      all_label="全部")
        run_async(modrinth.fetch_categories, self.ptype,
                  on_done=self._fill_cats, on_err=lambda e: None)
        return dd

    def _fill_cats(self, cats):
        if not cats:
            return
        self.dd_cats.rebuild([("", [(s, l, False) for s, l in cats])])

    # ---------- 下拉回调 ----------
    # 文案：没选过时保持分类名，选了显示所选名称，取消后回到分类名（传 None）
    def _on_platform(self, vals):
        # 空 = 全部（双平台合并搜索）
        self.state["platform"] = vals[0] if vals else ALL
        self.dd_platform.set_value_label(
            vals[0] if vals else None)
        self._reset_page_search()

    def _on_sort(self, vals):
        self.state["sort"] = vals[0] if vals else ALL
        label = dict(SORT_ITEMS).get(self.state["sort"]) if vals else None
        self.dd_sort.set_value_label(label)
        self._reset_page_search()

    def _on_loaders(self, vals):
        self.state["loaders"] = vals
        label = "、".join(dict(self._loader_items()).get(v, v) for v in vals)
        self.dd_loader.set_value_label(label if vals else None)
        self._reset_page_search()

    def _on_versions(self, vals):
        self.state["versions"] = vals
        dd = self.dd_version_bar or self.dd_version_low
        label = "、".join(vals[:2]) + ("…" if len(vals) > 2 else "")
        dd.set_value_label(label if vals else None)
        self._reset_page_search()

    def _on_cats(self, vals):
        self.state["categories"] = vals
        label = "、".join(vals[:2]) + ("…" if len(vals) > 2 else "")
        self.dd_cats.set_value_label(label if vals else None)
        self._reset_page_search()

    # ---------- 翻页 ----------
    def _reset_page_search(self):
        """筛选条件变了：回到第 1 页再搜，并清掉总数缓存（换了条件总数就变了）"""
        self.page = 1
        self._mr_total = 0
        if hasattr(self, "pager"):
            self.pager.reset()
        self.do_search()

    def _goto_page(self, page):
        self.page = max(1, int(page))
        # 同步回控件（set_page 不发信号，不会递归触发搜索）。
        # 不同步的话：其它入口调到这里时，控件还显示旧页码，
        # 而 _render_hits 又会用控件页码覆盖 self.page，翻页就"跳回去"了。
        if hasattr(self, "pager") and self.pager.page != self.page:
            self.pager.set_page(self.page)
        self.do_search()

    def _offset(self):
        return (self.page - 1) * self.page_size

    def _query_sig(self):
        """当前查询条件的签名：两源总数缓存只在签名不变时复用"""
        st = self.state
        return (st.get("query"), st.get("platform"), st.get("sort"),
                tuple(st.get("versions") or ()), tuple(st.get("categories") or ()),
                tuple(st.get("loaders") or ()), self.ptype, self.page_size)

    # ---------- 搜索 ----------
    def do_search(self):
        if self._busy:
            return
        self._busy = True
        # 第五十七轮：中文搜索。两个站都只认英文，这里翻一遍再发；
        # 一个候选 0 条就自动退到下一个（末位是原词），见 _hits_or_next。
        raw = self.search_edit.text()
        self.state["query_raw"] = raw
        self._cands = cnsearch.candidates(self.ptype, raw) or [raw]
        self._cand_i = 0
        # 第七十六轮：内置词表 + 原词都 0 条时，还有一次问 BBSMC 的机会
        self._bbsmc_tried = False
        self._cn_hint = cnsearch.explain(self.ptype, raw)
        self.state["query"] = self._cands[0]
        self._start_search()

    def _start_search(self):
        """用当前候选（`_cands[_cand_i]`）真正发一轮请求。

        从 `do_search` 进来时 `_busy` 已经置上；从 `_hits_or_next` 回退进来时
        保持 `_busy` 不变（还没渲染完，不能放行第二次点击）。
        """
        st = self.state
        self._render_loading()
        if st["platform"] == ALL:
            run_async(self._search_merged, on_done=self._hits_or_next,
                      on_err=self._render_err)
        else:
            run_async(modrinth.search, self.ptype, st["query"],
                      categories=st["categories"] or None,
                      versions=st["versions"] or None,
                      loaders=st["loaders"] or None,
                      index=st["sort"] if st["sort"] != ALL else "relevance",
                      limit=self.page_size, offset=self._offset(),
                      on_done=self._hits_or_next, on_err=self._render_err)

    def _hits_or_next(self, data):
        """这一轮 0 条、且还有别的候选 → 换候选再搜一次，否则正常渲染。

        中文词表不可能覆盖所有项目，翻出来的词偶尔一条都不命中；
        与其给玩家一句「没有找到」，不如拿原词（或下一个候选）再试。

        第七十六轮又加一档：内置词表+原词全落空、且这句里有中文时，最后问一次
        BBSMC —— 它库里就是中文标题（`亡者世界` / `尘世之梦` 这种内置词表根本
        收不全），命中的 `slug` 换成英文再搜一轮。这是**异步**的，
        `_busy` 一直保持到真正渲染为止，玩家中途不能再点一次搜索。
        """
        hits = (data or {}).get("hits") or []
        cands = getattr(self, "_cands", None) or []
        i = int(getattr(self, "_cand_i", 0) or 0)
        if not hits and i + 1 < len(cands):
            self._cand_i = i + 1
            self.state["query"] = cands[self._cand_i]
            self._start_search()
            return
        raw = str(self.state.get("query_raw") or "")
        if (not hits and cnsearch.has_chinese(raw)
                and not getattr(self, "_bbsmc_tried", False)):
            self._bbsmc_tried = True
            self._search_via_bbsmc(raw, data)
            return
        self._render_hits(data)

    def _search_via_bbsmc(self, raw: str, fallback: dict):
        """拿中文原词问 BBSMC，把命中的 slug 当英文候选再搜一轮。

        `fallback` = 原来那轮的（空）结果 —— BBSMC 也翻不出来时照原样渲染，
        界面上仍然是「没有找到相关内容」，绝不无限重试。
        """
        self._render_loading()

        def done(terms):
            terms = [str(t) for t in (terms or []) if str(t or "").strip()]
            if not terms:
                self._render_hits(fallback)
                return
            self._cands = terms
            self._cand_i = 0
            self.state["query"] = terms[0]
            self._start_search()

        def err(_e):
            self._render_hits(fallback)

        run_async(bbsmc.cn_query_terms, raw, self.ptype,
                  on_done=done, on_err=err)


    def _search_merged(self):
        """平台「全部」：开源版只走 Modrinth 一个源。

        保留 mr_total / pages 的返回结构与统计口径，界面无需改动。
        """
        st = self.state
        off = self._offset()
        size = self.page_size
        hits = []
        # 缓存的总数只在「同一组查询条件」下有效
        sig = self._query_sig()
        if sig != getattr(self, "_sig", None):
            self._sig = sig
            self._mr_total = 0
        mr_total = int(getattr(self, "_mr_total", 0) or 0)

        if not mr_total or off < mr_total:
            first = modrinth.search(self.ptype, st["query"],
                                    categories=st["categories"] or None,
                                    versions=st["versions"] or None,
                                    loaders=st["loaders"] or None,
                                    index=st["sort"] if st["sort"] != ALL else "relevance",
                                    limit=size, offset=off)
            hits += list(first.get("hits") or [])
            t = int(first.get("total") or 0)
            if t:
                mr_total = t
            self._mr_total = mr_total

        total = mr_total or len(hits)
        pages = max(1, (mr_total + size - 1) // size) if mr_total else 1

        seen = set()
        uniq = []
        for h in hits:
            key = str(h.get("project_id") or h.get("id") or h.get("slug") or h.get("title"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(h)
        return {"hits": uniq, "total": total, "pages": pages,
                "mr_total": mr_total}

    def _render_loading(self):
        self._clear_grid()
        try:
            self.stat.hide()
        except AttributeError:
            pass
        # 第八十二轮：**带父窗创建**。无父 QLabel 就是一只顶层窗口，addWidget 之后
        # 若在排队 show 送达前被摘离，就会变成一块空白标题栏小窗（见 _drop_rows）
        tip = QLabel("正在搜索…", self.grid_host)
        tip.setStyleSheet("color:#5a6675;font-size:13px;")
        tip.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(tip)
        self.grid.addStretch(1)

    def _render_err(self, e):
        self._busy = False
        self._clear_grid()
        try:
            self.pager.set_total(0, self.page_size)     # 出错时翻页不可用
        except (AttributeError, RuntimeError):
            pass
        try:
            self.stat.hide()
        except AttributeError:
            pass
        # 网络类错误翻成人话，并给一个「重试」按钮，别让用户只能干看着报错
        msg = modrinth.friendly_error(e)
        box = QWidget(self.grid_host)          # 第八十二轮：带父窗创建（见 _drop_rows）
        box.setStyleSheet("background:transparent;")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 24, 0, 0)
        v.setSpacing(10)
        tip = QLabel(f"搜索失败：{msg}", box)
        tip.setStyleSheet("color:#e84a42;font-size:13px;")
        tip.setAlignment(Qt.AlignCenter)
        tip.setWordWrap(True)
        v.addWidget(tip)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = PillButton("重试", "accent", icon="search")
        btn.clicked.connect(self.do_search)
        row.addWidget(btn)
        row.addStretch(1)
        v.addLayout(row)
        self.grid.addWidget(box)
        self.grid.addStretch(1)

    def _clear_grid(self):
        """清空网格：卡片一并销毁

        第五十九轮起把卡片列表也置空再 `_drop_rows()` —— 网格会按窗口宽度
        重建行，如果这里只删 widget 而忘了清 `_cards`，下一次重建会去碰已被
        Qt 销毁的对象（RuntimeError: Internal C++ object already deleted）。
        """
        try:
            self.grid._cards = []
            self.grid._drop_rows()
        except Exception:
            # 兜底：沿用老办法硬清一遍（第八十二轮：一定要先 hide，否则排队中的
            # show 会在控件被摘成顶层之后把它显示成一块空白标题栏窗口）
            while self.grid.count():
                item = self.grid.takeAt(0)
                w = item.widget()
                if w:
                    w.hide()
                    w.deleteLater()
                elif item.layout():
                    while item.layout().count():
                        sub = item.layout().takeAt(0)
                        if sub.widget():
                            sub.widget().hide()
                            sub.widget().deleteLater()

    def _sync_cols(self, immediate=False):
        """按内容区实际宽度调整列数。

        `immediate=True` 用于「刚渲染完一批结果」：此时还没触发 resizeEvent，
        要立刻按当前宽度定好列数，否则会先按 3 列铺一遍再跳一下。
        窗口拖动时走 80ms 防抖 —— 每帧重建布局会明显卡顿。
        """
        grid = getattr(self, "grid", None)
        if grid is None:
            return
        # 没有卡片时不能重建：这时候网格里放的是「加载中 / 出错 / 没找到」
        # 的提示 label，重建会把它们当游离 widget 销毁，页面直接变空白。
        if not getattr(grid, "_cards", None):
            return
        try:
            base = self.scroll.viewport().width()
        except (AttributeError, RuntimeError):
            base = self.grid_host.width() if hasattr(self, "grid_host") else 0
        want = FlowLayout.cols_for(base)
        if want == grid.cols:
            return
        if immediate:
            grid.set_cols(want)
            return
        self._pending_cols = want
        if not hasattr(self, "_col_timer"):
            self._col_timer = QTimer(self)
            self._col_timer.setSingleShot(True)
            self._col_timer.setInterval(80)
            self._col_timer.timeout.connect(self._apply_cols)
        self._col_timer.start()

    def _apply_cols(self):
        grid = getattr(self, "grid", None)
        want = getattr(self, "_pending_cols", 0)
        if grid is None or not want:
            return
        try:
            grid.set_cols(want)
        except RuntimeError:
            pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._sync_cols()

    def _render_hits(self, data):
        self._busy = False
        self.hits = data.get("hits", [])
        # 翻页上限按实际总数算（不是按当前页条数），末页右箭头会自动置灰禁用
        self.total_hits = int(data.get("total") or len(self.hits))
        try:
            pages = int(data.get("pages") or 0)
            if pages:
                # 「全部」：页数由两源页数取大，不能用 总数/每页条数 反推
                self.pager.set_pages(pages)
            else:
                self.pager.set_total(self.total_hits, self.page_size)
            if self.pager.page != self.page:
                self.page = self.pager.page
        except (AttributeError, RuntimeError):
            pass
        self._update_stat(data)
        self._clear_grid()
        if not self.hits:
            tip = QLabel("没有找到相关内容，试试调整筛选条件或换个关键词",
                         self.grid_host)
            tip.setStyleSheet("color:#5a6675;font-size:13px;")
            tip.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(tip)
            self.grid.addStretch(1)
            return
        cards = []
        for h in self.hits:
            c = ResultCard(h, self)
            cards.append(c)
        self.grid.add_cards(cards)
        # 刚铺完就按当前宽度定一次列数，免得先按旧列数闪一下再跳
        self._sync_cols(immediate=True)
        self.grid.addStretch(1)
        # 第七十六轮：先让玩家立刻看到结果（可能还是英文名），再后台问 BBSMC
        # 补中文名，补到了就地换字 —— 不让网络等待挡住首屏。
        self._fill_cn_async()

    def _fill_cn_async(self):
        """后台补中文名（BBSMC），补到就让卡片自己刷新，不重建列表。

        用代数（`_cn_gen`）防串台：玩家翻页/换词之后，上一轮的回调**绝不能**
        去改新一页的卡片 —— 那会让新结果莫名其妙变成上一批的中文名。
        """
        hits = list(self.hits or [])
        if not hits:
            return
        query = str(self.state.get("query_raw") or self.state.get("query") or "")
        ptype = self.ptype
        self._cn_gen = int(getattr(self, "_cn_gen", 0) or 0) + 1
        gen = self._cn_gen

        def done(stat):
            if gen != getattr(self, "_cn_gen", 0):
                return
            if not stat or not stat.get("added"):
                return
            for card in self.findChildren(ResultCard):
                card.refresh_cn()

        run_async(bbsmc.fill_cn, hits, query, ptype,
                  on_done=done, on_err=lambda _e: None)

    def _update_stat(self, data):
        """结果统计：「全部」时把两个源分开列出来，用户能直接看到 CF 被算进去了

        第五十七轮：中文关键词被翻译过时，把实际用的英文查询也标出来 ——
        否则玩家看到「共 1,203 个」不知道搜索框里的中文到底被当成了什么。
        """
        stat = getattr(self, "stat", None)
        if stat is None:
            return
        total = int(data.get("total") or 0)
        if not total:
            stat.hide()
            return
        mr = data.get("mr_total")
        parts = []
        if mr:
            parts.append(f"Modrinth {int(mr):,}")
        text = f"共 {total:,} 个结果"
        if parts:
            text += "（" + " · ".join(parts) + "）"
        hint = str(getattr(self, "_cn_hint", "") or "")
        used = str(self.state.get("query") or "")
        raw = str(self.state.get("query_raw") or "")
        if hint and raw:
            if used and used.lower() != raw.lower():
                text += f"　·　中文已译为「{used}」"
            elif used.strip() == raw.strip():
                # 译名 0 条、已经退回原词搜了 —— 别让玩家以为那句中文被无视了
                text += f"　·　「{hint}」没结果，已按原词搜"
        stat.setText(text)
        stat.show()

    def refresh_installed(self):
        """只刷新按钮的「已安装 / 下载」状态（不重新发起搜索）。

        玩家在资源管理器里删掉整合包或模组后回到本页，按钮立刻变回「下载」。
        """
        for card in self.findChildren(ResultCard):
            try:
                card.sync_installed()
            except Exception:
                pass

    # ---------- 下载安装 ----------
    def download_hit(self, hit, version=None):
        """下载并安装。

        `version`（第五十八轮）= 详情页「往期版本」里选中的那一行
        （`modrinth.version_rows()` 的行）；不给就自动挑最新兼容版本。

        第六十轮：除整合包外，先弹一次「版本匹配确认」—— 玩家核对「所选游戏的
        MC 版本 + 加载器」和「这个文件支持的版本」对不对得上，还能就地换目标
        游戏；确认之后才真的开始下载。挑自动版本时也改用**所选游戏**的版本号
        过滤（以前只看侧栏筛选，没设就等于不筛，于是经常下到别的 MC 版本的 jar）。

        第六十一轮：所选游戏是原版时，模组 / 光影直接挡掉并说明原因
        （数据包看 MC 版本 —— 1.13 才有；材质包原版就能用）。
        """
        if self._dl_busy():
            return
        note = gamelaunch.caps_note(self.ptype, target_caps())
        if note:
            self._dl_reset()
            Toast.show_toast(self.window_ref, note[:80], ms=4200, kind="error")
            return
        target = {}
        if self.ptype != "modpack":
            from .vercheck import ask as ask_match
            tgt, go = ask_match(
                self.window_ref, self.ptype, hit,
                (hit.get("title") or hit.get("name") or "内容"))
            if not go:
                self._dl_reset()
                return
            target = tgt or {}
        pid = hit.get("project_id") or hit.get("id")
        name = (hit.get("title") or hit.get("name") or "内容").strip()
        suffix = f"（{version.get('label')}）" if version else ""
        task = TASKS.create(f"{TYPE_NAMES[self.ptype]}：{name[:20]}{suffix}"[:40],
                            self.ptype)
        dlg = ProgressDailog(self.window_ref, title=f"下载{TYPE_NAMES[self.ptype]}")
        state = {}

        def finished(t):
            self._dl_reset()
            if t.status != "done":
                # 装包失败 / 被取消：不要留着一个「等装完再汉化」的悬空状态，
                # 否则玩家下次点别的整合包的「下载」时会莫名被续上一段汉化
                self._pending_lang = None
                return
            res = state.get("res")
            if not res:
                self._pending_lang = None
                return
            if self.ptype == "modpack" and not res.get("simple"):
                try:
                    dlg.hide()             # 先收起进度窗，避免和后续弹窗叠在一起
                except Exception:
                    pass
                self._ask_pack_memory(res)
                self._pack_result_dialog(res)
            else:
                Toast.show_toast(
                    self.window_ref,
                    f"已安装到 {os.path.basename(res.get('dest_dir', ''))}，官方启动器可直接使用")
            # 第七十六轮：这次下载是「点汉化」带出来的 → 装完接着打汉化
            pending = getattr(self, "_pending_lang", None)
            self._pending_lang = None
            if pending and self.ptype == "modpack":
                inst = str(res.get("instance_dir") or "") or \
                    self._pack_instance_dir(pending.get("pack") or hit)
                if inst and os.path.isdir(inst):
                    self._run_langpack(pending.get("pack") or hit,
                                       pending.get("ver") or {},
                                       pending.get("proj") or {}, inst)
                else:
                    Toast.show_toast(self.window_ref,
                                     "整合包装好了，但没找到实例目录，汉化请重试一次",
                                     ms=4000, kind="error")

        dlg.bind(task, on_finished=finished)
        dlg.show()

        def work():
            st = self.state
            # ---- ① 玩家选了往期版本：照那一行下，不再自动匹配 ----
            if version:
                row = version
                raw = row.get("raw") or {}
                TASKS.set_phase(task.tid, "获取下载地址")
                f = raw.get("file") or {}
                url = str(f.get("url") or "")
                fname = str(f.get("filename") or "")
                fsize = int(f.get("size") or 0)
                if not url:
                    raise RuntimeError("这个版本没有可下载文件（可能已被作者删除）")
                if not fname:
                    from urllib.parse import unquote
                    fname = os.path.basename(unquote(url.split("?")[0])) or "download.zip"
                dest = os.path.join(installer.downloads_dir(), fname)
                TASKS.set_phase(task.tid, "下载中")
                modrinth.download_to(url, dest, progress_cb=progress_for(task.tid),
                                     cancel_flag=task.cancel_flag,
                                     expected_size=fsize)
                TASKS.set_details(task.tid, [
                    {"name": row.get("label") or fname, "size": fsize, "status": "done"}])
                return dest, {"version": row.get("label")}, False
            loaders = st["loaders"] or None
            versions = st["versions"] or None
            if self.ptype in ("modpack", "mod", "datapack"):
                versions = versions or None
            # 第六十轮：侧栏没设版本筛选时，用**所选游戏**的 MC 版本兜底 ——
            # 否则「自动」等于挑这个项目最新的一次发布，很容易下到别的 MC 版本。
            if not versions and target.get("mc"):
                versions = [target["mc"]]
            if not loaders and target.get("loader"):
                loaders = [target["loader"]]
            TASKS.set_phase(task.tid, "解析可下载文件")
            resolved = modrinth.best_file_for(pid, loaders=loaders, game_versions=versions)
            if not resolved:
                raise RuntimeError("未找到可用版本文件")
            ver, url, fname = resolved
            try:
                fsize = int(((ver.get("files") or [{}])[0]).get("size") or 0)
            except Exception:                       # noqa: BLE001
                fsize = 0
            dest = os.path.join(installer.downloads_dir(), fname or "download.zip")
            TASKS.set_phase(task.tid, "下载中")
            modrinth.download_to(url, dest, progress_cb=progress_for(task.tid),
                                 cancel_flag=task.cancel_flag, expected_size=fsize)
            return dest, ver, False

        def done(res):
            dest, ver, _is_cf = res
            # 第五十七轮：保存前先确认落点（按所选游戏分配），模组/数据包可改名。
            # 整合包不走这条路 —— 它是整个实例，落点由实例名决定。
            plan = {}
            if self.ptype != "modpack":
                plan = self._ask_save_plan(dest, hit, target=target)
                if plan is None:
                    TASKS.finish(task.tid, "cancelled", "已取消保存")
                    return
            self._install_path(dest, hit, task, state, plan=plan)

        def err(e):
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
            else:
                TASKS.finish(task.tid, "failed", msg[:120])
                Toast.show_toast(self.window_ref, f"下载失败：{msg[:100]}")

        run_async(work, on_done=done, on_err=err)

    # ---------- 拖进来的整合包（第六十二轮）----------
    def install_local_pack(self, path):
        """本地整合包（玩家拖进窗口的那个）→ 走与下载完全一样的安装流程。

        为什么不另写一套：进度窗、逐文件明细、内存分配、结果弹窗、
        「已注册到官方启动器」这些话术全在 `_install_path` 里，复制一份迟早
        两边跑偏。这里只要开个任务、把路径喂进去即可。
        """
        name = os.path.basename(str(path or "")) or "整合包"
        task = TASKS.create(f"整合包：{name[:20]}"[:40], "modpack")
        dlg = ProgressDailog(self.window_ref, title="安装整合包")
        state = {}

        def finished(t):
            self._dl_reset()
            if t.status != "done":
                return
            res = state.get("res")
            if not res:
                return
            try:
                dlg.hide()                 # 先收起进度窗，避免和后续弹窗叠在一起
            except Exception:              # noqa: BLE001
                pass
            self._ask_pack_memory(res)
            self._pack_result_dialog(res)

        dlg.bind(task, on_finished=finished)
        dlg.show()
        self._install_path(path, None, task, state, plan={}, as_pack=True)

    # ---------- 依赖下载（详情窗里的前置/下游）----------
    def download_dep(self, row):
        self.download_deps([row])

    def download_deps(self, rows):
        """把依赖项直接下到对应内容目录（模组->mods，光影->shaderpacks…）"""
        from urllib.parse import unquote

        rows = [r for r in (rows or []) if r.get("url")]
        if not rows:
            Toast.show_toast(self.window_ref, "没有可直接下载的依赖项")
            return
        # 原版装不了模组，那「自动补充前置」也没意义 —— 一样挡掉
        note = gamelaunch.caps_note(self.ptype, target_caps())
        if note:
            Toast.show_toast(self.window_ref, note[:80], ms=4200, kind="error")
            return
        if self._dl_busy():
            return
        label = str(rows[0].get("name") or "依赖")
        title = label if len(rows) == 1 else f"{len(rows)} 个依赖"
        task = TASKS.create(f"依赖：{title[:16]}", self.ptype)
        dlg = ProgressDailog(self.window_ref, title="下载依赖")
        state = {"ok": 0, "fail": [], "n": len(rows)}

        def finished(t):
            self._dl_reset()
            if t.status != "done":
                return
            okn, fails = state.get("ok", 0), state.get("fail") or []
            if fails:
                Toast.show_toast(
                    self.window_ref,
                    f"已装 {okn} 个，{len(fails)} 个失败：{fails[0][1][:40]}")
            else:
                Toast.show_toast(self.window_ref,
                                 f"已安装 {okn} 个依赖，官方启动器可直接使用")

        dlg.bind(task, on_finished=finished)
        dlg.show()

        kind = self.ptype if self.ptype in ("mod", "shader", "resourcepack",
                                            "datapack") else "mod"

        def work():
            TASKS.set_phase(task.tid, "准备依赖")
            TASKS.set_details(task.tid, [
                {"name": r.get("name") or "依赖", "size": int(r.get("size") or 0),
                 "status": "wait"} for r in rows])
            jobs = []
            for i, r in enumerate(rows):
                base = os.path.basename(unquote((r.get("url") or "").split("?")[0]))
                if not base or not os.path.splitext(base)[1]:
                    base = f"{r.get('name') or 'dep'}{i}.jar"
                jobs.append({"index": i, "url": r["url"],
                             "dest": os.path.join(installer.downloads_dir(), base),
                             "size": int(r.get("size") or 0)})
            TASKS.set_phase(task.tid, "下载依赖")
            ok, failed = dl.download_many(
                jobs, cancel_flag=task.cancel_flag,
                on_item=lambda i, st, note="": TASKS.mark_detail(task.tid, i, st, note),
                on_bytes=progress_for(task.tid))
            state["ok"], state["fail"] = ok, failed
            if failed and ok == 0:
                raise RuntimeError(failed[0][1] or "全部依赖下载失败")
            TASKS.set_phase(task.tid, "复制到内容目录")
            # 第五十七轮：依赖也跟着「所选游戏」走 —— 前置装错文件夹等于没装，
            # 主模组进了 <实例>/mods，前置却躺在官方根目录是最坑人的一种错配。
            inst = gamelaunch.target_instance_dir()
            done_n = 0
            for j in jobs:
                if any(j["index"] == fi for fi, _ in failed):
                    continue
                try:
                    installer.install_simple(kind, j["dest"], progress_cb=None,
                                             instance=inst)
                    done_n += 1
                except Exception as e:                 # noqa: BLE001
                    failed.append((j["index"], str(e)[:60]))
            state["ok"] = done_n
            return done_n

        def done(n):
            TASKS.finish(task.tid, "done",
                         f"已安装 {n} 个依赖" if n else "没有成功安装的依赖")

        def err(e):
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
            else:
                TASKS.finish(task.tid, "failed", msg[:120])
                Toast.show_toast(self.window_ref, f"依赖下载失败：{msg[:80]}")

        run_async(work, on_done=done, on_err=err)

    def _dl_busy(self):
        # 允许少量并发（后台下载时还能继续挑别的），过多则提示
        if TASKS.running_count() >= 3:
            Toast.show_toast(self.window_ref, "已有 3 个任务在进行，请稍候或先取消")
            return True
        self._dl_running = True
        def reset():
            self._dl_running = False
        self._dl_reset = reset
        return False

    # ---------- 汉化补丁（第七十六轮）----------
    def _pack_instance_dir(self, hit) -> str:
        """这个整合包装在哪个目录（没装返回空串）。

        只认磁盘上真在的目录：玩家在资源管理器里删了实例，这里就该返回空，
        于是「汉化」会自动变成「先装包再汉化」，而不是往一个不存在的路径里写。
        """
        pid = str((hit or {}).get("project_id") or (hit or {}).get("id") or "")
        if not pid:
            return ""
        try:
            rec = CONFIG.installed_record(pid) or {}
        except Exception:                              # noqa: BLE001
            rec = {}
        p = str(rec.get("path") or "")
        return p if p and os.path.isdir(p) else ""

    def start_langpack(self, hit, picked=None, lang_ver=None, lang_proj=None,
                       auto=False):
        """详情窗「汉化」按钮的落点。

        第七十六轮定的交互：**没装就先装，装完自动接着打汉化**（玩家点一次
        就把两件事办完，不用「先下包→再回来点汉化」跑两趟）。已经装了就直接
        进汉化流程。
        """
        if not lang_ver:
            Toast.show_toast(self.window_ref, "没有可用的汉化包", ms=3200,
                             kind="error")
            return
        if self._dl_busy():
            return
        if auto:
            Toast.show_toast(
                self.window_ref,
                f"所选版本没有对应汉化，按最新汉化处理：{(lang_ver.get('name') or '')[:24]}",
                ms=4200)
        inst = self._pack_instance_dir(hit) if self.ptype == "modpack" else ""
        if not inst:
            self._pending_lang = {"pack": hit, "ver": lang_ver,
                                  "proj": lang_proj or {}}
            Toast.show_toast(self.window_ref,
                             "整合包还没装：先装包，装完自动接着打汉化", ms=3600)
            self.download_hit(hit, version=picked)
            return
        self._run_langpack(hit, lang_ver, lang_proj or {}, inst)

    def _run_langpack(self, hit, lang_ver, lang_proj, inst_dir: str):
        """下载 → 解压 → 让玩家核对 → 覆盖进整合包目录。"""
        import shutil

        from .langpack import LangPackDialog, scan_rows
        url = str(lang_ver.get("file_url") or "")
        fname = str(lang_ver.get("file_name") or "langpack.zip")
        fsize = int(lang_ver.get("file_size") or 0)
        page = bbsmc.language_page(lang_proj or {})
        name = str((hit or {}).get("title") or (hit or {}).get("name") or "整合包")
        ver_label = str(lang_ver.get("ver_text") or "")
        if ver_label:
            ver_label = f"对应整合包 {ver_label}"

        task = TASKS.create(f"汉化：{name[:18]}"[:40], "modpack")
        dlg = ProgressDailog(self.window_ref, title="汉化补丁")
        base = installer.downloads_dir()
        tmp_dir = os.path.join(base, "_langtmp")
        zip_path = os.path.join(base, "_langpack.zip")
        state = {}

        def work():
            TASKS.set_phase(task.tid, "下载汉化包")
            os.makedirs(base, exist_ok=True)
            bbsmc.download(url, zip_path,
                           progress_cb=progress_for(task.tid),
                           cancel_flag=task.cancel_flag,
                           expected_size=fsize)
            TASKS.set_phase(task.tid, "解压汉化包")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            rows = bbsmc.unpack(zip_path, tmp_dir)
            return {"rows": rows}

        def done(res):
            self._dl_reset()
            rows = (res or {}).get("rows") or []
            if not rows:
                TASKS.finish(task.tid, "failed", "汉化包里没有文件")
                self._lang_offline(hit, lang_ver, lang_proj, inst_dir,
                                   "汉化包解开之后一个文件都没有")
                return
            over = sum(1 for r in scan_rows(rows, inst_dir) if r["exists"])
            TASKS.finish(task.tid, "done",
                         f"{len(rows)} 个文件待核对（{over} 个会覆盖）")
            try:
                dlg.hide()          # 先收进度窗，别和核对窗叠在一起
            except Exception:                      # noqa: BLE001
                pass
            self._lang_review(name, ver_label, lang_ver, rows, tmp_dir,
                              inst_dir, page)
            # zip 无论成没成都清掉：占了下载目录几十 MB，
            # 玩家下次看一眼「下载」文件夹发现一堆 _lang* 会莫名其妙
            # （临时解压目录由 _lang_review 自己收）
            try:
                if os.path.isfile(zip_path):
                    os.remove(zip_path)
            except Exception:                      # noqa: BLE001
                pass

        def err(e):
            self._dl_reset()
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
                return
            TASKS.finish(task.tid, "failed", msg[:120])
            try:
                dlg.hide()
            except Exception:                      # noqa: BLE001
                pass
            # 1.5.1：连不上 CDN 时不再只丢一句红字 —— 直接给「打开网页 / 选本地
            # 文件」两条活路（这个 CDN 在国内整条 IP 不可达，死磕重试没意义）
            self._lang_offline(hit, lang_ver, lang_proj, inst_dir, msg)

        dlg.bind(task, on_finished=lambda t: None)
        dlg.show()
        run_async(work, on_done=done, on_err=err)

    def _lang_review(self, name, ver_label, lang_ver, rows, tmp_dir,
                     inst_dir, page) -> bool:
        """摊开文件夹树让玩家核对，确认就覆盖。**返回是否真的写进去了。**

        真正的实现在 `langpack.review_rows()` —— 第七十八轮起有三条入口
        （自己下载 / 下载失败选本地 zip / 游戏目录里的「汉化补丁」），
        核对这一步必须**共用一份**，否则谁多删了临时目录、谁的汇总算法不一样，
        玩家看到的行为就会莫名其妙地不一致。
        """
        from .langpack import review_rows
        return review_rows(self.window_ref, name, ver_label, rows, tmp_dir,
                           inst_dir, lang_ver=lang_ver, page_url=page)

    def _lang_offline(self, hit, lang_ver, lang_proj, inst_dir, reason):
        """下载走不通时的兜底：引导浏览器下载 / 收下玩家已经下好的 zip。

        第七十六轮补丁（1.5.1）。`cdn.bbsmc.net` 只有一条 CNAME 链，最终落到
        一个在国内**整个不可达**的香港 IP（202.189.5.156，443/80 都是纯 TCP
        超时，三个公共 DNS 返回的还都是它）。所以「重试」「换 UA」救不了 ——
        唯一稳定可行的路是让浏览器去下（浏览器走系统代理/加速器），再把文件
        交回启动器。这个函数就是那条路的入口。
        """
        from .langpack import LangFetchFailedDialog, open_local_zip
        name = str((hit or {}).get("title") or (hit or {}).get("name") or "整合包")
        page = bbsmc.language_page(lang_proj or {})
        ver_label = str(lang_ver.get("ver_text") or "")
        if ver_label:
            ver_label = f"对应整合包 {ver_label}"

        d = LangFetchFailedDialog(self.window_ref, name, reason, page)
        d.exec()
        if d.choice != "file":
            return
        # 选 zip → 校验 → 解包 → 文件夹树核对，全在 langpack.open_local_zip 里
        # （和「游戏目录 → 汉化补丁」走的是同一条路）
        open_local_zip(self.window_ref, inst_dir, pack_name=name,
                       pack_ver=ver_label, page_url=page)

    def _ask_save_plan(self, src_path, hit=None, target=None):
        """保存前的确认：按所选游戏算落点，模组/数据包可改名。

        `target`（第六十轮）= 版本确认窗里玩家最后选定的那个目标
        （可能在窗里被手动换成了别的实例）；不给就退回「当前所选游戏」。

        返回 `{"instance": 实例目录, "name": 文件名, "dir": 目标文件夹}`；
        玩家点「取消」返回 None（任务按「已取消」收尾，不装）。
        """
        if target:
            inst = str(target.get("instance") or "")
            label = str(target.get("label") or "")
        else:
            inst = gamelaunch.target_instance_dir()
            label = gamelaunch.target_label() if inst else ""
        try:
            dest_dir = installer.content_dir(self.ptype,
                                             installer.download_dir(), inst)
        except Exception:                            # noqa: BLE001
            # 数据包在「没选游戏 + 目录里没存档」时没有合法落点；其它类型抛错
            # 多半是目标盘只读 / 没权限 —— 都退到下载根目录下对应的子文件夹，
            # 别让一句异常把整个安装卡死（对话框里会如实显示这个兜底路径）。
            sub = installer.CONTENT_DIRS.get(self.ptype, "datapacks")
            dest_dir = os.path.join(installer.download_dir(), sub)
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except OSError:
                pass
        # 第五十八轮：模组/数据包落盘名带中文前缀 `[中文名]原名`。
        # 光影/材质包**绝不动名字** —— 它们的内容里写着自己的文件名。
        base = os.path.basename(str(src_path or ""))
        if hit is not None and self.ptype in ("mod", "datapack"):
            base = cn_names.prefixed_name(hit, base) or base
        from .installto import ContentSaveDialog
        dlg = ContentSaveDialog(self.window_ref, self.ptype, base, dest_dir, label)
        if dlg.exec() != ContentSaveDialog.Accepted:
            return None
        return {"instance": inst, "name": dlg.file_name(), "dir": dest_dir}

    def _drop_old_copy(self, pid, new_path, dest_dir):
        """换版本时把同一项目上一版的文件删掉。

        第五十八轮：详情页能选往期版本了，`[中文名]原名` 又让文件名随版本变，
        新旧两份 jar 都躺在 mods 里 = 同一个 modid 加载两次，游戏直接崩。
        只删**我们记录过、且确实在目标文件夹里**的旧文件，别的一概不碰。
        """
        if not pid or not new_path:
            return ""
        rec = CONFIG.installed_record(pid) or {}
        old = str(rec.get("path") or "")
        if not old or os.path.normcase(old) == os.path.normcase(new_path):
            return ""
        try:
            if not os.path.isfile(old):
                return ""
            if dest_dir and os.path.normcase(
                    os.path.dirname(old)) != os.path.normcase(dest_dir):
                return ""
            if os.path.splitext(old)[1].lower() not in (".jar", ".zip"):
                return ""
            os.remove(old)
            return os.path.basename(old)
        except OSError:
            return ""

    def _install_path(self, path, hit, task, state=None, plan=None, as_pack=False):
        """下载完成后安装（或直接由导入/拖放调用）；沿用同一个任务继续报进度。

        `plan`（第五十七轮）= `_ask_save_plan()` 的结果：目标实例目录 + 文件名。
        没给就按老路子装到全局游戏目录、用源文件名。

        `as_pack`（第六十二轮）= 这次装的是**整合包**，跟当前停在哪一页无关。
        以前这里只看 `self.ptype`，于是玩家在「模组」页把整合包拖进来时，那个 zip 会被
        `install_simple("mod", ...)` 原样复制进 `mods/` —— 一份整合包当模组装，游戏当然起不来。
        """
        state = state if state is not None else {}
        plan = plan or {}
        inst = str(plan.get("instance") or "")
        dest_name = str(plan.get("name") or "")
        is_pack = bool(as_pack) or self.ptype == "modpack"

        def progress(done, total, text=""):
            TASKS.set_items(task.tid, done, total, phase=text or "安装中")

        def work():
            # 第四十五轮：装到「官方」还是「应用」，由主页那两个胶囊决定
            gdir = installer.download_dir()
            if is_pack:
                TASKS.set_phase(task.tid, "解压与安装")
                return installer.install_modpack(
                    path, progress_cb=progress, cancel_flag=task.cancel_flag,
                    on_details=lambda rows: TASKS.set_details(task.tid, rows),
                    on_detail=lambda i, st, note="": TASKS.mark_detail(
                        task.tid, i, st, note),
                    bytes_cb=progress_for(task.tid),
                    # 顺手把项目图标存成 <实例>/icon.png：游戏管理里显示整合包自己的图标
                    icon_url=(hit.get("icon_url") or "") if hit else "",
                    game_dir=gdir)
            TASKS.set_phase(task.tid, "复制文件")
            dest = installer.install_simple(
                {"mod": "mod", "shader": "shader", "resourcepack": "resourcepack",
                 "datapack": "datapack"}[self.ptype],
                path, game_dir=gdir, progress_cb=None,
                dest_name=dest_name, instance=inst,
                target_dir=str(plan.get("dir") or ""))
            return {"name": dest_name or os.path.basename(path), "simple": True,
                    "dest_dir": dest, "file": os.path.join(
                        dest, installer.safe_dest_name(dest_name, path))}

        def done(res):
            pid = hit.get("project_id") or hit.get("id") if hit else None
            dropped = ""
            if pid and not is_pack:
                # 换版本（往期版本 / 改名）时清掉上一份，避免同 modid 加载两次
                dropped = self._drop_old_copy(str(pid), res.get("file") or "",
                                              res.get("dest_dir") or "")
            if pid:
                # 记下真实路径：玩家以后在资源管理器里删掉它，按钮会自动变回「下载」
                # 第六十五轮：顺手记下官网图标地址 —— 文件列表里包里没图标时用它兜底，
                # 省得以后再去 API 查一次（离线也能显示，只要缓存里有图）。
                CONFIG.mark_installed(str(pid), {
                    "name": res.get("name", ""),
                    "kind": self.ptype,
                    "path": res.get("instance_dir") or res.get("file") or "",
                    "icon_url": (hit.get("icon_url") or "") if hit else "",
                })
            state["res"] = res
            TASKS.finish(task.tid, "done",
                         "已装好，可直接启动" if res.get("launch_target")
                         else ("已注册到官方启动器" if res.get("profile_ok")
                               else "已放入游戏目录"))
            if dropped:
                Toast.show_toast(self.window_ref,
                                 f"已用新版本替换旧文件 {dropped}"[:70])
            self.do_search()
            # 第五十七轮：装完让玩家立刻能看见文件在里面。
            # 第六十轮：改叫内置文件管理器（会高亮刚写的那个文件），不再弹系统
            # 资源管理器；整合包是整份实例，装完由结果弹窗接管，不在这里弹。
            if not is_pack:
                folder = res.get("dest_dir") or plan.get("dir") or ""
                open_folder(folder,
                            highlight=os.path.basename(str(res.get("file") or "")),
                            parent=self.window_ref)

        def err(e):
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
            else:
                TASKS.finish(task.tid, "failed", msg[:120])
                Toast.show_toast(self.window_ref, f"安装失败：{msg[:100]}")

        run_async(work, on_done=done, on_err=err)

    def _ask_pack_memory(self, res):
        """整合包没声明运行内存时，弹窗让玩家手动分配（与设置页同一套控件）。

        第一百零五轮：同一个弹窗里顺带问归类。玩家明确要求「下载或导入整合包
        完成后在运行内存分配弹窗内提示是否创建分类」，所以**即使内存已由包
        声明、不需要分配**，也弹这个框（只是把内存控件隐藏）。
        · 没有分类 → 问「请问是否创建分类？」，选是会展开命名框；
        · 已有分类 → 下拉选一个存入，或选「＋ 新建分类…」；
        · 选「否 / 不分类」→ 什么都不建，**绝不影响已完成的下载 / 导入**。
        """
        from .dialogs import PackMemoryDialog
        from ..core import categories as cats
        try:
            ctx = cats.context_from_install(res)
        except Exception:                                   # noqa: BLE001
            ctx = None
        pid = res.get("profile_id") or ""
        need_mem = (not res.get("memory_mb")) and bool(res.get("profile_ok")) \
            and bool(pid)
        if not need_mem and not ctx:
            return
        d = PackMemoryDialog(self.window_ref, res.get("name", "整合包"),
                             show_memory=need_mem, category_ctx=ctx)
        d.exec()
        if ctx:
            ok, note = cats.apply_choice(ctx, d.category_choice)
            res["category_note"] = note
            if not ok:
                Toast.show_toast(self.window_ref, note, ms=3600, kind="error")
        if not need_mem:
            if res.get("memory_mb"):
                return
            if not res.get("profile_ok") or not pid:
                res.setdefault("memory_note", "档案未注册，内存请在设置里分配")
            else:
                res.setdefault("memory_note", "未分配（使用官方启动器默认内存）")
            return
        if d.mb <= 0:
            res["memory_note"] = "未分配（使用官方启动器默认内存）"
            return
        ok, msg = installer.apply_memory_to_profile(pid, d.mb,
                                                   auto_min=d.field.auto_min())
        res["memory_note"] = msg
        if ok:
            res["memory_mb"] = int(d.mb)
            res["memory_src"] = "安装后手动分配"

    def _pack_result_dialog(self, res):
        from .dialogs import _DialogBase
        # 第九十六轮：装完默认就是**独立启动目标**，不再自动把玩家送进官启。
        # 官启档案只是顺带配的一份（配不上也不影响玩）。
        standalone = False
        try:
            standalone = bool(res.get("launch_target")) and gamelaunch.standalone_enabled()
        except Exception:
            standalone = False
        launched = ""
        if not standalone:
            # 只有不走独立启动时才代玩家打开官方启动器；否则点一下按钮就开局了
            try:
                launch_official_async(self.window_ref)
                launched = "已打开官方启动器"
            except Exception:
                pass
        d = _DialogBase(self.window_ref, width=470, title=f"整合包「{res['name']}」已安装")
        lines = []
        lines.append(f"实例目录：{res.get('instance_dir','')}")
        if res.get("version_id"):
            # 实例就在 versions/<包名> 里，官启版本列表能看到它（PCL2 的版本隔离语义）
            lines.append(f"版本列表新增「{res['version_id']}」（选了它启动就是这套存档与模组）")
        if res.get("memory_mb"):
            src = res.get("memory_src") or "整合包声明"
            lines.append(f"运行内存：{int(res['memory_mb'])} MB（{src}，已写入官启 JVM 参数）")
        elif res.get("memory_note"):
            lines.append(f"运行内存：{res['memory_note']}")
        if res.get("category_note"):
            lines.append(f"分类：{res['category_note']}")
        if standalone:
            lines.append("已设为独立启动目标：点下面的「直接启动」，不经过官方启动器。")
        elif res.get("profile_ok"):
            lines.append(launched + "，档案已自动配置完成——" if launched else "")
            lines.append("在官方启动器的档案列表中选择该整合包即可开始游戏。")
        elif launched:
            lines.append(launched + "（档案注册未完成，见下方提示）")
        for n in (res.get("runtime_notes") or [])[:2]:
            lines.append(n)
        if res.get("loader_hint"):
            lines.append(res["loader_hint"])
        for w in res.get("warnings", [])[:3]:
            lines.append("⚠ " + w)
        msg = QLabel("\n\n".join([l for l in lines if l]))
        msg.setWordWrap(True)
        msg.setStyleSheet("color:#2a3442;font-size:12px;")
        d.body.addWidget(msg)
        if standalone:
            d.add_buttons([
                ("好的", "ghost", d.accept),
                ("直接启动", "accent", self._launch_standalone),
            ])
        else:
            d.add_buttons([
                ("打开官启再启动一次", "ghost", self._launch_official),
                ("好的", "accent", d.accept),
            ])
        d.exec()

    def _launch_standalone(self):
        """装完直接开局：走独立启动，不碰官方启动器。"""
        win = self.window_ref
        if gamelaunch.is_running():
            Toast.show_toast(win, f"「{gamelaunch.running_name()}」正在运行，别急",
                             ms=2600, kind="info")
            return
        Toast.show_toast(win, "正在启动游戏（独立启动，不经过官方启动器）…", ms=2000)

        def done(r):
            ok, msg = r if isinstance(r, tuple) and len(r) == 2 else (False, str(r))
            Toast.show_toast(win, msg, ms=3600 if ok else 6200,
                             kind="success" if ok else "error")
            if ok:
                hook = getattr(win, "on_game_launched", None)
                if callable(hook):
                    try:
                        hook(msg)
                    except Exception:
                        pass

        def err(e):
            Toast.show_toast(win, f"独立启动失败：{e}", ms=6200, kind="error")

        run_async(gamelaunch.launch, on_done=done, on_err=err)

    def _launch_official(self):
        launch_official_async(self.window_ref)

    # ---------- 导入 ----------
    def _import_file(self):
        # 用软件内置选择器（系统文件对话框是 shell 窗口，样式不搭且会跑出主窗口）
        from .fspicker import pick_file
        pats = {"modpack": ["*.mrpack", "*.zip"],
                "mod": ["*.jar"],
                "shader": ["*.zip"],
                "resourcepack": ["*.zip"],
                "datapack": ["*.zip"]}[self.ptype]
        path = pick_file(self, f"导入{TYPE_NAMES[self.ptype]}",
                         os.path.expanduser("~"), pats)
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        task = TASKS.create(f"导入{TYPE_NAMES[self.ptype]}：{name[:20]}", self.ptype)
        dlg = ProgressDailog(self.window_ref, title=f"导入{TYPE_NAMES[self.ptype]}")

        def finished(t):
            if t.status == "done":
                Toast.show_toast(self.window_ref, t.message or "导入完成", kind="success")

        dlg.bind(task, on_finished=finished)
        dlg.show()
        # 导入也走同一套「按所选游戏分配 + 保存前确认」：玩家不会觉得
        # 「从网上下的要选目录、从本地拖进来的却直接丢进官方目录」是同一件事
        plan = {}
        if self.ptype != "modpack":
            plan = self._ask_save_plan(path)
            if plan is None:
                TASKS.finish(task.tid, "cancelled", "已取消保存")
                return
        self._install_path(path, None, task, plan=plan)
