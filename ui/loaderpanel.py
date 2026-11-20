# -*- coding: utf-8 -*-
"""主页「下载游戏」展开面板：选游戏版本 + 加载器 + 加载器版本，下载游戏本体。

纯加载器 = 只有 Fabric/Quilt/Forge/NeoForge 本体，不含任何模组。
开了「独立启动」之后，加载器可以选「无」—— 那就只下原版游戏，一个加载器都不装
（独立启动自己会拼命令行，不像官方启动器那样必须有个加载器版本才跑得起来）。
"""
import os

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)

from ..core import gamelaunch, loader
from ..core.asyncio_qt import run_async
from ..core.tasks import TASKS
from . import theme
from .dialogs import ProgressDailog
from .widgets import FrostCard, PillButton, PopupPanel, Toast

COMBO_QSS = (
    "QComboBox{background:rgba(255,255,255,190);border:1px solid rgba(31,39,51,45);"
    "border-radius:8px;padding:3px 8px;font-size:12px;color:#2a3442;}"
    "QComboBox:hover{border:1px solid rgba(47,124,246,120);}"
    "QComboBox::drop-down{border:none;width:14px;}"
    "QComboBox QAbstractItemView{background:#ffffff;border:1px solid rgba(31,39,51,45);"
    "selection-background-color:#2f7cf6;selection-color:#ffffff;font-size:12px;"
    "outline:0;}")

_preferred = "1.20.1"
NONE_KEY = ""          # 加载器下拉里「无」的 data

_SEG_QSS = (
    "QPushButton{border:none;border-radius:7px;font-size:11.5px;padding:0px;"
    "background:rgba(255,255,255,150);color:#4c5866;}"
    "QPushButton:hover{background:rgba(255,255,255,225);}"
    "QPushButton:checked{background:#2f7cf6;color:#ffffff;font-weight:600;}")

# 「独立启动」开着时的「官方」胶囊（第六十八轮）：灰掉、不给悬停反馈、
# 选中也不变蓝 —— 它此刻是个「不许选」的选项，只是还得能点（点了要弹提示）。
_SEG_QSS_LOCKED = (
    "QPushButton{border:none;border-radius:7px;font-size:11.5px;padding:0px;"
    "background:rgba(255,255,255,80);color:#b6bfca;}"
    "QPushButton:hover{background:rgba(255,255,255,80);}"
    "QPushButton:checked{background:rgba(31,39,51,24);color:#96a0ac;font-weight:600;}")

LOCK_SAY = "需要切换还请移步至设置→常规→关闭独立启动"

_LIVE = []          # 还活着的 TargetSwitch（弱引用），设置改完统一刷新灰态


def refresh_all_locks():
    """设置里改了「独立启动」之后，把屏幕上所有胶囊的灰态刷新一遍"""
    for ref in list(_LIVE):
        w = ref()
        if w is None:
            try:
                _LIVE.remove(ref)
            except ValueError:
                pass
            continue
        try:
            w.refresh_lock()
        except RuntimeError:
            pass


