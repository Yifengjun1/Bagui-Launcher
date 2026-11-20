# -*- coding: utf-8 -*-
"""各游戏独立设置：左分类 / 右内容的单个游戏设置窗。

从「游戏管理」每一行的齿轮进来，五类：

    概览       一键打开模组 / 存档 / 光影 / 材质包 / 数据包文件夹
    设置       版本隔离胶囊开关
    游戏 Java  跟随全局，或手动指定一个 java.exe
    运行内存   跟随全局 / 自动配置 / 自定义
    导出       把这份游戏打成整合包 zip

三类值全部落在 `core/launchprefs.py` 的「每游戏覆盖」里（存 None / "global"
表示跟随全局，不存当时的快照），独立启动（`core/gamelaunch.py`）只读那一层 ——
所以这里改完，下次点启动就生效，不需要再走一遍全局设置。
"""
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSlider, QSpinBox, QStackedWidget,
                               QVBoxLayout, QWidget)

from ..core import contentcount, gearcache, installer, launchprefs as lp, packexport
from ..core.asyncio_qt import run_async
from . import theme
from .dialogs import _DialogBase, _NAV_QSS, _ScrollPage
from .widgets import (ChoiceExpander, DropDown, ElideLabel, GlyphIcon,
                      PillButton, PillSwitch, Toast)

_TIP = "color:#5a6675;font-size:11px;"


def _alive(w) -> bool:
    """异步结果回来时控件还活着吗（第 105 轮，和 gamemanager 同一口径）。"""
    try:
        import shiboken6
        return shiboken6.isValid(w)
    except Exception:
        return True
_META = "color:#8a95a3;font-size:10.5px;"
_H2 = "color:#2a3442;font-size:12px;font-weight:600;"

_FOLDER_ROWS = (
    # key, 标题, 图标符号, 图标色, 说明, 计数键（contentcount 返回的那一项）
    ("mods", "模组文件夹", "mods", "#5b8f6d", "整合包里的 jar 都在这", "mods"),
    ("saves", "存档文件夹", "world", "#b5764a", "每个世界一个子目录", "saves"),
    ("shaderpacks", "光影文件夹", "shader", "#8a6fb0", "Iris / OptiFine 光影", "shader"),
    ("resourcepacks", "材质包文件夹", "resourcepack", "#4d7fb5", "资源包放这里", "resourcepack"),
    ("datapacks", "数据包文件夹", "datapack", "#b08a3e", "数据包装在世界的 datapacks 里", "datapack"),
)


class _FolderRow(QWidget):
    """概览页的一行：图标 + 名称 + 路径 + 数量 + 打开。整行可点（点哪儿都算点打开）。

    `expandable`（第六十四轮）：左侧多一个小三角，点它就在这行**下面**展开内容 ——
    给「模组文件夹」用：不管内置文件管理器开没开，玩家都能就地看清单、禁用 / 删除，
    不用先点「打开」跳进另一个窗口。三角点击**不**触发打开文件夹。

    第九十一轮：左侧补上**类型图标**（模组/存档/光影/材质包/数据包），右侧补上
    **实际数量**。起因是玩家反馈「非启动器下载的整合包，这几个文件夹的图标显示不全」——
    真相是这一页从来就没有图标（只有小三角和文字），跟实例是谁装的无关；数量则顺便
    回答了「这行是空的还是我打开错了文件夹」。没有三角的行要给三角留同样的宽度，
    五行的图标才落在同一条竖线上。
    """

    def __init__(self, title, desc, path, on_open, note="",
                 expandable=False, on_toggle=None,
                 icon="folder", icon_color="#5b8f6d"):
        super().__init__()
        self.path = path
        self._on_open = on_open
        self._on_toggle = on_toggle
        self._icon_color = icon_color
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(50)
        self.setStyleSheet(
            "QWidget{background:rgba(255,255,255,160);border-radius:10px;}"
            "QWidget:hover{background:rgba(255,255,255,225);}")
        h = QHBoxLayout(self)
        # 有三角没三角，左边都要占住同样的位置（第九十一轮改成占位控件而不是 addSpacing：
        # addSpacing 也会吃一份 layout spacing，两边算出来差 16px，图标就不在同一竖线上了）
        h.setContentsMargins(6, 6, 10, 6)
        h.setSpacing(8)

        self.tri = None
        if expandable:
            from .modlist import TriToggle
            self.tri = TriToggle(tip="展开，就地禁用 / 删除里面的文件",
                                 tip_on="收起这份清单")
            self.tri.clicked.connect(self._toggle)
            h.addWidget(self.tri, 0, Qt.AlignVCenter)
        else:
            slot = QWidget(self)
            slot.setFixedSize(18, 18)
            h.addWidget(slot, 0, Qt.AlignVCenter)

        self.ico = GlyphIcon(icon, size=18, color=icon_color, w=1.8)
        h.addWidget(self.ico, 0, Qt.AlignVCenter)

        info = QVBoxLayout()
        info.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        t = QLabel(title)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;color:#1f2733;")
        top.addWidget(t)
        state = QLabel("")
        exists = bool(path) and os.path.isdir(path)
        state.setText("" if exists else "（未创建）")
        state.setStyleSheet("background:transparent;color:#b08a3e;font-size:10px;")
        top.addWidget(state)
        top.addStretch(1)
        info.addLayout(top)

        sub = ElideLabel(note or desc)
        sub.setStyleSheet("background:transparent;" + _META)
        sub.setToolTip(f"{desc}\n{path}")
        sub.setTextInteractionFlags(Qt.NoTextInteraction)
        info.addWidget(sub)
        h.addLayout(info, 1)

        # 数量：先留空（异步算完再填），别让这几行先摆一个大数字再跳
        self.lb_count = QLabel("")
        self.lb_count.setStyleSheet("background:transparent;" + _META)
        h.addWidget(self.lb_count, 0, Qt.AlignVCenter)

        btn = PillButton("打开", "ghost")
        btn.setFixedHeight(26)
        btn.setMinimumWidth(58)
        btn.clicked.connect(lambda: self._on_open(self.path))
        h.addWidget(btn)

    def set_count(self, n, label="个"):
        """填数量。`n is None` = 还没算出来（保持空白）；0 显示「空」而不是「0 个」。"""
        if n is None:
            self.lb_count.setText("")
            return
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            self.lb_count.setText("空")
            self.lb_count.setStyleSheet("background:transparent;color:#b0b8c2;font-size:10.5px;")
        else:
            self.lb_count.setText(f"{n} {label}")
            self.lb_count.setStyleSheet(
                "background:transparent;color:%s;font-size:10.5px;" % self._icon_color)

    def _toggle(self):
        if self.tri is None:
            return
        on = not self.tri.is_on()
        self.tri.set_on(on)
        if self._on_toggle:
            self._on_toggle(on)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._on_open(self.path)
            e.accept()
            return
        super().mousePressEvent(e)


