# -*- coding: utf-8 -*-
"""内置文件 / 文件夹选择器（替代系统文件对话框）。

为什么自己画：
    Windows 的系统文件对话框是 shell 窗口 —— 样式跟软件不搭、会跑到窗口外面、
    还能被拖到任意位置。这里用软件自己的磨砂卡片重做一个：路径栏可手输、
    左侧常用位置、中间列表双击进入（选文件夹时单击即选中）、拖不出主窗口。

对外只有三个函数：
    pick_dir(parent, title, start)             -> 选中的目录（取消返回 ""）
    pick_file(parent, title, start, patterns)  -> 选中的文件（取消返回 ""）
    pick_view(parent, title, path, highlight)  -> 只浏览一个目录（无返回值）
"""
import os

from PySide6.QtCore import QDir, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QVBoxLayout)

from ..core import modfiles
from . import fileicons
from .dialogs import _DialogBase
from .widgets import PillButton

MAX_ROWS = 3000          # 目录里条目太多时不铺满，避免一次塞几万行
# 第六十七轮补丁：列表分帧建行。
# 一个 213 个模组的目录，把 213 个 QListWidgetItem 一次 addItem 进去要 736ms ——
# 玩家点「打开文件夹」看到的就是卡一下。首帧只建够铺满一屏的行（窗口立刻有内容），
# 剩下的每 8ms 补一批。间隔不能用 0：zero-timer 会自链，把重绘饿死。
# 实测（213 个模组）：40/60 → 首帧 28ms、最长 67ms；16/40 → 首帧 25ms、最长 28ms。
ROWS_FIRST = 16
ROWS_TICK = 40
ROWS_DELAY = 8


def _places():
    """常用位置（存在才列）"""
    home = os.path.expanduser("~")
    try:
        from ..core import winlnk
        desk = winlnk.desktop_dir() or os.path.join(home, "Desktop")
    except Exception:                                  # noqa: BLE001
        desk = os.path.join(home, "Desktop")
    cands = [("桌面", desk),
             ("下载", os.path.join(home, "Downloads")),
             ("文档", os.path.join(home, "Documents")),
             ("用户目录", home)]
    try:
        from ..core.config import CONFIG
        gd = str(CONFIG.get("game_dir") or "").strip()
        if gd:
            cands.append(("游戏目录", gd))
    except Exception:                                  # noqa: BLE001
        pass
    out = [(n, p) for n, p in cands if os.path.isdir(p)]
    for d in QDir.drives():
        root = d.absolutePath()
        out.append((root, root))
    return out


