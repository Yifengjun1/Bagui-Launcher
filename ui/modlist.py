# -*- coding: utf-8 -*-
"""目录文件清单（第六十四轮）：图标 + 名字 + 大小 + 行内「禁用/启用」「删除」。

用在「游戏设置 → 概览」的模组文件夹下面：点文件夹左边的小三角就展开这份清单，
不用跳出去也能看到装了什么、随手禁掉一个再启动试试（第六十四轮补丁：跟内置文件
管理器开不开无关，一直都在）。

为什么不直接铺 QListWidget：每行右侧要挂两个按钮，`setItemWidget` 会跟「选中 / 双击 /
悬停」三套事件打架（点按钮被列表当成选行）。而且每一行的样子本来就要变（禁用态压暗 +
挂「已禁用」标签），干脆一行一个 widget。

代价是一行一个 widget 建起来不便宜（实测 213 行 1 秒），所以 `reload()` 分块建：
先建 24 行把可见区填满，剩下的等玩家滚到底再补，界面全程不卡。

第六十七轮：概览里这条链路会带 `scope`（"import" / "app"），目录清单与图标字节
临时缓存到 `<安装目录>\\cache\\` 下的两摊里，滑回来 / 禁用一个再刷新都不重新开 jar。
"""
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QVBoxLayout, QWidget)

from ..core import gearcache, modfiles
from . import fileicons, theme
from .dialogs import _DialogBase
from .widgets import ElideLabel, IconButton, Toast

_META = "color:#8a95a3;font-size:10.5px;"


def _norm(p):
    """路径规范化（大小写 + 绝对化），用于跨平台比对本地文件与记录路径"""
    try:
        return os.path.normcase(os.path.abspath(p or ""))
    except (OSError, ValueError):
        return str(p or "").lower()


class TriToggle(QWidget):
    """小三角（展开 / 收起）。自绘 12×12。

    为什么不用 QToolButton + 系统箭头：系统箭头在浅色磨砂底上发灰，而且带 20px 的
    方框留白，会把整行文字往右挤。这里收起时右向、展开时下向，跟资源管理器一致。
    """
    clicked = Signal()

    def __init__(self, size=12, tip="展开看看里面有什么", tip_on="收起"):
        super().__init__()
        self._on = False
        self._tip = tip
        self._tip_on = tip_on
        self.setFixedSize(size + 6, size + 6)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, v):
        v = bool(v)
        if v != self._on:
            self._on = v
            self.setToolTip(self._tip_on if v else self._tip)
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = float(self.width())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4a5867" if self._on else "#8b95a3"))
        tri = QPainterPath()
        if self._on:                       # 展开：向下
            tri.moveTo(s * 0.28, s * 0.40)
            tri.lineTo(s * 0.72, s * 0.40)
            tri.lineTo(s * 0.50, s * 0.68)
        else:                              # 收起：向右
            tri.moveTo(s * 0.40, s * 0.28)
            tri.lineTo(s * 0.68, s * 0.50)
            tri.lineTo(s * 0.40, s * 0.72)
        tri.closeSubpath()
        p.drawPath(tri)
        p.end()


class _ConfirmFile(_DialogBase):
    """删一个文件前的确认 —— 说明会进回收站，不是永久删"""

    def __init__(self, parent, name: str, size_text: str = ""):
        super().__init__(parent, width=430, title="删除文件")
        tip = QLabel(f"确定要删除「{name}」吗？")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#1f2733;font-size:13px;")
        self.body.addWidget(tip)
        note = ("会移入 Windows 回收站，删错了还能捡回来。\n"
                "如果这个模组是整合包自带的定制版，删掉之后不一定能在网上下回来 ——"
                "只是想先关掉的话，用「禁用」更安全。")
        lb = QLabel(note + (f"\n\n文件大小：{size_text}" if size_text else ""))
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#8a6a2a;font-size:11px;background:rgba(232,164,66,26);"
                         "border-radius:10px;padding:10px;")
        self.body.addWidget(lb)
        self.add_buttons([("取消", "ghost", self.reject),
                          ("移到回收站", "accent", self.accept)])