class _Collapse(QWidget):
    """收放式卡片：标题行（三角 + 标题 + 右侧摘要）+ 一块默认隐藏的内容区。

    第九十三轮加，第一个用户是「修改加载器」。为什么不用 `ChoiceExpander`：
    那个是「一排选项挑一个」，这里展开后是一整块自定义面板（加载器切换 + 版本下拉 +
    安装按钮 + 状态），装不进去。三角仍用 `modlist.TriToggle`，跟概览那行手感一致。
    """

    def __init__(self, title, summary="", parent=None):
        super().__init__(parent)
        from .modlist import TriToggle

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("collapseCard")
        # 只限定 objectName：写 QWidget{...} 会把展开后的每个子 label 都刷上底色
        self.setStyleSheet("#collapseCard{background:rgba(255,255,255,170);"
                           "border-radius:10px;}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 10, 8)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.tri = TriToggle(tip="展开，改这个游戏的 Mod 加载器版本",
                             tip_on="收起")
        self.tri.clicked.connect(self.toggle)
        head.addWidget(self.tri, 0, Qt.AlignVCenter)
        # 标题用 ElideLabel 而不是 QLabel：QLabel 的 minimumSizeHint 就是整行文字宽，
        # 标题一长就把整张卡的最小宽顶上去 —— 页面是 QScrollArea(widgetResizable)，
        # 它不肯把内容压到最小宽以下，结果整块被视口裁掉右边（第 93 轮实测 704 > 520）
        t = ElideLabel(title)
        t.setToolTip(title)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;color:#1f2733;")
        head.addWidget(t, 1)
        # 摘要留在右边（版本号这种短文本，QLabel 的最小宽够用；标题已改成会省略的，
        # 不会再把它一起顶到 700+）
        self.lb_summary = QLabel(summary)
        self.lb_summary.setToolTip(summary)
        self.lb_summary.setStyleSheet("background:transparent;color:#63707f;"
                                      "font-size:10.5px;")
        head.addWidget(self.lb_summary, 0, Qt.AlignVCenter)
        v.addLayout(head)

        self.body = QWidget(self)
        self.body.setStyleSheet("background:transparent;")
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(26, 2, 0, 2)
        self.body_lay.setSpacing(7)
        self.body.setVisible(False)             # 默认收起
        v.addWidget(self.body)

    def set_summary(self, text):
        try:
            self.lb_summary.setText(text)
            self.lb_summary.setToolTip(text)
        except RuntimeError:
            pass

    def is_open(self):
        return self.body.isVisible()

    def toggle(self):
        on = not self.body.isVisible()
        self.tri.set_on(on)
        self.body.setVisible(on)
        self.updateGeometry()
        self._refit()
        if on:
            hook = getattr(self, "on_open", None)
            if callable(hook):
                hook()

    def _refit(self):
        """展开/收起后让外面重算尺寸。

        第九十三轮踩到的：这一页是 `dialogs._ScrollPage`（显式最小高、没有横滚
        动条），只有 `showEvent / resizeEvent` 会调 `fit()`。卡片自己长高之后没人
        叫它，页面还按旧尺寸摆 —— 末行被裁掉半行，「安装到此游戏」整个被挤出可视
        区（看起来就像按钮没画出来）。这里沿父链找到那个滚动页重新 fit 一次。
        """
        sa = None
        w = self.parentWidget()
        for _ in range(10):
            if w is None:
                break
            lay = w.layout()
            if lay is not None:
                try:
                    lay.activate()
                except Exception:                       # noqa: BLE001
                    pass
            if sa is None and type(w).__name__ == "_ScrollPage":
                sa = w
            w = w.parentWidget()
        top = self.window()
        if top is not None and top is not self:
            box = getattr(top, "_fit_box", None)        # 对话框先尽量长高
            if callable(box):
                try:
                    box()
                except Exception:                       # noqa: BLE001
                    pass
        if sa is not None:
            try:
                sa.fit()
            except Exception:                           # noqa: BLE001
                pass