class TargetSwitch(QWidget):
    """「官方 / 应用」两个小胶囊：选现在看哪条游戏目录（第四十五轮起，第四十六轮扩用）。

    它同时是三处的「当前目录」：下载（装到哪）、游戏管理（列哪条的游戏）、
    选择要启动的游戏（列哪条的游戏）。三处写同一份配置，改哪边都一样。

      · 官方 → `<安装目录>\\official_assets\\.minecraft`，官方启动器用的那份；
      · 应用 → `<安装目录>\\.minecraft`，本启动器自己的地盘（独立启动用）。

    没亲手点过时跟随「独立启动」开关：关着默认官方，开着默认应用（见 `paths.default_target`）。
    """

    changed = Signal(str)

    # scope 决定「前缀小字」和点了之后的提示话术，免得在游戏管理里说「下载将装到」。
    # view（游戏管理）不写前缀 —— 标题旁边直接两个胶囊，跟用户描述的样子一致。
    SCOPE_LABEL = {"download": "下载到", "view": "", "launch": "列表"}
    SCOPE_SAY = {
        "download": "下载将装到「{cn}」目录：",
        "view": "现在只看「{cn}」目录里的游戏：",
        "launch": "现在只列「{cn}」目录里的游戏：",
    }

    def __init__(self, window=None, parent=None, scope="download"):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup, QPushButton
        from ..core import paths
        self.window_ref = window
        self._paths = paths
        self.scope = scope if scope in self.SCOPE_LABEL else "download"
        self.setFixedHeight(34)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 3, 0, 3)
        h.setSpacing(4)
        # 第八十二轮：**带父窗创建**。以前这里是 `QLabel(text)` + `setVisible(...)`：
        # 一只没有父窗的 QLabel 在被 addWidget 挂上之前就是顶层窗口，setVisible(True)
        # 等于把它当成窗口显示出来 —— 桌面上于是多一块白标题栏小窗（标题还是
        # applicationName「八桂启动器」）。先给父窗，再 setVisible。
        lb = QLabel(self.SCOPE_LABEL[self.scope], self)
        lb.setStyleSheet("color:#5a6675;font-size:11px;")
        lb.setVisible(bool(self.SCOPE_LABEL[self.scope]))     # 空前缀就别占位
        h.addWidget(lb)
        self._prefix = lb               # 前缀小字（下载到 / 列表；游戏管理没有）
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns = {}
        # 显示顺序：官方在前（用户描述的顺序），默认选中哪个看配置
        for i, key in enumerate(("official", "app")):
            b = QPushButton(paths.TARGET_CN[key])
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(44, 26)
            b.setStyleSheet(_SEG_QSS)
            self._group.addButton(b, i)
            b.clicked.connect(lambda _=False, k=key: self._click(k))
            h.addWidget(b)
            self._btns[key] = b
        self._locked = False
        self.refresh_lock()
        self.set_target(paths.target(), notify=False)
        self.setToolTip(self._tip_text())
        try:
            import weakref
            _LIVE.append(weakref.ref(self))
        except Exception:                                # noqa: BLE001
            pass

    # ---------------- 独立启动锁（第六十八轮） ----------------
    def _click(self, key):
        """点胶囊。独立启动开着时「官方」是死的 —— 点了只解释一句，不切。"""
        if key == "official" and getattr(self, "_locked", False):
            Toast.show_toast(self.window() or self, LOCK_SAY, ms=4200, kind="info")
            cur = self._btns.get(self._paths.target())
            if cur is not None:
                cur.setChecked(True)          # 视觉上弹回原来那个，别让玩家以为切过去了
            return
        self.set_target(key)

    def refresh_lock(self):
        """按「独立启动」开关重算「官方」能不能点。

        开着 = 只能看/只能装应用目录（独立启动自己拼命令行，官启那条目录用不上），
        所以「官方」灰掉；如果配置里还停在官方，顺手纠回应用 —— 否则界面显示灰着
        官方、实际下载却往官方目录塞，比不灰更糊涂。
        """
        locked = bool(self._paths.standalone_on())
        self._locked = locked
        b = self._btns.get("official")
        if b is not None:
            b.setStyleSheet(_SEG_QSS_LOCKED if locked else _SEG_QSS)
            b.setToolTip(LOCK_SAY if locked else
                         "官方启动器用的那份 .minecraft（下载到官方 / 看官方装了什么）")
        if locked and self._paths.target() != "app":
            try:
                self._paths.set_target("app")
            except Exception:                            # noqa: BLE001
                pass
            self.set_target("app", notify=False)
        self.setToolTip(self._tip_text())

    def _tip_text(self):
        p = self._paths
        locked = "　·　独立启动开着，官方目录暂时不可选" if getattr(
            self, "_locked", False) else ""
        return ("官方：官方启动器用的那份 .minecraft（下载到官方 / 看官方装了什么）\n"
                f"  {p.official_game_dir()}\n"
                "应用：本启动器自己的目录（独立启动 / 下载到应用）\n"
                f"  {p.app_game_dir()}\n"
                "没点过的时候跟着「独立启动」开关：关着=官方，开着=应用" + locked)

    def target(self):
        return self._paths.target()

    def set_target(self, key, notify=True):
        key = str(key or "").lower()
        if key not in self._paths.TARGETS:
            key = self._paths.default_target()
        b = self._btns.get(key)
        if b is not None:
            b.setChecked(True)
        if notify:
            self._paths.set_target(key)
            # 选到官方时顺手把目录准备好（骨架 + 零拷贝共享），别等点了下载才建
            if key == "official":
                run_async(self._paths.ensure_dir, "official",
                          on_done=lambda _r: None, on_err=lambda _e: None)
            if self.window_ref is not None:
                say = self.SCOPE_SAY[self.scope].format(
                    cn=self._paths.TARGET_CN[key])
                Toast.show_toast(self.window_ref,
                                 f"{say}{self._paths.target_dir(key)}",
                                 ms=3600, kind="success")
            self.setToolTip(self._tip_text())
            self.changed.emit(key)


