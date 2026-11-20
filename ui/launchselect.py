# -*- coding: utf-8 -*-
"""「游戏启动选择」面板（第三十一轮起，第三十二轮改成右侧抽屉）。

只在**开启独立启动**时出现：顶上的「整合包 / 模组 / 数据包 / 光影 / 材质包」点一次是
下载页，**同一个再点一次就在下载页右边滑出这个面板** —— 不用回主页也能挑游戏开局，
而且左边的下载页还留在原处，不占满整个窗口。

没开独立启动时这个面板根本不会出现（switch_page 直接走下载页），
因为那时候启动游戏只能走官方启动器，列一堆「启动」按钮只会误导。
"""
import os

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                               QWidget)

from ..core import categories as cats
from ..core import gamelaunch, importer, installer, paths
from ..core.asyncio_qt import run_async
from . import gamecats as gc
from . import theme
from .badge import LoaderBadge
from .importui import ImportGameDialog, _AddedRow, render_added_list
from .widgets import ElideLabel, FrostCard, IconButton, PillButton, Toast


class _PickRow(QWidget):
    """一行可选游戏：图标 + 名称 + 说明 + 「启动」

    第五十七轮起**整行可点**：点哪儿都算「选中它」（启动目标立刻换成这条），
    右下角「启动」按钮变成「选中后立刻开局」的快捷方式。选中态要明显 ——
    蓝边 + 白底 + 一个 ✓，玩家一眼能看出「现在启动的就是这个」。
    """

    clicked = Signal(object)          # 点整行（不含两个按钮）

    def __init__(self, item, kind, page):
        super().__init__()
        self.item = item
        self.kind = kind
        self.page = page
        self._selected = False
        self._batch = False          # 第 106 轮：_apply_style() 在构造早期就会读
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("pickRow")
        self.setFixedHeight(60)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False
        self._apply_style()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)

        self.badge = LoaderBadge(item, kind, 32)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setSpacing(2)
        self.lb_name = ElideLabel(str(item.get("name") or item.get("id") or "-"))
        f = QFont(theme.FONT)
        f.setPixelSize(12.5)
        f.setBold(True)
        self.lb_name.setFont(f)
        self.lb_name.setStyleSheet("background:transparent;")
        box.addWidget(self.lb_name)

        self.lb_meta = ElideLabel(self._meta())
        self.lb_meta.setToolTip(self._meta())
        self.lb_meta.setStyleSheet("color:#63707f;font-size:10.5px;"
                                   "background:transparent;")
        box.addWidget(self.lb_meta)
        lay.addLayout(box, 1)

        # 选中标记：单个 ✓ 就够（宽度只有十几个像素，不挤名字）
        self.lb_sel = QLabel("✓")
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        f.setBold(True)
        self.lb_sel.setFont(f)
        self.lb_sel.setStyleSheet("color:#2f7cf6;background:transparent;")
        self.lb_sel.setToolTip("这就是当前启动目标")
        self.lb_sel.hide()
        lay.addWidget(self.lb_sel, 0, Qt.AlignVCenter)

        self.btn_go = PillButton("启动", "accent")
        self.btn_go.setFixedHeight(28)
        self.btn_go.setFixedWidth(64)
        self.btn_go.clicked.connect(self._launch)
        lay.addWidget(self.btn_go, 0, Qt.AlignVCenter)

        self.btn_dir = PillButton("目录", "ghost")
        self.btn_dir.setFixedHeight(28)
        self.btn_dir.setFixedWidth(58)
        self.btn_dir.setToolTip("打开所在文件夹")
        self.btn_dir.clicked.connect(self._open_dir)
        lay.addWidget(self.btn_dir, 0, Qt.AlignVCenter)

        # 第 106 轮：Ctrl 点选批量 / 长按拖到分类（带图标、多条堆叠）
        self._press = QPoint()
        self._drag_ready = False
        self._pending_remove = False    # 第一一二轮：松开 Ctrl 后按下已选行，
                                        # 长按=整批拖走；快速松开才取消这一项
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(420)
        self._hold.timeout.connect(self._on_hold)
        self.setToolTip("点一下选为启动目标；按住 Ctrl 点选可批量，长按拖到分类")

    # ---- 展示 ----
    def _vid(self):
        return str(self.item.get("version_id") or self.item.get("id")
                   or os.path.basename(str(self.item.get("path") or "")))

    def _is_target(self):
        t = gamelaunch.launch_target()
        return bool(t) and str(t.get("version_id") or "") == self._vid()

    def _meta(self):
        """一行灰字：**先讲最要紧的**（是不是当前目标、是包还是版本），再讲版本号。

        第五十七轮加了 ✓ 之后这行更窄，原来「当前目标 · 整合包」这种尾巴会被
        省略号吃掉 —— 而它恰恰是玩家最需要一眼看到的两件事。
        """
        it = self.item
        bits = []
        if self._is_target():
            bits.append("当前目标")
        bits.append("整合包" if self.kind == "instance" else "版本")
        mc = str(it.get("mc") or "")
        if mc:
            bits.append(f"MC {mc}")
        ld = str(it.get("loader") or "")
        if ld:
            cn = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric",
                  "quilt": "Quilt"}.get(ld, ld.capitalize())
            bits.append(f"{cn} {it.get('loader_ver') or ''}".strip())
        else:
            bits.append("原版")
        return " · ".join([b for b in bits if b])

    def _apply_style(self):
        if self._batch:
            bg = "rgba(47,124,246,58)"
            edge = "border:1.5px solid rgba(47,124,246,220);"
        elif self._selected:
            bg = "rgba(255,255,255,240)"
            edge = "border:1.5px solid rgba(47,124,246,220);"
        else:
            bg = ("rgba(255,255,255,200)" if self._hover
                  else "rgba(255,255,255,150)")
            edge = ("border:1.5px solid rgba(47,124,246,150);"
                    if self._is_target() else "")
        # 限定 objectName：写成 QWidget{...} 会级联到行内所有子控件
        self.setStyleSheet(f"#pickRow{{background:{bg};border-radius:10px;{edge}}}")
        try:
            self.lb_sel.setVisible(self._selected)
        except AttributeError:
            pass

    def set_batch_selected(self, on):
        on = bool(on)
        if on == self._batch:
            return
        self._batch = on
        self._apply_style()

    def set_selected(self, on):
        """页面统一调用：整列只留一个高亮（别各自 setStyleSheet 打架）"""
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._apply_style()
        self.update()

    def refresh_meta(self):
        """选中态变了，「当前目标」那几个字要跟着换（不重建整张列表，省一次读盘）"""
        try:
            txt = self._meta()
            self.lb_meta.setText(txt)
            # 加了 ✓ 之后这一行更挤，「整合包/版本」常被省略号吃掉 —— 悬停给全文
            self.lb_meta.setToolTip(txt)
        except Exception:
            pass

    def enterEvent(self, e):
        self._hover = True
        self._apply_style()

    def leaveEvent(self, e):
        self._hover = False
        self._apply_style()

    def mousePressEvent(self, e):
        """整行可点 = 选中（第五十七轮）。

        必须先排除「启动」「目录」两个按钮：不排除的话点按钮会先被这里吞掉，
        变成「点一下只是选中、得再点一次才启动」，手感莫名其妙。
        第 106 轮再分岔：Ctrl 点选 / 松开 Ctrl 点已选项取消，都只改批量选中态，
        **不切启动目标**，也不启动长按拖拽。
        """
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)
            return
        self._press = e.position().toPoint()
        self._reset_drag()
        if self._on_button(self._press):
            super().mousePressEvent(e)
            return
        sel = getattr(self.page, "selection", None)
        if sel is not None:
            act = gc.press_action(sel, cats.game_key(self.item))
            if act == "add":
                self.page._sync_batch_styles()
                return
            if act == "remove":
                # 第一一二轮：press_action 按下时已把这项移出集合 —— 先放回去，
                # 长按(420ms)进入拖拽 = 整批（含这一条）一起拖；快速松开才真正取消
                sel.add(cats.game_key(self.item))
                self._pending_remove = True
                self._hold.start()
                return
        self._hold.start()
        self._select()
        return

    def mouseMoveEvent(self, e):
        if self._drag_ready:
            pos = e.position().toPoint()
            if (pos - self._press).manhattanLength() > 7:
                self._start_drag()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        pending = self._pending_remove
        dragged = self._drag_ready      # 拖过的话 _start_drag 里已清掉 pending
        self._reset_drag()
        if pending and not dragged:
            sel = getattr(self.page, "selection", None)
            if sel is not None:
                sel.remove(cats.game_key(self.item))
                self.page._sync_batch_styles()
        super().mouseReleaseEvent(e)

    def _on_hold(self):
        self._drag_ready = True
        self.setCursor(Qt.ClosedHandCursor)

    def _reset_drag(self):
        was = self._drag_ready
        self._hold.stop()
        self._drag_ready = False
        self._pending_remove = False
        if was:
            self.setCursor(Qt.ArrowCursor)

    def _start_drag(self):
        try:
            key = cats.game_key(self.item)
            sel = getattr(self.page, "selection", None)
            rows, names = [], []
            if sel is not None and sel.count() > 1 and sel.has(key):
                index = getattr(self.page, "_rows_by_key", {}) or {}
                for k in sel.keys_snapshot():
                    info = index.get(k)
                    if not info:
                        continue
                    r, kd = info
                    rows.append((k, kd))
                    names.append(gc.item_name(getattr(r, "item", None)
                                              or getattr(r, "entry", None)))
            if not rows:
                rows = [(key, gc.row_kind(self.kind))]
                names = [gc.item_name(self.item)]
            drag = gc.make_drag(self, rows, names, len(rows))
            self._reset_drag()
            if drag is not None:
                drag.exec(Qt.CopyAction)
        except Exception as e:                              # noqa: BLE001
            # 第一一一轮教训：这里吞过 NameError（gamecats 漏 import QDrag），
            # 玩家只能看到「拖不动」，连日志都没有 —— 必须留痕
            try:
                from ..core import crashlog
                crashlog.note("启动选择：拖拽启动失败 %s" % e)
            except Exception:                               # noqa: BLE001
                pass
            self._reset_drag()

    def _on_button(self, pos):
        w = self.childAt(pos)
        while w is not None and w is not self:
            if w in (self.btn_go, self.btn_dir):
                return True
            w = w.parentWidget()
        return False

    # ---- 动作 ----
    def _target_kw(self):
        """写进配置的目标信息（两条动作共用，避免一处漏 game_dir）"""
        return dict(kind=self.kind, version_id=self._vid(),
                    path=self.item.get("path") or self.item.get("dir") or "",
                    name=self.item.get("name") or self.item.get("id") or self._vid(),
                    memory_mb=self.item.get("memory_mb") or 0,
                    game_dir=paths.target_dir())

    def _select(self):
        """点卡片：把启动目标换成这条，两侧启动按钮的文字跟着变。"""
        kw = self._target_kw()
        name = kw["name"]
        win = self.page.window_ref
        if self._is_target() and gamelaunch.launch_mode() == "standalone":
            Toast.show_toast(win, f"「{name}」已经是启动目标了", ms=2000)
            self.page.select_row(self)
            return
        gamelaunch.set_launch_target(kw["kind"], kw["version_id"], kw["path"],
                                     kw["name"], kw["memory_mb"],
                                     game_dir=kw["game_dir"])
        self.page.select_row(self)
        if not gamelaunch.standalone_enabled():
            Toast.show_toast(win, f"已记下「{name}」。到 设置 → 常规 打开「独立启动」"
                                  "后，点启动按钮就能直接开局", ms=4600, kind="info")
            self._refresh_ui()
            return
        gamelaunch.set_launch_mode("standalone")
        self._refresh_ui()
        Toast.show_toast(win, f"已选中「{name}」：右上角启动按钮已切成它，点一下开局",
                         ms=3000)

    def _refresh_ui(self):
        win = self.page.window_ref
        hook = getattr(win, "refresh_launch_ui", None)
        if hook:
            try:
                hook()
            except Exception:
                pass

    def _open_dir(self):
        p = str(self.item.get("path") or self.item.get("dir") or "")
        if os.path.isdir(p):
            try:
                os.startfile(p)      # noqa: S606
            except Exception:
                pass

    def _launch(self):
        kw = self._target_kw()
        name = kw["name"]
        gamelaunch.set_launch_target(kw["kind"], kw["version_id"], kw["path"],
                                     kw["name"], kw["memory_mb"],
                                     game_dir=kw["game_dir"])
        gamelaunch.set_launch_mode("standalone")
        self.page.select_row(self)
        self._refresh_ui()
        Toast.show_toast(self.page.window_ref, f"正在启动「{name}」…", ms=2000)
        self.page.launch_async(name, self.page.window_ref)