class FilePicker(_DialogBase):
    """文件 / 文件夹选择器。

    mode：
      · `"file"` 选文件（有文件名输入框，双击文件直接选定）
      · `"dir"`  选文件夹（点「选择」用当前打开的目录）
      · `"view"` 只看不选（第六十轮：保存完用它显示「文件确实进去了」，
                 可以高亮刚写进去的那一个，也可以一键转去系统文件夹）
    """

    def __init__(self, parent, title="选择", mode="file", start="",
                 patterns=None, highlight="", on_langpack=None):
        super().__init__(parent, width=800, title=title)
        self.mode = mode
        self.highlight = str(highlight or "")
        # 第七十九轮：只有「游戏目录」那个查看器才给这个回调 ——
        # 其它地方（下载完去看看 / 日志目录）摆一个「汉化补丁」是莫名其妙
        self.on_langpack = on_langpack
        # 第六十轮修：调用方常把 patterns 传成**字符串**（`"*"` / `"*.exe"`），
        # 直接迭代会把 "*" 拆成 ['*']，再经 `lstrip("*")` 变成空串 →
        # 每个文件都不匹配 → 「添加 java.exe 时文件夹里一个文件都看不见」。
        # 这里统一成列表，并丢掉空规则（空规则 = 不过滤）。
        if isinstance(patterns, str):
            patterns = [patterns]
        self.patterns = [p.strip().lower() for p in (patterns or [])
                         if p and p.strip()]
        self.picked = ""
        # 第九十八轮：选文件夹时「单击选中的那个子目录」。空 = 没点过任何子目录，
        # 这时「选择」按当前打开的目录算（老行为）。
        self.picked_dir = ""
        self.cwd = ""
        # 打开时定的那个根目录（玩家可能一路翻进 mods/，汉化补丁仍要铺在根上）
        self._root = ""

        body = QHBoxLayout()
        body.setSpacing(12)

        # ---------------- 左：常用位置 ----------------
        self.places = QListWidget()
        self.places.setFixedWidth(140)
        self.places.setFrameShape(QListWidget.NoFrame)
        self.places.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.places.setStyleSheet(
            "QListWidget{background:rgba(31,39,51,8);border:none;border-radius:10px;"
            "padding:4px;font-size:12px;color:#3a4757;}"
            "QListWidget::item{padding:7px 10px;border-radius:8px;}"
            "QListWidget::item:hover{background:rgba(31,39,51,16);}"
            "QListWidget::item:selected{background:rgba(47,124,246,36);color:#1f2733;}")
        for name, path in _places():
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, path)
            self.places.addItem(it)
        self.places.itemClicked.connect(
            lambda it: self.set_dir(it.data(Qt.UserRole)))
        body.addWidget(self.places)

        # ---------------- 右：路径栏 + 列表 ----------------
        right = QVBoxLayout()
        right.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        up = PillButton("上一级", "ghost")
        up.setFixedHeight(32)
        up.setMinimumWidth(74)
        up.clicked.connect(self._go_up)
        bar.addWidget(up)
        self.ed_path = QLineEdit()
        self.ed_path.setFixedHeight(32)
        self.ed_path.setPlaceholderText("可以直接输入/粘贴路径后回车")
        self.ed_path.returnPressed.connect(
            lambda: self.set_dir(self.ed_path.text().strip()))
        bar.addWidget(self.ed_path, 1)
        right.addLayout(bar)

        self.list = QListWidget()
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setStyleSheet(
            "QListWidget{background:rgba(255,255,255,150);border:none;border-radius:10px;"
            "padding:4px;font-size:12.5px;color:#1f2733;}"
            "QListWidget::item{padding:6px 8px;border-radius:8px;}"
            "QListWidget::item:hover{background:rgba(47,124,246,20);}"
            "QListWidget::item:selected{background:rgba(47,124,246,42);color:#152238;}")
        self.list.itemDoubleClicked.connect(self._on_double)
        self.list.itemClicked.connect(self._on_click)
        # 第六十四轮：右键模组 → 禁用 / 启用 / 删除（只在「查看」模式下给）
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        # 第六十五轮：jar / zip 里的图标是后台读的，读好了再换上去
        fileicons.bus().icons_ready.connect(self._fill_icons)
        right.addWidget(self.list, 1)

        self.lbl_name = QLabel("文件名")
        self.lbl_name.setStyleSheet("color:#1f2733;font-size:12px;font-weight:600;")
        self.ed_name = QLineEdit()
        self.ed_name.setFixedHeight(32)
        self.ed_name.setPlaceholderText("选中上面的文件，或直接输入文件名")
        self.ed_name.returnPressed.connect(self._accept)
        if self.mode == "file":
            right.addWidget(self.lbl_name)
            right.addWidget(self.ed_name)

        body.addLayout(right, 1)
        self.body.addLayout(body)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#7b8794;font-size:11px;")
        self.hint.setWordWrap(True)
        self.body.addWidget(self.hint)

        if self.mode == "view":
            # 只浏览：不给「选择」，改成「去系统文件夹看看」+（有的场景）+ 关闭
            # 第七十九轮：「游戏目录」这个查看器在「关闭」左侧多一个「汉化补丁」，
            # 直接进核对流程（不用先去下载页绕一圈）。别的查看器没有这个回调，
            # 也就不会摆这个按钮。
            specs = [("打开系统文件夹", "ghost", self._open_system)]
            if self.on_langpack:
                specs.append(("汉化补丁", "ghost", self._langpack))
            specs.append(("关闭", "accent", self.accept))
            self.add_buttons(specs)
            self.setFixedHeight(600)
        else:
            self.add_buttons([("取消", "ghost", self.reject),
                              ("选择", "accent", self._accept)])
            self.setFixedHeight(560)

        if not self.set_dir(start, defer=True):
            # 起点无效（目录被删 / 空串）→ 退到用户目录，再不行就用第一个磁盘
            home = os.path.expanduser("~")
            if not self.set_dir(home, defer=True):
                drives = QDir.drives()
                self.set_dir(drives[0].absolutePath() if drives else home, defer=True)
        self.places.setCurrentRow(-1)
        self.places.clearSelection()
        self.ed_path.setFocus()
        self.ed_path.deselect()                       # 别一进来就整段高亮
        self.ed_path.setCursorPosition(len(self.ed_path.text()))

    # ---------------- 目录遍历 ----------------
    def set_dir(self, path, defer: bool = False):
        """进到某个目录。

        `defer=True`（只给构造时用）：把「列目录 + 建行 + 给图标」推到事件循环的
        下一帧。一个 213 个模组的目录，这几步加起来 400ms，同步做就是「点了没反应」；
        推后的话窗口先弹出来（玩家看到了），列表随后填上。
        """
        path = (path or "").strip().strip('"')
        if path and os.path.isfile(path):
            if self.mode == "file":
                self.ed_name.setText(os.path.basename(path))
            elif self.mode == "view":
                # 传进来的是文件 → 进去它所在的目录，并把它高亮出来
                self.highlight = os.path.basename(path)
            path = os.path.dirname(path)
        if not path or not os.path.isdir(path):
            return False
        self.cwd = os.path.normpath(path)
        if not self._root:                 # 第一次成功进入的就是「根」，之后翻目录不改它
            self._root = self.cwd
        self.ed_path.setText(self.cwd)
        if defer:
            self.hint.setText("正在读取这个文件夹…")
            QTimer.singleShot(0, self._populate)
        else:
            self._populate()
        return True

    def _go_up(self):
        parent = os.path.dirname(self.cwd)
        if parent and parent != self.cwd:
            self.set_dir(parent)

    def _populate(self):
        """读目录 + 分帧建行（第六十七轮补丁：不再一口气建完）。

        以前这里是「scandir → 213 次 addItem → 排队图标」一条直线，实测这一帧
        736ms，就是玩家说的「打开文件夹卡那么一下」。现在只把**目录读出来**（几毫秒）、
        首帧建 40 行，剩下的交给 `_row_tick` 每 8ms 补 60 行 —— 窗口一打开就有内容，
        往下滚也不会看到空列表（8ms 一档，213 行 30ms 内补完）。

        `scandir` 顺手把 mtime 一起收下（Windows 上是目录枚举白送的信息）：建行算图标
        时直接把它传下去，`isfile` + `getmtime` 这两次 stat 全省 —— 这个目录上
        一次 stat 约 0.7ms，213 个文件就是 300ms。
        """
        self._pop_gen = getattr(self, "_pop_gen", 0) + 1
        gen = self._pop_gen
        # 换了目录 = 之前单击选中的那个子目录不再算数（否则路径栏显示 B、
        # 「选择」却给出 A，玩家没法判断到底装哪）
        self.picked_dir = ""
        self._pend = []
        self._row_of = {}
        self._row_i = 0
        self.list.clear()
        dirs, files = [], []                    # files: (name, mtime)
        try:
            with os.scandir(self.cwd) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            dirs.append(e.name)
                        elif (self.mode in ("file", "view")
                              and self._match(e.name)):
                            files.append((e.name, e.stat().st_mtime))
                    except OSError:
                        continue
        except OSError as exc:
            self.hint.setText("这个目录打不开：%s" % exc)
            return
        dirs.sort(key=str.lower)
        files.sort(key=lambda x: x[0].lower())
        rows = [("dir", n, 0.0) for n in dirs]
        rows += [("file", n, mt) for n, mt in files]
        trimmed = 0
        if len(rows) > MAX_ROWS:
            trimmed = len(rows) - MAX_ROWS
            rows = rows[:MAX_ROWS]
        self._pend = rows
        self._hint_for(len(rows), trimmed)        # 列表还在补，提示先说实话
        self._row_tick(gen, first=True)

    def _hint_for(self, shown, trimmed):
        """底部那行说明 —— 分帧建行时先算好，别等最后一行补上才显示"""
        tail = ("；另有 %d 项未列出，可用路径栏直接输入" % trimmed) if trimmed else ""
        if self.mode == "file":
            self.hint.setText("双击文件夹进入 · 双击文件直接选定 · 只显示 "
                              "%s%s" % ("、".join(self.patterns) or "所有文件", tail))
        elif self.mode == "view":
            self.hint.setText("双击文件夹进去看 · 双击文件用系统程序打开 · "
                              "右键模组可以禁用 / 删除" +
                              ("；另有 %d 项未列出" % trimmed if trimmed else ""))
        else:
            # 第九十八轮：`dir` 模式改成「单击子目录即选中」。
            # 选中了就把选的是谁写出来 —— 否则玩家点了一下不知道有没有生效。
            sel = getattr(self, "picked_dir", "")
            if sel:
                self.hint.setText("已选中「%s」· 点「选择」就用它 · 双击文件夹可以进去看"
                                  % os.path.basename(sel) + tail)
            else:
                self.hint.setText("单击文件夹即选中 · 双击文件夹进去看 · "
                                  "点「选择」用当前打开的目录" + tail)

    def _row_tick(self, gen, first=False):
        """补一批行 + 顺手把这几行交给后台读图标

        每行都带 mtime（目录枚举白拿的），所以算初始图标**零 stat、零磁盘**；
        真的包里图标要等后台读好 `icons_ready` 带回来的那几条，见 `_fill_icons`。
        """
        if gen != getattr(self, "_pop_gen", -1):
            return                                # 又进了别的目录，这批作废
        try:
            self.list.count()                     # 窗口销毁了就别再碰
        except RuntimeError:
            return
        pend = getattr(self, "_pend", [])
        take = ROWS_FIRST if first else ROWS_TICK
        end = min(self._row_i + take, len(pend))
        cwd = self.cwd
        batch = []
        self.list.setUpdatesEnabled(False)        # 这一批加完再统一排一次版
        try:
            for k in range(self._row_i, end):
                kind, name, mt = pend[k]
                if kind == "file":
                    full = os.path.join(cwd, name)
                    it = QListWidgetItem(
                        fileicons.icon_for_deferred(full, 18, mtime=mt), name)
                    batch.append((full, mt))
                    self._row_of[os.path.normcase(name)] = (it, mt)
                else:
                    it = QListWidgetItem(
                        fileicons.icon_for_deferred(name, 18, is_dir=True), name)
                it.setData(Qt.UserRole, kind)
                self.list.addItem(it)
        finally:
            self.list.setUpdatesEnabled(True)
        self._row_i = end
        if batch:
            fileicons.prewarm_pairs(batch)        # 只排队，读好的走 icons_ready
        if end < len(pend):
            QTimer.singleShot(ROWS_DELAY, lambda: self._row_tick(gen))
        else:
            self._finish_rows()

    def _finish_rows(self):
        """行齐了 —— 收尾那点小事（清选中 / 高亮刚写进去的文件）"""
        try:
            self.list.clearSelection()
            self.list.setCurrentRow(-1)   # 别让第一行一进来就带蓝框
            # 保存完「去看看」时把刚写进去的那个文件选中并滚到可见
            if self.highlight:
                self._select_name(self.highlight)
        except RuntimeError:
            pass

    def _prewarm_files(self, names):
        """排队读这批文件的图标（只排队，读好的走 icons_ready 回来换）"""
        try:
            cwd = self.cwd
        except RuntimeError:
            return
        fileicons.prewarm([os.path.join(cwd, n) for n in names])

    def _fill_icons(self, just=None):
        """后台读好了几个包的图标 —— **只换这几行**，不整表重刷。

        第六十七轮补丁：以前这里是「从头把 213 行全 setIcon 一遍」。后台每 60ms
        发一次 `icons_ready`，一整个读包过程（四五秒）里主线程就一直在重贴没变过的
        图标 —— 实测 4 秒里 2.1 秒干这个，单帧最长 460ms，玩家看到的就是「卡」。
        现在信号带回来「这一轮刚读到的是哪几个」，按文件名直接找到那几行换掉。
        """
        just = just or []
        try:
            self.list.count()
        except RuntimeError:
            return
        row_of = getattr(self, "_row_of", {})
        hit = []
        for path, mtime in just:
            got = row_of.get(os.path.normcase(os.path.basename(path)))
            if not got:
                continue                       # 不是当前这个目录里的（换过目录了）
            item, mt = got
            if mt and mtime and abs(mt - mtime) > 1e-6:
                continue                       # 同名但已经换过文件了
            hit.append((item, path, mtime))
        if not hit:
            return
        self.list.setUpdatesEnabled(False)
        try:
            for item, path, mtime in hit:
                try:
                    item.setIcon(fileicons.icon_for_deferred(path, 18, mtime=mtime))
                except RuntimeError:           # 行已经跟着列表一起销毁
                    return
        finally:
            try:
                self.list.setUpdatesEnabled(True)
            except RuntimeError:
                pass

    def _select_name(self, name):
        low = str(name or "").lower()
        if not low:
            return False
        for i in range(self.list.count()):
            it = self.list.item(i)
            if (it.data(Qt.UserRole) == "file"
                    and it.text().lower() == low):
                self.list.setCurrentItem(it)
                self.list.scrollToItem(it)
                return True
        return False

    def _match(self, name):
        """文件名是否该出现在列表里。

        规则很宽松，因为这里只用来「少显示点无关文件」，不该变成隐形过滤器：
          · 没规则 / 规则就是 `*`        → 全部通过
          · `*.exe`、`.exe`              → 按扩展名
          · `java.exe`（不带星号）        → 按全名精确匹配
        第六十轮之前只认「以点开头」的规则，`java.exe` 这种写法会让整个
        目录一个文件都不显示。
        """
        pats = [p for p in self.patterns if p and p != "*"]
        if not pats:
            return True
        # 规则里只要出现一个「全通过」(* / *.*)，就别再拿别的规则去排除 ——
        # 混着写 `["*", "*.exe"]` 时把 java.exe 过滤掉是最容易踩的坑。
        if any(p in ("*", "*.*") for p in self.patterns):
            return True
        low = name.lower()
        for p in pats:
            core = p.lstrip("*").strip()
            if not core:
                return True
            if low == core or low.endswith(core):
                return True
        return False

    # ---------------- 交互 ----------------
    def _on_click(self, it):
        kind = it.data(Qt.UserRole)
        if kind == "dir":
            # 第九十八轮：选文件夹时**单击就选中这个子目录**。
            # 以前 `dir` 模式单击毫无反应，想把游戏装进 D:\MC\实例 必须先双击进它、
            # 再点「选择」—— 玩家的感受就是「这个文件夹点不动」。现在单击记下它，
            # 双击仍然是「进去看看」（进去后按当前目录算）。
            if self.mode == "dir":
                full = os.path.join(self.cwd, it.text())
                if os.path.isdir(full):
                    self.picked_dir = os.path.normpath(full)
                    self._hint_for(self.list.count(), 0)
            return
        if kind == "file" and self.mode == "file":
            self.ed_name.setText(it.text())

    def _on_double(self, it):
        if it.data(Qt.UserRole) == "dir":
            self.set_dir(os.path.join(self.cwd, it.text()))
            return
        if self.mode == "view":
            self._open_file(it.text())
            return
        self.ed_name.setText(it.text())
        self._accept()

    def _open_file(self, name):
        """「查看」模式双击文件 → 交给系统里关联的程序打开"""
        full = os.path.join(self.cwd, name)
        try:
            os.startfile(full)                           # noqa: S606
        except Exception:                                # noqa: BLE001
            try:
                from .widgets import Toast
                Toast.show_toast(self, "打不开这个文件：%s" % name)
            except Exception:                            # noqa: BLE001
                pass

    def _open_system(self):
        """「查看」模式 → 转去 Windows 自己的资源管理器"""
        try:
            os.startfile(self.cwd or ".")                # noqa: S606
        except Exception as exc:                         # noqa: BLE001
            self.hint.setText("系统文件夹打不开：%s" % exc)

    def _langpack(self):
        """「汉化补丁」：对着这个窗口打开的**根目录**走汉化补丁核对流程。

        第七十九轮。用 `_root` 而不是 `cwd`：玩家很可能已经翻进 `mods/` 或
        `config/` 里看文件了，要是按当前目录铺，`config/kubejs/resources` 会
        整坨塞进 `mods/` —— 那比不装更糟。根目录在打开窗口时就定死了。
        """
        dest = self._root or self.cwd
        if not dest or not os.path.isdir(dest):
            self.hint.setText("这个目录打不开了，先刷新一下")
            return
        if not callable(self.on_langpack):
            return
        try:
            self.on_langpack(dest)
        except Exception as exc:                          # noqa: BLE001
            # Qt 回调里抛异常 = 静默失效（打包后没有 traceback），必须自己兜
            self.hint.setText("汉化补丁没能开始：%s" % exc)

    # ---------------- 右键：模组禁用 / 删除（第六十四轮）----------------
    def _on_menu(self, pos):
        if self.mode != "view":
            return
        it = self.list.itemAt(pos)
        if it is None or it.data(Qt.UserRole) != "file":
            return
        name = it.text()
        if not modfiles.is_mod(name):
            return                       # 不是模组就别给「禁用」这种误导入口
        full = os.path.join(self.cwd, name)
        from PySide6.QtWidgets import QMenu
        from .widgets import Toast

        m = QMenu(self)
        off = modfiles.is_disabled(name)
        a1 = m.addAction("启用这个模组" if off else "禁用这个模组（改名 .disabled）")
        a2 = m.addAction("删除（移到回收站）")
        act = m.exec(self.list.viewport().mapToGlobal(pos))
        if act is a1:
            ok, _new, msg = modfiles.set_disabled(full, not off)
            Toast.show_toast(self, msg, ms=3400,
                             kind="success" if ok else "error")
            if ok:
                self.highlight = ""
                self._populate()
        elif act is a2:
            try:
                sz = modfiles.human_size(os.path.getsize(full))
            except OSError:
                sz = ""
            from .modlist import _ConfirmFile
            if not _ConfirmFile(self, name, sz).exec():
                return
            ok, msg = modfiles.delete_to_trash(full)
            Toast.show_toast(self, msg, ms=3400,
                             kind="success" if ok else "error")
            if ok:
                self.highlight = ""
                self._populate()

    def _accept(self):
        if self.mode == "dir":
            # 单击选中过子目录就用它，否则用当前打开的目录（老行为）
            self.picked = self.picked_dir or self.cwd
            if not self.picked or not os.path.isdir(self.picked):
                self.picked = ""
                self.hint.setText("这个目录已经不存在了，换个文件夹再试")
                return
        else:
            name = self.ed_name.text().strip().strip('"')
            if not name:
                self.hint.setText("先选一个文件，或输入文件名")
                return
            full = name if os.path.isabs(name) else os.path.join(self.cwd, name)
            if not os.path.isfile(full):
                self.hint.setText("找不到这个文件：%s" % full)
                return
            self.picked = os.path.normpath(full)
        if self.picked:
            self.accept()


