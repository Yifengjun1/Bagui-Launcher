# -*- coding: utf-8 -*-
"""汉化补丁核对窗：把汉化包里的文件摊开给玩家看，勾选后才覆盖进整合包目录。

为什么不能「下载完直接覆盖」
--------------------------
BBSMC 的汉化包是**整包覆盖型** —— 里面就是 `config/`、`kubejs/`、
`resourcepacks/` 这些要直接铺进游戏运行目录的东西（官方教程原话：
「将解压后的所有文件和文件夹复制到游戏目录中，选择替换原有文件」）。
一个 60MB 的汉化包里上千个文件，其中很多是玩家自己调过的配置（按键、光影
设置、任务进度）。直接盖下去 = 玩家半个档的设置没了。

所以这里做三件事：
1. 摊开：每个文件列出来，标清 **新增** 还是 **覆盖**；
2. 让选：默认全选，但可以单独取消（或启用「只看覆盖」逐条审）；
3. 再落地：确认之后只复制勾上的。

为什么是自绘列表而不是一堆 QCheckBox
----------------------------------
一个包上千个文件 = 上千个控件，光创建就要几百毫秒、每帧还要 polish 一遍
（第七十四轮为此把任务明细改成虚拟列表，首帧 777ms → 35ms）。这里同理：
只画视口里那十几行，勾选状态存在 `rows` 里，跟控件无关。
"""
import os
import shutil

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from . import theme
from .dialogs import _DialogBase
from .widgets import PillButton, Toast


def _human(n) -> str:
    try:
        n = int(n or 0)
    except Exception:                                  # noqa: BLE001
        return ""
    if n <= 0:
        return ""
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


def scan_rows(items, dest_dir: str) -> list:
    """把解包清单标上「会不会覆盖已有文件」。

    `items` = `bbsmc.unpack()` 的返回（`[{"path", "size"}]`）；
    返回的每一行多两个字段：`exists`（目标位置已经有这个文件）、`checked`。
    """
    out = []
    base = str(dest_dir or "")
    for it in items or []:
        rel = str((it or {}).get("path") or "").replace("\\", "/").lstrip("/")
        if not rel:
            continue
        dst = os.path.join(base, rel.replace("/", os.sep)) if base else ""
        out.append({
            "path": rel,
            "size": int((it or {}).get("size") or 0),
            "exists": bool(dst and os.path.isfile(dst)),
            "checked": True,
        })
    return out


# ================================================================ 自绘文件夹树
def _split_rel(rel) -> list:
    """`config/fancymenu/a.txt` → `["config", "fancymenu", "a.txt"]`"""
    return [s for s in str(rel or "").replace("\\", "/").split("/") if s]


