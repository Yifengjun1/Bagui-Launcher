# -*- coding: utf-8 -*-
"""主页「游戏管理」：查看 / 删除本机已装的游戏版本与整合包。

版本与整合包分开列；删除走回收站（可还原），不会永久抹掉玩家数据。
"""
import os

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QDrag, QFont
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..core import categories as cats
from ..core import crashlog, installer, runtime
from ..core.asyncio_qt import run_async
from . import gamecats as gc
from . import theme
from .badge import LoaderBadge as _LoaderBadge
from .widgets import (ElideLabel as _ElideLabel, FrostCard, IconButton,
                      PillButton, Toast)


def _alive(w) -> bool:
    """控件底下的 C++ 对象还在吗。

    异步结果回来时弹窗可能已经关了 / 被重新建了一个：这时再去 `addWidget`
    就是往一个已析构的 QWidget 上操作。PySide6 对**方法**会抛 RuntimeError
    （`asyncio_qt._drain()` 兜住），但对成员属性、信号连接不保证 —— 干脆先查。
    """
    try:
        import shiboken6
        return shiboken6.isValid(w)
    except Exception:
        return True


def _row_card(parent=None):
    w = QWidget(parent)
    w.setStyleSheet(
        "QWidget{background:rgba(255,255,255,150);border-radius:10px;}")
    return w


_CAT_MIME = gc.MIME


class _CategoryFolder(gc.CategoryFolder):
    """第 106 轮起改用共用分类容器：默认收起、批量拖放、两处视图同步。

    真正实现在 `ui/gamecats.py`；保留这个类名是为了让第 105 轮的回归与
    诊断脚本还能照常引用，游戏管理本身也仍通过 `gc.render_kind()` 用它。
    """

    def __init__(self, rec, rows, popup, default_open=False):
        super().__init__(rec, rows, popup, default_open=default_open)
        self.popup = popup          # 旧诊断脚本读这个属性