# ---------------------------------------------------------------- 对外接口
def use_builtin() -> bool:
    """是否用内置文件管理器（设置 → 常规 →「文件管理器」，默认开）。

    关掉之后所有选目录 / 选文件的地方一律走 Windows 自己的对话框 —— 有些玩家
    更习惯系统那种（能直接在地址栏粘贴 `%APPDATA%` 这类变量）。
    """
    try:
        from ..core.config import CONFIG
        return bool(CONFIG.get("builtin_fs", True))
    except Exception:                                  # noqa: BLE001
        return True


def _system_pick(parent, title, mode, start, patterns=None):
    """系统文件对话框（设置里关掉内置管理器时用）"""
    from PySide6.QtWidgets import QFileDialog
    start = start or ""
    if mode == "dir":
        p = QFileDialog.getExistingDirectory(parent, title, start)
        return os.path.normpath(p) if p else ""
    if patterns:
        flt = "指定文件 (%s);;所有文件 (*.*)" % " ".join(patterns)
    else:
        flt = "所有文件 (*.*)"
    p, _sel = QFileDialog.getOpenFileName(parent, title, start, flt)
    return os.path.normpath(p) if p else ""


def _run(parent, title, mode, start, patterns=None):
    if not use_builtin():
        return _system_pick(parent, title, mode, start, patterns)
    d = FilePicker(parent, title=title, mode=mode, start=start,
                   patterns=patterns)
    d.exec()
    return d.picked or ""