class FileTree(QWidget):
    """收放式文件夹核对清单（纯自绘 + 虚拟化）。

    为什么是「文件夹树」而不是平铺几百行
    ----------------------------------
    一个汉化包六七十 MB、三百到上千个文件，平铺出来玩家根本核不完 —— 他要决定的
    其实是「这一整坨配置要不要盖」，那是**文件夹**粒度的判断。所以：

    * 默认只列**顶层文件夹**（`config/` `kubejs/` `resources/` …），每行后面直接
      写清「N 个文件 · 覆盖 M 个」；
    * 点文件夹行（点勾选框以外的地方）展开，逐级往下，到文件粒度才给「新增 / 覆盖」；
    * 勾选框在文件夹上是**三态**：全选 / 部分（一根横线）/ 未选，点一下全给或全撤；
    * 勾选状态永远存在**文件**上（文件夹只是它的投影），所以汇总、覆盖、落地三处
      只有一套真值，不会出现「文件夹打了勾但里面文件没勾」这种自相矛盾。

    性能照旧（第七十四轮的铁律）：节点树只建一次，画的时候只画视口那十几行。
    """

    ROW_H = 22                          # 行高：比纯文件列表高一点，行里有箭头
    INDENT = 14                         # 每深一级缩进
    changed = Signal()                  # 勾选变了 → 外面刷新汇总
    layout_changed = Signal()           # 展开/收起变了 → 外面重新算列表高度

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._files = []
        self._nodes = {}
        self._roots = []
        self._open = set()              # 展开过的文件夹（切「只看覆盖」时保留）
        self.rows = []                  # **可见**节点（决定高度 / 绘制范围）
        self._f = QFont(theme.FONT)
        self._f.setPixelSize(11)
        self._fb = QFont(theme.FONT)
        self._fb.setPixelSize(11)
        self._fb.setBold(True)
        self._fx = QFont(theme.FONT)    # 右侧统计用小一号
        self._fx.setPixelSize(10)
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.set_rows(rows)

    # ---------- 建树 ----------
    def set_rows(self, rows):
        self._build(rows)
        self._hover = -1
        self._refresh()

    def _build(self, rows):
        self._files = [r for r in (rows or []) if isinstance(r, dict)]
        self._nodes = {}
        self._roots = []
        for r in self._files:
            parts = _split_rel(r.get("path"))
            if not parts:
                continue
            parent = ""
            for i, seg in enumerate(parts[:-1]):
                p = f"{parent}/{seg}" if parent else seg
                node = self._nodes.get(p)
                if node is None:
                    node = {"kind": "dir", "path": p, "name": seg,
                            "parent": parent, "depth": i, "open": p in self._open,
                            "kids": []}
                    self._nodes[p] = node
                    if parent:
                        self._nodes[parent]["kids"].append(p)
                    else:
                        self._roots.append(p)
                parent = p
            fp = "/".join(parts)
            if fp in self._nodes:                  # 目录与文件同名（极少见）
                fp = fp + "\u200b"
            self._nodes[fp] = {"kind": "file", "path": fp, "name": parts[-1],
                               "parent": parent, "depth": len(parts) - 1,
                               "row": r}
            if parent:
                self._nodes[parent]["kids"].append(fp)
            else:
                self._roots.append(fp)
        self._sort_kids(self._roots)
        for n in self._nodes.values():
            if n["kind"] == "dir":
                self._sort_kids(n["kids"])

    def _sort_kids(self, paths):
        """文件夹在前、文件在后，各自按名字排（不区分大小写）"""
        def key(p):
            n = self._nodes.get(p) or {}
            return (0 if n.get("kind") == "dir" else 1, str(n.get("name") or "").lower())
        paths.sort(key=key)

    # ---------- 统计 ----------
    def _stat(self, path):
        """(这个文件夹里一共几个文件, 其中几个会覆盖)"""
        n = self._nodes.get(path) or {}
        if n.get("kind") == "file":
            return 1, (1 if (n.get("row") or {}).get("exists") else 0)
        if "cnt" in n:
            return n["cnt"], n["ovr"]
        cnt = ovr = 0
        for k in n.get("kids") or []:
            a, b = self._stat(k)
            cnt += a
            ovr += b
        n["cnt"], n["ovr"] = cnt, ovr
        return cnt, ovr

    def _desc_files(self, path) -> list:
        """这个节点下属的全部**文件行**（文件夹就是递归到底）"""
        n = self._nodes.get(path) or {}
        if n.get("kind") == "file":
            return [n["row"]]
        out = []
        for k in n.get("kids") or []:
            out.extend(self._desc_files(k))
        return out

    def _dir_state(self, path) -> int:
        """0 = 未选，1 = 部分，2 = 全选"""
        rows = self._desc_files(path)
        on = sum(1 for r in rows if r.get("checked"))
        if not on:
            return 0
        return 2 if on == len(rows) else 1

    # ---------- 对外 ----------
    def counts(self) -> tuple:
        """(文件总数, 已选文件数, 已选里会覆盖的)"""
        total = len(self._files)
        on = [r for r in self._files if r.get("checked")]
        return total, len(on), sum(1 for r in on if r.get("exists"))

    def checked_rows(self) -> list:
        return [r for r in self._files if r.get("checked")]

    def set_all(self, on: bool):
        for r in self._files:
            r["checked"] = bool(on)
        self.update()
        self.changed.emit()

    def expand_all(self):
        for n in self._nodes.values():
            if n["kind"] == "dir":
                n["open"] = True
                self._open.add(n["path"])
        self._refresh()

    def open_paths(self) -> set:
        """当前展开了哪些文件夹（切「只看覆盖」前后要能还原）"""
        return set(self._open)

    def set_open(self, paths):
        self._open = set(paths or ())
        for n in self._nodes.values():
            if n["kind"] == "dir":
                n["open"] = n["path"] in self._open
        self._refresh()

    def collapse_all(self):
        for n in self._nodes.values():
            if n["kind"] == "dir":
                n["open"] = False
        self._open.clear()
        self._refresh()

    def dir_count(self) -> int:
        return sum(1 for n in self._nodes.values() if n["kind"] == "dir")

    # ---------- 可见行 ----------
    def _flatten(self) -> list:
        out = []
        stack = list(reversed(self._roots))
        while stack:
            p = stack.pop()
            n = self._nodes.get(p)
            if n is None:
                continue
            out.append(n)
            if n.get("kind") == "dir" and n.get("open"):
                stack.extend(reversed(n.get("kids") or []))
        return out

    def _refresh(self):
        self.rows = self._flatten()
        self.setFixedHeight(max(self.ROW_H, len(self.rows) * self.ROW_H))
        self.updateGeometry()       # 高度变了要让滚动区重新算滚动范围
        self.update()
        self.layout_changed.emit()

    # ---------- 尺寸 ----------
    # **这两个必须存在**（第七十七轮补丁）：`QScrollArea.setWidget()` 是按
    # `sizeHint()` 给子控件定尺寸的，而裸 `QWidget` 的 sizeHint 是无效值
    # `(-1, -1)` → 宽度直接落成 **0**，于是「数据全在、画面一片空白」。
    def sizeHint(self):
        return QSize(480, max(self.ROW_H, len(self.rows) * self.ROW_H))

    def minimumSizeHint(self):
        return QSize(160, self.ROW_H)

    # ---------- 交互 ----------
    def _index_at(self, y) -> int:
        i = int(y) // self.ROW_H
        return i if 0 <= i < len(self.rows) else -1

    def _check_x(self, node) -> int:
        return 6 + int(node.get("depth") or 0) * self.INDENT

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        i = self._index_at(e.position().y())
        if i < 0:
            return
        node = self.rows[i]
        x0 = self._check_x(node)
        # 点在勾选框那一块（含右侧一点余量）→ 切换勾选；否则文件夹展开/收起
        if e.position().x() <= x0 + 20:
            self._set_checked(node["path"],
                              not self._is_on(node))
        elif node["kind"] == "dir":
            node["open"] = not node["open"]
            if node["open"]:
                self._open.add(node["path"])
            else:
                self._open.discard(node["path"])
            self._refresh()
            return
        else:
            self._set_checked(node["path"], not self._is_on(node))
        self.update()
        self.changed.emit()

    def _is_on(self, node) -> bool:
        if node["kind"] == "file":
            return bool((node.get("row") or {}).get("checked"))
        return self._dir_state(node["path"]) == 2

    def _set_checked(self, path, on):
        for r in self._desc_files(path):
            r["checked"] = bool(on)

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().y())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, e):
        if self._hover != -1:
            self._hover = -1
            self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = ev.rect()
        i0 = max(0, r.top() // self.ROW_H)
        i1 = min(len(self.rows) - 1, r.bottom() // self.ROW_H)
        w = self.width()
        for i in range(i0, i1 + 1):
            self._paint_row(p, i, w)
        p.end()

    def _paint_row(self, p: QPainter, i: int, w: int):
        node = self.rows[i]
        y = i * self.ROW_H
        is_dir = node["kind"] == "dir"
        x0 = self._check_x(node)
        if i == self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(47, 124, 246, 18))
            p.drawRoundedRect(QRectF(2, y + 1, w - 4, self.ROW_H - 2), 6, 6)

        # 勾选框（文件夹是三态）
        bs = 13
        by = y + (self.ROW_H - bs) // 2
        state = self._dir_state(node["path"]) if is_dir else (
            2 if (node.get("row") or {}).get("checked") else 0)
        blue = QColor(47, 124, 246)
        p.setPen(QPen(blue if state else QColor(160, 172, 186), 1.4))
        p.setBrush(blue if state == 2 else Qt.NoBrush)
        p.drawRoundedRect(QRectF(x0, by, bs, bs), 4, 4)
        if state == 2:
            p.setPen(QPen(QColor("#ffffff"), 1.8))
            p.drawLine(int(x0 + 3), int(by + 7), int(x0 + 5), int(by + 9))
            p.drawLine(int(x0 + 5), int(by + 9), int(x0 + 10), int(by + 4))
        elif state == 1:
            p.setPen(QPen(blue, 1.8))
            p.drawLine(int(x0 + 3), int(by + 6), int(x0 + 10), int(by + 6))

        # 展开箭头（文件夹才有；文件也留出这一段宽度，好让名字对齐）
        ax = x0 + bs + 6
        cx, cy = ax + 4, y + self.ROW_H // 2
        if is_dir:
            p.setPen(QPen(QColor(120, 134, 150), 1.6))
            if node.get("open"):
                p.drawLine(cx - 3, cy - 1, cx, cy + 2)
                p.drawLine(cx, cy + 2, cx + 3, cy - 1)
            else:
                p.drawLine(cx - 1, cy - 3, cx + 2, cy)
                p.drawLine(cx + 2, cy, cx - 1, cy + 3)

        name_x = ax + 14
        if is_dir:
            cnt, ovr = self._stat(node["path"])
            right = f"{cnt} 个文件" + (f" · 覆盖 {ovr}" if ovr else "")
            rw = QFontMetrics(self._fx).horizontalAdvance(right) + 4
            p.setFont(self._fx)
            p.setPen(QColor("#b06a1e") if ovr else QColor("#8a95a3"))
            p.drawText(QRectF(w - rw - 10, y, rw, self.ROW_H),
                       Qt.AlignVCenter | Qt.AlignRight, right)
            avail = max(20, w - name_x - rw - 26)
            on = state > 0
            p.setFont(self._fb)
            p.setPen(QColor("#2a3442") if on else QColor("#6b7785"))
            p.drawText(QRectF(name_x, y, avail, self.ROW_H),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(self._fb).elidedText(
                           node["name"] + "/", Qt.ElideMiddle, avail))
            return

        # 文件行：右边「新增 / 覆盖」标签
        row = node.get("row") or {}
        exists = bool(row.get("exists"))
        on = bool(row.get("checked"))
        tag = "覆盖" if exists else "新增"
        p.setFont(self._fx)
        fmx = QFontMetrics(self._fx)
        tw = fmx.horizontalAdvance(tag) + 10
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(196, 108, 32, 30) if exists else QColor(60, 150, 110, 26))
        p.drawRoundedRect(QRectF(w - tw - 10, y + 4, tw, self.ROW_H - 8), 6, 6)
        p.setPen(QColor("#b06a1e") if exists else QColor("#3a8f6c"))
        p.drawText(QRectF(w - tw - 10, y, tw, self.ROW_H), Qt.AlignCenter, tag)

        avail = max(20, w - name_x - tw - 26)
        p.setFont(self._f)
        p.setPen(QColor("#2a3442") if on else QColor("#8a95a3"))
        p.drawText(QRectF(name_x, y, avail, self.ROW_H),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(self._f).elidedText(
                       str(node.get("name") or ""), Qt.ElideMiddle, avail))