class _LoaderEditor(QWidget):
    """「修改加载器」面板：切 Forge / NeoForge / Fabric → 选版本 → 装到当前游戏。

    装的是**加载器本身**（`core/loader.install_loader`，跑官方安装器 / Fabric meta），
    装完磁盘上多一个 `versions/<新 id>`。安装走后台线程（首次 1~3 分钟，可能还要
    下原版 jar），界面上只留一行状态文字，按钮全程禁用 —— 绝不阻塞主线程。
    """

    KINDS = (("forge", "Forge"), ("neoforge", "NeoForge"), ("fabric", "Fabric"))

    def __init__(self, item, dirs, on_done=None, parent=None):
        super().__init__(parent)
        self.item = dict(item or {})
        self.dirs = dict(dirs or {})
        self._on_done = on_done
        self._kind = str(self.item.get("loader") or "forge").lower()
        if self._kind not in dict(self.KINDS):
            self._kind = "forge"
        self._ver = ""
        self._busy = False
        self._loaded = {}            # kind -> [版本…]

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(7)

        row = QHBoxLayout()
        row.setSpacing(6)
        lb = QLabel("加载器")
        lb.setStyleSheet("background:transparent;" + _META)
        row.addWidget(lb)
        self._kind_btns = {}
        for key, cn in self.KINDS:
            b = PillButton(cn, "ghost")
            b.setFixedHeight(26)
            b.setMinimumWidth(72)
            b.clicked.connect(lambda _c=False, k=key: self.set_kind(k))
            self._kind_btns[key] = b
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        lb2 = QLabel("版本")
        lb2.setStyleSheet("background:transparent;" + _META)
        row2.addWidget(lb2)
        self.dd_ver = DropDown("加载中…", [("", [("_loading", "正在取版本列表…", False)])],
                               multi=False, on_change=self._on_pick, width=260)
        row2.addWidget(self.dd_ver)
        row2.addStretch(1)
        self.btn_go = PillButton("安装到此游戏", "accent")
        self.btn_go.setFixedHeight(28)
        self.btn_go.clicked.connect(self._install)
        row2.addWidget(self.btn_go)
        v.addLayout(row2)

        self.lb_state = QLabel("")
        self.lb_state.setStyleSheet("background:transparent;" + _META)
        self.lb_state.setWordWrap(True)
        v.addWidget(self.lb_state)

        self._sync_kind_btns()
        self._sync_hint()

    # ---- 展示 ----
    def _sync_kind_btns(self):
        for key, b in self._kind_btns.items():
            b.kind = "accent" if key == self._kind else "ghost"
            b.update()

    def _sync_hint(self):
        mc = str(self.item.get("mc") or "")
        if not mc:
            self.lb_state.setText("没识别出这个游戏的 Minecraft 版本，装不了加载器 —— "
                                  "先去「游戏管理」确认这个版本是完整的。")
            self.btn_go.setEnabled(False)
            return
        cur = str(self.item.get("loader") or "")
        ver = str(self.item.get("loader_ver") or "")
        cn = dict(self.KINDS).get(cur, "")
        now = f"当前：{cn} {ver}（MC {mc}）" if cur and cn else f"当前：原版（MC {mc}）"
        self.lb_state.setText(now + "。换版本会新装一个版本目录，旧的不删，随时能改回来。")

    # ---- 数据 ----
    def set_kind(self, kind):
        if self._busy or kind == self._kind:
            return
        self._kind = kind
        self._ver = ""
        self._sync_kind_btns()
        self._load_versions(True)

    def load(self):
        self._load_versions(False)

    def _load_versions(self, reinstall=False):
        mc = str(self.item.get("mc") or "")
        if not mc:
            return
        kind = self._kind
        if kind in self._loaded and not reinstall:
            self._fill(self._loaded[kind])
            return
        self.dd_ver.rebuild([("", [("_loading", "正在取版本列表…", False)])])
        from ..core import loader as _loader
        run_async(_loader.loader_versions, kind, mc,
                  on_done=lambda vs, k=kind: self._fill(vs, k),
                  on_err=lambda e: self._fail_versions(e))

    def _fail_versions(self, e):
        try:
            self.dd_ver.rebuild([("", [("_none", "取版本失败：" + str(e)[:40], False)])])
            self.lb_state.setText("取不到版本列表（网络 / 镜像不通）。"
                                  "检查网络后点「版本」再试一次。")
        except RuntimeError:
            pass

    def _fill(self, vs, kind=None):
        kind = kind or self._kind
        if kind != self._kind:
            return
        vs = [str(x) for x in (vs or []) if x]
        self._loaded[kind] = vs
        if not vs:
            try:
                self.dd_ver.rebuild([("", [("_none", "这个 MC 版本没有可用版本", False)])])
            except RuntimeError:
                pass
            return
        cur = ""
        if kind == str(self.item.get("loader") or "").lower():
            cur = str(self.item.get("loader_ver") or "")
        items = [(v, v, v == cur) for v in vs[:120]]
        try:
            self.dd_ver.rebuild([("可用版本（新 → 旧）", items)])
            if cur and cur in vs:
                self._ver = cur
                self.dd_ver.set_value_label(cur)
            else:
                self._ver = vs[0]
                self.dd_ver.set_value_label(vs[0])
        except RuntimeError:
            return

    def _on_pick(self, vals):
        if vals:
            self._ver = str(vals[0] or "")
            self.dd_ver.set_value_label(self._ver)

    # ---- 安装 ----
    def _install(self):
        if self._busy:
            return
        mc = str(self.item.get("mc") or "")
        kind = self._kind
        ver = self._ver
        gd = str(self.dirs.get("root") or self.dirs.get("work") or "")
        if not (mc and ver and gd):
            Toast.show_toast(self.window(), "缺 MC 版本或游戏目录，装不了加载器", ms=3600,
                             kind="error")
            return
        cn = dict(self.KINDS).get(kind, kind)
        self._busy = True
        self.btn_go.setEnabled(False)
        for b in self._kind_btns.values():
            b.setEnabled(False)
        self.lb_state.setText(f"正在安装 {cn} {ver}（MC {mc}）… 首次要下原版和生成运行文件，"
                              "1~3 分钟，别关这个窗口。")
        from ..core import loader as _loader
        run_async(_loader.install_loader, mc, kind, ver, None,
                  on_done=lambda res: self._installed(res, mc, kind, ver, gd),
                  on_err=lambda e: self._install_failed(e))

    def _installed(self, res, mc, kind, ver, gd):
        self._busy = False
        self.btn_go.setEnabled(True)
        for b in self._kind_btns.values():
            b.setEnabled(True)
        ok, vid = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
        cn = dict(self.KINDS).get(kind, kind)
        win = None
        try:
            win = self.window()
        except RuntimeError:
            win = None
        if not ok:
            self._install_failed(vid)
            return
        # 登记过的游戏：把记录指到新版本目录（不删旧的，随时能改回）
        moved = False
        try:
            from ..core import importer
            moved = importer.retarget_added(self.item.get("path") or "", gd, vid,
                                            kind, ver, mc)
        except Exception:                                 # noqa: BLE001
            moved = False
        self.item.update({"loader": kind, "loader_ver": ver, "mc": mc,
                          "path": os.path.join(gd, "versions", vid),
                          "game_dir": gd, "version_id": vid})
        if moved:
            self.lb_state.setText(f"已装好 {cn} {ver}（{vid}），这个游戏已切到它 —— "
                                  "关掉窗口后启动的就是新版本。")
        else:
            self.lb_state.setText(f"已装好 {cn} {ver}（{vid}）。这个游戏不是通过"
                                  "「添加已有文件夹」登记的，所以没自动切换 —— "
                                  "到「游戏管理」里选这个新版本即可。")
        Toast.show_toast(win, f"{cn} {ver} 安装完成", ms=3600, kind="success")
        if callable(self._on_done):
            try:
                self._on_done(ok, vid)
            except Exception:                             # noqa: BLE001
                pass

    def _install_failed(self, e):
        self._busy = False
        try:
            self.btn_go.setEnabled(True)
            for b in self._kind_btns.values():
                b.setEnabled(True)
            self.lb_state.setText("安装失败：" + str(e)[:120])
            Toast.show_toast(self.window(), "加载器安装失败：" + str(e)[:80],
                             ms=6000, kind="error")
        except RuntimeError:
            pass