class _GameRow(QWidget):
    """一行：加载器徽章 + 名称 + 说明 + 选目标 / 打开目录 / 删除 / 设置"""

    def __init__(self, item, kind, popup):
        super().__init__()
        self.item = item
        self.kind = kind                 # "version" | "instance"
        self.popup = popup
        self.setFixedHeight(58)
        # 纯 QWidget 子类要开这个，样式表里的背景才会真正画出来
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._qss = (
            "QWidget{background:rgba(255,255,255,165);border-radius:11px;"
            + ("border:1.5px solid rgba(47,124,246,150);" if self._is_target() else "")
            + "}")
        self.setStyleSheet(self._qss)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 8, 10, 8)
        lay.setSpacing(7)          # 四个图标按钮要挤得下，间距比原来收一点

        badge = _LoaderBadge(item, kind)
        lay.addWidget(badge, 0, Qt.AlignVCenter)

        info = QVBoxLayout()
        info.setSpacing(2)
        name = _ElideLabel(str(item.get("name") or item.get("id") or "-"))
        f = QFont(theme.FONT)
        f.setPixelSize(13)
        f.setBold(True)
        name.setFont(f)
        name.setStyleSheet("background:transparent;")
        name.setToolTip(name.full_text())
        info.addWidget(name)

        meta = _ElideLabel(self._meta())
        meta.setStyleSheet("color:#5a6675;font-size:11px;background:transparent;")
        meta.setWordWrap(False)
        meta.setToolTip(self.item.get("path") or self.item.get("dir") or "")
        info.addWidget(meta)
        lay.addLayout(info, 1)

        btn_target = IconButton("play", 26, 12,
                                color="#2f7cf6" if self._is_target() else "#5a6675")
        btn_target.setToolTip("选它作为「独立启动」的游戏" if not self._is_target()
                              else "就是它（独立启动会启动这个）")
        btn_target.clicked.connect(self._set_target)
        lay.addWidget(btn_target)

        btn_dir = IconButton("folder", 26, 13)
        btn_dir.setToolTip("打开所在文件夹")
        btn_dir.clicked.connect(self._open)
        lay.addWidget(btn_dir)

        btn_del = IconButton("trash", 26, 13, color="#d0514a")
        btn_del.setToolTip("删除（可选移入回收站或彻底删除）")
        btn_del.clicked.connect(self._ask_delete)
        lay.addWidget(btn_del)

        # 第四十四轮：删除右边的齿轮 = 这个游戏自己的设置
        btn_cfg = IconButton("gear", 26, 13, color="#5a6675")
        btn_cfg.setToolTip("这个游戏的独立设置（文件夹 / 版本隔离 / Java / 内存 / 导出）")
        btn_cfg.clicked.connect(self._open_settings)
        lay.addWidget(btn_cfg)

        # 第 105 轮：长按拖到分类。420ms 才算「长按」，短按照旧是普通点击；
        # 四个图标按钮自己接收事件，父行不会把按钮点击吞掉。QTimer 归本行所有，
        # 行被 _clear() 摘掉时一并释放，不会留一个野计时器。
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(420)
        self._hold.timeout.connect(self._on_hold)
        self._press = QPoint()
        self._drag_ready = False
        self._pending_remove = False    # 第一一二轮：松开 Ctrl 后按下已选行，
                                        # 长按=整批拖走；快速松开才取消这一项
        self._batch = False
        self.setToolTip("长按这一行拖到分类；按住 Ctrl 点选可批量拖")

    # ---- 批量选中 / 长按拖到分类（第 106 轮）----
    def _apply_style(self):
        if self._drag_ready:
            self.setStyleSheet(
                "QWidget{background:rgba(47,124,246,45);border-radius:11px;}")
        elif self._batch:
            self.setStyleSheet(
                "QWidget{background:rgba(47,124,246,58);border-radius:11px;"
                "border:1.5px solid rgba(47,124,246,220);}")
        else:
            self.setStyleSheet(self._qss)

    def set_batch_selected(self, on):
        on = bool(on)
        if on == self._batch:
            return
        self._batch = on
        self._apply_style()

    def _on_hold(self):
        self._drag_ready = True
        self.setCursor(Qt.ClosedHandCursor)
        self._apply_style()

    def _reset_drag(self):
        was = self._drag_ready
        self._hold.stop()
        self._drag_ready = False
        self._pending_remove = False
        if was:                       # 普通点击不碰 QSS，避免每拍 setStyleSheet
            self.setCursor(Qt.ArrowCursor)
            self._apply_style()

    def mousePressEvent(self, e):
        self._press = (e.position().toPoint() if hasattr(e, "position")
                       else e.pos())
        self._reset_drag()
        if e.button() == Qt.LeftButton:
            w = self.childAt(self._press)
            if not isinstance(w, QPushButton):
                sel = getattr(self.popup, "selection", None)
                if sel is not None:
                    act = gc.press_action(sel, cats.game_key(self.item))
                    if act == "add":
                        # 这一次点击只改选中态，不走行本来的按钮 / 拖拽逻辑
                        self.popup._sync_batch_styles()
                        return
                    if act == "remove":
                        # 第一一二轮：先放回去，长按=整批（含这一条）一起拖；
                        # 快速松开才真正取消这项（见 mouseReleaseEvent）
                        sel.add(cats.game_key(self.item))
                        self._pending_remove = True
                        self._hold.start()
                        return
                self._hold.start()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_ready:
            pos = (e.position().toPoint() if hasattr(e, "position")
                   else e.pos())
            if (pos - self._press).manhattanLength() > 7:
                self._start_drag()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        pending = self._pending_remove
        dragged = self._drag_ready      # 拖过的话 _start_drag 里已清掉 pending
        self._reset_drag()
        if pending and not dragged:
            sel = getattr(self.popup, "selection", None)
            if sel is not None:
                sel.remove(cats.game_key(self.item))
                self.popup._sync_batch_styles()
        super().mouseReleaseEvent(e)

    def _start_drag(self):
        try:
            key = cats.game_key(self.item)
            sel = getattr(self.popup, "selection", None)
            rows, names = [], []
            # 从「已批量选中的任一条」起拖 = 整批一起搬；没选中就只拖自己
            if sel is not None and sel.count() > 1 and sel.has(key):
                index = getattr(self.popup, "_rows_by_key", {}) or {}
                for k in sel.keys_snapshot():
                    info = index.get(k)
                    if not info:
                        continue
                    r, kd = info
                    rows.append((k, kd))
                    names.append(gc.item_name(getattr(r, "item", None)))
            if not rows:
                rows = [(key, gc.row_kind(self.kind))]
                names = [gc.item_name(self.item)]
            drag = gc.make_drag(self, rows, names, len(rows))
            self._reset_drag()
            if drag is not None:
                drag.exec(Qt.CopyAction)
        except Exception as e:                              # noqa: BLE001
            crashlog.note("游戏管理：拖拽启动失败 %s" % e)
            self._reset_drag()

    # ---- 独立启动目标 ----
    def _vid(self):
        return str(self.item.get("version_id") or self.item.get("id")
                   or os.path.basename(self.item.get("path") or ""))

    def _is_target(self):
        from ..core import gamelaunch
        t = gamelaunch.launch_target()
        return bool(t) and str(t.get("version_id") or "") == self._vid()

    def _set_target(self):
        from ..core import gamelaunch
        name = self.item.get("name") or self.item.get("id") or self._vid()
        gamelaunch.set_launch_target(
            self.kind, self._vid(),
            self.item.get("path") or self.item.get("dir") or "",
            name, self.item.get("memory_mb") or 0)
        # 开关没开的话，玩家选了目标也看不到独立启动按钮 —— 这里直接点破
        if gamelaunch.standalone_enabled():
            Toast.show_toast(self.popup.parent(),
                             f"已选「{name}」为独立启动的游戏", ms=2600, kind="success")
        else:
            Toast.show_toast(
                self.popup.parent(),
                f"已记下「{name}」；到 设置 → 常规 打开「独立启动」就能一键直启",
                ms=4600, kind="info")
        self.popup.reload()
        win = self.popup.parent()
        hook = getattr(win, "refresh_launch_ui", None)
        if hook:
            try:
                hook()
            except Exception:
                pass

    def _meta(self):
        it = self.item
        bits = []
        if self.kind == "version":
            bits.append(f"MC {it.get('mc') or '未知'}")
            if it.get("loader"):
                cn = runtime.LOADER_CN.get(it["loader"], str(it["loader"]))
                bits.append(f"{cn} {it.get('loader_ver') or ''}".strip())
            else:
                bits.append("原版")
        else:
            if it.get("mc"):
                bits.append(f"MC {it['mc']}")
            if it.get("loader"):
                bits.append(runtime.LOADER_CN.get(
                    it["loader"], str(it["loader"]).capitalize()))
            if it.get("memory_mb"):
                bits.append(f"{max(1, int(it['memory_mb']) // 1024)} GB 内存")
            if it.get("imported"):
                # 导入进来的：标一下来源，玩家一眼看出这包是从哪个启动器接管的
                src = str(it.get("source") or "").strip()
                bits.append(f"导入自 {src}" if src else "已导入")
            elif not it.get("has_meta"):
                bits.append("非本软件安装")
        size = installer.fmt_size(it.get("size") or 0)
        bits.append(size + ("+" if it.get("size_capped") else ""))
        if self._is_target():
            bits.append("启动目标")
        return " · ".join([b for b in bits if b])

    def _open(self):
        path = self.item.get("path") or self.item.get("dir") or ""
        try:
            os.makedirs(path, exist_ok=True)
            from .fspicker import pick_view          # 第六十轮：内置优先
            win = self.popup.parent()
            # 第七十九轮：「游戏目录」这个查看器底部多一个「汉化补丁」，
            # 点它直接进核对流程（选 zip → 文件夹树核对 → 覆盖到这个目录）
            pick_view(win, "游戏目录", path,
                      on_langpack=lambda dest: self._langpack(dest, win))
        except Exception:
            Toast.show_toast(self.popup.parent(), "无法打开该目录")

    def _langpack(self, dest, win):
        """「游戏目录 → 汉化补丁」：直接走核对流程。

        这里**不需要**先知道它是哪个 BBSMC 项目 —— 玩家手上已经有 zip 了，
        选它、在文件夹树上核对、覆盖进这个游戏目录就行。所以入口比下载页那条
        短：没有「有没有汉化 / 对应哪个版本」的查询，一步到核对。
        """
        from .langpack import open_local_zip
        name = str(self.item.get("name") or self.item.get("id") or "游戏")
        try:
            open_local_zip(win, dest, pack_name=name, pack_ver="")
        except Exception as exc:                     # noqa: BLE001
            Toast.show_toast(win, f"汉化补丁没能开始：{str(exc)[:60]}")

    def _ask_delete(self):
        from .dialogs import ConfirmDeleteDialog
        label = self.item.get("name") or self.item.get("id")
        d = ConfirmDeleteDialog(self.popup.parent(), label, self.kind)
        if not d.exec() or not d.choice:
            return
        self.popup.delete_item(self.kind, self.item, mode=d.choice)

    def _open_settings(self):
        """打开这个游戏自己的设置窗（左分类 / 右内容）。

        弹窗要先收起：GameManagerPopup 是 `Qt.Popup`，不关掉的话设置窗
        一出现就被它「点外面」的逻辑立刻关掉（点第一个按钮就没了）。
        关掉设置窗再把列表弹回来，玩家能看到刚改过的状态。
        """
        from .gamesettings import GameSettingsDialog
        win, anchor = self.popup.parent(), self.popup.anchor
        self.popup.close()
        d = GameSettingsDialog(win, self.item, self.kind)
        d.exec()
        if anchor is not None and anchor.isVisible() and hasattr(anchor, "_toggle"):
            anchor._toggle()