class _AddedHost:
    """把抽屉伪装成 `_AddedRow` 眼里的那个 dialog。

    「已导入」那一行原本是给 AddedGamesDialog 写的（按钮回调里调 dialog.run_added /
    import_added / reload_added）。这里不去复制一套行控件，直接给它一个替身，
    行为跟游戏管理里那个窗口完全一致，只是不弹新窗口 —— 列表就长在抽屉里。
    """

    def __init__(self, page, win):
        self._page = page
        self._win = win
        # 抽屉里那几个收放式文件夹的展开状态（刷新列表时不重置）
        self._open_ids = set()

    def parent(self):
        return self._win

    # 第 106 轮：给 `_AddedRow` 的批量选中钩子（与版本行走同一套 Selection）
    @property
    def selection(self):
        return self._page.selection

    @property
    def rows_by_key(self):
        return self._page._rows_by_key

    def _sync_batch_styles(self):
        self._page._sync_batch_styles()

    def reload_added(self, force=True):
        # 移除 / 配置之后登记真的变了，强制重读一次并作废缓存
        gc.invalidate_added()
        self._page.reload(force=force)

    def run_added(self, entry):
        gd = str(entry.get("game_dir") or "")
        vid = str(entry.get("version_id") or "")
        name = entry.get("name") or vid or "-"
        if not gd or not vid:
            Toast.show_toast(self._win,
                             "这个目录里没有能直接运行的版本 —— 先点「配置至官方启动器」",
                             ms=4600, kind="error")
            return
        gamelaunch.set_launch_target(entry.get("kind") or "instance", vid,
                                     entry.get("path") or "", name, 0, game_dir=gd)
        refresh = getattr(self._win, "refresh_launch_ui", None)
        # 按当前启动方式分工（跟游戏管理 →「已导入」里那颗「运行」一致）：
        # 官方式 = 打开官方启动器，让它在自己的版本列表里开局；独立式才直接开。
        if not gamelaunch.standalone_enabled():
            Toast.show_toast(self._win,
                             f"用官方启动器打开「{name}」…", ms=2000)
            if refresh:
                refresh()
            from .widgets import launch_official_async
            launch_official_async(self._win)
            return
        gamelaunch.set_launch_mode("standalone")
        if refresh:
            refresh()
        Toast.show_toast(self._win, f"正在直接启动「{name}」（不复制文件）…", ms=2200)
        self._page.launch_async(name, self._win)

    def pick_added(self, row):
        """抽屉里点「已导入」的某一行 = 选它当启动目标（第九十三轮）。

        只是**选中**，不开局（开局要点那颗「运行」）—— 跟上面「游戏版本」那栏的
        卡片行为保持一致，不然点一下就直接启动，玩家想先看看别的都没机会。
        """
        e = row.entry or {}
        gd = str(e.get("game_dir") or "")
        vid = str(e.get("version_id") or "")
        name = e.get("name") or vid or "-"
        if not gd or not vid:
            Toast.show_toast(self._win,
                             "这个目录里没有能直接运行的版本 —— 先去游戏管理里把它"
                             "「导入」到官方启动器", ms=4600, kind="error")
            return
        gamelaunch.set_launch_target(e.get("kind") or "instance", vid,
                                     e.get("path") or "", name, 0, game_dir=gd)
        self._page.select_row(row)
        refresh = getattr(self._win, "refresh_launch_ui", None)
        if refresh:
            try:
                refresh()
            except Exception:                             # noqa: BLE001
                pass
        if not gamelaunch.standalone_enabled():
            Toast.show_toast(self._win,
                             f"已记下「{name}」。到 设置 → 常规 打开「独立启动」后，"
                             "点启动按钮就能直接开局", ms=4600, kind="info")
            return
        gamelaunch.set_launch_mode("standalone")
        Toast.show_toast(self._win,
                         f"已选中「{name}」：右上角启动按钮已切成它，点一下开局",
                         ms=3000)

    def import_added(self, entry):
        d = ImportGameDialog(self._win, preset=[dict(entry, imported=False)])
        d.exec()
        gc.invalidate_added()
        gc.invalidate_games()
        self._page.reload(force=True)