class FileRow(QWidget):
    """一行文件：图标 / 名字 / 大小 / 更新·禁用·启用·删除（全图标）

    第八十七轮：禁用 / 删除从文字按钮改成图标按钮，新增「更新」图标
    （只在该模组有可更新版本时出现）。行内操作不再用文字，省地方也更整齐。
    """

    def __init__(self, row: dict, panel):
        super().__init__()
        self.row = row
        self.panel = panel
        self._upd = None
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(32)
        self.setStyleSheet(
            "QWidget{background:transparent;border-radius:8px;}"
            "QWidget:hover{background:rgba(255,255,255,190);}")

        h = QHBoxLayout(self)
        h.setContentsMargins(6, 2, 6, 2)
        h.setSpacing(6)

        self.ic = QLabel()
        self.ic.setFixedSize(18, 18)
        # 传**完整路径**：jar / zip 要打开包读里面的 icon.png / pack.png。
        # 用 async 版：没读过的那几个先显示字母方块，后台读好了面板会回来换。
        # 第六十七轮补丁：mtime 清单里带来了，别再 stat 一次。
        self.ic.setPixmap(fileicons.pixmap_async(row["path"], 18,
                                                mtime=row.get("mtime")))
        self.ic.setStyleSheet("background:transparent;")
        self.ic.setToolTip(row["name"])
        h.addWidget(self.ic)

        nm = ElideLabel(row["name"])
        nm.setStyleSheet("background:transparent;font-size:12px;color:%s;"
                         % ("#8b95a3" if row["disabled"] else "#1f2733"))
        nm.setToolTip(row["path"])
        h.addWidget(nm, 1)

        if row["disabled"]:
            tag = QLabel("已禁用")
            tag.setStyleSheet("background:rgba(31,39,51,26);color:#6b7684;"
                              "font-size:9.5px;border-radius:6px;padding:1px 5px;")
            h.addWidget(tag)

        sz = QLabel(modfiles.human_size(row["size"]))
        sz.setFixedWidth(56)
        sz.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sz.setStyleSheet("background:transparent;" + _META)
        h.addWidget(sz)

        # 更新图标（仅可更新时显示）：下载箭头 = 就地升到最新版
        self.b_upd = IconButton("download", size=26, icon_size=14,
                                color="#2f7cf6", hover_bg=QColor(47, 124, 246, 40))
        self.b_upd.setVisible(False)
        self.b_upd.clicked.connect(lambda: panel.update_one(row))
        h.addWidget(self.b_upd)

        # 禁用 / 启用（图标）：只有模组（.jar / .jar.disabled）才给。
        # 启用态显示「禁用」符号（点了关掉），禁用态显示「启用」符号（点了打开）。
        if row["mod"]:
            icon = "enable" if row["disabled"] else "disable"
            tip = ("改回 .jar → 下次启动重新加载"
                   if row["disabled"]
                   else "改名成 .disabled → 游戏下次启动就不加载它了（文件还在）")
            self.b_toggle = IconButton(icon, size=26, icon_size=14)
            self.b_toggle.setToolTip(tip)
            self.b_toggle.clicked.connect(lambda: panel.toggle(row))
            h.addWidget(self.b_toggle)

        # 删除（图标）：移入回收站，删错了还能捡回来
        self.b_del = IconButton("trash", size=26, icon_size=14, color="#c0392b")
        self.b_del.setToolTip("移入回收站（可还原）")
        self.b_del.clicked.connect(lambda: panel.remove(row))
        h.addWidget(self.b_del)

    def set_updatable(self, info):
        """info=None 表示没有可更新版本 → 隐藏更新图标"""
        self._upd = info
        self.b_upd.setVisible(bool(info))
        if info:
            self.b_upd.setToolTip("更新到 %s" % (info.get("version") or "最新版本"))


