# -*- coding: utf-8 -*-
"""「导入游戏」窗口 + 「已导入」列表窗口。

语义（第二十七~三十一轮定型）：
  · **导入游戏**（第六十六轮起只从「已导入」里进：勾条目点「导入」，或「添加已有文件夹」
    之后自动导入）：扫本机其它启动器（PCL2 / HMCL / Prism…）里能接管的
    版本与整合包。勾选后点「导入选中」才会**搬进** `versions/<包名>` 并写官启档案 ——
    这一步会复制文件，所以默认不自动做。
  · **添加已有文件夹**：指到哪认到哪（实例目录 / 版本目录 / 带 versions 的根 /
    一堆实例的上级）。**默认只登记不复制** —— 版本目录本身就能直接跑
    （libraries / assets 都在上两级的 .minecraft 里），点「运行」即可开局。
  · **已导入**（AddedGamesDialog，游戏管理里唯一的入口）：登记过的目录 + 已经搬进
    官启的，一行一个；能直接运行的显示「运行」，要进官启的显示「导入」。
    每一行还有齿轮 —— 打开跟内置游戏一样的独立设置（模组禁用 / 启用 / 删除都在里面）。
  · 设置 → 常规 的「自动导入」打开时，「添加已有文件夹」才会加完立刻搬进官启。

交互约定（沿用项目里其它弹窗的规矩）：
  · 走 _DialogBase，所以自带「拖不出主窗口」的夹取；
  · 一切提示用 Toast，不弹系统框；
  · 扫描与导入都在工作线程里做，界面只读结果（绝不从线程碰 QWidget）；
    复制进度靠「工作线程只写字符串 + 主线程定时器读」传递。
"""
import os

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..core import gamelaunch, importer
from ..core.asyncio_qt import run_async
from . import gamecats as gc
from . import theme
from .badge import LoaderBadge
from .dialogs import _DialogBase
from .fspicker import pick_dir
from .widgets import (ElideLabel, GlyphIcon, IconButton, PillButton,
                      ProgressBar, Toast)

_LOADER_CN = importer.LOADER_CN