class GameManagerPopup(QWidget):
    """游戏管理弹层。

    `grab=True`（默认）用 `Qt.Popup`：点窗外就收起，像下拉菜单。
    `grab=False` 用 `Qt.Tool`：**不独占鼠标**。引导教程走到「官方 / 应用 /
    勾 / 刷新」这四步时必须用它 —— `Qt.Popup` 开着的时候会把整个屏幕的
    鼠标都抓在自己手里，点窗外既关掉弹层又把这次点击吞掉，引导小窗上的
    ← / → 永远收不到点击，玩家看到的就是「到游戏管理之后翻页没反应」。
    """

    def __init__(self, parent, anchor, grab=True):
        flags = (Qt.Popup if grab else Qt.Tool) | Qt.FramelessWindowHint
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.anchor = anchor
        self.nograb = not grab          # 引导用：谁开的、要不要独占鼠标
        self._busy = False
        self._pending = False      # 统计途中又点了「官方/应用」：跑完再来一轮
        # 第 106 轮：一次会话只统计一次；批量选中集合；分类变更订阅
        self._cached = None
        self._rows_by_key = {}
        self.selection = gc.Selection(on_change=self._sync_batch_styles)
        gc.subscribe_widget(self)
        self._cat_watching = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = FrostCard(self, radius=16, tint=246)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)

        head = QHBoxLayout()
        t = QLabel("游戏管理")
        f = QFont(theme.FONT)
        f.setPixelSize(14)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t)
        head.addSpacing(8)
        # 第四十六轮：「游戏管理」字样右边加官方 / 应用 —— 只看那条目录里的游戏。
        # 默认跟着「独立启动」开关（关=官方，开=应用），跟下载面板是同一份配置。
        from .loaderpanel import TargetSwitch
        self.target_switch = TargetSwitch(self.parent(), card, scope="view")
        self.target_switch.changed.connect(lambda _k: self.reload())
        head.addWidget(self.target_switch)
        head.addStretch(1)
        # 第 105 轮：「+」就摆在「✓（已导入）」左侧。点它建一个**空**分类，
        # 建完要玩家长按下面的游戏拖进去（创建时可先选装游戏版本还是整合包）。
        self.btn_cat = IconButton("plus", 26, 13, color="#2f7cf6")
        self.btn_cat.setToolTip("创建分类：可选「游戏版本」或「整合包」；"
                                "建好后长按游戏拖进去")
        self.btn_cat.clicked.connect(self._create_category)
        head.addWidget(self.btn_cat)
        # 只剩「已导入」一个入口（第六十六轮）：它自己就能「添加已有文件夹」，
        # 再放一个「导入其他启动器」纯属重复 —— 玩家得先点进去才知道该点哪个。
        self.btn_added = IconButton("check", 26, 13, color="#5a6675")
        self.btn_added.setToolTip("已导入的游戏（登记过的目录，可直接运行，也能在这里导入新的）")
        self.btn_added.clicked.connect(self.open_added)
        head.addWidget(self.btn_added)
        # 右边：刷新列表（换成通用的环形箭头，别再让人误认成下载）
        self.btn_refresh = IconButton("refresh", 26, 13, color="#5a6675")
        self.btn_refresh.setToolTip("重新统计一次（平时打开不会再读盘）")
        # 玩家亲手点「刷新」= 明确要重读；普通打开走会话缓存
        self.btn_refresh.clicked.connect(lambda: self.reload(force=True))
        head.addWidget(self.btn_refresh)
        lay.addLayout(head)

        self.lb_tip = QLabel("点 ▶ 选一个游戏作为「独立启动」的目标；删除会移入回收站（可还原）。")
        self.lb_tip.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.lb_tip.setWordWrap(True)
        lay.addWidget(self.lb_tip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.host = QWidget()
        self.host.setStyleSheet("background:transparent;")
        self.body = QVBoxLayout(self.host)
        self.body.setContentsMargins(0, 0, 4, 0)
        self.body.setSpacing(6)
        self.scroll.setWidget(self.host)
        # 第一一二轮：列表空白处接「拖出分类」—— 从文件夹拖出来松手即移出
        gc.enable_drag_out(self.host)
        self.scroll.setFixedHeight(392)
        lay.addWidget(self.scroll)

        self.setFixedWidth(430)
        self.adjustSize()
        # 除滚动区之外的固定高度（标题 + 说明 + 边距）。
        # 只在构造时量一次：此时滚动区是固定 392，减法可靠；之后改滚动区高度
        # 会让 min/max 一起变，再反推就会把「固定高度」越算越大，弹窗顶出窗口。
        self._frame_h = max(60, self.height() - self.scroll.height())
        self.reload()

    # ---------------- 已导入 ----------------
    def open_added(self):
        """打开「已导入」列表（登记过的目录 + 已搬进官启的）。

        第六十六轮起这里也负责「导入新的」—— 窗口里的「添加已有文件夹」就是入口，
        所以游戏管理上那个单独的「导入其他启动器」按钮撤掉了。

        关掉之后把弹窗重新弹出来 —— 玩家在这个窗口里做的事（运行 / 导入 / 改设置）
        大多会让主界面状态变化，回来正好看到刷新后的列表。
        """
        from .importui import AddedGamesDialog
        win, anchor = self.parent(), self.anchor
        self.close()
        d = AddedGamesDialog(win)
        d.exec()
        if hasattr(win, "_refresh_installed"):
            win._refresh_installed()
        if anchor is not None and anchor.isVisible() and hasattr(anchor, "_toggle"):
            anchor._toggle()

    # ---------------- 创建分类（第 105 轮）----------------
    def _create_category(self):
        """「+」→ 建一个空分类。

        `GameManagerPopup` 是 `Qt.Popup`，不先关掉的话后面的对话框一出现
        就会被「点外面就收起」的逻辑吞掉（和 open_added 同样的处理）。
        关掉对话框后借 anchor._toggle() 重建弹层，列表自然是刷新的。
        """
        from .dialogs import CategoryDialog
        win, anchor = self.parent(), self.anchor
        self.close()
        d = CategoryDialog(win, default_kind="pack")
        d.exec()
        if d.name:
            cid = cats.create(d.name, d.kind)
            c = cats.get(cid)
            Toast.show_toast(
                win, f"分类「{c['name'] if c else d.name}」已创建（空的）："
                     f"长按游戏拖进去", ms=4200, kind="success")
        if anchor is not None and anchor.isVisible() and hasattr(anchor, "_toggle"):
            anchor._toggle()

    # ---------------- 分类同步 / 批量高亮 ----------------
    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_cat_watching", False):
            gc.subscribe_widget(self)
            self._cat_watching = True

    def hideEvent(self, e):
        # popup 关掉后外层按钮还留着引用，不退订会攒下一堆 WeakMethod
        if getattr(self, "_cat_watching", False):
            gc.unsubscribe_widget(self)
            self._cat_watching = False
        super().hideEvent(e)

    def on_categories_changed(self):
        """在「选择要启动的游戏」里建/删/拖分类 → 这边当场跟着变，不读盘。"""
        if not _alive(self) or not self.isVisible():
            return
        data = getattr(self, "_cached", None)
        if data is None:
            return
        try:
            self._render(data[0], data[1])
        except Exception as e:                              # noqa: BLE001
            crashlog.note("游戏管理：分类同步渲染失败 %s" % e)

    def _sync_batch_styles(self):
        if not _alive(self):
            return
        sel = getattr(self, "selection", None)
        if sel is None:
            return
        try:
            for r in self.host.findChildren(_GameRow):
                r.set_batch_selected(sel.has(cats.game_key(r.item)))
        except RuntimeError:
            pass

    # ---------------- 列表 ----------------
    def reload(self, force=False):
        if self._busy:
            # 上一轮还没回来。以前这里直接 return —— 玩家切了目录却看到上一个目录的
            # 列表（要再点一次才对）。改成记一笔，等这轮回来立刻按当前目录重跑。
            self._pending = True
            crashlog.note("游戏管理：统计中又切了目录，排队重跑")
            return
        # 第 106 轮：一次会话里第一次点开才统计，之后直接拿缓存渲染。
        # 缓存按当前游戏目录分键，切官方 / 应用不会串目录。
        key = gc.dir_key()
        if not force:
            cached = gc.CACHE.get(gc.NS_GM, key)
            if cached is not None:
                self._cached = cached
                self._render(cached[0], cached[1])
                return
        self._busy = True
        self._pending = False
        self._clear()
        tip = QLabel("正在统计体积…")
        tip.setStyleSheet("color:#8a95a3;font-size:12px;")
        tip.setAlignment(Qt.AlignCenter)
        self.body.addWidget(tip)
        crashlog.note("游戏管理：开始统计体积（本会话首次）")

        def done(res):
            self._busy = False
            if not _alive(self):
                crashlog.note("游戏管理：统计回来了但弹窗已销毁，丢弃结果")
                return
            if self._pending:
                # 期间又切过目录：这份结果已经过时，直接作废重跑
                self._busy = False
                self._pending = False
                self.reload()
                return
            versions, instances = res
            gc.CACHE.put(gc.NS_GM, (list(versions), list(instances)), key)
            self._cached = (list(versions), list(instances))
            crashlog.note("游戏管理：统计完成 版本%d 实例%d，开始渲染"
                          % (len(versions), len(instances)))
            try:
                self._render(versions, instances)
            except Exception as e:                      # noqa: BLE001
                # 渲染炸了只影响这张列表，不许把整个软件带走
                crashlog.note("游戏管理：渲染失败 %s" % e)
                if not _alive(self):
                    return
                self._clear()
                t = QLabel(f"列表显示失败：{str(e)[:100]}")
                t.setStyleSheet("color:#e84a42;font-size:12px;background:transparent;")
                t.setWordWrap(True)
                self.body.addWidget(t)

        def err(e):
            self._busy = False
            if not _alive(self):
                return
            if self._pending:
                self._pending = False
                self.reload()
                return
            crashlog.note("游戏管理：统计出错 %s" % e)
            self._clear()
            t = QLabel(f"读取失败：{str(e)[:100]}")
            t.setStyleSheet("color:#e84a42;font-size:12px;background:transparent;")
            t.setWordWrap(True)
            self.body.addWidget(t)

        def work():
            # 只列「当前这条目录」里的游戏（官方 / 应用，见界面顶上那两个胶囊）
            from ..core import paths
            gd = paths.target_dir()
            try:
                return (installer.list_game_versions(gd),
                        installer.list_game_instances(gd))
            except Exception:
                # 一个列表挂了另一个还得显示：分开跑，失败的那边当空
                vs = []
                try:
                    vs = installer.list_game_versions(gd)
                except Exception:
                    pass
                ins = []
                try:
                    ins = installer.list_game_instances(gd)
                except Exception:
                    pass
                return vs, ins

        run_async(work, on_done=done, on_err=err)

    def _clear(self):
        while self.body.count():
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                # 先摘再删：只 deleteLater 的话，DeferredDelete 派发前旧行还按老位置
                # 画着，切「官方/应用」的那一帧会看到两套列表叠在一起。
                # 摘下来也能让这些控件立刻不归布局管，GC 在主线程随手就收了。
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _section(self, title, count):
        lb = QLabel(f"{title}（{count}）")
        lb.setStyleSheet("color:#4c5866;font-size:12px;font-weight:600;"
                         "background:transparent;padding:2px 2px;")
        self.body.addWidget(lb)

    def _render(self, versions, instances):
        self._clear()
        self._rows_by_key = {}
        from ..core import paths
        cn = paths.TARGET_CN.get(paths.target(), "")
        self._section("游戏版本", len(versions))
        if not versions:
            self._empty(f"「{cn}」目录里还没有安装任何游戏版本")
        self._render_kind("version", versions)
        self._section("整合包", len(instances))
        if not instances:
            self._empty(f"「{cn}」目录里还没有安装整合包")
        self._render_kind("pack", instances)
        self.body.addStretch(1)
        self._sync_batch_styles()
        self._fit()

    def _render_kind(self, kind, items):
        """第 106 轮：与「选择要启动的游戏」共用 `gamecats.render_kind()`。

        分类默认收起；分过类的进文件夹，没分类的平铺；一条游戏只出现一次。
        """

        def make(it):
            r = _GameRow(it, "version" if kind == "version" else "instance", self)
            self._rows_by_key[cats.game_key(it)] = (r, kind)
            return r

        gc.render_kind(self.body, kind, items, self, make)

    def _empty(self, text):
        lb = QLabel(text)
        lb.setStyleSheet("color:#8a95a3;font-size:11px;background:transparent;"
                         "padding:2px 4px;")
        self.body.addWidget(lb)

    # ---------------- 删除 ----------------
    def delete_item(self, kind, item, mode="recycle"):
        """第 122 轮：mode = recycle（移入回收站，可还原）/ permanent（彻底删除）。"""
        permanent = (mode == "permanent")
        tip_normal = ("点 ▶ 选一个游戏作为「独立启动」的目标；"
                      + ("删除为彻底删除（不可还原）。" if permanent
                         else "删除会移入回收站（可还原）。"))
        if kind == "version":
            fn, args = installer.delete_game_version, (item.get("id"), None, permanent)
        else:
            fn, args = installer.delete_game_instance, (
                item.get("path"), item.get("profile_id") or "", None, permanent)
        self.lb_tip.setText("正在删除…")

        def done(res):
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            if not _alive(self):
                return
            self.lb_tip.setText(tip_normal)
            Toast.show_toast(self.parent(), msg,
                             ms=5200, kind="success" if ok else "error")
            gc.invalidate_games()       # 删完列表确实变了，缓存必须作废
            self.reload(force=True)
            win = self.parent()
            if hasattr(win, "_refresh_installed"):
                win._refresh_installed()

        def err(e):
            if not _alive(self):
                return
            self.lb_tip.setText(tip_normal)
            Toast.show_toast(self.parent(), f"删除失败：{str(e)[:100]}",
                             ms=5200, kind="error")

        run_async(fn, *args, on_done=done, on_err=err)

    # ---------------- 定位 / 自适应高度 ----------------
    def _bounds(self):
        """可用的上下边界（取「软件窗口」与「屏幕可用区」的交集）。"""
        win = self.anchor.window()
        scr = (win.screen() if win is not None else None) or QApplication.primaryScreen()
        sa = scr.availableGeometry()
        if win is not None and win.isVisible():
            wr = QRect(win.mapToGlobal(QPoint(0, 0)), win.size())
            top = max(wr.top(), sa.top()) + 8
            bottom = min(wr.bottom(), sa.bottom()) - 8
        else:
            top, bottom = sa.top() + 8, sa.bottom() - 8
        return top, bottom

    def _max_scroll(self):
        """滚动区最大高度 = 可用高度 - 其它部件高度，保证整个弹窗不越界。"""
        top, bottom = self._bounds()
        return max(72, (bottom - top) - self._frame_h)

    def _apply_height(self, scroll_h):
        """直接钉死「滚动区 + 整窗」高度（比 adjustSize 可靠）。"""
        self.scroll.setFixedHeight(scroll_h)
        self.setFixedHeight(self._frame_h + scroll_h)

    def _natural_height(self):
        """内容自然高度。

        不能读 host.sizeHint()：QScrollArea 把宿主设为 widgetResizable 后，
        宿主的 sizeHint 会一直是 0，导致「内容很多却只开一条缝」。
        这里直接累加各行的高度提示，结果确定且立即生效。
        """
        lay = self.body
        m = lay.contentsMargins()
        total = m.top() + m.bottom()
        shown = 0
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is None:
                continue
            total += max(w.sizeHint().height(), w.minimumHeight())
            shown += 1
        if shown > 1:
            total += lay.spacing() * (shown - 1)
        return total

    def _fit(self):
        """按内容自适应高度：内容少就收紧，内容多就滚动，且不超过可用高度。"""
        natural = self._natural_height()
        h = min(392, self._max_scroll(), max(96, natural))
        if h != self.scroll.height() or self.height() != self._frame_h + h:
            self._apply_height(h)
        if self.isVisible():
            self._reposition()

    def _reposition(self):
        top, bottom = self._bounds()
        win = self.anchor.window()
        scr = (win.screen() if win is not None else None) or QApplication.primaryScreen()
        sa = scr.availableGeometry()
        g = self.anchor.mapToGlobal(QPoint(0, self.anchor.height() + 6))
        w, h = self.width(), self.height()
        # 横向：优先贴按钮左侧，越出窗口右边界就左移
        right = min(sa.right(), QRect(win.mapToGlobal(QPoint(0, 0)),
                                      win.size()).right()) if win is not None else sa.right()
        x = min(g.x(), right - w - 8)
        x = max(x, sa.left() + 8)
        # 纵向：放不下就往上翻（贴着按钮上沿展开），再不行就顶到可用区顶部
        y = g.y()
        if y + h > bottom:
            up = g.y() - self.anchor.height() - 6 - h
            y = up if up >= top else bottom - h
        y = max(y, top)
        self.move(x, y)

    def _close_siblings(self):
        """同一时刻只许有一块「游戏管理」面板。

        为什么会撞车：`_toggle` / `open_for_tour` 各建一块，`close()` 只是隐藏，
        旧那块还在跑体积统计（干净机器上要 8 秒）—— 结果日志里能看见两块同时
        「统计完成…开始渲染」。两块 430×500 的白卡片叠在屏幕左边，玩家看到的就是
        「点整合包时左边临时冒出一片弹窗」。
        """
        from PySide6.QtWidgets import QApplication
        for w in QApplication.allWidgets():
            if isinstance(w, GameManagerPopup) and w is not self:
                try:
                    if w.isVisible():
                        w.close()
                except RuntimeError:                 # C++ 侧已销毁
                    pass

    def popup(self):
        self._close_siblings()
        self.setFixedWidth(430)
        self._apply_height(min(392, self._max_scroll()))
        self._reposition()
        self.show()
        self.raise_()
        self._reposition()      # show 之后窗口几何才最终确定，再校正一次


class GameManagerButton(PillButton):
    """主页「Java 下载」右侧的游戏管理按钮"""

    def __init__(self, window, parent=None):
        super().__init__("游戏管理", "ghost", icon="grid", parent=parent)
        self.window_ref = window
        self._popup = None
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("查看并删除已安装的游戏，或导入其它启动器里的游戏")
        self.clicked.connect(self._toggle)

    def _toggle(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.close()
            return
        self._popup = GameManagerPopup(self.window_ref, self)
        self._popup.popup()

    def open_for_tour(self):
        """引导教程用：开一个**不抢鼠标**的弹层（原因见 GameManagerPopup）。

        已经开着一个（哪怕是玩家自己点的）就换成这个版本 —— 带鼠标独占的
        那个会让引导小窗的箭头点不动。
        """
        cur = self._popup
        if cur is not None and cur.isVisible() and getattr(cur, "nograb", False):
            return cur
        if cur is not None and cur.isVisible():
            cur.close()
        self._popup = GameManagerPopup(self.window_ref, self, grab=False)
        self._popup.popup()
        return self._popup