def pick_dir(parent, title="选择文件夹", start=""):
    return _run(parent, title, "dir", start)


def pick_file(parent, title="选择文件", start="", patterns=None):
    return _run(parent, title, "file", start, patterns)


def pick_view(parent, title="文件在这里", path="", highlight="",
              on_langpack=None):
    """只在软件里显示一个目录的内容（不选任何东西）。

    第六十轮：下载页 / 整合包保存完，用户点「去看看」时不再弹 Windows 资源
    管理器，而是用这个内置窗口把刚写进去的文件摆出来并高亮 —— 用户在软件
    内部就能确认「东西确实进去了」。
    设置 → 常规里关掉「文件管理器」后，退回系统资源管理器。

    `on_langpack`（第七十九轮）＝ 给了就在底部「关闭」左边多一个「汉化补丁」，
    点它把 `dest_dir`（就是这里打开的根目录）交给回调。**只有「游戏目录」传它。**
    传了它就一定用内置窗口：那个按钮只存在于内置窗口里，退回资源管理器等于
    功能凭空消失。
    """
    path = (path or "").strip().strip('"')
    if not use_builtin() and not on_langpack:
        target = path
        if target and os.path.isfile(target):
            target = os.path.dirname(target)
        if target and os.path.isdir(target):
            try:
                os.startfile(target)                     # noqa: S606
            except Exception:                            # noqa: BLE001
                pass
        return
    d = FilePicker(parent, title=title, mode="view", start=path,
                   highlight=highlight, on_langpack=on_langpack)
    d.exec()