class GameSettingsDialog(_DialogBase):
    """单个游戏的设置窗（左右分栏）"""

    def __init__(self, parent, item, kind="instance"):
        pw = parent.width() if parent is not None else 720
        dw = max(620, min(720, pw - 32))
        name = str(item.get("name") or item.get("id") or "游戏")
        super().__init__(parent, width=dw, title=f"游戏设置 · {name[:18]}")
        self.item = item or {}
        self.kind = kind
        self._dirs = lp.game_dirs(self.item)
        self._conf0 = lp.game_conf(self.item)
        # 第六十七轮：概览里那些文件夹（清单 + 图标）的临时缓存归哪一摊。
        # 取消 / 保存 / 直接关窗口都会在 done() 里清掉，下次打开重新缓存。
        try:
            self._scope = gearcache.scope_for(self.item)
        except Exception:                                     # noqa: BLE001
            self._scope = ""
        self._prog = [0, 0]
        self._busy = False
        self._plan = None
        self._export_planned = False
        # 第一百零五轮：导出页的动态「其它文件夹」/ 规则表 / 标准格式开关。
        # extra 默认一个都不勾 —— 玩家不主动加，包体与 1.7.4 完全一致。
        self._export_folders = []
        self._export_rules = None
        self._export_mrpack = False
        self._timer = QTimer(self)
        self._timer.setInterval(140)
        self._timer.timeout.connect(self._tick_progress)

        box = QWidget()
        self._box = box
        box.setFixedHeight(470)
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)

        navw = QWidget()
        navw.setFixedWidth(132)
        nl = QVBoxLayout(navw)
        nl.setContentsMargins(0, 2, 0, 0)
        nl.setSpacing(3)
        self.stack = QStackedWidget()
        self._nav = QButtonGroup(self)
        self._nav.setExclusive(True)
        pages = [
            ("概览", self._page_overview),
            ("设置", self._page_switch),
            ("游戏 Java", self._page_java),
            ("运行内存", self._page_mem),
            ("导出", self._page_export),
        ]
        for i, (label, builder) in enumerate(pages):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(34)
            b.setStyleSheet(_NAV_QSS)
            b.setChecked(i == 0)
            self._nav.addButton(b, i)
            nl.addWidget(b)
            self.stack.addWidget(self._scrolled(builder()))
        nl.addStretch(1)
        self._nav.idClicked.connect(self._on_nav)
        h.addWidget(navw)
        h.addWidget(self.stack, 1)
        self.body.addWidget(box)
        self._fit_box()

        self.add_buttons([("取消", "ghost", self.reject),
                          ("保存", "accent", self._save)])

    # ------------------------------------------------------------ 骨架
    def _scrolled(self, inner):
        sa = _ScrollPage(inner)
        sa.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0 2px 2px;}"
            "QScrollBar::handle:vertical{background:rgba(31,39,51,64);"
            "border-radius:3px;min-height:26px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(31,39,51,104);}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{"
            "background:transparent;}")
        return sa

    def _page(self, title, desc=""):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 8, 2)
        lay.setSpacing(8)
        t = QLabel(title)
        f = QFont(theme.FONT)
        f.setPixelSize(15)
        f.setBold(True)
        t.setFont(f)
        lay.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setStyleSheet(_TIP)
            d.setWordWrap(True)
            lay.addWidget(d)
        return w, lay

    def _content_width(self):
        return max(240, self.width() - 52 - 132 - 16 - 8)

    def _fit_box(self):
        if not hasattr(self, "_box"):
            return
        cw = self._content_width()
        need = 0
        for i in range(self.stack.count()):
            wd = self.stack.widget(i)
            inner = getattr(wd, "_inner", wd)
            lay = inner.layout()
            if lay is None:
                continue
            hh = inner.heightForWidth(cw)
            if hh <= 0:
                hh = lay.sizeHint().height()
            need = max(need, int(hh))
        cap = 470
        par = self.parentWidget()
        if par is not None and par.isVisible():
            cap = min(cap, max(300, par.height() - 137))
        hgt = int(min(max(300, need), cap))
        if self._box.height() != hgt:
            self._box.setFixedHeight(hgt)
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            if isinstance(w, _ScrollPage):
                w.fit()
        self.adjustSize()

    def _on_nav(self, i):
        self.stack.setCurrentIndex(i)
        if self.stack.widget(i) is self.stack.widget(4):
            self._ensure_export_plan()

    # ------------------------------------------------------------ 概览
    def _page_overview(self):
        w, lay = self._page("概览", "点任意一行就能打开对应文件夹；不存在的目录会自动创建。"
                                   "模组那行最左边的小三角能就地展开清单，"
                                   "直接禁用 / 启用 / 删除，不用跳出去。")
        it = self.item
        bits = []
        if it.get("mc"):
            bits.append(f"MC {it['mc']}")
        if it.get("loader"):
            from ..core import runtime as _rt
            cn = _rt.LOADER_CN.get(str(it["loader"]).lower(), str(it["loader"]))
            bits.append(f"{cn} {it.get('loader_ver') or ''}".strip())
        elif self.kind == "version":
            bits.append("原版")
        if it.get("memory_mb"):
            bits.append(f"{max(1, int(it['memory_mb']) // 1024)} GB 内存")
        head = QLabel(" · ".join([b for b in bits if b]) or "（没有更多信息）")
        head.setStyleSheet(_H2)
        lay.addWidget(head)

        path = str(it.get("path") or it.get("dir") or "")
        wd = self._dirs.get("work") or self._dirs.get("root") or path
        lb = ElideLabel(f"游戏目录：{wd}")
        lb.setStyleSheet(_META)
        tip = wd
        if self._dirs.get("root") and self._dirs["root"] != wd:
            tip = f"{wd}\n（根目录：{self._dirs['root']}；当前开启了版本隔离）"
        lb.setToolTip(tip)
        lay.addWidget(lb)

        # 第六十四轮修：三角**一直**给模组行，跟内置文件管理器开不开无关。
        # 起初只在关掉内置管理器时才挂（当时想的是「开着就能用文件管理器管」），
        # 但玩家的习惯是「在游戏设置里看见模组就想顺手关一个」—— 让他先点「打开」
        # 跳进另一个窗、找到文件、再右键，两步变五步。
        self._mod_panel = None
        self._mods_row = None
        self._folder_rows = {}
        for key, title, icon, color, note, ckey in _FOLDER_ROWS:
            p = self._dirs.get(key) or ""
            if key == "datapacks":
                _dp, dp_note = lp.datapack_dir(self.item)
                note = dp_note
            exp = (key == "mods")
            row = _FolderRow(title, note, p, self._open_dir, note,
                             expandable=exp,
                             on_toggle=(self._toggle_mods if key == "mods" else None),
                             icon=icon, icon_color=color)
            row._count_key = ckey
            self._folder_rows[key] = row
            lay.addWidget(row)
            if key == "mods":
                self._mods_row = row
                self._mods_lay = lay
        lay.addWidget(self._cache_line())
        lay.addStretch(1)
        # 数量在后台数（第九十一轮）：这五行是整页最先画出来的东西，读盘放到主线程
        # 会在「非启动器下载的大整合包」上卡住 —— 那种目录动辄几千个文件。
        QTimer.singleShot(0, self._refresh_folder_counts)
        return w

    def _refresh_folder_counts(self):
        """后台数这个实例的模组/存档/光影/材质包/数据包，回来填到五行右侧。"""
        rows = getattr(self, "_folder_rows", None)
        if not rows:
            return
        run_async(contentcount.counts_for_entry, self.item,
                  on_done=self._fill_folder_counts,
                  on_err=lambda _e: None)

    def _fill_folder_counts(self, counts):
        rows = getattr(self, "_folder_rows", None)
        if not rows:
            return
        for key, row in rows.items():
            try:
                n = int((counts or {}).get(getattr(row, "_count_key", ""), 0) or 0)
            except (TypeError, ValueError, RuntimeError):
                continue
            try:
                row.set_count(n)
            except RuntimeError:          # 窗口已经关了 / 行被销毁
                return

    def _cache_line(self):
        """底部一行：临时缓存到底在哪（第六十七轮补丁）

        玩家反馈「安装目录里也没有 cache」—— 有很大一部分是「不知道它在哪」：
        这份缓存放在 `<安装目录>\\cache`，跟游戏的 `.minecraft` 不是同一个地方
        （换过安装目录之后更不是一个）。这里把真实路径和用途写在概览页底部，
        点一下就能用内置文件管理器打开看。
        """
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 6, 0, 0)
        h.setSpacing(8)
        tip = QLabel("临时缓存")
        tip.setStyleSheet(_META)
        h.addWidget(tip)
        where = gearcache.root(create=False) or "（还没建出来，点「打开」会新建）"
        lb = ElideLabel(where)
        lb.setStyleSheet(_META)
        lb.setToolTip("%s\n\n模组清单和包内图标临时落在这里，"
                      "关掉设置窗（取消 / 保存 / 直接关）就清空内容 —— 随时能整个删掉。"
                      % where)
        h.addWidget(lb, 1)
        b = PillButton("打开", "ghost")
        b.setFixedSize(52, 24)
        b.setToolTip("用内置文件管理器打开缓存目录")
        b.clicked.connect(self._open_cache)
        h.addWidget(b)
        return box

    def _open_cache(self):
        from . import fspicker
        d = gearcache.root()
        if not d:
            Toast.show_toast(self, "缓存目录建不出来（安装目录没有写权限？）", ms=3400,
                             kind="error")
            return
        fspicker.pick_view(self, "临时缓存", d)

    def _toggle_mods(self, expanded):
        """概览里展开 / 收起模组清单（第六十四轮）

        第六十七轮补丁：点下去**先出来**，数据下一帧再读。以前是在这一帧里建面板
        + 读目录 + 建 24 行 + 重排整页高度，213 个模组要 400~700ms，表现就是
        「点小三角卡那么一下」。
        """
        lay = getattr(self, "_mods_lay", None)
        row = getattr(self, "_mods_row", None)
        if lay is None or row is None:
            return
        if self._mod_panel is None:
            from .modlist import FileListPanel
            self._mod_panel = FileListPanel(
                self._dirs.get("mods") or "", on_changed=self._on_mods_changed,
                scope=self._scope, load=False,
                mc_version=str(self.item.get("mc") or ""),
                loader=str(self.item.get("loader") or ""))
        if expanded:
            idx = lay.indexOf(row)
            lay.insertWidget(idx + 1 if idx >= 0 else lay.count() - 1,
                             self._mod_panel)
            self._mod_panel.setVisible(True)
            QTimer.singleShot(0, self._mod_panel.reload)
            QTimer.singleShot(0, self._fit_box)
        else:
            lay.removeWidget(self._mod_panel)
            self._mod_panel.hide()  # 第八十二轮：先 hide 再摘，别让它变成窗口
            self._mod_panel.setParent(None)
            self._fit_box()

    def _on_mods_changed(self):
        """模组清单里禁用 / 删除 / 刷新之后：重排高度 + 重数「模组文件夹」那行的数量。"""
        self._fit_box()
        try:
            contentcount.forget(self.item)
        except Exception:                                     # noqa: BLE001
            pass
        QTimer.singleShot(260, self._refresh_folder_counts)

    def _open_dir(self, path):
        if not path:
            Toast.show_toast(self, "还没确定这个游戏的目录", kind="error")
            return
        try:
            os.makedirs(path, exist_ok=True)
            from .fspicker import pick_view          # 第六十轮：内置优先
            pick_view(self, "文件夹内容", path)
        except Exception as e:                       # noqa: BLE001
            Toast.show_toast(self, f"打不开：{str(e)[:60]}", kind="error")

    # ------------------------------------------------------------ 设置
    def _page_switch(self):
        w, lay = self._page(
            "设置",
            "版本隔离：开启时这个游戏的存档、模组、配置各自独立；"
            "关闭时和其它版本共用同一份（省空间，但模组容易互相打架）。")
        box = QWidget()
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setStyleSheet("QWidget{background:rgba(255,255,255,170);border-radius:10px;}")
        bh = QHBoxLayout(box)
        bh.setContentsMargins(12, 8, 10, 8)
        bh.setSpacing(10)
        info = QVBoxLayout()
        info.setSpacing(2)
        t = QLabel("版本隔离")
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;color:#1f2733;")
        info.addWidget(t)
        self.lb_iso = QLabel("")
        self.lb_iso.setStyleSheet("background:transparent;" + _META)
        self.lb_iso.setWordWrap(True)
        info.addWidget(self.lb_iso)
        bh.addLayout(info, 1)
        on = self._conf0["iso"] == "on" if self._conf0["iso"] else lp.should_isolate(self.item)
        self.sw_iso = PillSwitch(on)
        self.sw_iso.toggled.connect(lambda _v: self._sync_iso_hint())
        bh.addWidget(self.sw_iso, 0, Qt.AlignVCenter)
        lay.addWidget(box)

        self._sync_iso_hint()
        more = QLabel(
            "开启后工作目录是「实例目录」（版本目录或整合包目录），"
            "关掉后是游戏根目录。改动会在下次启动时生效，"
            "已有的存档不会自动搬过去 —— 需要的话在概览里手动打开两个文件夹复制。")
        more.setStyleSheet(_META)
        more.setWordWrap(True)
        lay.addWidget(more)

        # 第九十三轮：收放式「修改加载器」（默认收起）—— 复制 PCL 那套「独立游戏
        # 设置里能就地换 Forge/NeoForge/Fabric」的习惯，省得为了换个加载器版本
        # 跑去用别的启动器重装一遍。
        lay.addWidget(self._loader_card())
        lay.addStretch(1)
        return w

    def _loader_card(self):
        cur = str(self.item.get("loader") or "")
        cn = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
              "quilt": "Quilt"}.get(cur, "")
        ver = str(self.item.get("loader_ver") or "")
        mc = str(self.item.get("mc") or "")
        summary = (f"{cn} {ver}".strip() if cn else "原版")
        if mc:
            summary += f" · MC {mc}"
        card = _Collapse("修改加载器", summary)
        card.tri.setToolTip("展开，改这个游戏的 Mod 加载器版本"
                            "（Forge / NeoForge / Fabric）")
        self._loader_editor = _LoaderEditor(self.item, self._dirs,
                                            on_done=self._after_loader_install)
        card.body_lay.addWidget(self._loader_editor)
        # 版本列表要联网取：展开那一下才去取（这个设置窗经常只是来看一眼的）
        card.on_open = self._loader_editor.load
        return card

    def _after_loader_install(self, ok, vid):
        """装完加载器：概览里的版本号那一行 / 窗口标题要跟着变，别还写着老版本"""
        if not ok:
            return
        try:
            self._dirs = lp.game_dirs(self.item)
        except Exception:                                 # noqa: BLE001
            pass
        self._fit_box()

    def _sync_iso_hint(self):
        glob = dict((k, n) for k, n, _d in lp.ISO_MODES).get(lp.iso_mode(), "关闭")
        self.lb_iso.setText(f"全局现在是「{glob}」；这个开关只影响「"
                            f"{self.item.get('name') or '这个游戏'}」")

    # ------------------------------------------------------------ Java
    def _page_java(self):
        w, lay = self._page(
            "游戏 Java",
            "「独立启动」用的 Java。留「跟随全局」时由启动引擎按版本 json 的 "
            "javaVersion 自动挑（1.20.5+ 用 21、1.17~1.20.4 用 17、1.16 及更早用 8）。")
        mode, gpath = lp.java_choice()
        cur = self._conf0["java_path"] if self._conf0["java"] == "custom" else "global"
        # 先用「跟随全局 + 添加」两行起个头，扫描本机 Java 放后台 ——
        # 首次扫描要读十几个 JDK 的 release 文件，同步做会让弹窗卡半秒。
        self.ex_java = ChoiceExpander("Java 运行环境",
                                      self._java_options(cur, gpath, scan=False), cur)
        lay.addWidget(self.ex_java)
        self.lb_java = QLabel("")
        self.lb_java.setStyleSheet(_META)
        self.lb_java.setWordWrap(True)
        lay.addWidget(self.lb_java)
        self.lb_scan = QLabel("正在识别本机 Java…")
        self.lb_scan.setStyleSheet(_META)
        self.lb_scan.setWordWrap(True)
        lay.addWidget(self.lb_scan)
        self.ex_java.changed.connect(self._on_java_changed)
        self._sync_java_hint()
        self._load_javas()
        lay.addStretch(1)
        return w

    def _load_javas(self):
        from ..core import javamanager

        def done(lst):
            try:
                cur = self.ex_java.value()
                self.ex_java.set_options(
                    self._java_options(cur, lp.java_choice()[1]), cur)
                self.lb_scan.setText(
                    f"本机找到 {len(lst)} 个 Java —— 点开上面一行就能指定" if lst else
                    "本机没找到 Java：可以点开上面一行「添加 Java.exe…」手动指定")
            except Exception:
                pass

        run_async(lambda: javamanager.scan_all(), on_done=done, on_err=lambda e: None)

    def _java_options(self, cur, gpath="", scan=True):
        from ..core import javamanager
        gmode, gp = lp.java_choice()
        gdesc = "按每个版本需要的 Java 版本自动挑"
        if gmode == "path" and gp:
            gdesc = f"全局指定：{gp}"
        opts = [("global", "跟随全局", gdesc)]
        seen = set()
        for j in (javamanager.scan_all() if scan else []):
            seen.add(os.path.normcase(j["exe"]))
            opts.append((j["exe"], f"Java {j['major']}（{j['ver']}）",
                         f"{j['home']} · 来源：{j.get('source') or '本机'}"))
        for p in (gpath, cur):
            if p and p not in ("global", "__add__") and \
                    os.path.normcase(p) not in seen:
                seen.add(os.path.normcase(p))
                opts.append((p, "手动指定的 java.exe", p))
        opts.append(("__add__", "添加 Java.exe…",
                     "手动挑一个 java.exe（绿色版 JDK 解压在别处时用）"))
        return opts

    def _on_java_changed(self, key):
        if key == "__add__":
            from .fspicker import pick_file
            p = pick_file(self, "选择 java.exe", "")
            if p and os.path.isfile(p):
                self.ex_java.set_options(self._java_options(p), p)
            else:
                self.ex_java.set_value("global")
        self._sync_java_hint()

    def _sync_java_hint(self):
        from ..core import javamanager
        v = self.ex_java.value()
        if v in ("", "global"):
            self.lb_java.setText("当前：跟随全局（设置 → 启动项 → Java 运行环境）")
            return
        home = javamanager.java_home_of(v)
        major = javamanager._release_major(home)[0] or \
            javamanager._guess_major_from_path(home)
        self.lb_java.setText(f"当前：{v}" + (f"（Java {major}）" if major else ""))

    # ------------------------------------------------------------ 内存
    def _page_mem(self):
        w, lay = self._page(
            "运行内存",
            "只影响这个游戏。自动配置最少给 4 GB，模组越多、本机内存越大再往上加；"
            "自定义则自己拖滑条。")
        cur = self._conf0["memory"]
        self.ex_mem = ChoiceExpander(
            "内存分配方式",
            [("global", "跟随全局", f"全局现在是「"
              + dict((k, n) for k, n, _d in lp.MEMORY_MODES).get(lp.memory_mode(), "自动配置")
              + "」")]
            + [(k, n, d) for k, n, d in lp.MEMORY_MODES],
            cur if cur in ("auto", "custom") else "global")
        self.ex_mem.changed.connect(lambda _k: self._sync_mem())
        lay.addWidget(self.ex_mem)

        total = installer.total_ram_gb()
        max_mb = int(min(65536, max(2048, (total - 2) * 1024)))
        mb0 = int(self._conf0["memory_mb"] or 0) or lp.memory_for(self.item)[0]
        mb0 = max(1024, min(mb0, max_mb))

        self.mem_big = QLabel("")
        f = QFont(theme.FONT)
        f.setPixelSize(24)
        f.setBold(True)
        self.mem_big.setFont(f)
        self.mem_big.setStyleSheet("color:#2f7cf6;")
        lay.addWidget(self.mem_big)

        self._sl_on = (
            "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,36);border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:#2f7cf6;border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
            "border-radius:8px;background:#ffffff;border:2px solid #2f7cf6;}")
        self._sl_off = (
            "QSlider::groove:horizontal{height:6px;background:rgba(31,39,51,20);border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:rgba(31,39,51,40);border-radius:3px;}"
            "QSlider::handle:horizontal{width:16px;height:16px;margin:-5px 0;"
            "border-radius:8px;background:#eef1f5;border:2px solid #c2cad4;}")
        self.mem_slider = QSlider(Qt.Horizontal)
        self.mem_slider.setRange(1024, max_mb)
        self.mem_slider.setSingleStep(256)
        self.mem_slider.setPageStep(1024)
        self.mem_slider.setValue(mb0)
        self.mem_slider.setStyleSheet(self._sl_on)
        lay.addWidget(self.mem_slider)

        inrow = QHBoxLayout()
        inrow.setSpacing(8)
        lb = QLabel("手动输入")
        lb.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;")
        inrow.addWidget(lb)
        self._spin_on = (
            "QSpinBox{background:rgba(255,255,255,190);border:1px solid rgba(31,39,51,45);"
            "border-radius:8px;padding:0 8px;font-size:12px;color:#2a3442;}"
            "QSpinBox:focus{border:1px solid rgba(47,124,246,160);}"
            "QSpinBox::up-button,QSpinBox::down-button{width:0;border:none;}")
        self._spin_off = (
            "QSpinBox{background:rgba(31,39,51,12);border:1px solid rgba(31,39,51,22);"
            "border-radius:8px;padding:0 8px;font-size:12px;color:#9aa4b0;}"
            "QSpinBox::up-button,QSpinBox::down-button{width:0;border:none;}")
        self.mem_spin = QSpinBox()
        self.mem_spin.setRange(1024, max_mb)
        self.mem_spin.setSingleStep(256)
        self.mem_spin.setSuffix(" MB")
        self.mem_spin.setValue(mb0)
        self.mem_spin.setFixedHeight(32)
        self.mem_spin.setFixedWidth(140)
        self.mem_spin.setStyleSheet(self._spin_on)
        inrow.addWidget(self.mem_spin)
        inrow.addStretch(1)
        lay.addLayout(inrow)

        self.lb_mem = QLabel("")
        self.lb_mem.setStyleSheet(_META)
        self.lb_mem.setWordWrap(True)
        lay.addWidget(self.lb_mem)

        # 第一百零四轮：每实例自定义参数。空串 = 跟随全局；填了**完全覆盖**
        # 全局（与 PCL-CE ArgConfig 同语义），由 build_command 按选项去重后拼入。
        from PySide6.QtWidgets import QLineEdit
        adv = QLabel("本游戏的启动参数（留空跟随全局；填了就完全覆盖全局）")
        adv.setStyleSheet(_H2)
        adv.setWordWrap(True)
        lay.addWidget(adv)

        def _edit(placeholder, value):
            e = QLineEdit()
            e.setPlaceholderText(placeholder)
            e.setText(str(value or ""))
            e.setFixedHeight(28)
            e.setStyleSheet(
                "QLineEdit{background:rgba(255,255,255,190);"
                "border:1px solid rgba(31,39,51,45);border-radius:7px;"
                "padding:0 8px;font-size:11px;color:#2a3442;}")
            lay.addWidget(e)
            return e

        _c = self._conf0
        self.ed_g_jvm = _edit("本游戏 JVM 参数（例：-Xmx6G -XX:+UseZGC）", _c.get("jvm"))
        self.ed_g_game = _edit("本游戏游戏参数（例：--quickPlay singleplayer）", _c.get("game"))
        self.ed_g_pre = _edit("本游戏预启动命令（可留空）", _c.get("pre_launch"))
        self.ed_g_window = _edit("窗口：宽x高|模式|标题", _c.get("window"))
        self.ed_g_server = _edit("启动后自动进服务器：地址[:端口]", _c.get("server"))

        self.mem_slider.valueChanged.connect(self._from_slider)
        self.mem_spin.valueChanged.connect(self._from_spin)
        self._sync_mem()
        lay.addStretch(1)
        return w

    def _from_slider(self, v):
        self.mem_spin.blockSignals(True)
        self.mem_spin.setValue(int(v))
        self.mem_spin.blockSignals(False)
        self._mem_label(int(v))

    def _from_spin(self, v):
        self.mem_slider.blockSignals(True)
        self.mem_slider.setValue(int(v))
        self.mem_slider.blockSignals(False)
        self._mem_label(int(v))

    def _sync_mem(self):
        mode = self.ex_mem.value()
        editable = mode == "custom"
        if mode == "auto":
            mb, why = lp.memory_for(self.item)
            for wd in (self.mem_slider, self.mem_spin):
                wd.blockSignals(True)
                wd.setValue(int(mb))
                wd.blockSignals(False)
            self._mem_label(int(mb))
            self.lb_mem.setText(why)
        elif mode == "global":
            mb, why = lp.memory_for(self.item)
            for wd in (self.mem_slider, self.mem_spin):
                wd.blockSignals(True)
                wd.setValue(int(mb))
                wd.blockSignals(False)
            self._mem_label(int(mb))
            self.lb_mem.setText("跟随全局：" + why)
        else:
            self._mem_label(int(self.mem_spin.value()))
            self.lb_mem.setText("自定义：按你拖的值分配")
        for wd in (self.mem_slider, self.mem_spin):
            wd.setEnabled(editable)
        self.mem_slider.setStyleSheet(self._sl_on if editable else self._sl_off)
        self.mem_spin.setStyleSheet(self._spin_on if editable else self._spin_off)

    def _mem_label(self, mb):
        mb = int(mb)
        self.mem_big.setText(f"{mb / 1024:.1f} GB".replace(".0 GB", " GB"))

    # ------------------------------------------------------------ 导出
    def _page_export(self):
        w, lay = self._page(
            "导出整合包",
            "把这份游戏打成一个 zip（manifest.json + overrides/），"
            "别人解压后把 overrides 里的内容放进 .minecraft 就能还原。")
        self.cb_sec = {}
        for key, name, desc in packexport.SECTIONS:
            cb = QCheckBox(f"{name}　—　{desc}")
            cb.setChecked(key in packexport.DEFAULT_SECTIONS)
            cb.setStyleSheet("color:#4c5866;font-size:12px;")
            cb.toggled.connect(lambda _v: self._refresh_export_hint())
            self.cb_sec[key] = cb
            lay.addWidget(cb)

        # ---------------- 第 105 轮：其它文件夹（默认收起、默认一个都不勾） ----
        # 固定 4 段会漏 kubejs / scripts / defaultconfigs / openloader / xaero，
        # 照 PCL-CE 的思路扫出来给玩家自己勾，但用 Qt 原生 QListWidget
        # （自带虚拟化），不一条一个 QWidget + 每拍 setStyleSheet。
        self.ex_extra = _Collapse("其它文件夹", "扫描中…")
        self.lst_extra = QListWidget()
        self.lst_extra.setFixedHeight(128)
        self.lst_extra.setStyleSheet(
            "QListWidget{background:rgba(255,255,255,150);border:1px solid "
            "rgba(31,39,51,30);border-radius:8px;font-size:11px;color:#3a4654;}"
            "QListWidget::item{padding:3px 6px;}")
        self.lst_extra.itemChanged.connect(
            lambda _it: self._refresh_export_hint())
        self.ex_extra.body_lay.addWidget(self.lst_extra)
        row_scan = QHBoxLayout()
        row_scan.setSpacing(8)
        b_scan = PillButton("重新扫描", "ghost")
        b_scan.setFixedHeight(26)
        b_scan.clicked.connect(lambda: self._scan_export_folders(force=True))
        row_scan.addWidget(b_scan)
        row_scan.addStretch(1)
        self.lb_extra_tip = QLabel("默认不勾；勾了才会进包")
        self.lb_extra_tip.setStyleSheet(_META)
        row_scan.addWidget(self.lb_extra_tip)
        self.ex_extra.body_lay.addLayout(row_scan)
        lay.addWidget(self.ex_extra)
        QTimer.singleShot(0, lambda: self._scan_export_folders())

        self.cb_mrpack = QCheckBox(
            "同时写标准 modrinth.index.json（.mrpack 格式，文件仍全量内嵌）")
        self.cb_mrpack.setStyleSheet("color:#4c5866;font-size:11px;")
        self.cb_mrpack.toggled.connect(self._on_mrpack)
        lay.addWidget(self.cb_mrpack)

        row_cfg = QHBoxLayout()
        row_cfg.setSpacing(8)
        b_out = PillButton("导出规则配置…", "ghost")
        b_out.setFixedHeight(26)
        b_out.clicked.connect(self._rules_out)
        row_cfg.addWidget(b_out)
        b_in = PillButton("导入规则配置…", "ghost")
        b_in.setFixedHeight(26)
        b_in.clicked.connect(self._rules_in)
        row_cfg.addWidget(b_in)
        row_cfg.addStretch(1)
        lay.addLayout(row_cfg)
        self.lb_rules = QLabel("规则：默认排除表")
        self.lb_rules.setStyleSheet(_META)
        self.lb_rules.setWordWrap(True)
        lay.addWidget(self.lb_rules)

        self.lb_export = QLabel("正在统计…")
        self.lb_export.setStyleSheet(_META)
        self.lb_export.setWordWrap(True)
        lay.addWidget(self.lb_export)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_export = PillButton("导出…", "accent")
        self.btn_export.setFixedHeight(32)
        self.btn_export.clicked.connect(self._do_export)
        row.addWidget(self.btn_export)
        btn_re = PillButton("重新统计", "ghost")
        btn_re.setFixedHeight(32)
        btn_re.clicked.connect(lambda: self._ensure_export_plan(force=True))
        row.addWidget(btn_re)
        row.addStretch(1)
        lay.addLayout(row)

        note = QLabel("日志、崩溃报告、缓存目录不会被打进包里；"
                      "存档体积可能很大，不需要就别勾。")
        note.setStyleSheet(_META)
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return w

    def _sections(self):
        return [k for k, cb in self.cb_sec.items() if cb.isChecked()]

    # ---------------- 第 105 轮：动态其它文件夹 / 规则配置 / 标准格式 ----------------
    def _selected_extra(self):
        """勾中的「其它文件夹」名列表（默认一个都没有）。"""
        lst = getattr(self, "lst_extra", None)
        if lst is None:
            return []
        out = []
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.Checked:
                out.append(str(it.data(Qt.UserRole) or ""))
        return [x for x in out if x]

    def _scan_export_folders(self, force=False):
        """扫盘放进后台线程；只在数据回来那一刻填一次列表。"""
        if getattr(self, "_extra_scanned", False) and not force:
            return
        self._extra_scanned = True
        item = dict(self.item)
        keep = set(self._selected_extra())

        def done(folders):
            if not _alive(self):
                return
            self._export_folders = folders or []
            lst = self.lst_extra
            lst.blockSignals(True)
            lst.clear()
            for f in self._export_folders:
                it = QListWidgetItem(
                    f"{f['name']}　·　{f['files']} 个文件 / "
                    f"{packexport.fmt_size(f['bytes'])}")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if f["name"] in keep else Qt.Unchecked)
                it.setData(Qt.UserRole, f["name"])
                it.setToolTip(f["path"])
                lst.addItem(it)
            lst.blockSignals(False)
            n = len(self._export_folders)
            self.ex_extra.set_summary(f"{n} 个可选" if n else "没有其它文件夹")
            # 有东西且还收着才展开；「重新扫描」别把玩家手动收起的状态翻回去
            if n and not self.ex_extra.is_open():
                self.ex_extra.toggle()
            self._refresh_export_hint()

        def err(e):
            if not _alive(self):
                return
            self.ex_extra.set_summary("扫描失败")
            self.lb_extra_tip.setText(f"扫描失败：{str(e)[:60]}")

        run_async(lambda: packexport.extra_folders(item),
                  on_done=done, on_err=err)

    def _on_mrpack(self, on):
        self._export_mrpack = bool(on)
        # 文件内容不变，不用重算 plan

    def _rules_out(self):
        from .fspicker import pick_dir
        folder = pick_dir(self, "把导出规则配置存到哪个文件夹",
                          os.path.expanduser("~"))
        if not folder:
            return
        try:
            path = packexport.free_name(folder, "export_config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(packexport.dump_config_text(
                    self._export_rules, self._selected_extra()))
            Toast.show_toast(self, f"规则已导出：{os.path.basename(path)}",
                             kind="success")
        except Exception as e:                              # noqa: BLE001
            Toast.show_toast(self, f"规则导出失败：{str(e)[:60]}", kind="error")

    def _rules_in(self):
        from .fspicker import pick_file
        f = pick_file(self, "选择 export_config.txt",
                      os.path.expanduser("~"))
        if not f:
            return
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                rules, extra = packexport.load_config_text(fp.read())
            self._export_rules = rules
            want = set(extra)
            lst = self.lst_extra
            lst.blockSignals(True)
            for i in range(lst.count()):
                it = lst.item(i)
                it.setCheckState(Qt.Checked
                                 if str(it.data(Qt.UserRole) or "") in want
                                 else Qt.Unchecked)
            lst.blockSignals(False)
            self.lb_rules.setText(
                f"规则：已导入 {len(rules)} 条（{os.path.basename(f)}）")
            self._refresh_export_hint()
            Toast.show_toast(self, "导出规则已导入", kind="success")
        except Exception as e:                              # noqa: BLE001
            Toast.show_toast(self, f"规则导入失败：{str(e)[:60]}", kind="error")

    def _ensure_export_plan(self, force=False):
        if self._export_planned and not force:
            return
        self._export_planned = True
        secs = self._sections()
        extra = tuple(self._selected_extra())
        rules = self._export_rules
        self.lb_export.setText("正在统计要打包的文件…")
        item = dict(self.item)

        def done(pl):
            try:
                self._plan = pl
                self.lb_export.setText(
                    f"共 {pl['files']} 个文件，约 {packexport.fmt_size(pl['bytes'])}")
            except Exception:
                pass

        def err(e):
            try:
                self._plan = None
                self.lb_export.setText(f"统计失败：{str(e)[:80]}")
            except Exception:
                pass

        run_async(lambda: packexport.plan(item, secs, extra, rules),
                  on_done=done, on_err=err)

    def _refresh_export_hint(self):
        self._ensure_export_plan(force=True)

    def _do_export(self):
        if self._busy:
            Toast.show_toast(self, "正在导出，请稍等", kind="info")
            return
        secs = self._sections()
        if not secs:
            Toast.show_toast(self, "至少勾一项要导出的内容", kind="error")
            return
        from .fspicker import pick_dir
        try:
            from ..core import winlnk
            start = winlnk.desktop_dir() or os.path.expanduser("~")
        except Exception:                              # noqa: BLE001
            start = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        folder = pick_dir(self, "选择导出到哪个文件夹", start)
        if not folder:
            return
        mrpack = self.cb_mrpack.isChecked()
        dest = packexport.free_name(
            folder, packexport.default_name(self.item, mrpack))
        self._busy = True
        self.btn_export.setEnabled(False)
        self._prog = [0, 0]
        self.lb_export.setText("正在导出…")
        self._timer.start()
        item = dict(self.item)

        extra = tuple(self._selected_extra())
        rules = self._export_rules

        def work():
            def prog(d, t):
                # 只写数据（工作线程不许碰控件），界面由 _tick_progress 读
                self._prog[0] = d
                self._prog[1] = t
            return packexport.export_pack(item, dest, secs, progress_cb=prog,
                                          extra=extra, rules=rules,
                                          mrpack=mrpack)

        def done(res):
            self._busy = False
            self.btn_export.setEnabled(True)
            self._timer.stop()
            self.lb_export.setText(
                f"已导出：{res['path']}（{packexport.fmt_size(res['size'])}，"
                f"{res['files']} 个文件）")
            Toast.show_toast(self, "整合包已导出", ms=3200, kind="success")

        def err(e):
            self._busy = False
            self.btn_export.setEnabled(True)
            self._timer.stop()
            msg = str(e)[:120]
            self.lb_export.setText(f"导出失败：{msg}")
            Toast.show_toast(self, f"导出失败：{msg}", ms=5200, kind="error")

        run_async(work, on_done=done, on_err=err)

    def _tick_progress(self):
        d, t = self._prog[0], self._prog[1]
        if t:
            self.lb_export.setText(
                f"正在导出… {d} / {t}（{int(d * 100 / t)}%）")

    # ------------------------------------------------------------ 收尾
    def done(self, r):
        """窗口关上（保存 / 取消 / 点 × / Esc）时清掉这次用的缓存。

        第六十七轮：`<安装目录>\\cache\\<那一摊>` 只是「打开期间别卡」的临时落脚点，
        用户要求「取消或保存后自动清除，下次打开重新缓存」。放在 `done()` 里是
        因为它是 accept / reject / 关窗口的唯一汇合点 —— 三处各写一遍迟早漏一处。
        """
        try:
            if getattr(self, "_scope", ""):
                gearcache.clear(self._scope)
        except Exception:                                     # noqa: BLE001
            pass
        # 第九十一轮：数量行别在窗口关掉之后还被异步回调摸到（C++ 对象已销毁）
        self._folder_rows = {}
        super().done(r)

    # ------------------------------------------------------------ 保存
    def _save(self):
        iso = "on" if self.sw_iso.isChecked() else "off"
        jav = self.ex_java.value()
        mem = self.ex_mem.value()
        kv = {
            "iso": iso,
            "java": "global" if jav in ("", "global") else "custom",
            "java_path": "" if jav in ("", "global") else jav,
            "memory": mem if mem in ("auto", "custom") else "global",
            "memory_mb": int(self.mem_spin.value()),
        }
        # 第一百零四轮：每实例参数。这一页可能没被翻到过（懒建），
        # 用 getattr 兜底，绝不能因为新增控件让「保存」抛异常。
        for _k, _attr in (("jvm", "ed_g_jvm"), ("game", "ed_g_game"),
                          ("pre_launch", "ed_g_pre"), ("window", "ed_g_window"),
                          ("server", "ed_g_server")):
            _w = getattr(self, _attr, None)
            if _w is not None:
                kv[_k] = _w.text().strip()
        lp.set_game_conf(self.item, **kv)
        msg = "已保存这个游戏的设置"
        # 有官启档案、且这里没选「跟随全局」的，顺手把内存写进档案 ——
        # 走官方启动器开局时也生效；跟随全局时不动档案，交给全局同步去管。
        pid = str(self.item.get("profile_id") or "")
        if pid and kv["memory"] != "global":
            mb = (int(self.mem_spin.value()) if kv["memory"] == "custom"
                  else lp.memory_for(self.item)[0])
            ok, m = installer.apply_memory_to_profile(
                pid, mb, str(self.item.get("game_dir") or "") or None)
            if not ok and m and "无需改动" not in m:
                msg += f"（档案内存未写入：{m}）"
        Toast.show_toast(self.parent(), msg, ms=3200, kind="success")
        self.accept()


def open_game_settings(parent, item, kind="instance"):
    """从「游戏管理」行里打开（统一入口，方便测试打桩）"""
    d = GameSettingsDialog(parent, item, kind)
    d.exec()
    return d