class FileListPanel(QWidget):
    """一个目录的文件清单（带行内操作）。改完自己 reload，也能通知外面。"""

    LIST_MAX_H = 216          # 超过这个高度就在面板内部滚，别把整页撑长
    CHUNK = 24                # 一次建多少行（一行一个 widget，200 行一次建要 1 秒）
    FIRST_CHUNK = 8           # 第一批只建够铺满可视区的（约 7 行）—— 点小三角当场就出来

    def __init__(self, folder: str, parent=None, on_changed=None,
                 limit: int = 300, scope: str = "", load: bool = True,
                 mc_version: str = "", loader: str = ""):
        super().__init__(parent)
        self.folder = str(folder or "")
        self._on_changed = on_changed
        self._limit = int(limit)
        # 第六十七轮："" = 不落盘（内置文件管理器 / 下载页）；"import"/"app" =
        # 概览里这条链路，目录清单与图标字节都临时缓存到 <安装目录>\cache\<那一摊>
        self._scope = str(scope or "")
        # 第八十七轮：更新检测要按当前实例的 MC 版本 / 加载器筛兼容版本
        self._mc = str(mc_version or "")
        self._loader = str(loader or "")
        self._rows = []
        self._empty = None
        self._pending = []
        self._updatable = {}        # normpath -> {pid,version,url,filename}
        self._filter = "all"        # all | disabled | update
        self._check_started = False
        self.setObjectName("flp")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("#flp{background:rgba(47,124,246,14);border-radius:10px;}")

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.lb_info = QLabel("")
        self.lb_info.setStyleSheet("background:transparent;color:#5a6675;font-size:11px;")
        head.addWidget(self.lb_info, 1)

        # 第九十三轮：搜索（图上就是一个放大镜），点开在下方拉出搜索框。
        # 摆在「禁用总览」左边 —— 一排筛选控件从左到右是「找 → 只看禁用 → 只看可更新 → 重读」
        self.b_search = IconButton("search", size=28, icon_size=15)
        self.b_search.setToolTip("搜索：按文件名筛选（再点一下收起）")
        self.b_search.clicked.connect(self._toggle_search)
        head.addWidget(self.b_search)

        # 第八十七轮：刷新左侧新增「禁用总览 / 更新总览」两个开关（筛选视图）
        # 第八十八轮：三个都只留图标，文字改成鼠标悬停提示（激活态 = 淡蓝底 + 蓝图标）
        self.b_disabled = IconButton("disable", size=28, icon_size=15)
        self.b_disabled.setToolTip("禁用总览：只显示已禁用的模组")
        self.b_disabled.clicked.connect(lambda: self._toggle_filter("disabled"))
        head.addWidget(self.b_disabled)

        self.b_update = IconButton("download", size=28, icon_size=15)
        self.b_update.setToolTip("更新总览：只显示可更新的模组")
        self.b_update.setEnabled(False)        # 没查到可更新前先禁用
        self.b_update.clicked.connect(lambda: self._toggle_filter("update"))
        head.addWidget(self.b_update)

        self.b_refresh = IconButton("refresh", size=28, icon_size=15)
        self.b_refresh.setToolTip("刷新")
        self.b_refresh.clicked.connect(self.reload)
        head.addWidget(self.b_refresh)
        v.addLayout(head)

        # 搜索条：默认**收起**（点上面那个放大镜才出现），输入即筛，不用点按钮
        self._query = ""                      # 已生效的关键词（小写）
        self._search_row = QWidget(self)
        self._search_row.setVisible(False)
        sr = QHBoxLayout(self._search_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(6)
        self.ed_search = QLineEdit(self._search_row)
        self.ed_search.setPlaceholderText("搜文件名，例如 jei、光影、1.20.1…")
        self.ed_search.setFixedHeight(30)
        self.ed_search.setClearButtonEnabled(True)
        # 跟进面板的浅色磨砂底：默认 QLineEdit 的白框在这里太硬
        self.ed_search.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,225);color:#1f2733;"
            "border:1px solid rgba(31,39,51,36);border-radius:8px;"
            "padding:0 8px;font-size:11.5px;}"
            "QLineEdit:focus{border:1px solid rgba(47,124,246,180);}")
        self.ed_search.textChanged.connect(self._on_query_changed)
        self.ed_search.returnPressed.connect(self._apply_query_now)
        sr.addWidget(self.ed_search, 1)
        # 搜索框右边的「搜」，也用放大镜图标（跟左边那个开关长一样，位置不同）
        self.b_search_go = IconButton("search", size=30, icon_size=15,
                                      color="#2f7cf6")
        self.b_search_go.setToolTip("搜索（也可以直接按回车）")
        self.b_search_go.clicked.connect(self._apply_query_now)
        sr.addWidget(self.b_search_go)
        v.addWidget(self._search_row)
        # 每敲一个字就重建一次列表太浪费：停手 180ms 再筛
        self._q_timer = QTimer(self)
        self._q_timer.setSingleShot(True)
        self._q_timer.setInterval(180)
        self._q_timer.timeout.connect(self._apply_query)

        self._sa = QScrollArea()
        self._sa.setWidgetResizable(True)
        self._sa.setFrameShape(QFrame.NoFrame)
        self._sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._sa.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;margin:2px 0;}"
            "QScrollBar::handle:vertical{background:rgba(31,39,51,64);"
            "border-radius:3px;min-height:24px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._inner = QWidget()
        self._inner.setStyleSheet("background:transparent;")
        self._il = QVBoxLayout(self._inner)
        self._il.setContentsMargins(0, 0, 4, 0)
        self._il.setSpacing(1)
        self._sa.setWidget(self._inner)
        v.addWidget(self._sa)

        self._row_widgets = []
        self._sa.verticalScrollBar().valueChanged.connect(self._on_scroll)
        fileicons.bus().icons_ready.connect(self._refresh_icons)   # 包里图标读好了
        if load:
            self.reload()
        else:
            # 懒装：先把面板插进布局（点小三角立刻有反馈），数据等事件循环再读
            self.lb_info.setText("正在读取…")
            self._sa.setFixedHeight(60)

    def _build_chunk(self, first: bool = False):
        """建下一批行。

        为什么不是「剩下的全建完」：一行一个 widget，往布局里加一个就要重算一次
        整个内层布局 —— 实测 213 行分 9 批建要 5.5 秒，一次性建只要 1 秒。
        所以只在**滚到底**的时候再要下一批，玩家不滚就永远不建（面板只显示 7 行）。
        第一批只建 `FIRST_CHUNK` 行：24 行要 200ms，展开时那一下卡顿就是它。
        """
        if not self._pending:
            return
        take = min(self.FIRST_CHUNK if first else self.CHUNK, len(self._pending))
        batch, self._pending = self._pending[:take], self._pending[take:]
        for r in batch:
            w = FileRow(r, self)
            # 第八十七轮：建行时就把已查到的「可更新」状态贴上去
            w.set_updatable(self._updatable.get(_norm(r["path"])))
            self._row_widgets.append(w)
            self._il.insertWidget(self._il.count() - 1, w)   # 永远插在 stretch 前面

    def _prewarm_all(self):
        """把整份清单丢给后台读图标（只排队、不建行）。

        玩家滑到后面时图标已经在内存/缓存里了，不会「滑到哪卡到哪」。
        延后一个事件循环再跑：这一步每个文件要 2~3 次 stat，213 个约 100ms，
        放在展开的那一帧里就是「卡那么一下」。

        第六十七轮补丁：清单里已经带了 mtime（`scandir` 白拿的），走
        `prewarm_pairs` 就一次 stat 都不做 —— 这一拍从 320ms 降到接近 0。
        """
        try:
            rows = list(self._rows)
        except RuntimeError:                     # 面板已经被销毁
            return
        if rows:
            fileicons.prewarm_pairs([(r["path"], r.get("mtime") or 0.0)
                                     for r in rows], scope=self._scope)

    def _on_scroll(self, _v=0):
        """滚到底就再要一批 —— 玩家不滚就一直是前 24 行，界面不卡"""
        bar = self._sa.verticalScrollBar()
        if bar.value() + bar.pageStep() >= bar.maximum() - 2:
            self._build_chunk()


    def _refresh_icons(self, just=None):
        """后台把 jar / zip 里的图标读出来了 —— 只换图标，不重建行

        第六十七轮补丁：信号带回来「这一轮刚读到的是哪几个」，只贴那几个。
        以前是 24 行全刷一遍（每行一次 stat + 可能一次解图），而后台每 60ms
        发一次 —— 读包的几秒里，主线程一直在干这件没变化的事。
        """
        if not self._row_widgets:
            return
        want = None
        if just:
            want = set()
            for path, _mt in just:
                try:
                    want.add(os.path.normcase(os.path.abspath(path)))
                except (OSError, ValueError):
                    continue
        for w in self._row_widgets:
            try:
                if want is not None:
                    try:
                        if os.path.normcase(os.path.abspath(
                                w.row["path"])) not in want:
                            continue
                    except (OSError, ValueError):
                        continue
                w.ic.setPixmap(fileicons.pixmap_async(w.row["path"], 18,
                                                      mtime=w.row.get("mtime")))
            except RuntimeError:          # 行已经被销毁（reload 过）
                continue

    # ---------------- 数据 ----------------
    def _clear(self):
        while self._il.count():
            it = self._il.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()            # 第八十二轮：先 hide，别让排队 show 把它变成窗口
                w.setParent(None)
                w.deleteLater()

    def reload(self):
        # 第六十七轮：有归属就带上缓存（命中时只 stat 一次目录，不再逐个 scandir）
        if self._scope:
            rows, cut = gearcache.list_files(self.folder, self._scope,
                                             limit=self._limit)
        else:
            rows, cut = modfiles.list_files(self.folder, limit=self._limit)
        self._rows = rows
        self._cut = cut
        self._clear()
        self._row_widgets = []
        self._empty = None
        self._setup_pending()              # 按当前筛选过滤
        if not self._pending:
            # 目录空 / 当前筛选下没有文件：给一句提示
            if not rows:
                self._empty = QLabel(
                    "这个文件夹是空的 —— 在下载页装模组时选「这个游戏」，"
                    "文件就会出现在这里" if os.path.isdir(self.folder) else
                    "文件夹还没建出来 —— 装第一个模组时会自动创建")
            else:
                self._empty = QLabel(self._empty_text())
            self._empty.setWordWrap(True)
            self._empty.setStyleSheet("background:transparent;color:#8a95a3;"
                                      "font-size:11px;padding:8px 4px;")
            self._il.addWidget(self._empty)
            self._sa.setFixedHeight(52)
        else:
            self._il.addStretch(1)       # 分块时插在它前面，别插到末尾去
            self._sa.setFixedHeight(
                min(max(32, len(self._pending) * 32 + 4), self.LIST_MAX_H))
            self._build_chunk(first=True)   # 先铺满可视区，剩下的滚到再建
            # 整份清单排队读图标那一步也要 ~100ms（每个文件 2~3 次 stat），
            # 放到事件循环之后再干，点小三角/打开文件夹的那一下才不会卡。
            QTimer.singleShot(0, self._prewarm_all)

        n_on = sum(1 for r in rows if not r["disabled"])
        n_off = len(rows) - n_on
        total = len(rows) + cut
        if not rows:
            self.lb_info.setText("0 个文件")
        else:
            bits = [f"共 {total} 个文件", f"启用 {n_on}"]
            if n_off:
                bits.append(f"已禁用 {n_off}")
            if cut:
                bits.append(f"另有 {cut} 个未列出")
            if self._updatable:
                bits.append(f"可更新 {len(self._updatable)}")
            if getattr(self, "_query", ""):
                bits.append(f"匹配 {getattr(self, '_n_match', 0)}")
            self.lb_info.setText(" · ".join(bits))

        # 第八十七轮：展开后跑一次后台更新检测（只跑一次，刷新不再重复联网）
        self._run_update_check()

    def _refresh_info(self):
        """重算信息栏（异步更新检测回来后也要刷新「可更新 N」）"""
        rows = self._rows
        if not rows:
            return
        cut = getattr(self, "_cut", 0)
        n_on = sum(1 for r in rows if not r["disabled"])
        n_off = len(rows) - n_on
        total = len(rows) + cut
        bits = [f"共 {total} 个文件", f"启用 {n_on}"]
        if n_off:
            bits.append(f"已禁用 {n_off}")
        if cut:
            bits.append(f"另有 {cut} 个未列出")
        if self._updatable:
            bits.append(f"可更新 {len(self._updatable)}")
        if getattr(self, "_query", ""):
            bits.append(f"匹配 {getattr(self, '_n_match', 0)}")
        self.lb_info.setText(" · ".join(bits))

    # ---------------- 搜索（第九十三轮）----------------
    def _toggle_search(self):
        on = not self._search_row.isVisible()
        self._search_row.setVisible(on)
        self.b_search.set_active(on)
        if on:
            self.ed_search.setFocus()
        elif self.ed_search.text():
            # 收起 = 不再按关键词筛（否则列表看着「少了一半文件」还以为坏了）
            self.ed_search.clear()
            self._query = ""
            self._rebuild_for_filter()
        self._notify_height()

    def _on_query_changed(self, text):
        self._q_timer.start()
        # 清空要立刻生效（不然最后一个字还卡着筛）
        if not str(text or "").strip():
            self._apply_query_now()

    def _apply_query_now(self):
        try:
            self._q_timer.stop()
        except RuntimeError:
            pass
        self._apply_query()

    def _apply_query(self):
        q = ""
        try:
            q = str(self.ed_search.text() or "").strip().lower()
        except RuntimeError:
            q = getattr(self, "_query", "")
        if q == self._query:
            return
        self._query = q
        self._rebuild_for_filter()
        self._notify_height()

    def _notify_height(self):
        """高度变了（搜索条出现 / 列表变短），让外面重算这一页要多高"""
        if self._on_changed:
            try:
                self._on_changed()
            except Exception:                             # noqa: BLE001
                pass

    def _empty_text(self):
        q = getattr(self, "_query", "")
        if q:
            if self._filter == "disabled":
                return f"（已禁用的文件里没有包含「{q}」的）"
            if self._filter == "update":
                return f"（可更新的文件里没有包含「{q}」的）"
            return f"（没有文件名包含「{q}」的文件）"
        if self._filter == "disabled":
            return "（没有已禁用的模组）"
        if self._filter == "update":
            return "（暂无可更新的模组）"
        return "（当前筛选下没有文件）"

    # ---------------- 筛选（第八十七轮）----------------
    def _match(self, r):
        if self._filter == "disabled" and not r["disabled"]:
            return False
        if self._filter == "update" and _norm(r["path"]) not in self._updatable:
            return False
        q = getattr(self, "_query", "")
        if q and q not in str(r.get("name") or "").lower():
            return False
        return True

    def _setup_pending(self):
        self._pending = [r for r in self._rows if self._match(r)]
        self._n_match = len(self._pending)

    def _rebuild_for_filter(self):
        """切筛选 / 改关键词时重排：清空已有行，按新条件重建（保持分块 + 图标预读）"""
        self._clear()
        self._row_widgets = []
        self._empty = None
        self._setup_pending()
        self._refresh_info()          # 让「匹配 N」跟着变
        if not self._pending:
            self._empty = QLabel(self._empty_text())
            self._empty.setWordWrap(True)
            self._empty.setStyleSheet("background:transparent;color:#8a95a3;"
                                      "font-size:11px;padding:8px 4px;")
            self._il.addWidget(self._empty)
            self._sa.setFixedHeight(52)
            return
        self._il.addStretch(1)
        self._sa.setFixedHeight(
            min(max(32, len(self._pending) * 32 + 4), self.LIST_MAX_H))
        self._build_chunk(first=True)
        QTimer.singleShot(0, self._prewarm_all)

    def _set_active(self, btn, active):
        btn.set_active(active)

    def _toggle_filter(self, kind):
        new = kind if self._filter != kind else "all"
        self._filter = new
        self._set_active(self.b_disabled, new == "disabled")
        self._set_active(self.b_update, new == "update")
        self._rebuild_for_filter()

    # ---------------- 操作 ----------------
    def toggle(self, row):
        ok, _new, msg = modfiles.set_disabled(row["path"], not row["disabled"])
        Toast.show_toast(self.window(), msg, ms=3400,
                         kind="success" if ok else "error")
        if ok:
            self.reload()
            if self._on_changed:
                self._on_changed()

    def remove(self, row):
        d = _ConfirmFile(self.window(), row["name"], modfiles.human_size(row["size"]))
        if not d.exec():
            return
        ok, msg = modfiles.delete_to_trash(row["path"])
        Toast.show_toast(self.window(), msg, ms=3400,
                         kind="success" if ok else "error")
        if ok:
            self.reload()
            if self._on_changed:
                self._on_changed()

    # ---------------- 更新检测 / 就地更新（第八十九轮：按文件哈希认任意模组）----------------
    def _run_update_check(self):
        """后台算哈希 + 批量查最新版。只跑一次，刷新不再重复联网。"""
        if self._check_started:
            return
        self._check_started = True
        try:
            self.lb_info.setText(self.lb_info.text() + " · 正在检查更新…")
        except RuntimeError:
            pass
        from ..core.asyncio_qt import run_async
        from ..core import modupdate
        run_async(modupdate.check_updates, self.folder, self._mc, self._loader,
                  on_done=self._on_update_result, on_err=lambda _e: None)

    def _on_update_result(self, result):
        try:
            self._updatable = dict(result or {})
        except RuntimeError:                     # 面板已销毁
            return
        # 行内更新图标：已建的行直接贴，未建的行在 _build_chunk 里贴
        for w in self._row_widgets:
            try:
                w.set_updatable(self._updatable.get(_norm(w.row["path"])))
            except RuntimeError:
                continue
        # 更新总览开关：有可更新才可用（数量已在信息栏显示）
        has = bool(self._updatable)
        self.b_update.setEnabled(has)
        if has:
            self.b_update.setToolTip(
                "更新总览：只显示可更新的模组（%d 个）" % len(self._updatable))
        else:
            self.b_update.setToolTip("更新总览：当前没有可更新的模组")
            if self._filter == "update":
                self._filter = "all"
                self._set_active(self.b_update, False)
                self._rebuild_for_filter()
        self._refresh_info()
        # 若正停在「更新总览」筛选项，把可更新的那批补出来
        if self._filter == "update" and has:
            self._rebuild_for_filter()

    def update_one(self, row):
        """就地把某个模组升到最新兼容版本（后台下载 + 安装 + 替换旧文件）"""
        info = self._updatable.get(_norm(row["path"]))
        if not info:
            return
        old_path = row["path"]
        Toast.show_toast(self.window(), "正在更新 %s …" % row["name"], ms=2600,
                         kind="info")
        from ..core.asyncio_qt import run_async
        from ..core import modupdate

        def on_done(res):
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            Toast.show_toast(self.window(), msg, ms=3800,
                             kind="success" if ok else "error")
            if ok:
                # 更新成功后清掉这条旧「可更新」条目，否则信息栏数量会虚高
                self._updatable.pop(_norm(old_path), None)
                self.reload()
                self._on_update_result(self._updatable)
                if self._on_changed:
                    self._on_changed()

        run_async(modupdate.update_mod, info, self.folder, old_path,
                  on_done=on_done,
                  on_err=lambda e: Toast.show_toast(
                      self.window(), "更新失败：%s" % str(e)[:80], ms=3800,
                      kind="error"))