class _CatSeg(QWidget):
    """版本分类小胶囊：正式 / 快照 / 远古 / 愚人（第七十二轮）。

    以前下拉里只有正式版，还只列前 60 个 —— 想装快照或者 1.7.10 之前的老版本，
    玩家在整个界面里找不到入口。现在分类单独一行，点哪类，下面「游戏版本」
    下拉就只列哪类的版本。
    """

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup, QPushButton
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns = {}
        for i, (key, cn) in enumerate(loader.CATS):
            b = QPushButton(cn)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(24)
            b.setMinimumWidth(42)
            b.setStyleSheet(_SEG_QSS)
            b.setToolTip(f"只看「{cn}」版本")
            self._group.addButton(b, i)
            b.clicked.connect(lambda _=False, k=key: self._click(k))
            h.addWidget(b)
            self._btns[key] = b
        h.addStretch(1)
        self._cur = "release"
        self._counts = {}
        self._btns["release"].setChecked(True)

    def current(self):
        return self._cur

    def set_counts(self, counts):
        """把每类的数量写进悬停提示 —— 不点也知道「愚人」里有几个"""
        self._counts = dict(counts or {})
        for key, b in self._btns.items():
            n = self._counts.get(key)
            cn = loader.CAT_CN.get(key, key)
            b.setToolTip(f"{cn}：{n} 个版本" if n is not None else f"只看「{cn}」版本")

    def set_current(self, key, notify=True):
        key = str(key or "")
        if key not in self._btns or key == self._cur:
            return
        self._cur = key
        self._btns[key].setChecked(True)
        if notify:
            self.changed.emit(key)

    def _click(self, key):
        if key == self._cur:
            return
        self.set_current(key)