class LaunchSelectPage(QWidget):
    """独立启动的「游戏启动选择」抽屉面板（贴在下载页右侧，不占满窗口）"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ref = window
        self.on_close = None          # 主窗注入：点「×」收起面板
        self._rows = []
        self._busy = False
        self._mode = "versions"       # versions | added（「已导入」那一栏）
        self._added_host = _AddedHost(self, window)
        # 第 106 轮：会话缓存 + 批量选中 + 分类变更订阅
        self._cached = None
        self._rows_by_key = {}
        self.selection = gc.Selection(on_change=self._sync_batch_styles)
        gc.subscribe_widget(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = FrostCard(radius=18, tint=200)
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        t = QLabel("选择要启动的游戏")
        f = QFont(theme.FONT)
        f.setPixelSize(15)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t)
        head.addStretch(1)
        # 第四十六轮：跟游戏管理同一套「官方 / 应用」（同一份配置、同一个默认规则）
        from .loaderpanel import TargetSwitch
        self.target_switch = TargetSwitch(window, self.card, scope="launch")
        self.target_switch.changed.connect(lambda _k: self.reload())
        head.addWidget(self.target_switch)
        self.btn_close = IconButton("close", 26, 12, color="#5a6675")
        self.btn_close.setToolTip("收起面板（回到下载页）")
        self.btn_close.clicked.connect(self._close)
        head.addWidget(self.btn_close)
        lay.addLayout(head)

        self.lb_tip = QLabel("点卡片选中要启动的游戏（启动按钮的名字会跟着换），"
                             "点「启动」直接用它的目录开局。")
        self.lb_tip.setStyleSheet("color:#4c5866;font-size:11px;")
        self.lb_tip.setWordWrap(True)
        lay.addWidget(self.lb_tip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{width:8px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(90,102,117,90);"
            "border-radius:4px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        self.host = QWidget()
        self.host.setStyleSheet("background:transparent;")
        self.body = QVBoxLayout(self.host)
        self.body.setContentsMargins(0, 0, 4, 0)
        self.body.setSpacing(6)
        self.scroll.setWidget(self.host)
        # 第一一二轮：列表空白处接「拖出分类」—— 从文件夹拖出来松手即移出
        gc.enable_drag_out(self.host)
        lay.addWidget(self.scroll, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_refresh = PillButton("刷新", "ghost")
        self.btn_refresh.setFixedHeight(28)
        self.btn_refresh.setToolTip("重新读一次（平时展开不会再读盘）")
        self.btn_refresh.clicked.connect(lambda: self.reload(force=True))
        foot.addWidget(self.btn_refresh)
        # 「已导入」：就在刷新右边，点开直接在抽屉里列出登记过的游戏（不另开窗口）
        self.btn_added = PillButton("已导入", "ghost")
        self.btn_added.setFixedHeight(28)
        self.btn_added.setToolTip("列出游戏管理里「已导入」的那些游戏，可直接运行")
        self.btn_added.clicked.connect(self._toggle_added)
        foot.addWidget(self.btn_added)
        # 第 105 轮：「已导入」右侧的创建分类，用法与游戏管理头部「+」完全一致
        # （同一个 CategoryDialog、同一份 categories 数据层、建出来也是空的）。
        self.btn_cat = PillButton("创建分类", "ghost")
        self.btn_cat.setFixedHeight(28)
        self.btn_cat.setToolTip("给游戏版本 / 整合包建分类；建好后到"
                                "「游戏管理」长按游戏拖进去")
        self.btn_cat.clicked.connect(self._create_category)
        foot.addWidget(self.btn_cat)
        foot.addStretch(1)
        self.lb_count = QLabel("")
        self.lb_count.setStyleSheet("color:#8a95a3;font-size:10.5px;")
        foot.addWidget(self.lb_count)
        lay.addLayout(foot)

    def _close(self):
        if callable(self.on_close):
            self.on_close()

    # ---------------- 第 106 轮：分类同步 / 批量高亮 ----------------
    def on_categories_changed(self):
        """游戏管理里建/删/拖分类 → 这边当场跟着变，不读盘。"""
        if not self.isVisible():
            return                      # 抽屉收着，等下次展开用缓存渲染即可
        self._rerender_from_cache()

    def _rerender_from_cache(self):
        key = gc.dir_key()
        try:
            if self._mode == "added":
                data = gc.CACHE.get(gc.NS_ADDED, gc.ADDED_KEY)
                if data is not None:
                    self._render_added(data)
            else:
                data = gc.CACHE.get(gc.NS_LS, key)
                if data is not None:
                    self._render(data[0], data[1])
        except RuntimeError:
            pass

    def _sync_batch_styles(self):
        sel = getattr(self, "selection", None)
        if sel is None:
            return
        for r in list(self._rows or []):
            fn = getattr(r, "set_batch_selected", None)
            if not callable(fn):
                continue
            it = getattr(r, "item", None) or getattr(r, "entry", None)
            if it is None:
                continue
            try:
                fn(sel.has(cats.game_key(it)))
            except RuntimeError:
                pass

    def _index_rows(self):
        """批量拖拽时按 game_key 反查行（版本行 / 已导入行都登记进来）。"""
        self._rows_by_key = {}
        for r in list(self._rows or []):
            it = getattr(r, "item", None) or getattr(r, "entry", None)
            if it is None:
                continue
            kind = "pack" if getattr(r, "kind", "") == "instance" else "version"
            if getattr(r, "entry", None) is not None:
                kind = "pack"
            self._rows_by_key[cats.game_key(it)] = (r, kind)

    def select_row(self, row):
        """点卡片选中（第五十七轮）：整列只留一个高亮，「当前目标」字样同步过去。

        不重建整张列表 —— 重建要重新读一遍磁盘，点一下卡一下，而且列表会跳回顶部。
        """
        for r in list(self._rows or []):
            fn = getattr(r, "set_selected", None)
            if fn is None:                 # 「已导入」那栏的行没有选中态，跳过
                continue
            try:
                fn(r is row)
            except Exception:
                pass
            rf = getattr(r, "refresh_meta", None)
            if rf:
                try:
                    rf()
                except Exception:
                    pass

    def _sync_selected(self):
        """渲染完把选中态补回来：刷新列表 / 重开面板不该把高亮丢掉"""
        for r in list(self._rows or []):
            fn = getattr(r, "set_selected", None)
            if fn is None:
                continue
            try:
                fn(r._is_target())
            except Exception:
                pass

    # ---------------- 创建分类（第 105 轮）----------------
    def _create_category(self):
        from .dialogs import CategoryDialog
        from ..core import categories as cats
        d = CategoryDialog(self.window_ref, default_kind="pack")
        d.exec()
        if not d.name:
            return
        cid = cats.create(d.name, d.kind)
        c = cats.get(cid)
        Toast.show_toast(self.window_ref,
                         f"分类「{c['name'] if c else d.name}」已创建（空的）："
                         f"按住 Ctrl 点选几个游戏，再长按拖进去",
                         ms=4200, kind="success")
        # cats.create() 会 notify：这里和游戏管理当场一起重渲染（用缓存，不读盘）
        self._rerender_from_cache()

    # ---------------- 两个视图（游戏版本 / 已导入） ----------------
    def _toggle_added(self):
        if self._mode == "added":
            self._mode = "versions"
        else:
            self._mode = "added"
        self._sync_mode()
        self.reload()

    def _sync_mode(self):
        added = self._mode == "added"
        self.btn_added.kind = "accent" if added else "ghost"
        self.btn_added.update()
        self.btn_added.setText("游戏版本" if added else "已导入")
        self.btn_added.setToolTip(
            "回到游戏版本 / 整合包列表" if added
            else "列出游戏管理里「已导入」的那些游戏，可直接运行")
        self.lb_tip.setText(
            "点一行选中它当启动目标（启动按钮的名字会跟着换）；点「运行」直接用原目录开局，"
            "一个字节都不复制。" if added else
            "点卡片选中要启动的游戏（启动按钮的名字会跟着换），点「启动」直接用它的目录开局。")

    # ---------------- 数据 ----------------
    def reload(self, force=False):
        """第 106 轮：展开后第一次才读盘，之后一律用会话缓存。

        `force=True` 只给玩家亲手点的「刷新」和数据真的变了的写操作用。
        """
        if self._mode == "added":
            self._load_added(force=force)
        else:
            self._load_versions(force=force)

    def _placeholder(self, text):
        self._clear()
        tip = QLabel(text)
        tip.setStyleSheet("color:#8a95a3;font-size:12px;background:transparent;")
        tip.setAlignment(Qt.AlignCenter)
        self.body.addWidget(tip)

    def _load_versions(self, force=False):
        if self._busy:
            return
        key = gc.dir_key()
        if not force:
            cached = gc.CACHE.get(gc.NS_LS, key)
            if cached is not None:
                self._cached = cached
                self._render(cached[0], cached[1])
                return
        self._busy = True
        self._placeholder("正在读取版本列表…")

        def done(res):
            self._busy = False
            versions, instances = res
            gc.CACHE.put(gc.NS_LS, (list(versions), list(instances)), key)
            self._cached = (list(versions), list(instances))
            self._render(versions, instances)

        def err(e):
            self._busy = False
            self._clear()
            lb = QLabel(f"读取失败：{str(e)[:100]}")
            lb.setStyleSheet("color:#e84a42;font-size:12px;background:transparent;")
            lb.setWordWrap(True)
            self.body.addWidget(lb)

        run_async(self._read_games, on_done=done, on_err=err)

    @staticmethod
    def _read_games():
        """只读当前那条目录（官方 / 应用）里的游戏"""
        from ..core import paths
        gd = paths.target_dir()
        return (installer.list_game_versions(gd),
                installer.list_game_instances(gd))

    def _load_added(self, force=False):
        if self._busy:
            return
        key = gc.ADDED_KEY
        if not force:
            cached = gc.CACHE.get(gc.NS_ADDED, key)
            if cached is not None:
                self._render_added(cached)
                return
        self._busy = True
        self._placeholder("正在读取已导入的游戏…")

        def done(entries):
            self._busy = False
            gc.CACHE.put(gc.NS_ADDED, list(entries or []), key)
            self._render_added(entries)

        def err(e):
            self._busy = False
            self._clear()
            lb = QLabel(f"读取失败：{str(e)[:100]}")
            lb.setStyleSheet("color:#e84a42;font-size:12px;background:transparent;")
            lb.setWordWrap(True)
            self.body.addWidget(lb)

        run_async(importer.added_entries, on_done=done, on_err=err)

    def _clear(self):
        self._rows = []
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w is not None:
                # 同 importui._clear_added：DeferredDelete 派发前旧控件还在原位置画，
                # 会和新列表叠在一起，先摘下来
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _section(self, title, count):
        lb = QLabel(f"{title}（{count}）")
        lb.setStyleSheet("color:#4c5866;font-size:12px;font-weight:600;"
                         "background:transparent;padding:2px;")
        self.body.addWidget(lb)

    def _render(self, versions, instances):
        self._clear()
        total = len(versions) + len(instances)
        if hasattr(self, "lb_count"):
            self.lb_count.setText(f"共 {total} 个" if total else "")
        if not total:
            from ..core import paths
            cn = paths.TARGET_CN.get(paths.target(), "")
            lb = QLabel(f"「{cn}」目录里还没装过游戏。点「下载游戏」（主页）装一个，或用"
                        "「游戏管理 → ✓（已导入）」把别的启动器里的接管过来；"
                        "也可以点上面的官方 / 应用换一条目录看。")
            lb.setStyleSheet("color:#63707f;font-size:11.5px;background:transparent;")
            lb.setWordWrap(True)
            self.body.addWidget(lb)
            return
        self._rows_by_key = {}

        def make(kind):
            def _make(it):
                row = _PickRow(it, "instance" if kind == "pack" else "version",
                               self)
                self._rows.append(row)
                self._rows_by_key[cats.game_key(it)] = (row, kind)
                return row
            return _make

        # 第 106 轮：与游戏管理共用同一批 game_categories（默认收起）
        if instances:
            self._section("整合包 / 实例", len(instances))
            gc.render_kind(self.body, "pack", instances, self, make("pack"))
        if versions:
            self._section("游戏版本", len(versions))
            gc.render_kind(self.body, "version", versions, self, make("version"))
        self._sync_batch_styles()
        self.body.addStretch(1)
        self._sync_selected()

    def _render_added(self, entries):
        """「已导入」视图：跟游戏管理里那个窗口同一套行（_AddedRow），只是长在抽屉里。

        第 107 轮：这里**只认「已导入」自己的 added_groups 文件夹**（添加已有文件夹时
        命名建的那批，默认收起、点三角展开），不再显示主视图 / 游戏管理里那套
        `game_categories` 分类容器 —— 那是「游戏版本 / 整合包」列表的分类，摆进
        「已导入」会跟它自己的文件夹混成两套，玩家分不清哪个是哪个。
        分类同步照旧只在主视图与「游戏管理」之间进行（`cats.create` 会通知两边）。
        """
        self._clear()
        total = len(entries)
        nf = len({str(e.get("group") or "") for e in entries
                  if str(e.get("group") or "")})
        if hasattr(self, "lb_count"):
            self.lb_count.setText(
                (f"共 {total} 个已导入" + (f" · {nf} 个文件夹" if nf else ""))
                if total else "")
        if not total:
            lb = QLabel("还没有添加过。去「游戏管理 → 已导入」点「添加已有文件夹」，"
                        "指到那个包或版本所在的目录即可；添加完这里也能直接运行。")
            lb.setStyleSheet("color:#63707f;font-size:11.5px;background:transparent;")
            lb.setWordWrap(True)
            self.body.addWidget(lb)
            return
        self._section("已导入的游戏", total)
        self._rows_by_key = {}
        # 全部条目一律交给 render_added_list：有 group 的进 added_groups 文件夹，
        # 其余平铺。不要再按 game_categories 分流，也别再建 gc.CategoryFolder。
        render_added_list(self.body, entries, self._added_host,
                          self._added_host._open_ids, self._rows, compact=True)
        self._index_rows()
        self._sync_batch_styles()
        self.body.addStretch(1)
        self._sync_selected()

    # ---------------- 启动 ----------------
    def launch_async(self, name, win):
        def done(res):
            ok, msg = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
            Toast.show_toast(win, msg, ms=3600 if ok else 6200,
                             kind="success" if ok else "error")
            if ok:
                hook = getattr(win, "on_game_launched", None)
                if hook:
                    try:
                        hook(msg)
                    except Exception:
                        pass

        def err(e):
            Toast.show_toast(win, f"启动失败：{e}", ms=6200, kind="error")

        run_async(gamelaunch.launch, on_done=done, on_err=err)