class _CheckBox(QWidget):
    """18×18 的自绘勾选框（不用系统 QCheckBox，配色才跟得上磨砂玻璃）"""

    clicked = Signal()

    def __init__(self, parent=None, size=18):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.on = False

    def set_on(self, v):
        if self.on != v:
            self.on = bool(v)
            self.update()

    # 第六十九轮：列表里的勾选要能点（全选 / 批量移除用），
    # 所以自带 mousePressEvent；父行/文件夹标题的点击不会被它吞掉以外的东西影响。
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    def paintEvent(self, e):
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        if self.on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#3f7d5a"))
            p.drawRoundedRect(r, 5, 5)
            p.setPen(QPen(QColor("#ffffff"), 1.8, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            sz = self.width()
            p.drawPolyline(QPolygonF([
                QPointF(sz * 0.24, sz * 0.52),
                QPointF(sz * 0.43, sz * 0.70),
                QPointF(sz * 0.77, sz * 0.31)]))
        else:
            p.setPen(QPen(QColor(120, 132, 148, 170), 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 5, 5)
        p.end()


class _Toggle(QWidget):
    """勾选框 + 说明文字。窗口里独立的开关也自绘，系统 QCheckBox 的配色对不上。"""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.chk = _CheckBox(size=16)
        lay.addWidget(self.chk)
        self.lb = QLabel(text)
        self.lb.setStyleSheet("color:#5a6675;font-size:10.5px;"
                              "background:transparent;")
        lay.addWidget(self.lb)
        self.setToolTip("默认保留原目录，两个启动器都能用；勾了导入完把它移到回收站，"
                        "省一份空间（回收站里可还原）")

    def isChecked(self):
        return bool(self.chk.on)

    def setChecked(self, v):
        self.chk.set_on(v)

    def setEnabled(self, v):
        super().setEnabled(bool(v))
        self.setCursor(Qt.PointingHandCursor if v else Qt.ArrowCursor)
        color = "#5a6675" if v else "#9aa4b0"
        self.lb.setStyleSheet(f"color:{color};font-size:10.5px;background:transparent;")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.isEnabled():
            self.chk.set_on(not self.chk.on)


class _ImportRow(QWidget):
    """一条可导入项：勾选框 + 图标 + 名称 + 元信息 + 来源"""

    def __init__(self, entry, dialog):
        super().__init__()
        self.entry = entry
        self.dialog = dialog
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("importRow")
        self.setFixedHeight(54)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False
        self._state = "已导入" if entry.get("imported") else ""
        self._apply_style()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(9)
        self.chk = _CheckBox()
        self.chk.set_on(False)
        lay.addWidget(self.chk)

        # 图标：整合包/版本自己的图（PCL2 的 icon.png、Prism 的 icons/…），
        # 没有才回退加载器徽章 —— 和「游戏管理」用同一个控件，观感一致。
        self.badge = LoaderBadge(entry, entry.get("kind") or "instance", 28)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setSpacing(1)
        self.lb_name = ElideLabel(entry.get("name") or "-")
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        self.lb_name.setFont(f)
        self.lb_name.setStyleSheet("background:transparent;")
        self.lb_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        box.addWidget(self.lb_name)

        self.lb_meta = ElideLabel(self._meta())
        self.lb_meta.setStyleSheet("color:#63707f;font-size:10.5px;"
                                  "background:transparent;")
        self.lb_meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        box.addWidget(self.lb_meta)
        lay.addLayout(box, 1)

        # 第八十九轮：内容类型图标（模组/材质包/光影/数据包），后台算好再填
        self.content_box = QWidget()
        self.content_box.setStyleSheet("background:transparent;")
        _cb = QHBoxLayout(self.content_box)
        _cb.setContentsMargins(0, 0, 0, 0)
        _cb.setSpacing(6)
        lay.addWidget(self.content_box, 0, Qt.AlignVCenter)

        self.lb_badge = QLabel("")
        self.lb_badge.setStyleSheet("color:#5b8f6d;font-size:10px;"
                                    "background:transparent;")
        lay.addWidget(self.lb_badge)
        if entry.get("imported"):
            self.lb_badge.setText("已导入")
            self.setEnabled(False)
            self._apply_style()
        _schedule_content(self)

    def _meta(self):
        e = self.entry
        bits = []
        if e.get("mc"):
            bits.append(f"MC {e['mc']}")
        if e.get("loader"):
            cn = _LOADER_CN.get(e["loader"], str(e["loader"]).capitalize())
            bits.append(f"{cn} {e.get('loader_ver') or ''}".strip())
        elif e.get("kind") == "version":
            bits.append("原版")
        bits.append("实例" if e.get("kind") == "instance" else "版本")
        bits.append(str(e.get("source") or ""))
        return " · ".join([b for b in bits if b])

    def _apply_style(self):
        if not self.isEnabled():
            bg = "rgba(255,255,255,96)"
        elif self.chk.on if hasattr(self, "chk") else False:
            bg = "rgba(63,125,90,40)"
        elif self._hover:
            bg = "rgba(255,255,255,190)"
        else:
            bg = "rgba(255,255,255,150)"
        # 必须用 #name 限定：写成 QWidget{...} 会级联命中行里的所有子控件
        #（QLabel/ElideLabel 全被画上白底，右侧徽章还会被撑高跑出行外）
        self.setStyleSheet(f"#importRow{{background:{bg};border-radius:9px;}}")

    def enterEvent(self, e):
        self._hover = True
        self._apply_style()

    def leaveEvent(self, e):
        self._hover = False
        self._apply_style()

    def mousePressEvent(self, e):
        if not self.isEnabled() or e.button() != Qt.LeftButton:
            return
        self.chk.set_on(not self.chk.on)
        self._apply_style()
        self.dialog.sync_buttons()

    def set_state(self, text, kind="info"):
        self.lb_badge.setText(text)
        color = {"ok": "#4a8f63", "err": "#c9534c", "info": "#5a6675"}.get(kind, "#5a6675")
        self.lb_badge.setStyleSheet(f"color:{color};font-size:10px;background:transparent;")

    def set_on(self, v):
        self.chk.set_on(v)
        self._apply_style()


class _AddedRow(QWidget):
    """「已导入」页的一行：图标 + 名称 + 状态 + 运行/导入/打开/移除

    `compact=True`（第九十三轮）：启动选择抽屉里那份**只留「运行」和「目录」**，
    整行可点 = 选中它当启动目标（那边是「挑一个开局」，不是管理列表 —— 导入 / 设置 /
    移除三个按钮摆在那儿只会让人点错）。游戏管理里的「已导入」不受影响。
    """

    def __init__(self, entry, dialog, compact=False):
        super().__init__()
        self.entry = entry
        self.dialog = dialog
        self._compact = bool(compact)
        self._selected = False
        self._batch = False
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("addedRow")
        self.setFixedHeight(56)
        self._hover = False
        if self._compact:
            self.setCursor(Qt.PointingHandCursor)
            # 第 106 轮：抽屉里的行支持 Ctrl 批量点选 + 长按拖到分类
            self._press = QPoint()
            self._drag_ready = False
            self._pending_remove = False   # 第一一二轮：松开 Ctrl 后按下已选行，
                                           # 长按=整批拖走；快速松开才取消这项
            self._hold = QTimer(self)
            self._hold.setSingleShot(True)
            self._hold.setInterval(420)
            self._hold.timeout.connect(self._on_hold)
        self._apply_style()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        lay.setSpacing(9)

        # 第六十九轮：只有「已导入的游戏」窗口要勾选（全选 / 批量移除），
        # 启动选择抽屉里那一份不显示勾选框 —— 那边是「挑一个启动」，不是管理列表。
        self.key = os.path.normcase(os.path.realpath(str(entry.get("path") or "")))
        self.chk = None
        self._checked = False
        if getattr(dialog, "sel_enabled", False):
            self.chk = _CheckBox()
            self.chk.setToolTip("勾上后可「移除选中」")
            self.chk.clicked.connect(self._toggle_sel)
            lay.addWidget(self.chk, 0, Qt.AlignVCenter)

        self.badge = LoaderBadge(entry, entry.get("kind") or "instance", 30)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setSpacing(1)
        self.lb_name = ElideLabel(entry.get("name") or "-")
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        self.lb_name.setFont(f)
        self.lb_name.setStyleSheet("background:transparent;")
        self.lb_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        box.addWidget(self.lb_name)

        self.lb_meta = ElideLabel(self._meta())
        self.lb_meta.setStyleSheet("color:#63707f;font-size:10.5px;"
                                   "background:transparent;")
        self.lb_meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        box.addWidget(self.lb_meta)
        lay.addLayout(box, 1)

        # 第八十九轮：内容类型图标（模组/材质包/光影/数据包），后台算好再填
        self.content_box = QWidget()
        self.content_box.setStyleSheet("background:transparent;")
        _cb = QHBoxLayout(self.content_box)
        _cb.setContentsMargins(0, 0, 0, 0)
        _cb.setSpacing(6)
        lay.addWidget(self.content_box, 0, Qt.AlignVCenter)

        # 精简模式：「这就是当前启动目标」的 ✓（跟 _PickRow 一致）
        self.lb_sel = None
        if self._compact:
            self.lb_sel = QLabel("✓", self)
            f2 = QFont(theme.FONT)
            f2.setPixelSize(14)
            f2.setBold(True)
            self.lb_sel.setFont(f2)
            self.lb_sel.setStyleSheet("color:#2f7cf6;background:transparent;")
            self.lb_sel.setToolTip("这就是当前启动目标")
            self.lb_sel.hide()
            lay.addWidget(self.lb_sel, 0, Qt.AlignVCenter)

        # 主按钮：能直接跑就给「运行」，否则给「导入」（进官方启动器）
        # 已经配进官启的（「已配置」页里那些）不再给「导入」——点了也只会回一句
        # 「已经在官方启动器里了」，摆在那儿纯属误导。
        if self._compact:
            self.btn_main = PillButton("运行", "accent")
            self.btn_main.clicked.connect(self._act)
            if entry.get("runnable"):
                self.btn_main.setToolTip(
                    "独立启动直接开局，不复制任何文件"
                    if gamelaunch.standalone_enabled()
                    else "打开官方启动器，在它的版本列表里选这个游戏开局")
            else:
                # 保留下「运行」这个名字（一眼知道这行是干嘛的），但点不动，
                # 鼠标悬停说明原因 —— 比变出一个「导入」按钮更不容易误会。
                self.btn_main.setEnabled(False)
                self.btn_main.setToolTip("这个目录里没有能直接运行的版本 —— "
                                         "先去游戏管理里「导入」到官方启动器")
            lay.addWidget(self.btn_main, 0, Qt.AlignVCenter)
        elif entry.get("runnable"):
            self.btn_main = PillButton("运行", "accent")
            self.btn_main.clicked.connect(self._act)
            # 点下去到底会发生什么，先说清楚（官方 / 独立启动两条路）
            self.btn_main.setToolTip(
                "独立启动直接开局，不复制任何文件"
                if gamelaunch.standalone_enabled()
                else "打开官方启动器，在它的版本列表里选这个游戏开局")
            lay.addWidget(self.btn_main, 0, Qt.AlignVCenter)
        elif not (entry.get("imported") or entry.get("configured")):
            self.btn_main = PillButton("导入", "ghost")
            self.btn_main.clicked.connect(self._act)
            lay.addWidget(self.btn_main, 0, Qt.AlignVCenter)
        else:
            self.btn_main = None
        if self.btn_main is not None:
            self.btn_main.setFixedHeight(26)
            self.btn_main.setFixedWidth(56)

        self.btn_dir = IconButton("folder", 26, 13)
        self.btn_dir.setToolTip("打开所在文件夹")
        self.btn_dir.clicked.connect(self._open_dir)
        lay.addWidget(self.btn_dir)

        # 第六十六轮：导入进来的游戏也要有**独立设置** —— 跟启动器里下载的游戏一样，
        # 齿轮进去就是那套「概览 / 开关 / Java / 内存 / 导出」，概览里的模组清单
        # 能就地禁用 / 启用 / 删除，「打开」走内置文件管理器。
        # 第九十三轮：抽屉里那份精简模式不摆齿轮和垃圾桶（那两个动作属于「管理」）。
        self.btn_cfg = None
        self.btn_del = None
        if not self._compact:
            self.btn_cfg = IconButton("gear", 26, 13, color="#5a6675")
            self.btn_cfg.setToolTip("这个游戏的独立设置（模组 / 文件夹 / Java / 内存）")
            self.btn_cfg.clicked.connect(self._open_settings)
            lay.addWidget(self.btn_cfg)

            self.btn_del = IconButton("trash", 26, 13, color="#c05a52")
            self.btn_del.setToolTip("从「已导入」里移除（不动硬盘上的文件）")
            self.btn_del.clicked.connect(self._remove)
            lay.addWidget(self.btn_del)

        hook = getattr(dialog, "register_row", None)
        if callable(hook):
            hook(self)
        _schedule_content(self)

    def _meta(self):
        e = self.entry
        bits = []
        if e.get("mc"):
            bits.append(f"MC {e['mc']}")
        if e.get("loader"):
            cn = _LOADER_CN.get(e["loader"], str(e["loader"]).capitalize())
            bits.append(f"{cn} {e.get('loader_ver') or ''}".strip())
        if e.get("configured"):
            bits.append("已配进官启器")
        elif e.get("runnable"):
            bits.append("可直接运行")
        else:
            bits.append("需配置进官启")
        bits.append(str(e.get("path") or ""))
        return " · ".join([b for b in bits if b])

    def _apply_style(self):
        if self._batch:
            bg = "rgba(47,124,246,58)"
            edge = "border:1.5px solid rgba(47,124,246,220);"
        elif self._compact and self._selected:
            bg = "rgba(255,255,255,240)"
            edge = "border:1.5px solid rgba(47,124,246,220);"
        else:
            bg = ("rgba(255,255,255,196)" if self._hover else "rgba(255,255,255,150)")
            edge = ("border:1.5px solid rgba(47,124,246,150);"
                    if self._compact and self._is_target() else "")
        # 同 _ImportRow：限定 objectName，别让背景色级联到行内的文字控件
        self.setStyleSheet(f"#addedRow{{background:{bg};border-radius:9px;{edge}}}")
        try:
            if self.lb_sel is not None:
                self.lb_sel.setVisible(bool(self._selected))
        except (RuntimeError, AttributeError):
            pass

    def set_batch_selected(self, on):
        on = bool(on)
        if on == self._batch:
            return
        self._batch = on
        self._apply_style()

    # ---- 选中（第九十三轮，仅抽屉精简模式）----
    def _is_target(self):
        """这一行是不是当前启动目标（比 version_id + game_dir，别拿名字比）"""
        try:
            t = gamelaunch.launch_target() or {}
        except Exception:                                 # noqa: BLE001
            return False
        vid = str(self.entry.get("version_id") or "")
        if not vid or str(t.get("version_id") or "") != vid:
            return False
        want = os.path.normcase(str(t.get("game_dir") or ""))
        have = os.path.normcase(str(self.entry.get("game_dir") or ""))
        return (not want) or (not have) or want == have

    def set_selected(self, on):
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._apply_style()

    def refresh_meta(self):
        """选中态变了，「当前目标」几个字要跟着换（不重建整行）"""
        try:
            txt = self._meta()
            self.lb_meta.setText(txt)
            self.lb_meta.setToolTip(txt)
        except (RuntimeError, AttributeError):
            pass

    def _on_action_btn(self, pos):
        """点的是按钮还是整行：按钮归按钮，别被「整行可点」吞掉"""
        w = self.childAt(pos)
        while w is not None and w is not self:
            if w in (self.btn_main, self.btn_dir):
                return True
            w = w.parentWidget()
        return False

    def mousePressEvent(self, e):
        if not (self._compact and e.button() == Qt.LeftButton
                and not self._on_action_btn(e.position().toPoint())):
            super().mousePressEvent(e)
            return
        self._press = e.position().toPoint()
        self._reset_drag()
        sel = getattr(self.dialog, "selection", None)
        if sel is not None:
            act = gc.press_action(sel, self.key)
            if act == "add":
                sync = getattr(self.dialog, "_sync_batch_styles", None)
                if callable(sync):
                    sync()
                return
            if act == "remove":
                # 第一一二轮：先放回去，长按=整批（含这一条）一起拖；
                # 快速松开才真正取消这项（见 mouseReleaseEvent）
                sel.add(self.key)
                self._pending_remove = True
                self._hold.start()
                return
        self._hold.start()
        hook = getattr(self.dialog, "pick_added", None)
        if callable(hook):
            hook(self)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._compact and self._drag_ready:
            if (e.position().toPoint() - self._press).manhattanLength() > 7:
                self._start_drag()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._compact:
            pending = self._pending_remove
            dragged = self._drag_ready  # 拖过的话 _start_drag 里已清掉 pending
            self._reset_drag()
            if pending and not dragged:
                sel = getattr(self.dialog, "selection", None)
                if sel is not None:
                    sel.remove(self.key)
                    sync = getattr(self.dialog, "_sync_batch_styles", None)
                    if callable(sync):
                        sync()
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
            key = self.key
            sel = getattr(self.dialog, "selection", None)
            rows, names = [], []
            if sel is not None and sel.count() > 1 and sel.has(key):
                index = getattr(self.dialog, "rows_by_key", {}) or {}
                for k in sel.keys_snapshot():
                    info = index.get(k)
                    if info:
                        r, kd = info
                        rows.append((k, kd))
                        names.append(gc.item_name(getattr(r, "entry", None)))
            if not rows:
                rows = [(key, gc.row_kind(self.entry.get("kind") or "instance"))]
                names = [gc.item_name(self.entry)]
            drag = gc.make_drag(self, rows, names, len(rows))
            self._reset_drag()
            if drag is not None:
                drag.exec(Qt.CopyAction)
        except Exception as e:                              # noqa: BLE001
            # 第一一一轮教训：拖拽失败必须留痕，别让玩家对着「拖不动」发呆
            try:
                from ..core import crashlog
                crashlog.note("已导入：拖拽启动失败 %s" % e)
            except Exception:                               # noqa: BLE001
                pass
            self._reset_drag()

    def enterEvent(self, e):
        self._hover = True
        self._apply_style()

    def leaveEvent(self, e):
        self._hover = False
        self._apply_style()

    # ---- 勾选（第六十九轮）----
    def set_checked(self, v):
        self._checked = bool(v)
        if self.chk is not None:
            self.chk.set_on(self._checked)

    def _toggle_sel(self):
        self.set_checked(not self._checked)
        hook = getattr(self.dialog, "row_checked", None)
        if callable(hook):
            hook(self.key, self._checked)

    # ---- 动作 ----
    def _act(self):
        if self.entry.get("runnable"):
            self.dialog.run_added(self.entry)
        else:
            self.dialog.import_added(self.entry)

    def _open_settings(self):
        """这个导入进来的游戏的独立设置窗（第六十六轮）。

        `GameSettingsDialog` 只要 item 里有 `path` / `game_dir` / `name` 就能算出
        各个内容目录（`launchprefs.game_dirs`），所以直接把条目丢进去即可 ——
        前提是**别把 entry 本身传进去**：那条数据是登记的原始记录，被设置窗改了字段
        就回不去了，复制一份最稳。
        """
        from .gamesettings import GameSettingsDialog
        e = dict(self.entry or {})
        win = None
        try:
            win = self.dialog.window() if self.dialog is not None else None
        except RuntimeError:
            win = None
        e.setdefault("name", os.path.basename(str(e.get("path") or "")) or "游戏")
        e.setdefault("game_dir", e.get("game_dir") or "")
        d = GameSettingsDialog(win, e, str(e.get("kind") or "instance"))
        d.exec()
        # 设置里可能新建了 mods / 改了隔离，回来把状态行重刷一遍
        try:
            self.lb_meta.setText(self._meta())
        except RuntimeError:
            pass

    def _open_dir(self):
        """打开所在文件夹。

        第六十六轮：改成跟别处一致 —— **内置文件管理器优先**（`fspicker.pick_view`），
        设置里关掉了它才落回系统资源管理器。以前这里直接 `os.startfile`，
        于是「内置文件管理器」这个开关对导入的游戏完全不起作用。
        """
        p = str(self.entry.get("path") or "")
        if not p:
            return
        try:
            os.makedirs(p, exist_ok=True)
            from .fspicker import pick_view
            pick_view(self.dialog.window() if self.dialog is not None else None,
                      "游戏目录", p)
        except Exception as exc:                              # noqa: BLE001
            Toast.show_toast(self.dialog.parent() if self.dialog else None,
                             f"打不开：{str(exc)[:60]}", kind="error")

    def _remove(self):
        e = self.entry
        did = importer.forget_added(e.get("path"))
        if did or e.get("in_launcher") or e.get("imported"):
            extra = "；它已经在官方启动器里，那边照旧能玩" if e.get("in_launcher") else ""
            Toast.show_toast(self.dialog.parent(),
                             f"已从列表移除「{e.get('name')}」（硬盘上的文件没动{extra}）",
                             ms=3000, kind="info")
            self.dialog.reload_added(force=True)


# ------------------------------------------------------ 收放式文件夹（分类）
# 第九十一轮：`_GIcon` 搬到 `widgets.GlyphIcon`（概览页那五行也要用同款），这里只留别名，
# 免得这个文件里十几处调用全要改。
_GIcon = GlyphIcon


# 第八十九轮：内容类型图标；第九十轮补上中文名（做悬停提示）并把图标放大一号
_CONTENT_META = (
    ("mods", "mods", "#5b8f6d", "模组"),
    ("resourcepack", "resourcepack", "#4d7fb5", "材质包"),
    ("shader", "shader", "#8a6fb0", "光影"),
    ("datapack", "datapack", "#b08a3e", "数据包"),
)


def _content_pair(host, icon_name, n, col, label):
    """「图标 + 数字」包成一组：组内贴紧，组与组之间才留空，看起来是一项一项的。"""
    grp = QWidget(host)
    grp.setStyleSheet("background:transparent;")
    grp.setToolTip("%s %d 个" % (label, n))
    h = QHBoxLayout(grp)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(1)
    h.addWidget(_GIcon(icon_name, size=14, color=col, parent=grp), 0, Qt.AlignVCenter)
    lb = QLabel(str(n), grp)
    lb.setStyleSheet("background:transparent;color:%s;font-size:10px;" % col)
    h.addWidget(lb, 0, Qt.AlignVCenter)
    return grp


def _schedule_content(row):
    """后台算这一行实例的内容数量，算好回主线程填图标（不卡列表构建那一帧）"""
    box = getattr(row, "content_box", None)
    entry = getattr(row, "entry", None)
    if box is None or not isinstance(entry, dict):
        return
    from ..core import contentcount
    run_async(contentcount.counts_for_entry, entry,
              on_done=lambda counts, r=row: _apply_content(r, counts),
              on_err=lambda _e: None)


def _apply_content(row, counts):
    """把计数填成「图标 + 数字」。0 的类型不显示；一个都没有就整块隐藏。"""
    box = getattr(row, "content_box", None)
    if box is None:
        return
    try:
        lay = box.layout()
    except RuntimeError:
        return
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()                # 先 hide 再删（铁律：绝不 setParent(None)）
            w.deleteLater()
    any_on = False
    for key, icon_name, col, label in _CONTENT_META:
        try:
            n = int((counts or {}).get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            continue
        any_on = True
        lay.addWidget(_content_pair(box, icon_name, n, col, label),
                      0, Qt.AlignVCenter)
    box.setVisible(any_on)


class _FolderHead(QWidget):
    """文件夹标题行：勾选 + 文件夹图标 + 名称 + 数量 + 右侧（移除 / 三角）"""

    H = 38

    def __init__(self, name, count, is_open, on_toggle, parent=None,
                 dialog=None, gid="", on_remove=None):
        super().__init__(parent)
        self.gid = str(gid or "")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("folderHead")
        self.setFixedHeight(self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.is_open = bool(is_open)
        self._on_toggle = on_toggle
        self._dialog = dialog
        self._hover = False
        self._checked = False
        self._apply_style()

        # 有勾选框 / 有移除按钮时，左右留的边距要跟着变：
        # 三角画在 width-23..width-10（移除按钮在它左边，所以右边距要留 42）。
        self.chk = None
        self.btn_del = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 42 if on_remove else 26, 4)
        lay.setSpacing(8)
        if getattr(dialog, "sel_enabled", False):
            self.chk = _CheckBox(size=16)
            self.chk.setToolTip("选中这个分类里的全部游戏")
            self.chk.clicked.connect(self._toggle_sel)
            lay.addWidget(self.chk, 0, Qt.AlignVCenter)
        lay.addWidget(_GIcon("folder"), 0, Qt.AlignVCenter)
        self.lb_name = ElideLabel(name)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        self.lb_name.setFont(f)
        self.lb_name.setStyleSheet("background:transparent;")
        self.lb_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        lay.addWidget(self.lb_name, 1)
        self.lb_count = QLabel(f"{count} 个")
        self.lb_count.setStyleSheet("color:#63707f;font-size:10.5px;"
                                    "background:transparent;")
        lay.addWidget(self.lb_count, 0, Qt.AlignVCenter)
        # 第六十九轮：整组移除 —— 摆在三角**左边**，只删列表里的登记，硬盘不动
        if on_remove is not None:
            self.btn_del = IconButton("trash", 24, 12, color="#c05a52")
            self.btn_del.setToolTip("移除这个分类（里面的游戏一起从列表移除，"
                                    "硬盘上的文件不动）")
            self.btn_del.clicked.connect(lambda: on_remove(self.gid))
            lay.addWidget(self.btn_del, 0, Qt.AlignVCenter)

    def set_checked(self, v):
        self._checked = bool(v)
        if self.chk is not None:
            self.chk.set_on(self._checked)

    def _toggle_sel(self):
        self.set_checked(not self._checked)
        hook = getattr(self._dialog, "group_checked", None)
        if callable(hook):
            hook(self.gid, self._checked)

    def _apply_style(self):
        bg = "rgba(255,255,255,190)" if self._hover else "rgba(255,255,255,110)"
        # 照旧只限定 objectName：写 QWidget{...} 会级联把行内文字控件刷上底色
        self.setStyleSheet(f"#folderHead{{background:{bg};border-radius:8px;}}")

    def enterEvent(self, e):
        self._hover = True
        self._apply_style()

    def leaveEvent(self, e):
        self._hover = False
        self._apply_style()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and callable(self._on_toggle):
            self._on_toggle()

    def paintEvent(self, e):
        super().paintEvent(e)          # 先让样式表把圆角底色铺上
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 右侧小三角：展开朝下，收起朝右
        theme.draw_icon(p, "chev" if self.is_open else "chev_right",
                        QRectF(self.width() - 23, (self.H - 13) / 2, 13, 13),
                        color="#5a6675", w=1.9)
        p.end()


class _AddedFolder(QWidget):
    """收放式文件夹：一行标题 + 里面的游戏行（点三角 / 标题展开收起）

    展开状态记在调用方给的 `open_ids` 集合里（不是控件自己身上）—— 列表刷新时
    控件会整批重建，状态留在外面才不会被重置回「全部收起」。
    """

    def __init__(self, gid, name, entries, dialog, open_ids=None, on_remove=None,
                 compact=False):
        super().__init__()
        self.gid = gid
        self.dialog = dialog
        self.entries = list(entries or [])
        self.rows = []
        self._open_ids = open_ids if open_ids is not None else set()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("folderBox")
        self.setStyleSheet("#folderBox{background:rgba(31,39,51,10);"
                           "border-radius:11px;}")
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(5)
        self.head = _FolderHead(name, len(self.entries),
                                gid in self._open_ids, self.toggle,
                                dialog=dialog, gid=gid, on_remove=on_remove)
        v.addWidget(self.head)

        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(14, 0, 0, 0)      # 子行往里缩进，看得出从属
        bl.setSpacing(5)
        for e in self.entries:
            row = _AddedRow(e, dialog, compact=compact)
            self.rows.append(row)
            bl.addWidget(row)
        v.addWidget(self.body)
        self.body.setVisible(self.head.is_open)
        hook = getattr(dialog, "register_folder", None)
        if callable(hook):
            hook(self)

    def toggle(self):
        on = not self.head.is_open
        self.head.is_open = on
        self.head.update()
        self.body.setVisible(on)
        if on:
            self._open_ids.add(self.gid)
        else:
            self._open_ids.discard(self.gid)

    # ---- 勾选：整组一起勾 / 一起取消（第六十九轮）----
    def set_all(self, on):
        on = bool(on)
        self.head.set_checked(on)
        for r in self.rows:
            r.set_checked(on)

    def keys(self):
        return [r.key for r in self.rows]


class _GroupPopup(QWidget):
    """「分类文件夹重命名」浮层。

    选完文件夹弹出来：输入框默认填**那个大文件夹的名字**，右侧是「×」，
    × 左边是「勾」——勾 = 确认，按这个名字建一个收放式文件夹；× = 取消，
    这一次导入就当没发生（一个都不登记）。
    """

    W = 330

    def __init__(self, parent, default_name, on_done=None):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._on_done = on_done
        self._fired = False
        self._default = str(default_name or "未命名文件夹")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(7)
        t = QLabel("给这个文件夹起个名字")
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        t.setFont(f)
        t.setStyleSheet("background:transparent;")
        outer.addWidget(t)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.ed = QLineEdit(self._default)
        self.ed.setFixedHeight(32)
        self.ed.setPlaceholderText("分类文件夹名")
        self.ed.returnPressed.connect(self._ok)
        row.addWidget(self.ed, 1)
        # 勾在左、× 在右，两个都是图标按钮
        self.btn_ok = IconButton("check", 32, 15, color="#3f7d5a")
        self.btn_ok.setToolTip("确认：按这个名字建文件夹")
        self.btn_ok.clicked.connect(self._ok)
        row.addWidget(self.btn_ok)
        self.btn_no = IconButton("close", 32, 15, color="#c05a52")
        self.btn_no.setToolTip("取消：这次不导入")
        self.btn_no.clicked.connect(self._cancel)
        row.addWidget(self.btn_no)
        outer.addLayout(row)

        hint = QLabel("确认后「已导入」里会多一个可展开的文件夹；点 × 就当没加过")
        hint.setStyleSheet("color:#6b7684;font-size:10.5px;background:transparent;")
        hint.setWordWrap(True)
        hint.setFixedWidth(self.W - 28)         # Qt.Popup 里 wordWrap 必须定宽
        outer.addWidget(hint)
        self.setFixedWidth(self.W)
        self.adjustSize()

    # ---- 结果 ----
    def _ok(self):
        name = self.ed.text().strip() or self._default
        self._fire(name)

    def _cancel(self):
        self._fire(None)

    def _fire(self, name):
        if self._fired:
            return
        self._fired = True
        cb = self._on_done
        self.close()
        if cb:
            cb(name)

    def hideEvent(self, e):
        super().hideEvent(e)
        # 点外面 / Esc 都算取消：绝不悄悄按默认名导进去
        if not self._fired:
            self._fire(None)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(e)

    def show_at(self, anchor):
        """贴着按钮弹：优先弹在按钮上方，放不下才翻到下方"""
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QGuiApplication
        self.adjustSize()
        base = anchor.mapToGlobal(QPoint(0, 0))
        x = base.x() + anchor.width() - self.width()
        y = base.y() - self.height() - 8
        scr = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
        r = scr.availableGeometry()
        x = min(max(x, r.left() + 8), r.right() - self.width() - 8)
        if y < r.top() + 8:
            y = base.y() + anchor.height() + 8
        y = min(max(y, r.top() + 8), r.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()
        self.ed.setFocus()
        self.ed.selectAll()

    def paintEvent(self, e):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)
        p.fillPath(path, QColor(252, 253, 255, 249))
        p.setPen(QPen(QColor(31, 39, 51, 34), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


def ask_folder_name(parent, anchor, dir_path, on_ok, on_cancel=None):
    """弹「分类文件夹重命名」浮层。

    勾 → `on_ok(name)`；× / Esc / 点外面 → `on_cancel()`（这次导入直接放弃）。
    返回浮层控件本身（测试里拿它点按钮）。
    """
    default = importer.default_group_name(dir_path)

    def done(name):
        if name:
            on_ok(name)
        elif on_cancel:
            on_cancel()

    pop = _GroupPopup(parent, default, on_done=done)
    pop.show_at(anchor)
    return pop


def render_added_list(layout, entries, dialog, open_ids=None, rows_out=None,
                      compact=False):
    """把「已导入」条目按分类文件夹铺进 layout（窗口与右侧抽屉共用）。

    · 有 `group` 且文件夹还在登记表里的 → 塞进收放式文件夹（默认收起）；
    · 其余的 → 老样子平铺在下面，不影响没分过类的老数据。
    `rows_out` 收集所有叶子行（扁平），调用方统计条数用。
    """
    known = {g["id"]: g for g in importer.groups()}
    buckets, order, loose = {}, [], []
    for e in entries or []:
        gid = str(e.get("group") or "")
        if gid and gid in known:
            if gid not in buckets:
                buckets[gid] = []
                order.append(gid)
            buckets[gid].append(e)
        else:
            loose.append(e)
    order.sort(key=lambda g: known[g].get("time") or "")
    rows = [] if rows_out is None else rows_out
    on_remove = getattr(dialog, "remove_folder", None)
    for gid in order:
        folder = _AddedFolder(gid, known[gid].get("name") or "未命名文件夹",
                              buckets[gid], dialog, open_ids, on_remove=on_remove,
                              compact=compact)
        rows.extend(folder.rows)
        layout.addWidget(folder)
    for e in loose:
        row = _AddedRow(e, dialog, compact=compact)
        rows.append(row)
        layout.addWidget(row)
    return len(order)


class ImportGameDialog(_DialogBase):
    """导入窗口本体。`on_finish` 在全部导入跑完后回调（用来刷新游戏管理）

    「已导入」列表不在这个窗口里 —— 它在游戏管理标题栏那个 ✓ 按钮里
    （见 AddedGamesDialog）。这里只管「从别的启动器扫出来 / 手动指一个文件夹 → 搬进官启」。
    """

    def __init__(self, parent, on_finish=None, preset=None):
        super().__init__(parent, width=660, title="导入游戏")
        self.on_finish = on_finish
        self._rows = []
        self._queue = []
        self._busy = False
        self._extra_roots = []
        self._ok_count = 0
        self._fail = []
        self._prog = ""          # 工作线程只往这里写字符串，主线程定时器读
        self._head = ""

        tip = QLabel("把 PCL2 / HMCL / Prism 等启动器里的版本和整合包接管过来。\n"
                     "「添加已有文件夹」直接指到那个包的目录即可：默认**不复制**，"
                     "加完在「游戏管理 → 已导入」里点「运行」用原目录开局；"
                     "想让它也出现在官方启动器里，就在这个列表里勾上点「导入选中」。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#63707f;font-size:11px;")
        self.body.addWidget(tip)

        self.body.addWidget(self._build_import_page())

        row = self.add_buttons([("关闭", "ghost", self.reject),
                                ("导入选中", "accent", self._start)])
        self.btn_close = row.itemAt(row.count() - 2).widget()
        self.btn_go = row.itemAt(row.count() - 1).widget()
        self.btn_go.icon_name = "import_game"     # paintEvent 直接读这个字段
        self.btn_go.update()

        if preset:
            QTimer.singleShot(80, lambda: self._import_preset(preset))
        self.sync_buttons()
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        QTimer.singleShot(30, self.rescan)

    # ---------------- 页面骨架 ----------------
    def _build_import_page(self):
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.lb_status = ElideLabel("正在扫描…")
        self.lb_status.setStyleSheet("color:#4c5866;font-size:11px;font-weight:600;")
        lay.addWidget(self.lb_status)

        self.scroll = self._make_scroll()
        self.host = QWidget()
        self.host.setStyleSheet("background:transparent;")
        self.list = QVBoxLayout(self.host)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(5)
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll)

        opt = QHBoxLayout()
        opt.setContentsMargins(2, 0, 0, 0)
        self.chk_move = _Toggle("搬完把原实例移到回收站（可还原）")
        opt.addWidget(self.chk_move)
        opt.addStretch(1)
        lay.addLayout(opt)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.btn_all = PillButton("全选", "ghost")
        self.btn_all.setFixedHeight(30)
        self.btn_all.clicked.connect(self._toggle_all)
        tools.addWidget(self.btn_all)

        self.btn_manual = PillButton("添加已有文件夹", "ghost")
        self.btn_manual.setFixedHeight(30)
        self.btn_manual.setToolTip("直接指到那个包/版本所在的文件夹。默认只登记不复制，"
                                   "登记后可以在「已导入」里一键运行")
        self.btn_manual.clicked.connect(self._manual)
        tools.addWidget(self.btn_manual)

        tools.addStretch(1)
        self.btn_rescan = IconButton("refresh", 30, 15, color="#5a6675")
        self.btn_rescan.setToolTip("重新扫描")
        self.btn_rescan.clicked.connect(self.rescan)
        tools.addWidget(self.btn_rescan)
        lay.addLayout(tools)
        return page

    @staticmethod
    def _make_scroll():
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        s.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{width:8px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(90,102,117,90);"
            "border-radius:4px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        s.setFixedHeight(292)
        return s

    # ---------------- 扫描 ----------------
    def rescan(self):
        if self._busy:
            return
        self._busy = True
        self._clear()
        self.lb_status.setText("正在扫描本机启动器…")
        self.sync_buttons()
        roots = list(self._extra_roots)

        def done(entries):
            self._busy = False
            self._render(entries)

        def err(e):
            self._busy = False
            self.lb_status.setText(f"扫描失败：{str(e)[:80]}")

        run_async(lambda: importer.scan(extra_roots=roots), on_done=done, on_err=err)

    def _clear(self):
        self._rows = []
        while self.list.count():
            it = self.list.takeAt(0)
            w = it.widget()
            if w is not None:
                # 跟 _clear_added 一个道理：先摘再删，别等 DeferredDelete
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _render(self, entries):
        self._clear()
        todo = [e for e in entries if not e.get("imported")]
        self.lb_status.setText(
            f"找到 {len(entries)} 个可导入的游戏，其中 {len(todo)} 个还没导入"
            if entries else "没找到其它启动器的游戏，可以点「添加已有文件夹」指定目录")
        for e in entries:
            row = _ImportRow(e, self)
            self._rows.append(row)
            self.list.addWidget(row)
        self.list.addStretch(1)
        for row in self._rows:
            if not row.isEnabled():
                row.set_state("已导入", "info")
        self.sync_buttons()

    def _picked(self):
        return [r for r in self._rows if r.isEnabled() and r.chk.on]

    def sync_buttons(self):
        n = len(self._picked())
        # 第一百一十六轮：繁体界面下按钮显示「導入選中」，但这里的判断必须按
        # 简体原文做，否则 startswith 永远不成立、按钮不更新。
        from . import i18n_ui as _iu
        for b in self.findChildren(PillButton):
            if _iu.original_text(b).startswith("导入选中"):
                b.setText(f"导入选中（{n}）" if n else "导入选中")
                b.setEnabled(n > 0 and not self._busy)
                # 文案变长 + 后加的图标都要重算宽度，否则图标会压在字的左边
                b.setFixedWidth(max(92, b.sizeHint().width()))
        if hasattr(self, "btn_all"):
            all_on = bool(self._rows) and all(
                (not r.isEnabled()) or r.chk.on for r in self._rows)
            self.btn_all.setText("全不选" if all_on and n else "全选")
            self.btn_all.setEnabled(bool(self._rows) and not self._busy)
            self.btn_rescan.setEnabled(not self._busy)
            self.btn_manual.setEnabled(not self._busy)

    def _toggle_all(self):
        on = not (bool(self._rows) and all(
            (not r.isEnabled()) or r.chk.on for r in self._rows))
        for r in self._rows:
            if r.isEnabled():
                r.set_on(on)
        self.sync_buttons()

    # ---------------- 添加已有文件夹 ----------------
    def _manual(self):
        """PCL2 式的「添加已有文件夹」：指到哪个目录就认哪个目录。

        认四种形态：实例目录 / 版本目录 / 带 versions 的启动器根 / 一堆实例的上级。
        加完**默认只登记**（配置里「自动导入」关着时）——登记进「已导入」，
        点「运行」直接用原目录跑，一个字节都不复制；打开「自动导入」才会立刻搬进官启。
        """
        if self._busy:
            return
        d = pick_dir(self, "添加已有文件夹（选那个包或版本所在的目录）",
                     os.path.expandvars(r"%APPDATA%\.minecraft"))
        if not d:
            return
        # 选完目录先问「分类文件夹叫什么」：勾 = 建文件夹并导入，× = 这次不导
        ask_folder_name(
            self, self.btn_manual, d,
            lambda name: self._manual_go(d, name),
            on_cancel=lambda: self.lb_status.setText("已取消导入（没有登记任何东西）"))

    def _manual_go(self, d, group_name):
        """用户点了勾：按这个名字建（或改名）收放式文件夹，然后照旧识别 + 登记"""
        gid = importer.group_id_for(d)
        importer.remember_group(gid, group_name, d)
        self._busy = True
        self._head = "正在识别这个文件夹…"
        self.lb_status.setText(self._head)
        self.sync_buttons()

        def done(entries):
            self._busy = False
            self.sync_buttons()
            if not entries:
                self.lb_status.setText("这个文件夹里没认出游戏版本或整合包")
                Toast.show_toast(self.parent(),
                                 "这里没找到能导入的游戏（要选带 mods 的整合包目录，"
                                 "或 versions 里的某个版本目录）", ms=5200, kind="error")
                return
            importer.remember_added(entries, group=gid)
            rows = self._insert_rows(entries)
            todo = [r for r in rows if r.isEnabled()]
            names = "、".join(e.get("name") or "" for e in entries[:3])
            if not todo:
                self.lb_status.setText(f"这个文件夹已经导入过了（文件夹「{group_name}」）")
                Toast.show_toast(self.parent(),
                                 f"「{entries[0].get('name')}」已经在官方启动器里了",
                                 ms=3000, kind="info")
                return
            if not importer.auto_import_enabled():
                for r in todo:
                    r.set_on(True)
                self.sync_buttons()
                self.lb_status.setText(
                    f"已添加 {names} 到文件夹「{group_name}」，"
                    f"去「游戏管理 → 已导入」里点「运行」就能开局")
                Toast.show_toast(
                    self.parent(),
                    f"已添加 {names}（文件夹「{group_name}」）：不用复制，关掉这个窗口，"
                    f"在「游戏管理 → 已导入」里展开文件夹点「运行」直接启动",
                    ms=6000, kind="success")
                return
            for r in todo:
                r.set_on(True)
            self.sync_buttons()
            self._head = (f"已添加 {len(todo)} 项（{names}），正在配置进官方启动器…"
                          if len(todo) > 1 else
                          f"已添加「{names}」，正在配置进官方启动器…")
            self.lb_status.setText(self._head)
            self._begin(todo)

        def err(e):
            self._busy = False
            self.sync_buttons()
            self.lb_status.setText(f"识别失败：{str(e)[:80]}")

        run_async(lambda: importer.scan_path(d), on_done=done, on_err=err)

    def _insert_rows(self, entries):
        """把新条目插到列表最上面（重复目录直接跳过），返回新建的行"""
        have = {os.path.normcase(os.path.realpath(r.entry.get("path") or ""))
                for r in self._rows}
        rows = []
        for e in entries:
            key = os.path.normcase(os.path.realpath(e.get("path") or ""))
            if key in have:
                continue
            have.add(key)
            row = _ImportRow(e, self)
            if row.isEnabled():
                row.set_state("待导入", "info")
            self.list.insertWidget(len(rows), row)
            rows.append(row)
        self._rows = rows + self._rows
        return rows

    # ---------------- 从「已导入」里点「导入」跳过来 ----------------
    def _import_preset(self, entries):
        """带着指定条目直接开跑（「已导入」里那些不能直跑的项走这条）"""
        rows = self._insert_rows([dict(e, imported=False) for e in entries])
        todo = [r for r in rows if r.isEnabled()]
        if not todo:
            Toast.show_toast(self.parent(), "这一项已经在官启里了", ms=2600, kind="info")
            return
        for r in todo:
            r.set_on(True)
        names = "、".join(str(e.get("name") or "") for e in entries[:2])
        self._head = f"正在把「{names}」配置进官方启动器…"
        self.lb_status.setText(self._head)
        self.sync_buttons()
        self._begin(todo)

    # ---------------- 导入 ----------------
    def _set_prog(self, text):
        """工作线程调这里：只写字符串，不碰任何控件"""
        self._prog = str(text or "")

    def _tick(self):
        if self._busy and self._prog:
            self.lb_status.setText(f"{self._head} —— {self._prog}")

    def _start(self):
        rows = self._picked()
        if not rows or self._busy:
            return
        self._ok_count = 0
        self._fail = []
        self._head = f"开始导入，共 {len(rows)} 项…"
        self.lb_status.setText(self._head)
        self._begin(rows)

    def _begin(self, rows):
        """真正开跑（「导入选中」和「添加已有文件夹」共用）"""
        self._queue = list(rows)
        self._ok_count = 0
        self._fail = []
        self._prog = ""
        self._busy = True
        self.chk_move.setEnabled(False)
        self.sync_buttons()
        self._timer.start()
        self._next()

    def _next(self):
        if not self._queue:
            self._finish()
            return
        row = self._queue.pop(0)
        done_n = self._ok_count + len(self._fail)
        self._prog = ""
        self._head = (f"正在导入 {done_n + 1}/{done_n + 1 + len(self._queue)}："
                      f"{row.entry.get('name')}")
        self.lb_status.setText(self._head)
        row.set_state("导入中…", "info")
        move = self.chk_move.isChecked()

        def done(res):
            ok, msg, extra = res if isinstance(res, tuple) and len(res) == 3 \
                else (False, str(res), {})
            if ok:
                self._ok_count += 1
                row.set_state("完成", "ok")
                row.entry["imported"] = True
                row.setEnabled(False)
                row._apply_style()
            else:
                self._fail.append((row.entry.get("name"), msg))
                row.set_state("失败", "err")
                row.lb_meta.setToolTip(msg)
            self._next()

        def err(e):
            self._fail.append((row.entry.get("name"), str(e)[:120]))
            row.set_state("失败", "err")
            self._next()

        run_async(lambda: importer.import_one(row.entry, progress=self._set_prog,
                                              move=move),
                  on_done=done, on_err=err)

    def _finish(self):
        self._busy = False
        self._timer.stop()
        self._prog = ""
        self.chk_move.setEnabled(True)
        self.sync_buttons()
        if self._fail:
            name, msg = self._fail[0]
            self.lb_status.setText(f"完成 {self._ok_count} 个，失败 {len(self._fail)} 个")
            Toast.show_toast(self.parent(), f"「{name}」导入失败：{msg}",
                             ms=6000, kind="error")
        else:
            self.lb_status.setText(f"全部导入完成（{self._ok_count} 个）")
            Toast.show_toast(self.parent(),
                             f"已导入 {self._ok_count} 个游戏（官启的版本列表与「安装」里都有了）",
                             ms=3600, kind="success")
        if self.on_finish:
            try:
                self.on_finish()
            except Exception:
                pass


# ---------------------------------------------------------------- 页签样式
_TAB_QSS = """
QPushButton{border:none;border-radius:8px;font-size:11.5px;padding:0px 12px;
background:rgba(255,255,255,140);color:#4c5866;}
QPushButton:hover{background:rgba(255,255,255,215);}
QPushButton:checked{background:#2f7cf6;color:#ffffff;font-weight:600;}
"""


# ---------------------------------------------------------------- 后台小工具
def _toast_win(win, text, ms=5000, kind="info"):
    """给主窗口弹一条提示；窗口已经销毁就安静跳过（配置任务可能比弹窗活得久）。"""
    try:
        if win is not None:
            Toast.show_toast(win, text, ms=ms, kind=kind)
    except (RuntimeError, TypeError):
        pass


def _refresh_main(win):
    """配置完通知主界面刷一遍「已安装」状态（窗口没了就算了）。"""
    try:
        if win is not None and hasattr(win, "_refresh_installed"):
            win._refresh_installed()
    except (RuntimeError, TypeError, AttributeError):
        pass


def configure_job(entries, tid, game_dir=""):
    """把一批游戏配置进官方启动器（**工作线程**）。

    第七十轮两处关键：
      · `game_dir` 必须是**官方启动器那一份**（`<安装目录>\\official_assets\\.minecraft`），
        否则版本与档案写进应用自己的 `.minecraft`，官启在它的 gameDir 下根本看不到；
      · `select=False` —— 批量配置**不改玩家原本选中的启动项**，原有游戏一点不受影响。

    进度只写 `core.tasks` 里的任务数据，不碰任何控件 —— 玩家中途关掉弹窗，
    这个任务照样跑完，主页「下载任务」里能看到。
    """
    from ..core import tasks as _t
    T = _t.TASKS
    gd = game_dir or importer.official_game_dir()
    # 官方那份目录可能还没建过（玩家换过安装目录、或第一次就点配置）——
    # 不先建好，`launcher_profiles.json` 根本写不进去
    for sub in ("", "versions"):
        try:
            os.makedirs(os.path.join(gd, sub) if sub else gd, exist_ok=True)
        except OSError:
            pass
    n = len(entries or [])
    ok, bad = [], []
    T.set_items(tid, 0, n, phase="准备中")
    for i, e in enumerate(entries or []):
        if T.is_cancelled(tid):
            return {"ok": ok, "bad": bad, "n": n, "cancelled": True}
        name = str(e.get("name") or "")

        def tell(_text="", _i=i, _name=name):
            # 单条导入没有字节级进度，把「这一条正在干什么」写进阶段文字就够
            T.set_items(tid, _i, n, phase=f"配置「{_name}」")

        tell()
        try:
            good, why, extra = importer.import_one(e, game_dir=gd, progress=tell,
                                                   select=False)
        except Exception as ex:                          # noqa: BLE001
            good, why, extra = False, str(ex)[:80], {}
        if good:
            ok.append(name)
            # 只有真的配进去了才写「已配置」——「已配置」页签只认这个标记
            try:
                importer.mark_configured(e.get("path"), extra)
            except Exception:                            # noqa: BLE001
                pass
        else:
            bad.append((name, why))
        T.set_items(tid, i + 1, n, phase=f"配置「{name}」")
    return {"ok": ok, "bad": bad, "n": n, "cancelled": False}


class _RemoveAsk(_DialogBase):
    """批量 / 整组移除前的确认（第六十九轮）。

    措辞必须把「只是不在这儿的列表里显示」讲清楚 —— 玩家最怕的是点一下就
    把几十 G 的整合包删了。实际动作只有 `importer.forget_*`，一字节都不动。
    """

    def __init__(self, parent, count, folder=""):
        title = "移除分类文件夹" if folder else "从列表移除"
        super().__init__(parent, width=440, title=title)
        what = f"分类文件夹「{folder}」里的 {count} 个游戏" if folder else f"这 {count} 个游戏"
        tip = QLabel(f"把{what}从列表里移除？")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#1f2733;font-size:13px;")
        self.body.addWidget(tip)
        note = QLabel("只移除这里的显示，硬盘上的文件一个都不动；\n"
                      "已经配进官方启动器的，官方启动器那边照旧能玩。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a6a2a;font-size:11px;background:rgba(232,164,66,26);"
                           "border-radius:10px;padding:10px;")
        self.body.addWidget(note)
        self.add_buttons([("取消", "ghost", self.reject),
                          ("移除", "accent", self.accept)])


class _SwitchingVeil(QWidget):
    """页签切换时的过渡层：一块半透明白 + 「切换到「已配置」…」+ 三个跳动的点。

    为什么要它：游戏一多（60+ 行），重建列表是上百毫秒的全同步活。直接换的话
    点下去像「卡了一下」，玩家会以为点没上。先把这层亮出来，下一拍再真换 ——
    点下去立刻有反馈，列表换完这层就退场。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._text = "切换中"
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(130)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def show_over(self, text, w, h):
        self._text = text or "切换中"
        self.setGeometry(0, 0, max(1, int(w)), max(1, int(h)))
        self._phase = 0
        self.show()
        self.raise_()
        self._timer.start()
        self.update()

    def hide_over(self):
        try:
            self._timer.stop()
            self.hide()
        except RuntimeError:
            pass

    def _tick(self):
        self._phase = (self._phase + 1) % 4
        self.update()

    def paintEvent(self, e):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QFont, QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(247, 250, 253, 214))
        p.drawRoundedRect(self.rect(), 10, 10)
        text = self._text + "." * self._phase
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(text) + 34
        box = QRectF((self.width() - w) / 2.0, self.height() / 2.0 - 15, w, 30)
        p.setBrush(QColor(255, 255, 255, 245))
        p.drawRoundedRect(box, 15, 15)
        p.setPen(QColor("#2f7cf6"))
        p.drawText(box, Qt.AlignCenter, text)
        p.end()


class AddedGamesDialog(_DialogBase):
    """「已导入 / 已配置」列表：游戏管理里那个勾形按钮打开的（第六十六轮起唯一入口）。

    第六十九轮按玩家要求改成两个页签：
      · **已导入** = 还没进官方启动器的（能直接运行的 / 需要导入的）；
      · **已配置** = 已经进官方启动器的。
    两页都用同一套收放式文件夹分类，看起来一致。页签右边带条数。

    这个窗口还要干三件批量的事：全选 → 移除选中、整个分类移除、配置至官方启动器。
    所有「移除」都只删登记（`importer.forget_*`），**硬盘上的文件一个都不动**。
    """

    sel_enabled = True        # 让 _AddedRow / _FolderHead 挂勾选框（抽屉那边不挂）

    def __init__(self, parent):
        super().__init__(parent, width=640, title="已导入的游戏")
        self._added_rows = []
        self._open_ids = set()      # 哪些收放式文件夹是展开的（刷新后保持）
        self._entries = []          # 当前列表（两个页签共用一份原始数据）
        self._tab = "added"         # added=已导入 / done=已配置
        self._sel = set()           # 勾上的条目（key）
        self._rows = {}             # key -> _AddedRow
        self._folders = {}          # gid -> _AddedFolder
        self._cfg_busy = False
        self._cfg_tid = ""
        self._official_on = False
        self._btn_row = None
        self._tabs = {}
        self._tab_box = None        # 页签容器（只在官方目标下显示）
        self._tab_gen = 0           # 切页签的代数：连点两下时旧的过期作废

        tip = QLabel("点「运行」用原目录直接开局，一个字节都不复制；"
                     "「导入」是把不能直跑的那种（Prism 的实例）搬进官方启动器。\n"
                     "「添加已有文件夹」会先问你文件夹叫什么，确认后列表里多一个可展开的文件夹。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#63707f;font-size:11px;")
        self.body.addWidget(tip)

        self._build_tabs()

        self.lb_added = ElideLabel("正在读取…")
        self.lb_added.setStyleSheet("color:#4c5866;font-size:11px;font-weight:600;")
        self.body.addWidget(self.lb_added)

        # ---- 「配置至官方启动器」的进度条 ----
        # 要求：文字写「配置至官方启动器中… xx%」，文字**下方**一条进度条。
        self.cfg_bar = ProgressBar(self, height=8)
        self.cfg_bar.hide()
        self.body.addWidget(self.cfg_bar)

        self.scroll_added = QScrollArea()
        self.scroll_added.setWidgetResizable(True)
        self.scroll_added.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_added.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{width:8px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(90,102,117,90);"
            "border-radius:4px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        self.scroll_added.setFixedHeight(300)
        self.host_added = QWidget()
        self.host_added.setStyleSheet("background:transparent;")
        self.list_added = QVBoxLayout(self.host_added)
        self.list_added.setContentsMargins(0, 0, 4, 0)
        self.list_added.setSpacing(5)
        self.scroll_added.setWidget(self.host_added)
        self.body.addWidget(self.scroll_added)
        # 切页签的过渡层（盖在列表上，不吃鼠标事件）
        self.veil = _SwitchingVeil(self.scroll_added)
        self.veil.hide()

        row = self.add_buttons([("关闭", "accent", self.accept)])
        self._btn_row = row
        # 「添加已有文件夹」靠左单独放，别跟「关闭」挤在右边
        self.btn_add_manual = PillButton("添加已有文件夹", "ghost")
        self.btn_add_manual.setFixedHeight(36)
        self.btn_add_manual.setMinimumWidth(100)
        self.btn_add_manual.setToolTip("指到那个包/版本所在的文件夹，登记后可直接运行")
        self.btn_add_manual.clicked.connect(self._manual)
        row.insertWidget(0, self.btn_add_manual)

        # 「配置至官方启动器」紧跟其后 —— 把已经登记的游戏一次性全部配进官方启动器
        self.btn_to_official = PillButton("配置至官方启动器", "ghost")
        self.btn_to_official.setFixedHeight(36)
        self.btn_to_official.setMinimumWidth(130)
        self.btn_to_official.setToolTip(
            "把上面这些还没进官方启动器的游戏，一次性全部配进官方启动器")
        self.btn_to_official.clicked.connect(self._to_official)
        row.insertWidget(1, self.btn_to_official)

        # 全选 / 移除选中：位置跟着「配置至官方启动器」在不在走（见 _place_bulk）
        self.btn_all = PillButton("全选", "ghost")
        self.btn_all.setFixedHeight(36)
        self.btn_all.setMinimumWidth(64)
        self.btn_all.setToolTip("选中当前页里的全部游戏（包括分类文件夹里的）")
        self.btn_all.clicked.connect(self._toggle_all)
        self.btn_trim = PillButton("移除选中", "ghost")
        self.btn_trim.setFixedHeight(36)
        self.btn_trim.setMinimumWidth(80)
        self.btn_trim.setToolTip("把勾上的游戏从列表移除（硬盘上的文件不动）")
        self.btn_trim.clicked.connect(self._remove_selected)

        self._sync_official_btn()
        self._sync_bulk()
        QTimer.singleShot(20, self.reload_added)

    # ---------------- 页签 ----------------
    def _build_tabs(self):
        """标题右边那两颗页签（已导入 / 已配置）。

        **只在官方目标下才显示** —— 应用目标不写官启档案，分「已配置」纯属误导。
        仍然建出来是为了目标切换时能立刻亮出来，不切目标就一直藏着。
        """
        self._tab_box = None
        if self.head is None:
            return
        from PySide6.QtWidgets import QPushButton
        box = QWidget()
        box.setStyleSheet("background:transparent;")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        for key, text in (("added", "已导入"), ("done", "已配置")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(26)
            b.setMinimumWidth(78)
            b.setStyleSheet(_TAB_QSS)
            b.clicked.connect(lambda _=False, k=key: self._set_tab(k))
            bl.addWidget(b)
            self._tabs[key] = b
        self._tabs["added"].setChecked(True)
        self.head.addWidget(box, 0, Qt.AlignVCenter)
        self._tab_box = box
        self._sync_tabs()

    def _set_tab(self, key, instant=False):
        """切「已导入 / 已配置」。

        第七十一轮：不再同步重建列表（60+ 个游戏时会卡一下），改成
        **先亮「切换到…」的过渡层，下一拍再真换**。`instant=True` 时直接换
        （内部调用，比如目标从官方切到应用时强制回「已导入」）。
        """
        key = key if key in ("added", "done") else "added"
        for k, b in self._tabs.items():
            b.setChecked(k == self._tab)     # 可勾按钮点一下会自己取消，这里强制对齐
        if key == self._tab:
            return
        self._tab = key
        for k, b in self._tabs.items():
            b.setChecked(k == self._tab)
        if not self._entries:
            return
        tabs_on = self._tab_box is not None and not self._tab_box.isHidden()
        if instant or not tabs_on:
            self._render_added(self._entries)
            return
        self._tab_gen += 1
        gen = self._tab_gen
        name = "已配置" if key == "done" else "已导入"
        try:
            self.veil.show_over(f"切换到「{name}」", self.scroll_added.width(),
                                self.scroll_added.height())
        except RuntimeError:
            pass
        # 70ms：足够让过渡层先画上去，又不至于让玩家觉得慢
        QTimer.singleShot(70, lambda: self._apply_tab(gen))

    def _apply_tab(self, gen):
        """过渡层之后的真正换页（同一拍里连点两下时，旧的这次作废）。"""
        if gen != self._tab_gen:
            return
        try:
            self._render_added(self._entries)
        finally:
            try:
                self.veil.hide_over()
            except RuntimeError:
                pass

    def _is_done(self, e) -> bool:
        """是不是**我们点「配置至官方启动器」配过去的**。

        第七十轮改：以前用 `in_launcher or imported`，等于「只要官启 versions 目录里
        有同名版本就算」—— 那样官方启动器自己下载的游戏、以及还没配过的也会被算进去，
        而且一配完就从「已导入」里消失。现在只认 `configured` 这个标记。
        """
        return bool(e.get("configured"))

    def _sync_tabs(self) -> bool:
        """页签只在**官方**目标下显示（应用目标不写官启档案，分「已配置」没意义）。"""
        ok = self._is_official()
        if self._tab_box is not None:
            self._tab_box.setVisible(ok)
        if not ok:
            self._tab = "added"
            self._tab_gen += 1          # 作废还没执行的换页
            veil = getattr(self, "veil", None)     # _build_tabs 时它还没建
            if veil is not None:
                try:
                    veil.hide_over()
                except RuntimeError:
                    pass
        return ok

    # ---------------- 配置至官方启动器 ----------------
    def _is_official(self) -> bool:
        try:
            from ..core import paths
            return str(paths.target()) == "official"
        except Exception:                                # noqa: BLE001
            return False

    def _sync_official_btn(self):
        """按当前下载/安装目标显示或收起「配置至官方启动器」，并把全选摆到正确位置。

        应用路径下不写官启档案，这个按钮摆着只会误导 —— 直接不显示，
        全选就顶到「添加已有文件夹」右边（用户要求的就是这个顺序）。
        """
        ok = self._is_official()
        self._official_on = ok
        self.btn_to_official.setVisible(ok)
        self.btn_to_official.setEnabled(ok and not self._cfg_busy)
        self.btn_to_official.setToolTip(
            "把上面这些还没配进官方启动器的游戏，一次性全部配进去"
            if ok else "先切到「官方」再配置（应用路径不写官方启动器的档案）")
        self._sync_tabs()      # 页签只跟着官方目标出现
        self._place_bulk()

    def _place_bulk(self):
        row = self._btn_row
        if row is None:
            return
        for w in (self.btn_all, self.btn_trim):
            row.removeWidget(w)
        base = 2 if self._official_on else 1
        row.insertWidget(base, self.btn_all)
        row.insertWidget(base + 1, self.btn_trim)

    # ---------------- 勾选 ----------------
    def register_row(self, row):
        self._rows[row.key] = row
        row.set_checked(row.key in self._sel)

    def register_folder(self, folder):
        self._folders[folder.gid] = folder
        keys = folder.keys()
        folder.head.set_checked(bool(keys) and all(k in self._sel for k in keys))

    def row_checked(self, key, on):
        if on:
            self._sel.add(key)
        else:
            self._sel.discard(key)
        for f in self._folders.values():
            keys = f.keys()
            if keys:
                f.head.set_checked(all(k in self._sel for k in keys))
        self._sync_bulk()

    def group_checked(self, gid, on):
        f = self._folders.get(gid)
        if f is None:
            return
        keys = f.keys()
        if on:
            self._sel.update(keys)
        else:
            self._sel.difference_update(keys)
        f.set_all(on)
        self._sync_bulk()

    def _sync_bulk(self):
        total = len(self._rows)
        n = len(self._sel)
        self.btn_trim.setText(f"移除选中 ({n})" if n else "移除选中")
        self.btn_trim.setEnabled(n > 0)
        self.btn_all.setEnabled(total > 0)
        self.btn_all.setText("取消全选" if total and n >= total else "全选")

    def _toggle_all(self):
        total = len(self._rows)
        all_on = total > 0 and len(self._sel) >= total
        if all_on:
            self._sel.clear()
        else:
            self._sel = set(self._rows.keys())
        on = not all_on
        for r in self._rows.values():
            r.set_checked(on)
        for f in self._folders.values():
            f.head.set_checked(on and bool(f.keys()))
        self._sync_bulk()

    # ---------------- 移除 ----------------
    def _entries_by_keys(self, keys):
        out = []
        for e in self._entries:
            k = os.path.normcase(os.path.realpath(str(e.get("path") or "")))
            if k in keys:
                out.append(e)
        return out

    def _remove_selected(self):
        todo = self._entries_by_keys(set(self._sel))
        if not todo:
            return
        d = _RemoveAsk(self, len(todo))
        if not d.exec():
            return
        n = importer.forget_many([e.get("path") for e in todo])
        self._sel.clear()
        _toast_win(self.parent(),
                   f"已从列表移除 {n} 个（硬盘上的文件没动）", 3600, "info")
        self._after_remove()

    def remove_folder(self, gid):
        """整组移除（文件夹标题行上那个垃圾桶）—— 只删登记，硬盘不动。"""
        gid = str(gid or "")
        folder = self._folders.get(gid)
        name = ""
        for g in importer.groups():
            if g["id"] == gid:
                name = g.get("name") or ""
                break
        # 数「这一组到底有几个」必须看原始数据、不能只看当前页签里那几行：
        # 一个分类里可能有的已配进官启（在「已配置」页）、有的还没配（在「已导入」页），
        # 只按可见行数报，确认窗会说「1 个」实际却移掉 2 个。
        n = sum(1 for e in self._entries if str(e.get("group") or "") == gid)
        if not n and folder is not None:
            n = len(folder.rows)
        d = _RemoveAsk(self, n, folder=name or "未命名文件夹")
        if not d.exec():
            return
        got = importer.forget_group(gid)
        self._open_ids.discard(gid)
        _toast_win(self.parent(),
                   f"已移除分类「{name or '未命名文件夹'}」及其 {got} 个游戏"
                   "（硬盘上的文件没动）", 4200, "info")
        self._after_remove()

    def _after_remove(self):
        _refresh_main(self.parent())
        self.reload_added(force=True)

    # ---------------- 列表 ----------------
    def _clear_added(self):
        self._added_rows = []
        self._rows = {}
        self._folders = {}
        while self.list_added.count():
            it = self.list_added.takeAt(0)
            w = it.widget()
            if w is not None:
                # 只 deleteLater 是不够的：DeferredDelete 要等回到真正的事件循环才派发，
                # 切页签/刷新的那一帧里旧控件还在 host 上按老几何位置画着 ——
                # 看起来就是新旧两份列表叠在一起（文字互相压）。先摘下来才干净。
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def reload_added(self, game_dir=None, force=False):
        self._tab_gen += 1              # 重新读数据了，之前排队的换页作废
        veil = getattr(self, "veil", None)
        if veil is not None:
            try:
                veil.hide_over()
            except RuntimeError:
                pass
        # 第 106 轮：与「选择要启动的游戏 → 已导入」共用一份会话缓存，
        # 整个应用会话只读一次；登记真的变了的写操作一律传 force=True。
        key = str(game_dir or "") or gc.ADDED_KEY
        if not force:
            cached = gc.CACHE.get(gc.NS_ADDED, key)
            if cached is not None:
                self._render_added(cached)
                return
        self._clear_added()
        self.lb_added.setText("正在读取…")

        def done(entries):
            gc.CACHE.put(gc.NS_ADDED, list(entries or []), key)
            self._render_added(entries)

        def err(e):
            self.lb_added.setText(f"读取失败：{str(e)[:80]}")

        run_async(importer.added_entries, on_done=done, on_err=err)

    def _render_added(self, entries):
        self._clear_added()
        self._entries = list(entries or [])
        self._sel.clear()
        tabs = self._sync_tabs()          # 应用目标下没有页签，只有官方才有
        n_done = sum(1 for e in self._entries if self._is_done(e))
        n_add = len(self._entries) - n_done
        if self._tabs:
            self._tabs["added"].setText(f"已导入 {len(self._entries)}")
            self._tabs["done"].setText(f"已配置 {n_done}")
        # 「已导入」= **全部**登记过的（配过的也还留在这儿，不挪走）；
        # 「已配置」= 其中确实被我们配进官方启动器的那部分。应用目标下不分页签，全列。
        if tabs and self._tab == "done":
            view = [e for e in self._entries if self._is_done(e)]
        else:
            view = list(self._entries)
        nf = len({str(e.get("group") or "") for e in view if str(e.get("group") or "")})
        done_tab = bool(tabs) and self._tab == "done"
        if view:
            hint = ("；点文件夹右边的小三角展开" if nf else "")
            self.lb_added.setText(
                ("共 %d 个已配置到官方启动器" % len(view) if done_tab
                 else "共 %d 个游戏" % len(view))
                + (f" · {nf} 个文件夹" if nf else "") + hint)
        elif done_tab:
            self.lb_added.setText(
                "还没有配到官方启动器的游戏；在上面「配置至官方启动器」点一下，"
                "配好的会出现在这里（「已导入」那边照旧留着）")
        else:
            self.lb_added.setText(
                "还没有添加过。点下面「添加已有文件夹」，指到那个包或版本所在的目录即可")
        render_added_list(self.list_added, view, self, self._open_ids,
                          self._added_rows)
        self.list_added.addStretch(1)
        if hasattr(self, "btn_add_manual"):
            self.btn_add_manual.setEnabled(True)
        self._sync_official_btn()
        self._sync_bulk()

    # ---------------- 运行 / 导入（第七十三轮） ----------------
    def _win(self):
        """这个窗口背后真正的主窗。

        注意**不能用 `self.window()`** —— QDialog 自己就是一个顶层窗口，
        `window()` 返回的是对话框本身，那样 Toast 会挂在马上要关掉的窗口上，
        `refresh_launch_ui` / `on_official_launched` 也全都找不到。
        主窗是从 parent 链往上认（`AddedGamesDialog(win)` 里传的就是主窗）。
        """
        p = None
        try:
            p = self.parent()
        except RuntimeError:
            return None
        for _ in range(6):
            if p is None:
                break
            if hasattr(p, "refresh_launch_ui") or hasattr(p, "page_home"):
                return p
            try:
                p = p.parent()
            except RuntimeError:
                return None
        return p

    def run_added(self, entry):
        """「已导入」那一行点「运行」。

        **第七十三轮修的 bug**：这个方法以前只有启动抽屉里的替身 `_AddedHost`
        实现过，`AddedGamesDialog` 自己根本没有 —— 游戏管理 →「已导入」里点
        「运行」，Qt 回调里直接 AttributeError（窗口化打包后看不见报错），
        玩家的感受就是「点了没动静」。

        分工按当前启动方式走（用户原话：「官方的直接打开官方启动器，
        独立启动就直接独立启动」）：
          · 独立启动关着 → 打开官方启动器，那边选这个版本开局；
          · 独立启动开着 → 记下目标后直接开局，一个字节都不复制。
        """
        e = dict(entry or {})
        gd = str(e.get("game_dir") or "")
        vid = str(e.get("version_id") or "")
        name = e.get("name") or vid or "-"
        win = self._win() or self
        if not gd or not vid:
            Toast.show_toast(win,
                             "这个目录里没有能直接运行的版本 —— 先点「配置至官方启动器」",
                             ms=4600, kind="error")
            return
        # 记成当前启动目标：官方那条路（启动按钮 / 官启）也认这份记录
        gamelaunch.set_launch_target(e.get("kind") or "instance", vid,
                                     e.get("path") or "", name, 0, game_dir=gd)
        hook = getattr(win, "refresh_launch_ui", None)

        if not gamelaunch.standalone_enabled():
            if e.get("configured") or e.get("in_launcher"):
                Toast.show_toast(win, f"用官方启动器打开「{name}」…", ms=2000)
            else:
                Toast.show_toast(
                    win,
                    f"「{name}」还没配进官方启动器，官启的版本列表里可能看不到它 —— "
                    f"先回列表点「配置至官方启动器」", ms=6000, kind="info")
            if callable(hook):
                hook()
            from .widgets import launch_official_async
            launch_official_async(win)
            return

        gamelaunch.set_launch_mode("standalone")
        if callable(hook):
            hook()
        Toast.show_toast(win, f"正在直接启动「{name}」（不复制文件）…", ms=2200)

        def done(res):
            ok, msg = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
            Toast.show_toast(win, msg, ms=3600 if ok else 6200,
                             kind="success" if ok else "error")
            if not ok:
                return
            cb = getattr(win, "on_game_launched", None)
            if callable(cb):
                try:
                    cb(msg)
                except Exception:                    # noqa: BLE001
                    pass
            # 游戏已经起来了，别让这个模态窗挡在前面
            try:
                self.accept()
            except RuntimeError:
                pass

        def err(exc):
            Toast.show_toast(win, f"启动失败：{exc}", ms=6200, kind="error")

        run_async(gamelaunch.launch, on_done=done, on_err=err)

    def import_added(self, entry):
        """「已导入」里那条还不能直接跑的游戏 → 走一遍导入，把它搬进官启。

        同样补上缺失的实现（见 run_added 的说明）。
        """
        d = ImportGameDialog(self._win(), preset=[dict(entry, imported=False)])
        d.exec()
        self.reload_added(force=True)

    # ---------------- 动作 ----------------
    def _manual(self):
        """在「已导入」里也能直接加目录（不用先开导入窗口）"""
        d = pick_dir(self, "添加已有文件夹（选那个包或版本所在的目录）",
                     os.path.expandvars(r"%APPDATA%\.minecraft"))
        if not d:
            return
        # 先问分类文件夹的名字（默认 = 大文件夹名），勾确认、× 取消
        ask_folder_name(
            self, self.btn_add_manual, d,
            lambda name: self._manual_go(d, name),
            on_cancel=lambda: self.lb_added.setText("已取消导入（没有登记任何东西）"))

    def _manual_go(self, d, group_name):
        gid = importer.group_id_for(d)
        importer.remember_group(gid, group_name, d)
        self.lb_added.setText(f"正在识别这个文件夹（文件夹「{group_name}」）…")

        def done(entries):
            if not entries:
                self.lb_added.setText("这个文件夹里没认出游戏版本或整合包")
                Toast.show_toast(self.parent(),
                                 "这里没找到能接管过来的游戏（要选带 mods 的整合包目录，"
                                 "或 versions 里的某个版本目录）", ms=5200, kind="error")
                return
            importer.remember_added(entries, group=gid)
            names = "、".join(str(e.get("name") or "") for e in entries[:3])
            if importer.auto_import_enabled():
                self.lb_added.setText(
                    f"已登记 {len(entries)} 个（{names}），正在自动导入官方启动器…")
            else:
                self.lb_added.setText(
                    f"已登记 {len(entries)} 个（{names}）到文件夹「{group_name}」")
            self.reload_added(force=True)
            _refresh_main(self.parent())

        def err(e):
            self.lb_added.setText(f"识别失败：{str(e)[:80]}")

        run_async(importer.scan_path, d, on_done=done, on_err=err)

    # ---------------- 配置至官方启动器（后台任务）----------------
    def _to_official(self):
        """把「已导入」里还没配过的游戏逐条配进官方启动器。

        第六十九轮：这件事挂到全局任务中心（`core.tasks`），进度只写任务数据。
        所以**中途关掉这个窗口也不会中断**，主界面「下载任务」里照样有进度条。

        第七十轮：落的目录是官方启动器那一份
        `<安装目录>\\official_assets\\.minecraft\\versions`（以前错落到应用自己的
        `.minecraft` 里，官启看不见）；配好的游戏**同时留在「已导入」**，只是多出现在
        「已配置」里，并记一个 `configured` 标记。原有游戏一个都不动（`select=False`）。
        """
        if self._cfg_busy:
            return
        if not self._is_official():
            _toast_win(self, "先切到「官方」再配置（应用路径不写官方启动器的档案）",
                       4200, "info")
            return
        # 只配还没配过的（配过的再配一遍只会回一句「已在」，白占进度）
        todo = [e for e in (self._entries or []) if not self._is_done(e)]
        if not todo:
            _toast_win(self, "这里都已经配进官方启动器了，不用再配", 3800, "info")
            return
        from ..core import installer as _inst
        from ..core import tasks as _t
        try:
            if _inst.launcher_running():
                _toast_win(self, "官方启动器正在运行，先关掉它再配置"
                                 "（它会覆盖刚写进去的档案）", 5200, "error")
                return
        except Exception:                                # noqa: BLE001
            pass

        task = _t.TASKS.create(f"配置至官方启动器（{len(todo)} 个游戏）", kind="import")
        tid = task.tid
        _t.TASKS.set_items(tid, 0, len(todo), phase="准备中")
        self._cfg_tid = tid
        self._begin_cfg(len(todo))
        # 下面两个回调可能在这个弹窗关掉之后才跑，所以**只碰 win / tid / TASKS**，
        # 一个 self 都不能引用（Python 侧对象可能已经回收）。
        win = self.parent()

        def done(res):
            res = res or {}
            ok = res.get("ok") or []
            bad = res.get("bad") or []
            if res.get("cancelled"):
                _t.TASKS.finish(tid, "cancelled", "已取消")
            elif bad and not ok:
                _t.TASKS.finish(tid, "failed", f"{bad[0][0]}：{bad[0][1]}")
            elif bad:
                _t.TASKS.finish(tid, "done",
                                f"配好 {len(ok)} 个，{len(bad)} 个没成功")
            else:
                _t.TASKS.finish(tid, "done",
                                f"已把 {len(ok)} 个游戏配置进官方启动器")
            _refresh_main(win)
            if bad and not ok:
                _toast_win(win, f"配置失败：{bad[0][0]}（{bad[0][1]}）", 6000, "error")
            elif bad:
                _toast_win(win, f"配好 {len(ok)} 个，{len(bad)} 个没成功："
                                f"{bad[0][0]}（{bad[0][1]}）", 7000, "error")
            elif ok:
                _toast_win(win, f"已把 {len(ok)} 个游戏配置进官方启动器", 5000,
                           "success")

        def err(e):
            _t.TASKS.finish(tid, "failed", str(e)[:90])
            _refresh_main(win)
            _toast_win(win, f"配置中断：{str(e)[:90]}", 6000, "error")

        run_async(configure_job, todo, tid, importer.official_game_dir(),
                  on_done=done, on_err=err)

    def _begin_cfg(self, n: int):
        self._cfg_busy = True
        self.btn_to_official.setEnabled(False)
        self.btn_add_manual.setEnabled(False)
        self.cfg_bar.set_value(0.0)
        self.cfg_bar.show()
        if getattr(self, "_cfg_tick", None) is None:
            t = QTimer(self)
            t.setInterval(140)
            t.timeout.connect(self._paint_cfg)
            self._cfg_tick = t
        self._cfg_tick.start()
        self._paint_cfg()

    @staticmethod
    def _phase_name(task) -> str:
        ph = str(getattr(task, "phase", "") or "")
        if ph.startswith("配置「") and ph.endswith("」"):
            return ph[3:-1]
        return "" if ph in ("准备中", "已完成") else ph

    def _paint_cfg(self):
        from ..core import tasks as _t
        task = _t.TASKS.get(self._cfg_tid) if self._cfg_tid else None
        if task is None:
            self._end_cfg()
            return
        n = int(task.total_items or 0)
        done = int(task.done_items or 0)
        pct = (float(done) / n) if n else 0.0
        self.cfg_bar.set_value(pct)
        who = self._phase_name(task)
        if task.status == "running":
            self.lb_added.setText(
                f"配置至官方启动器中… {int(round(pct * 100))}%"
                + (f"（{who}）" if who else ""))
        else:
            self.lb_added.setText("配置至官方启动器… 100%")
            self._end_cfg()
            # 配完立刻重刷：「已配置」页签要马上多出刚配好的那些，
            # 「已导入」那边也还留着（只是按钮从「导入」变成没有）
            self.reload_added(force=True)

    def _end_cfg(self):
        self._cfg_busy = False
        if getattr(self, "_cfg_tick", None) is not None:
            try:
                self._cfg_tick.stop()
            except RuntimeError:
                pass
        try:
            self.cfg_bar.hide()
        except RuntimeError:
            pass
        try:
            self.btn_add_manual.setEnabled(True)
            self._sync_official_btn()
        except RuntimeError:
            pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        try:
            if self.veil.isVisible():
                self.veil.setGeometry(0, 0, self.scroll_added.width(),
                                      self.scroll_added.height())
        except (RuntimeError, AttributeError):
            pass

    def hideEvent(self, e):
        """中途关窗：任务不杀（它在后台任务中心里跑），提示去主页看进度。"""
        try:
            self.veil.hide_over()
        except (RuntimeError, AttributeError):
            pass
        try:
            super().hideEvent(e)
        except Exception:                                # noqa: BLE001
            pass
        if getattr(self, "_cfg_busy", False):
            running = True
            try:
                from ..core import tasks as _t
                t = _t.TASKS.get(self._cfg_tid)
                running = bool(t is not None and t.status == "running")
            except Exception:                            # noqa: BLE001
                running = True
            self._end_cfg()
            if running:
                _toast_win(self.parent(),
                           "配置仍在后台进行，进度看主页的「下载任务」", 4600, "info")