class LoaderPopup(PopupPanel):
    def __init__(self, parent, anchor):
        super().__init__(parent, anchor)
        self._mc_loaded = False
        self._catalog = []               # 全部版本（含分类），第七十二轮
        self._busy = False
        self._auto_picked = False        # 已经为“该 MC 没这个加载器”自动切过一次
        self._auto_note = ""             # 自动切换的提示，成功后拼在 tip 前面
        # 「无」只在开着独立启动时给：那种模式自己拼命令行，原版裸游戏也能直接起；
        # 没开独立启动时玩家只能靠官启启动，官启那边裸原版是另一个入口，别在这儿混。
        self._allow_none = gamelaunch.standalone_enabled()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = FrostCard(self, radius=16, tint=246)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        t = QLabel("下载游戏")
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t)
        head.addStretch(1)
        # 「下载游戏」字样右边：官方 / 应用 两个小胶囊（第四十五轮）
        # 主页上不显示它 —— 玩家点了下载才需要决定装到哪一条。
        win = anchor.window() if anchor is not None else None
        self.target_switch = TargetSwitch(win, card)
        self.target_switch.changed.connect(lambda _k: self._sync_dest())
        head.addWidget(self.target_switch)
        lay.addLayout(head)

        sub = QLabel("下载游戏本体：原版 + 想要的加载器。"
                     + ("加载器选「无」就只下原版。" if self._allow_none else "")
                     + "装好后在官方启动器的版本列表里可选。")
        sub.setStyleSheet("color:#5a6675;font-size:11px;")
        sub.setWordWrap(True)
        # Popup 里 wordWrap 的 QLabel 定不出高度，会跟下一行叠在一起 ——
        # 先给死宽度（360 - 左右各 18 的边距），heightForWidth 才有意义。
        sub.setFixedWidth(324)
        lay.addWidget(sub)

        def row(label, widget):
            h = QHBoxLayout()
            h.setSpacing(8)
            lb = QLabel(label)
            lb.setFixedWidth(58)
            lb.setStyleSheet("color:#4c5866;font-size:12px;")
            h.addWidget(lb)
            h.addWidget(widget, 1)
            lay.addLayout(h)
            return widget

        self.cb_mc = row("游戏版本", QComboBox())
        # 分类行：点「快照」下面下拉就只列快照（第七十二轮）
        self.seg_cat = row("分类", _CatSeg())
        self.seg_cat.changed.connect(lambda _k: self._fill_mc())
        self.cb_kind = row("加载器", QComboBox())
        self.cb_ver = row("加载器版本", QComboBox())
        for cb in (self.cb_mc, self.cb_kind, self.cb_ver):
            cb.setStyleSheet(COMBO_QSS)
            cb.setFixedHeight(28)

        self.cb_mirror = QCheckBox("强制使用国内镜像（官方源连不上时用）")
        self.cb_mirror.setStyleSheet("color:#4c5866;font-size:11px;")
        self.cb_mirror.setToolTip(
            "官方仓库 maven.neoforged.net / maven.minecraftforge.net 在国内经常连不上。\n"
            "不勾选时默认先试官方、失败自动换 BMCLAPI 镜像；勾选后直接走镜像。")
        self.cb_mirror.setChecked(loader.mirror_mode() == "always")
        self.cb_mirror.toggled.connect(self._on_mirror_toggle)
        lay.addWidget(self.cb_mirror)

        for key, cn in loader.KINDS:
            self.cb_kind.addItem(cn, key)
        if self._allow_none:
            self.cb_kind.insertItem(0, "无（只下原版）", NONE_KEY)
            # insertItem(0) 会把 currentIndex 顶到 1，手动回 0 太反直觉 ——
            # 保持默认还是第一个真加载器，想只要原版的自己选「无」。
            self.cb_kind.setCurrentIndex(1)
        self.cb_mc.addItem("读取中…")
        self.cb_ver.addItem("—")

        self.tip = QLabel("")
        self.tip.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.tip.setWordWrap(True)
        # 提示文字会在一行/两行之间变（选「无」、报错、成功…）。高度定住，
        # 面板就不会在显示之后往下长、把「开始下载」顶出窗口。
        self.tip.setFixedWidth(324)
        self.tip.setMinimumHeight(32)
        lay.addWidget(self.tip)

        # 下载会装到哪：标题行那两个胶囊（官方 / 应用）选的就是它
        from ..core import paths as _paths
        self.lb_dest = QLabel("")
        self.lb_dest.setStyleSheet("color:#8a95a3;font-size:10.5px;")
        self.lb_dest.setWordWrap(True)
        self.lb_dest.setFixedWidth(324)
        lay.addWidget(self.lb_dest)
        self._sync_dest()

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel = PillButton("关闭", "ghost")
        cancel.setFixedHeight(30)
        cancel.setMinimumWidth(64)
        cancel.clicked.connect(self.close)
        btns.addWidget(cancel)
        self.btn_go = PillButton("开始下载", "accent")
        self.btn_go.setFixedHeight(30)
        self.btn_go.setMinimumWidth(88)
        self.btn_go.clicked.connect(self._start)
        btns.addWidget(self.btn_go)
        lay.addLayout(btns)

        self.cb_mc.currentIndexChanged.connect(lambda _: self._load_loader_versions())
        self.cb_kind.currentIndexChanged.connect(lambda _: self._load_loader_versions())
        self.setFixedWidth(360)
        self.adjustSize()
        self._load_mc()

    # ---------------- 数据 ----------------
    def _sync_dest(self):
        """把「装到哪」写在面板上 —— 玩家不用回主页确认自己选的是官方还是应用"""
        from ..core import paths as _paths
        t = _paths.target()
        self.lb_dest.setText(
            f"安装到「{_paths.TARGET_CN[t]}」：{_paths.target_dir(t)}")

    def _on_mirror_toggle(self, on):
        loader.set_mirror_mode("always" if on else "auto")
        if self._mc_loaded:
            self._load_loader_versions()

    def _load_mc(self):
        def done(vs):
            # vs 是完整目录（含快照 / 远古 / 愚人）；老接口只给正式版，也兜住
            self._mc_loaded = True
            self._catalog = [x for x in (vs or []) if isinstance(x, dict)]
            if not self._catalog:
                self.cb_mc.blockSignals(True)
                self.cb_mc.clear()
                self.cb_mc.addItem("读取失败（检查网络）")
                self.cb_mc.blockSignals(False)
                self.tip.setText("无法获取版本列表，请检查网络后重开面板。")
                return
            counts = {}
            for x in self._catalog:
                counts[x.get("cat", "release")] = counts.get(x.get("cat", "release"), 0) + 1
            self.seg_cat.set_counts(counts)
            # 默认停在「正式」：玩家十有八九只是想要个能和朋友联机的正式版
            pref_cat = next((x["cat"] for x in self._catalog
                             if x.get("id") == _preferred), "release")
            self.seg_cat.set_current(pref_cat, notify=False)
            self._fill_mc()

        def err(e):
            self.cb_mc.clear()
            self.cb_mc.addItem("读取失败")
            self.tip.setText(str(e)[:80])

        run_async(loader.mc_catalog, on_done=done, on_err=err)

    def _fill_mc(self, load=True):
        """按当前分类重填「游戏版本」下拉；能保住原来的选择就保住"""
        cat = self.seg_cat.current()
        want = self.cb_mc.currentText() if self.cb_mc.count() else _preferred
        ids = [x["id"] for x in self._catalog if x.get("cat") == cat]
        self.cb_mc.blockSignals(True)
        self.cb_mc.clear()
        for vid in ids:
            self.cb_mc.addItem(vid)
        idx = self.cb_mc.findText(want)
        if idx < 0:
            idx = self.cb_mc.findText(_preferred)
        self.cb_mc.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_mc.blockSignals(False)
        cn = loader.CAT_CN.get(cat, cat)
        self.cb_mc.setToolTip(f"{cn}版本共 {len(ids)} 个，新版本在上"
                              + ("；这类只有老版本，装好后建议配独立启动" if cat == "ancient" else ""))
        if not ids:
            self.tip.setText(f"「{cn}」里没有版本 —— 可能清单没取全，换个分类试试。")
            return
        if load:
            self._load_loader_versions()

    def _load_loader_versions(self, keep_note=False):
        if not self._mc_loaded:
            return
        if not keep_note:
            self._auto_note = ""
        mc = self.cb_mc.currentText()
        kind = self.cb_kind.currentData()

        # 加载器 = 无：只下原版，没有「加载器版本」这回事
        if kind == NONE_KEY:
            self.cb_ver.blockSignals(True)
            self.cb_ver.clear()
            self.cb_ver.addItem("—")
            self.cb_ver.setCurrentIndex(0)
            self.cb_ver.setEnabled(False)
            self.cb_ver.blockSignals(False)
            self.tip.setText(f"只下载原版 {mc}：不装任何加载器，"
                             f"装好就能在「独立启动」里直接开局。")
            return
        self.cb_ver.setEnabled(True)

        if not mc or not kind:
            return
        self.cb_ver.blockSignals(True)
        self.cb_ver.clear()
        self.cb_ver.addItem("读取中…")
        self.cb_ver.blockSignals(False)
        self.tip.setText("")

        def stale():
            """取版本是异步的：玩家可能已经换了加载器/游戏版本，
            这批结果回来时就不能再改界面（否则「无（只下原版）」会被
            上一次请求的提示和版本列表覆盖回去）"""
            return (self.cb_kind.currentData() != kind
                    or self.cb_mc.currentText() != mc)

        def done(vs):
            if stale():
                return
            self.cb_ver.blockSignals(True)
            self.cb_ver.clear()
            if not vs:
                self.cb_ver.addItem("无可用版本")
                self.cb_ver.blockSignals(False)
                err = loader.loader_error(kind, mc)
                if err:
                    self.tip.setText(f"获取版本失败：{err}。\n"
                                     f"可勾选上方“强制使用国内镜像”后重试，或换个游戏版本。")
                    return
                # 该 MC 版本本来就没有这个加载器（例如 NeoForge 没有 1.20.1）
                # 挑最近可用版本时要拿**全部正式版**比，不能只看当前分类的下拉
                alt = loader.suggest_mc(kind, loader.mc_versions(), mc)
                if alt and alt != mc and not self._auto_picked:
                    self._auto_picked = True
                    idx = self.cb_mc.findText(alt)
                    if idx < 0:
                        # alt 在别的分类里（例如当前停在「快照」）—— 先把分类切过去
                        cat = next((x["cat"] for x in self._catalog
                                    if x.get("id") == alt), "")
                        if cat:
                            self.seg_cat.set_current(cat, notify=False)
                            self._fill_mc(load=False)
                            idx = self.cb_mc.findText(alt)
                    if idx >= 0:
                        self.cb_mc.blockSignals(True)
                        self.cb_mc.setCurrentIndex(idx)
                        self.cb_mc.blockSignals(False)
                        self._auto_note = (f"{loader.KIND_CN.get(kind, kind)} 不支持 {mc}，"
                                           f"已自动切到 {alt}")
                        self._load_loader_versions(keep_note=True)
                        return
                self.tip.setText(
                    f"{loader.KIND_CN.get(kind, kind)} 可能不支持 {mc}，换个版本试试。")
                return
            self._auto_picked = False
            for v in vs:
                self.cb_ver.addItem(v)
            self.cb_ver.setCurrentIndex(0)
            self.cb_ver.blockSignals(False)
            src = "国内镜像" if loader.last_used_mirror() else "官方源"
            pre = self._auto_note + "。" if self._auto_note else ""
            self.tip.setText(f"{pre}共 {len(vs)} 个可用版本，默认最新（来源：{src}）。")

        def err(e):
            if stale():
                return
            self.cb_ver.clear()
            self.cb_ver.addItem("读取失败")
            self.tip.setText(str(e)[:80])

        run_async(loader.loader_versions, kind, mc, on_done=done, on_err=err)

    # ---------------- 安装 ----------------
    def _start(self):
        if self._busy:
            return
        mc = self.cb_mc.currentText()
        kind = self.cb_kind.currentData()
        lver = self.cb_ver.currentText()
        only_vanilla = (kind == NONE_KEY)
        if not mc or mc in ("读取中…", "读取失败（检查网络）"):
            Toast.show_toast(self.parent(), "请先选择有效的版本")
            return
        if not only_vanilla:
            if not kind or not lver or lver in ("读取中…", "无可用版本", "读取失败"):
                Toast.show_toast(self.parent(), "请先选择有效的版本")
                return
        self._busy = True
        self.btn_go.setEnabled(False)
        self.btn_go.setText("下载中…")
        self.close()

        if only_vanilla:
            title = f"下载原版 {mc}"
            desc = f"原版：MC {mc}"
        else:
            cn = loader.KIND_CN.get(kind, kind)
            title = f"下载 {cn}"
            desc = f"加载器：{cn} {lver}（MC {mc}）"
        task = TASKS.create(desc, "loader")
        dlg = ProgressDailog(self.parent(), title=title)

        def finished(t):
            self._busy = False
            self.btn_go.setEnabled(True)
            self.btn_go.setText("开始下载")
            if t.status == "done":
                if only_vanilla:
                    Toast.show_toast(
                        self.parent(),
                        f"已下载原版 {mc} 到「{tkey}」目录：到「游戏管理」里点 ▶ 选它，"
                        f"就能用独立启动开局",
                        ms=5600, kind="success")
                else:
                    Toast.show_toast(self.parent(),
                                     f"已安装 {desc.split('：', 1)[1]} 到「{tkey}」目录，"
                                     f"官方启动器可直接选择该版本",
                                     ms=5200, kind="success")
            elif t.status == "failed":
                Toast.show_toast(self.parent(), t.message or "安装失败",
                                 ms=6000, kind="error")

        dlg.bind(task, on_finished=finished)
        dlg.show()

        from ..core import paths as _paths
        gdir = _paths.download_dir()
        tkey = _paths.TARGET_CN[_paths.target()]

        def work():
            if only_vanilla:
                return loader.install_vanilla_only(mc, task.tid, game_dir=gdir)
            # keep_visible=True（第一百一十二轮）：主页下载的加载器版本是玩家要
            # 直接玩的游戏 —— 留在 versions/ 并显示，不搬 loaders/、不藏列表
            return loader.install_loader(mc, kind, lver, task.tid, game_dir=gdir,
                                         keep_visible=True)

        def done(res):
            ok, info = res if isinstance(res, tuple) else (False, str(res))
            if ok:
                TASKS.finish(task.tid, "done", f"版本 {info} 已就绪")
            else:
                TASKS.finish(task.tid, "failed", info)

        def err(e):
            msg = str(e)
            if "已取消" in msg:
                TASKS.finish(task.tid, "cancelled", "已取消")
            else:
                TASKS.finish(task.tid, "failed", msg[:150])

        run_async(work, on_done=done, on_err=err)

    # ---------------- 定位 ----------------
    def popup(self):
        """贴按钮弹出，但整块必须留在主窗里（位置算法见 PopupPanel.place）。

        主页按钮在卡片中下部，面板又有 300 多高 —— 只夹左右不夹上下的话，
        下面那截（开始下载按钮）会掉到窗口外面去。这里额外先把「装到哪」
        那行刷新一遍再算尺寸，否则首次弹出高度会少一行。
        """
        self._sync_dest()
        super().popup()


class LoaderButton(PillButton):
    """主页「打开游戏目录」右侧的展开按钮：下载游戏（原版 / 原版+加载器）"""

    def __init__(self, window, parent=None):
        super().__init__("下载游戏", "ghost", icon="download", parent=parent)
        self.window_ref = window
        self._popup = None
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("下载游戏本体（原版，或原版 + Fabric / Quilt / Forge / NeoForge）\n"
                        "装到哪由右边「官方 / 应用」决定")
        self.clicked.connect(self._toggle)

    def _toggle(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.close()
            return
        self._popup = LoaderPopup(self.window_ref, self)
        self._popup.popup()