# 老名字（第七十六～七十八轮一直叫 FileList）：现在它是一棵**文件夹树**。
# 留着别名是因为 `_probe_r76_ui.py` / `_probe_r77_ui.py` / `_chk_r69.py` 都按
# 这个名字查符号，改名不该把老探针一起判死。
FileList = FileTree

# ================================================================ 核对窗
class LangFetchFailedDialog(_DialogBase):
    """汉化包下不回来时给两条活路：浏览器下载、或用已下好的本地文件。

    第七十六轮补丁（1.5.1）：`cdn.bbsmc.net` 在国内经常**整条 IP 不可达**
    （实测 202.189.5.156 的 443/80 都是纯 TCP 超时，换三个公共 DNS 也还是这一个
    IP）。也就是说这不是「我们代码写错了」，是那条线路到不了。死磕重试没有意义，
    能做的是把玩家引导到能走通的那条路 —— 浏览器（走系统代理 / 加速器）下载，
    再把 zip 交回启动器。这个窗就是那个岔路口。

    `choice` 三态：`""`（取消）/ `"page"`（打开网页）/ `"file"`（选本地文件）
    """

    def __init__(self, parent, pack_name, reason, page_url=""):
        super().__init__(parent, width=560, title="汉化包没下下来")
        self.choice = ""
        self.page_url = str(page_url or "")

        head = QLabel(f"「{pack_name}」的汉化包下载失败")
        f = QFont(theme.FONT)
        f.setPixelSize(12.5)
        head.setFont(f)
        head.setWordWrap(True)
        self.body.addWidget(head)

        why = QLabel(str(reason or "未知原因")[:300])
        why.setWordWrap(True)
        why.setStyleSheet(
            "QLabel{background:rgba(179,55,43,20);color:#a33a2c;font-size:11px;"
            "border-radius:8px;padding:8px 10px;}")
        why.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(why)

        tip = QLabel(
            "BBSMC 的下载节点（cdn.bbsmc.net）在国内经常连不上，这不是你的网络坏了。\n"
            "两个办法，任选一个：\n"
            "· 点「打开汉化页」→ 用浏览器下载那个 zip（浏览器会走你的代理/加速器）；\n"
            "· 下好之后点「选择本地文件」→ 选中那个 zip，启动器接着帮你核对并覆盖。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "QLabel{background:rgba(47,124,246,14);color:#2a3442;font-size:11.5px;"
            "border-radius:8px;padding:10px 12px;}")
        self.body.addWidget(tip)

        brow = QHBoxLayout()
        brow.setSpacing(10)
        brow.addStretch(1)
        b_cancel = PillButton("取消", "ghost")
        b_cancel.setFixedHeight(36)
        b_cancel.setMinimumWidth(92)
        b_cancel.clicked.connect(self.reject)
        brow.addWidget(b_cancel)
        if self.page_url:
            b_page = PillButton("打开汉化页", "ghost")
            b_page.setFixedHeight(36)
            b_page.setMinimumWidth(104)
            b_page.clicked.connect(self._page)
            brow.addWidget(b_page)
        b_file = PillButton("选择本地文件", "accent")
        b_file.setFixedHeight(36)
        b_file.setMinimumWidth(104)
        b_file.clicked.connect(self._file)
        brow.addWidget(b_file)
        self.body.addLayout(brow)

    def _page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        self.choice = "page"
        self.accept()
        if self.page_url:
            QDesktopServices.openUrl(QUrl(self.page_url))

    def _file(self):
        self.choice = "file"
        self.accept()


# ================================================================ 复制进度
class _CopyBar(QWidget):
    """「确认并覆盖」按下去之后的复制进度：胶囊进度条 + 百分比。

    摆在「取消」左边（第八十轮）：那个位置正好是玩家确认前目光停留的地方，
    复制上千个文件要好几秒，没反馈就跟卡死一样。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(152, 20)
        self._p = 0.0
        self._text = ""
        self._font = QFont(theme.FONT)
        self._font.setPixelSize(11)
        self.hide()

    def reset(self):
        self._p = 0.0
        self._text = "0%"
        self.update()

    def set_progress(self, done, total):
        p = 0.0 if not total else max(0.0, min(1.0, float(done) / float(total)))
        self._p = p
        self._text = f"{int(round(p * 100))}%"
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bar_w = 104
        h = 8
        y = (self.height() - h) / 2.0
        track = QRectF(0.5, y, bar_w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(31, 39, 51, 32))
        p.drawRoundedRect(track, h / 2, h / 2)
        if self._p > 0:
            fill = QRectF(track)
            fill.setWidth(max(h, track.width() * self._p))
            p.setBrush(QColor(47, 124, 246))
            p.drawRoundedRect(fill, h / 2, h / 2)
        p.setFont(self._font)
        p.setPen(QColor(90, 102, 117))
        p.drawText(QRectF(bar_w + 8, 0, self.width() - bar_w - 8, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)


# ================================================================ 核对窗
class LangPackDialog(_DialogBase):
    """汉化补丁核对 + 落地。

    参数
    ----
    pack_name  整合包显示名（标题用）
    pack_ver   这次汉化对应的整合包版本（写清「按哪个版本挑的」）
    lang_ver   `bbsmc.language_versions()` 里选中的那一行
    rows       `scan_rows()` 的结果（已标好 exists/checked）
    src_dir    汉化包解压出来的目录（确认后从这里复制）
    dest_dir   整合包实例目录（复制到这里）
    page_url   汉化项目的网页地址（下载被拦时让玩家自己去下）
    """

    def __init__(self, parent, pack_name, pack_ver, lang_ver, rows,
                 src_dir, dest_dir, page_url=""):
        super().__init__(parent, width=620, title="汉化补丁核对")
        self.pack_name = str(pack_name or "整合包")
        self.pack_ver = str(pack_ver or "")
        self.lang_ver = lang_ver or {}
        self.src_dir = str(src_dir or "")
        self.dest_dir = str(dest_dir or "")
        self.page_url = str(page_url or "")
        self.ok = False
        self._all_rows = list(rows or [])
        self._only_over = False

        # ---- 说明 ----
        vname = str(self.lang_ver.get("name") or "").strip() or "汉化补丁"
        size = _human(self.lang_ver.get("file_size"))
        head = f"「{self.pack_name}」" + (f"　{self.pack_ver}" if self.pack_ver else "")
        lb1 = QLabel(head)
        f = QFont(theme.FONT)
        f.setPixelSize(12)
        lb1.setFont(f)
        self.body.addWidget(lb1)

        lb2 = QLabel(f"汉化包：{vname}" + (f"　·　{size}" if size else ""))
        lb2.setStyleSheet("color:#5a6675;font-size:11.5px;")
        lb2.setWordWrap(True)
        self.body.addWidget(lb2)

        dest = QLabel(f"将复制到：{self.dest_dir}")
        dest.setStyleSheet("color:#5a6675;font-size:11px;")
        dest.setWordWrap(True)
        dest.setToolTip(self.dest_dir)
        self.body.addWidget(dest)

        tip = QLabel("下面是汉化包里的文件夹。**点文件夹可以展开看里面的子文件**；"
                     "文件夹后面写着「N 个文件 · 覆盖 M 个」，勾选框在文件夹上是"
                     "「全给 / 部分 / 全撤」三态。标「覆盖」的文件会替换掉你整合包里"
                     "现有的同名文件（自己改过的配置建议先取消勾选）。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "QLabel{background:rgba(47,124,246,16);color:#2a3442;font-size:11px;"
            "border-radius:8px;padding:8px 10px;}")
        self.body.addWidget(tip)

        # ---- 汇总 + 操作条 ----
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._sum = QLabel("")
        self._sum.setStyleSheet("color:#2a3442;font-size:11.5px;")
        bar.addWidget(self._sum)
        bar.addStretch(1)
        self._btn_all = PillButton("全选", "ghost")
        self._btn_all.setFixedHeight(26)
        self._btn_all.clicked.connect(lambda: self._list.set_all(True))
        bar.addWidget(self._btn_all)
        self._btn_none = PillButton("全不选", "ghost")
        self._btn_none.setFixedHeight(26)
        self._btn_none.clicked.connect(lambda: self._list.set_all(False))
        bar.addWidget(self._btn_none)
        self._btn_over = PillButton("只看覆盖", "ghost")
        self._btn_over.setFixedHeight(26)
        self._btn_over.setCheckable(True)
        self._btn_over.clicked.connect(self._toggle_over)
        bar.addWidget(self._btn_over)
        self.body.addLayout(bar)

        # ---- 文件 / 文件夹树 ----
        self._list = FileTree(self._all_rows)
        self._list.changed.connect(self._sync_sum)
        self._list.layout_changed.connect(self._fit_list)
        self._snap_open = None          # 进「只看覆盖」前的展开状态，退出时还原
        scroll = QScrollArea()
        # `setWidgetResizable(True)`：宽度跟着视口走（否则子控件只有 sizeHint 那么宽，
        # 右边留一条灰带）；纵向由 `FileTree.setFixedHeight` 撑着，滚轮照常生效。
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;}"
            "QScrollBar:vertical{width:6px;background:transparent;margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(120,132,148,90);"
            "border-radius:3px;min-height:28px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        scroll.setWidget(self._list)
        # 高度跟着「当前可见行数」走，夹在 96~320：默认只看几个文件夹时窗口是矮的，
        # 展开到文件粒度才长高；超过 320 就往里滚（再高就把按钮挤到屏幕外了）。
        scroll.setFixedHeight(
            max(96, min(320, len(self._list.rows) * self._list.ROW_H + 8)))
        self.body.addWidget(scroll)
        self._scroll = scroll

        # ---- 状态 + 按钮 ----
        self._status = QLabel("")
        self._status.setStyleSheet("color:#8a95a3;font-size:11px;")
        self.body.addWidget(self._status)

        brow = QHBoxLayout()
        brow.setSpacing(10)
        if self.page_url:
            b_page = PillButton("打开汉化页", "ghost")
            b_page.setFixedHeight(36)
            b_page.setToolTip("下载被拦时，用浏览器在这里手动下")
            b_page.clicked.connect(self._open_page)
            brow.addWidget(b_page)
        brow.addStretch(1)
        # 复制进度条（第八十轮）：只在按下「确认并覆盖」之后出现，摆在取消左边
        self._bar = _CopyBar()
        brow.addWidget(self._bar)
        b_cancel = PillButton("取消", "ghost")
        b_cancel.setFixedHeight(36)
        b_cancel.setMinimumWidth(92)
        b_cancel.clicked.connect(self.reject)
        brow.addWidget(b_cancel)
        self._btn_cancel = b_cancel
        self._btn_ok = PillButton("确认并覆盖", "accent")
        self._btn_ok.setFixedHeight(36)
        self._btn_ok.setMinimumWidth(92)
        self._btn_ok.clicked.connect(self._apply)
        brow.addWidget(self._btn_ok)
        self.body.addLayout(brow)

        self._sync_sum()

    # ---------- 尺寸 / 汇总 ----------
    def _fit_list(self):
        """可见行数变了 → 重算列表高度，并让对话框跟着长高 / 缩回。

        不这样做的话：默认只列三四个文件夹，列表区却固定 200 多像素，又变成
        一大片空白 —— 玩家第一眼就会觉得「没东西给我核对」（第七十八轮那句
        「没有核对的文件夹呀」就是这么来的）。
        """
        sc = getattr(self, "_scroll", None)
        if sc is None:
            return
        h = max(96, min(320, len(self._list.rows) * self._list.ROW_H + 8))
        if h == sc.height():
            return
        sc.setFixedHeight(h)
        if self.isVisible():
            self.adjustSize()

    def _sync_sum(self):
        total, on, over = self._list.counts()
        new = on - over
        self._sum.setText(
            f"共 {total} 个文件　·　已选 {on} 个（新增 {new} · 覆盖 {over}）")
        self._btn_ok.setEnabled(on > 0)
        self._btn_ok.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)
        self._btn_ok.setToolTip("" if on else "一个文件都没勾选")

    def _toggle_over(self):
        """切「只看覆盖」：只留下会覆盖已有文件的那几行，**并自动展开**到文件粒度。

        为什么要自动展开：这个开关的目的就是「逐条审这几个要盖掉的」，如果还停在
        文件夹层面，等于什么都没看到。退出时还原进开关之前的展开状态。
        """
        self._only_over = bool(self._btn_over.isChecked())
        if self._only_over:
            rows = [r for r in self._all_rows if r.get("exists")]
            if not rows:
                self._only_over = False
                self._btn_over.setChecked(False)
                self._status.setText("这个汉化包里没有会覆盖的文件（全是新增）")
                return
            self._snap_open = self._list.open_paths()
            self._status.setText(
                f"只显示会覆盖现有文件的 {len(rows)} 个（取消勾选 = 保留你自己的那份）")
            self._list.set_rows(rows)
            self._list.expand_all()
        else:
            self._status.setText("")
            self._list.set_rows(self._all_rows)
            if self._snap_open is not None:
                self._list.set_open(self._snap_open)
                self._snap_open = None
        self._sync_sum()

    def _open_page(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if self.page_url:
            QDesktopServices.openUrl(QUrl(self.page_url))

    # ---------- 落地 ----------
    def _apply(self):
        todo = self._list.checked_rows()
        if not todo:
            Toast.show_toast(self, "一个文件都没勾选", ms=3000, kind="error")
            return
        if not self.src_dir or not os.path.isdir(self.src_dir):
            Toast.show_toast(self, "汉化包解压目录不见了，请重新下载", ms=3500,
                             kind="error")
            return
        if not self.dest_dir or not os.path.isdir(self.dest_dir):
            Toast.show_toast(self, "整合包目录不见了，请先安装整合包", ms=3500,
                             kind="error")
            return

        n_ok, failed = 0, []
        last_flush = 0.0
        import time as _t
        total = len(todo)
        # 复制期间锁住两个按钮：中途关窗会留下写了一半的文件，「确认并覆盖」再点
        # 一次则会两个循环同时往同一批目标写。进度条顶上来给反馈。
        self._btn_ok.setEnabled(False)
        self._btn_cancel.setEnabled(False)
        self._bar.reset()
        self._bar.show()
        self._status.setText(f"正在复制 0/{total} …")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for i, r in enumerate(todo, 1):
                rel = str(r.get("path") or "")
                src = os.path.join(self.src_dir, rel.replace("/", os.sep))
                dst = os.path.join(self.dest_dir, rel.replace("/", os.sep))
                try:
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    shutil.copy2(src, dst)
                    n_ok += 1
                except Exception:                      # noqa: BLE001
                    failed.append(rel)
                # 每 60ms（或最后一个）刷一次：按个数刷的话，遇到几个大贴图会一顿一顿的
                now = _t.time()
                if i == total or (now - last_flush) >= 0.06:
                    self._bar.set_progress(i, total)
                    self._status.setText(f"正在复制 {i}/{total} …")
                    QApplication.processEvents()
                    last_flush = now
        finally:
            QApplication.restoreOverrideCursor()
            self._bar.hide()
            self._btn_cancel.setEnabled(True)
            self._btn_ok.setEnabled(True)

        if failed:
            self._status.setText(
                f"完成：写入 {n_ok} 个，失败 {len(failed)} 个（{failed[0]} …）")
            # 失败不关窗：玩家能看清是哪几个，自己决定要不要再来一次
            self._sync_sum()
            return
        self.ok = True
        self.accept()

    def done(self, r):
        super().done(r)


# ================================================================ 对外的两个入口
# 为什么放在模块级而不是 `BrowsePage` 的方法上：第七十八轮起有**三个**地方要
# 走这套流程 —— 下载完的自动核对、下载失败时的「选本地文件」、以及
# 「游戏管理 → 游戏目录 → 汉化补丁」。挂在下载页的实例方法上，游戏目录那边
# 就得先造一个 BrowsePage（那是一整个页面，代价离谱），只能复制代码。
def review_rows(parent, pack_name, pack_ver, rows, src_dir, dest_dir,
                lang_ver=None, page_url="") -> bool:
    """摊开核对 → 确认后覆盖进 `dest_dir`。返回是否真的写进去了。

    **所有来源共用这一个**：自己从 BBSMC 下的、玩家手动选的本地 zip、
    游戏目录里点「汉化补丁」。复制成几份迟早跑偏（谁多删了临时目录、
    谁的汇总算法不一样），玩家看到的就会莫名其妙地不一致。
    """
    scan = scan_rows(rows, dest_dir)
    if not scan:
        Toast.show_toast(parent, "汉化包里没有可用的文件", kind="error")
        shutil.rmtree(src_dir, ignore_errors=True)
        return False
    d = LangPackDialog(parent, pack_name, pack_ver, lang_ver or {}, scan,
                       src_dir, dest_dir, page_url)
    d.exec()
    ok = bool(getattr(d, "ok", False))
    shutil.rmtree(src_dir, ignore_errors=True)
    if ok:
        Toast.show_toast(
            parent, f"汉化已覆盖进「{pack_name}」，启动游戏即可看到中文"[:60],
            ms=5000)
    return ok


def open_local_zip(parent, dest_dir, pack_name="整合包", pack_ver="",
                   page_url="", start_dir="") -> bool:
    """选一个本地 zip → 校验 → 解包 → 文件夹式核对 → 覆盖进 `dest_dir`。

    「游戏目录 → 汉化补丁」和「下载失败 → 选择本地文件」都走这里。
    """
    import os as _os

    from ..core import bbsmc
    from . import fspicker

    base = ""
    try:
        from ..core import installer
        base = installer.downloads_dir()
    except Exception:                                  # noqa: BLE001
        base = ""

    p = fspicker.pick_file(parent, "选择汉化补丁（zip）",
                           start_dir or base, ["*.zip"])
    if not p:
        return False
    if not bbsmc.is_zip(p):
        Toast.show_toast(parent, "这个文件不是压缩包", ms=3600, kind="error")
        return False
    # 不像汉化包只提醒一句、**不拦着**：玩家可能拿到的是别的打包写法，
    # 后面核对窗会把文件摊开，他自己一眼就能判断
    if not bbsmc.looks_like_langpack(p):
        Toast.show_toast(
            parent, "这个包里没看到 config/kubejs 之类的汉化文件，"
                    "请先确认选对了", ms=5000, kind="error")

    tmp_dir = _os.path.join(base or _os.path.dirname(p), "_langtmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        rows = bbsmc.unpack(p, tmp_dir)
    except Exception as e:                             # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        Toast.show_toast(parent, f"解压失败：{str(e)[:80]}", ms=4500,
                         kind="error")
        return False
    if not rows:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        Toast.show_toast(parent, "这个压缩包里没有文件", ms=3600, kind="error")
        return False
    lang_ver = {"name": _os.path.basename(p),
                "file_size": _os.path.getsize(p) if _os.path.isfile(p) else 0}
    return review_rows(parent, pack_name, pack_ver, rows, tmp_dir, dest_dir,
                       lang_ver=lang_ver, page_url=page_url)
